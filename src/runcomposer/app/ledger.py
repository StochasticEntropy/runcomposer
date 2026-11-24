"""The ledger: the one writer of lifecycle state (DESIGN.md §4).

Runners, ingestion, the API and the CLI all change what a run is *through
here*. Every method reads the facts from the store, asks ``core.lifecycle``
what they mean, writes the dispatch, re-derives the run's headline from its
latest dispatch, and emits the event. Nothing else calls
``store.update_dispatch(state=…)`` or ``store.set_run_headline``.

Each of those read-evaluate-write steps is one decision taken inside the
store's ``atomic()`` unit: several processes share one store (two servers, a
server and a CLI), and a decision taken on facts that another process changed
between the reading and the writing — a shard it delivered, a cancel it
recorded — would contradict what is stored. The events of a decision are
emitted once it is committed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from runcomposer.core import events as kinds
from runcomposer.core import lifecycle
from runcomposer.core.errors import UnknownRun
from runcomposer.core.ids import new_ulid
from runcomposer.core.model import DISPATCH_ERROR_CODES, DispatchRecord, RunRecord, Verdict
from runcomposer.core.timeutil import utc_now

from .eventbus import EventBus

__all__ = ["Ledger"]

log = logging.getLogger("runcomposer.lifecycle")

#: An event decided inside a unit, emitted after it: (run, kind, dispatch, data).
_Pending = tuple[str, str, "str | None", dict[str, Any]]

#: The states recovery may mark STALE: a runner was executing them.
_EXECUTING = ("DISPATCHED", "RUNNING")


class _Decisions(threading.local):
    """Per thread: the events of the decision being taken, if one is."""

    pending: list[_Pending] | None = None


class Ledger:
    def __init__(
        self, store: Any, bus: EventBus, *, redact: Callable[[Any], Any] | None = None
    ):
        self._store = store
        self._bus = bus
        self._redact = redact or (lambda value: value)
        self._lock = threading.RLock()
        self._local = _Decisions()

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
        return self._scored(dispatch, verdicts)

    def _scored(self, dispatch: DispatchRecord, verdicts: list[Verdict]) -> list[Verdict]:
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
        with self._decision() as pending:
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
            self._publish(pending, dispatch)
            return dispatch

    def declare(
        self,
        dispatch_id: str,
        *,
        shards: Sequence[str],
        shard_items: Mapping[str, Sequence[str]],
        plan: str,
        links: Mapping[str, str],
        groups: Mapping[str, str] | None = None,
    ) -> DispatchRecord:
        """The hand-off: shard set known, execution begins."""
        with self._decision() as pending:
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
                shard_groups=dict(groups or {}),
                plan=plan,
                links=links,
            )
            extra: dict[str, Any] = {"shards": list(shards), "plan": plan}
            if groups:
                extra["groups"] = dict(groups)
            self._publish(pending, dispatch, extra=extra)
            return dispatch

    def move(
        self,
        dispatch_id: str,
        target: str,
        *,
        error: str = "",
        error_code: str = "",
        extra: Mapping[str, Any] | None = None,
    ) -> DispatchRecord:
        """Move a dispatch to ``target`` (validated by the state machine).
        Terminal targets settle the outcome from what was delivered.
        ``error_code`` is one of ``DISPATCH_ERROR_CODES``, with ``error``."""
        with self._decision() as pending:
            return self._move(
                pending, self._require(dispatch_id), target, error=error, error_code=error_code, extra=extra
            )

    def mark_stale(
        self, dispatch_id: str, *, owner: str, error: str, extra: Mapping[str, Any] | None = None
    ) -> DispatchRecord | None:
        """Restart recovery's move: ``STALE`` — if the dispatch is, when the
        decision is taken, still executing under ``owner``. Otherwise
        (another process recovered it first, or it moved on) nothing is
        written and the answer is ``None``."""
        with self._decision() as pending:
            current = self._require(dispatch_id)
            if current.state not in _EXECUTING or current.owner != owner:
                return None
            return self._move(
                pending, current, "STALE", error=error, error_code="interrupted",
                error_owner=owner or None, extra=extra,
            )

    def discard(self, dispatch_id: str, reason: str, *, crashed: bool = False) -> None:
        """A dispatch that never handed anything off is not a dispatch: the
        record is removed, the run's headline falls back to what it was, and
        the reason stays in the event stream."""
        with self._decision() as pending:
            current = self._require(dispatch_id)
            self._store.delete_dispatch(dispatch_id)
            self._sync_headline(current.run_id)
            pending.append(
                (
                    current.run_id,
                    kinds.DISPATCH_REFUSED,
                    dispatch_id,
                    {"reason": reason, "mode": current.mode, "crashed": crashed},
                )
            )

    def recompute(self, dispatch_id: str, *, finalize: bool = False) -> DispatchRecord:
        """Re-evaluate a dispatch after a delivery (or an explicit finalize):
        all declared shards in → ``COMPLETE`` with a computed completion.

        The shards delivered, the verdicts and the completion written are read
        and written in one decision: a delivery another process makes for
        another shard lands before it (and is counted) or after it (and
        recomputes again) — never in between."""
        with self._decision() as pending:
            current = self._require(dispatch_id)
            delivered = self._store.delivered_shards(current.run_id, dispatch_id)
            outstanding = lifecycle.outstanding_shards(current.shards, delivered)
            verdicts: list[Verdict] = self._store.verdicts_for(current.run_id, dispatch_id)
            settle = finalize or (bool(current.shards) and not outstanding)
            if not settle or current.state == "CANCELLED":
                # Nothing to settle: the summary follows what was delivered —
                # with the derived MISSING verdicts for a dispatch that is
                # already over, as when it was settled.
                return self._store.update_dispatch(  # type: ignore[no-any-return, unused-ignore]
                    dispatch_id, summary=lifecycle.summarize(self._scored(current, verdicts))
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
                error_code="" if not was_complete else current.error_code,
                error_owner=None if not was_complete else current.error_owner,
                # Settled in the same write as every other terminal move
                # (_move): no reader may find a COMPLETE dispatch still
                # owned, i.e. executing, while the runner's thread is still
                # on its way out.
                owner=None,
            )
            if not was_complete or completion != current.completion:
                extra: dict[str, Any] = {}
                if finalize and outstanding:
                    extra = {"finalized": True, "outstanding_shards": outstanding}
                self._publish(pending, dispatch, extra=extra)
            else:
                self._sync_headline(dispatch.run_id)
            return dispatch

    def release(self, dispatch_id: str, *, owner: str | None = None) -> None:
        """Nothing in this process is working on the dispatch any more. With
        ``owner``, only a dispatch that still names that owner is released.
        A terminal dispatch was released by the move that settled it; this
        is for a dispatch a runner returned from while it is still active
        (awaiting deliveries from elsewhere)."""
        with self._decision():
            current = self._store.get_dispatch(dispatch_id)
            if current is None or current.owner is None:
                return
            if owner is None or current.owner == owner:
                self._store.update_dispatch(dispatch_id, owner=None)

    def release_unless_cancel_requested(self, dispatch_id: str, *, owner: str) -> bool:
        """What ``release`` does for a dispatch its runner returned from —
        unless a cancel was requested for it and it is still active: then it
        stays ``owner``'s, who closes it. True when it was released (or is
        not ``owner``'s to release), False when it was kept.

        One decision, so that a cancel requested before it finds the
        dispatch owned — and leaves it to its owner — and one requested
        after it finds it free, and closes it itself (:meth:`claim`)."""
        with self._decision():
            current = self._store.get_dispatch(dispatch_id)
            if current is None or current.owner != owner:
                return True
            if current.cancel_requested and current.state in lifecycle.ACTIVE_STATES:
                return False
            self._store.update_dispatch(dispatch_id, owner=None)
            return True

    def claim(
        self, dispatch_id: str, *, owner: str, alive: Callable[[str], bool], force: bool = False
    ) -> tuple[bool, DispatchRecord | None]:
        """Make ``owner`` the one who closes an active dispatch nobody is
        working on: one whose owner is none, or gone (``alive`` says no), or
        — with ``force`` — another's whatever ``alive`` says. Returns whether
        it is ``owner``'s now, and the record as it was found.

        One decision: of two who would close the same dispatch — two
        cancels at once, in one process or in two — one gets it. A dispatch
        ``owner`` already holds is not claimed again, except with ``force``:
        the way out for a dispatch that names its owner but that nobody is
        working on."""
        with self._decision():
            current = self._store.get_dispatch(dispatch_id)
            if current is None or current.state not in lifecycle.ACTIVE_STATES:
                return False, current
            if current.owner and not force and (current.owner == owner or alive(current.owner)):
                return False, current
            self._store.update_dispatch(dispatch_id, owner=owner)
            return True, current

    # -- internals ------------------------------------------------------------------

    @contextmanager
    def _decision(self) -> Iterator[list[_Pending]]:
        """One lifecycle decision: the store's atomic unit around reading the
        facts, evaluating them and writing the result, then the events the
        decision produced — after the commit, so that a reader woken by an
        event finds the state it announces. A decision taken inside another
        joins it, and its events wait for the outer one."""
        outer = self._local.pending
        if outer is not None:
            yield outer
            return
        pending: list[_Pending] = []
        with self._lock:
            self._local.pending = pending
            try:
                with self._store.atomic():
                    yield pending
            finally:
                self._local.pending = None
            for run_id, kind, dispatch_id, data in pending:
                self._bus.emit(run_id, kind, dispatch_id=dispatch_id, data=data)

    def _move(
        self,
        pending: list[_Pending],
        current: DispatchRecord,
        target: str,
        *,
        error: str,
        extra: Mapping[str, Any] | None,
        error_code: str = "",
        error_owner: str | None = None,
    ) -> DispatchRecord:
        dispatch_id = current.dispatch_id
        changes: dict[str, Any] = {"state": lifecycle.advance(current.state, target)}
        if error:
            changes["error"] = self._redact(error)
        if error_code:
            if error_code not in DISPATCH_ERROR_CODES:
                raise ValueError(f"unknown dispatch error code {error_code!r}")
            changes["error_code"] = error_code
            changes["error_owner"] = error_owner
        if target in lifecycle.TERMINAL_STATES:
            changes["owner"] = None
        dispatch: DispatchRecord = self._store.update_dispatch(dispatch_id, **changes)
        if target in lifecycle.TERMINAL_STATES:
            dispatch = self._store.update_dispatch(
                dispatch_id, summary=lifecycle.summarize(self.scored_verdicts(dispatch))
            )
        self._publish(pending, dispatch, extra=extra)
        return dispatch

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

    def _publish(
        self,
        pending: list[_Pending],
        dispatch: DispatchRecord,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        """Headline now, inside the decision; the event once it is committed."""
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
        pending.append((dispatch.run_id, kinds.STATE_EVENTS[dispatch.state], dispatch.dispatch_id, data))
