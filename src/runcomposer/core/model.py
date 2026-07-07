"""Core domain model — the only vocabulary the core knows (DESIGN.md §2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

VERDICT_STATUSES = ("PASS", "FAIL", "SKIP", "ERROR")


@dataclass(frozen=True)
class Item:
    """A runnable test: an opaque stable id minted by its TestSource, plus tags.

    The core never parses, splits, or normalizes ids — it compares them for
    equality only (DESIGN.md §2, "Item id (normative)").
    """

    id: str
    tags: tuple[str, ...] = ()
    name: str = ""
    hierarchy: tuple[str, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict, compare=False)

    @property
    def display_name(self) -> str:
        return self.name or self.id


@dataclass(frozen=True)
class Verdict:
    """Per-item result. `flaky` is derivable from attempts, never stored."""

    item_id: str
    status: str
    duration_ms: int = 0
    message: str = ""
    artifacts: tuple[str, ...] = ()
    attempt: int = 1

    def __post_init__(self) -> None:
        if self.status not in VERDICT_STATUSES:
            raise ValueError(f"verdict status must be one of {VERDICT_STATUSES}, got {self.status!r}")
