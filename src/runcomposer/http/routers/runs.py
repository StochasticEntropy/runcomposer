"""Runs: compose, list, read, dispatch, cancel, rerun, finalize, delete."""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, Depends, Query, Response

from runcomposer.app.dispatching import Outcome
from runcomposer.app.service import Service
from runcomposer.app.views import dispatch_json, event_json, run_detail, run_row, verdict_rows
from runcomposer.core.errors import DispatchRefused, InvalidRequest, NotFound, RunComposerError
from runcomposer.core.model import REPORTABLE_STATUSES, VERDICT_STATUSES, DispatchRecord

from ..models import ComposeRequest, DispatchRequest, RerunRequest
from . import service_of

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


def _labels(pairs: list[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise InvalidRequest(f"label filter must be key=value, got {pair!r}")
        labels[key] = value
    return labels


def _spec_url(run_id: str, dispatch_id: str) -> str:
    return f"/api/v1/runs/{run_id}/dispatches/{dispatch_id}/spec"


def _start(
    service: Service, run_id: str, request: DispatchRequest
) -> tuple[dict[str, Any], DispatchRecord | None]:
    """Carry out a dispatch request for a stored run: an export hand-out, or
    a runner dispatch — which returns once the dispatch is recorded and at
    most ``core.dispatch.handoff_wait_s`` later, so a refusal that happens
    right away is the answer to this request rather than an event to find."""
    if request.mode == "export":
        exported = service.export(run_id, format=request.format)
        extra: dict[str, Any] = {
            "spec_url": _spec_url(run_id, exported.dispatch.dispatch_id),
            "spec_sha256": exported.dispatch.spec_sha256,
            "spec_media_type": exported.media_type,
            # Shown once: only its hash is stored. Rotate it to get a new one.
            "ingest_token": exported.token,
        }
        return extra, exported.dispatch
    assert request.runner is not None
    outcome: Outcome = service.dispatch(run_id, request.runner, request.parameters)
    handed_off = service.dispatcher.wait_handoff(
        outcome, float(service.config.dispatch["handoff_wait_s"])
    )
    if outcome.refusal is not None:
        raise DispatchRefused(outcome.refusal)
    return {"handoff": _handoff(handed_off)}, service.store.get_dispatch(outcome.dispatch_id)


def _handoff(handed_off: bool) -> str:
    """``done`` — the runner declared (or finished) within the hand-off
    wait; ``pending`` — it is still preparing. A refusal that arrives while
    ``pending`` cannot be this request's answer any more: the run, which the
    answer has named, stays ``COMPOSED`` with a ``dispatch.refused`` event."""
    return "done" if handed_off else "pending"


@router.post("", status_code=201)
def create_run(body: ComposeRequest, service: Service = Depends(service_of)) -> dict[str, Any]:
    """Compose a run, and optionally dispatch it in the same request.

    The answer never waits for an execution: a dispatched run comes back
    ``DISPATCHED`` or ``RUNNING`` and is followed through ``GET /runs/{id}``
    and its events. Everything that can be checked up front — the runner id,
    its per-run parameters — is checked before a run exists. If the dispatch
    is refused within the hand-off wait, that refusal is the answer and the
    composed run is removed again unless ``keep_run`` is set; the error says
    which (``run_kept``). A run an answer has named is never removed
    implicitly: when the answer says ``handoff: pending`` and the runner
    refuses afterwards, the run stays ``COMPOSED`` with the refusal as a
    ``dispatch.refused`` event, whatever ``keep_run`` said.
    """
    request = body.dispatch
    runner_id = request.runner if request else None
    result = service.compose_run(
        body.selection,
        title=body.title,
        labels=body.labels or None,
        origin="api",
        runner_section=body.runner,
        expect_format=body.expect_format,
        runner_id=runner_id,
        parameters=request.parameters if request and runner_id else None,
    )
    assert result.run is not None
    run_id = result.run.id
    response: dict[str, Any] = {
        "warnings": result.warnings,
        # For a history selection: which run it resolved to, and why that one.
        "derived_from": list(result.spec["selection"].get("derived_from") or []),
    }
    if request is not None:
        try:
            extra, dispatch = _start(service, run_id, request)
        except RunComposerError as exc:
            if not body.keep_run:
                service.delete_run(run_id)
            exc.extra = {
                "run_kept": body.keep_run,
                "run_id": run_id if body.keep_run else None,
            }
            raise
        response.update(extra)
        response["dispatch"] = dispatch_json(dispatch) if dispatch else None
    response["run"] = run_row(service.require_run(run_id))
    return response


@router.get("")
def list_runs(
    state: str | None = None,
    label: list[str] = Query(default=[], description="key=value, repeatable; all must match"),
    since: str | None = Query(None, description="created at/after this ISO-8601 time"),
    until: str | None = Query(None, description="created at/before this ISO-8601 time"),
    q: str | None = Query(None, description="case-insensitive substring of the title"),
    limit: int = Query(50, ge=0, le=500),
    offset: int = Query(0, ge=0),
    service: Service = Depends(service_of),
) -> dict[str, Any]:
    """Runs, newest first. Each row is complete for a list — verdict counts,
    planned count, duration, the latest dispatch's mode — and ``total`` is the
    size of the whole filtered set."""
    page = service.list_runs(
        state=state, labels=_labels(label), since=since, until=until, q=q,
        limit=limit, offset=offset,
    )
    return {
        "runs": [run_row(run) for run in page.runs],
        "total": page.total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{run_id}")
def run_detail_view(
    run_id: str,
    dispatch: str | None = Query(None, description="show this dispatch instead of the latest"),
    service: Service = Depends(service_of),
) -> dict[str, Any]:
    """One run in full: dispatches, deliveries, verdicts (shard, attempt,
    duration; live status flagged), the per-shard roll-up, artifacts with
    hrefs, recent events, and the selection it was composed from."""
    run = service.require_run(run_id)
    if dispatch is not None and all(d.dispatch_id != dispatch for d in run.dispatches):
        raise NotFound(f"run {run_id} has no dispatch {dispatch!r}")
    return run_detail(service, run, dispatch_id=dispatch)


@router.delete("/{run_id}", status_code=204)
def delete_run(run_id: str, service: Service = Depends(service_of)) -> Response:
    service.delete_run(run_id)
    return Response(status_code=204)


@router.get("/{run_id}/items")
def run_items(run_id: str, service: Service = Depends(service_of)) -> dict[str, Any]:
    materialized = (service.require_spec(run_id).get("selection") or {}).get("materialized") or {}
    return {"item_ids": materialized.get("item_ids", []), "count": materialized.get("count", 0)}


@router.get("/{run_id}/spec")
def run_spec(
    run_id: str, format: str = Query("json", pattern="^(json|yaml)$"),
    service: Service = Depends(service_of),
) -> Any:
    """The run's spec document as stored. It carries no ingest token — a
    token exists only in the answer to the export that minted it. For the
    exact bytes of a hand-out, see ``/dispatches/{id}/spec``."""
    spec = service.require_spec(run_id)
    if format == "yaml":
        return Response(
            content=yaml.safe_dump(spec, sort_keys=False, allow_unicode=True),
            media_type="application/yaml",
        )
    return spec


@router.get("/{run_id}/events")
def run_events(
    run_id: str,
    after: int = Query(0, ge=0, description="return events with seq greater than this"),
    limit: int = Query(500, ge=1, le=5000),
    wait: float = Query(0, ge=0, le=25, description="block up to this many seconds for news"),
    service: Service = Depends(service_of),
) -> dict[str, Any]:
    """The run's event stream after a cursor. With ``wait`` the request is
    held until an event arrives or the time is up (long polling)."""
    service.require_run(run_id)
    if wait:
        service.bus.wait(lambda: bool(service.store.events(run_id, after=after, limit=1)), wait)
    events = service.store.events(run_id, after=after, limit=limit)
    return {
        "events": [event_json(event) for event in events],
        "last_seq": events[-1].seq if events else after,
    }


@router.get("/{run_id}/verdicts")
def run_verdicts(
    run_id: str,
    dispatch: str | None = Query(None, description="default: the latest dispatch"),
    shard: str | None = None,
    status: list[str] = Query(default=[], description="repeatable"),
    limit: int = Query(500, ge=0, le=5000),
    offset: int = Query(0, ge=0),
    service: Service = Depends(service_of),
) -> dict[str, Any]:
    """Verdicts of one dispatch, filtered and paged. Includes the derived
    ``MISSING`` rows once the dispatch is over, and live rows (``live``) for
    shards that have not delivered yet."""
    run = service.require_run(run_id)
    for value in status:
        if value not in VERDICT_STATUSES:
            raise InvalidRequest(
                f"unknown verdict status {value!r} (one of {list(VERDICT_STATUSES)}; an "
                f"executor reports {list(REPORTABLE_STATUSES)})"
            )
    record = run.latest_dispatch
    if dispatch is not None:
        record = next((d for d in run.dispatches if d.dispatch_id == dispatch), None)
        if record is None:
            raise NotFound(f"run {run_id} has no dispatch {dispatch!r}")
    rows = verdict_rows(service, record, run_id)
    if shard is not None:
        rows = [row for row in rows if row["shard"] == shard]
    if status:
        rows = [row for row in rows if row["status"] in status]
    return {
        "verdicts": rows[offset : offset + limit],
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "dispatch_id": record.dispatch_id if record else None,
    }


@router.post("/{run_id}/dispatch", status_code=202)
def dispatch_run(
    run_id: str, body: DispatchRequest, service: Service = Depends(service_of)
) -> dict[str, Any]:
    """Dispatch (or re-dispatch) a stored run to a runner, or export it.
    Returns without waiting for the execution."""
    service.require_run(run_id)
    extra, dispatch = _start(service, run_id, body)
    return {
        **extra,
        "dispatch": dispatch_json(dispatch) if dispatch else None,
        "run": run_row(service.require_run(run_id)),
    }


@router.get("/{run_id}/dispatches/{dispatch_id}/spec")
def dispatch_spec(run_id: str, dispatch_id: str, service: Service = Depends(service_of)) -> Response:
    """The exact bytes handed out for this dispatch — byte for byte what the
    dispatch's ``spec_sha256`` was computed over, so a marker written from a
    download of this URL verifies. Re-downloadable; never contains a token."""
    content, media_type, dispatch = service.dispatch_spec(run_id, dispatch_id)
    extension = "yaml" if "yaml" in media_type else "json"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="runspec-{run_id}.{extension}"',
            "ETag": f'"{dispatch.spec_sha256}"',
            "X-Runcomposer-Spec-Sha256": dispatch.spec_sha256 or "",
            "Cache-Control": "no-store",
        },
    )


@router.post("/{run_id}/dispatches/{dispatch_id}/token")
def rotate_token(run_id: str, dispatch_id: str, service: Service = Depends(service_of)) -> dict[str, Any]:
    """Mint a new ingest token for the dispatch. The previous one stops
    working; the new one is in this answer and nowhere else."""
    return {"dispatch_id": dispatch_id, "ingest_token": service.rotate_token(run_id, dispatch_id)}


@router.post("/{run_id}/cancel", status_code=202)
def cancel_run(
    run_id: str,
    force: bool = Query(False, description="close the dispatch now, whoever owns it"),
    service: Service = Depends(service_of),
) -> dict[str, Any]:
    """Request cancellation of the dispatch in flight. A dispatch that is
    executing becomes ``CANCELLED`` when its runner comes back — follow the
    run; one that only waits for results is ``CANCELLED`` at once. ``force``
    closes a dispatch whose owning process cannot be reached to honour the
    request."""
    dispatch = service.cancel(run_id, force=force)
    return {"dispatch": dispatch_json(dispatch), "run": run_row(service.require_run(run_id))}


@router.post("/{run_id}/rerun", status_code=201)
def rerun(run_id: str, body: RerunRequest, service: Service = Depends(service_of)) -> dict[str, Any]:
    """``scope: all`` dispatches the same run again; ``scope: failed``
    composes a new run from what did not pass and dispatches that. ``run`` in
    the answer is the run that was dispatched."""
    target, outcome = service.rerun(
        run_id, scope=body.scope, runner_id=body.runner, parameters=body.parameters,
        title=body.title,
    )
    handed_off = service.dispatcher.wait_handoff(
        outcome, float(service.config.dispatch["handoff_wait_s"])
    )
    if outcome.refusal is not None:
        error = DispatchRefused(outcome.refusal)
        if target != run_id:
            service.delete_run(target)
            error.extra = {"run_kept": False, "run_id": None}
        raise error
    dispatch = service.store.get_dispatch(outcome.dispatch_id)
    return {
        "handoff": _handoff(handed_off),
        "source_run_id": run_id,
        "rerun_of": {"run_id": run_id, "scope": body.scope},
        "dispatch": dispatch_json(dispatch) if dispatch else None,
        "run": run_row(service.require_run(target)),
    }


@router.post("/{run_id}/finalize")
def finalize(run_id: str, service: Service = Depends(service_of)) -> dict[str, Any]:
    """Close the latest dispatch as it stands: outstanding shards are given
    up and items nobody reported count as ``MISSING``."""
    return run_row(service.finalize(run_id))
