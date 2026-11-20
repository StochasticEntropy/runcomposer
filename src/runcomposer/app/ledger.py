"""The ledger: the one writer of lifecycle state (DESIGN.md §4).

Runners, ingestion, the API and the CLI all change what a run is *through
here*. Every method reads the facts from the store, asks ``core.lifecycle``
what they mean, writes the dispatch, re-derives the run's headline from its
latest dispatch, and emits the event. Nothing else calls
``store.update_dispatch(state=…)`` or ``store.set_run_headline``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from runcomposer.core import events as kinds
from runcomposer.core import lifecycle
from runcomposer.core.errors import UnknownRun
from runcomposer.core.ids import new_ulid
from runcomposer.core.model import DispatchRecord, RunRecord, Verdict
from runcomposer.core.timeutil import utc_now

from .eventbus import EventBus

__all__ = ["Ledger"]

log = logging.getLogger("runcomposer.lifecycle")


class Ledger:
    def __init__(
        self, store: Any, bus: EventBus, *, redact: Callable[[Any], Any] | None = None
    ):
        self._store = store
        self._bus = bus
        self._redact = redact or (lambda value: value)
        self._lock = threading.RLock()

    # -- reading ----------------------------------------------------------------

    def run(self, run_id: str) -> RunRecord:
        run: RunRecord | None = self._store.get_run(run_id)
        if run is None:
            raise UnknownRun(run_id)
        return run

    def planned_ids(self, run_id: str) -> list[str]:
        spec = self._store.get_spec_document(run_id) or {}
        materialized = (spec.get("selection") or {}).get("materialized") or {}
        return list(materialized.get("item_ids") or [])

    def scored_verdicts(self, dispatch: DispatchRecord) -> list[Verdict]:
        """What a dispatch's outcome is computed from: its delivered verdicts,
        plus — once nothing more is expected — a derived ``MISSING`` verdict
        for every planned item that never reported."""
        verdicts: list[Verdict] = self._store.verdicts_for(dispatch.run_id, dispatch.dispatch_id)
        if dispatch.state in lifecycle.TERMINAL_STATES:
            verdicts = verdicts + lifecycle.missing_verdicts(
                self.planned_ids(dispatch.run_id), verdicts, dispatch.shard_items
            )
        return verdicts

    # -- writing ------------------------------------------------------------------

    def open(
        self,
        run_id: str,
        mode: str,
        *,
        state: str = "DISPATCHED",
        owner: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        shards: Sequence[str] = (),
        dispatch_id: str | None = None,
        exclusive: bool = False,
    ) -> DispatchRecord:
        """Record a new dispatch and make it the run's headline. With
        ``exclusive`` the store refuses (``RunActive``) when the run already
        has a dispatch in flight — atomically, across processes."""
        with self._lock:
            dispatch: DispatchRecord = self._store.add_dispatch(
                run_id,
                dispatch_id=dispatch_id or new_ulid(),
                mode=mode,
                state=state,
                owner=owner,
                parameters=parameters,
                exclusive=exclusive,
            )
            if shards:
                dispatch = self._store.update_dispatch(dispatch.dispatch_id, shards=list(shards))
            self._publish(dispatch)
            return dispatch

    def declare(
        self,
        dispatch_id: str,
        *,
        shards: Sequence[str],
        shard_items: Mapping[str, Sequence[str]],
        plan: str,
        links: Mapping[str, str],
    ) -> DispatchRecord:
        """The hand-off: shard set known, execution begins."""
        with self._lock:
            current = self._require(dispatch_id)
            # A plan may quote a configured option; what came from the
            # environment is blanked out before it is recorded (§8). Links are
            # kept as given: they are how `cancel` finds the executor again.
            plan = str(self._redact(plan))
            dispatch: DispatchRecord = self._store.update_dispatch(
                dispatch_id,
                state=lifecycle.advance(current.state, "RUNNING"),
                shards=list(shards),
                shard_items=shard_items,
                plan=plan,
                links=links,
            )
            self._publish(dispatch, extra={"shards": list(shards), "plan": plan})
            return dispatch

    def move(
        self, dispatch_id: str, target: str, *, error: str = "", extra: Mapping[str, Any] | None = None
    ) -> DispatchRecord:
        """Move a dispatch to ``target`` (validated by the state machine).
        Terminal targets settle the outcome from what was delivered."""
        with self._lock:
            current = self._require(dispatch_id)
            changes: dict[str, Any] = {"state": lifecycle.advance(current.state, target)}
            if error:
                changes["error"] = self._redact(error)
            if target in lifecycle.TERMINAL_STATES:
                changes["owner"] = None
            dispatch: DispatchRecord = self._store.update_dispatch(dispatch_id, **changes)
            if target in lifecycle.TERMINAL_STATES:
                dispatch = self._store.update_dispatch(
                    dispatch_id, summary=lifecycle.summarize(self.scored_verdicts(dispatch))
                )
            self._publish(dispatch, extra=extra)
            return dispatch

    def discard(self, dispatch_id: str, reason: str, *, crashed: bool = False) -> None:
        """A dispatch that never handed anything off is not a dispatch: the
        record is removed, the run's headline falls back to what it was, and
        the reason stays in the event stream."""
        with self._lock:
            current = self._require(dispatch_id)
            self._store.delete_dispatch(dispatch_id)
            self._sync_headline(current.run_id)
            self._bus.emit(
                current.run_id,
                kinds.DISPATCH_REFUSED,
                dispatch_id=dispatch_id,
                data={"reason": reason, "mode": current.mode, "crashed": crashed},
            )

    def recompute(self, dispatch_id: str, *, finalize: bool = False) -> DispatchRecord:
        """Re-evaluate a dispatch after a delivery (or an explicit finalize):
        all declared shards in → ``COMPLETE`` with a computed completion."""
        with self._lock:
            current = self._require(dispatch_id)
            delivered = self._store.delivered_shards(current.run_id, dispatch_id)
            outstanding = lifecycle.outstanding_shards(current.shards, delivered)
            verdicts: list[Verdict] = self._store.verdicts_for(current.run_id, dispatch_id)
            settle = finalize or (bool(current.shards) and not outstanding)
            if not settle or current.state == "CANCELLED":
                return self._store.update_dispatch(  # type: ignore[no-any-return, unused-ignore]
                    dispatch_id, summary=lifecycle.summarize(verdicts)
                )
            scored = verdicts + lifecycle.missing_verdicts(
                self.planned_ids(current.run_id), verdicts, current.shard_items
            )
            completion = lifecycle.completion_of(scored)
            was_complete = current.state == "COMPLETE"
            dispatch: DispatchRecord = self._store.update_dispatch(
                dispatch_id,
                state=lifecycle.advance(current.state, "COMPLETE"),
                completion=completion,
                completed_at=current.completed_at if was_complete else utc_now(),
                summary=lifecycle.summarize(scored),
                error="" if not was_complete else current.error,
            )
            if not was_complete or completion != current.completion:
                extra: dict[str, Any] = {}
                if finalize and outstanding:
                    extra = {"finalized": True, "outstanding_shards": outstanding}
                self._publish(dispatch, extra=extra)
            else:
                self._sync_headline(dispatch.run_id)
            return dispatch

    def release(self, dispatch_id: str) -> None:
        """Nothing in this process is working on the dispatch any more."""
        with self._lock:
            if self._store.get_dispatch(dispatch_id) is not None:
                self._store.update_dispatch(dispatch_id, owner=None)

    # -- internals ------------------------------------------------------------------

    def _require(self, dispatch_id: str) -> DispatchRecord:
        dispatch: DispatchRecord | None = self._store.get_dispatch(dispatch_id)
        if dispatch is None:
            raise LookupError(f"unknown dispatch id {dispatch_id!r}")
        return dispatch

    def _sync_headline(self, run_id: str) -> RunRecord:
        run = self.run(run_id)
        state, completion, completed_at = lifecycle.headline(run.dispatches)
        if (state, completion, completed_at) != (run.state, run.completion, run.completed_at):
            self._store.set_run_headline(
                run_id, state, completion=completion, completed_at=completed_at
            )
        return run

    def _publish(self, dispatch: DispatchRecord, extra: Mapping[str, Any] | None = None) -> None:
        """Headline first, then the event — a reader woken by the event finds
        the state it announces."""
        run = self._sync_headline(dispatch.run_id)
        data: dict[str, Any] = {
            "state": dispatch.state,
            "mode": dispatch.mode,
            "title": run.title,
            "labels": dict(run.labels),
            "latest": bool(run.dispatches) and run.dispatches[-1].dispatch_id == dispatch.dispatch_id,
        }
        if dispatch.completion:
            data["completion"] = dispatch.completion
        if dispatch.summary:
            data["summary"] = dict(dispatch.summary)
        if dispatch.error:
            data["error"] = dispatch.error
        if dispatch.links:
            data["links"] = dict(dispatch.links)
        data.update(extra or {})
        self._bus.emit(
            dispatch.run_id,
            kinds.STATE_EVENTS[dispatch.state],
            dispatch_id=dispatch.dispatch_id,
            data=data,
        )
