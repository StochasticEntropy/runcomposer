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
  cancelled meanwhile, and that can ride out the server being away for a
  while (``outage_tolerance_s``: a restart, a proxy that drops connections);
- **the job's declared parameters** (``job_parameters``: the names;
  ``job_parameter_definitions``: name, kind, choices and default of each, as
  :class:`JobParameter`) — Jenkins ignores a build parameter its job does
  not declare, silently, and does not start a build whose choice parameter
  gets a value it does not list, so this is how a runner finds out before
  anybody wonders why the job did not get it;
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
import http.client
import http.cookiejar
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "JenkinsClient",
    "JenkinsError",
    "JenkinsQueueCancelled",
    "JenkinsTimeout",
    "JobParameter",
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


class JenkinsQueueCancelled(JenkinsError):
    """The queued item was cancelled before it became a build — by somebody
    in Jenkins, or by :meth:`JenkinsClient.cancel`. A :class:`JenkinsError`
    with ``status`` ``None`` like an unreachable server, so it has a class of
    its own: it is an answer, and final — ``follow`` never waits it out as
    an outage. ``queue_url`` is the item's URL."""

    def __init__(self, queue_url: str):
        super().__init__(f"the queued Jenkins build was cancelled: {queue_url}")
        self.queue_url = queue_url


class JenkinsTimeout(JenkinsError):
    """A wait ran out: the build did not ``what`` (``"start"``, ``"finish"``)
    within ``timeout_s`` seconds."""

    def __init__(self, what: str, timeout_s: float):
        super().__init__(f"the Jenkins build did not {what} within {timeout_s:g}s")
        self.what = what
        self.timeout_s = timeout_s


@dataclass(frozen=True)
class JobParameter:
    """One build parameter a Jenkins job declares
    (:meth:`JenkinsClient.job_parameter_definitions`).

    - ``name`` — the parameter's name.
    - ``kind`` — Jenkins's type of it without ``ParameterDefinition``, in
      lower case: ``string``, ``text``, ``choice``, ``boolean``,
      ``password``, or whatever a plugin adds; ``""`` when the server did
      not say.
    - ``choices`` — what a ``choice`` parameter lists, in its order (empty
      for the other kinds).
    - ``default`` — the default value the job declares, ``None`` when it
      shows none (a password parameter never does).
    - ``description`` — the job's description of it.
    """

    name: str
    kind: str = ""
    choices: tuple[str, ...] = ()
    default: Any = None
    description: str = ""

    def accepts(self, value: str) -> bool:
        """Whether a trigger with ``value`` for this parameter starts a
        build: a ``choice`` parameter takes only what it lists; every other
        kind takes any text."""
        return self.kind != "choice" or str(value) in self.choices


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

    def job_parameters(self, job: str) -> list[str] | None:
        """The names of the build parameters the job declares, in its order,
        or ``None`` when the job is not parameterized at all. Raises
        :class:`JenkinsError` (``status == 404``) for a job that does not
        exist or is not readable with these credentials. One attempt, as
        :meth:`job_exists` — it is what a health check asks.

        Worth asking because Jenkins drops a build parameter its job does not
        declare without a word: the trigger succeeds, the build runs, and the
        value never arrives. The definitions are read where current servers
        keep them (the job's ``ParametersDefinitionProperty``) and where older
        ones did (an action of the job)."""
        definitions = self.job_parameter_definitions(job)
        return [definition.name for definition in definitions] if definitions else None

    def job_parameter_definitions(self, job: str) -> list[JobParameter] | None:
        """The build parameters the job declares, in its order, each with its
        kind, choices, default and description (:class:`JobParameter`) —
        ``None`` when the job is not parameterized at all. Errors and the
        single attempt as :meth:`job_parameters`.

        What it adds to the names: a ``choice`` parameter refuses a value it
        does not list, and Jenkins then starts no build — ``definition.
        accepts(value)`` says beforehand whether a value will be taken."""
        fields = "name,type,description,choices,defaultParameterValue[value]"
        tree = f"property[parameterDefinitions[{fields}]],actions[parameterDefinitions[{fields}]]"
        document = self.get_json(
            f"{self.job_url(job)}/api/json?tree=" + urllib.parse.quote(tree, safe="[],{}"),
            retry=False,
        )
        found: dict[str, JobParameter] = {}
        for holder in [*(document.get("property") or []), *(document.get("actions") or [])]:
            if not isinstance(holder, Mapping):
                continue
            for definition in holder.get("parameterDefinitions") or []:
                if not isinstance(definition, Mapping) or not definition.get("name"):
                    continue
                name = str(definition["name"])
                if name not in found:
                    found[name] = _job_parameter(name, definition)
        return list(found.values()) or None

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

    def build_of(self, queue_url: str, *, retry: bool = True) -> str | None:
        """The URL of the build a queue item became (no trailing slash), or
        ``None`` while it is still queued. Raises
        :class:`JenkinsQueueCancelled` when the queued item was cancelled,
        and :class:`JenkinsError` with ``status == 404`` once Jenkins has
        forgotten the item — then :meth:`find_build` still finds the build.
        ``retry=False``: one attempt instead of the client's bounded retry
        (as for every lookup below)."""
        item = self.get_json(queue_url.rstrip("/") + "/api/json", retry=retry)
        if item.get("cancelled"):
            raise JenkinsQueueCancelled(queue_url)
        executable = item.get("executable") or {}
        return str(executable["url"]).rstrip("/") if executable.get("url") else None

    def find_build(
        self, job: str, queue_id: int, *, depth: int = 50, retry: bool = True
    ) -> str | None:
        """The build of ``job`` that came from queue item ``queue_id``, looked
        up among the job's last ``depth`` builds by the queue id each build
        records. This is the way to a build after its queue item expired."""
        tree = f"builds[number,url,queueId]{{0,{int(depth)}}}"
        listing = self.get_json(
            f"{self.job_url(job)}/api/json?tree=" + urllib.parse.quote(tree, safe="[],{}"),
            retry=retry,
        )
        for build in listing.get("builds") or []:
            if build.get("queueId") == queue_id and build.get("url"):
                return str(build["url"]).rstrip("/")
        return None

    def locate_build(self, job: str, queue_url: str, *, retry: bool = True) -> str | None:
        """:meth:`build_of`, falling back to :meth:`find_build` when the
        queue item is gone. ``None`` while the build has not started."""
        try:
            return self.build_of(queue_url, retry=retry)
        except JenkinsError as exc:
            if exc.status != 404:
                raise
        queue_id = queue_id_of(queue_url)
        return self.find_build(job, queue_id, retry=retry) if queue_id is not None else None

    def build_info(
        self, build_url: str, tree: str = "building,result,number,url", *, retry: bool = True
    ) -> dict[str, Any]:
        """The build's API document, restricted to ``tree``."""
        return self.get_json(
            f"{build_url.rstrip('/')}/api/json?tree=" + urllib.parse.quote(tree, safe="[],{}"),
            retry=retry,
        )

    def wait(
        self,
        probe: Callable[[], Any],
        *,
        what: str,
        timeout_s: float,
        poll_interval_s: float = 5.0,
        check: Callable[[], None] | None = None,
        outage_tolerance_s: float | None = None,
        on_outage: OutageCallback | None = None,
    ) -> Any:
        """Poll ``probe`` until it returns something true and return that.
        ``check`` is called before every probe and may raise to give up (pass
        ``ctx.raise_if_cancelled``). Raises :class:`JenkinsTimeout` after
        ``timeout_s``, naming ``what`` was waited for.

        ``outage_tolerance_s`` lets the wait ride out a server that is away
        for a while: a probe that fails with an *outage* — no answer at all
        (refused, dropped, timed out) or a transient status (429, 502, 503,
        504) — is tried again at the next poll, as long as the server last
        answered less than ``outage_tolerance_s`` seconds ago (counted from
        the start of the wait until it first answers). What is measured is
        the time since the last answer, never a count of errors: failures
        with answers in between do not add up. Past the tolerance the wait
        raises a :class:`JenkinsError` that says for how long nothing
        answered. Anything else — a 404, a 401, a cancelled queue item — is
        an answer and raised at once, as is everything without a tolerance
        (``None`` or ``0``). The tolerance does not extend ``timeout_s``.
        The probe's own retries come on top: a probe for a tolerant wait
        should make one attempt (``retry=False``), as the waits of this
        client do.

        ``on_outage(answering, seconds)`` is told of each change while the
        wait rides out an outage: once with ``False`` when the server first
        fails to answer (seconds since its last answer), once with ``True``
        on its first answer after that (seconds nothing answered) — so a
        runner can say in its log that the server was away, and for how
        long, instead of waiting silently."""
        return self._wait(
            probe, what=what, timeout_s=timeout_s, poll_interval_s=poll_interval_s,
            check=check, answers=_Answers(outage_tolerance_s, on_outage),
        )

    def _wait(
        self,
        probe: Callable[[], Any],
        *,
        what: str,
        timeout_s: float,
        poll_interval_s: float,
        check: Callable[[], None] | None,
        answers: _Answers,
    ) -> Any:
        deadline = time.monotonic() + timeout_s
        while True:
            if check is not None:
                check()
            try:
                value = probe()
            except JenkinsError as exc:
                answers.failed(exc)  # raises unless it is an outage within the tolerance
            else:
                answers.answered()
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
        outage_tolerance_s: float | None = None,
        on_outage: OutageCallback | None = None,
    ) -> str:
        """Wait until the queue item became a build; returns its URL.
        ``outage_tolerance_s`` and ``on_outage`` as for :meth:`wait`."""
        return self._wait_for_build(
            job, queue_url, timeout_s=timeout_s, poll_interval_s=poll_interval_s, check=check,
            answers=_Answers(outage_tolerance_s, on_outage),
        )

    def _wait_for_build(
        self,
        job: str,
        queue_url: str,
        *,
        timeout_s: float,
        poll_interval_s: float,
        check: Callable[[], None] | None,
        answers: _Answers,
    ) -> str:
        retry = not answers.tolerant  # with a tolerance, that is the retry policy
        return str(
            self._wait(
                lambda: self.locate_build(job, queue_url, retry=retry),
                what="start", timeout_s=timeout_s, poll_interval_s=poll_interval_s, check=check,
                answers=answers,
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
        outage_tolerance_s: float | None = None,
        on_outage: OutageCallback | None = None,
    ) -> dict[str, Any]:
        """Wait until the build is over; returns its API document (``result``
        is ``SUCCESS``, ``UNSTABLE``, ``FAILURE``, ``ABORTED`` …).
        ``outage_tolerance_s`` and ``on_outage`` as for :meth:`wait`."""
        return self._wait_until_finished(
            build_url, timeout_s=timeout_s, poll_interval_s=poll_interval_s, check=check,
            tree=tree, answers=_Answers(outage_tolerance_s, on_outage),
        )

    def _wait_until_finished(
        self,
        build_url: str,
        *,
        timeout_s: float,
        poll_interval_s: float,
        check: Callable[[], None] | None,
        tree: str,
        answers: _Answers,
    ) -> dict[str, Any]:
        retry = not answers.tolerant

        def finished() -> dict[str, Any] | None:
            build = self.build_info(build_url, tree, retry=retry)
            return build if not build.get("building") and build.get("result") else None

        result: dict[str, Any] = self._wait(
            finished, what="finish", timeout_s=timeout_s, poll_interval_s=poll_interval_s,
            check=check, answers=answers,
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
        outage_tolerance_s: float | None = None,
        on_outage: OutageCallback | None = None,
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
        :class:`JenkinsTimeout`; ``timeout_s`` applies to each of the two
        waits (to start, to finish). A queue item cancelled in Jenkins raises
        :class:`JenkinsQueueCancelled`.

        ``outage_tolerance_s`` lets the follow ride out the server being
        away — a restart, a proxy dropping connections — for up to that many
        seconds since it last answered, across both waits (see :meth:`wait`
        for what counts as an outage). ``on_build`` is still told exactly
        once, ``check`` still runs before every probe (a cancel during an
        outage is noticed; stopping the build then may fail like any request
        to a server that is away), and neither timeout starts again. With a
        tolerance every probe is a single attempt — the tolerance is the
        retry policy, so the client's own retries do not multiply the time a
        probe may take. ``on_outage`` is told when the server stops
        answering and when it answers again, across both waits (see
        :meth:`wait`)."""
        if check is not None:
            try:
                check()
            except BaseException:
                try:
                    self.cancel(job, queue_url)
                except JenkinsError:
                    pass  # already gone, or unreachable: nothing more to do here
                raise
        answers = _Answers(outage_tolerance_s, on_outage)  # one measure across both waits
        build_url = self._wait_for_build(
            job, queue_url, timeout_s=timeout_s, poll_interval_s=poll_interval_s, check=check,
            answers=answers,
        )
        if on_build is not None:
            on_build(build_url)
        build = self._wait_until_finished(
            build_url, timeout_s=timeout_s, poll_interval_s=poll_interval_s, check=check,
            tree=tree, answers=answers,
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
            try:
                content = response.read()
            except (OSError, http.client.HTTPException) as exc:
                raise JenkinsError(f"the download of {url} broke off: {exc}") from None
        path.write_bytes(content)
        return path

    # -- HTTP plumbing ---------------------------------------------------------------------------

    def get_json(self, url: str, *, retry: bool = True) -> dict[str, Any]:
        """GET a JSON object. ``retry=False``: one attempt."""
        with self.request("GET", url, retry=retry) as response:
            try:
                body = json.loads(response.read().decode("utf-8"))
            except ValueError:
                raise JenkinsError(f"unexpected answer from {url} (not JSON)") from None
            except (OSError, http.client.HTTPException) as exc:
                raise JenkinsError(f"the answer from {url} broke off: {exc}") from None
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
            except (http.client.HTTPException, ConnectionError) as exc:
                # The connection was dropped after the request went out (a
                # restarting server, a proxy): no answer — and, for a
                # request that must not happen twice, maybe received.
                if retry_unsent_only:
                    raise JenkinsError(
                        f"the connection was dropped: {method} {url} ({exc!r})"
                    ) from None
                last, status = exc, None
            if attempt + 1 < attempts:
                time.sleep(self.retry_backoff_s * (2**attempt))
        reason = last.reason if isinstance(last, urllib.error.URLError) else last
        raise JenkinsError(
            f"{reason} for {method} {url} (after {attempts} attempt(s))", status=status
        )


#: ``on_outage(answering, seconds)``: ``False`` when the server first stops
#: answering a tolerant wait (seconds since its last answer), ``True`` on its
#: first answer after that (seconds nothing answered).
OutageCallback = Callable[[bool, float], None]


class _Answers:
    """When the server last answered, for a wait that tolerates outages —
    and whether it is away now, to tell ``on_outage`` of each change."""

    def __init__(self, tolerance_s: float | None, on_outage: OutageCallback | None = None):
        self.tolerance_s = float(tolerance_s or 0)
        self.last = time.monotonic()
        self.on_outage = on_outage
        self.away = False

    @property
    def tolerant(self) -> bool:
        return self.tolerance_s > 0

    def answered(self) -> None:
        now = time.monotonic()
        if self.away:
            self.away = False
            if self.on_outage is not None:
                self.on_outage(True, now - self.last)
        self.last = now

    def failed(self, exc: JenkinsError) -> None:
        """Return to wait on, or raise: ``exc`` itself when it is not an
        outage (or nothing is tolerated), an error saying for how long the
        server has not answered when the tolerance is used up."""
        outage = not isinstance(exc, (JenkinsQueueCancelled, JenkinsTimeout)) and (
            exc.status is None or exc.status in _TRANSIENT
        )
        if not self.tolerant or not outage:
            raise exc
        silent = time.monotonic() - self.last
        if not self.away:
            self.away = True
            if self.on_outage is not None:
                self.on_outage(False, silent)
        if silent >= self.tolerance_s:
            raise JenkinsError(
                f"Jenkins has not answered for {silent:.1f}s, longer than "
                f"outage_tolerance_s={self.tolerance_s:g}: {exc}",
                status=exc.status,
            ) from exc


def _job_parameter(name: str, definition: Mapping[str, Any]) -> JobParameter:
    """A parameter definition of a job's ``api/json`` as a :class:`JobParameter`."""
    kind = str(definition.get("type") or "")
    if kind.endswith("ParameterDefinition"):
        kind = kind[: -len("ParameterDefinition")]
    choices = definition.get("choices")
    default = definition.get("defaultParameterValue")
    return JobParameter(
        name=name,
        kind=kind.lower(),
        choices=tuple(str(choice) for choice in choices) if isinstance(choices, list) else (),
        default=default.get("value") if isinstance(default, Mapping) else None,
        description=str(definition.get("description") or ""),
    )
