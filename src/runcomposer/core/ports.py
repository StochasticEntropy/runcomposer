"""In-process plugin protocols (DESIGN.md §6).

P0 shipped TestSource and Runner. P1 adds RunStore (§6.3) and ResultParser
(§5) — the store because compose/export needs persistence, the parser because
the export round-trip ingests result bundles. Framework-native parsers arrive
with P2/P3; P1 ships only the runcomposer-verdicts reference format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .model import DeliveryRecord, DispatchRecord, Item, RunRecord, Verdict


class DispatchRefused(RuntimeError):
    """Raised by a Runner that refuses a dispatch (e.g. the §3.3 drift check)."""


@dataclass(frozen=True)
class RunnerInfo:
    """Runner identity + capability flags (e.g. live_status, cancel)."""

    id: str
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class DispatchHandle:
    """Returned by Runner.dispatch: dispatch id, declared shards, links.

    ``spec_sha256`` is the hash of the exact document bytes the runner handed
    to the executor — when set, ingestion can verify returned markers (§5)."""

    dispatch_id: str
    shards: int = 1
    links: Mapping[str, str] = field(default_factory=dict)
    spec_sha256: str | None = None


@runtime_checkable
class DispatchRecorder(Protocol):
    """Writes the dispatch record of a reservation (see DispatchReservation).

    Keyword-only, so a runner declares exactly what it knows: ``shards`` (the
    §4 shard declaration, known after planning) and ``spec_sha256`` (the hash
    of the exact document bytes it handed to the executor, §5)."""

    def __call__(self, *, shards: int | None = None, spec_sha256: str | None = None) -> None: ...


@dataclass(frozen=True)
class DispatchReservation:
    """A dispatch id minted by runcomposer *before* the runner executes.

    A runner that exposes the optional ``bind_dispatch`` hook is handed one of
    these ahead of ``dispatch()``. It uses ``dispatch_id`` as its dispatch id
    and calls ``record(...)`` at the moment it hands the work to its executor.
    From that call on, the dispatch exists in the store — so a runner that
    blocks for the whole execution is visible as a run that is RUNNING *with*
    its dispatch, instead of a run whose hand-off only appears once it is
    over (DESIGN.md §4: a dispatch is the record of a hand-off).

    Refusing before ``record`` leaves nothing behind: no hand-off, no
    dispatch. ``record`` may be called once, and only during ``dispatch()``.
    """

    dispatch_id: str
    record: DispatchRecorder


@dataclass(frozen=True)
class ParsedVerdict:
    """A ResultParser's raw output: the *native* name as the artifact spells
    it. Correlation to an Item id always goes through TestSource.resolve —
    the core never string-compares native names (DESIGN.md §5)."""

    native_name: str
    status: str
    duration_ms: int = 0
    message: str = ""
    attempt: int = 1


@runtime_checkable
class TestSource(Protocol):
    """Enumerates Items, produces a content-hashed catalog snapshot, and owns
    native-name→id resolution (DESIGN.md §6.1)."""

    provider_id: str

    def items(self) -> list[Item]: ...

    def snapshot(self) -> str: ...

    def resolve(self, native_name: str) -> str | None: ...


@runtime_checkable
class Runner(Protocol):
    """Fulfills a run spec. ``dispatch`` takes the document only (§6.2).

    ``describe`` + ``dispatch`` are the whole required contract. Two optional
    hooks are discovered by attribute — implement neither, one, or both:

    - ``bind(*, store, source, artifact_root)`` — in-process capability
      injection (live status, duration history, the §3.3 drift check).
    - ``bind_dispatch(reservation: DispatchReservation)`` — receive the
      dispatch id before executing and record the hand-off when it happens.
      Runners that block while the work runs should implement this; without
      it the dispatch is recorded from the returned handle, i.e. only once
      ``dispatch()`` has returned.
    """

    def describe(self) -> RunnerInfo: ...

    def dispatch(self, spec: Mapping[str, Any]) -> DispatchHandle: ...


@runtime_checkable
class ResultParser(Protocol):
    """Turns native result artifacts into ParsedVerdicts (§5). Parsers MUST
    parse defensively (for XML formats: no external entities, no DTD
    expansion)."""

    format_id: str

    def parse(self, path: Any) -> list[ParsedVerdict]: ...


@runtime_checkable
class RunStore(Protocol):
    """Persistence port (§6.3). Persisted schema (normative): runs, specs,
    dispatches, deliveries, verdicts, artifact_refs — and no runner-lifecycle
    fields (runner health/pool state are ephemeral runner memory).

    ``record_delivery`` carries the §5 idempotency contract:
    a byte-identical bundle (same content hash) is a no-op ("duplicate");
    a new bundle for the same (run, dispatch, shard) replaces that shard's
    verdicts ("replaced"); otherwise it is "new". No monotonic merge —
    corrections must be able to flip FAIL→PASS.
    """

    store_id: str

    def create_run(self, spec: Mapping[str, Any], *, origin: str, ingest_token_sha256: str | None = None) -> RunRecord: ...

    def get_ingest_token_sha256(self, run_id: str) -> str | None: ...

    def latest_completed_run(self, *, completed_before: str | None = None) -> RunRecord | None: ...

    def prune_runs(self, *, before: str | None = None, max_runs: int | None = None) -> list[str]: ...

    def get_run(self, run_id: str) -> RunRecord | None: ...

    def list_runs(
        self,
        *,
        state: str | None = None,
        labels: Mapping[str, str] | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> list[RunRecord]: ...

    def get_spec_document(self, run_id: str) -> dict[str, Any] | None: ...

    def add_dispatch(
        self,
        run_id: str,
        *,
        dispatch_id: str,
        mode: str,
        declared_shards: int | None,
        spec_sha256: str | None = None,
    ) -> DispatchRecord:
        """Declare a dispatch. Calling it again with the same ``dispatch_id``
        re-declares that same dispatch (never a second row, never a second
        ``created_at``): a dispatch is recorded at hand-off time, when the
        shard count may not be final yet, and refined from the returned
        handle."""
        ...

    def record_delivery(
        self,
        run_id: str,
        *,
        dispatch_id: str | None,
        shard: str,
        content_hash: str,
        format: str,
        verdicts: Sequence[Verdict],
    ) -> str: ...

    def delivered_shards(self, run_id: str, dispatch_id: str | None) -> set[str]: ...

    def record_live_verdict(self, run_id: str, dispatch_id: str, shard: str, verdict: Verdict) -> None: ...

    def clear_live_verdicts(self, run_id: str, dispatch_id: str) -> None: ...

    def duration_aggregates(
        self, *, labels: Mapping[str, str] | None = None, last_n: int = 5
    ) -> dict[str, float]: ...

    def verdicts_for(self, run_id: str, dispatch_id: str | None = None) -> list[Verdict]: ...

    def set_run_state(
        self,
        run_id: str,
        state: str,
        *,
        completion: str | None = None,
        completed_at: str | None = None,
    ) -> None: ...

    def add_artifact_ref(
        self,
        run_id: str,
        dispatch_id: str | None,
        *,
        name: str,
        media_type: str,
        url_or_path: str,
    ) -> None: ...
