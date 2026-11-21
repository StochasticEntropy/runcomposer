"""A refusal that ends the connection before the request body was read to its
end (DESIGN.md §5): the whole answer goes out first, then what the client
still sends is discarded for a bounded time, and only then does the response
end — a client in the middle of sending reads the refusal, not a reset.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from runcomposer.http import errors
from runcomposer.http.errors import ClosingJSONResponse

ANSWER = {"detail": "this token does not belong to the run", "code": "token-rejected"}


class Wire:
    """The two ends a response is given: what it sends, in order, and a
    script of what the client still sends."""

    def __init__(self, incoming: list[dict[str, Any]], *, then_silent: bool = False):
        self.incoming = list(incoming)
        self.then_silent = then_silent
        self.log: list[Any] = []

    async def receive(self) -> dict[str, Any]:
        if self.incoming:
            message = self.incoming.pop(0)
            self.log.append(("received", message["type"], message.get("more_body", False)))
            return message
        assert self.then_silent, "the response asked for more than the client sends"
        self.log.append("waiting")
        await asyncio.Event().wait()  # a client that neither sends nor goes away
        raise AssertionError("unreachable")

    async def send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.log.append(("start", message["status"]))
        else:
            self.log.append(("body", message.get("body", b""), message.get("more_body", False)))


def respond(wire: Wire, *, unread: bool = True) -> list[Any]:
    response = ClosingJSONResponse(
        status_code=403, content=ANSWER, headers={"Connection": "close"}, unread=unread
    )
    asyncio.run(response({"type": "http"}, wire.receive, wire.send))
    return wire.log


def chunk(more: bool = True) -> dict[str, Any]:
    return {"type": "http.request", "body": b"x" * 1024, "more_body": more}


def test_the_whole_answer_is_out_before_anything_more_is_taken_in_and_the_rest_is_discarded():
    log = respond(Wire([chunk(), chunk(), chunk(), {"type": "http.disconnect"}]))
    assert log[0] == ("start", 403)
    kind, body, more = log[1]
    assert kind == "body" and json.loads(body) == ANSWER and more is True
    assert log[2:6] == [
        ("received", "http.request", True), ("received", "http.request", True),
        ("received", "http.request", True), ("received", "http.disconnect", False),
    ]
    assert log[6:] == [("body", b"", False)], "the response ends once the client went away"


def test_a_body_that_ends_ends_the_wait():
    log = respond(Wire([chunk(), chunk(more=False)]))
    assert log[-2:] == [("received", "http.request", False), ("body", b"", False)]


def test_a_client_that_neither_sends_nor_leaves_is_waited_for_only_so_long(monkeypatch):
    monkeypatch.setattr(errors, "LINGER_S", 0.05)
    log = respond(Wire([chunk()], then_silent=True))
    assert log[-2:] == ["waiting", ("body", b"", False)]


def test_a_body_read_to_its_end_is_not_waited_for():
    wire = Wire([])  # asking for more would fail the test
    log = respond(wire, unread=False)
    assert log[0] == ("start", 403)
    assert [entry for entry in log if entry[0] == "body"] == [
        ("body", json.dumps(ANSWER, separators=(",", ":")).encode(), False)
    ]


@pytest.mark.parametrize("seconds", [errors.LINGER_S])
def test_the_bound_is_two_seconds(seconds):
    assert seconds == 2.0
