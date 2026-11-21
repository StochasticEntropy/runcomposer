"""Restart recovery (DESIGN.md §4): what a dead process left behind.

A dispatch names its owner while a runner is executing it in some process.
When that process is gone — the server was killed, the machine rebooted — the
dispatch can never finish on its own, and leaving it ``RUNNING`` forever is a
lie. On startup every dispatch whose owner is no longer alive is reconciled:

An owner is gone when its process id is dead, when it is an earlier life of
this very process id (pid 1 of a restarted container), or when it ran on
another host — one instance owns a store, so that is a replaced container.

- still ``DISPATCHED`` or ``RUNNING`` → ``STALE``, with an event saying why.
  Its partial results are kept, and results that arrive later may still
  complete it;
- already past that (awaiting results, or terminal) → only the stale owner
  mark is cleared.

A served deployment does not stop at startup: another process on the same
store — a foreground ``runcomposer run`` — may die while the server is up,
and its dispatch would stay ``RUNNING`` in every answer the server gives.
:class:`PeriodicRecovery` reconciles again every
``core.dispatch.recovery_interval_s`` seconds. Each pass decides per
dispatch inside the ledger's unit, so it never marks a dispatch whose owner
is alive or is this process, and never one that moved on meanwhile.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from runcomposer.core.model import DispatchRecord

from .liveness import owner_alive

if TYPE_CHECKING:
    from .service import Service

__all__ = ["PeriodicRecovery", "recover"]

log = logging.getLogger("runcomposer.recovery")


def recover(service: Service) -> list[DispatchRecord]:
    """Reconcile interrupted dispatches. Returns those marked STALE."""
    stale: list[DispatchRecord] = []
    mine = service.dispatcher.owner
    for dispatch in service.store.owned_dispatches():
        if dispatch.owner and owner_alive(dispatch.owner, mine):
            continue
        owner = dispatch.owner or ""
        if dispatch.state in ("DISPATCHED", "RUNNING"):
            # Decided again inside the ledger's unit: another process
            # recovering the same store at the same moment marks it once.
            marked = service.ledger.mark_stale(
                dispatch.dispatch_id,
                owner=owner,
                error=(
                    f"interrupted while {dispatch.state}: the process that was executing "
                    f"this dispatch ({dispatch.owner}) is gone"
                ),
                extra={"was": dispatch.state, "owner": dispatch.owner},
            )
            if marked is not None:
                log.warning(
                    "dispatch %s of run %s was %s under owner %s, which is gone — marked STALE",
                    dispatch.dispatch_id, dispatch.run_id, dispatch.state, dispatch.owner,
                )
                stale.append(marked)
        else:
            service.ledger.release(dispatch.dispatch_id, owner=owner)
    return stale


class PeriodicRecovery:
    """A background loop inside ``serve``: :func:`recover` every
    ``core.dispatch.recovery_interval_s`` seconds. Starts no thread while
    the option is ``null``."""

    def __init__(self, service: Service):
        self._service = service
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def interval_s(self) -> float | None:
        interval = self._service.config.dispatch.get("recovery_interval_s")
        return None if interval is None else float(interval)

    def start(self) -> None:
        if self.interval_s() is None or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="runcomposer-recovery", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        interval = self.interval_s() or 15.0
        while not self._stop.wait(interval):
            try:
                recover(self._service)
            except Exception:
                log.exception("recovering dispatches of processes that are gone failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
