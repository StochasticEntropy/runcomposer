"""The observer port and the three reference observers (DESIGN.md §6.5),
end to end: a ``Service`` configured with ``observers:``, a real run, a real
HTTP receiver, real commands.

Observers are notified on a thread of their own, so every test calls
``service.bus.flush()`` before it looks — never a sleep.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from runcomposer.sdk import ConfigError, ObserverBase, Service

from .conftest import compose

LIFECYCLE_OF_A_RUN = [
    "run.composed",
    "run.dispatched",
    "run.running",
    "delivery.recorded",
    "run.completed",
]


def run_once(service: Service, selection: str = "Auth-SSO", seed: str = "a", **kwargs: Any) -> str:
    run_id = compose(service, selection, **kwargs)
    service.dispatch(run_id, "demo", {"seed": seed}, foreground=True)
    assert service.bus.flush(30), "observer notifications did not drain"
    return run_id


def warnings_of(service: Service, run_id: str) -> list[str]:
    return [e.data["message"] for e in service.store.events(run_id) if e.kind == "warning"]


# -- a receiver for the webhook ------------------------------------------------------


class Receiver:
    """A real HTTP server on a free local port that keeps what it is sent."""

    def __init__(self, status: int = 204):
        self.requests: list[dict[str, Any]] = []
        self.status = status
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                receiver.requests.append(
                    {"path": self.path, "headers": dict(self.headers.items()), "body": body}
                )
                self.send_response(receiver.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args: Any) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hooks/runcomposer"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=10)


@pytest.fixture()
def receiver():
    made: list[Receiver] = []

    def factory(status: int = 204) -> Receiver:
        made.append(Receiver(status))
        return made[-1]

    yield factory
    for one in made:
        one.close()


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# -- observers defined here, configured by import path ---------------------------------

SEEN: dict[str, list[Any]] = {}


class RecordingObserver(ObserverBase):
    def __init__(self, name: str):
        self.name = name
        SEEN[name] = []

    def notify(self, event: Any) -> None:
        SEEN[self.name].append(event)


class FaultyObserver(ObserverBase):
    def notify(self, event: Any) -> None:
        raise RuntimeError(f"cannot cope with {event.kind}")


class QuotingObserver(ObserverBase):
    """Raises with what it was configured with in the message."""

    def __init__(self, token: str = ""):
        self.token = token

    def notify(self, event: Any) -> None:
        raise RuntimeError(f"the hook refused the token {self.token}")


def recording(name: str, **extra: Any) -> dict[str, Any]:
    return {"module": "tests.test_observers:RecordingObserver", "name": name, **extra}


# -- jsonl --------------------------------------------------------------------------------


class TestJsonl:
    def test_one_line_per_lifecycle_event(self, make_service, tmp_path):
        service = make_service(observers={"jsonl": {"path": "logs/events.jsonl"}})
        run_id = run_once(service, title="Anmeldung prüfen", labels={"suite": "nightly"})

        log = tmp_path / "logs" / "events.jsonl"  # relative to the config file
        lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert [line["kind"] for line in lines] == LIFECYCLE_OF_A_RUN
        assert {line["run_id"] for line in lines} == {run_id}
        assert [line["seq"] for line in lines] == sorted(line["seq"] for line in lines)
        completed = lines[-1]
        assert set(completed) == {"seq", "run_id", "at", "kind", "dispatch_id", "data"}
        assert completed["data"]["completion"] == "PASS"
        assert completed["data"]["summary"] == {"PASS": 3}
        assert completed["data"]["title"] == "Anmeldung prüfen"
        assert completed["data"]["labels"] == {"suite": "nightly"}
        assert "prüfen" in log.read_text(encoding="utf-8"), "kept as UTF-8, not escaped"
        # What the observer wrote is what the event stream holds.
        stored = {e.seq: e.kind for e in service.store.events(run_id)}
        assert all(stored[line["seq"]] == line["kind"] for line in lines)

    def test_it_appends_across_runs(self, make_service, tmp_path):
        service = make_service(observers={"jsonl": {"path": "events.jsonl"}})
        first, second = run_once(service), run_once(service, seed="b")
        lines = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
        assert [line["run_id"] for line in lines] == [first] * 5 + [second] * 5
        assert lines[-1]["data"]["completion"] == "FAIL"


# -- which events an observer is told about -------------------------------------------------


class TestEventFilter:
    def test_by_default_lifecycle_events_but_not_log_lines(self, make_service):
        service = make_service(observers={"all": recording("default")})
        run_id = compose(service, "Auth-SSO")
        service.bus.emit(run_id, "log", data={"message": "a runner said something"})
        service.dispatch(run_id, "demo", {"seed": "a"}, foreground=True)
        service.bus.flush(30)
        assert [event.kind for event in SEEN["default"]] == LIFECYCLE_OF_A_RUN

    def test_events_selects_kinds_with_wildcards(self, make_service):
        service = make_service(
            observers={
                "done": recording("done", events=["run.completed", "run.failed"]),
                "runs": recording("runs", events=["run.*"]),
                "everything": recording("everything", events=["*"]),
            }
        )
        run_id = compose(service, "Auth-SSO")
        service.bus.emit(run_id, "log", data={"message": "a runner said something"})
        service.dispatch(run_id, "demo", {"seed": "a"}, foreground=True)
        service.bus.flush(30)
        assert [e.kind for e in SEEN["done"]] == ["run.completed"]
        assert [e.kind for e in SEEN["runs"]] == [
            "run.composed", "run.dispatched", "run.running", "run.completed",
        ]
        assert [e.kind for e in SEEN["everything"]] == [
            "run.composed", "log", "run.dispatched", "run.running", "delivery.recorded",
            "run.completed",
        ]

    def test_notifications_arrive_in_event_order(self, make_service):
        service = make_service(observers={"order": recording("order", events=["*"])})
        for seed in ("a", "b", "c"):
            run_once(service, seed=seed)
        seqs = [event.seq for event in SEEN["order"]]
        assert len(seqs) == 15 and seqs == sorted(seqs)

    def test_a_refused_dispatch_is_a_lifecycle_event_too(self, make_service):
        service = make_service(observers={"all": recording("refusals")})
        run_id = compose(service, "Auth-SSO")
        service.export(run_id)
        service.cancel(run_id)
        service.bus.flush(30)
        kinds = [event.kind for event in SEEN["refusals"]]
        assert kinds == ["run.composed", "run.awaiting_results", "run.cancelled"]

    @pytest.mark.parametrize("bad", ["run.completed", [1, 2], {"kind": "x"}])
    def test_a_malformed_events_option_fails_startup(self, make_service, bad):
        service = make_service(observers={"broken": recording("broken", events=bad)})
        with pytest.raises(ConfigError, match=r"observers\.broken\.events must be a list"):
            service.validate()

    def test_an_unknown_observer_option_fails_startup_naming_the_accepted_ones(self, make_service):
        service = make_service(observers={"jsonl": {"path": "e.jsonl", "pth": "typo"}})
        with pytest.raises(ConfigError) as refused:
            service.validate()
        assert "observers.jsonl" in str(refused.value) and "['pth']" in str(refused.value)
        assert "accepts: ['path']" in str(refused.value)


# -- webhook ----------------------------------------------------------------------------


class TestWebhook:
    def test_events_are_posted_as_signed_json(self, make_service, receiver, monkeypatch):
        target = receiver()
        monkeypatch.setenv("SHOP_HOOK_SECRET", "s3cr3t-of-the-shop")
        service = make_service(
            observers={
                "webhook": {
                    "url": target.url,
                    "secret": "${env:SHOP_HOOK_SECRET}",
                    "headers": {"X-Shop-Team": "checkout"},
                }
            }
        )
        run_id = run_once(service, title="Anmeldung prüfen")

        assert [json.loads(r["body"])["kind"] for r in target.requests] == LIFECYCLE_OF_A_RUN
        for request in target.requests:
            headers = {key.lower(): value for key, value in request["headers"].items()}
            body = request["body"]
            payload = json.loads(body.decode("utf-8"))
            assert request["path"] == "/hooks/runcomposer"
            assert headers["content-type"] == "application/json"
            assert headers["x-runcomposer-event"] == payload["kind"]
            assert headers["x-shop-team"] == "checkout"
            assert payload["run_id"] == run_id
            # The signature verifies over the raw bytes received.
            expected = hmac.new(b"s3cr3t-of-the-shop", body, hashlib.sha256).hexdigest()
            assert headers["x-runcomposer-signature"] == f"sha256={expected}"
        last = json.loads(target.requests[-1]["body"])
        assert last["data"]["completion"] == "PASS" and last["data"]["title"] == "Anmeldung prüfen"
        assert warnings_of(service, run_id) == []

    def test_without_a_secret_nothing_is_signed(self, make_service, receiver):
        target = receiver()
        service = make_service(observers={"webhook": {"url": target.url, "events": ["run.completed"]}})
        run_once(service)
        (request,) = target.requests
        assert "x-runcomposer-signature" not in {key.lower() for key in request["headers"]}

    def test_a_missing_secret_variable_fails_startup_naming_it(self, make_service, monkeypatch):
        monkeypatch.delenv("SHOP_HOOK_SECRET", raising=False)
        service = make_service(
            observers={"webhook": {"url": "http://127.0.0.1:9/h", "secret": "${env:SHOP_HOOK_SECRET}"}}
        )
        with pytest.raises(ConfigError, match="SHOP_HOOK_SECRET"):
            service.validate()

    def test_a_non_2xx_answer_is_a_warning_and_the_run_still_completes(self, make_service, receiver):
        target = receiver(status=500)
        service = make_service(observers={"webhook": {"url": target.url}})
        run_id = run_once(service)
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "PASS")
        assert len(target.requests) == 5, "every event was still attempted"
        warnings = warnings_of(service, run_id)
        assert len(warnings) == 5
        assert all("observer 'webhook' failed on" in w and "webhook answered 500" in w for w in warnings)
        warning_events = [e for e in service.store.events(run_id) if e.kind == "warning"]
        assert warning_events[0].data["observer"] == "webhook"

    def test_an_unreachable_receiver_is_a_warning_and_the_run_still_completes(self, make_service):
        url = f"http://127.0.0.1:{closed_port()}/nobody-home"
        service = make_service(
            observers={"webhook": {"url": url, "timeout_s": 5, "events": ["run.completed"]}}
        )
        run_id = run_once(service)
        assert service.require_run(run_id).completion == "PASS"
        (warning,) = warnings_of(service, run_id)
        assert "failed on run.completed" in warning and "webhook unreachable" in warning

    def test_a_url_that_is_not_http_is_refused_at_startup(self, make_service):
        service = make_service(observers={"webhook": {"url": "file:///etc/hosts"}})
        with pytest.raises(ConfigError, match="url must start with http"):
            service.validate()


# -- command -----------------------------------------------------------------------------

_HOOK_SCRIPT = """\
import json, os, sys
record = {key: value for key, value in os.environ.items() if key.startswith("RUNCOMPOSER_")}
record["stdin"] = json.loads(sys.stdin.buffer.read().decode("utf-8"))  # UTF-8 by contract
record["argv"] = sys.argv[2:]
with open(sys.argv[1], "a", encoding="utf-8") as out:
    out.write(json.dumps(record) + "\\n")
"""


@pytest.fixture()
def hook(tmp_path: Path):
    script = tmp_path / "notify_hook.py"
    script.write_text(_HOOK_SCRIPT, encoding="utf-8")
    out = tmp_path / "hook-calls.jsonl"

    def calls() -> list[dict[str, Any]]:
        if not out.exists():
            return []
        return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]

    return script, out, calls


class TestCommand:
    def test_an_argv_command_gets_the_event_in_its_environment_and_on_stdin(self, make_service, hook):
        script, out, calls = hook
        service = make_service(
            observers={
                "command": {
                    "command": [sys.executable, str(script), str(out), "fixed-argument"],
                    "events": ["run.completed"],
                }
            }
        )
        run_id = run_once(service, title="Kasse; rm -rf $HOME `whoami`")

        (call,) = calls()
        dispatch_id = service.require_run(run_id).latest_dispatch.dispatch_id
        assert call["RUNCOMPOSER_EVENT"] == "run.completed"
        assert call["RUNCOMPOSER_RUN_ID"] == run_id
        assert call["RUNCOMPOSER_DISPATCH_ID"] == dispatch_id
        assert call["RUNCOMPOSER_STATE"] == "COMPLETE"
        assert call["RUNCOMPOSER_COMPLETION"] == "PASS"
        # A hostile title is data in the environment, never part of a command line.
        assert call["RUNCOMPOSER_TITLE"] == "Kasse; rm -rf $HOME `whoami`"
        assert call["argv"] == ["fixed-argument"]
        assert call["RUNCOMPOSER_AT"].endswith("Z")
        assert json.loads(call["RUNCOMPOSER_EVENT_JSON"]) == call["stdin"]
        assert call["stdin"]["kind"] == "run.completed" and call["stdin"]["run_id"] == run_id
        assert call["stdin"]["data"]["summary"] == {"PASS": 3}
        assert warnings_of(service, run_id) == []

    def test_a_title_in_any_script_reaches_the_command_as_utf_8(self, make_service, hook):
        """``✓`` and ``→`` are outside the text encoding Windows uses by
        default: before, writing the event to the command's stdin raised
        there, and the command never ran."""
        script, out, calls = hook
        service = make_service(
            observers={"command": {"command": [sys.executable, str(script), str(out)], "events": ["run.completed"]}}
        )
        title = "Kasse \u2713 \u2192 \u20ac 12"
        run_id = run_once(service, title=title)
        (call,) = calls()
        assert call["stdin"]["data"]["title"] == title
        assert call["RUNCOMPOSER_TITLE"] == title
        assert warnings_of(service, run_id) == []

    def test_a_string_command_is_run_by_the_shell(self, make_service, hook):
        script, out, calls = hook
        service = make_service(
            observers={
                "command": {
                    "command": f'"{sys.executable}" "{script}" "{out}"',
                    "events": ["run.composed", "run.completed"],
                }
            }
        )
        run_id = run_once(service)
        assert [call["RUNCOMPOSER_EVENT"] for call in calls()] == ["run.composed", "run.completed"]
        composed = calls()[0]
        assert composed["RUNCOMPOSER_RUN_ID"] == run_id
        assert composed["RUNCOMPOSER_DISPATCH_ID"] == "" and composed["RUNCOMPOSER_COMPLETION"] == ""

    def test_a_failing_command_is_a_warning_and_the_run_still_completes(self, make_service):
        service = make_service(
            observers={
                "command": {
                    "command": [
                        sys.executable, "-c",
                        "import sys; sys.stderr.write('pager is down'); sys.exit(4)",
                    ],
                    "events": ["run.completed"],
                }
            }
        )
        run_id = run_once(service)
        assert service.require_run(run_id).completion == "PASS"
        (warning,) = warnings_of(service, run_id)
        assert "observer command exited 4" in warning and "pager is down" in warning

    def test_a_command_that_overruns_its_timeout_is_a_warning(self, make_service):
        service = make_service(
            observers={
                "command": {
                    # A command that never comes back by itself.
                    "command": [sys.executable, "-c", "import threading; threading.Event().wait()"],
                    "timeout_s": 0.2,
                    "events": ["run.completed"],
                }
            }
        )
        run_id = run_once(service)
        (warning,) = warnings_of(service, run_id)
        assert "did not finish within 0.2s" in warning

    @pytest.mark.parametrize("command", ["", "   ", [], ["ok", 5], 7])
    def test_a_malformed_command_fails_startup(self, make_service, command):
        service = make_service(observers={"command": {"command": command}})
        with pytest.raises(ConfigError, match="command observer"):
            service.validate()


# -- the port's promise ---------------------------------------------------------------------


class TestAnObserverNeverAffectsARun:
    def test_an_observer_that_raises_is_recorded_and_everything_else_goes_on(self, make_service):
        service = make_service(
            observers={
                "faulty": {"module": "tests.test_observers:FaultyObserver"},
                "after": recording("after-faulty"),
            }
        )
        run_id = run_once(service)

        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "PASS")
        # The observer configured after the faulty one was still told everything.
        assert [event.kind for event in SEEN["after-faulty"]] == LIFECYCLE_OF_A_RUN
        warnings = warnings_of(service, run_id)
        assert warnings == [
            f"observer 'faulty' failed on {kind}: RuntimeError: cannot cope with {kind}"
            for kind in LIFECYCLE_OF_A_RUN
        ]
        # The failure is recorded on the run, not fed back to observers.
        assert "warning" not in [event.kind for event in SEEN["after-faulty"]]

    def test_an_observer_that_quotes_a_secret_is_recorded_and_logged_without_it(
        self, make_service, monkeypatch, caplog
    ):
        """What an observer says when it fails is recorded on the run and
        served by the API: a configured secret in it is blanked there and in
        the log line, traceback included."""
        monkeypatch.setenv("SHOP_HOOK_TOKEN", "hook-token-example-42")
        service = make_service(
            observers={"hook": {"module": "tests.test_observers:QuotingObserver", "token": "${env:SHOP_HOOK_TOKEN}"}}
        )
        with caplog.at_level("WARNING", logger="runcomposer"):
            run_id = run_once(service)
        warnings = warnings_of(service, run_id)
        assert warnings and all("the hook refused the token" in warning for warning in warnings)
        stored = repr([event.data for event in service.store.events(run_id)])
        assert "hook-token-example-42" not in stored
        failures = [record for record in caplog.records if "observer 'hook' failed on" in record.getMessage()]
        assert failures
        for record in failures:
            assert "hook-token-example-42" not in record.getMessage()
            assert "Traceback" in record.getMessage() and record.exc_info is None

    def test_several_observers_of_one_plugin_under_different_keys(self, make_service, tmp_path):
        service = make_service(
            observers={
                "audit": {"plugin": "jsonl", "path": "audit.jsonl"},
                "results": {"plugin": "jsonl", "path": "results.jsonl", "events": ["run.completed"]},
            }
        )
        run_once(service)
        assert len((tmp_path / "audit.jsonl").read_text().splitlines()) == 5
        assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 1

    def test_no_observers_configured_is_fine(self, service):
        run_id = run_once(service)
        assert service.require_run(run_id).completion == "PASS"
