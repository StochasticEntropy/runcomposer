"""JSON views (DESIGN.md §9): the shapes the HTTP API returns and the CLI
prints with ``--json`` — built in one place so the two cannot drift.

A run *row* carries everything a list needs without a second request: verdict
counts, planned count, duration, the latest dispatch's mode. A run *detail*
adds the verdicts, the per-shard roll-up, artifacts, events and the selection
the run was composed from.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from runcomposer.core import lifecycle
from runcomposer.core.model import (
    DeliveryRecord,
    DispatchRecord,
    Item,
    RunEvent,
    RunRecord,
    Verdict,
)
from runcomposer.core.timeutil import epoch_ms

from .ingestion import IngestReport, expected_format

if TYPE_CHECKING:
    from .service import Service

__all__ = [
    "dispatch_json",
    "event_json",
    "ingest_json",
    "item_json",
    "run_detail",
    "run_row",
    "verdict_json",
    "verdict_rows",
]


def item_json(item: Item) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.display_name,
        "tags": list(item.tags),
        "hierarchy": list(item.hierarchy),
    }


def dispatch_json(dispatch: DispatchRecord) -> dict[str, Any]:
    return {
        "dispatch_id": dispatch.dispatch_id,
        "mode": dispatch.mode,
        "state": dispatch.state,
        "created_at": dispatch.created_at,
        "shards": list(dispatch.shards),
        "declared_shards": dispatch.declared_shards,
        "completion": dispatch.completion,
        "completed_at": dispatch.completed_at,
        "summary": dict(dispatch.summary),
        "error": dispatch.error,
        "plan": dispatch.plan,
        "links": dict(dispatch.links),
        "parameters": dict(dispatch.parameters),
        "spec_sha256": dispatch.spec_sha256,
        "cancel_requested": dispatch.cancel_requested,
        "executing": dispatch.owner is not None,
        # How many verdicts the dispatch owes over all its shards — what a
        # progress display divides by. None when no shard declared its items.
        "planned_verdicts": (
            sum(len(items) for items in dispatch.shard_items.values())
            if dispatch.shard_items
            else None
        ),
    }


def delivery_json(delivery: DeliveryRecord) -> dict[str, Any]:
    return {
        "delivery_id": delivery.delivery_id,
        "dispatch_id": delivery.dispatch_id,
        "shard": delivery.shard,
        "content_hash": delivery.content_hash,
        "format": delivery.format,
        "created_at": delivery.created_at,
    }


def event_json(event: RunEvent) -> dict[str, Any]:
    return {
        "seq": event.seq,
        "run_id": event.run_id,
        "at": event.at,
        "kind": event.kind,
        "dispatch_id": event.dispatch_id,
        "data": dict(event.data),
    }


def verdict_json(verdict: Verdict, *, live: bool = False) -> dict[str, Any]:
    return {
        "item_id": verdict.item_id,
        "status": verdict.status,
        "duration_ms": verdict.duration_ms,
        "message": verdict.message,
        "shard": verdict.shard,
        "attempt": verdict.attempt,
        "live": live,
    }


def ingest_json(report: IngestReport) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "dispatch_id": report.dispatch_id,
        "shard": report.shard,
        "outcome": report.outcome,
        "verdict_count": report.verdict_count,
        "run_state": report.run_state,
        "completion": report.completion,
        "warnings": report.warnings,
    }


def run_row(run: RunRecord) -> dict[str, Any]:
    """One run as a list shows it — complete without further requests."""
    latest = run.latest_dispatch
    first = run.dispatches[0] if run.dispatches else None
    duration_s = None
    if first is not None and run.completed_at:
        duration_s = max(0, (epoch_ms(run.completed_at) - epoch_ms(first.created_at)) // 1000)
    return {
        "id": run.id,
        "title": run.title,
        "created_at": run.created_at,
        "origin": run.origin,
        "labels": dict(run.labels),
        "state": run.state,
        "completion": run.completion,
        "completed_at": run.completed_at,
        "planned_count": run.planned_count,
        "verdict_summary": dict(latest.summary) if latest else {},
        "mode": latest.mode if latest else None,
        "duration_s": duration_s,
        "dispatch_count": len(run.dispatches),
        "dispatches": [dispatch_json(dispatch) for dispatch in run.dispatches],
    }


def verdict_rows(service: Service, dispatch: DispatchRecord | None, run_id: str) -> list[dict[str, Any]]:
    """Everything known per item for one dispatch: delivered verdicts, the
    derived ``MISSING`` ones once nothing more is expected, and — for shards
    that have not delivered — the live status streamed so far (``live``)."""
    if dispatch is None:
        return []
    rows = [verdict_json(verdict) for verdict in service.ledger.scored_verdicts(dispatch)]
    delivered = {row["shard"] for row in rows if row["status"] != "MISSING"}
    for verdict in service.store.live_verdicts(run_id, dispatch.dispatch_id):
        if verdict.shard not in delivered:
            rows.append(verdict_json(verdict, live=True))
    return rows


def shard_summaries(
    dispatch: DispatchRecord | None, rows: Sequence[Mapping[str, Any]], delivered: set[str]
) -> list[dict[str, Any]]:
    """The fan-out roll-up: one entry per shard — declared ones first, in
    declaration order — with its own counts and computed completion. "Green
    on partition A, red on partition B" is read here, not aggregated by the
    caller. ``planned`` is how many items the shard said it would report
    (None when it never said), so progress has a denominator per shard."""
    if dispatch is None:
        return []
    order = list(dispatch.shards)
    grouped: dict[str, list[Verdict]] = {shard: [] for shard in order}
    for row in rows:
        if row["live"]:
            continue
        grouped.setdefault(row["shard"], []).append(
            Verdict(row["item_id"], row["status"], attempt=row["attempt"], shard=row["shard"])
        )
    summaries = []
    for shard, verdicts in grouped.items():
        if shard == "" and not verdicts:
            continue
        has_delivered = shard in delivered
        summaries.append(
            {
                "shard": shard,
                "declared": shard in order,
                "delivered": has_delivered,
                "count": len(lifecycle.effective_verdicts(verdicts)),
                "planned": len(dispatch.shard_items[shard]) if shard in dispatch.shard_items else None,
                "summary": lifecycle.summarize(verdicts),
                "completion": lifecycle.completion_of(verdicts) if has_delivered else None,
            }
        )
    return summaries


def run_detail(
    service: Service, run: RunRecord, *, dispatch_id: str | None = None, events_limit: int = 200
) -> dict[str, Any]:
    data = run_row(run)
    dispatch = run.latest_dispatch
    if dispatch_id is not None:
        dispatch = next((d for d in run.dispatches if d.dispatch_id == dispatch_id), None)
    spec = service.store.get_spec_document(run.id) or {}
    selection = spec.get("selection") or {}
    rows = verdict_rows(service, dispatch, run.id)
    delivered = (
        service.store.delivered_shards(run.id, dispatch.dispatch_id) if dispatch else set()
    )
    events = service.store.events(run.id, after=0, limit=100_000)
    data.update(
        {
            "deliveries": [delivery_json(delivery) for delivery in run.deliveries],
            "dispatch_id": dispatch.dispatch_id if dispatch else None,
            "verdicts": rows,
            "shards": shard_summaries(dispatch, rows, delivered),
            "artifacts": service.artifacts.list(run.id),
            "events": [event_json(event) for event in events[-events_limit:]],
            "last_event_seq": events[-1].seq if events else 0,
            "selection": {
                "tag_filter": selection.get("tag_filter"),
                "item_ids": list(selection.get("item_ids") or []),
                "derived_from": list(selection.get("derived_from") or []),
            },
            "expect_format": expected_format(spec),
            "source": dict(spec.get("source") or {}),
        }
    )
    return data
