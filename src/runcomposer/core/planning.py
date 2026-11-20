"""Duration-balanced chunk planning (DESIGN.md §6.2).

Splitting a selection into chunks of roughly equal *runtime* is the same
problem for every runner that executes in parallel, so it lives here rather
than inside one of them. Longest-processing-time first onto the lightest
chunk; with no duration history at all (a fresh store) it degrades to
round-robin and says so — the plan text is part of the result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = ["ChunkPlan", "plan_chunks", "split_max"]


@dataclass(frozen=True)
class ChunkPlan:
    chunks: tuple[tuple[str, ...], ...]
    #: Estimated milliseconds per chunk (0.0 where nothing is known).
    estimates_ms: tuple[float, ...]
    #: False on a cold start: no item had a known duration.
    balanced: bool
    #: How many of the planned items had duration history.
    known: int
    total: int

    def describe(self) -> str:
        """The human-readable plan: what was decided and on what evidence."""
        lines = []
        if not self.balanced:
            lines.append(
                "cold start: no duration history — round-robin chunking; balance "
                "improves as completed dispatches accrue"
            )
        else:
            lines.append(
                f"duration-balanced chunking: history for {self.known}/{self.total} item(s)"
            )
        for index, (chunk, estimate) in enumerate(
            zip(self.chunks, self.estimates_ms, strict=True), start=1
        ):
            lines.append(f"chunk {index}: {len(chunk)} item(s), est {estimate:.0f}ms")
        return "\n".join(lines)


def plan_chunks(
    item_ids: Sequence[str],
    chunk_count: int,
    durations: Mapping[str, float] | None = None,
) -> ChunkPlan:
    """Distribute ``item_ids`` over at most ``chunk_count`` chunks.

    Items without a known duration are weighted with the mean of the known
    ones. Empty chunks are dropped, so fewer chunks than asked for may come
    back (and none at all for an empty selection).
    """
    items = list(item_ids)
    count = max(1, min(int(chunk_count), len(items) or 1))
    history = durations or {}
    known = [item for item in items if history.get(item)]
    buckets: list[list[str]] = [[] for _ in range(count)]
    if not known:
        for index, item in enumerate(items):
            buckets[index % count].append(item)
    else:
        fallback = sum(history[item] for item in known) / len(known)
        loads = [0.0] * count
        for item in sorted(items, key=lambda i: history.get(i) or fallback, reverse=True):
            lightest = loads.index(min(loads))
            buckets[lightest].append(item)
            loads[lightest] += history.get(item) or fallback
    kept = [bucket for bucket in buckets if bucket]
    return ChunkPlan(
        chunks=tuple(tuple(bucket) for bucket in kept),
        estimates_ms=tuple(sum(history.get(item) or 0.0 for item in bucket) for bucket in kept),
        balanced=bool(known),
        known=len(known),
        total=len(items),
    )


def split_max(item_ids: Sequence[str], max_items: int | None) -> list[list[str]]:
    """Cut one chunk into consecutive pieces of at most ``max_items`` — the
    bound on a single execution's size. ``None`` or 0 means unbounded."""
    items = list(item_ids)
    if not max_items or max_items <= 0 or len(items) <= max_items:
        return [items] if items else []
    return [items[start : start + max_items] for start in range(0, len(items), max_items)]
