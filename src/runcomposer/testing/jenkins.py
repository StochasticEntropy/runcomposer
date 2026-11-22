"""runcomposer.testing.jenkins — a Jenkins stand-in for testing a runner that
drives Jenkins (one built on ``runcomposer.sdk.jenkins``, or the reference
``jenkins`` runner with your configuration and callables).

``FakeJenkins`` is a real HTTP server on a local port that speaks the part of
Jenkins' remote API such a runner uses, with the behaviours that matter for
correctness:

- a CSRF crumb bound to a session cookie — a POST without the matching pair
  is a 403, exactly as on a real instance;
- ``buildWithParameters`` answers 201 with a ``Location`` naming a *queue
  item*, which only later points at a build — and which can be forgotten
  (``expire``), after which the build is found by the queue id it records;
- a build has ``building`` / ``result`` / ``artifacts[relativePath]``, serves
  its archived artifacts and whatever else a job puts below its URL
  (``pages``: a console text, a publisher's report), and can be stopped; a
  queue item can be cancelled;
- a job may declare its build parameters — names, or definitions with a
  kind, choices, a default and a description, served in the job's
  ``api/json`` as a real server serves them — and then, as on a real
  server, a trigger's form field the job does not declare is dropped
  without a word;
- optional Basic authentication; injected faults, stalled answers, and a
  server that is down for a while (``down`` / ``up``).

::

    from runcomposer.testing.jenkins import Build, FakeJenkins

    def job(build: Build) -> None:                  # what the job "does"
        build.pages["consoleText"] = b"=== results eu ===\n…"
        build.artifacts["reports/junit.xml"] = b"<testsuite …/>"

    fake = FakeJenkins()
    fake.add_job("shop-nightly", job, parameters=["SPEC_B64", "REGIONS"])
    try:
        ...  # point your runner at fake.url; read fake.triggers(), fake.builds
    finally:
        fake.close()

What it covers is the runner's side of the protocol. What it cannot cover is
a real Jenkins' own behaviour (plugin versions, a job's shell environment);
runcomposer checks its reference runner against a real server as well
(``ci/jenkins/e2e.py`` in its repository).

Stdlib only, and — like the rest of the kit — never imports pytest.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

__all__ = ["Build", "FakeJenkins", "QueueItem", "Seen"]


@dataclass
class Seen:
    """One request the stand-in received."""

    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    form: dict[str, str]


@dataclass
class Build:
    """A build of the stand-in — what a job callable is handed, and what a
    test reads afterwards. A job sets ``artifacts`` (archived files by
    relative path), ``pages`` (anything else served below the build's URL)
    and, to end other than ``SUCCESS``, ``result``; ``params`` are the build
    parameters of the trigger the job accepted (a job that declares its
    parameters gets those only); ``stop`` is set when the build was asked to
    stop (a job that holds waits on it)."""

    job: str
    number: int
    params: dict[str, str]
    url: str
    building: bool = True
    result: str | None = None
    artifacts: dict[str, bytes] = field(default_factory=dict)
    #: What the build serves below its own URL without archiving it — a
    #: publisher's report, the console text: ``{relative path: content}``.
    pages: dict[str, bytes] = field(default_factory=dict)
    queue_id: int = 0
    stop: threading.Event = field(default_factory=threading.Event)


@dataclass
class QueueItem:
    """A queue item: it becomes a build when an executor takes it."""

    number: int
    job: str
    params: dict[str, str]
    cancelled: bool = False
    build: Build | None = None


class FakeJenkins:
    """An in-process Jenkins that speaks the protocol a runner uses — see the
    module docstring. ``fake.url`` is its base URL; ``fake.add_job(name,
    job)`` registers a job whose builds run ``job(build)`` (a
    :class:`Build`) on a thread; ``fake.close()`` stops it.

    - ``crumbs=False`` — no crumb issuer; ``auth=(user, token)`` — Basic
      authentication required; ``auto_start=False`` — builds stay queued
      until ``fake.start(queue_number)``.
    - ``fake.add_job(name, job, parameters=[names])`` — a job that declares
      its build parameters: the job's ``api/json`` lists them (as a
      ``ParametersDefinitionProperty``). An entry may also be a definition,
      ``{"name": "SPEED", "kind": "choice", "choices": ["normal", "fast"],
      "default": "normal", "description": "…"}`` (``kind`` defaults to
      ``string``; ``text``, ``boolean``, ``password`` … as Jenkins names its
      types), served with its type, choices and default as Jenkins serves
      them; as on a real server, a trigger with a value a choice parameter
      does not list is answered ``500`` and queues nothing. A trigger's form
      field it does
      not declare is dropped silently, as a real server does — it never
      reaches the queue item or ``Build.params``, and is recorded in
      ``fake.ignored_parameters`` as ``(job, name)``. Without
      ``parameters=`` the job is not parameterized and takes every field.
    - ``fake.expire(queue_number)`` forgets a queue item (404 from then on),
      as Jenkins does by itself some minutes after the item left the queue.
    - ``fake.faults`` — ``(method, path fragment, status)`` answered once;
      ``fake.stalls`` — ``{path fragment: event}`` an answer waits for.
    - ``fake.down(status=503)`` — every request is answered with ``status``
      (``status=None``: the connection is closed without an answer) until
      ``fake.up()``: a server restarting, a proxy in front of one that is
      away. Builds go on meanwhile, as they do on a real restart's agents.
    - What happened: ``fake.seen`` (every request, also those answered while
      down), ``fake.triggers()``, ``fake.posts(fragment)``,
      ``fake.gets(fragment)``, ``fake.queue``, ``fake.builds``
      (``{(job, number): Build}``), ``fake.ignored_parameters``.
    """

    #: The Jenkins version it answers as: the one the reference runner is
    #: checked against on a real server (the image pinned in ci/jenkins).
    VERSION = "2.541.3"
    CRUMB_FIELD = "Jenkins-Crumb"

    def __init__(
        self,
        *,
        crumbs: bool = True,
        auth: tuple[str, str] | None = None,
        auto_start: bool = True,
    ):
        self.crumbs = crumbs
        self.auth = auth
        self.auto_start = auto_start
        self.send_location = True
        self.jobs: dict[str, Callable[[Build], None] | None] = {}
        #: job → the names of the build parameters it declares (``None``: not
        #: parameterized).
        self.job_parameters: dict[str, list[str] | None] = {}
        #: job → the definitions of its build parameters, as given to add_job.
        self.job_parameter_definitions: dict[str, list[dict[str, Any]] | None] = {}
        #: ``(job, name)`` of every form field a trigger sent that the job
        #: does not declare — dropped, as a real server drops it.
        self.ignored_parameters: list[tuple[str, str]] = []
        #: While down: the status every request is answered with (``None``:
        #: the connection is closed without an answer).
        self._down: tuple[int | None] | None = None
        self.queue: dict[int, QueueItem] = {}
        self.builds: dict[tuple[str, int], Build] = {}
        self.sessions: dict[str, str] = {}
        self.seen: list[Seen] = []
        #: ``(method, path fragment, status)`` — answered once, then removed.
        self.faults: list[tuple[str, str, int]] = []
        #: path fragment → event the answer waits for (a stalled server).
        self.stalls: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return None

            def do_GET(self) -> None:
                fake._handle(self, "GET")

            def do_POST(self) -> None:
                fake._handle(self, "POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._serving = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._serving.start()

    def close(self) -> None:
        for event in self.stalls.values():
            event.set()
        for build in self.builds.values():
            build.stop.set()
        self.server.shutdown()
        self.server.server_close()
        for thread in self._threads:
            thread.join(timeout=10)

    # -- what tests arrange and read -------------------------------------------

    def add_job(
        self,
        name: str,
        job: Callable[[Build], None] | None = None,
        parameters: Sequence[str | dict[str, Any]] | None = None,
    ) -> None:
        """Register (or replace) a job. ``parameters``: the build parameters
        it declares, as names or definitions — see the class docstring."""
        if isinstance(parameters, str):
            raise TypeError("parameters is a list of names, not one string")
        self.jobs[name] = job
        if parameters is None:
            self.job_parameters[name] = None
            self.job_parameter_definitions[name] = None
            return
        definitions = [_definition(entry) for entry in parameters]
        self.job_parameter_definitions[name] = definitions
        self.job_parameters[name] = [definition["name"] for definition in definitions]

    def down(self, status: int | None = 503) -> None:
        """From now on answer every request with ``status`` — or, with
        ``None``, close each connection without answering — until
        :meth:`up`."""
        self._down = (status,)

    def up(self) -> None:
        """Answer again, after :meth:`down`."""
        self._down = None

    def job_url(self, name: str) -> str:
        return self.url + "".join(f"/job/{part}" for part in name.split("/"))

    def triggers(self) -> list[Seen]:
        return [s for s in self.seen if s.method == "POST" and s.path.endswith("/buildWithParameters")]

    def posts(self, fragment: str) -> list[Seen]:
        return [s for s in self.seen if s.method == "POST" and fragment in s.path]

    def gets(self, fragment: str) -> list[Seen]:
        return [s for s in self.seen if s.method == "GET" and fragment in s.path]

    def start(self, number: int) -> Build:
        """Let a queued item become a build (what an idle executor does)."""
        with self._lock:
            item = self.queue[number]
            numbers = [n for (job, n) in self.builds if job == item.job]
            build = Build(
                job=item.job,
                number=max(numbers, default=0) + 1,
                params=item.params,
                url=f"{self.job_url(item.job)}/{max(numbers, default=0) + 1}/",
                queue_id=number,
            )
            self.builds[(build.job, build.number)] = build
            item.build = build
        thread = threading.Thread(target=self._run, args=(build,), daemon=True)
        self._threads.append(thread)
        thread.start()
        return build

    def expire(self, number: int) -> None:
        """Forget a queue item, as Jenkins does a few minutes after the item
        left the queue: its URL answers 404 from then on."""
        with self._lock:
            del self.queue[number]

    def _run(self, build: Build) -> None:
        job = self.jobs.get(build.job)
        try:
            if job is not None:
                job(build)
        except Exception:
            build.result = build.result or "FAILURE"
        if build.result is None:
            build.result = "ABORTED" if build.stop.is_set() else "SUCCESS"
        build.building = False

    # -- the protocol -------------------------------------------------------------

    def _handle(self, request: BaseHTTPRequestHandler, method: str) -> None:
        parsed = urllib.parse.urlsplit(request.path)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        length = int(request.headers.get("Content-Length") or 0)
        body = request.rfile.read(length) if length else b""
        form = dict(urllib.parse.parse_qsl(body.decode("utf-8"), keep_blank_values=True))
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.seen.append(Seen(method, parsed.path, query, headers, form))

        down = self._down
        if down is not None:
            if down[0] is None:
                request.close_connection = True  # no answer at all
                return None
            return self._send(request, down[0], b"Service Unavailable")
        for fragment, event in list(self.stalls.items()):
            if fragment in parsed.path:
                event.wait(30)
        for fault in list(self.faults):
            if fault[0] == method and fault[1] in parsed.path:
                self.faults.remove(fault)
                return self._send(request, fault[2], b"injected fault")
        if self.auth is not None:
            expected = "Basic " + base64.b64encode(":".join(self.auth).encode()).decode()
            if headers.get("authorization") != expected:
                return self._send(request, 401, b"Unauthorized")

        segments = [s for s in parsed.path.split("/") if s]
        if segments == ["crumbIssuer", "api", "json"]:
            if not self.crumbs:
                return self._send(request, 404, b"no crumb issuer")
            session, crumb = secrets.token_hex(8), secrets.token_hex(16)
            self.sessions[session] = crumb
            return self._json(
                request,
                {"crumbRequestField": self.CRUMB_FIELD, "crumb": crumb},
                {"Set-Cookie": f"JSESSIONID.fake={session}; Path=/; HttpOnly"},
            )
        if segments == ["api", "json"]:
            return self._json(request, {"mode": "NORMAL"})
        if method == "POST" and not self._crumb_ok(headers):
            return self._send(request, 403, b"No valid crumb was included in the request")
        if segments[:2] == ["queue", "item"] and segments[3:] == ["api", "json"]:
            item = self.queue.get(int(segments[2]))
            if item is None:
                return self._send(request, 404, b"no such queue item")
            executable = {"number": item.build.number, "url": item.build.url} if item.build else None
            return self._json(request, {"cancelled": item.cancelled, "executable": executable})
        if segments == ["queue", "cancelItem"] and method == "POST":
            item = self.queue.get(int(query.get("id", "0")))
            if item is None:
                return self._send(request, 404, b"no such queue item")
            item.cancelled = True
            return self._send(request, 204, b"")
        job, rest = self._job_of(segments)
        if job is None or job not in self.jobs:
            return self._send(request, 404, b"no such job")
        if rest == ["api", "json"]:
            newest_first = sorted(
                (b for (name, _n), b in self.builds.items() if name == job),
                key=lambda b: b.number, reverse=True,
            )
            definitions = self.job_parameter_definitions.get(job)
            return self._json(
                request,
                {
                    "name": job.rsplit("/", 1)[-1],
                    "property": [] if definitions is None else [
                        {
                            "_class": "hudson.model.ParametersDefinitionProperty",
                            "parameterDefinitions": [_served(entry) for entry in definitions],
                        }
                    ],
                    "builds": [
                        {"number": b.number, "url": b.url, "queueId": b.queue_id} for b in newest_first
                    ],
                },
            )
        if rest == ["buildWithParameters"] and method == "POST":
            return self._trigger(request, job, form)
        if rest and rest[0].isdigit():
            build = self.builds.get((job, int(rest[0])))
            if build is None:
                return self._send(request, 404, b"no such build")
            return self._build(request, method, build, rest[1:])
        return self._send(request, 404, b"not found")

    def _crumb_ok(self, headers: dict[str, str]) -> bool:
        if not self.crumbs:
            return True
        cookie = headers.get("cookie", "")
        session = cookie.partition("JSESSIONID.fake=")[2].split(";")[0]
        return bool(session) and headers.get(self.CRUMB_FIELD.lower()) == self.sessions.get(session)

    @staticmethod
    def _job_of(segments: list[str]) -> tuple[str | None, list[str]]:
        names = []
        index = 0
        while index + 1 < len(segments) and segments[index] == "job":
            names.append(urllib.parse.unquote(segments[index + 1]))
            index += 2
        return ("/".join(names) or None), segments[index:]

    def _trigger(self, request: BaseHTTPRequestHandler, job: str, form: dict[str, str]) -> None:
        for definition in self.job_parameter_definitions.get(job) or []:
            name = definition["name"]
            if definition["kind"] == "choice" and name in form and form[name] not in definition["choices"]:
                # As a real server: an error answer, and no build queued.
                message = f"illegal choice for parameter {name}: {form[name]}"
                return self._send(request, 500, message.encode("utf-8"))
        declared = self.job_parameters.get(job)
        if declared is not None:
            self.ignored_parameters.extend((job, name) for name in form if name not in declared)
            form = {name: value for name, value in form.items() if name in declared}
        with self._lock:
            number = len(self.queue) + 1
            self.queue[number] = QueueItem(number, job, form)
        if self.auto_start:
            self.start(number)
        headers = {"Location": f"{self.url}/queue/item/{number}/"} if self.send_location else {}
        self._send(request, 201, b"", headers)

    def _build(
        self, request: BaseHTTPRequestHandler, method: str, build: Build, rest: list[str]
    ) -> None:
        if rest == ["api", "json"]:
            return self._json(
                request,
                {
                    "building": build.building,
                    "result": None if build.building else build.result,
                    "url": build.url,
                    "artifacts": [{"relativePath": path} for path in sorted(build.artifacts)],
                },
            )
        if rest == ["stop"] and method == "POST":
            build.stop.set()
            return self._send(request, 302, b"", {"Location": build.url})
        if rest[:1] == ["artifact"]:
            content = build.artifacts.get("/".join(urllib.parse.unquote(part) for part in rest[1:]))
            if content is None:
                return self._send(request, 404, b"no such artifact")
            return self._send(request, 200, content)
        if not rest:
            return self._send(request, 200, b"<html>build page</html>")
        page = build.pages.get("/".join(urllib.parse.unquote(part) for part in rest))
        if page is not None:
            return self._send(request, 200, page)
        return self._send(request, 404, b"not found")

    def _json(self, request: BaseHTTPRequestHandler, body: dict, headers: dict | None = None) -> None:
        self._send(
            request, 200, json.dumps(body).encode(), {"Content-Type": "application/json", **(headers or {})}
        )

    def _send(
        self, request: BaseHTTPRequestHandler, status: int, body: bytes, headers: dict | None = None
    ) -> None:
        try:
            request.send_response(status)
            request.send_header("X-Jenkins", self.VERSION)
            request.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                request.send_header(key, value)
            request.end_headers()
            request.wfile.write(body)
        except OSError:
            pass  # the client gave up waiting (a test about exactly that)


#: Jenkins's own names of the core parameter types, by kind.
_TYPES = {
    "string": "String", "text": "Text", "choice": "Choice", "boolean": "Boolean",
    "password": "Password", "file": "File", "run": "Run",
}


def _definition(entry: str | dict[str, Any]) -> dict[str, Any]:
    """A parameter of ``add_job``: a name, or a definition, checked."""
    if isinstance(entry, str):
        return {"name": entry, "kind": "string", "choices": [], "default": None, "description": ""}
    if not isinstance(entry, dict) or not entry.get("name"):
        raise TypeError(f"a job parameter is a name or a mapping with a 'name', got {entry!r}")
    unknown = sorted(set(entry) - {"name", "kind", "choices", "default", "description"})
    if unknown:
        raise TypeError(f"job parameter {entry['name']!r}: unknown key(s) {unknown}")
    kind = str(entry.get("kind") or "string")
    choices = [str(choice) for choice in entry.get("choices") or []]
    if kind == "choice" and not choices:
        raise TypeError(f"job parameter {entry['name']!r}: a choice parameter lists its choices")
    default = entry.get("default", choices[0] if choices else None)
    return {
        "name": str(entry["name"]), "kind": kind, "choices": choices,
        "default": default, "description": str(entry.get("description") or ""),
    }


def _served(definition: dict[str, Any]) -> dict[str, Any]:
    """A definition as a job's ``api/json`` shows it."""
    kind = definition["kind"]
    type_name = _TYPES.get(kind, kind[:1].upper() + kind[1:]) + "ParameterDefinition"
    served: dict[str, Any] = {
        "_class": f"hudson.model.{type_name}",
        "name": definition["name"],
        "type": type_name,
        "description": definition["description"],
    }
    if kind == "choice":
        served["choices"] = list(definition["choices"])
    if definition["default"] is not None and kind != "password":
        served["defaultParameterValue"] = {"value": definition["default"]}
    return served
