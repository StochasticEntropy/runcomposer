"""An execution runcomposer did not start asks whether its dispatch was
cancelled (DESIGN.md §5): ``GET /api/v1/runs/{id}/dispatches/{dispatch_id}/state``,
authorized exactly as a push for that dispatch, and
``RemoteLiveChannel.cancel_requested()`` on the channel the execution
already holds.

The situation is the one a CI build is in: the `jenkins` runner in callback
mode hands the build the dispatch's token and callback URL, and the stop
it sends when the run is cancelled can be lost by the CI server. Everything
runs against a real server on a socket of its own (``live_server``).
"""

from __future__ import annotations

import inspect
import json
import logging
import pickle
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from runcomposer.core import remote
from runcomposer.sdk import RemoteLiveChannel

from .conftest import compose, wait_until
from .test_jenkins import JOB, RUNNER, closed_port, jenkins, live_server

__all__ = ["jenkins", "live_server"]  # the fixtures


def http(method: str, url: str, token: str | None = None, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Runcomposer-Token"] = token
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def handed_out(live_server: Any, jenkins: Any, number: int = 1) -> dict[str, str]:
    """What the build was given: the callback URL, the token, the dispatch id."""
    wait_until(lambda: (JOB, number) in jenkins.builds, what=f"build {number} to start")
    return jenkins.builds[(JOB, number)].params


def dispatch_over_http(live_server: Any) -> str:
    status, body = live_server.post(
        "/api/v1/runs", {"selection": {"tag_filter": "Payments-Cards"}, "dispatch": {"runner": RUNNER}}
    )
    assert status == 201, body
    return str(body["run"]["id"])


def state_url(params: dict[str, str], dispatch_id: str | None = None) -> str:
    run_url = params["CALLBACK_URL"].rsplit("/results", 1)[0]
    return f"{run_url}/dispatches/{dispatch_id or params['DISPATCH_ID']}/state"


class Serving:
    """A socket that answers every connection with ``answer(connection)`` —
    for answers no real server gives."""

    def __init__(self, answer: Any):
        self.answer = answer
        self.listening = socket.socket()
        self.listening.bind(("127.0.0.1", 0))
        self.listening.listen(8)
        self.threads: list[threading.Thread] = []

    def __enter__(self) -> str:
        def accept() -> None:
            while True:
                try:
                    connection, _ = self.listening.accept()
                except OSError:
                    return  # closed
                thread = threading.Thread(target=self.answer, args=(connection,), daemon=True)
                thread.start()
                self.threads.append(thread)

        self.accepting = threading.Thread(target=accept, daemon=True)
        self.accepting.start()
        return f"http://127.0.0.1:{self.listening.getsockname()[1]}"

    def __exit__(self, *_exc: Any) -> None:
        self.listening.close()
        self.accepting.join(5)
        for thread in self.threads:
            thread.join(5)


@pytest.fixture()
def awaiting(live_server, jenkins):
    """A dispatch in callback mode whose build runs until it is stopped."""
    jenkins.add_job(JOB, lambda build: build.stop.wait(30))
    run_id = dispatch_over_http(live_server)
    params = handed_out(live_server, jenkins)
    # The build exists before the dispatcher has written that it now waits
    # for results: a test that writes a state itself must come after that.
    wait_until(
        lambda: live_server.service.store.get_dispatch(params["DISPATCH_ID"]).state == "AWAITING_RESULTS",
        what="the dispatch to await its results",
    )
    return run_id, params


class TestTheEndpoint:
    def test_it_says_whether_the_dispatch_should_stop(self, live_server, awaiting):
        run_id, params = awaiting
        token, dispatch_id = params["INGEST_TOKEN"], params["DISPATCH_ID"]
        assert http("GET", state_url(params), token) == (200, {
            "run_id": run_id, "dispatch_id": dispatch_id, "state": "AWAITING_RESULTS",
            "cancel_requested": False, "stop": False,
        })
        status, _ = live_server.post(f"/api/v1/runs/{run_id}/cancel", {})
        assert status == 202
        assert http("GET", state_url(params), token) == (200, {
            "run_id": run_id, "dispatch_id": dispatch_id, "state": "CANCELLED",
            "cancel_requested": True, "stop": True,
        })

    def test_a_cancel_recorded_before_the_dispatch_has_settled_says_stop(self, live_server, awaiting):
        """What a cancel writes first — the request, in the store — before
        the process that executes the dispatch has closed it."""
        _run_id, params = awaiting
        live_server.service.store.update_dispatch(params["DISPATCH_ID"], cancel_requested=True)
        status, state = http("GET", state_url(params), params["INGEST_TOKEN"])
        assert status == 200
        assert (state["state"], state["cancel_requested"], state["stop"]) == ("AWAITING_RESULTS", True, True)

    @pytest.mark.parametrize("state", ["DISPATCHED", "RUNNING", "AWAITING_RESULTS", "COMPLETE", "FAILED", "STALE"])
    def test_only_a_cancel_says_stop(self, live_server, awaiting, state):
        """A dispatch that is over for another reason — complete, failed,
        left behind by its process — is not one to stop: its results may
        still be pushed."""
        _run_id, params = awaiting
        live_server.service.store.update_dispatch(params["DISPATCH_ID"], state=state)
        status, answer = http("GET", state_url(params), params["INGEST_TOKEN"])
        assert status == 200
        assert (answer["state"], answer["cancel_requested"], answer["stop"]) == (state, False, False)

    def test_it_is_refused_as_a_push_would_be(self, live_server, jenkins, awaiting):
        run_id, params = awaiting
        token, dispatch_id = params["INGEST_TOKEN"], params["DISPATCH_ID"]
        live = params["CALLBACK_URL"].rsplit("/results", 1)[0] + "/live"
        update = {"updates": [{"shard": "1", "item_id": "Shop.Payments.Cards.T001", "status": "RUNNING"}]}

        def both(headers_token: str | None, url: str, push_url: str, body: dict) -> tuple[tuple, tuple]:
            asked = http("GET", url, headers_token)
            pushed = http("POST", push_url, headers_token, body)
            return (asked[0], asked[1]["code"]), (pushed[0], pushed[1]["code"])

        # No token, a wrong one.
        asked, pushed = both(None, state_url(params), live, update)
        assert asked == pushed == (401, "token-required")
        asked, pushed = both("rct_not-the-token", state_url(params), live, update)
        assert asked == pushed == (403, "token-rejected")
        # An unknown run.
        unknown = state_url(params).replace(run_id, "01NOSUCHRUN000000000000000")
        asked, pushed = both(token, unknown, live.replace(run_id, "01NOSUCHRUN000000000000000"), update)
        assert asked == pushed == (404, "unknown-run")
        # Another dispatch of the same run: the run dispatched again.
        assert live_server.post(f"/api/v1/runs/{run_id}/cancel", {})[0] == 202
        assert live_server.post(f"/api/v1/runs/{run_id}/dispatch", {"runner": RUNNER})[0] == 202
        second = handed_out(live_server, jenkins, 2)
        assert second["DISPATCH_ID"] != dispatch_id
        asked, pushed = both(second["INGEST_TOKEN"], state_url(params), live, {**update, "dispatch": dispatch_id})
        assert asked == pushed == (403, "token-rejected")
        # A dispatch id nobody knows, with a dispatch's token: another dispatch's id.
        asked = http("GET", state_url(params, "01NOSUCHDISPATCH0000000000"), token)
        assert (asked[0], asked[1]["code"]) == (403, "token-rejected")
        # Its own token still asks about its own dispatch.
        assert http("GET", state_url(second), second["INGEST_TOKEN"])[1]["stop"] is False

    def test_without_tokens_it_is_open_and_an_unknown_dispatch_is_a_404(self, make_client):
        client, service = make_client(core={"ingestion": {"tokens": "disabled"}})
        run_id = compose(service, "Auth-SSO")
        exported = service.export(run_id)
        dispatch_id = exported.dispatch.dispatch_id
        answer = client.get(f"/api/v1/runs/{run_id}/dispatches/{dispatch_id}/state")
        assert answer.status_code == 200 and answer.json()["state"] == "AWAITING_RESULTS"
        other_run = compose(service, "Auth-SSO")
        for unknown in ("01NOSUCHDISPATCH0000000000", service.export(other_run).dispatch.dispatch_id):
            asked = client.get(f"/api/v1/runs/{run_id}/dispatches/{unknown}/state")
            pushed = client.post(f"/api/v1/runs/{run_id}/log", json={"dispatch": unknown, "lines": [{"message": "x"}]})
            assert asked.status_code == pushed.status_code == 404
            assert asked.json()["code"] == pushed.json()["code"] == "unknown-dispatch"

    def test_it_reads_no_verdicts(self, live_server, awaiting, monkeypatch):
        """However many results the run holds, the answer costs the run
        record and one dispatch row."""
        _run_id, params = awaiting
        store = live_server.service.store

        def not_here(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("the dispatch's state needs no verdicts")

        for name in ("verdicts_for", "live_verdicts", "events", "latest_events", "artifact_refs"):
            monkeypatch.setattr(store, name, not_here)
        assert http("GET", state_url(params), params["INGEST_TOKEN"])[0] == 200

    def test_the_schema_lists_it_with_the_token_headers(self, make_client):
        client, _service = make_client()
        operation = client.get("/openapi.json").json()["paths"][
            "/api/v1/runs/{run_id}/dispatches/{dispatch_id}/state"]["get"]
        headers = {p["name"] for p in operation["parameters"] if p["in"] == "header"}
        assert headers == {"authorization", "x-runcomposer-token"}


def checkout_suite(channel: RemoteLiveChannel, item_ids: list[str]) -> list[str]:
    """The loop of docs/connectors.md §3.3, the part on asking for a cancel,
    as the guide writes it: an execution elsewhere that stops between two
    tests once runcomposer says its dispatch was cancelled."""
    done = []
    for item_id in item_ids:
        if channel.cancel_requested():
            channel.log("checkout suite: the run was cancelled — stopping", level="warning")
            break
        channel.live("1", item_id, "RUNNING")
        done.append(item_id)  # … the test itself …
        channel.live("1", item_id, "PASS")
    return done


def test_the_guide_writes_the_loop_as_it_is_run():
    guide = (Path(__file__).resolve().parent.parent / "docs" / "connectors.md").read_text(encoding="utf-8")
    signature, _docstring, body = inspect.getsource(checkout_suite).split('"""', 2)
    assert signature.strip() + body in guide


class TestTheChannel:
    def channel(self, params: dict[str, str], **options: Any) -> RemoteLiveChannel:
        return RemoteLiveChannel(
            params["CALLBACK_URL"], params["INGEST_TOKEN"], dispatch_id=params["DISPATCH_ID"], **options
        )

    @pytest.fixture()
    def asked(self, monkeypatch):
        """The state requests the channel sends (real ones, counted)."""
        sent: list[str] = []
        real = remote._open

        def counting(request: Any, *args: Any) -> Any:
            if request.full_url.endswith("/state"):
                sent.append(request.full_url)
            return real(request, *args)

        monkeypatch.setattr(remote, "_open", counting)
        return sent

    def test_false_until_the_run_is_cancelled_then_true_for_good(self, live_server, awaiting, asked):
        run_id, params = awaiting
        channel = pickle.loads(pickle.dumps(self.channel(params, cancel_check_interval_s=0)))
        assert channel.cancel_requested() is False
        assert live_server.post(f"/api/v1/runs/{run_id}/cancel", {})[0] == 202
        assert channel.cancel_requested() is True
        before = len(asked)
        assert all(channel.cancel_requested() for _ in range(20))
        assert len(asked) == before == 2, "once true, nothing more is asked"
        assert channel.failures == 0

    def test_the_loop_of_the_guide_stops_between_two_tests(self, live_server, awaiting):
        run_id, params = awaiting
        channel = self.channel(params, cancel_check_interval_s=0)
        items = [f"Shop.Payments.Cards.T00{n}" for n in range(1, 4)]
        assert checkout_suite(channel, items[:1]) == items[:1]
        assert live_server.post(f"/api/v1/runs/{run_id}/cancel", {})[0] == 202
        assert checkout_suite(channel, items[1:]) == []

    def test_the_interval_bounds_the_requests(self, awaiting, asked):
        _run_id, params = awaiting
        slow = self.channel(params, cancel_check_interval_s=60)
        assert not any(slow.cancel_requested() for _ in range(200))
        assert len(asked) == 1
        asked.clear()
        brisk = self.channel(params, cancel_check_interval_s=0.3)
        started = time.monotonic()
        calls = 0
        while time.monotonic() - started < 1.0:
            brisk.cancel_requested()
            calls += 1
        elapsed = time.monotonic() - started
        assert calls > 20
        assert 2 <= len(asked) <= int(elapsed / 0.3) + 1, (len(asked), elapsed)

    def test_the_default_asks_at_most_every_five_seconds(self, awaiting, asked):
        _run_id, params = awaiting
        channel = self.channel(params)
        assert channel.cancel_check_interval_s == 5.0
        assert not any(channel.cancel_requested() for _ in range(50)) and len(asked) == 1

    def test_a_refusal_and_an_unknown_dispatch_are_not_known(self, awaiting, asked):
        _run_id, params = awaiting
        wrong = RemoteLiveChannel(params["CALLBACK_URL"], "rct_not-the-token",
                                  dispatch_id=params["DISPATCH_ID"], cancel_check_interval_s=0)
        assert wrong.cancel_requested() is False
        nobody = RemoteLiveChannel(params["CALLBACK_URL"], params["INGEST_TOKEN"],
                                   dispatch_id="01NOSUCHDISPATCH0000000000", cancel_check_interval_s=0)
        assert nobody.cancel_requested() is False
        assert len(asked) == 2

    def test_a_server_that_cannot_be_reached_is_not_known_and_raises_nothing(self, asked):
        channel = RemoteLiveChannel(
            f"http://127.0.0.1:{closed_port()}/api/v1/runs/01RUN/results", "rct_unreachable",
            dispatch_id="01DISPATCH", timeout_s=0.5, cancel_check_interval_s=0,
        )
        started = time.monotonic()
        assert channel.cancel_requested() is False and channel.cancel_requested() is False
        assert time.monotonic() - started < 10 and len(asked) == 2

    def test_an_answer_that_arrives_a_little_at_a_time_is_given_up(self):
        """Each part comes within ``timeout_s``, the whole never does: the
        call ends about ``timeout_s`` after the answer began, not when the
        server is done."""
        done = threading.Event()

        def trickle(connection: socket.socket) -> None:
            with connection:
                connection.recv(65536)
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
                try:
                    for byte in b'{"stop": true' + b" " * 200 + b"}":
                        if done.wait(0.1):
                            return
                        connection.sendall(bytes([byte]))
                except OSError:
                    pass  # the channel closed the connection

        with Serving(trickle) as base:
            channel = RemoteLiveChannel(
                f"{base}/api/v1/runs/01RUN/results", "rct_trickle", dispatch_id="01DISPATCH",
                timeout_s=0.5, cancel_check_interval_s=0,
            )
            started = time.monotonic()
            try:
                assert channel.cancel_requested() is False
            finally:
                done.set()
            assert time.monotonic() - started < 5, "the whole answer would have taken 20 seconds"

    def test_an_answer_far_too_long_is_not_read(self):
        def flood(connection: socket.socket) -> None:
            with connection:
                connection.recv(65536)
                body = b'{"stop": true, "padding": "' + b"x" * (1024 * 1024) + b'"}'
                head = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                try:
                    connection.sendall(head + body)
                except OSError:
                    pass  # the channel closed the connection before it was all sent

        with Serving(flood) as base:
            channel = RemoteLiveChannel(
                f"{base}/api/v1/runs/01RUN/results", "rct_flood", dispatch_id="01DISPATCH",
                timeout_s=5, cancel_check_interval_s=0,
            )
            assert channel.cancel_requested() is False

    @pytest.mark.parametrize(("length", "read"), [(64 * 1024, True), (64 * 1024 + 1, False)])
    def test_an_answer_of_64_kib_is_read_and_one_byte_more_is_not(self, length, read):
        frame = b'{"stop": true, "padding": ""}'
        body = b'{"stop": true, "padding": "' + b"x" * (length - len(frame)) + b'"}'
        assert len(body) == length

        def answer(connection: socket.socket) -> None:
            with connection:
                connection.recv(65536)
                head = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                try:
                    connection.sendall(head + body)
                except OSError:
                    pass  # the channel closed the connection before it was all sent

        with Serving(answer) as base:
            channel = RemoteLiveChannel(
                f"{base}/api/v1/runs/01RUN/results", "rct_limit", dispatch_id="01DISPATCH",
                timeout_s=5, cancel_check_interval_s=0,
            )
            assert channel.cancel_requested() is read

    def test_without_a_dispatch_id_nothing_is_asked(self, awaiting, asked):
        _run_id, params = awaiting
        channel = RemoteLiveChannel(params["CALLBACK_URL"], params["INGEST_TOKEN"], cancel_check_interval_s=0)
        assert channel.state_url is None
        assert channel.cancel_requested() is False and asked == []

    def test_a_negative_interval_is_refused_where_it_is_given(self):
        with pytest.raises(ValueError, match="cancel_check_interval_s"):
            RemoteLiveChannel("http://127.0.0.1:1/api/v1/runs/r/results", None, cancel_check_interval_s=-1)

    def test_the_token_reaches_no_log_event_or_answer(self, live_server, awaiting, caplog):
        run_id, params = awaiting
        token = params["INGEST_TOKEN"]
        caplog.set_level(logging.DEBUG)
        answers = [
            http("GET", state_url(params), token),
            http("GET", state_url(params, "01NOSUCHDISPATCH0000000000"), token),
            http("GET", state_url(params).replace(run_id, "01NOSUCHRUN000000000000000"), token),
        ]
        channel = self.channel(params, cancel_check_interval_s=0)
        channel.cancel_requested()
        live_server.post(f"/api/v1/runs/{run_id}/cancel", {})
        assert channel.cancel_requested() is True
        answers.append(http("GET", state_url(params), token))
        assert token not in caplog.text
        assert token not in json.dumps(answers)
        events = [event.data for event in live_server.service.store.events(run_id)]
        assert events and token not in json.dumps(events, default=str)
