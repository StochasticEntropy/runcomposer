"""The ports (DESIGN.md §6): the protocols a connector implements, and the
context runcomposer hands to a runner.

Five roles — ``TestSource``, ``Runner``, ``ResultParser``, ``RunStore``,
``RunObserver`` — plus ``DispatchContext``, which is the other direction:
everything a runner may ask of runcomposer while it fulfills one dispatch.
A runner receives nothing else and reaches for nothing else; there are no
hooks discovered by attribute.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, Protocol

from .model import (
    ArtifactRef,
    DispatchRecord,
    Item,
    RunEvent,
    RunPage,
    RunRecord,
    Verdict,
)
from .parameters import Parameter

__all__ = [
    "DispatchContext",
    "Health",
    "History",
    "LiveChannel",
    "ParsedVerdict",
    "ResultParser",
    "RunObserver",
    "RunStore",
    "Runner",
    "RunnerBase",
    "RunnerInfo",
    "TestSource",
]


# -- value types ---------------------------------------------------------------


@dataclass(frozen=True)
class RunnerInfo:
    """What a runner says about itself.

    ``id`` names the plugin; the id a deployment dispatches to is the key the
    runner is configured under (one plugin may be configured several times).
    ``capabilities`` are flags a client may branch on — the ones with meaning
    to runcomposer are ``live_status``, ``partitions``, ``cancel``,
    ``external_delivery`` (results arrive by ingestion after ``dispatch``
    returns) and ``simulated`` (no test is really executed). ``parameters``
    are the per-run options the runner accepts from whoever starts a run.
    ``result_format`` is the ResultParser format id of what it produces, when
    that is fixed.
    """

    id: str
    title: str = ""
    description: str = ""
    capabilities: tuple[str, ...] = ()
    parameters: tuple[Parameter, ...] = ()
    result_format: str | None = None


@dataclass(frozen=True)
class Health:
    """A runner's answer to "could you take a dispatch right now?".
    ``status`` is ``ok``, ``degraded``, ``down`` or ``unknown``; ``data`` is
    runner-defined and displayed verbatim."""

    status: str = "unknown"
    detail: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedVerdict:
    """A ResultParser's raw output: the *native* name as the artifact spells
    it. Correlation to an Item id always goes through ``TestSource.resolve``
    — the core never string-compares native names (DESIGN.md §5)."""

    native_name: str
    status: str
    duration_ms: int = 0
    message: str = ""
    attempt: int = 1


# -- what runcomposer gives a runner ---------------------------------------------


class History(Protocol):
    """Read access to what earlier runs established."""

    def durations(
        self, *, labels: Mapping[str, str] | None = None, last_n: int = 5
    ) -> dict[str, float]:
        """Mean duration in milliseconds per item id, over the last ``last_n``
        dispatches that delivered, of runs carrying all of ``labels``. Empty
        on a fresh store."""
        ...


class LiveChannel(Protocol):
    """A picklable handle for streaming live status out of a worker process.
    Works with any RunStore: it carries no store, only a place to write that
    the dispatching process reads."""

    def live(
        self, shard: str, item_id: str, status: str, *, duration_ms: int = 0, message: str = ""
    ) -> None: ...


class DispatchContext(Protocol):
    """Everything a runner gets for one dispatch, and everything it may do.

    Identity and input are attributes; the rest are calls. The contract in
    one sentence: *prepare, then* ``declare`` *exactly once, then* ``deliver``
    *each declared shard* — refusing is only possible before ``declare``, and
    whatever is raised after it is a failed execution whose partial results
    are kept.
    """

    #: The run and the dispatch this context belongs to. ``dispatch_id`` is
    #: minted by runcomposer; use it wherever the executor needs one.
    run_id: str
    dispatch_id: str
    #: The key this runner is configured under.
    runner_id: str
    #: The run spec document, read-only.
    spec: Mapping[str, Any]
    #: ``selection.materialized.item_ids`` — the authoritative executed set.
    item_ids: tuple[str, ...]
    #: Configured options overlaid by the validated per-run parameters. A
    #: document cannot override anything the runner did not declare as a
    #: parameter.
    options: Mapping[str, Any]
    #: The validated per-run parameters on their own.
    parameters: Mapping[str, Any]
    #: The links given to ``declare`` (empty before it) — what ``cancel``
    #: finds its executor by.
    links: Mapping[str, str]
    #: A directory for this dispatch under the artifact root, already created.
    #: Files written here are servable as artifacts.
    workdir: Path
    #: The catalog as it is *now* — for the drift check and name resolution.
    source: TestSource
    history: History

    def refuse(self, reason: str) -> NoReturn:
        """Decline the dispatch (raises ``DispatchRefused``). Legal only
        before ``declare``; leaves no dispatch behind."""
        ...

    def declare(
        self,
        shards: Sequence[str] | Mapping[str, Sequence[str]],
        *,
        plan: str = "",
        links: Mapping[str, str] | None = None,
        spec_sha256: str | None = None,
    ) -> None:
        """THE hand-off. Records the shard set — names, or names mapped to
        the item ids each shard will report — moves the run to RUNNING and
        stores the plan text and links. Exactly once per dispatch.
        ``spec_sha256`` records the hash of document bytes the runner
        serialized and handed out itself (``export_spec`` does it for you)."""
        ...

    def live(
        self, shard: str, item_id: str, status: str, *, duration_ms: int = 0, message: str = ""
    ) -> None:
        """Stream one item's status while the shard is still executing."""
        ...

    def live_channel(self) -> LiveChannel:
        """The same call as a picklable object for worker processes."""
        ...

    def parse(self, bundle: str | Path, *, format: str | None = None) -> list[Verdict]:
        """Parse native result artifacts with the configured ResultParser for
        ``format`` (default: what the spec expects) and correlate every native
        name through ``TestSource.resolve``. Names the catalog does not know
        are dropped with a warning event. For runners that need to relabel
        verdicts (attempt numbers) before delivering them."""
        ...

    def deliver(
        self,
        shard: str,
        *,
        verdicts: Sequence[Verdict] | None = None,
        bundle: str | Path | None = None,
        format: str | None = None,
    ) -> str:
        """Deliver one shard: correlated verdicts, or a path to native
        artifacts that is parsed as by ``parse``. Idempotent by content hash;
        returns ``new``, ``replaced`` or ``duplicate``. Completion is
        recomputed after every delivery."""
        ...

    def add_artifact(
        self,
        name: str,
        path_or_url: str | Path,
        *,
        media_type: str | None = None,
        shard: str | None = None,
    ) -> None: ...

    def log(self, message: str, level: str = "info") -> None:
        """Append a line to the run's event stream."""
        ...

    def export_spec(self) -> bytes:
        """The spec document as the exact JSON bytes to hand to an external
        executor. Their hash is recorded on the dispatch, so a marker written
        from them verifies on the way back (DESIGN.md §5)."""
        ...

    def ingest_token(self) -> str | None:
        """Mint the token an external executor needs to push results for this
        dispatch (``None`` when the deployment disabled tokens). Only its
        hash is stored; hand it to the executor and nowhere else."""
        ...

    @property
    def cancelled(self) -> bool: ...

    def raise_if_cancelled(self) -> None:
        """Raise ``DispatchCancelled`` once cancellation was requested."""
        ...


# -- the five roles ---------------------------------------------------------------


class TestSource(Protocol):
    """Enumerates Items, produces a content-hashed catalog snapshot, and owns
    native-name→id resolution (DESIGN.md §6.1).

    Optional members, each a real capability when present:

    - ``fingerprint() -> str`` — a cheap token that changes when the corpus on
      disk may have changed (file sizes and modification times, say). With it
      the catalog is reloaded only when needed; without it, on request.
    - ``duplicate_ids`` — ids that more than one item carries.
    """

    __test__: bool = False  # a protocol, not a test class
    provider_id: str

    def items(self) -> list[Item]: ...

    def snapshot(self) -> str: ...

    def resolve(self, native_name: str) -> str | None: ...


class Runner(Protocol):
    """Fulfills a run spec (DESIGN.md §6.2).

    One instance is built per configured runner id and may be asked to
    dispatch several runs at once: everything that belongs to one dispatch
    lives on its context, never on the runner.
    """

    def describe(self) -> RunnerInfo: ...

    def dispatch(self, ctx: DispatchContext) -> None:
        """Prepare, ``ctx.declare``, execute or hand off, ``ctx.deliver``.
        May block for as long as the execution takes — runcomposer calls it
        off the request path. Returning with shards outstanding means their
        results arrive by ingestion."""
        ...

    def cancel(self, ctx: DispatchContext) -> None:
        """Stop the execution behind ``ctx``. Called from another thread while
        ``dispatch`` may still be running, and — for a dispatch whose results
        are awaited — with a context rebuilt from the stored record. Declared
        by the ``cancel`` capability."""
        ...

    def health(self) -> Health: ...


class RunnerBase:
    """Defaults for the optional half of :class:`Runner`. Subclassing is a
    convenience, not a requirement."""

    def describe(self) -> RunnerInfo:
        raise NotImplementedError

    def dispatch(self, ctx: DispatchContext) -> None:
        raise NotImplementedError

    def cancel(self, ctx: DispatchContext) -> None:
        """Nothing to stop beyond what ``ctx.cancelled`` already signals."""

    def health(self) -> Health:
        return Health()


class ResultParser(Protocol):
    """Turns native result artifacts into ParsedVerdicts (§5). Bad input is
    a ``ParseError`` and nothing else; XML is read through the defused helper
    (``forbid_dtd`` / ``parse_xml``)."""

    format_id: str

    def parse(self, path: Any) -> list[ParsedVerdict]: ...


class RunObserver(Protocol):
    """Told about every lifecycle event of every run (DESIGN.md §6.5). An
    exception raised here is recorded as a warning and affects nothing."""

    def notify(self, event: RunEvent) -> None: ...


class RunStore(Protocol):
    """Persistence port (§6.3): runs, specs, dispatches, deliveries,
    verdicts, live status, artifact references and the event stream.

    The store records; it decides nothing. Lifecycle state is computed by
    ``core.lifecycle`` and written through ``update_dispatch`` /
    ``set_run_headline``. Every method must be safe to call from several
    threads, and ``record_delivery`` must be atomic across processes.
    """

    store_id: str

    # -- runs ---------------------------------------------------------------

    def create_run(self, spec: Mapping[str, Any], *, origin: str) -> RunRecord:
        """Store the document as given and a run record for it. The record's
        ``created_at`` is the document's, normalized to UTC (``normalize_time``)
        — times are filtered, ordered and pruned as stored."""
        ...

    def get_run(self, run_id: str) -> RunRecord | None:
        """The run with all its dispatches and deliveries, oldest first."""
        ...

    def list_runs(
        self,
        *,
        state: str | None = None,
        labels: Mapping[str, str] | None = None,
        since: str | None = None,
        until: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> RunPage:
        """Newest first. ``q`` is a case-insensitive substring of the title;
        ``labels`` must all match. Each run carries its dispatches (not its
        deliveries); ``total`` counts the whole filtered set."""
        ...

    def delete_run(self, run_id: str) -> bool:
        """Remove a run and everything recorded for it."""
        ...

    def prune_runs(self, *, before: str | None = None, max_runs: int | None = None) -> list[str]: ...

    def get_spec_document(self, run_id: str) -> dict[str, Any] | None: ...

    def set_spec_document(self, run_id: str, spec: Mapping[str, Any]) -> None: ...

    def set_run_headline(
        self, run_id: str, state: str, *, completion: str | None, completed_at: str | None
    ) -> None: ...

    def latest_completed_run(
        self,
        *,
        completed_before: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> RunRecord | None:
        """§7: 'latest' means latest COMPLETE by ``completed_at``, optionally
        bounded and scoped to runs carrying all of ``labels``."""
        ...

    # -- dispatches -----------------------------------------------------------

    def add_dispatch(
        self,
        run_id: str,
        *,
        dispatch_id: str,
        mode: str,
        state: str,
        owner: str | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> DispatchRecord: ...

    def update_dispatch(self, dispatch_id: str, **changes: Any) -> DispatchRecord:
        """Set any of: ``state``, ``shards``, ``shard_items``, ``spec_sha256``,
        ``error``, ``plan``, ``links``, ``completion``, ``completed_at``,
        ``summary``, ``owner``, ``cancel_requested``."""
        ...

    def get_dispatch(self, dispatch_id: str) -> DispatchRecord | None: ...

    def delete_dispatch(self, dispatch_id: str) -> None:
        """Remove a dispatch that never handed anything off."""
        ...

    def owned_dispatches(self) -> list[DispatchRecord]:
        """Every dispatch that still names an owner — the restart-recovery
        question."""
        ...

    def set_dispatch_spec(self, dispatch_id: str, content: bytes, media_type: str) -> None:
        """Keep the exact document bytes handed out for this dispatch."""
        ...

    def get_dispatch_spec(self, dispatch_id: str) -> tuple[bytes, str] | None: ...

    # -- ingest tokens (§5): hashes only ---------------------------------------

    def set_ingest_token(self, run_id: str, dispatch_id: str | None, token_sha256: str) -> None:
        """Bind a token hash to a dispatch (replacing that dispatch's previous
        one) or, with ``dispatch_id=None``, to the run as a whole."""
        ...

    def ingest_tokens(self, run_id: str) -> dict[str, str | None]:
        """``{token_sha256: dispatch_id or None}`` for the run."""
        ...

    # -- deliveries and verdicts ------------------------------------------------

    def record_delivery(
        self,
        run_id: str,
        *,
        dispatch_id: str,
        shard: str,
        content_hash: str,
        format: str,
        verdicts: Sequence[Verdict],
    ) -> str:
        """The §5 idempotency contract, keyed on (run, dispatch, shard): the
        same content hash again is a no-op (``duplicate``); different content
        for a shard that already delivered replaces it (``replaced``);
        otherwise ``new``. Atomic: concurrent deliveries to one shard leave
        exactly one. Clears the shard's live status."""
        ...

    def delivered_shards(self, run_id: str, dispatch_id: str) -> set[str]: ...

    def verdicts_for(
        self, run_id: str, dispatch_id: str | None = None, *, shard: str | None = None
    ) -> list[Verdict]:
        """Delivered verdicts, each carrying its ``shard`` and ``attempt``."""
        ...

    def record_live_verdict(
        self, run_id: str, dispatch_id: str, shard: str, verdict: Verdict
    ) -> None:
        """Status streamed while a shard executes — kept apart from delivered
        verdicts, replaced per (shard, item), cleared by the shard's
        delivery."""
        ...

    def live_verdicts(self, run_id: str, dispatch_id: str) -> list[Verdict]: ...

    def duration_aggregates(
        self, *, labels: Mapping[str, str] | None = None, last_n: int = 5
    ) -> dict[str, float]: ...

    # -- artifacts ----------------------------------------------------------------

    def add_artifact_ref(
        self,
        run_id: str,
        dispatch_id: str | None,
        *,
        name: str,
        media_type: str,
        url_or_path: str,
        shard: str | None = None,
    ) -> None: ...

    def artifact_refs(self, run_id: str, dispatch_id: str | None = None) -> list[ArtifactRef]: ...

    # -- events -------------------------------------------------------------------

    def append_event(
        self,
        run_id: str,
        *,
        kind: str,
        at: str,
        dispatch_id: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> RunEvent: ...

    def events(self, run_id: str, *, after: int = 0, limit: int = 500) -> list[RunEvent]:
        """Events with ``seq > after``, oldest first."""
        ...

    def close(self) -> None: ...
