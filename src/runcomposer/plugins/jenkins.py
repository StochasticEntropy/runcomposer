"""`jenkins` Runner (DESIGN.md §6.2b): triggers an existing parameterized
Jenkins job and takes its results back. Built on ``runcomposer.sdk.jenkins``
— the client a connector composes when this runner's switches are not enough.

**What travels to the job** is configured, not fixed: ``parameters`` maps each
build-parameter name of *your* job to a template. Available fields:

- the document: ``{spec_b64}`` (the exact document bytes, gzip + base64 — one
  line of ASCII that survives any form or shell; decode with
  ``base64 -d | gunzip``), ``{spec_url}`` (where the job can download the
  exact bytes; needs ``callback_base``) and ``{spec_json}`` (the document as
  text — for small specs only: a build parameter is one form field and one
  environment variable of the build, both limited in size);
- identity: ``{run_id}``, ``{dispatch_id}``, ``{title}``, ``{labels[key]}``;
- the way back: ``{callback_url}`` and ``{token}`` (the ingest token for this
  dispatch — bind it to a password parameter);
- the work: ``{item_ids}`` (one per line), ``{shards}`` (comma-separated) and
  ``{params[name]}`` (per-run parameters declared under ``run_parameters``);
- your own: every name under ``field_providers`` — a ``"pkg.mod:callable"``
  called with the context and the fields above, returning the text of the
  field. It may ``ctx.refuse`` when the job cannot express the run.

A trigger whose parameters exceed ``max_parameter_bytes`` (one value) or
``max_request_bytes`` (all of them) is refused before anything is sent.

**Shards.** ``shards`` names what the build will report separately — a list,
or a template rendering to a comma- or newline-separated one
(``"{params[regions]}"``). The job pushes each with ``shard=<name>``, or
leaves each one's files where ``artifact_paths`` / ``result_urls`` point —
``{shard}`` in those is replaced by the shard's name.

**How results come back** is ``completion``:

- ``callback`` (default): the job POSTs its bundle to ``{callback_url}`` with
  the token. ``dispatch`` returns once the build is queued; the run waits in
  ``AWAITING_RESULTS`` until the delivery lands.
- ``poll``: for a Jenkins that cannot call out. runcomposer follows the queue
  item to the build, waits for it to finish, downloads the archived files
  matching ``artifact_paths`` and the build-relative URLs listed under
  ``result_urls`` (what a publisher serves without archiving it), runs
  ``postprocess`` over them if one is configured, and delivers them.

The build is identified through the queue item the trigger returns — never by
"the latest build of the job", which may be somebody else's — and, after
Jenkins has forgotten the queue item, by the queue id the build records.
"""

from __future__ import annotations

import fnmatch
import string
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from runcomposer.sdk import (
    ConfigError,
    DispatchCancelled,
    DispatchContext,
    Health,
    PlanContext,
    PlanPreview,
    PluginError,
    RunnerBase,
    RunnerInfo,
    load_import_path,
    parameters_from_config,
)
from runcomposer.sdk.jenkins import JenkinsClient, JenkinsError, JenkinsTimeout, encode_spec

__all__ = ["JenkinsRunner"]

DEFAULT_PARAMETERS = {
    "SPEC_JSON": "{spec_json}",
    "DISPATCH_ID": "{dispatch_id}",
    "CALLBACK_URL": "{callback_url}",
    "INGEST_TOKEN": "{token}",
}
TEMPLATE_FIELDS = (
    "spec_json", "spec_b64", "spec_url", "run_id", "dispatch_id", "callback_url", "token",
    "item_ids", "title", "shards", "labels", "params",
)
_INDEXED = ("labels", "params")
_AVAILABLE = (
    "{spec_b64} {spec_url} {spec_json} {run_id} {dispatch_id} {callback_url} {token} "
    "{item_ids} {title} {shards} {labels[key]} {params[name]}"
)
#: Shown in a plan preview for what only exists once a dispatch does.
_AT_DISPATCH = "‹at dispatch›"


class _Lenient(dict[str, Any]):
    """A missing label or parameter renders as nothing, not as a KeyError."""

    def __missing__(self, key: str) -> Any:
        return ""


def _fields_of(template: str, where: str) -> list[str]:
    try:
        return [field for _, field, _, _ in string.Formatter().parse(template) if field]
    except ValueError as exc:
        raise ConfigError(f"jenkins: {where}: {exc}") from None


def _check_template(where: str, template: Any, extra: tuple[str, ...] = ()) -> str:
    if not isinstance(template, str):
        raise ConfigError(f"jenkins: {where} must be a template string")
    for field in _fields_of(template, where):
        root = field.split("[", 1)[0].split(".", 1)[0]
        known = root in TEMPLATE_FIELDS or root in extra
        if not known or (root in _INDEXED) != ("[" in field):
            more = "".join(f" {{{name}}}" for name in extra)
            raise ConfigError(
                f"jenkins: {where} uses {{{field}}} — available: {_AVAILABLE}{more}"
            )
    return template


def _load(ref: Any, option: str) -> Callable[..., Any]:
    try:
        target = load_import_path(str(ref))
    except PluginError as exc:
        raise ConfigError(f"jenkins: {option}: {exc}") from None
    if not callable(target):
        raise ConfigError(f"jenkins: {option}: {ref!r} is not callable")
    loaded: Callable[..., Any] = target
    return loaded


class JenkinsRunner(RunnerBase):
    PATH_OPTIONS = ("ca_bundle",)

    def __init__(
        self,
        base_url: str,
        job: str,
        parameters: Mapping[str, str] | None = None,
        callback_base: str | None = None,
        completion: str = "callback",
        shards: list[str] | str | None = None,
        artifact_paths: list[str] | None = None,
        result_urls: list[str] | None = None,
        field_providers: Mapping[str, str] | None = None,
        postprocess: str | None = None,
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
        max_parameter_bytes: int | None = 128_000,
        max_request_bytes: int | None = 1_000_000,
    ):
        if completion not in ("callback", "poll"):
            raise ConfigError(f"jenkins: completion must be 'callback' or 'poll', got {completion!r}")
        if not job or not isinstance(job, str):
            raise ConfigError("jenkins: 'job' is required")
        try:
            self.client = JenkinsClient(
                str(base_url),
                user=user,
                api_token=api_token,
                verify_tls=verify_tls,
                ca_bundle=ca_bundle,
                request_timeout_s=request_timeout_s,
                retries=retries,
                retry_backoff_s=retry_backoff_s,
            )
        except ValueError as exc:
            raise ConfigError(f"jenkins: {exc}") from None
        self.base_url = self.client.base_url
        self.job = job
        self.job_url = self.client.job_url(job)
        self.field_providers = {str(name): str(ref) for name, ref in (field_providers or {}).items()}
        for name, ref in self.field_providers.items():
            if not name.isidentifier() or name in TEMPLATE_FIELDS or name == "shard":
                raise ConfigError(
                    f"jenkins: field_providers.{name}: a field name is an identifier that is "
                    f"not one of the built-in fields {list(TEMPLATE_FIELDS)}"
                )
            module, sep, attr = ref.partition(":")
            if not sep or not module or not attr:
                raise ConfigError(
                    f"jenkins: field_providers.{name} is an import path "
                    f"'package.module:callable', got {ref!r}"
                )
        provided = tuple(self.field_providers)
        self.parameters = {
            str(name): _check_template(f"parameters.{name}", template, provided)
            for name, template in (parameters or DEFAULT_PARAMETERS).items()
        }
        self.callback_base = (callback_base or "").rstrip("/") or None
        self.completion = completion
        if shards is None:
            self.shards: list[str] | str = ["1"]
        elif isinstance(shards, str):
            self.shards = _check_template("shards", shards)
            if "shards" in _fields_of(shards, "shards"):
                raise ConfigError("jenkins: shards cannot be computed from {shards}")
        elif isinstance(shards, (list, tuple)) and shards and all(str(s).strip() for s in shards):
            self.shards = [str(shard).strip() for shard in shards]
            if len(set(self.shards)) != len(self.shards):
                raise ConfigError(f"jenkins: shards must be distinct, got {self.shards}")
        else:
            raise ConfigError(
                "jenkins: shards is a non-empty list of names, or a template rendering to a "
                "comma-separated list (\"{params[regions]}\")"
            )
        self.artifact_paths = [
            str(p) for p in (artifact_paths if artifact_paths is not None else
                             ([] if result_urls else ["results/*"]))
        ]
        self.result_urls = [str(p).lstrip("/") for p in (result_urls or [])]
        for url in self.result_urls:
            if "://" in url or ".." in url.split("/"):
                raise ConfigError(
                    f"jenkins: result_urls entries are paths below the build's URL, got {url!r}"
                )
        self.postprocess = str(postprocess) if postprocess else None
        if self.postprocess:
            module, sep, attr = self.postprocess.partition(":")
            if not sep or not module or not attr:
                raise ConfigError(
                    "jenkins: postprocess is an import path 'package.module:callable', "
                    f"got {postprocess!r}"
                )
        self.format = format
        self.user = user
        self.poll_interval_s = float(poll_interval_s)
        self.timeout_s = float(timeout_s)
        self.run_parameters = parameters_from_config(run_parameters, "jenkins: run_parameters")
        for name, limit in (("max_parameter_bytes", max_parameter_bytes),
                            ("max_request_bytes", max_request_bytes)):
            if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
                raise ConfigError(f"jenkins: {name} must be a positive integer or null (no limit)")
        self.max_parameter_bytes = max_parameter_bytes
        self.max_request_bytes = max_request_bytes

    # -- self-description -----------------------------------------------------------

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="jenkins",
            title="Jenkins",
            description=f"Triggers the Jenkins job {self.job!r}; results return by "
            + ("callback." if self.completion == "callback" else "polling the build."),
            capabilities=("external_delivery", "cancel", "plan", self.completion),
            parameters=self.run_parameters,
            result_format=self.format,
        )

    def health(self) -> Health:
        data: dict[str, Any] = {"job_url": self.job_url, "completion": self.completion}
        try:
            data["version"] = self.client.version()
        except Exception as exc:
            return Health(status="down", detail=f"Jenkins unreachable: {exc}", data=data)
        try:
            readable = self.client.job_exists(self.job)
            problem = None if readable else "404 Not Found"
        except Exception as exc:
            readable, problem = False, str(exc)
        data["checks"] = [
            {"check": "server reachable", "ok": True, "detail": f"Jenkins {data['version']}".strip()},
            {"check": f"job {self.job!r} readable", "ok": readable, "detail": problem or self.job_url},
        ]
        if not readable:
            return Health(
                status="degraded", detail=f"job {self.job!r} not readable: {problem}", data=data
            )
        return Health(status="ok", detail=f"Jenkins {data['version']}".strip(), data=data)

    # -- what would be sent ---------------------------------------------------------------

    def _callback_url(self, ctx: PlanContext) -> str:
        if self.completion != "callback":
            return ""
        if not self.callback_base:
            ctx.refuse(
                "jenkins completion=callback needs the callback_base option (the URL under "
                "which the job can reach this runcomposer's API)"
            )
        return f"{self.callback_base}/api/v1/runs/{ctx.run_id}/results"

    def _uses(self, field: str) -> bool:
        """Does any template (a parameter, the shard list) use ``field``?"""
        templates = [*self.parameters.values()]
        if isinstance(self.shards, str):
            templates.append(self.shards)
        return any(field in _fields_of(template, "template") for template in templates)

    def _render(
        self, ctx: PlanContext, *, dispatch_id: str, token: str
    ) -> tuple[list[str], dict[str, str]]:
        """``(shards, build parameters)`` for this run. Everything a trigger
        can be refused for without talking to Jenkins is refused here."""
        run = ctx.spec.get("run") or {}
        spec_url = ""
        if self._uses("spec_url"):
            if not self.callback_base:
                ctx.refuse(
                    "jenkins: a parameter uses {spec_url}, which needs the callback_base option "
                    "(the URL under which the job can reach this runcomposer's API)"
                )
            spec_url = f"{self.callback_base}/api/v1/runs/{ctx.run_id}/dispatches/{dispatch_id}/spec"
        document = ctx.export_spec() if self._uses_document() else b""
        fields = _Lenient(
            spec_json=document.decode("utf-8") if self._uses("spec_json") else "",
            spec_b64=encode_spec(document) if self._uses("spec_b64") else "",
            spec_url=spec_url,
            run_id=ctx.run_id,
            dispatch_id=dispatch_id,
            callback_url=self._callback_url(ctx),
            token=token,
            item_ids="\n".join(ctx.item_ids),
            title=str(run.get("title") or ""),
            labels=_Lenient({str(k): str(v) for k, v in (run.get("labels") or {}).items()}),
            params=_Lenient({str(k): _text(v) for k, v in ctx.parameters.items()}),
        )
        formatter = string.Formatter()
        if isinstance(self.shards, str):
            rendered = formatter.vformat(self.shards, (), fields)
            shards = [part.strip() for part in rendered.replace("\n", ",").split(",") if part.strip()]
            if not shards:
                ctx.refuse(f"jenkins: shards ({self.shards!r}) rendered to nothing for this run")
            if len(set(shards)) != len(shards):
                ctx.refuse(f"jenkins: shards ({self.shards!r}) rendered to duplicates: {shards}")
        else:
            shards = list(self.shards)
        fields["shards"] = ",".join(shards)
        for name, ref in self.field_providers.items():
            value = _load(ref, f"field_providers.{name}")(ctx, dict(fields))
            if not isinstance(value, str):
                raise TypeError(
                    f"jenkins: field provider {ref} returned {type(value).__name__}, not text"
                )
            fields[name] = value
        build_parameters = {
            name: formatter.vformat(template, (), fields)
            for name, template in self.parameters.items()
        }
        self._check_sizes(ctx, build_parameters)
        if self.completion == "poll" and len(shards) > 1 and not self.postprocess and not any(
            "{shard}" in path for path in (*self.artifact_paths, *self.result_urls)
        ):
            ctx.refuse(
                f"jenkins: {len(shards)} shards with completion: poll need a way to tell their "
                "results apart — put {shard} into artifact_paths / result_urls, or configure a "
                "postprocess that splits what was downloaded"
            )
        return shards, build_parameters

    def _uses_document(self) -> bool:
        return self._uses("spec_json") or self._uses("spec_b64") or self._uses("spec_url")

    def _check_sizes(self, ctx: PlanContext, build_parameters: Mapping[str, str]) -> None:
        hint = (
            " — pass the document as {spec_b64} (compressed) or {spec_url} (downloaded by the "
            "job) instead of {spec_json}, or raise the limit if your server and agents take more"
        )
        if self.max_parameter_bytes is not None:
            for name, value in build_parameters.items():
                size = len(value.encode("utf-8"))
                if size > self.max_parameter_bytes:
                    ctx.refuse(
                        f"jenkins: build parameter {name} would be {size} bytes, more than "
                        f"max_parameter_bytes ({self.max_parameter_bytes}): a build parameter is "
                        "also an environment variable of the build" + hint
                    )
        if self.max_request_bytes is not None:
            total = len(urllib.parse.urlencode(dict(build_parameters)).encode("utf-8"))
            if total > self.max_request_bytes:
                ctx.refuse(
                    f"jenkins: the trigger request would be {total} bytes, more than "
                    f"max_request_bytes ({self.max_request_bytes})" + hint
                )

    def plan(self, ctx: PlanContext) -> PlanPreview:
        shards, build_parameters = self._render(ctx, dispatch_id=_AT_DISPATCH, token=_AT_DISPATCH)
        shown = {
            name: value if len(value) <= 200 else f"‹{len(value.encode('utf-8'))} bytes›"
            for name, value in build_parameters.items()
        }
        lines = [self._plan_text(shards)]
        lines.extend(f"  {name} = {value}" for name, value in shown.items())
        return PlanPreview(
            text="\n".join(lines),
            shards={shard: [] for shard in shards},
            notes=(
                "the job distributes the items over its shards itself — a preview cannot say "
                "which item goes where",
                "nothing was sent to Jenkins; the token and the dispatch id exist only once "
                "the run is dispatched",
            ),
            data={
                "job_url": self.job_url,
                "completion": self.completion,
                "build_parameters": shown,
                "request_bytes": len(urllib.parse.urlencode(build_parameters).encode("utf-8")),
            },
        )

    def _plan_text(self, shards: list[str], *, triggered: bool = False) -> str:
        return (
            f"jenkins: job {self.job_url} {'triggered' if triggered else 'would be triggered'}, "
            f"{len(shards)} shard(s) {shards}; completion by "
            + ("callback POST" if self.completion == "callback" else "polling the build")
        )

    # -- dispatch ---------------------------------------------------------------------

    def dispatch(self, ctx: DispatchContext) -> None:
        token = (ctx.ingest_token() or "") if self._uses("token") else ""
        shards, build_parameters = self._render(ctx, dispatch_id=ctx.dispatch_id, token=token)
        ctx.raise_if_cancelled()
        try:
            queue_url = self.client.trigger(self.job, build_parameters)
        except Exception as exc:
            ctx.refuse(f"Jenkins trigger failed for {self.job_url}: {exc}")
        if ctx.cancelled:
            # Cancelled while the trigger was under way: nothing was handed
            # off, so nobody would stop this build later — take it back now.
            if queue_url:
                try:
                    ctx.log("jenkins: cancelled during the trigger — "
                            + self.client.cancel(self.job, queue_url))
                except Exception as exc:
                    ctx.log(f"jenkins: the triggered build could not be taken back: {exc}",
                            level="warning")
            raise DispatchCancelled("cancelled while the Jenkins build was being triggered")
        # The trigger was accepted: that is the hand-off.
        links = {"job": self.job_url}
        if queue_url:
            links["queue"] = queue_url
        ctx.declare(shards, plan=self._plan_text(shards, triggered=True), links=links)
        if self.completion == "poll":
            self._poll_and_deliver(ctx, queue_url, shards)

    # -- polling --------------------------------------------------------------------------

    def _poll_and_deliver(
        self, ctx: DispatchContext, queue_url: str | None, shards: list[str]
    ) -> None:
        if not queue_url:
            raise RuntimeError(
                "Jenkins accepted the trigger but returned no queue location, so the build "
                "cannot be identified — use completion: callback with this server"
            )
        def started(url: str) -> None:
            # Known from here on: a cancel — also one made after a restart,
            # with a context rebuilt from the record — goes straight to it.
            ctx.set_link("build", url)
            ctx.add_artifact("Jenkins build", url, media_type="text/html")
            ctx.log(f"jenkins: build started — {url}")

        try:
            build = self.client.follow(
                self.job, queue_url, timeout_s=self.timeout_s,
                poll_interval_s=self.poll_interval_s, check=ctx.raise_if_cancelled,
                on_build=started,
            )
            build_url = str(build["url"])
        except JenkinsTimeout as exc:
            raise RuntimeError(
                f"the Jenkins build did not {exc.what} within timeout_s={self.timeout_s:g}"
            ) from None
        except JenkinsError as exc:
            raise RuntimeError(str(exc)) from None
        if build.get("result") == "ABORTED":
            raise RuntimeError(f"the Jenkins build was aborted: {build_url}")
        archived = [str(artifact["relativePath"]) for artifact in build.get("artifacts") or []]
        # A job may push results itself while it is polled: what has
        # delivered needs no download.
        outstanding = [shard for shard in shards if shard in set(ctx.outstanding_shards())]
        problems: list[str] = []
        for shard in outstanding:
            bundle = ctx.workdir / "results" if len(shards) == 1 else ctx.workdir / "results" / shard
            fetched = self._download(ctx, build_url, archived, shard, bundle)
            if not fetched:
                problems.append(
                    f"shard {shard}: the build ({build.get('result')}) left nothing matching "
                    f"{self._wanted(shard)}: {build_url}/"
                    if len(shards) > 1
                    else f"the build ({build.get('result')}) archived nothing matching "
                    f"{self._wanted(shard)}: {build_url}/artifact/"
                )
                continue
            delivered: Path = bundle
            if self.postprocess:
                changed = _load(self.postprocess, "postprocess")(ctx, shard, bundle)
                if changed is not None:
                    delivered = Path(changed)
            for file in fetched:
                if file.is_file():
                    ctx.add_artifact(
                        file.name if len(shards) == 1 else f"{file.name} ({shard})", file,
                        shard=shard if len(shards) > 1 else None,
                    )
            ctx.deliver(shard, bundle=delivered, format=self.format)
        if problems:
            raise RuntimeError("; ".join(problems))

    def _wanted(self, shard: str) -> list[str]:
        return [
            path.replace("{shard}", shard) for path in (*self.artifact_paths, *self.result_urls)
        ]

    def _download(
        self, ctx: DispatchContext, build_url: str, archived: list[str], shard: str, bundle: Path
    ) -> list[Path]:
        """Everything of one shard: archived artifacts matching
        ``artifact_paths``, and the ``result_urls`` below the build."""
        patterns = [path.replace("{shard}", shard) for path in self.artifact_paths]
        fetched: list[Path] = []
        for relative in archived:
            if any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns):
                fetched.append(
                    self.client.download_artifact(build_url, relative, bundle / Path(relative).name)
                )
        for url in self.result_urls:
            relative = url.replace("{shard}", shard)
            name = Path(urllib.parse.urlsplit(relative).path).name or "result"
            try:
                fetched.append(self.client.download_build_file(build_url, relative, bundle / name))
            except JenkinsError as exc:
                if exc.status != 404:
                    raise
                ctx.log(f"jenkins: {build_url}/{relative} does not exist", level="warning")
        return fetched

    # -- cancel -----------------------------------------------------------------------------

    def cancel(self, ctx: DispatchContext) -> None:
        """Stop the build behind this dispatch: the build itself when its URL
        is known, else whatever the queue item is by now — a running build
        (also when Jenkins has forgotten the queue item) or a queued one."""
        build_url, queue_url = ctx.links.get("build"), ctx.links.get("queue")
        if build_url:
            self.client.stop_build(build_url)
            ctx.log(f"jenkins: stop requested for {build_url}")
            return
        if not queue_url:
            ctx.log("jenkins: no queue location recorded — the build cannot be stopped from here",
                    level="warning")
            return
        ctx.log("jenkins: " + self.client.cancel(self.job, queue_url))


def _text(value: Any) -> str:
    """A per-run parameter as the text of a template field: a list joined by
    commas, a mapping as ``name=value`` lines."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(entry) for entry in value)
    if isinstance(value, Mapping):
        return "\n".join(f"{key}={entry}" for key, entry in value.items())
    return str(value)
