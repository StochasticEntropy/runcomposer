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
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from runcomposer.core.model import DispatchRecord

from .dispatching import owner_alive

if TYPE_CHECKING:
    from .service import Service

__all__ = ["recover"]

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
