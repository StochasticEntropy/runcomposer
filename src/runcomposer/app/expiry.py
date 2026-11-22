"""Expiry of dispatches that wait for results too long (DESIGN.md §4).

A dispatch handed to an external executor waits in ``AWAITING_RESULTS`` until
its results arrive. If the executor died, they never do. With
``core.dispatch.awaiting_timeout_s`` set, a served deployment closes such a
dispatch itself (:func:`expire_awaiting`, which ``Service.expire_awaiting``
calls); :class:`AwaitingExpiry` is the loop that asks. ``runcomposer gc``
asks once, for deployments without a server.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from runcomposer.core import events as kinds
from runcomposer.core import lifecycle
from runcomposer.core.errors import InvalidTransition
from runcomposer.core.model import DispatchRecord, RunRecord
from runcomposer.core.timeutil import epoch_ms

if TYPE_CHECKING:
    from .service import Service

__all__ = ["AwaitingExpiry", "expire_awaiting"]

log = logging.getLogger("runcomposer.expiry")


class AwaitingExpiry:
    """A background loop inside ``serve``. Does nothing — starts no thread —
    while the option is unset."""

    def __init__(self, service: Service):
        self._service = service
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def interval_s(self) -> float | None:
        timeout = self._service.config.dispatch.get("awaiting_timeout_s")
        # Often enough that a dispatch is closed within a quarter of the
        # timeout after it ran out; never busier than once a second.
        return None if not timeout else max(1.0, min(60.0, float(timeout) / 4))

    def start(self) -> None:
        if self.interval_s() is None or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="runcomposer-expiry", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        interval = self.interval_s() or 60.0
        while not self._stop.wait(interval):
            try:
                self._service.expire_awaiting()
            except Exception:
                log.exception("expiring dispatches that await results failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def expire_awaiting(service: Service, *, now_ms: int | None = None) -> list[DispatchRecord]:
    """Close dispatches that have waited for results longer than
    ``core.dispatch.awaiting_timeout_s`` (DESIGN.md §4): they become
    ``FAILED`` with a ``run.failed`` event saying so and naming the
    shards that never delivered. What was delivered is kept, and results
    that still arrive may complete the dispatch — exactly as for any
    other failed dispatch. Nothing happens while the option is unset.

    The clock starts when the dispatch began to wait and restarts with
    every delivery: a job that reports shard after shard is not expired
    between two of them."""
    timeout = service.config.dispatch.get("awaiting_timeout_s")
    if not timeout:
        return []
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    waiting: list[RunRecord] = []
    offset = 0
    while True:
        page = service.store.list_runs(state="AWAITING_RESULTS", limit=200, offset=offset)
        waiting.extend(page.runs)
        offset += len(page.runs)
        if not page.runs or offset >= page.total:
            break
    expired: list[DispatchRecord] = []
    for run in waiting:
        dispatch = run.latest_dispatch
        if dispatch is None or dispatch.state != "AWAITING_RESULTS":
            continue
        since = epoch_ms(dispatch.created_at) or 0
        for event in service.store.latest_events(run.id, limit=500):
            if event.dispatch_id == dispatch.dispatch_id and event.kind in (
                kinds.RUN_AWAITING, kinds.DELIVERY_RECORDED
            ):
                since = max(since, epoch_ms(event.at) or 0)
        waited_s = (now - since) / 1000.0
        if waited_s < float(timeout):
            continue
        outstanding = lifecycle.outstanding_shards(
            dispatch.shards, service.store.delivered_shards(run.id, dispatch.dispatch_id)
        )
        try:
            # The ledger re-reads the state inside its own unit: a
            # dispatch another process completed (or cancelled) since it
            # was found waiting is not moved — that race is lost
            # benignly, the results came after all.
            moved = service.ledger.move(
                dispatch.dispatch_id,
                "FAILED",
                error_code="expired",
                error=(
                    f"expired: no results for {waited_s:.0f}s, longer than "
                    f"core.dispatch.awaiting_timeout_s ({float(timeout):g}s) — "
                    f"outstanding shard(s): {outstanding or 'none'}. Results that still "
                    "arrive are taken in and may complete it"
                ),
                extra={
                    "expired": True,
                    "waited_s": round(waited_s, 1),
                    "awaiting_timeout_s": float(timeout),
                    "outstanding_shards": outstanding,
                },
            )
        except InvalidTransition:
            current = service.store.get_dispatch(dispatch.dispatch_id)
            log.info(
                "dispatch %s of run %s was not expired: it is %s by now",
                dispatch.dispatch_id, run.id, current.state if current else "gone",
            )
            continue
        expired.append(moved)
        log.warning("dispatch %s of run %s expired while awaiting results",
                    dispatch.dispatch_id, run.id)
    return expired
