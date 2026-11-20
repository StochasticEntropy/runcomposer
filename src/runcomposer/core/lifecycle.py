"""Run lifecycle (DESIGN.md §4): the state machine, shard accounting and
computed completion — pure functions, the single place these rules live.

The lifecycle is tracked **per dispatch**. A run's headline state is derived
from its latest dispatch and never written independently, so re-dispatching a
finished run cannot erase what the previous dispatch established, and a late
bundle for an old dispatch cannot change the headline.

Completion is computed, never guessed:

- a dispatch is complete when every shard it *declared by name* has
  delivered — a delivery under some other label does not stand in for one;
- its completion is aggregated over the **effective** verdicts (the highest
  attempt per shard and item) plus one derived ``MISSING`` verdict for every
  planned item that never came back. A run with a missing item is
  ``INCOMPLETE`` at best; it cannot be ``PASS``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from .errors import InvalidTransition
from .model import DispatchRecord, Verdict

__all__ = [
    "ACTIVE_STATES",
    "COMPLETIONS",
    "RUN_STATES",
    "TERMINAL_STATES",
    "advance",
    "completion_of",
    "effective_verdicts",
    "headline",
    "missing_verdicts",
    "outstanding_shards",
    "summarize",
]

RUN_STATES = (
    "COMPOSED",
    "DISPATCHED",
    "RUNNING",
    "AWAITING_RESULTS",
    "COMPLETE",
    "FAILED",
    "CANCELLED",
    "STALE",
)
#: A dispatch somebody is still expected to act on.
ACTIVE_STATES = ("DISPATCHED", "RUNNING", "AWAITING_RESULTS")
TERMINAL_STATES = ("COMPLETE", "FAILED", "CANCELLED", "STALE")
COMPLETIONS = ("PASS", "FAIL", "ERROR", "INCOMPLETE")

# The whole state machine. A dispatch is born DISPATCHED (queued, or the
# runner is preparing); `declare` moves it to RUNNING. STALE and FAILED keep
# their partial results and may still be completed by results that arrive
# late — an interrupted process does not un-run the tests that had finished.
_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "DISPATCHED": frozenset(
        {"RUNNING", "AWAITING_RESULTS", "COMPLETE", "FAILED", "CANCELLED", "STALE"}
    ),
    "RUNNING": frozenset({"AWAITING_RESULTS", "COMPLETE", "FAILED", "CANCELLED", "STALE"}),
    "AWAITING_RESULTS": frozenset({"COMPLETE", "FAILED", "CANCELLED", "STALE"}),
    "STALE": frozenset({"COMPLETE"}),
    "FAILED": frozenset({"COMPLETE"}),
    "COMPLETE": frozenset({"COMPLETE"}),  # a corrected shard recomputes completion
    "CANCELLED": frozenset(),
}


def advance(current: str, target: str) -> str:
    """THE transition function: return ``target`` if a dispatch in
    ``current`` may move there, raise :class:`InvalidTransition` otherwise."""
    if current not in _TRANSITIONS or target not in RUN_STATES:
        raise InvalidTransition(f"unknown lifecycle state in {current!r} → {target!r}")
    if target not in _TRANSITIONS[current]:
        raise InvalidTransition(f"a dispatch cannot move from {current} to {target}")
    return target


def outstanding_shards(declared: Sequence[str], delivered: Iterable[str]) -> list[str]:
    """Declared shards that have not delivered, in declaration order."""
    have = set(delivered)
    return [shard for shard in declared if shard not in have]


def effective_verdicts(verdicts: Iterable[Verdict]) -> list[Verdict]:
    """The verdict that counts per (shard, item): the highest attempt, the
    later one on a tie. A retried item that finally passed is a pass — the
    earlier attempts stay readable, they just do not decide the outcome."""
    best: dict[tuple[str, str], Verdict] = {}
    for verdict in verdicts:
        key = (verdict.shard, verdict.item_id)
        held = best.get(key)
        if held is None or verdict.attempt >= held.attempt:
            best[key] = verdict
    return list(best.values())


def missing_verdicts(
    planned: Sequence[str],
    verdicts: Iterable[Verdict],
    shard_items: Mapping[str, Sequence[str]] | None = None,
) -> list[Verdict]:
    """One ``MISSING`` verdict per planned item nobody reported.

    With a per-shard plan (``shard_items``) the question is asked per shard —
    an item fanned out over two partitions and reported by one is missing in
    the other. Without one, an item reported anywhere in the dispatch counts
    as reported. Either way the *planned set* is what is owed: a planned item
    that no shard's plan mentions, and nobody reported, is missing too — a
    runner cannot shrink the run by declaring less than it was given.
    """
    seen = list(verdicts)
    missing: list[Verdict] = []
    if shard_items:
        reported = {(v.shard, v.item_id) for v in seen}
        for shard, items in shard_items.items():
            for item_id in items:
                if (shard, item_id) not in reported:
                    missing.append(
                        Verdict(item_id, "MISSING", message="not-reported", shard=shard)
                    )
        covered = {item_id for items in shard_items.values() for item_id in items}
        reported_anywhere = {v.item_id for v in seen}
        missing.extend(
            Verdict(item_id, "MISSING", message="not-reported")
            for item_id in planned
            if item_id not in covered and item_id not in reported_anywhere
        )
        return missing
    reported_ids = {v.item_id for v in seen}
    return [
        Verdict(item_id, "MISSING", message="not-reported")
        for item_id in planned
        if item_id not in reported_ids
    ]


def completion_of(verdicts: Iterable[Verdict]) -> str:
    """Aggregate verdicts — already including the derived ``MISSING`` ones —
    to a completion: ``ERROR`` > ``FAIL`` > ``INCOMPLETE`` > ``PASS``.

    Nothing reported at all is ``INCOMPLETE``, never ``PASS``: an empty result
    is the absence of evidence. ``SKIP`` alone completes as ``PASS`` — the
    item was reported, and it did not fail."""
    result = "PASS"
    seen = False
    for verdict in effective_verdicts(verdicts):
        seen = True
        if verdict.status == "ERROR":
            return "ERROR"
        if verdict.status == "FAIL":
            result = "FAIL"
        elif verdict.status == "MISSING" and result == "PASS":
            result = "INCOMPLETE"
    return result if seen else "INCOMPLETE"


def summarize(verdicts: Iterable[Verdict]) -> dict[str, int]:
    """Counts by status over the effective verdicts."""
    counts: dict[str, int] = {}
    for verdict in effective_verdicts(verdicts):
        counts[verdict.status] = counts.get(verdict.status, 0) + 1
    return counts


def headline(dispatches: Sequence[DispatchRecord]) -> tuple[str, str | None, str | None]:
    """A run's ``(state, completion, completed_at)``: its latest dispatch's,
    or ``COMPOSED`` when nothing was ever handed off."""
    if not dispatches:
        return "COMPOSED", None, None
    latest = dispatches[-1]
    return latest.state, latest.completion, latest.completed_at
