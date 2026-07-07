"""Run lifecycle (DESIGN.md §4): states, shard accounting, computed completion.

Completion is computed, never guessed: a run reaches COMPLETE when all
declared shards of its latest dispatch have delivered, or on explicit
finalize. The headline state reflects the latest dispatch.
"""

from __future__ import annotations

from typing import Iterable

from .model import Verdict

RUN_STATES = ("COMPOSED", "DISPATCHED", "RUNNING", "AWAITING_RESULTS", "COMPLETE", "STALE")
COMPLETIONS = ("PASS", "FAIL", "ERROR")


def completion_of(verdicts: Iterable[Verdict]) -> str:
    """Aggregate verdicts to a run completion: ERROR > FAIL > PASS.

    SKIPs alone (or an empty set) still complete as PASS — an executed run
    where nothing failed."""
    result = "PASS"
    for verdict in verdicts:
        if verdict.status == "ERROR":
            return "ERROR"
        if verdict.status == "FAIL":
            result = "FAIL"
    return result


def all_shards_delivered(declared_shards: int | None, delivered: set[str]) -> bool:
    """Export-mode dispatches default to 1 shard unless declared otherwise
    (DESIGN.md §4)."""
    return len(delivered) >= (declared_shards or 1)
