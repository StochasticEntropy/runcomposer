"""Errors over HTTP (DESIGN.md §9): the ONE place an exception type becomes a
status code.

Every error answer has the same body — ``{"detail": <string>, "code":
<stable identifier>}`` — whatever raised it: a domain error, a request that
failed validation, a missing route. ``detail`` is always a string; a
validation failure adds ``fields`` (one entry per offending field), and a few
errors add context of their own (``run_kept``, ``quarantine_entry``,
``parameter``).
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Receive, Scope, Send

from runcomposer.core.errors import (
    AuthRequired,
    ConfigError,
    Conflict,
    Forbidden,
    IngestError,
    InvalidParameter,
    InvalidRequest,
    NotFound,
    ParseError,
    PayloadTooLarge,
    RunComposerError,
)
from runcomposer.core.taxonomy import TaxonomyError

__all__ = [
    "CrossSiteRequest",
    "UnknownHost",
    "error_body",
    "error_response",
    "install_error_handlers",
    "status_of",
]

log = logging.getLogger("runcomposer.http")

# First match wins, so subclasses come before their bases.
_STATUS: tuple[tuple[type[BaseException], int], ...] = (
    (NotFound, 404),
    (AuthRequired, 401),
    (Forbidden, 403),
    (PayloadTooLarge, 413),
    (ParseError, 422),
    (Conflict, 409),
    (InvalidRequest, 400),
    # Reaching a request with these means the deployment broke under a running
    # server (the taxonomy file was edited, a plugin vanished): a server-side
    # fault that still says exactly what is wrong.
    (TaxonomyError, 500),
    (ConfigError, 500),
)
# Why a bundle was refused decides the status, not the fact that it was.
_INGEST_STATUS = {
    "unknown-run": 404,
    "unknown-dispatch": 404,
    "sha-mismatch": 409,
    "marker-conflict": 409,
    "dispatch-cancelled": 409,
    "nothing-planned": 409,
}


class CrossSiteRequest(Forbidden):
    """A state-changing request a browser sent on behalf of another site's
    page (see ``crosssite``). A refusal of the HTTP transport, so it lives
    here rather than in the domain's error taxonomy."""

    code = "cross-site-request"


class UnknownHost(InvalidRequest):
    """A request for a host name the server does not answer to
    (``core.api.hosts``, see ``crosssite``). A refusal of the HTTP transport,
    like :class:`CrossSiteRequest`."""

    code = "unknown-host"


def status_of(exc: BaseException) -> int:
    if isinstance(exc, IngestError):
        return _INGEST_STATUS.get(exc.reason, 422)
    for error_type, status in _STATUS:
        if isinstance(exc, error_type):
            return status
    return 500


def error_body(detail: str, code: str, **extra: Any) -> dict[str, Any]:
    return {"detail": detail, "code": code, **extra}


# How long a refusal that ends the connection goes on discarding what the
# client still sends, so that the client reads the answer instead of a reset.
LINGER_S = 2.0


class ClosingJSONResponse(JSONResponse):
    """An answer after which the connection ends although the request body
    was not read to its end.

    Closing at once would lose the answer: a client that is still sending
    when the server closes gets a connection reset, and with it whatever of
    the answer it had not read yet. So the whole answer goes out first, then
    what still arrives is discarded — unparsed, unstored — until the client
    stops, goes away, or ``LINGER_S`` has passed, and only then does the
    response end and the connection with it. ``unread=False`` says the body
    was read to its end: there is nothing to wait for."""

    def __init__(self, *args: Any, unread: bool = True, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._unread = unread

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._unread:
            await super().__call__(scope, receive, send)
            return
        await send(
            {"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers}
        )
        await send({"type": "http.response.body", "body": self.body, "more_body": True})
        with anyio.move_on_after(LINGER_S):
            while True:
                message = await receive()
                if message["type"] != "http.request" or not message.get("more_body", False):
                    break
        with contextlib.suppress(OSError):
            await send({"type": "http.response.body", "body": b"", "more_body": False})


def error_response(
    request: Request, exc: RunComposerError, *, close: bool = False, unread: bool = True
) -> JSONResponse:
    """The answer to a domain error. ``close`` ends the connection after
    the answer — for a refusal given before the request body was read to its
    end: the rest of that body is never wanted, and a keep-alive connection
    could only go on reading it. Such an answer is a
    :class:`ClosingJSONResponse`: the client gets it even while it is still
    sending (``unread`` — whether any of the body is still outstanding)."""
    extra = dict(exc.extra or {})
    # Which per-run parameter was refused — so a form can put the message
    # on the field instead of reading it out of the text.
    if isinstance(exc, InvalidParameter) and exc.parameter:
        extra.setdefault("parameter", exc.parameter)
    # A runner may have quoted a configured option in what it raised.
    service = getattr(request.app.state, "service", None)
    detail = service.redact(str(exc)) if service is not None else str(exc)
    content = error_body(detail, str(exc.code), **extra)
    if close:
        return ClosingJSONResponse(
            status_code=status_of(exc), content=content, headers={"Connection": "close"},
            unread=unread,
        )
    return JSONResponse(status_code=status_of(exc), content=content)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RunComposerError)
    async def _domain(request: Request, exc: RunComposerError) -> JSONResponse:
        return error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = []
        for error in exc.errors():
            location = [str(part) for part in error.get("loc", ()) if part != "body"]
            fields.append({"field": ".".join(location), "message": str(error.get("msg", ""))})
        summary = "; ".join(
            f"{field['field']}: {field['message']}" if field["field"] else field["message"]
            for field in fields
        )
        return JSONResponse(
            status_code=400,
            content=error_body(
                f"invalid request — {summary}" if summary else "invalid request",
                "invalid-request",
                fields=fields,
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not-found", 405: "method-not-allowed"}
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(
                str(exc.detail), codes.get(exc.status_code, f"http-{exc.status_code}")
            ),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=error_body(
                f"internal error: {type(exc).__name__} (see the server log)", "internal-error"
            ),
        )
