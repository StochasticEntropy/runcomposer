"""In-process dispatch (DESIGN.md §4, §6.2): the dispatch manager and the
``DispatchContext`` it hands to a runner.

The manager owns the part of the lifecycle that happens while a runner's
``dispatch()`` executes — on a bounded pool of threads for the server, in the
calling thread for the CLI; it is the same code either way:

```
start()      validate → record the dispatch (DISPATCHED) → run the runner
  runner       ctx.refuse(...)      → no dispatch remains; reason in the events
               ctx.declare(shards)  → RUNNING           (the hand-off)
               ctx.deliver(shard)   → completion recomputed
  returns      all shards in → COMPLETE, otherwise AWAITING_RESULTS
  raises       before declare → as a refusal; after → FAILED, results kept
               InvalidParameter before declare → the request's 400; after → FAILED
cancel()     flag + runner.cancel(ctx) → CANCELLED when the runner comes back
```

A dispatch that leaves no record (refused, invalid, raised or returned
before the hand-off) leaves no files either: the work and private
directories this attempt created are removed.

The context is safe to use from several threads at once — a runner's
workers deliver, log, add artifacts, stream live status and set links
concurrently; ``declare`` and ``set_link`` are serialized with each other, so
a link set while another thread declares is part of the hand-off or stored
after it, never lost. What stays the runner's to order: one shard is
delivered by one thread (two deliveries of one shard are two deliveries, the
later one replacing the earlier), and ``declare`` happens before the
deliveries it announces.

A dispatch names its owner — this process — for as long as the runner is
executing. That is what lets a restarted server tell an interrupted dispatch
from one that is merely waiting for results (``app.recovery``; who an owner
is and whether it is alive: ``app.liveness``).
"""

from __future__ import annotations

import copy
import json
import logging
import mimetypes
import threading
import time
import traceback
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NoReturn

from runcomposer.core import events as kinds
from runcomposer.core.errors import (
    Conflict,
    DispatchCancelled,
    DispatchQueueFull,
    DispatchRefused,
    InvalidParameter,
    InvalidRequest,
    InvalidTransition,
    RunActive,
    UnknownRun,
)
from runcomposer.core.handoff import (
    HandoffOrder,
    check_delivery_args,
    check_reportable,
    normalize_groups,
    normalize_shards,
)
from runcomposer.core.hashing import bundle_hash, sha256_hex
from runcomposer.core.lifecycle import ACTIVE_STATES, outstanding_shards
from runcomposer.core.marker import read_marker
from runcomposer.core.model import LIVE_STATUSES, DispatchRecord, LiveStatus, Verdict
from runcomposer.core.parameters import validate_parameters
from runcomposer.core.ports import PlanPreview, RunnerInfo

from .ingestion import expected_format
from .livechannel import FileLiveChannel, LiveTail
from .liveness import LIVE_OWNERS, owner_alive, owner_identity

if TYPE_CHECKING:
    from .service import Service

__all__ = [
    "DispatchManager",
    "Outcome",
    "PlanOutcome",
    "spec_bytes",
]

log = logging.getLogger("runcomposer.dispatch")

_LIVE_DIR = ".live"
#: How often the store is asked whether another process requested a cancel.
_CANCEL_POLL_S = 0.25
#: How long settling a cancelled dispatch waits for the runner's cancel().
_CANCEL_WAIT_S = 60.0


def spec_bytes(document: Mapping[str, Any]) -> bytes:
    """A spec document as the exact bytes a dispatch hands out."""
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _redacted_invalid(service: Service, error: InvalidParameter) -> InvalidParameter:
    """A runner's ``InvalidParameter`` as it is answered: the same parameter,
    the message with the deployment's secrets blanked out (a runner may
    quote an option)."""
    return InvalidParameter(
        str(service.redact(str(error))), parameter=error.parameter,
        problem=error.problem, key=error.key,
    )


class _History:
    def __init__(self, store: Any):
        self._store = store

    def durations(
        self,
        *,
        labels: Mapping[str, str] | None = None,
        last_n: int = 5,
        statuses: Sequence[str] | None = None,
    ) -> dict[str, float]:
        return dict(
            self._store.duration_aggregates(labels=labels, last_n=last_n, statuses=statuses)
        )


class _PlanContext:
    """The real ``PlanContext``: the input of a dispatch, nothing to act
    with. It has no store to write to — a preview cannot record anything."""

    def __init__(
        self,
        service: Service,
        *,
        runner_id: str,
        spec: Mapping[str, Any],
        options: Mapping[str, Any],
        parameters: Mapping[str, Any],
        fresh_source: bool = True,
    ):
        self._document = copy.deepcopy(dict(spec))
        self.spec: Mapping[str, Any] = MappingProxyType(self._document)
        self.run_id = str((self._document.get("run") or {}).get("id") or "")
        self.runner_id = runner_id
        materialized = (self._document.get("selection") or {}).get("materialized") or {}
        self.item_ids: tuple[str, ...] = tuple(materialized.get("item_ids") or ())
        self.options: Mapping[str, Any] = MappingProxyType(dict(options))
        self.parameters: Mapping[str, Any] = MappingProxyType(dict(parameters))
        self.history = _History(service.store)
        self._service = service
        self._fresh = fresh_source
        self._source: Any = None

    @property
    def source(self) -> Any:
        if self._source is None:
            self._source = self._service.catalog.fresh() if self._fresh else self._service.catalog
        return self._source

    def connector_settings(self, name: str) -> Mapping[str, Any]:
        return self._service.connector_settings(name)

    def refuse(self, reason: str) -> NoReturn:
        raise DispatchRefused(reason)

    def export_spec(self) -> bytes:
        return spec_bytes(self._document)


class PlanOutcome:
    """What a plan request established: the preview, or why the dispatch
    would be refused — never both."""

    def __init__(
        self,
        *,
        runner_id: str,
        run_id: str | None,
        item_ids: Sequence[str],
        parameters: Mapping[str, Any],
        preview: PlanPreview | None,
        refusal: str | None,
        warnings: Sequence[str] = (),
    ):
        self.runner_id = runner_id
        self.run_id = run_id
        self.item_ids = tuple(item_ids)
        self.parameters = dict(parameters)
        self.preview = preview
        self.refusal = refusal
        self.warnings = list(warnings)


class _Context:
    """The real ``DispatchContext``."""

    def __init__(
        self,
        service: Service,
        dispatch: DispatchRecord,
        *,
        spec: Mapping[str, Any],
        options: Mapping[str, Any],
        parameters: Mapping[str, Any],
    ):
        self._service = service
        self.run_id = dispatch.run_id
        self.dispatch_id = dispatch.dispatch_id
        self.runner_id = dispatch.mode
        self._document = copy.deepcopy(dict(spec))
        self.spec: Mapping[str, Any] = MappingProxyType(self._document)
        materialized = (self._document.get("selection") or {}).get("materialized") or {}
        self.item_ids: tuple[str, ...] = tuple(materialized.get("item_ids") or ())
        self._planned = frozenset(self.item_ids)
        self.parameters: Mapping[str, Any] = MappingProxyType(dict(parameters))
        # Configured options only: a per-run value never shadows an option.
        self.options: Mapping[str, Any] = MappingProxyType(dict(options))
        self.history = _History(service.store)
        self._order = HandoffOrder(dispatch.dispatch_id, declared=dispatch.state != "DISPATCHED")
        self._links: dict[str, str] = dict(dispatch.links)
        self.links: Mapping[str, str] = MappingProxyType(self._links)
        self._shards: tuple[str, ...] = dispatch.shards
        self._private_dir: Path | None = None
        self._cancel = threading.Event()
        self._workdir: Path | None = None
        self._source: Any = None
        self._spec_bytes: bytes | None = None
        self._token: str | None = None
        self._tail: LiveTail | None = None
        self._tail_thread: threading.Thread | None = None
        self._tail_stop = threading.Event()
        self._lock = threading.RLock()
        #: Serializes declare / set_link / refuse: the hand-off and the links
        #: it records are one step whichever thread calls them.
        self._handoff_lock = threading.RLock()
        #: One reader of the live files at a time — the tail thread and a
        #: delivering thread both drain them.
        self._live_lock = threading.Lock()
        #: Directories this attempt created, outermost first — what a dispatch
        #: that leaves no record removes again.
        self._created: list[Path] = []
        #: Set when the dispatch was declared, refused or settled — what a
        #: caller waiting for "did the hand-off happen" waits on.
        self.handoff = threading.Event()

    # -- input ------------------------------------------------------------------

    def _make_dir(self, root: Path) -> Path:
        """``root/<run>/<dispatch>``, created, remembering which of the two
        levels below ``root`` did not exist before."""
        run_dir = root / self.run_id
        path = run_dir / self.dispatch_id
        missing = [p for p in (run_dir, path) if not p.exists()]
        path.mkdir(parents=True, exist_ok=True)
        self._created.extend(missing)
        return path.resolve()

    @property
    def workdir(self) -> Path:
        with self._lock:
            if self._workdir is None:
                self._workdir = self._make_dir(self._service.config.artifact_dir)
            return self._workdir

    @property
    def private_dir(self) -> Path:
        with self._lock:
            if self._private_dir is None:
                self._private_dir = self._make_dir(self._service.config.private_dir)
            return self._private_dir

    def remove_created_dirs(self) -> None:
        """Remove the work and private directories of this attempt and every
        parent directory it created that is empty now — for a dispatch that
        leaves no record. Nothing an earlier dispatch made is touched."""
        import shutil

        with self._lock:
            for directory in (self._workdir, self._private_dir):
                if directory is not None:
                    shutil.rmtree(directory, ignore_errors=True)
            for created in reversed(self._created):
                try:
                    created.rmdir()
                except OSError:
                    pass  # gone already, or something else lives there now
            self._created.clear()
            self._workdir = self._private_dir = None

    @property
    def source(self) -> Any:
        if self._source is None:
            self._source = self._service.catalog.fresh()
        return self._source

    # -- the hand-off --------------------------------------------------------------

    @property
    def declared(self) -> bool:
        return self._order.declared

    def connector_settings(self, name: str) -> Mapping[str, Any]:
        return self._service.connector_settings(name)

    def refuse(self, reason: str) -> NoReturn:
        with self._handoff_lock:
            self._order.refuse(reason)

    def declare(
        self,
        shards: Sequence[str] | Mapping[str, Sequence[str]],
        *,
        plan: str = "",
        links: Mapping[str, str] | None = None,
        spec_sha256: str | None = None,
        groups: Mapping[str, str] | None = None,
    ) -> None:
        with self._handoff_lock:
            self._order.before_declare()
            names, shard_items = normalize_shards(shards)
            shard_groups = normalize_groups(groups, names)
            with self._lock:
                # Links set before the hand-off are part of it.
                self._links.update({str(k): str(v) for k, v in (links or {}).items()})
                recorded = dict(self._links)
            self._service.ledger.declare(
                self.dispatch_id, shards=names, shard_items=shard_items, plan=plan,
                links=recorded, groups=shard_groups,
            )
            if spec_sha256:
                # The runner handed out bytes it serialized itself.
                self._service.store.update_dispatch(self.dispatch_id, spec_sha256=spec_sha256)
            self._shards = tuple(names)
            self._order.declared = True
        self.handoff.set()

    def set_link(self, name: str, url: str) -> None:
        with self._handoff_lock:
            with self._lock:
                self._links[str(name)] = str(url)
                recorded = dict(self._links)
            if not self._order.declared:
                return  # recorded with the hand-off
            # Written under the lock: two links set at once are both stored,
            # never the older snapshot over the newer.
            self._service.store.update_dispatch(self.dispatch_id, links=recorded)
        self._service.bus.emit(
            self.run_id,
            kinds.LOG,
            dispatch_id=self.dispatch_id,
            data={"message": f"link {name}: {url}", "level": "info", "link": {str(name): str(url)}},
        )

    def delivered_shards(self) -> set[str]:
        return set(self._service.store.delivered_shards(self.run_id, self.dispatch_id))

    def outstanding_shards(self) -> list[str]:
        return outstanding_shards(self._shards, self.delivered_shards())

    # -- results ---------------------------------------------------------------------

    def live(
        self, shard: str, item_id: str, status: str, *, duration_ms: int = 0, message: str = ""
    ) -> None:
        if status not in LIVE_STATUSES or item_id not in self._planned:
            return  # live status is best-effort; it never invents an item
        try:
            self._service.store.record_live_verdict(
                self.run_id,
                self.dispatch_id,
                str(shard),
                LiveStatus(item_id, status, int(duration_ms), str(message), shard=str(shard)),
            )
        except Exception:
            log.warning("live status could not be recorded", exc_info=True)

    def live_channel(self) -> FileLiveChannel:
        with self._lock:
            directory = self.workdir / _LIVE_DIR
            directory.mkdir(exist_ok=True)
            if self._tail is None:
                self._tail = LiveTail(directory)
                self._tail_thread = threading.Thread(
                    target=self._follow_live, name="runcomposer-live", daemon=True
                )
                self._tail_thread.start()
            return FileLiveChannel(str(directory))

    def _follow_live(self) -> None:
        while not self._tail_stop.wait(0.2):
            self._drain_live()

    def _drain_live(self) -> None:
        if self._tail is None:
            return
        # One reader at a time, and the lock is held until what was read is
        # in the stream: a delivery drains before it records, and must not
        # overtake a line the follower thread has read but not yet emitted.
        with self._live_lock:
            for entry in self._tail.read():
                if "log" in entry:
                    self.log(str(entry["log"]), level=str(entry.get("level") or "info"))
                    continue
                try:
                    self.live(
                        str(entry["shard"]),
                        str(entry["item_id"]),
                        str(entry["status"]),
                        duration_ms=int(entry.get("duration_ms") or 0),
                        message=str(entry.get("message") or ""),
                    )
                except (KeyError, TypeError, ValueError):
                    continue

    def parse(self, bundle: str | Path, *, format: str | None = None) -> list[Verdict]:
        verdicts, warnings = self._service.ingestion.parse(
            Path(bundle), format or expected_format(self.spec)
        )
        for warning in warnings:
            self.log(warning, level="warning")
        return verdicts

    def deliver(
        self,
        shard: str,
        *,
        verdicts: Sequence[Verdict] | None = None,
        bundle: str | Path | None = None,
        format: str | None = None,
    ) -> str:
        self._order.before_deliver()
        check_delivery_args(verdicts, bundle)
        format_id = format or expected_format(self.spec)
        content_hash: str | None = None
        if bundle is not None:
            path = Path(bundle)
            self._check_marker(path)
            delivered = self.parse(path, format=format_id)
            content_hash = bundle_hash(path)
        else:
            assert verdicts is not None
            delivered = check_reportable(verdicts)
        self._drain_live()
        dispatch = self._service.store.get_dispatch(self.dispatch_id)
        if dispatch is None:
            raise RuntimeError(f"dispatch {self.dispatch_id} no longer exists")
        return self._service.ingestion.record(
            dispatch, str(shard), delivered, content_hash=content_hash, format_id=format_id
        )

    def _check_marker(self, bundle: Path) -> None:
        """A bundle fetched from elsewhere may carry a marker; if it does, it
        must be *this* dispatch's (the same §5 verification ingestion does)."""
        marker = read_marker(bundle)
        if not marker:
            return
        if marker["run_id"] != self.run_id:
            raise RuntimeError(
                f"{bundle}: marker names run {marker['run_id']!r}, not {self.run_id} — "
                "these are another run's results"
            )
        if marker.get("dispatch_id") and marker["dispatch_id"] != self.dispatch_id:
            raise RuntimeError(
                f"{bundle}: marker names dispatch {marker['dispatch_id']!r}, not {self.dispatch_id}"
            )
        handed_out = sha256_hex(self._spec_bytes) if self._spec_bytes is not None else None
        if marker.get("spec_sha256") and handed_out and marker["spec_sha256"] != handed_out:
            raise RuntimeError(
                f"{bundle}: marker spec_sha256 does not match the spec handed out for this dispatch"
            )

    def add_artifact(
        self,
        name: str,
        path_or_url: str | Path,
        *,
        media_type: str | None = None,
        shard: str | None = None,
        group: str | None = None,
    ) -> None:
        target = str(path_or_url)
        guessed = media_type or mimetypes.guess_type(target)[0] or "application/octet-stream"
        self._service.store.add_artifact_ref(
            self.run_id,
            self.dispatch_id,
            name=name,
            media_type=guessed,
            url_or_path=target,
            shard=shard,
            group=group,
        )

    def redact(self, text: str) -> str:
        return str(self._service.redact(str(text)))

    def log(self, message: str, level: str = "info") -> None:
        kind = kinds.WARNING if level in ("warning", "error") else kinds.LOG
        self._service.bus.emit(
            self.run_id,
            kind,
            dispatch_id=self.dispatch_id,
            data={"message": str(message), "level": level},
        )

    # -- for external executors ---------------------------------------------------------

    def export_spec(self) -> bytes:
        with self._lock:
            if self._spec_bytes is None:
                content = spec_bytes(self._document)
                store = self._service.store
                store.set_dispatch_spec(self.dispatch_id, content, "application/json")
                store.update_dispatch(self.dispatch_id, spec_sha256=sha256_hex(content))
                self._spec_bytes = content
            return self._spec_bytes

    def ingest_token(self) -> str | None:
        with self._lock:
            if self._token is None:
                self._token = self._service.ingestion.mint_token(self.run_id, self.dispatch_id)
            return self._token

    # -- cancellation --------------------------------------------------------------------

    @property
    def cancelled(self) -> bool:
        """Whether cancellation was signalled to this dispatch. The flag has
        exactly one writer — the dispatch manager's ``_signal`` — which also
        calls the runner's ``cancel(ctx)``: a context never finds out about a
        cancel on its own (by reading the store, say), because then a runner
        that only checks the flag could come back, and the dispatch settle as
        cancelled, without ``cancel`` ever having been called."""
        return self._cancel.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise DispatchCancelled(f"dispatch {self.dispatch_id} was cancelled")

    def request_cancel(self) -> None:
        self._cancel.set()

    def close(self) -> None:
        self._tail_stop.set()
        if self._tail_thread is not None:
            self._tail_thread.join(timeout=5)
        self._drain_live()


class Outcome:
    """How one ``start`` ended, for a caller that waited."""

    def __init__(self, dispatch_id: str, run_id: str):
        self.dispatch_id = dispatch_id
        self.run_id = run_id
        #: declared | refused | finished
        self.refusal: str | None = None
        #: The runner refused a per-run parameter before the hand-off
        #: (``InvalidParameter``): the request was invalid, no dispatch
        #: remains. Transports answer it as they answer a parameter the
        #: declaration refuses (HTTP 400 ``invalid-parameter``).
        self.invalid: InvalidParameter | None = None
        self.done = threading.Event()

    def raise_if_rejected(self) -> None:
        """Raise what the dispatch was rejected with before the hand-off:
        the runner's ``InvalidParameter``, or its refusal as
        ``DispatchRefused``. Nothing when it was not rejected."""
        if self.invalid is not None:
            raise self.invalid
        if self.refusal is not None:
            raise DispatchRefused(self.refusal)


class _Active:
    def __init__(self, ctx: _Context, runner: Any, outcome: Outcome):
        self.ctx = ctx
        self.runner = runner
        self.outcome = outcome
        self.future: Future[None] | None = None
        self.cancel_sent = False
        #: Set once the cancellation was carried out — the flag raised and
        #: the runner's ``cancel(ctx)`` back. Settling as cancelled waits for it.
        self.cancel_done = threading.Event()


class DispatchManager:
    def __init__(self, service: Service, *, max_concurrent: int = 2, max_queued: int = 100):
        self._service = service
        #: Dispatches that may wait for a free slot of the pool; one more is
        #: refused (``DispatchQueueFull``) before anything is recorded.
        self._max_queued = max(0, int(max_queued))
        self.owner = owner_identity()
        LIVE_OWNERS.add(self.owner)
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(max_concurrent)), thread_name_prefix="runcomposer-dispatch"
        )
        self._active: dict[str, _Active] = {}
        self._lock = threading.RLock()
        self._monitor: threading.Thread | None = None
        self._closed = False

    # -- starting -----------------------------------------------------------------

    def prepare(
        self,
        run_id: str,
        runner_id: str,
        parameters: Mapping[str, Any] | None = None,
        document: Mapping[str, Any] | None = None,
    ) -> tuple[Any, RunnerInfo, dict[str, Any], dict[str, Any], list[str]]:
        """Everything that can be refused before anything is written:
        ``(runner, info, spec, accepted parameters, warnings)``.

        Per-run parameters come from ``parameters`` when given; else from the
        ``runner.<id>`` section of ``document`` (a copy of the run's spec
        handed in for this dispatch) or of the stored spec."""
        service = self._service
        spec = service.store.get_spec_document(run_id)
        run = service.store.get_run(run_id)
        if spec is None or run is None:
            raise UnknownRun(run_id)
        self._refuse_if_in_flight(run_id)
        runner, info, accepted, warnings = self._accept(
            runner_id, spec, parameters, document if document is not None else spec
        )
        return runner, info, spec, accepted, warnings

    def _accept(
        self,
        runner_id: str,
        spec: Mapping[str, Any],
        parameters: Mapping[str, Any] | None,
        carrier: Mapping[str, Any],
    ) -> tuple[Any, RunnerInfo, dict[str, Any], list[str]]:
        """The runner, and the per-run parameters it accepts for ``spec`` —
        the part of a dispatch request a plan request shares."""
        runner = self._service.runner(runner_id)
        info: RunnerInfo = runner.describe()
        materialized = (spec.get("selection") or {}).get("materialized") or {}
        if not materialized.get("item_ids"):
            run_id = (spec.get("run") or {}).get("id")
            raise InvalidRequest(
                f"run {run_id} has an empty materialized selection — there is nothing to execute"
            )
        declared = {parameter.name for parameter in info.parameters}
        section = (carrier.get("runner") or {}).get(runner_id)
        section = dict(section) if isinstance(section, Mapping) else {}
        warnings: list[str] = []
        if parameters is None:
            # Values travelling in the document: only what the runner declared
            # as a per-run parameter is honoured. Everything else is ignored —
            # a document does not reconfigure the machine that executes it.
            ignored = sorted(set(section) - declared)
            if ignored:
                warnings.append(
                    f"runner.{runner_id} in the spec carries {ignored}, which "
                    f"{runner_id!r} does not accept as per-run parameters — ignored"
                )
            values = {name: value for name, value in section.items() if name in declared}
        else:
            values = dict(parameters)
        accepted = validate_parameters(info.parameters, values)
        return runner, info, accepted, warnings

    # -- previewing -----------------------------------------------------------------

    def plan(
        self,
        runner_id: str,
        spec: Mapping[str, Any],
        parameters: Mapping[str, Any] | None = None,
        *,
        run_id: str | None = None,
    ) -> PlanOutcome:
        """Ask a runner what a dispatch of ``spec`` would do (``Runner.plan``).
        Nothing is recorded: no dispatch, no event, no token, no directory.
        ``run_id`` says the document is a stored run's; a refusal — the
        runner's own, or its failing — is part of the answer, not an error."""
        service = self._service
        runner, info, accepted, warnings = self._accept(runner_id, spec, parameters, spec)
        if "plan" not in info.capabilities:
            raise InvalidRequest(
                f"runner {runner_id!r} does not offer a plan preview (it does not declare the "
                "'plan' capability)"
            )
        document = dict(spec)
        document["runner"] = {**(spec.get("runner") or {}), runner_id: dict(accepted)}
        ctx = _PlanContext(
            service,
            runner_id=runner_id,
            spec=document,
            options=service.runner_options(runner_id),
            parameters=accepted,
        )
        preview: PlanPreview | None = None
        refusal: str | None = None
        try:
            answer = runner.plan(ctx)
            if not isinstance(answer, PlanPreview):
                raise TypeError(
                    f"plan() must return a PlanPreview, got {type(answer).__name__}"
                )
            preview = PlanPreview(
                text=str(service.redact(answer.text)),
                shards={str(k): [str(i) for i in v] for k, v in answer.shards.items()},
                groups={str(k): str(v) for k, v in answer.groups.items()},
                estimates_ms={str(k): float(v) for k, v in answer.estimates_ms.items()},
                notes=tuple(str(service.redact(note)) for note in answer.notes),
                data=service.redact(dict(answer.data)),
            )
        except DispatchRefused as exc:
            refusal = str(service.redact(str(exc)))
        except InvalidParameter as exc:
            # The runner says a per-run value cannot be carried out: the
            # request is invalid, as when the declaration refuses it.
            raise _redacted_invalid(service, exc) from None
        except Exception as exc:
            log.error(
                "runner %r raised while planning\n%s", runner_id,
                service.redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))),
            )
            refusal = str(service.redact(f"{type(exc).__name__}: {exc}"))
        return PlanOutcome(
            runner_id=runner_id,
            run_id=run_id,
            item_ids=ctx.item_ids,
            parameters=accepted,
            preview=preview,
            refusal=refusal,
            warnings=warnings,
        )

    def _refuse_if_in_flight(self, run_id: str) -> None:
        run = self._service.store.get_run(run_id)
        latest = run.latest_dispatch if run is not None else None
        if latest is not None and latest.state in ACTIVE_STATES:
            raise RunActive(
                f"run {run_id} already has a dispatch in flight ({latest.dispatch_id}, "
                f"{latest.state}) — cancel it or wait for it before dispatching again"
            )

    def start(
        self,
        run_id: str,
        runner_id: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        foreground: bool = False,
        document: Mapping[str, Any] | None = None,
    ) -> Outcome:
        """Record a dispatch and run the runner — on the pool, or (``foreground``)
        in the calling thread, returning when the runner did."""
        service = self._service
        runner, _info, spec, accepted, warnings = self.prepare(
            run_id, runner_id, parameters, document
        )
        with self._lock:
            if self._closed:
                raise Conflict("the dispatch manager is shut down")
            # Checked again under the lock that records the dispatch: two
            # requests for one run must not both find it idle. Another
            # process has its own lock — the store's exclusive insert below
            # is what holds across processes.
            self._refuse_if_in_flight(run_id)
            if not foreground:
                self._refuse_if_queue_full()
            dispatch = service.ledger.open(
                run_id, runner_id, owner=self.owner, parameters=accepted, exclusive=True
            )
            if parameters is not None or document is not None:
                # Accepted per-run values are part of the document (§3:
                # runner.*) — written only for the request that is accepted
                # (its dispatch is recorded), after its values were validated.
                spec = dict(spec)
                spec["runner"] = {**(spec.get("runner") or {}), runner_id: dict(accepted)}
                service.store.set_spec_document(run_id, spec)
            ctx = _Context(
                service,
                dispatch,
                spec=spec,
                options=service.runner_options(runner_id),
                parameters=accepted,
            )
            active = _Active(ctx, runner, Outcome(dispatch.dispatch_id, run_id))
            self._active[dispatch.dispatch_id] = active
        for warning in warnings:
            ctx.log(warning, level="warning")
        # The monitor turns a cancel requested by another process (a flag in
        # the store) into the runner's cancel(ctx) — in the foreground (the
        # CLI's `run`) as much as on the pool.
        self._ensure_monitor()
        if foreground:
            self._execute(active)
            if active.outcome.invalid is not None:
                raise active.outcome.invalid
        else:
            active.future = self._pool.submit(self._execute, active)
        return active.outcome

    def _refuse_if_queue_full(self) -> None:
        """Called under the lock: as many dispatches already wait for a free
        slot as are queued at most."""
        waiting = sum(
            1 for active in self._active.values()
            if active.future is not None and not active.future.running() and not active.future.done()
        )
        if waiting >= self._max_queued:
            raise DispatchQueueFull(
                f"{waiting} dispatch(es) already wait for a free slot (core.dispatch.max_queued: "
                f"{self._max_queued}, max_concurrent executing) — try again later"
            )

    def wait_handoff(self, outcome: Outcome, timeout: float) -> bool:
        """Wait up to ``timeout`` for the dispatch to be declared, refused or
        over. True when one of those happened."""
        with self._lock:
            active = self._active.get(outcome.dispatch_id)
        if active is None:
            return True
        return active.ctx.handoff.wait(max(0.0, timeout))

    # -- executing ------------------------------------------------------------------

    def _execute(self, active: _Active) -> None:
        ctx, service = active.ctx, self._service
        error: BaseException | None = None
        try:
            if ctx.cancelled:
                raise DispatchCancelled("cancelled before the runner started")
            active.runner.dispatch(ctx)
        except BaseException as exc:
            error = exc
        try:
            ctx.close()
            self._settle(active, error)
        except Exception:
            log.exception("settling dispatch %s failed", ctx.dispatch_id)
        finally:
            try:
                service.ledger.release(ctx.dispatch_id)
            except Exception:
                log.exception("releasing dispatch %s failed", ctx.dispatch_id)
            with self._lock:
                self._active.pop(ctx.dispatch_id, None)
            ctx.handoff.set()
            active.outcome.done.set()
        if error is not None and not isinstance(error, Exception):
            raise error  # KeyboardInterrupt / SystemExit belong to the caller

    def _settle(self, active: _Active, error: BaseException | None) -> None:
        ctx, ledger = active.ctx, self._service.ledger
        dispatch = self._service.store.get_dispatch(ctx.dispatch_id)
        if dispatch is None:
            return
        # Ctrl-C in a foreground dispatch is the person at the keyboard
        # cancelling it.
        cancelled = ctx.cancelled or isinstance(error, (DispatchCancelled, KeyboardInterrupt))
        if cancelled and dispatch.state != "COMPLETE":
            # However the cancellation came about — a request this process or
            # another one made, Ctrl-C, a runner giving up by itself — the
            # runner's cancel(ctx) is called, once, before the dispatch is
            # closed as cancelled: whatever it started is stopped by then.
            self._signal(active)
            if not active.cancel_done.wait(_CANCEL_WAIT_S):
                ctx.log(
                    f"the runner's cancel() had not returned after {_CANCEL_WAIT_S:g}s — "
                    "closing the dispatch as cancelled without waiting for it any longer",
                    level="warning",
                )
        if dispatch.state == "COMPLETE":
            if error is not None:
                ctx.log(
                    f"the runner raised after all shards were delivered: "
                    f"{type(error).__name__}: {error}",
                    level="warning",
                )
            return
        if not ctx.declared:
            if cancelled:
                ledger.move(
                    ctx.dispatch_id, "CANCELLED", error="cancelled before the hand-off", error_code="cancelled"
                )
                return
            answered = isinstance(error, (DispatchRefused, InvalidParameter))
            if error is None:
                reason = (
                    f"runner {ctx.runner_id!r} returned without calling ctx.declare() — "
                    "nothing was handed off"
                )
            elif isinstance(error, InvalidParameter):
                invalid = _redacted_invalid(self._service, error)
                active.outcome.invalid = invalid
                reason = str(invalid)
            elif isinstance(error, DispatchRefused):
                reason = str(error)
            else:
                self._log_crash("raised before the hand-off", ctx, error)
                reason = f"{type(error).__name__}: {error}"
            reason = str(self._service.redact(reason))
            if active.outcome.invalid is None:
                active.outcome.refusal = reason
            # No record remains of this attempt, so neither do its files.
            ctx.remove_created_dirs()
            ledger.discard(ctx.dispatch_id, reason, crashed=error is not None and not answered)
            return
        if cancelled:
            ledger.move(ctx.dispatch_id, "CANCELLED", error="cancelled on request", error_code="cancelled")
            return
        if error is not None:
            if not isinstance(error, (DispatchRefused, InvalidParameter)):
                self._log_crash("failed after the hand-off", ctx, error)
            ledger.move(
                ctx.dispatch_id, "FAILED", error=f"{type(error).__name__}: {error}", error_code="runner-raised"
            )
            return
        # A runner that declared no shards at all has nothing left to wait
        # for: the dispatch is settled as it stands (INCOMPLETE — nothing
        # reported), instead of waiting for results nobody will send.
        dispatch = ledger.recompute(ctx.dispatch_id, finalize=not dispatch.shards)
        if dispatch.state == "RUNNING":
            ledger.move(ctx.dispatch_id, "AWAITING_RESULTS")

    def _log_crash(self, what: str, ctx: _Context, error: BaseException) -> None:
        """A runner's traceback is the operator's diagnostic — logged as
        text, with configured secrets blanked out: the exception message may
        quote an option."""
        trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        log.error(
            "runner %r %s (run %s, dispatch %s)\n%s",
            ctx.runner_id, what, ctx.run_id, ctx.dispatch_id,
            self._service.redact(trace.rstrip()),
        )

    # -- cancelling -------------------------------------------------------------------

    def cancel(self, run_id: str, *, force: bool = False) -> DispatchRecord:
        """Request cancellation of the run's dispatch in flight.

        Executing here → the runner is told and the dispatch becomes
        CANCELLED when it comes back. Executing in another live process →
        the request is recorded and that process honours it. Only waiting for
        results → the runner is asked to stop the executor, and the dispatch
        is CANCELLED now.

        ``force`` is the operator's way out when the process a dispatch names
        as its owner cannot be reached to honour the request — it looks alive
        but is not the one (a reused process id): the dispatch is closed as
        CANCELLED here and now, whatever its owner."""
        service = self._service
        # "Is something in flight" and recording the request are one unit:
        # a completion another process commits lands before it (and the
        # answer is "nothing in flight") or after it.
        with service.store.atomic():
            run = service.store.get_run(run_id)
            if run is None:
                raise UnknownRun(run_id)
            dispatch = run.latest_dispatch
            if dispatch is None or dispatch.state not in ACTIVE_STATES:
                state = dispatch.state if dispatch else "COMPOSED"
                raise Conflict(f"run {run_id} has nothing in flight to cancel (state {state})")
            service.store.update_dispatch(dispatch.dispatch_id, cancel_requested=True)
        service.bus.emit(
            run_id, kinds.RUN_CANCEL_REQUESTED, dispatch_id=dispatch.dispatch_id,
            data={"state": dispatch.state},
        )
        with self._lock:
            active = self._active.get(dispatch.dispatch_id)
        if active is not None:
            self._signal(active)
        elif force or not (dispatch.owner and owner_alive(dispatch.owner, self.owner)):
            # Nobody is executing it: stop the executor if the runner can, and
            # close the dispatch here.
            self._cancel_detached(dispatch)
        refreshed = service.store.get_dispatch(dispatch.dispatch_id)
        return refreshed if refreshed is not None else dispatch

    def _signal(self, active: _Active) -> None:
        """THE one way a dispatch executing here learns that it is cancelled:
        raise the context's flag and call the runner's ``cancel(ctx)`` —
        once per dispatch, whoever asks first (a cancel made in this process,
        the monitor that saw another process's request in the store, the
        settling of a dispatch that ended cancelled). ``active.cancel_done``
        is set when the runner's ``cancel`` has returned."""
        with self._lock:
            if active.cancel_sent:
                return
            active.cancel_sent = True
        try:
            active.ctx.request_cancel()
            if active.future is not None and active.future.cancel():
                # Still queued: the runner never started, so there is nothing
                # of its to stop — settle it here.
                try:
                    self._service.ledger.move(
                        active.ctx.dispatch_id, "CANCELLED", error="cancelled while queued",
                        error_code="cancelled",
                    )
                finally:
                    with self._lock:
                        self._active.pop(active.ctx.dispatch_id, None)
                    active.ctx.handoff.set()
                    active.outcome.done.set()
                return
            self._call_runner_cancel(active.runner, active.ctx)
        finally:
            active.cancel_done.set()

    def _cancel_detached(self, dispatch: DispatchRecord) -> None:
        service = self._service
        try:
            runner = service.runner(dispatch.mode)
        except Exception:
            runner = None  # export / ingest dispatches, or a runner no longer configured
        if runner is not None:
            spec = service.store.get_spec_document(dispatch.run_id) or {}
            ctx = _Context(
                service,
                dispatch,
                spec=spec,
                options=service.runner_options(dispatch.mode),
                parameters=dispatch.parameters,
            )
            ctx.request_cancel()
            self._call_runner_cancel(runner, ctx)
        try:
            service.ledger.move(
                dispatch.dispatch_id, "CANCELLED", error="cancelled on request", error_code="cancelled"
            )
        except InvalidTransition:
            # The last results arrived — from another process, say — while
            # the executor was being stopped: the run completed, and that is
            # the answer, as for a cancel that came after it.
            current = service.store.get_dispatch(dispatch.dispatch_id)
            state = current.state if current is not None else "gone"
            raise Conflict(
                f"run {dispatch.run_id} has nothing in flight to cancel any more: it became "
                f"{state} before the cancel took effect"
            ) from None

    def _call_runner_cancel(self, runner: Any, ctx: _Context) -> None:
        try:
            runner.cancel(ctx)
        except Exception as exc:
            ctx.log(f"runner cancel failed: {type(exc).__name__}: {exc}", level="warning")

    def _ensure_monitor(self) -> None:
        with self._lock:
            if self._monitor is None or not self._monitor.is_alive():
                self._monitor = threading.Thread(
                    target=self._watch_cancel_requests, name="runcomposer-cancel", daemon=True
                )
                self._monitor.start()

    def _watch_cancel_requests(self) -> None:
        """A cancel may be requested by another process (the CLI against a
        running server): it arrives as a flag in the store. This loop is the
        only reader of that flag for dispatches executing here, and it hands
        what it reads to ``_signal`` — so that no dispatch hears of a cancel
        without its runner's ``cancel(ctx)`` being called."""
        while True:
            time.sleep(_CANCEL_POLL_S)
            with self._lock:
                actives = [a for a in self._active.values() if not a.cancel_sent]
                if not self._active:
                    self._monitor = None
                    return
            for active in actives:
                try:
                    dispatch = self._service.store.get_dispatch(active.ctx.dispatch_id)
                except Exception:
                    continue
                if dispatch is not None and dispatch.cancel_requested:
                    self._signal(active)

    def exclusive(self) -> Any:
        """The lock under which a dispatch is checked for and recorded — for
        the other way a dispatch comes into being (an export), so that the
        two cannot both find a run idle."""
        return self._lock

    # -- waiting & shutdown ---------------------------------------------------------------

    def active_dispatch_ids(self) -> list[str]:
        with self._lock:
            return list(self._active)

    def join(self, timeout: float | None = None) -> bool:
        """Wait until nothing is executing in this process."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                outcomes = [active.outcome for active in self._active.values()]
            if not outcomes:
                return True
            for outcome in outcomes:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                outcome.done.wait(remaining)

    def shutdown(self, *, cancel: bool = True, timeout: float = 10.0) -> None:
        """Stop accepting work; ask what is executing to stop."""
        with self._lock:
            self._closed = True
            actives = list(self._active.values())
        if cancel:
            for active in actives:
                try:
                    self._service.store.update_dispatch(
                        active.ctx.dispatch_id, cancel_requested=True
                    )
                    self._signal(active)
                except Exception:
                    log.exception("could not cancel dispatch %s", active.ctx.dispatch_id)
        self.join(timeout)
        self._pool.shutdown(wait=False, cancel_futures=True)
        LIVE_OWNERS.discard(self.owner)
