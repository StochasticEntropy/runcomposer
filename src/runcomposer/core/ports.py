"""The ports (DESIGN.md §6): the protocols a connector implements, and the
context runcomposer hands to a runner.

Five roles — ``TestSource``, ``Runner``, ``ResultParser``, ``RunStore``,
``RunObserver`` — plus ``DispatchContext``, which is the other direction:
everything a runner may ask of runcomposer while it fulfills one dispatch.
A runner receives nothing else and reaches for nothing else; there are no
hooks discovered by attribute.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, NoReturn, Protocol

from .model import (
    ArtifactRef,
    DispatchRecord,
    Item,
    LiveStatus,
    RunEvent,
    RunPage,
    RunRecord,
    Verdict,
)
from .parameters import Parameter

__all__ = [
    "Configurable",
    "DispatchContext",
    "Health",
    "History",
    "LiveChannel",
    "ObserverBase",
    "ParsedVerdict",
    "ParserBase",
    "PlanContext",
    "PlanPreview",
    "PluginBase",
    "ResultParser",
    "RunObserver",
    "RunStore",
    "Runner",
    "RunnerBase",
    "RunnerInfo",
    "SourceBase",
    "StoreBase",
    "TestSource",
    "contract_members",
    "missing_members",
]


# -- value types ---------------------------------------------------------------


@dataclass(frozen=True)
class RunnerInfo:
    """What a runner says about itself.

    ``id`` names the plugin; the id a deployment dispatches to is the key the
    runner is configured under (one plugin may be configured several times).
    ``capabilities`` are flags a client may branch on — the ones with meaning
    to runcomposer are ``live_status``, ``partitions``, ``cancel``, ``plan``
    (``Runner.plan`` answers with a real preview), ``external_delivery``
    (results arrive by ingestion after ``dispatch`` returns) and ``simulated``
    (no test is really executed). ``parameters``
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


@dataclass(frozen=True)
class PlanPreview:
    """A runner's answer to "what would this dispatch do?" (``Runner.plan``).

    ``text`` is the plan as people read it — the same text a dispatch records.
    ``shards`` maps every shard the dispatch would declare to the item ids it
    would report (an empty list when the runner does not decide that itself:
    a remote job that distributes on its own). ``groups`` labels shards that
    belong together, as ``declare(groups=…)`` does; ``estimates_ms`` gives a
    duration estimate per shard where history allows one; ``notes`` are
    things worth knowing before starting (what a preview cannot check, a
    cold start); ``data`` is runner-defined and shown verbatim.
    """

    text: str = ""
    shards: Mapping[str, Sequence[str]] = field(default_factory=dict)
    groups: Mapping[str, str] = field(default_factory=dict)
    estimates_ms: Mapping[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    data: Mapping[str, Any] = field(default_factory=dict)


# -- what runcomposer gives a runner ---------------------------------------------


class History(Protocol):
    """Read access to what earlier runs established."""

    def durations(
        self,
        *,
        labels: Mapping[str, str] | None = None,
        last_n: int = 5,
        statuses: Sequence[str] | None = None,
    ) -> dict[str, float]:
        """Mean duration in milliseconds per item id, over the last ``last_n``
        dispatches that delivered, of runs carrying all of ``labels``.
        ``statuses`` restricts the mean to executions that ended with one of
        them (``("PASS",)``: a test that fails usually stops short of its normal
        length and would skew the balance). Executions that reported 0 ms are not counted — 0 means
        "no duration known". An item without any measured duration falls back
        to an imported one (``runcomposer history import``) when there is one.
        Empty on a fresh store."""
        ...


class LiveChannel(Protocol):
    """A picklable handle for streaming out of a worker process: live status
    per item, and log lines for the run's event stream. Works with any
    RunStore: it carries no store, only a place to write that the dispatching
    process reads."""

    def live(
        self, shard: str, item_id: str, status: str, *, duration_ms: int = 0, message: str = ""
    ) -> None: ...

    def log(self, message: str, level: str = "info") -> None:
        """The worker-side counterpart of ``DispatchContext.log``."""
        ...


class PlanContext(Protocol):
    """What a runner gets to *preview* a dispatch (``Runner.plan``): the
    input of a dispatch and nothing to act with. There is no ``declare``, no
    ``deliver``, no artifact, no token and no work directory here — a preview
    records nothing and executes nothing.

    Every member is also a member of :class:`DispatchContext`, with the same
    meaning, so one planning function serves ``plan`` and ``dispatch``.
    """

    run_id: str
    runner_id: str
    spec: Mapping[str, Any]
    item_ids: tuple[str, ...]
    options: Mapping[str, Any]
    parameters: Mapping[str, Any]
    source: TestSource
    history: History

    def connector_settings(self, name: str) -> Mapping[str, Any]:
        """The deployment's ``connectors.<name>`` mapping (``${env:…}``
        resolved), read-only; empty when the deployment has none. It is how
        a callable named in a runner's configuration — a hook, a provider —
        reads the settings of the connector package it belongs to."""
        ...

    def refuse(self, reason: str) -> NoReturn:
        """Say that this dispatch would be refused, and why."""
        ...

    def export_spec(self) -> bytes:
        """The document as the bytes a dispatch would hand out. In a preview
        nothing is recorded for them."""
        ...


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
    #: The options of the runner's config entry, as its constructor received
    #: them — and nothing else. A per-run value never appears here, so a
    #: parameter cannot shadow an option of the same name.
    options: Mapping[str, Any]
    #: The validated per-run parameters (defaults applied): what whoever
    #: started the run chose. A document cannot set anything the runner did
    #: not declare as a parameter.
    parameters: Mapping[str, Any]
    #: The links given to ``declare`` and set with ``set_link`` — what
    #: ``cancel`` finds its executor by.
    links: Mapping[str, str]
    #: A directory for this dispatch under the artifact root, already created.
    #: A file written here is served only once it was recorded with
    #: ``add_artifact``.
    workdir: Path
    #: A directory for this dispatch that is never served, whatever is
    #: recorded: for files that must not be downloadable. Created on first
    #: use, removed with the run.
    private_dir: Path
    #: The catalog as it is *now* — for the drift check and name resolution.
    source: TestSource
    history: History

    def connector_settings(self, name: str) -> Mapping[str, Any]:
        """The deployment's ``connectors.<name>`` mapping (``${env:…}``
        resolved), read-only; empty when the deployment has none. It is how
        a callable named in a runner's configuration — a hook, a provider —
        reads the settings of the connector package it belongs to."""
        ...

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
        groups: Mapping[str, str] | None = None,
    ) -> None:
        """THE hand-off. Records the shard set — names, or names mapped to
        the item ids each shard will report — moves the run to RUNNING and
        stores the plan text and links. Exactly once per dispatch.
        ``groups`` labels shards that belong together (``{shard: label}`` —
        the partition each chunk runs on): the run detail then has a roll-up for
        every group next to the one for every shard. ``spec_sha256`` records the hash of document bytes
        the runner serialized and handed out itself (``export_spec`` does it
        for you)."""
        ...

    def set_link(self, name: str, url: str) -> None:
        """Record (or replace) one named link of the dispatch — for what is
        only known after the hand-off, such as the URL of the build a queued
        job became. Stored like the links given to ``declare``: a context
        rebuilt for ``cancel`` carries it."""
        ...

    def live(
        self, shard: str, item_id: str, status: str, *, duration_ms: int = 0, message: str = ""
    ) -> None:
        """Stream one item's status while the shard is still executing:
        ``RUNNING`` when it starts, then how it ended. Best-effort — an item
        that was not planned or a status outside ``LIVE_STATUSES`` is
        ignored."""
        ...

    def live_channel(self) -> LiveChannel:
        """``live`` and ``log`` as a picklable object for worker processes."""
        ...

    def delivered_shards(self) -> set[str]:
        """The shards of this dispatch that have delivered — by this runner
        or by a push from outside."""
        ...

    def outstanding_shards(self) -> list[str]:
        """Declared shards nothing was delivered for yet, in declaration
        order. Empty once the dispatch is complete — which, when an executor
        pushes results itself, may be the case while ``dispatch`` is still
        running."""
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
        group: str | None = None,
    ) -> None:
        """Record a file, a directory or an ``http(s)`` link as an artifact
        of the dispatch — optionally of one ``shard``, or of a ``group`` of
        shards. Recording is what makes a local file downloadable: only
        recorded files (and files under a recorded directory) inside the
        artifact root are served."""
        ...

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


# -- how a plugin class describes its options ---------------------------------------


class Configurable(Protocol):
    """What every plugin *class* tells the config loader (DESIGN.md §8).

    - ``OPTIONS`` — the option names an entry may carry: ``None`` (the
      default) means "the constructor's keyword parameters", a tuple means
      exactly these. Either way an unknown key is a ``ConfigError`` at
      startup naming the accepted ones.
    - ``resolve_config_paths(options, resolve)`` — anchor the options that
      are file paths to the config file's directory; ``resolve`` does it for
      one value (or a list). The default (:class:`PluginBase`) resolves the
      names listed in ``PATH_OPTIONS``; override it when the rule has
      exceptions (a store whose ``:memory:`` names no file).

    - ``CONNECTOR`` — ``None`` (the default), or the name of a mapping under
      the config file's ``connectors:`` section. When set, the plugin is
      built with that mapping as the keyword argument ``connector_settings``
      (``${env:…}`` references resolved): settings a connector package's
      plugins share, written once.

    :class:`PluginBase` supplies all three; every base class below derives
    from it.
    """

    OPTIONS: ClassVar[tuple[str, ...] | None]
    CONNECTOR: ClassVar[str | None]

    @classmethod
    def resolve_config_paths(
        cls, options: dict[str, Any], resolve: Callable[[Any], Any]
    ) -> dict[str, Any]: ...


class PluginBase:
    """Defaults for :class:`Configurable`: options are the constructor's
    keyword parameters, the names in ``PATH_OPTIONS`` (single values or
    lists) are paths relative to the config file, and no ``connectors:``
    mapping is handed over."""

    OPTIONS: ClassVar[tuple[str, ...] | None] = None
    PATH_OPTIONS: ClassVar[tuple[str, ...]] = ()
    CONNECTOR: ClassVar[str | None] = None

    @classmethod
    def resolve_config_paths(
        cls, options: dict[str, Any], resolve: Callable[[Any], Any]
    ) -> dict[str, Any]:
        for name in cls.PATH_OPTIONS:
            if name in options:
                options[name] = resolve(options[name])
        return options


# -- the five roles ---------------------------------------------------------------


class TestSource(Protocol):
    """Enumerates Items, produces a content-hashed catalog snapshot, and owns
    native-name→id resolution (DESIGN.md §6.1).

    ``fingerprint`` and ``duplicate_ids`` have defaults in
    :class:`SourceBase`; a source that does not derive from it must provide
    them itself.
    """

    __test__: bool = False  # a protocol, not a test class
    provider_id: str
    #: Ids that more than one item carries (reported, never raised).
    duplicate_ids: Sequence[str]

    def items(self) -> list[Item]: ...

    def snapshot(self) -> str: ...

    def resolve(self, native_name: str) -> str | None: ...

    def fingerprint(self) -> str | None:
        """A cheap token that changes when the corpus on disk may have
        changed (file sizes and modification times, say) — no parsing. With
        one, the catalog is reloaded exactly when it changes; ``None`` means
        "cannot tell": the source is rebuilt on request and before every
        dispatch."""
        ...


class SourceBase(PluginBase):
    """Defaults for the optional half of :class:`TestSource`."""

    provider_id: str = ""
    duplicate_ids: Sequence[str] = ()

    def items(self) -> list[Item]:
        raise NotImplementedError

    def snapshot(self) -> str:
        raise NotImplementedError

    def resolve(self, native_name: str) -> str | None:
        raise NotImplementedError

    def fingerprint(self) -> str | None:
        """No cheap probe: rebuilt on request and before every dispatch."""
        return None


class Runner(Protocol):
    """Fulfills a run spec (DESIGN.md §6.2).

    One instance is built per configured runner id and may be asked to
    dispatch several runs at once: everything that belongs to one dispatch
    lives on its context, never on the runner. ``plan``, ``cancel`` and
    ``health`` have defaults in :class:`RunnerBase`.
    """

    def describe(self) -> RunnerInfo: ...

    def plan(self, ctx: PlanContext) -> PlanPreview:
        """The plan a dispatch of ``ctx`` would follow — every item with the
        shard it would land in — and nothing else happens: no execution, no
        record, no trigger. May
        ``ctx.refuse`` for the reasons ``dispatch`` would. Declared by the
        ``plan`` capability."""
        ...

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


class RunnerBase(PluginBase):
    """Defaults for the optional half of :class:`Runner`."""

    def describe(self) -> RunnerInfo:
        raise NotImplementedError

    def plan(self, ctx: PlanContext) -> PlanPreview:
        """No preview of its own: says so. A runner that can tell overrides
        this and declares the ``plan`` capability."""
        return PlanPreview(
            text=f"runner {ctx.runner_id!r} offers no plan preview — "
            f"{len(ctx.item_ids)} item(s) would be handed to it"
        )

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


class ParserBase(PluginBase):
    """Base class for a :class:`ResultParser` (configuration defaults)."""

    format_id: str = ""

    def parse(self, path: Any) -> list[ParsedVerdict]:
        raise NotImplementedError


class RunObserver(Protocol):
    """Told about every lifecycle event of every run (DESIGN.md §6.5). An
    exception raised here is recorded as a warning and affects nothing."""

    def notify(self, event: RunEvent) -> None: ...


class ObserverBase(PluginBase):
    """Base class for a :class:`RunObserver` (configuration defaults)."""

    def notify(self, event: RunEvent) -> None:
        raise NotImplementedError


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
        exclusive: bool = False,
    ) -> DispatchRecord:
        """Record a new dispatch. With ``exclusive``, refuse with
        ``RunActive`` when the run's latest dispatch is in one of
        ``ACTIVE_STATES`` — checked in the same transaction as the insert, so
        two processes cannot both open a dispatch for one idle run."""
        ...

    def update_dispatch(self, dispatch_id: str, **changes: Any) -> DispatchRecord:
        """Set any of: ``state``, ``shards``, ``shard_items``, ``shard_groups``,
        ``spec_sha256``, ``error``, ``plan``, ``links``, ``completion``,
        ``completed_at``, ``summary``, ``owner``, ``cancel_requested``,
        ``error_code``, ``error_owner``."""
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
        self, run_id: str, dispatch_id: str, shard: str, verdict: LiveStatus
    ) -> None:
        """Status streamed while a shard executes (``RUNNING`` included) —
        kept apart from delivered verdicts, replaced per (shard, item),
        cleared by the shard's delivery."""
        ...

    def live_verdicts(self, run_id: str, dispatch_id: str) -> list[LiveStatus]: ...

    def duration_aggregates(
        self,
        *,
        labels: Mapping[str, str] | None = None,
        last_n: int = 5,
        statuses: Sequence[str] | None = None,
    ) -> dict[str, float]:
        """Mean duration per item id over the last ``last_n`` dispatches that
        delivered, of runs carrying all of ``labels`` — over verdicts with a
        duration above 0 and, with ``statuses``, one of those statuses. An
        item nothing was measured for falls back to its seeded duration."""
        ...

    def seed_durations(
        self, durations: Mapping[str, float], *, labels: Mapping[str, str] | None = None
    ) -> int:
        """Keep imported durations (milliseconds per item id) as the
        fallback ``duration_aggregates`` answers with for items it has no
        measurement for. ``labels`` scope a seed like a run's labels scope
        its history: it answers queries selecting a subset of them. Importing
        an item again under the same labels replaces its seed. Returns how
        many were stored."""
        ...

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
        group: str | None = None,
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

    def latest_events(self, run_id: str, *, limit: int = 200) -> list[RunEvent]:
        """The last ``limit`` events of the run, oldest first — a bounded
        read, whatever the length of the stream."""
        ...

    # -- atomicity ------------------------------------------------------------------

    def atomic(self) -> AbstractContextManager[None]:
        """A block in which every call this thread makes to the store is one
        atomic unit: no other writer — another thread, or another process
        sharing the store — writes between its reads and its writes, and an
        exception undoes what the block wrote. Nested blocks join the
        outermost. The ledger decides every lifecycle change inside one, so
        that a decision is never taken on facts another process has since
        changed. (The default in :class:`StoreBase` guarantees nothing: a
        store shared between processes provides its own.)"""
        ...

    def close(self) -> None:
        """Release what the store holds (default in :class:`StoreBase`)."""
        ...


class StoreBase(PluginBase):
    """Base class for a :class:`RunStore`: configuration defaults, an
    ``atomic`` that guarantees nothing beyond what each call does, and a
    ``close`` that has nothing to release. Every other member is the store's
    own."""

    store_id: str = ""

    def atomic(self) -> AbstractContextManager[None]:
        """No unit of its own: each call is as atomic as the store makes it."""
        return nullcontext()

    def close(self) -> None:
        """Nothing to release."""


# -- the contract, as data ---------------------------------------------------------


def contract_members(protocol: type) -> tuple[str, ...]:
    """The members a protocol declares — methods, properties and annotated
    attributes, in declaration order, dunders excluded. This is the list the
    config loader checks a plugin against at startup and the conformance
    kit checks a connector against; there is no other definition."""
    names: list[str] = []
    for name, member in vars(protocol).items():
        if name.startswith("_"):
            continue
        if inspect.isfunction(member) or isinstance(member, (property, classmethod, staticmethod)):
            names.append(name)
    for name in inspect.get_annotations(protocol):
        if not name.startswith("_") and name not in names:
            names.append(name)
    return tuple(names)


def missing_members(plugin: Any, protocol: type) -> list[str]:
    """The members of ``protocol`` that ``plugin`` (an instance, or a class
    for :class:`Configurable`) does not provide. Presence only — whether a
    member *behaves* is what the conformance kit's checks are for."""
    missing = []
    for name in contract_members(protocol):
        try:
            inspect.getattr_static(plugin, name)
        except AttributeError:
            missing.append(name)
    return missing

