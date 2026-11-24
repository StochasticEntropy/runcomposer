"""Request bodies read under a limit, counted while they arrive.

One way to read a body for every route that has a limit: a declared
``Content-Length`` beyond it is refused before a byte is read, and a body
that passes it while it arrives is refused at that byte — a ``413`` in the
project's error shape, the connection closed so that no buffer takes in the
rest. The executor routes (``/results``, ``/live``, ``/log``) read under
``core.ingestion.max_upload_mb``; every other route of the JSON API under
``core.api.max_body_mb`` (:class:`BodyLimitRoute`).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute
from starlette.requests import ClientDisconnect
from starlette.types import Message

from runcomposer.core.errors import InvalidRequest, PayloadTooLarge

from .errors import error_response

__all__ = ["BodyLimitRoute", "api_body_limit", "declared_too_large", "read_within"]


def declared_too_large(request: Request, limit: int) -> bool:
    """Whether the body's declared ``Content-Length`` is over ``limit``."""
    declared = request.headers.get("content-length", "")
    return declared.isdigit() and int(declared) > limit


async def read_within(request: Request, limit: int, too_large: PayloadTooLarge) -> Request | Response:
    """The request with its body read — counted while it arrives — and
    replayed from memory; or the ``413`` (or the answer to a client that
    went away) that ends it."""
    body = bytearray()
    try:
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                raise too_large
    except PayloadTooLarge as exc:
        return error_response(request, exc, close=True)
    except ClientDisconnect:
        return error_response(request, InvalidRequest("the client went away"), close=True, unread=False)
    replayed = bytes(body)

    async def replay() -> Message:
        return {"type": "http.request", "body": replayed, "more_body": False}

    return Request(request.scope, replay)


def api_body_limit(request: Request) -> tuple[int, PayloadTooLarge]:
    """``core.api.max_body_mb`` in bytes, and the error that names it."""
    megabytes = request.app.state.service.config.api["max_body_mb"]
    return int(megabytes) * 1024 * 1024, PayloadTooLarge(
        f"request body exceeds core.api.max_body_mb ({megabytes} MB)"
    )


class BodyLimitRoute(APIRoute):
    """A route of the JSON API whose body is read under
    ``core.api.max_body_mb`` before FastAPI parses it."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        if self.body_field is None:
            return handler

        async def bounded(request: Request) -> Response:
            limit, too_large = api_body_limit(request)
            if declared_too_large(request, limit):
                return error_response(request, too_large, close=True)
            read = await read_within(request, limit, too_large)
            if isinstance(read, Response):
                return read
            return await handler(read)

        return bounded
