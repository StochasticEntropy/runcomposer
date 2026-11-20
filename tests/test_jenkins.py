"""The `jenkins` runner (DESIGN.md §6.2b) against an in-process stand-in.

``FakeJenkins`` is a real HTTP server that speaks the part of Jenkins' remote
API the runner uses, with the behaviours that matter for correctness:

- a CSRF crumb bound to a session cookie — a POST without the matching pair
  is a 403, exactly as on a real instance;
- ``buildWithParameters`` answers 201 with a ``Location`` naming a *queue
  item*, which only later points at a build;
- a build has ``building`` / ``result`` / ``artifacts[relativePath]``, serves
  its artifacts, and can be stopped; a queue item can be cancelled;
- optional Basic authentication.

What this covers: the runner's side of the protocol, end to end, including
the callback loop into a real runcomposer server. What it cannot cover is a
real Jenkins' own behaviour (plugin versions, a job's shell environment) —
that is what the docker setup under ``ci/jenkins/`` is for.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from runcomposer.sdk import ConfigError, Service, load_config

from .conftest import compose, wait_until, write_config

RUNNER = "jenkins"
JOB = "shop-consumer"

# -- the stand-in -----------------------------------------------------------------


@dataclass
class Seen:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    form: dict[str, str]


@dataclass
class Build:
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
    number: int
    job: str
    params: dict[str, str]
    cancelled: bool = False
    build: Build | None = None


class FakeJenkins:
    VERSION = "2.462.3"
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

    def add_job(self, name: str, job: Callable[[Build], None] | None = None) -> None:
        self.jobs[name] = job

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
            return self._json(
                request,
                {
                    "name": job.rsplit("/", 1)[-1],
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


@pytest.fixture()
def jenkins():
    fake = FakeJenkins()
    fake.add_job(JOB)
    yield fake
    fake.close()


def closed_port() -> int:
    """A local port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def jenkins_service(make_service, fake: FakeJenkins | None = None, **options: Any) -> Service:
    """A Service on the demo corpus with the `jenkins` runner configured
    against the stand-in. Short intervals: the stand-in answers at once."""
    runner = {
        "base_url": fake.url if fake else "http://127.0.0.1:1",
        "job": JOB,
        "callback_base": "http://runcomposer.shop.example:8100",
        "poll_interval_s": 0.01,
        "retry_backoff_s": 0.01,
        "request_timeout_s": 5,
        **options,
    }
    extra = {key: runner.pop(key) for key in ("sources",) if key in runner}
    return make_service(runners={RUNNER: runner}, **extra)


def dispatch(service: Service, selection: Any = "Payments-Cards", parameters: dict | None = None, **kw: Any):
    """Compose and dispatch in the foreground: ``(run_id, outcome)``."""
    run_id = compose(service, selection, runner_id=RUNNER, parameters=parameters, **kw)
    return run_id, service.dispatch(run_id, RUNNER, parameters, foreground=True)


def junit_for(item_ids: list[str], failing: set[str] = frozenset()) -> bytes:
    """A JUnit report naming the demo corpus' items the way their ids spell
    them (classname + '.' + name), so they resolve by identity."""
    cases = []
    for item_id in item_ids:
        classname, _, name = item_id.rpartition(".")
        inner = '<failure message="refused by the gateway"/>' if item_id in failing else ""
        cases.append(f'<testcase classname="{classname}" name="{name}" time="0.2">{inner}</testcase>')
    return f'<testsuite name="shop" tests="{len(cases)}">{"".join(cases)}</testsuite>'.encode()


def events_of(service: Service, run_id: str, kind: str) -> list[dict]:
    return [dict(e.data) for e in service.store.events(run_id) if e.kind == kind]


# -- what travels to the job ----------------------------------------------------------


class TestBuildParameters:
    def test_the_default_set_arrives_form_encoded(self, make_service, jenkins):
        service = jenkins_service(make_service, jenkins)
        run_id, outcome = dispatch(service, title="Zahlungen prüfen")
        assert outcome.refusal is None
        (trigger,) = jenkins.triggers()
        assert trigger.path == f"/job/{JOB}/buildWithParameters"
        assert trigger.headers["content-type"] == "application/x-www-form-urlencoded"
        assert set(trigger.form) == {"SPEC_JSON", "DISPATCH_ID", "CALLBACK_URL", "INGEST_TOKEN"}
        assert trigger.form["DISPATCH_ID"] == outcome.dispatch_id
        assert trigger.form["CALLBACK_URL"] == (
            f"http://runcomposer.shop.example:8100/api/v1/runs/{run_id}/results"
        )
        spec = json.loads(trigger.form["SPEC_JSON"])
        assert spec["run"]["id"] == run_id and spec["run"]["title"] == "Zahlungen prüfen"

        # SPEC_JSON is byte for byte what the dispatch's hash was computed
        # over — so a marker written from it verifies on the way back (§5)…
        dispatch_record = service.require_run(run_id).latest_dispatch
        sent = trigger.form["SPEC_JSON"].encode("utf-8")
        assert hashlib.sha256(sent).hexdigest() == dispatch_record.spec_sha256
        content, media_type, _ = service.dispatch_spec(run_id, outcome.dispatch_id)
        assert content == sent and media_type == "application/json"
        # …and the token is this dispatch's, known to the store by hash only.
        token = trigger.form["INGEST_TOKEN"]
        assert token.startswith("rct_")
        assert service.ingestion.authorize_push(run_id, token) == outcome.dispatch_id

    def test_a_custom_mapping_speaks_the_job_s_own_parameter_names(self, make_service, jenkins):
        """An existing parameterized job has its own names: nothing about
        them is fixed by the runner."""
        service = jenkins_service(
            make_service,
            jenkins,
            parameters={
                "RUNSPEC": "{spec_json}",
                "RUN": "{run_id}",
                "DISPATCH": "{dispatch_id}",
                "RESULTS_URL": "{callback_url}",
                "PUSH_TOKEN": "{token}",
                "TESTS": "{item_ids}",
                "BUILD_NAME": "Shop: {title} ({run_id})",
                "SUITE": "{labels[suite]}",
                "OWNER": "{labels[owner]}",
                "STAGE": "{params[stage]}",
                "DRY_RUN": "{params[dry_run]}",
            },
            run_parameters=[
                {"name": "stage", "kind": "choice", "choices": ["staging", "production"],
                 "default": "staging", "label": "Stage"},
                {"name": "dry_run", "kind": "boolean", "default": False},
            ],
        )
        run_id, outcome = dispatch(
            service, parameters={"stage": "production"}, title="Card payments",
            labels={"suite": "nightly"},
        )
        assert outcome.refusal is None
        form = jenkins.triggers()[0].form
        planned = service.require_spec(run_id)["selection"]["materialized"]["item_ids"]
        assert set(form) == {
            "RUNSPEC", "RUN", "DISPATCH", "RESULTS_URL", "PUSH_TOKEN", "TESTS", "BUILD_NAME",
            "SUITE", "OWNER", "STAGE", "DRY_RUN",
        }
        assert json.loads(form["RUNSPEC"])["run"]["id"] == run_id
        assert (form["RUN"], form["DISPATCH"]) == (run_id, outcome.dispatch_id)
        assert form["RESULTS_URL"].endswith(f"/api/v1/runs/{run_id}/results")
        assert form["PUSH_TOKEN"].startswith("rct_")
        assert form["TESTS"].split("\n") == planned
        assert form["BUILD_NAME"] == f"Shop: Card payments ({run_id})"
        assert form["SUITE"] == "nightly"
        assert form["OWNER"] == ""  # a label the run does not carry renders as nothing
        assert (form["STAGE"], form["DRY_RUN"]) == ("production", "false")
        # The accepted per-run values are part of the document.
        assert service.require_spec(run_id)["runner"][RUNNER] == {"stage": "production", "dry_run": False}

    def test_the_runner_declares_its_configured_run_parameters(self, make_service, jenkins):
        service = jenkins_service(
            make_service, jenkins,
            run_parameters=[{"name": "stage", "kind": "choice", "choices": ["staging", "production"]}],
        )
        (info,) = service.runner_infos(health=False)
        assert info["plugin"] == "jenkins"
        assert {"external_delivery", "cancel", "callback"} <= set(info["capabilities"])
        assert [(p["name"], p["kind"], p["choices"]) for p in info["parameters"]] == [
            ("stage", "choice", ["staging", "production"])
        ]
        from runcomposer.sdk import InvalidParameter

        with pytest.raises(InvalidParameter, match="stage"):
            compose(service, "Payments-Cards", runner_id=RUNNER, parameters={"stage": "moon"})
        assert jenkins.triggers() == []

    @pytest.mark.parametrize(
        "template, complaint",
        [
            ("{build_number}", "build_number"),
            ("{labels}", "labels"),
            ("{run_id[0]}", "run_id"),
            ("{unclosed", "parameters.BROKEN"),
            (7, "must be a template string"),
        ],
    )
    def test_a_template_the_runner_cannot_fill_refuses_startup(self, make_service, template, complaint):
        service = jenkins_service(make_service, parameters={"BROKEN": template})
        with pytest.raises(ConfigError) as excinfo:
            service.validate()
        message = str(excinfo.value)
        assert "runners.jenkins" in message and complaint in message

    def test_no_token_is_minted_for_a_job_that_is_not_given_one(self, make_service, jenkins):
        service = jenkins_service(
            make_service, jenkins, completion="poll",
            parameters={"RUNSPEC": "{spec_json}", "DISPATCH": "{dispatch_id}"}, timeout_s=0.2,
        )
        jenkins.auto_start = False
        run_id, _ = dispatch(service)
        assert service.store.ingest_tokens(run_id) == {}

    def test_a_job_in_folders_is_addressed_segment_by_segment(self, make_service, jenkins):
        jenkins.add_job("shop/nightly regression")
        service = jenkins_service(make_service, jenkins, job="shop/nightly regression")
        run_id, outcome = dispatch(service)
        assert outcome.refusal is None
        assert jenkins.triggers()[0].path == "/job/shop/job/nightly%20regression/buildWithParameters"
        links = service.require_run(run_id).latest_dispatch.links
        assert links["job"] == f"{jenkins.url}/job/shop/job/nightly%20regression"

    def test_neither_the_token_nor_the_document_leaks(self, make_service, jenkins):
        """What goes to the job goes to the job only: not into the stored
        spec, not into the event stream, not into the dispatch record."""
        from runcomposer.app.views import run_detail

        service = jenkins_service(make_service, jenkins)
        run_id, _ = dispatch(service)
        token = jenkins.triggers()[0].form["INGEST_TOKEN"]
        assert "token" not in service.require_spec(run_id)["results"]
        everything = json.dumps(run_detail(service, service.require_run(run_id)))
        assert token not in everything and "SPEC_JSON" not in everything
        assert token not in json.dumps([dict(e.data) for e in service.store.events(run_id)])
        assert token.encode() not in service.dispatch_spec(run_id, service.require_run(run_id).latest_dispatch.dispatch_id)[0]


class TestCrumbsAndCredentials:
    def test_the_crumb_travels_with_its_session(self, make_service, jenkins):
        """The stand-in refuses a POST without the crumb AND the cookie it was
        issued for — the runner's trigger passes because it sends both."""
        bare = urllib.request.Request(
            f"{jenkins.url}/job/{JOB}/buildWithParameters", data=b"X=1", method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(bare, timeout=5)
        assert refused.value.code == 403

        service = jenkins_service(make_service, jenkins)
        _, outcome = dispatch(service)
        assert outcome.refusal is None
        trigger = jenkins.triggers()[-1]
        assert trigger.headers["jenkins-crumb"] in jenkins.sessions.values()
        assert "JSESSIONID.fake=" in trigger.headers["cookie"]

    def test_a_server_without_a_crumb_issuer_needs_none(self, make_service):
        fake = FakeJenkins(crumbs=False)
        fake.add_job(JOB)
        try:
            service = jenkins_service(make_service, fake)
            _, outcome = dispatch(service)
            assert outcome.refusal is None
            assert "jenkins-crumb" not in fake.triggers()[0].headers
        finally:
            fake.close()

    def test_an_expired_crumb_is_fetched_again_once(self, make_service, jenkins):
        service = jenkins_service(make_service, jenkins)
        dispatch(service)
        jenkins.sessions.clear()  # the server forgot every session (a restart)
        _, outcome = dispatch(service)
        assert outcome.refusal is None
        assert len(jenkins.gets("/crumbIssuer/")) == 2

    def test_credentials_come_from_the_environment(self, make_service, monkeypatch):
        fake = FakeJenkins(auth=("shop-bot", "s3cret-from-the-vault"))
        fake.add_job(JOB)
        try:
            monkeypatch.setenv("SHOP_JENKINS_TOKEN", "s3cret-from-the-vault")
            service = jenkins_service(
                make_service, fake, user="shop-bot", api_token="${env:SHOP_JENKINS_TOKEN}"
            )
            run_id, outcome = dispatch(service)
            assert outcome.refusal is None
            expected = "Basic " + base64.b64encode(b"shop-bot:s3cret-from-the-vault").decode()
            assert fake.triggers()[0].headers["authorization"] == expected
            # The resolved secret is in no event and in no API-shaped view.
            dump = json.dumps([dict(e.data) for e in service.store.events(run_id)])
            assert "s3cret" not in dump and "s3cret" not in json.dumps(service.runner_infos())
        finally:
            fake.close()

    def test_wrong_credentials_are_a_refusal_that_leaves_no_dispatch(self, make_service, monkeypatch):
        fake = FakeJenkins(auth=("shop-bot", "the-real-one"))
        fake.add_job(JOB)
        try:
            monkeypatch.setenv("SHOP_JENKINS_TOKEN", "a-stale-one")
            service = jenkins_service(
                make_service, fake, user="shop-bot", api_token="${env:SHOP_JENKINS_TOKEN}"
            )
            run_id, outcome = dispatch(service)
            assert "401" in outcome.refusal and "Jenkins trigger failed" in outcome.refusal
            assert "a-stale-one" not in outcome.refusal
            run = service.require_run(run_id)
            assert run.state == "COMPOSED" and run.dispatches == ()
        finally:
            fake.close()

    def test_a_missing_environment_variable_refuses_startup_by_name(self, make_service, monkeypatch):
        monkeypatch.delenv("SHOP_JENKINS_TOKEN", raising=False)
        service = jenkins_service(make_service, user="shop-bot", api_token="${env:SHOP_JENKINS_TOKEN}")
        with pytest.raises(ConfigError, match="SHOP_JENKINS_TOKEN"):
            service.validate()


class TestConstruction:
    def test_tls_options_construct(self, tmp_path):
        import ssl

        from runcomposer.plugins.jenkins import JenkinsRunner

        runner = JenkinsRunner(
            base_url="https://jenkins.shop.example/", job=JOB, verify_tls=False
        )
        assert runner.base_url == "https://jenkins.shop.example"
        assert runner.job_url == f"https://jenkins.shop.example/job/{JOB}"
        system_bundle = ssl.get_default_verify_paths().cafile
        if system_bundle and Path(system_bundle).is_file():
            JenkinsRunner(base_url="https://jenkins.shop.example", job=JOB, ca_bundle=system_bundle)
        with pytest.raises(OSError):
            JenkinsRunner(
                base_url="https://jenkins.shop.example", job=JOB, ca_bundle=str(tmp_path / "missing.pem")
            )

    def test_a_missing_ca_bundle_refuses_startup_naming_the_entry(self, make_service):
        service = jenkins_service(
            make_service, base_url="https://jenkins.shop.example", ca_bundle="certs/missing.pem"
        )
        with pytest.raises(ConfigError, match=r"runners\.jenkins"):
            service.validate()

    @pytest.mark.parametrize(
        "options, complaint",
        [
            ({"base_url": "jenkins.shop.example"}, "base_url must start with http"),
            ({"completion": "webhook"}, "completion must be 'callback' or 'poll'"),
            ({"job": ""}, "'job' is required"),
            ({"job_template": "x"}, "unknown option"),
            ({"run_parameters": [{"kind": "string"}]}, "must be a mapping with a 'name'"),
        ],
    )
    def test_bad_options_refuse_startup(self, make_service, options, complaint):
        with pytest.raises(ConfigError, match=complaint):
            jenkins_service(make_service, **options).validate()

    def test_constructing_the_runner_touches_no_network(self, make_service):
        """Every plugin is built at startup; a Jenkins that is down must not
        keep runcomposer from starting."""
        service = jenkins_service(make_service, base_url=f"http://127.0.0.1:{closed_port()}")
        service.validate()


# -- completion: callback ------------------------------------------------------------


def post_bundle(url: str, token: str, files: dict[str, bytes], fields: dict[str, str]) -> tuple[int, dict]:
    """A multipart results push with nothing but the standard library — what
    a job's post step does with curl."""
    boundary = "----shop" + secrets.token_hex(8)
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    for filename, content in files.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n".encode() + content + b"\r\n"
        )
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-Runcomposer-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture()
def live_server(tmp_path, jenkins):
    """A REAL runcomposer server (uvicorn, on a socket of its own) configured
    with the `jenkins` runner against the stand-in — so the job's callback
    is a genuine HTTP push into a running instance."""
    import uvicorn

    from runcomposer.http import create_app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    config = load_config(
        str(
            write_config(
                tmp_path,
                runners={
                    RUNNER: {"base_url": jenkins.url, "job": JOB, "callback_base": base,
                             "poll_interval_s": 0.01}
                },
            )
        )
    )
    app = create_app(config)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    wait_until(lambda: server.started, what="the runcomposer server to start")
    yield SimpleServer(base, app.state.service)
    server.should_exit = True
    thread.join(timeout=15)


@dataclass
class SimpleServer:
    base: str
    service: Service

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=30) as response:
            return json.loads(response.read())


def consumer_job(workspace: Path, pushes: list, *, tamper: bool = False, release: threading.Event | None = None):
    """The thin consumer stage of ci/jenkins, in Python: write SPEC_JSON to a
    file, run runcomposer-exec on it, POST the bundle to CALLBACK_URL."""
    import runcomposer_exec

    def job(build: Build) -> None:
        if release is not None:
            release.wait(30)
        work = workspace / f"build-{build.number}"
        work.mkdir(parents=True)
        spec = work / "spec.json"
        text = build.params["SPEC_JSON"] + ("\n" if tamper else "")
        spec.write_bytes(text.encode("utf-8"))
        code = runcomposer_exec.main(
            [str(spec), "--out", str(work / "results"), "--simulate",
             "--dispatch", build.params["DISPATCH_ID"]]
        )
        assert code == 0
        files = {p.name: p.read_bytes() for p in (work / "results").iterdir()}
        pushes.append(
            post_bundle(
                build.params["CALLBACK_URL"], build.params["INGEST_TOKEN"], files,
                {"dispatch": build.params["DISPATCH_ID"]},
            )
        )

    return job


class TestCallbackCompletion:
    def test_without_a_callback_base_the_dispatch_is_refused_before_any_trigger(self, make_service, jenkins):
        service = jenkins_service(make_service, jenkins, callback_base=None)
        run_id, outcome = dispatch(service)
        assert "callback_base" in outcome.refusal
        assert jenkins.triggers() == []
        assert service.require_run(run_id).dispatches == ()

    def test_dispatch_returns_once_the_build_is_queued(self, make_service, jenkins):
        jenkins.auto_start = False
        service = jenkins_service(make_service, jenkins)
        run_id, outcome = dispatch(service)
        run = service.require_run(run_id)
        assert run.state == "AWAITING_RESULTS" and run.completion is None
        record = run.latest_dispatch
        assert record.dispatch_id == outcome.dispatch_id and record.owner is None
        assert record.shards == ("1",)
        assert dict(record.links) == {
            "job": f"{jenkins.url}/job/{JOB}", "queue": f"{jenkins.url}/queue/item/1/",
        }
        assert "completion by callback POST" in record.plan
        assert [e.kind for e in service.store.events(run_id)] == [
            "run.composed", "run.dispatched", "run.running", "run.awaiting_results",
        ]

    def test_end_to_end_the_job_pushes_back_into_a_running_server(self, live_server, jenkins, tmp_path):
        """compose + dispatch over HTTP → the runner triggers the stand-in →
        its job runs runcomposer-exec on SPEC_JSON and POSTs the bundle with
        the token to the callback URL → the run completes."""
        pushes: list = []
        release = threading.Event()
        jenkins.add_job(JOB, consumer_job(tmp_path / "ws", pushes, release=release))
        status, body = live_server.post(
            "/api/v1/runs",
            {"selection": {"tag_filter": "Payments-Cards"}, "title": "Zahlungen prüfen",
             "dispatch": {"runner": RUNNER}},
        )
        assert status == 201, body
        run_id = body["run"]["id"]
        # The POST came back while the build had not even begun its work.
        awaiting = wait_until(
            lambda: (r := live_server.get(f"/api/v1/runs/{run_id}"))["state"] == "AWAITING_RESULTS" and r,
            what="the run to await results",
        )
        assert awaiting["mode"] == RUNNER and awaiting["completion"] is None
        assert pushes == []

        release.set()  # an executor picks the build up
        done = wait_until(
            lambda: (r := live_server.get(f"/api/v1/runs/{run_id}"))["state"] == "COMPLETE" and r,
            what="the run to complete",
        )
        (status, report), = pushes
        assert status == 200, report
        assert report["outcome"] == "new" and report["warnings"] == []
        assert report["dispatch_id"] == done["dispatch_id"]
        assert done["planned_count"] == sum(done["verdict_summary"].values()) == report["verdict_count"]
        assert "MISSING" not in done["verdict_summary"]

        # The marker the job wrote was verified against the bytes handed out.
        build = jenkins.builds[(JOB, 1)]
        sent = build.params["SPEC_JSON"].encode("utf-8")
        marker = json.loads((tmp_path / "ws" / "build-1" / "results" / "runcomposer_run.json").read_text())
        assert marker["spec_sha256"] == hashlib.sha256(sent).hexdigest()
        assert marker["spec_sha256"] == done["dispatches"][-1]["spec_sha256"]
        assert "prüfen" in build.params["SPEC_JSON"]  # non-ASCII survived the trip
        assert not [e for e in done["events"] if e["kind"] == "warning"]

    def test_a_bundle_from_a_tampered_spec_is_quarantined_not_ingested(self, live_server, jenkins, tmp_path):
        pushes: list = []
        jenkins.add_job(JOB, consumer_job(tmp_path / "ws", pushes, tamper=True))
        status, body = live_server.post(
            "/api/v1/runs",
            {"selection": {"tag_filter": "Payments-Cards"}, "dispatch": {"runner": RUNNER}},
        )
        assert status == 201, body
        run_id = body["run"]["id"]
        wait_until(lambda: pushes, what="the job's push")
        (status, refusal), = pushes
        assert status == 409 and refusal["code"] == "sha-mismatch"
        assert refusal["quarantine_entry"]
        run = live_server.get(f"/api/v1/runs/{run_id}")
        assert run["state"] == "AWAITING_RESULTS" and run["verdicts"] == []

    def test_a_push_with_another_dispatch_s_token_is_refused(self, live_server, jenkins, tmp_path):
        """The token minted for a dispatch delivers to that dispatch only."""
        jenkins.auto_start = False
        runs = []
        for _ in range(2):
            status, body = live_server.post(
                "/api/v1/runs",
                {"selection": {"tag_filter": "Payments-Cards"}, "dispatch": {"runner": RUNNER}},
            )
            assert status == 201, body
            runs.append(body["run"]["id"])
        first, second = (item.params for item in jenkins.queue.values())
        files = {"results.json": json.dumps({"format": "runcomposer-verdicts", "verdicts": []}).encode()}
        status, refusal = post_bundle(first["CALLBACK_URL"], second["INGEST_TOKEN"], files, {})
        assert status == 403 and refusal["code"] == "token-rejected"
        assert live_server.get("/api/v1/quarantine")["entries"] == []  # nothing was written


# -- completion: poll -----------------------------------------------------------------


def junit_job(failing: set[str] = frozenset(), *, marker: dict | None = None, paths: tuple[str, ...] = ("reports/junit.xml",)):
    """A job that archives a JUnit report for exactly the planned items."""

    def job(build: Build) -> None:
        item_ids = build.params["TESTS"].split("\n")
        for path in paths:
            build.artifacts[path] = junit_for(item_ids, failing)
        build.artifacts["reports/console.txt"] = b"not a result"
        described = marker if marker is not None else {
            "run_id": build.params["RUN"], "dispatch_id": build.params["DISPATCH"],
        }
        build.artifacts["reports/runcomposer_run.json"] = json.dumps(described).encode()
        if failing:
            build.result = "UNSTABLE"

    return job


POLL = {
    "completion": "poll",
    "format": "junit-xml",
    "artifact_paths": ["reports/*.xml", "reports/runcomposer_run.json"],
    "parameters": {"TESTS": "{item_ids}", "RUN": "{run_id}", "DISPATCH": "{dispatch_id}"},
}


class TestPollCompletion:
    def test_end_to_end_with_configured_artifact_paths_and_format(self, make_service, jenkins):
        service = jenkins_service(make_service, jenkins, **POLL)
        planned_fail = "Shop.Payments.Cards.T003"
        jenkins.add_job(JOB, junit_job({planned_fail}))
        run_id, outcome = dispatch(service)
        assert outcome.refusal is None
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "FAIL")
        assert run.latest_dispatch.owner is None
        verdicts = {v.item_id: v for v in service.store.verdicts_for(run_id)}
        assert len(verdicts) == run.planned_count
        assert verdicts[planned_fail].status == "FAIL"
        assert verdicts[planned_fail].message == "refused by the gateway"
        assert {v.status for i, v in verdicts.items() if i != planned_fail} == {"PASS"}
        assert {d.format for d in run.deliveries} == {"junit-xml"}
        # The build is identified through the queue item and recorded as a link…
        build_url = jenkins.builds[(JOB, 1)].url.rstrip("/")
        artifacts = {a["name"]: a for a in service.artifacts.list(run_id)}
        assert artifacts["Jenkins build"]["kind"] == "url"
        assert artifacts["Jenkins build"]["href"] == build_url
        # …and only what `artifact_paths` matches was downloaded.
        assert set(artifacts) == {"Jenkins build", "junit.xml", "runcomposer_run.json"}
        assert jenkins.gets("/artifact/reports/console.txt") == []
        assert artifacts["junit.xml"]["kind"] == "local" and artifacts["junit.xml"]["href"]
        assert "completion by polling the build" in run.latest_dispatch.plan
        assert any("build started" in e["message"] for e in events_of(service, run_id, "log"))

    def test_a_queued_build_that_never_starts_fails_the_dispatch(self, make_service, jenkins):
        """After the trigger was accepted there IS a dispatch: running out of
        time is a failed execution, not a refusal that erases it."""
        jenkins.auto_start = False
        service = jenkins_service(make_service, jenkins, **POLL, timeout_s=0.3)
        run_id, outcome = dispatch(service)
        assert outcome.refusal is None
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("FAILED", None)
        assert "did not start within timeout_s=0.3" in run.latest_dispatch.error
        assert len(jenkins.gets("/queue/item/1/api/json")) >= 2  # it really polled
        assert dict(run.latest_dispatch.summary) == {"MISSING": run.planned_count}
        assert events_of(service, run_id, "run.failed")[0]["error"] == run.latest_dispatch.error

    def test_a_build_that_never_finishes_fails_the_dispatch(self, make_service, jenkins):
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))  # until the stand-in closes
        service = jenkins_service(make_service, jenkins, **POLL, timeout_s=0.3)
        run_id, _ = dispatch(service)
        run = service.require_run(run_id)
        assert run.state == "FAILED" and "did not finish within timeout_s=0.3" in run.latest_dispatch.error
        # The build it gave up on is still named, for whoever looks.
        assert [a["name"] for a in service.artifacts.list(run_id)] == ["Jenkins build"]

    def test_without_a_queue_location_nothing_is_guessed(self, make_service, jenkins):
        """No Location, no way to know which build is ours — and "the job's
        latest build" may be somebody else's. Their results stay theirs."""
        foreign = Build(job=JOB, number=7, params={}, url=f"{jenkins.job_url(JOB)}/7/",
                        building=False, result="SUCCESS")
        foreign.artifacts["reports/junit.xml"] = junit_for(["Shop.Payments.Cards.T001"])
        jenkins.builds[(JOB, 7)] = foreign
        jenkins.send_location = False
        jenkins.auto_start = False
        service = jenkins_service(make_service, jenkins, **POLL)
        run_id, outcome = dispatch(service)
        assert outcome.refusal is None  # the trigger WAS accepted
        run = service.require_run(run_id)
        assert run.state == "FAILED"
        assert "no queue location" in run.latest_dispatch.error
        assert "completion: callback" in run.latest_dispatch.error
        assert service.store.verdicts_for(run_id) == []
        assert jenkins.gets("/7/") == [] and jenkins.gets("lastBuild") == []

    def test_a_transient_503_is_retried(self, make_service, jenkins):
        jenkins.add_job(JOB, junit_job())
        jenkins.faults.append(("GET", "/queue/item/1/api/json", 503))
        service = jenkins_service(make_service, jenkins, **POLL)
        run_id, _ = dispatch(service)
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "PASS")
        assert len(jenkins.gets("/queue/item/1/api/json")) >= 2
        assert jenkins.faults == []

    def test_an_error_that_does_not_go_away_fails_after_the_bounded_retries(self, make_service, jenkins):
        jenkins.add_job(JOB, junit_job())
        jenkins.faults.extend([("GET", "/queue/item/1/api/json", 503)] * 3)
        service = jenkins_service(make_service, jenkins, **POLL, retries=2)
        run_id, _ = dispatch(service)
        run = service.require_run(run_id)
        assert run.state == "FAILED" and "after 3 attempt(s)" in run.latest_dispatch.error
        assert len(jenkins.gets("/queue/item/1/api/json")) == 3

    def test_a_downloaded_bundle_naming_another_run_is_refused(self, make_service, jenkins):
        """The marker is verified on the poll path too: a build that archived
        some other run's results does not complete this one."""
        jenkins.add_job(JOB, junit_job(marker={"run_id": "01SOMEOTHERRUN0000000000000"}))
        service = jenkins_service(make_service, jenkins, **POLL)
        run_id, _ = dispatch(service)
        run = service.require_run(run_id)
        assert run.state == "FAILED" and "another run's results" in run.latest_dispatch.error
        assert service.store.verdicts_for(run_id) == [] and run.deliveries == ()

    def test_an_aborted_build_fails_the_dispatch(self, make_service, jenkins):
        def aborted(build: Build) -> None:
            build.result = "ABORTED"

        jenkins.add_job(JOB, aborted)
        service = jenkins_service(make_service, jenkins, **POLL)
        run_id, _ = dispatch(service)
        run = service.require_run(run_id)
        assert run.state == "FAILED" and "aborted" in run.latest_dispatch.error

    def test_a_build_that_archived_nothing_matching_fails_and_says_what_was_wanted(self, make_service, jenkins):
        jenkins.add_job(JOB, junit_job(paths=("target/surefire/junit.xml",)))
        service = jenkins_service(make_service, jenkins, **{**POLL, "artifact_paths": ["reports/*.xml"]})
        run_id, _ = dispatch(service)
        run = service.require_run(run_id)
        assert run.state == "FAILED"
        assert "archived nothing matching ['reports/*.xml']" in run.latest_dispatch.error


# -- when the trigger does not go through ----------------------------------------------


class TestTrigger:
    def test_a_refused_connection_is_a_clean_refusal(self, make_service):
        service = jenkins_service(
            make_service, base_url=f"http://127.0.0.1:{closed_port()}", retries=1
        )
        run_id, outcome = dispatch(service)
        assert "Jenkins trigger failed" in outcome.refusal and "refused" in outcome.refusal.lower()
        run = service.require_run(run_id)
        assert run.state == "COMPOSED" and run.dispatches == ()
        (event,) = events_of(service, run_id, "dispatch.refused")
        assert event["crashed"] is False and event["mode"] == RUNNER

    def test_an_explicit_try_later_is_retried(self, make_service, jenkins):
        """A 503 answer means the request did not take effect: safe to repeat."""
        jenkins.faults.append(("POST", "/buildWithParameters", 503))
        service = jenkins_service(make_service, jenkins)
        _, outcome = dispatch(service)
        assert outcome.refusal is None
        assert len(jenkins.triggers()) == 2 and len(jenkins.queue) == 1

    def test_a_trigger_that_timed_out_is_not_sent_again(self, make_service, jenkins):
        """After a timeout nobody knows whether the build was queued. Sending
        the trigger again could start the suite twice — so it is not."""
        stall = jenkins.stalls["/buildWithParameters"] = threading.Event()
        service = jenkins_service(make_service, jenkins, request_timeout_s=0.3, retries=3)
        run_id, outcome = dispatch(service)
        stall.set()
        assert "Jenkins trigger failed" in outcome.refusal
        assert "timed out" in outcome.refusal
        assert len(jenkins.triggers()) == 1
        assert service.require_run(run_id).dispatches == ()

    def test_a_server_error_is_a_refusal_with_its_status(self, make_service, jenkins):
        jenkins.faults.append(("POST", "/buildWithParameters", 500))
        service = jenkins_service(make_service, jenkins)
        run_id, outcome = dispatch(service)
        assert "500" in outcome.refusal and len(jenkins.triggers()) == 1
        assert service.require_run(run_id).state == "COMPOSED"

    def test_a_job_that_does_not_exist_is_a_refusal(self, make_service, jenkins):
        service = jenkins_service(make_service, jenkins, job="shop-renamed-away")
        _, outcome = dispatch(service)
        assert "404" in outcome.refusal and "shop-renamed-away" in outcome.refusal


# -- cancel ------------------------------------------------------------------------------


class TestCancel:
    def test_cancel_stops_the_running_build(self, make_service, jenkins):
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))  # runs until stopped
        service = jenkins_service(make_service, jenkins, **POLL)
        run_id = compose(service, "Payments-Cards", runner_id=RUNNER)
        service.dispatch(run_id, RUNNER)  # in the background: it polls
        wait_until(
            lambda: (JOB, 1) in jenkins.builds and service.artifacts.list(run_id),
            what="the runner to have found its build",
        )
        service.cancel(run_id)
        run = service.wait(run_id, timeout=30)
        assert run.state == "CANCELLED" and run.latest_dispatch.owner is None
        (stop,) = jenkins.posts("/stop")
        assert stop.path == f"/job/{JOB}/1/stop"
        assert jenkins.builds[(JOB, 1)].stop.is_set()
        assert any("stop requested" in e["message"] for e in events_of(service, run_id, "log"))
        assert jenkins.posts("/queue/cancelItem") == []

    def test_cancel_removes_a_build_that_is_still_queued(self, make_service, jenkins):
        jenkins.auto_start = False
        service = jenkins_service(make_service, jenkins, **POLL)
        run_id = compose(service, "Payments-Cards", runner_id=RUNNER)
        service.dispatch(run_id, RUNNER)
        wait_until(lambda: jenkins.gets("/queue/item/1/api/json"), what="the runner to poll the queue")
        service.cancel(run_id)
        run = service.wait(run_id, timeout=30)
        assert run.state == "CANCELLED"
        (cancel,) = jenkins.posts("/queue/cancelItem")
        assert cancel.query == {"id": "1"}
        assert jenkins.queue[1].cancelled is True
        assert jenkins.posts("/stop") == []

    def test_cancel_reaches_the_build_of_a_dispatch_that_only_awaits_results(
        self, make_service, jenkins, tmp_path
    ):
        """Callback mode: `dispatch()` returned long ago and nothing here is
        executing. Cancel still finds the build — through the queue link the
        dispatch recorded — and the run is CANCELLED at once."""
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        service = jenkins_service(make_service, jenkins)
        run_id, _ = dispatch(service)
        assert service.require_run(run_id).state == "AWAITING_RESULTS"
        cancelled = service.cancel(run_id)
        assert cancelled.state == "CANCELLED"
        assert [s.path for s in jenkins.posts("/stop")] == [f"/job/{JOB}/1/stop"]
        assert service.require_run(run_id).state == "CANCELLED"
        # Results that still arrive for a cancelled dispatch are not taken in.
        from runcomposer.sdk import IngestError

        bundle = tmp_path / "late"
        bundle.mkdir()
        (bundle / "results.json").write_text(
            json.dumps({"format": "runcomposer-verdicts",
                        "verdicts": [{"name": "Shop.Payments.Cards.T001", "status": "PASS"}]}),
            encoding="utf-8",
        )
        with pytest.raises(IngestError) as late:
            service.ingest(bundle, run_id=run_id, dispatch_id=cancelled.dispatch_id)
        assert late.value.reason == "dispatch-cancelled"
        assert service.store.verdicts_for(run_id) == []

    def test_cancel_works_when_the_base_url_comes_from_the_environment(
        self, make_service, jenkins, monkeypatch
    ):
        """Regression (found against a real Jenkins): the links a runner
        records are how a later cancel finds its build. They are kept as the
        runner gave them — blanking an environment value out of the stored
        queue link made the build unreachable."""
        monkeypatch.setenv("SHOP_CI_URL", jenkins.url)
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        service = jenkins_service(make_service, jenkins, base_url="${env:SHOP_CI_URL}")
        run_id, _ = dispatch(service)
        assert service.require_run(run_id).state == "AWAITING_RESULTS"
        assert service.cancel(run_id).state == "CANCELLED"
        assert [s.path for s in jenkins.posts("/stop")] == [f"/job/{JOB}/1/stop"]

    def test_a_jenkins_that_is_gone_does_not_keep_the_run_from_being_cancelled(self, make_service, jenkins):
        jenkins.auto_start = False
        service = jenkins_service(make_service, jenkins, retries=0)
        run_id, _ = dispatch(service)
        jenkins.close()
        assert service.cancel(run_id).state == "CANCELLED"
        assert any(
            "runner cancel failed" in w["message"] for w in events_of(service, run_id, "warning")
        )


# -- health --------------------------------------------------------------------------------


class TestHealth:
    def test_ok_with_the_server_s_version(self, make_service, jenkins):
        health = jenkins_service(make_service, jenkins).runner_health(RUNNER, refresh=True)
        assert health.status == "ok"
        assert FakeJenkins.VERSION in health.detail and health.data["version"] == FakeJenkins.VERSION
        # What each check found, for `doctor -v` and a runner card.
        assert [(c["check"], c["ok"]) for c in health.data["checks"]] == [
            ("server reachable", True), (f"job {JOB!r} readable", True),
        ]
        assert health.data["job_url"].endswith(f"/job/{JOB}") and health.data["completion"] == "callback"

    def test_degraded_when_the_job_is_not_there(self, make_service, jenkins):
        service = jenkins_service(make_service, jenkins, job="shop-renamed-away")
        health = service.runner_health(RUNNER, refresh=True)
        assert health.status == "degraded" and "shop-renamed-away" in health.detail
        assert health.data["version"] == FakeJenkins.VERSION
        assert [(c["check"], c["ok"]) for c in health.data["checks"]] == [
            ("server reachable", True), ("job 'shop-renamed-away' readable", False),
        ]

    def test_down_when_unreachable_and_it_does_not_take_long_to_say_so(self, make_service):
        service = jenkins_service(
            make_service, base_url=f"http://127.0.0.1:{closed_port()}", retries=5, retry_backoff_s=30
        )
        health = service.runner_health(RUNNER, refresh=True)  # a probe is one attempt, no backoff
        assert health.status == "down" and "unreachable" in health.detail

    def test_it_is_served_with_the_runner_listing(self, make_client, jenkins):
        client, _service = make_client(
            runners={RUNNER: {"base_url": jenkins.url, "job": JOB, "callback_base": "http://127.0.0.1:1"}}
        )
        (listed,) = client.get("/api/v1/runners?refresh=true").json()
        assert listed["id"] == RUNNER and listed["health"]["status"] == "ok"
        assert listed["health"]["data"]["version"] == FakeJenkins.VERSION
        assert all(check["ok"] for check in listed["health"]["data"]["checks"])
