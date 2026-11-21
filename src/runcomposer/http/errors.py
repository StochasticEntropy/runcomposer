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

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

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


def error_response(request: Request, exc: RunComposerError, *, close: bool = False) -> JSONResponse:
    """The answer to a domain error. ``close`` ends the connection after
    the answer — for a refusal given before the request body was read: the
    rest of that body is never wanted, and a keep-alive connection could
    only go on reading it."""
    extra = dict(exc.extra or {})
    # Which per-run parameter was refused — so a form can put the message
    # on the field instead of reading it out of the text.
    if isinstance(exc, InvalidParameter) and exc.parameter:
        extra.setdefault("parameter", exc.parameter)
    # A runner may have quoted a configured option in what it raised.
    service = getattr(request.app.state, "service", None)
    detail = service.redact(str(exc)) if service is not None else str(exc)
    return JSONResponse(
        status_code=status_of(exc),
        content=error_body(detail, str(exc.code), **extra),
        headers={"Connection": "close"} if close else None,
    )


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
