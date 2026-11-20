"""`jenkins` Runner (DESIGN.md §6.2b): triggers an existing parameterized
Jenkins job and takes its results back.

**What travels to the job** is configured, not fixed: ``parameters`` maps each
build-parameter name of *your* job to a template. Available fields:
``{spec_json}`` (the exact document bytes — their hash is recorded, so the
marker written from them verifies on the way back), ``{run_id}``,
``{dispatch_id}``, ``{callback_url}``, ``{token}`` (the ingest token for this
dispatch — bind it to a password parameter), ``{item_ids}`` (one per line),
``{title}``, ``{labels[key]}`` and ``{params[name]}`` (per-run parameters
declared under ``run_parameters``).

**How results come back** is ``completion``:

- ``callback`` (default): the job POSTs its bundle to ``{callback_url}`` with
  the token. ``dispatch`` returns once the build is queued; the run waits in
  ``AWAITING_RESULTS`` until the delivery lands.
- ``poll``: for a Jenkins that cannot call out. runcomposer follows the queue
  item to the build, waits for it to finish, downloads the archived files
  matching ``artifact_paths`` and delivers them.

The build is identified through the queue item the trigger returns — never by
"the latest build of the job", which may be somebody else's.

HTTP is stdlib urllib on purpose (no dependencies): credentials from the
environment (``api_token: ${env:JENKINS_TOKEN}``), TLS verification with an
optional CA bundle, per-request timeouts, and a bounded retry on transient
errors.
"""

from __future__ import annotations

import base64
import fnmatch
import http.cookiejar
import json
import ssl
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runcomposer.sdk import (
    ConfigError,
    DispatchContext,
    Health,
    RunnerBase,
    RunnerInfo,
    parameters_from_config,
)

__all__ = ["JenkinsRunner"]

DEFAULT_PARAMETERS = {
    "SPEC_JSON": "{spec_json}",
    "DISPATCH_ID": "{dispatch_id}",
    "CALLBACK_URL": "{callback_url}",
    "INGEST_TOKEN": "{token}",
}
TEMPLATE_FIELDS = (
    "spec_json", "run_id", "dispatch_id", "callback_url", "token", "item_ids", "title",
    "labels", "params",
)
_TRANSIENT = (429, 502, 503, 504)


class _Lenient(dict[str, Any]):
    """A missing label or parameter renders as nothing, not as a KeyError."""

    def __missing__(self, key: str) -> Any:
        return ""


def _check_template(name: str, template: Any) -> str:
    if not isinstance(template, str):
        raise ConfigError(f"jenkins: parameters.{name} must be a template string")
    try:
        fields = [field for _, field, _, _ in string.Formatter().parse(template) if field]
    except ValueError as exc:
        raise ConfigError(f"jenkins: parameters.{name}: {exc}") from None
    for field in fields:
        root = field.split("[", 1)[0].split(".", 1)[0]
        if root not in TEMPLATE_FIELDS or (root in ("labels", "params")) != ("[" in field):
            raise ConfigError(
                f"jenkins: parameters.{name} uses {{{field}}} — available: "
                "{spec_json} {run_id} {dispatch_id} {callback_url} {token} {item_ids} {title} "
                "{labels[key]} {params[name]}"
            )
    return template


class _Transient(Exception):
    """An error worth one more try."""


class JenkinsRunner(RunnerBase):
    PATH_OPTIONS = ("ca_bundle",)

    def __init__(
        self,
        base_url: str,
        job: str,
        parameters: Mapping[str, str] | None = None,
        callback_base: str | None = None,
        completion: str = "callback",
        artifact_paths: list[str] | None = None,
        format: str | None = None,
        user: str | None = None,
        api_token: str | None = None,
        verify_tls: bool = True,
        ca_bundle: str | None = None,
        request_timeout_s: float = 30.0,
        retries: int = 3,
        retry_backoff_s: float = 1.0,
        poll_interval_s: float = 5.0,
        timeout_s: float = 3600.0,
        run_parameters: list[Mapping[str, Any]] | None = None,
    ):
        if not str(base_url).startswith(("http://", "https://")):
            raise ConfigError("jenkins: base_url must start with http:// or https://")
        if completion not in ("callback", "poll"):
            raise ConfigError(f"jenkins: completion must be 'callback' or 'poll', got {completion!r}")
        if not job or not isinstance(job, str):
            raise ConfigError("jenkins: 'job' is required")
        self.base_url = str(base_url).rstrip("/")
        self.job = job
        self.job_url = self.base_url + "".join(
            "/job/" + urllib.parse.quote(part, safe="") for part in job.strip("/").split("/")
        )
        self.parameters = {
            str(name): _check_template(str(name), template)
            for name, template in (parameters or DEFAULT_PARAMETERS).items()
        }
        self.callback_base = (callback_base or "").rstrip("/") or None
        self.completion = completion
        self.artifact_paths = [str(p) for p in (artifact_paths or ["results/*"])]
        self.format = format
        self.user = user
        self._api_token = api_token
        self.request_timeout_s = float(request_timeout_s)
        self.retries = max(0, int(retries))
        self.retry_backoff_s = float(retry_backoff_s)
        self.poll_interval_s = float(poll_interval_s)
        self.timeout_s = float(timeout_s)
        self.run_parameters = parameters_from_config(run_parameters, "jenkins: run_parameters")
        context: ssl.SSLContext | None = None
        if self.base_url.startswith("https://"):
            context = ssl.create_default_context(cafile=ca_bundle)
            if not verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
        # Jenkins binds CSRF crumbs to the web session: the crumb request and
        # the POST that uses it must share cookies.
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)
        self._crumb: tuple[str, str] | None = None
        self._crumb_known = False
        self._lock = threading.Lock()

    # -- self-description -----------------------------------------------------------

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="jenkins",
            title="Jenkins",
            description=f"Triggers the Jenkins job {self.job!r}; results return by "
            + ("callback." if self.completion == "callback" else "polling the build."),
            capabilities=("external_delivery", "cancel", self.completion),
            parameters=self.run_parameters,
            result_format=self.format,
        )

    def health(self) -> Health:
        try:
            with self._request("GET", f"{self.base_url}/api/json?tree=mode", retry=False) as response:
                version = response.headers.get("X-Jenkins", "")
        except Exception as exc:
            return Health(status="down", detail=f"Jenkins unreachable: {exc}")
        try:
            with self._request("GET", f"{self.job_url}/api/json?tree=name", retry=False):
                pass
        except Exception as exc:
            return Health(
                status="degraded", detail=f"job {self.job!r} not readable: {exc}",
                data={"version": version},
            )
        return Health(status="ok", detail=f"Jenkins {version}".strip(), data={"version": version})

    # -- dispatch ---------------------------------------------------------------------

    def dispatch(self, ctx: DispatchContext) -> None:
        callback = ""
        if self.completion == "callback":
            if not self.callback_base:
                ctx.refuse(
                    "jenkins completion=callback needs the callback_base option (the URL under "
                    "which the job can reach this runcomposer's API)"
                )
            callback = f"{self.callback_base}/api/v1/runs/{ctx.run_id}/results"
        run = ctx.spec.get("run") or {}
        fields = _Lenient(
            spec_json=ctx.export_spec().decode("utf-8"),
            run_id=ctx.run_id,
            dispatch_id=ctx.dispatch_id,
            callback_url=callback,
            token=(ctx.ingest_token() or "") if any(
                "{token}" in template for template in self.parameters.values()
            ) else "",
            item_ids="\n".join(ctx.item_ids),
            title=str(run.get("title") or ""),
            labels=_Lenient({str(k): str(v) for k, v in (run.get("labels") or {}).items()}),
            params=_Lenient({str(k): _text(v) for k, v in ctx.parameters.items()}),
        )
        formatter = string.Formatter()
        build_parameters = {
            name: formatter.vformat(template, (), fields)
            for name, template in self.parameters.items()
        }
        try:
            queue_url = self._trigger(build_parameters)
        except Exception as exc:
            ctx.refuse(f"Jenkins trigger failed for {self.job_url}: {exc}")
        # The trigger was accepted: that is the hand-off.
        links = {"job": self.job_url}
        if queue_url:
            links["queue"] = queue_url
        ctx.declare(
            ["1"],
            plan=f"jenkins: job {self.job_url} triggered; completion by "
            + ("callback POST" if self.completion == "callback" else "polling the build"),
            links=links,
        )
        if self.completion == "poll":
            self._poll_and_deliver(ctx, queue_url)

    def _trigger(self, build_parameters: Mapping[str, str]) -> str | None:
        data = urllib.parse.urlencode(build_parameters).encode("utf-8")
        with self._request(
            "POST", f"{self.job_url}/buildWithParameters", data=data, retry_unsent_only=True
        ) as response:
            location = response.headers.get("Location")
        return str(location).rstrip("/") + "/" if location else None

    # -- polling --------------------------------------------------------------------------

    def _poll_and_deliver(self, ctx: DispatchContext, queue_url: str | None) -> None:
        if not queue_url:
            raise RuntimeError(
                "Jenkins accepted the trigger but returned no queue location, so the build "
                "cannot be identified — use completion: callback with this server"
            )
        deadline = time.monotonic() + self.timeout_s
        build_url = self._wait(ctx, deadline, lambda: self._build_of(queue_url), "start")
        ctx.add_artifact("Jenkins build", build_url, media_type="text/html")
        ctx.log(f"jenkins: build started — {build_url}")

        def finished() -> dict[str, Any] | None:
            build = self._get_json(
                f"{build_url}/api/json?tree=building,result,artifacts[relativePath]"
            )
            return build if not build.get("building") and build.get("result") else None

        build = self._wait(ctx, deadline, finished, "finish")
        if build.get("result") == "ABORTED":
            raise RuntimeError(f"the Jenkins build was aborted: {build_url}")
        wanted = [
            artifact["relativePath"]
            for artifact in build.get("artifacts") or []
            if any(fnmatch.fnmatchcase(artifact["relativePath"], p) for p in self.artifact_paths)
        ]
        if not wanted:
            raise RuntimeError(
                f"the build ({build.get('result')}) archived nothing matching "
                f"{self.artifact_paths}: {build_url}artifact/"
            )
        bundle = ctx.workdir / "results"
        for relative in wanted:
            target = bundle / Path(relative).name
            target.parent.mkdir(parents=True, exist_ok=True)
            quoted = urllib.parse.quote(relative)
            with self._request("GET", f"{build_url}/artifact/{quoted}") as response:
                target.write_bytes(response.read())
            ctx.add_artifact(Path(relative).name, target)
        ctx.deliver("1", bundle=bundle, format=self.format)

    def _wait(self, ctx: DispatchContext, deadline: float, probe: Any, what: str) -> Any:
        while True:
            ctx.raise_if_cancelled()
            value = probe()
            if value:
                return value
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"the Jenkins build did not {what} within timeout_s={self.timeout_s:g}"
                )
            time.sleep(self.poll_interval_s)

    def _build_of(self, queue_url: str) -> str | None:
        item = self._get_json(queue_url.rstrip("/") + "/api/json")
        if item.get("cancelled"):
            raise RuntimeError(f"the queued Jenkins build was cancelled: {queue_url}")
        executable = item.get("executable") or {}
        return str(executable["url"]).rstrip("/") if executable.get("url") else None

    # -- cancel -----------------------------------------------------------------------------

    def cancel(self, ctx: DispatchContext) -> None:
        """Stop the build behind this dispatch: the running build if the queue
        item already became one, else the queue item itself."""
        queue_url = ctx.links.get("queue")
        if not queue_url:
            ctx.log("jenkins: no queue location recorded — the build cannot be stopped from here",
                    level="warning")
            return
        build_url = None
        try:
            build_url = self._build_of(queue_url)
        except Exception:
            pass
        if build_url:
            with self._request("POST", f"{build_url}/stop", data=b""):
                pass
            ctx.log(f"jenkins: stop requested for {build_url}")
            return
        item_id = queue_url.rstrip("/").rsplit("/", 1)[-1]
        with self._request("POST", f"{self.base_url}/queue/cancelItem?id={item_id}", data=b""):
            pass
        ctx.log(f"jenkins: queue item {item_id} cancelled")

    # -- HTTP plumbing ------------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        headers = {"User-Agent": "runcomposer-jenkins"}
        if self.user and self._api_token:
            raw = f"{self.user}:{self._api_token}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return headers

    def _crumb_header(self) -> dict[str, str]:
        with self._lock:
            if not self._crumb_known:
                try:
                    request = urllib.request.Request(
                        f"{self.base_url}/crumbIssuer/api/json", headers=self._auth_headers()
                    )
                    with self._opener.open(request, timeout=self.request_timeout_s) as response:
                        body = json.loads(response.read().decode("utf-8"))
                    self._crumb = (body["crumbRequestField"], body["crumb"])
                except (urllib.error.URLError, ValueError, KeyError, OSError):
                    self._crumb = None  # no crumb issuer (or not reachable: the POST will say)
                self._crumb_known = True
            return {self._crumb[0]: self._crumb[1]} if self._crumb else {}

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        retry: bool = True,
        retry_unsent_only: bool = False,
    ) -> Any:
        """One HTTP exchange with bounded retry. ``retry_unsent_only`` is for
        the trigger: it is repeated only when the request provably did not
        take effect (connection refused, or an explicit "try later" status) —
        a timeout after sending could mean a queued build."""
        attempts = (self.retries if retry else 0) + 1
        last: Exception | None = None
        for attempt in range(attempts):
            headers = self._auth_headers()
            if method == "POST":
                headers.update(self._crumb_header())
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                return self._opener.open(request, timeout=self.request_timeout_s)
            except urllib.error.HTTPError as exc:
                if exc.code == 403 and method == "POST" and self._crumb_known and attempt == 0:
                    with self._lock:  # an expired crumb: fetch a fresh one once
                        self._crumb_known = False
                    last = exc
                    continue
                if exc.code not in _TRANSIENT:
                    raise RuntimeError(f"{exc.code} {exc.reason} for {method} {url}") from None
                last = exc
            except urllib.error.URLError as exc:
                refused = isinstance(exc.reason, ConnectionRefusedError)
                if retry_unsent_only and not refused:
                    raise RuntimeError(f"{exc.reason} for {method} {url}") from None
                last = exc
            except TimeoutError as exc:
                if retry_unsent_only:
                    raise RuntimeError(f"timed out: {method} {url}") from None
                last = exc
            if attempt + 1 < attempts:
                time.sleep(self.retry_backoff_s * (2**attempt))
        reason = getattr(last, "reason", last)
        raise RuntimeError(f"{reason} for {method} {url} (after {attempts} attempt(s))")

    def _get_json(self, url: str) -> dict[str, Any]:
        with self._request("GET", url) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not isinstance(body, dict):
            raise RuntimeError(f"unexpected answer from {url}")
        return body


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(entry) for entry in value)
    return str(value)
