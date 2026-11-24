"""Results in: the push endpoint, an external executor's progress, and the
quarantine.

The three routes an executor writes to — ``/results``, ``/live``, ``/log`` —
are :class:`TokenFirstRoute` routes: the run and the ingest token are checked
from the path and the headers before a single byte of the body is read, and
the body is read under ``core.ingestion.max_upload_mb``, counted while it
arrives.
"""

from __future__ import annotations

import functools
import shutil
import tempfile
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.types import Message, Receive

from runcomposer.app.ingestion import QuarantineReport, expected_format
from runcomposer.app.service import Service
from runcomposer.app.views import ingest_json
from runcomposer.core.errors import (
    Forbidden,
    IngestError,
    InvalidRequest,
    PayloadTooLarge,
    RunComposerError,
)

from ..bodies import BodyLimitRoute, declared_too_large, read_within
from ..errors import error_body, error_response, status_of
from ..models import AttachRequest, LivePush, LogPush, PromoteRequest
from ..uploads import FILES_FIELD, receive_bundle
from . import service_of

#: Room for the multipart envelope (part headers, boundaries, the text
#: fields) on top of ``max_upload_mb`` of files.
ENVELOPE_BYTES = 64 * 1024


def _max_upload(service: Service) -> tuple[int, PayloadTooLarge]:
    """``core.ingestion.max_upload_mb`` in bytes, and the error that says so."""
    megabytes = service.config.ingestion["max_upload_mb"]
    return int(megabytes) * 1024 * 1024, PayloadTooLarge(
        f"upload exceeds core.ingestion.max_upload_mb ({megabytes} MB)"
    )


def _authorize(service: Service, run_id: str, headers: Mapping[str, str]) -> str | None:
    """The run, then the token (``X-Runcomposer-Token`` or ``Authorization:
    Bearer``): an unknown run is a 404, a missing token 401, a wrong one 403.
    Returns the dispatch the token is bound to (``None`` with tokens
    disabled)."""
    service.require_run(run_id)
    token = headers.get("x-runcomposer-token")
    authorization = headers.get("authorization")
    if token is None and authorization and authorization.lower().startswith("bearer "):
        token = authorization[len("bearer "):].strip()
    return service.ingestion.authorize_push(run_id, token)


class _Body:
    """The request body as the server hands it out, remembering whether its
    end was reached — a refusal lingers only for a body still outstanding."""

    def __init__(self, receive: Receive):
        self._receive = receive
        self.ended = False

    async def __call__(self) -> Message:
        message = await self._receive()
        if message["type"] != "http.request" or not message.get("more_body", False):
            self.ended = True
        return message


class TokenFirstRoute(APIRoute):
    """A route an executor writes to with a dispatch's ingest token.

    FastAPI reads and parses a declared body *before* any dependency runs,
    so the check cannot be a dependency: it runs here, in front of the
    route's own handler. In order, and before the body is touched: the run
    (404), the token (401/403), a declared ``Content-Length`` beyond
    ``max_upload_mb`` (413). A refusal at this point closes the connection,
    so not even the server's buffers take in more of the body.

    A route that declares a body (``/live``, ``/log``) then gets it read
    here, counted while it arrives and refused (413) once it passes the
    limit, and parsed from that. A route that declares none (``/results``)
    reads ``request.stream()`` itself, under the same limit."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        reads_body = self.body_field is not None

        async def authorized_first(request: Request) -> Response:
            incoming = _Body(request.receive)
            request = Request(request.scope, incoming)
            request.state.ingest_body = incoming
            service = service_of(request)
            max_bytes, too_large = _max_upload(service)
            limit = max_bytes + ENVELOPE_BYTES
            try:
                bound = await run_in_threadpool(
                    _authorize, service, request.path_params["run_id"], request.headers
                )
                if declared_too_large(request, limit):
                    raise too_large
            except RunComposerError as exc:
                return error_response(request, exc, close=True)
            request.state.ingest_dispatch = bound
            if not reads_body:
                return await handler(request)
            read = await read_within(request, limit, too_large)
            if isinstance(read, Response):
                return read
            return await handler(read)

        return authorized_first


def ingest_dispatch(
    request: Request,
    authorization: str | None = Header(
        None, description="`Bearer <ingest token>` of the dispatch delivered to"
    ),
    x_runcomposer_token: str | None = Header(
        None, description="the ingest token of the dispatch delivered to"
    ),
) -> str | None:
    """The dispatch the request's token is bound to — checked by
    :class:`TokenFirstRoute` before the body was read. (The header
    parameters are declared here so the API schema shows them.)"""
    bound: str | None = request.state.ingest_dispatch
    return bound


router = APIRouter(prefix="/api/v1", tags=["ingestion"], route_class=BodyLimitRoute)
_executor = APIRouter(route_class=TokenFirstRoute)

_PUSH_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": [FILES_FIELD],
                    "properties": {
                        FILES_FIELD: {
                            "type": "array",
                            "items": {"type": "string", "format": "binary"},
                            "description": "the bundle's files; each is kept under its base name",
                        },
                        "shard": {"type": "string"},
                        "dispatch": {"type": "string"},
                        "format": {"type": "string"},
                    },
                }
            }
        },
    }
}


def _discard(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


@_executor.post("/runs/{run_id}/results", openapi_extra=_PUSH_BODY)
async def push_results(
    run_id: str,
    request: Request,
    bound_dispatch: str | None = Depends(ingest_dispatch),
    service: Service = Depends(service_of),
) -> Any:
    """Multipart results push (DESIGN.md §5, transport 1): the bundle's files
    under ``files``, optionally ``shard``, ``dispatch`` and ``format``.

    Auth is ``Authorization: Bearer <token>`` or ``X-Runcomposer-Token`` — the
    ingest token of the dispatch being delivered to — and it is **checked
    before anything is read or written**: an unknown run is a plain 404, a
    missing token 401, a wrong one 403, and the body stays unread. The body is
    then read as a stream: each file goes into a temporary bundle directory
    under its base name (a name that is not a file name — ``..``, empty, a
    control character —, on a Windows server a name Windows cannot keep, and
    two names this server's file system keeps as one file, are a 400), and ``core.ingestion.max_upload_mb`` is
    counted while the bytes arrive (413 as soon as it is passed, whatever the
    request declared). A bundle that is then refused for what it contains
    lands in the quarantine and answers 422 (unreadable) or 409 (contradicts
    the run), with the quarantine entry's id; it is named by its files' names,
    never by where it lay on this machine.
    """
    max_bytes, too_large = _max_upload(service)
    temp_root = Path(await run_in_threadpool(functools.partial(tempfile.mkdtemp, prefix="runcomposer-push-")))
    try:
        try:
            received = await receive_bundle(
                request,
                temp_root,
                max_file_bytes=max_bytes,
                max_body_bytes=max_bytes + ENVELOPE_BYTES,
                too_large=too_large,
            )
        except RunComposerError as exc:
            # Refused while reading: the rest of the body is not wanted.
            incoming: _Body = request.state.ingest_body
            return error_response(request, exc, close=True, unread=not incoming.ended)
        dispatch = received.fields.get("dispatch") or None
        shard = received.fields.get("shard") or None
        declared_format = received.fields.get("format") or None
        if bound_dispatch and dispatch and dispatch != bound_dispatch:
            raise Forbidden(
                f"this token belongs to dispatch {bound_dispatch}, not to {dispatch} — "
                "a dispatch's token delivers to that dispatch only"
            )
        if declared_format:
            expected = expected_format(await run_in_threadpool(service.require_spec, run_id))
            if declared_format != expected:
                raise InvalidRequest(
                    f"declared format {declared_format!r} does not match the run's expected "
                    f"results format {expected!r} (results.expect, §3.1)"
                )
        result = await run_in_threadpool(
            functools.partial(
                service.ingestion.ingest_or_quarantine,
                temp_root,
                transport="push",
                run_id=run_id,
                dispatch_id=dispatch or bound_dispatch,
                shard=shard,
                name=", ".join(dict.fromkeys(received.files)),
            )
        )
        if isinstance(result, QuarantineReport):
            refusal = IngestError(result.detail, reason=result.reason)
            return JSONResponse(
                status_code=status_of(refusal),
                content=error_body(
                    service.redact(result.detail), result.reason, quarantine_entry=result.entry_id
                ),
            )
        return ingest_json(result)
    finally:
        await run_in_threadpool(_discard, temp_root)


def _progress_dispatch(service: Service, run_id: str, named: str | None, bound: str | None) -> Any:
    """The dispatch a progress push reports for — the token's own (the
    token was checked before the body was read)."""
    if bound and named and named != bound:
        raise Forbidden(
            f"this token belongs to dispatch {bound}, not to {named} — "
            "a dispatch's token reports for that dispatch only"
        )
    return service.ingestion.progress_dispatch(run_id, named or bound)


@_executor.post("/runs/{run_id}/live")
def push_live(
    run_id: str,
    body: LivePush,
    bound_dispatch: str | None = Depends(ingest_dispatch),
    service: Service = Depends(service_of),
) -> dict[str, Any]:
    """Live status from an executor runcomposer did not start itself: which
    item is executing now (``RUNNING``) and how items ended, while the build
    is still under way. Authorized like the results push — by the ingest
    token of the dispatch, before the body is read. Live status is a progress
    report — shown flagged ``live``, replaced by the next update for the same
    shard and item, and dropped when the shard's results are delivered; it is
    never a verdict.

    Answers ``{dispatch_id, accepted, ignored, reason}``: an update for an
    item the run did not plan or a shard that has delivered is ignored, and
    so is everything once the dispatch is over (``reason`` says so)."""
    dispatch = _progress_dispatch(service, run_id, body.dispatch, bound_dispatch)
    result = service.ingestion.push_live(dispatch, [update.model_dump() for update in body.updates])
    return {"dispatch_id": dispatch.dispatch_id, **result}


@_executor.post("/runs/{run_id}/log")
def push_log(
    run_id: str,
    body: LogPush,
    bound_dispatch: str | None = Depends(ingest_dispatch),
    service: Service = Depends(service_of),
) -> dict[str, Any]:
    """Log lines from an external executor, appended to the run's event
    stream (``log`` / ``warning`` events with ``source: push``). Authorized
    by the ingest token of the dispatch, before the body is read."""
    dispatch = _progress_dispatch(service, run_id, body.dispatch, bound_dispatch)
    result = service.ingestion.push_log(dispatch, [line.model_dump() for line in body.lines])
    return {"dispatch_id": dispatch.dispatch_id, **result}


router.include_router(_executor)


# -- quarantine (§4, §5) ------------------------------------------------------


@router.get("/quarantine")
def quarantine_list(service: Service = Depends(service_of)) -> dict[str, Any]:
    return {"entries": [asdict(entry) for entry in service.quarantine.entries()]}


@router.post("/quarantine/{entry_id}/attach")
def quarantine_attach(
    entry_id: str, body: AttachRequest, service: Service = Depends(service_of)
) -> dict[str, Any]:
    """Bind a quarantined delivery to an existing run — an explicit decision
    by a person, recorded as a ``delivery.attached`` event on the run."""
    return ingest_json(
        service.ingestion.attach_quarantined(entry_id, body.run_id, format=body.format)
    )


@router.post("/quarantine/{entry_id}/promote")
def quarantine_promote(
    entry_id: str, body: PromoteRequest | None = None, service: Service = Depends(service_of)
) -> dict[str, Any]:
    """Promote a quarantined delivery to its own run (origin ``ingested``).
    ``format`` names the result format when the entry does not know it."""
    return ingest_json(
        service.ingestion.promote_quarantined(entry_id, format=body.format if body else None)
    )


@router.delete("/quarantine/{entry_id}", status_code=204)
def quarantine_discard(entry_id: str, service: Service = Depends(service_of)) -> Response:
    service.quarantine.remove(entry_id)
    return Response(status_code=204)
