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
    LiveStatus,
    RunEvent,
    RunRecord,
    Verdict,
)
from runcomposer.core.ports import PlanPreview
from runcomposer.core.timeutil import epoch_ms

from .ingestion import IngestReport, expected_format

if TYPE_CHECKING:
    from .service import Service

__all__ = [
    "dispatch_json",
    "event_json",
    "ingest_json",
    "item_json",
    "plan_json",
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
        # Seconds from the dispatch's start to its completion; None while it
        # has not completed.
        "duration_s": _duration_s(dispatch),
        "shard_groups": dict(dispatch.shard_groups),
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


def _duration_s(dispatch: DispatchRecord | None) -> int | None:
    if dispatch is None or not dispatch.completed_at:
        return None
    return max(0, (epoch_ms(dispatch.completed_at) - epoch_ms(dispatch.created_at)) // 1000)


def verdict_json(verdict: Verdict | LiveStatus, *, live: bool = False) -> dict[str, Any]:
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
    """One run as a list shows it — complete without further requests.
    ``duration_s`` describes the latest dispatch, like every other headline
    field (state, completion, counts): a run dispatched again a week later
    did not take a week. Each entry of ``dispatches`` carries its own."""
    latest = run.latest_dispatch
    duration_s = _duration_s(latest)
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
    streamed = service.store.live_verdicts(run_id, dispatch.dispatch_id)
    # A MISSING verdict says "nothing was delivered for this item" — which is
    # also true of an item that was seen passing live in a shard that was
    # cancelled before it delivered. What was last streamed about such an
    # item is stated next to its verdict (`last_live`), so that nobody has to
    # match rows up to tell "never started" from "ran, result not delivered".
    by_shard = {(status.shard, status.item_id): status for status in streamed}
    by_item = {status.item_id: status for status in streamed}
    for row in rows:
        if row["status"] != "MISSING":
            continue
        seen = (
            by_shard.get((row["shard"], row["item_id"]))
            if row["shard"]
            else by_item.get(row["item_id"])
        )
        row["last_live"] = (
            {"status": seen.status, "duration_ms": seen.duration_ms, "message": seen.message,
             "shard": seen.shard}
            if seen is not None
            else None
        )
    # A dispatch that is over (STALE, FAILED, CANCELLED, COMPLETE) executes
    # nothing: an item last seen RUNNING is not listed as running. What was
    # seen stays readable as its MISSING row's `last_live`.
    over = dispatch.state in lifecycle.TERMINAL_STATES
    for status in streamed:
        if status.shard not in delivered and not (over and status.status == "RUNNING"):
            rows.append(verdict_json(status, live=True))
    return rows


def _reported(verdicts: Sequence[Verdict]) -> int:
    """How many items have a reported result: one per (shard, item) — the
    effective verdict — and never a derived ``MISSING``, which says that
    nothing was reported. The one definition of ``count`` for a shard and
    for a group, so "N of M reported" reads the same at both levels."""
    return sum(1 for verdict in lifecycle.effective_verdicts(verdicts) if verdict.status != "MISSING")


def shard_summaries(
    dispatch: DispatchRecord | None, rows: Sequence[Mapping[str, Any]], delivered: set[str]
) -> list[dict[str, Any]]:
    """The fan-out roll-up: one entry per shard — declared ones first, in
    declaration order — with its own counts and computed completion. "Green
    on partition A, red on partition B" is read here, not aggregated by the
    caller. ``planned`` is how many items the shard said it would report
    (None when it never said), so progress has a denominator per shard;
    ``count`` is how many of them have a reported result (``MISSING`` is in
    ``summary``, not in ``count``)."""
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
                "count": _reported(verdicts),
                "planned": len(dispatch.shard_items[shard]) if shard in dispatch.shard_items else None,
                "summary": lifecycle.summarize(verdicts),
                "completion": lifecycle.completion_of(verdicts) if has_delivered else None,
            }
        )
    return summaries


def run_detail(
    service: Service,
    run: RunRecord,
    *,
    dispatch_id: str | None = None,
    events_limit: int = 200,
    server_paths: bool = False,
) -> dict[str, Any]:
    """``server_paths`` is for a reader on this machine (the CLI): artifacts
    then carry the path they were recorded under. Over HTTP they do not —
    a client gets ``href`` and a path relative to the artifact directory."""
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
    events = service.store.latest_events(run.id, limit=events_limit)
    data.update(
        {
            "deliveries": [delivery_json(delivery) for delivery in run.deliveries],
            "dispatch_id": dispatch.dispatch_id if dispatch else None,
            "verdicts": rows,
            "shards": shard_summaries(dispatch, rows, delivered),
            # Beside the per-shard roll-up: which group each shard belongs to,
            # the roll-up per group, and what is streaming right now.
            "shard_groups": dict(dispatch.shard_groups) if dispatch else {},
            "groups": group_summaries(dispatch, rows, delivered),
            "live_summary": live_summary(rows),
            # For a run composed by `rerun` with scope `failed`: the run it
            # was derived from. (Scope `all` re-dispatches the same run.)
            "rerun_of": _rerun_of(run, selection),
            "artifacts": service.artifacts.list(run.id, server_paths=server_paths),
            "events": [event_json(event) for event in events],
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


# -- shard groups, live counts, plans ------------------------------------------------


def _live_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


def live_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """What is streaming from shards that have not delivered, as counts per
    shard and live status — ``{"staging-01": {"RUNNING": 1, "PASS": 4}}``.
    A shard that streams nothing is not listed."""
    by_shard: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row["live"]:
            by_shard.setdefault(row["shard"], []).append(row)
    return {shard: _live_counts(streaming) for shard, streaming in by_shard.items()}


def group_summaries(
    dispatch: DispatchRecord | None, rows: Sequence[Mapping[str, Any]], delivered: set[str]
) -> list[dict[str, Any]]:
    """The roll-up per shard group — one entry per label a runner gave with
    ``declare(groups=…)``, in the order the labels first appear: which
    shards it holds, how many delivered, counts and a completion over all of
    them (``None`` until every shard of the group delivered). ``count`` is
    the sum of its shards' ``count``: items with a reported result. Empty
    for a dispatch that grouped nothing."""
    if dispatch is None or not dispatch.shard_groups:
        return []
    members: dict[str, list[str]] = {}
    for shard in dispatch.shards:
        label = dispatch.shard_groups.get(shard)
        if label is not None:
            members.setdefault(label, []).append(shard)
    summaries = []
    for label, shards in members.items():
        own = [row for row in rows if row["shard"] in shards]
        verdicts = [
            Verdict(row["item_id"], row["status"], attempt=row["attempt"], shard=row["shard"])
            for row in own
            if not row["live"]
        ]
        done = [shard for shard in shards if shard in delivered]
        planned = [dispatch.shard_items.get(shard) for shard in shards]
        summaries.append(
            {
                "group": label,
                "shards": shards,
                "declared_shards": len(shards),
                "delivered_shards": len(done),
                "planned": (
                    sum(len(items) for items in planned if items is not None)
                    if all(items is not None for items in planned)
                    else None
                ),
                "count": _reported(verdicts),
                "summary": lifecycle.summarize(verdicts),
                "live": _live_counts([row for row in own if row["live"]]),
                "completion": (
                    lifecycle.completion_of(verdicts) if len(done) == len(shards) else None
                ),
            }
        )
    return summaries


def _rerun_of(run: RunRecord, selection: Mapping[str, Any]) -> dict[str, Any] | None:
    if run.origin != "rerun":
        return None
    for entry in selection.get("derived_from") or ():
        if isinstance(entry, Mapping) and entry.get("resolved_run_id"):
            return {"run_id": str(entry["resolved_run_id"]), "scope": "failed"}
    return None


def plan_json(outcome: Any) -> dict[str, Any]:
    """A plan request's answer (``PlanOutcome``): ``plan`` is set, or
    ``refused`` is true and ``refusal`` says why. Shards come as a list, in
    the order the dispatch would declare them, each with its group, its
    items and — where history allows — an estimate; groups as their own
    roll-up."""
    preview: PlanPreview | None = outcome.preview
    plan: dict[str, Any] | None = None
    if preview is not None:
        shards: list[dict[str, Any]] = [
            {
                "shard": shard,
                "group": preview.groups.get(shard),
                "count": len(items),
                "item_ids": list(items),
                "estimate_ms": preview.estimates_ms.get(shard),
            }
            for shard, items in preview.shards.items()
        ]
        labels: dict[str, list[dict[str, Any]]] = {}
        for entry in shards:
            if entry["group"] is not None:
                labels.setdefault(entry["group"], []).append(entry)
        plan = {
            "text": preview.text,
            "shards": shards,
            "groups": [
                {
                    "group": label,
                    "shards": [entry["shard"] for entry in entries],
                    "count": sum(entry["count"] for entry in entries),
                    "estimate_ms": (
                        sum(entry["estimate_ms"] for entry in entries)
                        if all(entry["estimate_ms"] is not None for entry in entries)
                        else None
                    ),
                }
                for label, entries in labels.items()
            ],
            "notes": list(preview.notes),
            "data": dict(preview.data),
        }
    return {
        "runner": outcome.runner_id,
        "run_id": outcome.run_id,
        "item_count": len(outcome.item_ids),
        "parameters": dict(outcome.parameters),
        "refused": outcome.refusal is not None,
        "refusal": outcome.refusal,
        "plan": plan,
        "warnings": list(outcome.warnings),
    }
