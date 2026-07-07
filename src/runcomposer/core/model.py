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


@dataclass(frozen=True)
class DispatchRecord:
    """One hand-off to an executor (DESIGN.md §4). ``mode`` is a runner plugin
    id or ``"export"``. ``spec_sha256`` is the hash of the exact document
    bytes handed out — the marker verification anchor (§5)."""

    dispatch_id: str
    run_id: str
    mode: str
    declared_shards: int | None
    created_at: str
    spec_sha256: str | None = None


@dataclass(frozen=True)
class DeliveryRecord:
    """One ingested results bundle, content-hashed for idempotency (§5)."""

    delivery_id: str
    run_id: str
    dispatch_id: str | None
    shard: str
    content_hash: str
    format: str
    created_at: str


@dataclass(frozen=True)
class RunRecord:
    """The stored lifecycle record for one spec (DESIGN.md §2, §4)."""

    id: str
    title: str
    created_at: str
    origin: str
    labels: Mapping[str, str]
    state: str
    completion: str | None = None
    completed_at: str | None = None
    dispatches: tuple[DispatchRecord, ...] = ()
    deliveries: tuple[DeliveryRecord, ...] = ()
