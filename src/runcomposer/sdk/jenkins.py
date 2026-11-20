"""runcomposer.sdk.jenkins — the Jenkins toolkit.

:class:`JenkinsClient` is the HTTP client the reference ``jenkins`` runner is
made of, for a connector that drives a Jenkins job in a way of its own. It
covers what a runner needs from a Jenkins server and nothing else:

- **session and crumb** — Jenkins binds a CSRF crumb to the web session, so
  the crumb request and the POST using it share cookies; an expired crumb is
  fetched again once;
- **bounded retry** on transient errors (429, 502, 503, 504, a refused or
  timed-out connection), with the one exception a trigger needs: it is
  repeated only when the request provably did not take effect;
- **TLS** verification with an optional CA bundle;
- **trigger → queue item → build**: the build is identified through the queue
  item the trigger returns — never by "the latest build of the job", which
  may be somebody else's — and, once Jenkins has forgotten the queue item (it
  does a few minutes after the build started), by the queue id the build
  itself records;
- **stop / cancel** of a build or a queued item, and ``follow`` — a build
  from queue item to its end that is taken back when the dispatch is
  cancelled meanwhile;
- **results**: the archived artifacts of a build, and any other URL below
  the build (what a publisher plugin serves without archiving it).

Stdlib ``urllib`` on purpose — no dependency. Credentials are a user and an
API token (Basic auth); an unauthenticated server works without them.

Also here: :func:`encode_spec` / :func:`decode_spec`, the compressed form a
run spec travels in as a build parameter.
"""

from __future__ import annotations

import base64
import fnmatch
import gzip
import http.cookiejar
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "JenkinsClient",
    "JenkinsError",
    "JenkinsTimeout",
    "decode_spec",
    "encode_spec",
    "queue_id_of",
]

_TRANSIENT = (429, 502, 503, 504)


class JenkinsError(RuntimeError):
    """A request Jenkins did not answer as needed. ``status`` is the HTTP
    status when there was one (``404`` for something Jenkins no longer
    knows), else ``None`` (unreachable, timed out)."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class JenkinsTimeout(JenkinsError):
    """A wait ran out: the build did not ``what`` (``"start"``, ``"finish"``)
    within ``timeout_s`` seconds."""

    def __init__(self, what: str, timeout_s: float):
        super().__init__(f"the Jenkins build did not {what} within {timeout_s:g}s")
        self.what = what
        self.timeout_s = timeout_s


def encode_spec(content: bytes) -> str:
    """Bytes as gzip + base64 text: the form in which a document survives
    being a build parameter — one line of ASCII that no form encoding, shell
    or text widget can alter, and several times smaller than the JSON. The
    receiving side restores the exact bytes with :func:`decode_spec`, or in a
    shell with ``printf %s "$SPEC_B64" | base64 -d | gunzip > spec.json``.
    Deterministic: the same bytes always encode to the same text."""
    return base64.b64encode(gzip.compress(content, mtime=0)).decode("ascii")


def decode_spec(text: str) -> bytes:
    """The inverse of :func:`encode_spec`."""
    return gzip.decompress(base64.b64decode(text.strip()))


def queue_id_of(queue_url: str) -> int | None:
    """The numeric id in a queue item URL (``…/queue/item/42/``)."""
    tail = queue_url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


class JenkinsClient:
    """One Jenkins server, as a runner talks to it. Thread-safe; build one
    per configured runner and keep it (it holds the session)."""

    def __init__(
        self,
        base_url: str,
        *,
        user: str | None = None,
        api_token: str | None = None,
        verify_tls: bool = True,
        ca_bundle: str | None = None,
        request_timeout_s: float = 30.0,
        retries: int = 3,
        retry_backoff_s: float = 1.0,
        user_agent: str = "runcomposer-jenkins",
    ):
        if not str(base_url).startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        self.base_url = str(base_url).rstrip("/")
        self.user = user
        self._api_token = api_token
        self.request_timeout_s = float(request_timeout_s)
        self.retries = max(0, int(retries))
        self.retry_backoff_s = float(retry_backoff_s)
        self.user_agent = user_agent
        context: ssl.SSLContext | None = None
        if self.base_url.startswith("https://"):
            context = ssl.create_default_context(cafile=ca_bundle)
            if not verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)
        self._crumb: tuple[str, str] | None = None
        self._crumb_known = False
        self._lock = threading.Lock()

    # -- addresses ---------------------------------------------------------------------

    def job_url(self, job: str) -> str:
        """The URL of a job given as ``folder/name`` (each segment quoted)."""
        return self.base_url + "".join(
            "/job/" + urllib.parse.quote(part, safe="") for part in job.strip("/").split("/")
        )

    # -- the server ----------------------------------------------------------------------

    def version(self) -> str:
        """The server's version (the ``X-Jenkins`` header). Raises
        :class:`JenkinsError` when the server cannot be reached."""
        with self.request("GET", f"{self.base_url}/api/json?tree=mode", retry=False) as response:
            return str(response.headers.get("X-Jenkins", ""))

    def job_exists(self, job: str) -> bool:
        """Whether the job is readable with these credentials. Raises for
        anything but "yes" and "no such job"."""
        try:
            with self.request("GET", f"{self.job_url(job)}/api/json?tree=name", retry=False):
                return True
        except JenkinsError as exc:
            if exc.status == 404:
                return False
            raise

    # -- trigger → queue item → build -------------------------------------------------------

    def trigger(self, job: str, parameters: Mapping[str, str]) -> str | None:
        """Start a parameterized build; returns the queue item's URL (with a
        trailing slash), or ``None`` when the server did not say where it
        queued the build. Never repeated after the request may have been
        received — a second build would be worse than an error."""
        data = urllib.parse.urlencode(dict(parameters)).encode("utf-8")
        with self.request(
            "POST", f"{self.job_url(job)}/buildWithParameters", data=data, retry_unsent_only=True
        ) as response:
            location = response.headers.get("Location")
        return str(location).rstrip("/") + "/" if location else None

    def build_of(self, queue_url: str) -> str | None:
        """The URL of the build a queue item became (no trailing slash), or
        ``None`` while it is still queued. Raises :class:`JenkinsError` when
        the queued item was cancelled, and with ``status == 404`` once
        Jenkins has forgotten the item — then :meth:`find_build` still finds
        the build."""
        item = self.get_json(queue_url.rstrip("/") + "/api/json")
        if item.get("cancelled"):
            raise JenkinsError(f"the queued Jenkins build was cancelled: {queue_url}")
        executable = item.get("executable") or {}
        return str(executable["url"]).rstrip("/") if executable.get("url") else None

    def find_build(self, job: str, queue_id: int, *, depth: int = 50) -> str | None:
        """The build of ``job`` that came from queue item ``queue_id``, looked
        up among the job's last ``depth`` builds by the queue id each build
        records. This is the way to a build after its queue item expired."""
        tree = f"builds[number,url,queueId]{{0,{int(depth)}}}"
        listing = self.get_json(
            f"{self.job_url(job)}/api/json?tree=" + urllib.parse.quote(tree, safe="[],{}")
        )
        for build in listing.get("builds") or []:
            if build.get("queueId") == queue_id and build.get("url"):
                return str(build["url"]).rstrip("/")
        return None

    def locate_build(self, job: str, queue_url: str) -> str | None:
        """:meth:`build_of`, falling back to :meth:`find_build` when the
        queue item is gone. ``None`` while the build has not started."""
        try:
            return self.build_of(queue_url)
        except JenkinsError as exc:
            if exc.status != 404:
                raise
        queue_id = queue_id_of(queue_url)
        return self.find_build(job, queue_id) if queue_id is not None else None

    def build_info(self, build_url: str, tree: str = "building,result,number,url") -> dict[str, Any]:
        """The build's API document, restricted to ``tree``."""
        return self.get_json(
            f"{build_url.rstrip('/')}/api/json?tree=" + urllib.parse.quote(tree, safe="[],{}")
        )

    def wait(
        self,
        probe: Callable[[], Any],
        *,
        what: str,
        timeout_s: float,
        poll_interval_s: float = 5.0,
        check: Callable[[], None] | None = None,
    ) -> Any:
        """Poll ``probe`` until it returns something true and return that.
        ``check`` is called before every probe and may raise to give up (pass
        ``ctx.raise_if_cancelled``). Raises :class:`JenkinsTimeout` after
        ``timeout_s``, naming ``what`` was waited for."""
        deadline = time.monotonic() + timeout_s
        while True:
            if check is not None:
                check()
            value = probe()
            if value:
                return value
            if time.monotonic() >= deadline:
                raise JenkinsTimeout(what, timeout_s)
            time.sleep(poll_interval_s)

    def wait_for_build(
        self,
        job: str,
        queue_url: str,
        *,
        timeout_s: float,
        poll_interval_s: float = 5.0,
        check: Callable[[], None] | None = None,
    ) -> str:
        """Wait until the queue item became a build; returns its URL."""
        return str(
            self.wait(
                lambda: self.locate_build(job, queue_url),
                what="start", timeout_s=timeout_s, poll_interval_s=poll_interval_s, check=check,
            )
        )

    def wait_until_finished(
        self,
        build_url: str,
        *,
        timeout_s: float,
        poll_interval_s: float = 5.0,
        check: Callable[[], None] | None = None,
        tree: str = "building,result,artifacts[relativePath]",
    ) -> dict[str, Any]:
        """Wait until the build is over; returns its API document (``result``
        is ``SUCCESS``, ``UNSTABLE``, ``FAILURE``, ``ABORTED`` …)."""

        def finished() -> dict[str, Any] | None:
            build = self.build_info(build_url, tree)
            return build if not build.get("building") and build.get("result") else None

        result: dict[str, Any] = self.wait(
            finished, what="finish", timeout_s=timeout_s, poll_interval_s=poll_interval_s,
            check=check,
        )
        return result

    def follow(
        self,
        job: str,
        queue_url: str,
        *,
        timeout_s: float,
        poll_interval_s: float = 5.0,
        check: Callable[[], None] | None = None,
        on_build: Callable[[str], None] | None = None,
        tree: str = "building,result,artifacts[relativePath]",
    ) -> dict[str, Any]:
        """A triggered build from queue item to its end: wait until the queue
        item became a build, tell ``on_build(build_url)`` (record the link
        there — ``ctx.set_link("build", url)``), wait until the build is
        over, and return its API document with ``"url"`` set to the build's.

        ``check`` may raise to give up (pass ``ctx.raise_if_cancelled``). It
        is called **once before anything else, and if it raises there the
        build is taken back** — stopped, or taken out of the queue — before
        the exception goes on. That closes the one window a runner's
        ``cancel`` cannot: a cancel that arrived while the trigger was still
        under way found no link to stop anything by, so without this the
        build would run on for a cancelled dispatch. After that it is called
        before every probe, where a cancellation has already been acted on
        by the runner's ``cancel`` (the link was there), and once more when
        the build is over. A timeout leaves the build alone and raises
        :class:`JenkinsTimeout`."""
        if check is not None:
            try:
                check()
            except BaseException:
                try:
                    self.cancel(job, queue_url)
                except JenkinsError:
                    pass  # already gone, or unreachable: nothing more to do here
                raise
        build_url = self.wait_for_build(
            job, queue_url, timeout_s=timeout_s, poll_interval_s=poll_interval_s, check=check
        )
        if on_build is not None:
            on_build(build_url)
        build = self.wait_until_finished(
            build_url, timeout_s=timeout_s, poll_interval_s=poll_interval_s, check=check, tree=tree
        )
        if check is not None:
            check()
        return {**build, "url": build_url}

    # -- stopping ------------------------------------------------------------------------------

    def stop_build(self, build_url: str) -> None:
        """Ask a running build to stop (it ends ``ABORTED``)."""
        with self.request("POST", f"{build_url.rstrip('/')}/stop", data=b""):
            pass

    def cancel_queue_item(self, queue_id: int) -> None:
        """Take a build that has not started out of the queue."""
        with self.request(
            "POST", f"{self.base_url}/queue/cancelItem?id={int(queue_id)}", data=b""
        ):
            pass

    def cancel(self, job: str, queue_url: str) -> str:
        """Stop whatever the queue item is by now: the running build if it
        became one — found through the queue item, or by its queue id once
        the item expired — else the queued item. Returns what was done, for
        a log line."""
        build_url = None
        try:
            build_url = self.locate_build(job, queue_url)
        except JenkinsError:
            pass  # a cancelled item, an unreachable server: try the queue below
        if build_url:
            self.stop_build(build_url)
            return f"stop requested for {build_url}"
        queue_id = queue_id_of(queue_url)
        if queue_id is None:
            raise JenkinsError(f"{queue_url} is not a queue item URL — nothing to cancel")
        self.cancel_queue_item(queue_id)
        return f"queue item {queue_id} cancelled"

    # -- results ---------------------------------------------------------------------------------

    def artifacts(self, build_url: str, patterns: Sequence[str] = ("*",)) -> list[str]:
        """The relative paths of the build's archived artifacts matching any
        of the glob ``patterns`` (matched against the whole relative path)."""
        build = self.build_info(build_url, "artifacts[relativePath]")
        return [
            str(artifact["relativePath"])
            for artifact in build.get("artifacts") or []
            if any(fnmatch.fnmatchcase(str(artifact["relativePath"]), p) for p in patterns)
        ]

    def download_artifact(self, build_url: str, relative_path: str, target: str | Path) -> Path:
        """One archived artifact, saved to ``target``."""
        return self.download(
            f"{build_url.rstrip('/')}/artifact/{urllib.parse.quote(relative_path)}", target
        )

    def download_build_file(self, build_url: str, relative_url: str, target: str | Path) -> Path:
        """Any URL below the build, saved to ``target`` — for results a
        publisher serves without archiving them (``robot/report/output.xml``,
        ``testReport/api/json``)."""
        return self.download(f"{build_url.rstrip('/')}/{relative_url.lstrip('/')}", target)

    def download(self, url: str, target: str | Path) -> Path:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.request("GET", url) as response:
            path.write_bytes(response.read())
        return path

    # -- HTTP plumbing ---------------------------------------------------------------------------

    def get_json(self, url: str) -> dict[str, Any]:
        with self.request("GET", url) as response:
            try:
                body = json.loads(response.read().decode("utf-8"))
            except ValueError:
                raise JenkinsError(f"unexpected answer from {url} (not JSON)") from None
        if not isinstance(body, dict):
            raise JenkinsError(f"unexpected answer from {url}")
        return body

    def _auth_headers(self) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent}
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

    def request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        retry: bool = True,
        retry_unsent_only: bool = False,
    ) -> Any:
        """One HTTP exchange with bounded retry; returns the open response
        (use it as a context manager). ``retry_unsent_only`` is for requests
        that must not happen twice: they are repeated only when they provably
        did not take effect (connection refused, or an explicit "try later"
        status) — a timeout after sending could mean it did."""
        attempts = (self.retries if retry else 0) + 1
        last: Exception | None = None
        status: int | None = None
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
                    last, status = exc, exc.code
                    continue
                if exc.code not in _TRANSIENT:
                    raise JenkinsError(
                        f"{exc.code} {exc.reason} for {method} {url}", status=exc.code
                    ) from None
                last, status = exc, exc.code
            except urllib.error.URLError as exc:
                refused = isinstance(exc.reason, ConnectionRefusedError)
                if retry_unsent_only and not refused:
                    raise JenkinsError(f"{exc.reason} for {method} {url}") from None
                last, status = exc, None
            except TimeoutError as exc:
                if retry_unsent_only:
                    raise JenkinsError(f"timed out: {method} {url}") from None
                last, status = exc, None
            if attempt + 1 < attempts:
                time.sleep(self.retry_backoff_s * (2**attempt))
        reason = last.reason if isinstance(last, urllib.error.URLError) else last
        raise JenkinsError(
            f"{reason} for {method} {url} (after {attempts} attempt(s))", status=status
        )
