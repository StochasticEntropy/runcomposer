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
    """Items distributed over chunks by :func:`plan_chunks`, balanced by the
    durations known: the chunks, their estimates, and how much was known."""

    chunks: tuple[tuple[str, ...], ...]
    #: Estimated milliseconds per chunk (0.0 where nothing is known) — the
    #: sum of the *measured* durations of its items only.
    estimates_ms: tuple[float, ...]
    #: False on a cold start: no item had a known duration.
    balanced: bool
    #: How many of the planned items had duration history.
    known: int
    total: int
    #: The weight an item without a known duration was given: the mean of
    #: the known durations (0.0 on a cold start, or when every item is known
    #: — then nobody needed it).
    fallback_ms: float = 0.0
    #: What the balancer weighed per chunk: measured durations plus
    #: ``fallback_ms`` for every item without one. These are the totals the
    #: chunks were balanced on (empty on a cold start).
    loads_ms: tuple[float, ...] = ()
    #: Items without a known duration, per chunk.
    unknown: tuple[int, ...] = ()
    #: The heaviest item and its weight, when it alone weighs more than an
    #: even share of the whole selection: the chunk holding it cannot finish
    #: sooner, and the other items are balanced around it.
    dominant: tuple[str, float] | None = None

    def describe(self) -> str:
        """The human-readable plan: what was decided and on what evidence."""
        lines = []
        if not self.balanced:
            lines.append(
                "cold start: no duration history — round-robin chunking; balance "
                "improves as completed dispatches accrue"
            )
        else:
            line = f"duration-balanced chunking: history for {self.known}/{self.total} item(s)"
            if self.total - self.known == 1:
                line += (
                    "; the other one weighs the mean of the known durations, "
                    f"{self.fallback_ms:.0f}ms"
                )
            elif self.known < self.total:
                line += (
                    f"; the other {self.total - self.known} weigh the mean of the known "
                    f"durations, {self.fallback_ms:.0f}ms each"
                )
            lines.append(line)
        for index, (chunk, estimate) in enumerate(
            zip(self.chunks, self.estimates_ms, strict=True), start=1
        ):
            line = f"chunk {index}: {len(chunk)} item(s), est {estimate:.0f}ms"
            if self.loads_ms and self.unknown[index - 1]:
                line += (
                    f" measured + {self.unknown[index - 1]} without history → balanced on "
                    f"{self.loads_ms[index - 1]:.0f}ms"
                )
            lines.append(line)
        if self.dominant is not None and len(self.chunks) > 1:
            item, weight = self.dominant
            share = sum(self.loads_ms) / len(self.chunks)
            lines.append(
                f"{item} alone weighs {weight:.0f}ms, more than an even share of "
                f"{share:.0f}ms per chunk: its chunk is the longest whatever the split, "
                "and the other items are balanced around it"
            )
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
    fallback = 0.0
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

    def weight(item: str) -> float:
        return history.get(item) or fallback

    dominant: tuple[str, float] | None = None
    if known and len(kept) > 1:
        heaviest = max(items, key=weight)
        if weight(heaviest) > sum(weight(item) for item in items) / len(kept):
            dominant = (heaviest, weight(heaviest))
    return ChunkPlan(
        chunks=tuple(tuple(bucket) for bucket in kept),
        estimates_ms=tuple(sum(history.get(item) or 0.0 for item in bucket) for bucket in kept),
        balanced=bool(known),
        known=len(known),
        total=len(items),
        fallback_ms=fallback if len(known) < len(items) else 0.0,
        loads_ms=tuple(sum(weight(item) for item in bucket) for bucket in kept) if known else (),
        unknown=tuple(sum(1 for item in bucket if not history.get(item)) for bucket in kept)
        if known
        else (),
        dominant=dominant,
    )


def split_max(item_ids: Sequence[str], max_items: int | None) -> list[list[str]]:
    """Cut one chunk into consecutive pieces of at most ``max_items`` — the
    bound on a single execution's size. ``None`` or 0 means unbounded."""
    items = list(item_ids)
    if not max_items or max_items <= 0 or len(items) <= max_items:
        return [items] if items else []
    return [items[start : start + max_items] for start in range(0, len(items), max_items)]
