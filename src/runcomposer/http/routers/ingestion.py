"""Results in: the push endpoint and the quarantine."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from runcomposer.app.ingestion import QuarantineReport, expected_format
from runcomposer.app.service import Service
from runcomposer.app.views import ingest_json
from runcomposer.core.errors import (
    Forbidden,
    IngestError,
    InvalidRequest,
    PayloadTooLarge,
)

from ..errors import error_body, status_of
from ..models import AttachRequest, LivePush, LogPush, PromoteRequest
from . import service_of

router = APIRouter(prefix="/api/v1", tags=["ingestion"])


@router.post("/runs/{run_id}/results")
def push_results(
    run_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    shard: str | None = Form(None),
    dispatch: str | None = Form(None),
    format: str | None = Form(None),
    authorization: str | None = Header(None),
    x_runcomposer_token: str | None = Header(None),
    service: Service = Depends(service_of),
) -> Any:
    """Multipart results push (DESIGN.md §5, transport 1).

    Auth is ``Authorization: Bearer <token>`` or ``X-Runcomposer-Token`` — the
    ingest token of the dispatch being delivered to. **Nothing is written
    before the token checks out**: an unknown run is a plain 404, a missing
    token 401, a wrong one 403. A bundle that is then refused for what it
    contains lands in the quarantine and answers 422 (unreadable) or 409
    (contradicts the run), with the quarantine entry's id.
    """
    config = service.config
    max_bytes = int(config.ingestion["max_upload_mb"]) * 1024 * 1024
    too_large = PayloadTooLarge(
        f"upload exceeds core.ingestion.max_upload_mb ({config.ingestion['max_upload_mb']} MB)"
    )
    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > max_bytes + 65536:
        raise too_large

    # 1. the run, 2. the token — both before a single byte is kept.
    service.require_run(run_id)
    token = x_runcomposer_token
    if token is None and authorization and authorization.lower().startswith("bearer "):
        token = authorization[len("bearer "):].strip()
    bound_dispatch = service.ingestion.authorize_push(run_id, token)
    if bound_dispatch and dispatch and dispatch != bound_dispatch:
        raise Forbidden(
            f"this token belongs to dispatch {bound_dispatch}, not to {dispatch} — "
            "a dispatch's token delivers to that dispatch only"
        )
    spec = service.require_spec(run_id)
    if format and format != expected_format(spec):
        raise InvalidRequest(
            f"declared format {format!r} does not match the run's expected results format "
            f"{expected_format(spec)!r} (results.expect, §3.1)"
        )

    temp_root = Path(tempfile.mkdtemp(prefix="runcomposer-push-"))
    try:
        total = 0
        for upload in files:
            name = Path(upload.filename or "artifact").name or "artifact"  # no path traversal
            with open(temp_root / name, "wb") as out:
                while chunk := upload.file.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise too_large
                    out.write(chunk)
        result = service.ingestion.ingest_or_quarantine(
            temp_root,
            transport="push",
            run_id=run_id,
            dispatch_id=dispatch or bound_dispatch,
            shard=shard,
        )
        if isinstance(result, QuarantineReport):
            refusal = IngestError(result.detail, reason=result.reason)
            return JSONResponse(
                status_code=status_of(refusal),
                content=error_body(
                    result.detail, result.reason, quarantine_entry=result.entry_id
                ),
            )
        return ingest_json(result)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _progress_dispatch(
    service: Service,
    run_id: str,
    named: str | None,
    authorization: str | None,
    x_runcomposer_token: str | None,
) -> Any:
    """Authorize a progress push exactly as a results push is authorized —
    the run, then the token, before anything else — and name its dispatch."""
    service.require_run(run_id)
    token = x_runcomposer_token
    if token is None and authorization and authorization.lower().startswith("bearer "):
        token = authorization[len("bearer "):].strip()
    bound = service.ingestion.authorize_push(run_id, token)
    if bound and named and named != bound:
        raise Forbidden(
            f"this token belongs to dispatch {bound}, not to {named} — "
            "a dispatch's token reports for that dispatch only"
        )
    return service.ingestion.progress_dispatch(run_id, named or bound)


@router.post("/runs/{run_id}/live")
def push_live(
    run_id: str,
    body: LivePush,
    authorization: str | None = Header(None),
    x_runcomposer_token: str | None = Header(None),
    service: Service = Depends(service_of),
) -> dict[str, Any]:
    """Live status from an executor runcomposer did not start itself: which
    item is executing now (``RUNNING``) and how items ended, while the build
    is still under way. Authorized like the results push: by the ingest token of
    the dispatch. Live status is a progress report — shown flagged ``live``,
    replaced by the next update for the same shard and item, and dropped when
    the shard's results are delivered; it is never a verdict.

    Answers ``{dispatch_id, accepted, ignored, reason}``: an update for an
    item the run did not plan or a shard that has delivered is ignored, and
    so is everything once the dispatch is over (``reason`` says so)."""
    dispatch = _progress_dispatch(service, run_id, body.dispatch, authorization, x_runcomposer_token)
    result = service.ingestion.push_live(dispatch, [update.model_dump() for update in body.updates])
    return {"dispatch_id": dispatch.dispatch_id, **result}


@router.post("/runs/{run_id}/log")
def push_log(
    run_id: str,
    body: LogPush,
    authorization: str | None = Header(None),
    x_runcomposer_token: str | None = Header(None),
    service: Service = Depends(service_of),
) -> dict[str, Any]:
    """Log lines from an external executor, appended to the run's event
    stream (``log`` / ``warning`` events with ``source: push``). Authorized
    by the ingest token of the dispatch."""
    dispatch = _progress_dispatch(service, run_id, body.dispatch, authorization, x_runcomposer_token)
    result = service.ingestion.push_log(dispatch, [line.model_dump() for line in body.lines])
    return {"dispatch_id": dispatch.dispatch_id, **result}


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
