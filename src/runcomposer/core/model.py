"""Core domain model — the only vocabulary the core knows (DESIGN.md §2)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DISPATCH_ERROR_CODES",
    "LIVE_STATUSES",
    "REPORTABLE_STATUSES",
    "VERDICT_STATUSES",
    "ArtifactRef",
    "DeliveryRecord",
    "DispatchRecord",
    "Item",
    "LiveStatus",
    "RunEvent",
    "RunPage",
    "RunRecord",
    "Verdict",
]

#: What an executor can report about an item.
REPORTABLE_STATUSES = ("PASS", "FAIL", "SKIP", "ERROR")
#: ``MISSING`` is the one status nobody reports: it is *derived* for an item
#: that was planned and never came back (DESIGN.md §4). It is never stored and
#: a parser or a push that claims it is refused.
VERDICT_STATUSES = (*REPORTABLE_STATUSES, "MISSING")
#: What may be streamed about an item *while its shard executes*: that it is
#: executing now (``RUNNING``), or how it ended. ``RUNNING`` exists here only
#: — it is never delivered, never stored as a verdict and never scored.
LIVE_STATUSES = ("RUNNING", *REPORTABLE_STATUSES)
#: Why a dispatch ended with an error (``DispatchRecord.error_code``), beside
#: the free-text ``error``: ``runner-raised`` — the runner raised after the
#: hand-off (``FAILED``); ``interrupted`` — the process that was executing it
#: is gone, a server stopped or a machine restarted (``STALE``; the process
#: is ``error_owner``); ``expired`` — no results came within
#: ``core.dispatch.awaiting_timeout_s`` (``FAILED``); ``cancelled`` — on
#: request, before the hand-off or while queued (``CANCELLED``). A closed
#: list: ``""`` is no error.
DISPATCH_ERROR_CODES = ("runner-raised", "interrupted", "expired", "cancelled")


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
    """Per-item result. ``flaky`` is derivable from attempts, never stored.

    ``shard`` is the partition/chunk label the verdict was delivered under
    (DESIGN.md §4: run → dispatch → shard). It is **assigned by the delivery,
    not by the producer**: a parser or runner builds verdicts without it, the
    delivery labels the whole bundle, and every verdict read back carries it.
    ``attempt`` counts executions of the item within one shard; the highest
    attempt is the one that counts (``core.lifecycle.effective_verdicts``).
    """

    item_id: str
    status: str
    duration_ms: int = 0
    message: str = ""
    attempt: int = 1
    shard: str = ""

    def __post_init__(self) -> None:
        if self.status not in VERDICT_STATUSES:
            raise ValueError(f"verdict status must be one of {VERDICT_STATUSES}, got {self.status!r}")


@dataclass(frozen=True)
class LiveStatus:
    """What is known about one item while its shard is still executing
    (DESIGN.md §6.2): ``RUNNING``, or the status it ended with. Kept apart
    from verdicts on purpose — a live status is a progress report, replaced
    per (shard, item) by the next one and dropped when the shard delivers.
    ``duration_ms`` is 0 for an item that is still running."""

    item_id: str
    status: str
    duration_ms: int = 0
    message: str = ""
    attempt: int = 1
    shard: str = ""

    def __post_init__(self) -> None:
        if self.status not in LIVE_STATUSES:
            raise ValueError(f"live status must be one of {LIVE_STATUSES}, got {self.status!r}")


@dataclass(frozen=True)
class DispatchRecord:
    """One hand-off to an executor (DESIGN.md §4), and the unit the lifecycle
    is tracked on.

    ``mode`` is a configured runner id, ``"export"`` (a document handed out
    for somebody else to execute) or ``"ingest"`` (results that arrived for a
    run nothing was dispatched for). ``shards`` is the declared shard *set* —
    completion is computed against these names, never against a count — and
    ``shard_items`` optionally says which items each shard was planned to
    report. ``shard_groups`` optionally labels shards that belong together
    (the partition a chunk ran on, say): a display and roll-up grouping that
    decides nothing. ``spec_sha256`` is the hash of the exact document bytes handed
    out — the marker verification anchor (§5).
    """

    dispatch_id: str
    run_id: str
    mode: str
    state: str
    created_at: str
    shards: tuple[str, ...] = ()
    shard_items: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    shard_groups: Mapping[str, str] = field(default_factory=dict)
    spec_sha256: str | None = None
    error: str = ""
    plan: str = ""
    links: Mapping[str, str] = field(default_factory=dict)
    parameters: Mapping[str, Any] = field(default_factory=dict)
    completion: str | None = None
    completed_at: str | None = None
    #: Verdict counts by status over the effective verdicts, ``MISSING``
    #: included — maintained by the lifecycle so a run list needs no per-row
    #: aggregation.
    summary: Mapping[str, int] = field(default_factory=dict)
    #: Identity of the process executing an in-process dispatch; ``None`` once
    #: nothing in any process is working on it.
    owner: str | None = None
    cancel_requested: bool = False
    #: Why it ended with an error — one of :data:`DISPATCH_ERROR_CODES`, or
    #: ``""`` (no error, or one recorded before the codes existed).
    error_code: str = ""
    #: For an ``interrupted`` dispatch: the identity of the process that was
    #: executing it and is gone (``host:pid:…``) — kept out of ``error``.
    error_owner: str | None = None

    @property
    def declared_shards(self) -> int:
        return len(self.shards)


@dataclass(frozen=True)
class DeliveryRecord:
    """One ingested results bundle, content-hashed for idempotency (§5)."""

    delivery_id: str
    run_id: str
    dispatch_id: str
    shard: str
    content_hash: str
    format: str
    created_at: str


@dataclass(frozen=True)
class ArtifactRef:
    """A reference to one result artifact of a run (DESIGN.md §6.4).

    ``url_or_path`` is either an absolute ``http(s)`` URL — a remote artifact
    that passes through untouched and is never fetched — or a filesystem path
    written by whatever executed the run. Which of the two it is, and whether
    a local path is servable, is decided by the app layer against the
    configured artifact directory; the core records the string. An artifact
    belongs to the dispatch as a whole, to one ``shard``, or to a ``group``
    of shards (one log covering every chunk of a partition).
    """

    run_id: str
    dispatch_id: str | None
    name: str
    media_type: str
    url_or_path: str
    shard: str | None = None
    group: str | None = None


@dataclass(frozen=True)
class RunEvent:
    """One entry of a run's event stream (DESIGN.md §4): every state change,
    every delivery, every refusal and every runner log line. ``seq`` is the
    stream cursor — strictly increasing per store."""

    seq: int
    run_id: str
    at: str
    kind: str
    dispatch_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunRecord:
    """The stored record for one spec (DESIGN.md §2, §4). ``state``,
    ``completion`` and ``completed_at`` are the *headline*: derived from the
    latest dispatch by ``core.lifecycle.headline`` and cached here so runs can
    be listed and filtered by them."""

    id: str
    title: str
    created_at: str
    origin: str
    labels: Mapping[str, str]
    state: str
    completion: str | None = None
    completed_at: str | None = None
    planned_count: int = 0
    dispatches: tuple[DispatchRecord, ...] = ()
    deliveries: tuple[DeliveryRecord, ...] = ()

    @property
    def latest_dispatch(self) -> DispatchRecord | None:
        return self.dispatches[-1] if self.dispatches else None


@dataclass(frozen=True)
class RunPage:
    """One page of a run listing plus the size of the whole filtered set."""

    runs: tuple[RunRecord, ...]
    total: int
