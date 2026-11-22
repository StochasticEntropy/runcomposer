"""``Service`` — a thin facade over the application layer, and the embedding
surface (``runcomposer.sdk.Service``).

```python
from runcomposer.sdk import Service, load_config

with Service(load_config("config.yaml")) as service:
    run = service.compose_run({"tag_filter": "Checkout"}, title="Nightly", origin="script").run
    service.dispatch(run.id, "robot-pool", foreground=True)
    print(service.wait(run.id).completion)
```

Each responsibility lives in its own module (``compose``, ``dispatching``,
``ingestion``, ``ledger``, …); this class builds them once, lazily, and
offers the operations the transports share.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Generic, TypeVar, overload

from runcomposer.core import lifecycle
from runcomposer.core.errors import (
    ConfigError,
    Conflict,
    InvalidRequest,
    NotFound,
    RunActive,
    RunComposerError,
    RunnerUnavailable,
    UnknownRun,
    UnknownRunner,
)
from runcomposer.core.model import DispatchRecord, RunPage, RunRecord
from runcomposer.core.parameters import Parameter, validate_parameters
from runcomposer.core.ports import Health, RunnerInfo
from runcomposer.core.registry import installed_plugins
from runcomposer.core.taxonomy import resolve_taxonomy
from runcomposer.core.timeutil import normalize_time

from .artifacts import Artifacts
from .catalog import Catalog
from .compose import Composer, ComposeResult, ExportResult, Preview
from .config import Config, _resolve_env, resolve_options
from .ctrf import export_ctrf
from .dispatching import DispatchManager, Outcome, PlanOutcome
from .eventbus import EventBus, ObserverBinding
from .expiry import expire_awaiting
from .housekeeping import collect_garbage, remove_run_artifacts
from .ingestion import Ingestion, IngestReport, QuarantineReport
from .ledger import Ledger
from .quarantine import Quarantine
from .recovery import recover
from .redaction import FREE_TEXT_KEYS, log_failure, redact, redact_free_text, without_locations
from .taxonomy import read_taxonomy

__all__ = ["Service", "parameter_json"]

log = logging.getLogger("runcomposer.service")

_T = TypeVar("_T")


class _part(Generic[_T]):
    """A part of the Service, built on first use — exactly once, also when
    the first use comes from several threads at the same moment (a server's
    request threads all reach for the dispatch manager; two managers would
    each believe they are the only one)."""

    def __init__(self, factory: Callable[[Any], _T]):
        self._factory = factory
        self._name = factory.__name__
        self.__doc__ = factory.__doc__

    @overload
    def __get__(self, instance: None, owner: type | None = None) -> _part[_T]: ...

    @overload
    def __get__(self, instance: object, owner: type | None = None) -> _T: ...

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self
        parts = instance.__dict__
        if self._name not in parts:
            with instance._parts_lock:
                if self._name not in parts:
                    parts[self._name] = self._factory(instance)
        return parts[self._name]

_HEALTH_TTL_S = 30.0


def parameter_json(parameter: Parameter) -> dict[str, Any]:
    """One per-run parameter as a client renders a form field from it."""
    return {
        "name": parameter.name,
        "kind": parameter.kind,
        "label": parameter.label or parameter.name,
        "help": parameter.help,
        "default": parameter.default,
        "choices": list(parameter.choices),
        "required": parameter.required,
        "pattern": parameter.pattern,
        # The regular expression every key of a `mapping` must match (null:
        # any non-empty name), for a form to check names as it checks values.
        "key_pattern": parameter.key_pattern,
    }


class Service:
    """A runcomposer deployment in this process — catalog, store, runners,
    dispatch manager — driven from Python (compose, dispatch, wait, read)
    as the CLI and the HTTP API drive it; use it as a context manager."""

    def __init__(self, config: Config):
        self.config = config
        self._parts_lock = threading.RLock()
        self._runners: dict[str, Any] = {}
        self._runner_lock = threading.Lock()
        self._health: dict[str, tuple[float, Health]] = {}

    def __enter__(self) -> Service:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- the parts, built once ----------------------------------------------------

    @_part
    def store(self) -> Any:
        return self.config.build(self.config.store_spec())

    @_part
    def catalog(self) -> Catalog:
        return Catalog(self.config, redact=self.redact)

    @property
    def source(self) -> Catalog:
        """The catalog under the name the TestSource role goes by."""
        return self.catalog

    @_part
    def bus(self) -> EventBus:
        bindings = []
        for spec in self.config.plugin_specs("observers"):
            options = dict(spec.options)
            wanted = options.pop("events", None)
            if wanted is not None and (
                not isinstance(wanted, (list, tuple)) or not all(isinstance(k, str) for k in wanted)
            ):
                raise ConfigError(f"{spec.where}.events must be a list of event kinds")
            observer = self.config.build(type(spec)(spec.section, spec.key, spec.cls, options))
            binding = ObserverBinding(spec.key, observer)
            if wanted is not None:
                binding = ObserverBinding(spec.key, observer, tuple(wanted))
            bindings.append(binding)
        return EventBus(self.store, bindings, redact=self.redact_event_data, redact_text=self.redact)

    @_part
    def ledger(self) -> Ledger:
        return Ledger(self.store, self.bus, redact=self.redact)

    @_part
    def quarantine(self) -> Quarantine:
        return Quarantine(
            self.config.quarantine_dir, max_entries=int(self.config.ingestion["quarantine_max"])
        )

    @_part
    def ingestion(self) -> Ingestion:
        return Ingestion(self)

    @_part
    def composer(self) -> Composer:
        return Composer(self)

    @_part
    def artifacts(self) -> Artifacts:
        return Artifacts(self.store, self.config.artifact_dir)

    @_part
    def dispatcher(self) -> DispatchManager:
        return DispatchManager(
            self,
            max_concurrent=int(self.config.dispatch["max_concurrent"]),
            max_queued=int(self.config.dispatch["max_queued"]),
        )

    def redact(self, value: Any) -> Any:
        """``value`` with every secret this deployment takes from the
        environment blanked out, and without the locations of its own
        directories: a path in the artifact or private directory is written
        relative to it (``without_locations``). A runner may quote a
        configured option in a refusal, an error or its health — a hook
        command with a token in it, say — or a file in its work directory;
        what is recorded, logged and answered goes through here."""
        return without_locations(redact(value, self._secrets), self._locations)

    def redact_event_data(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """An event's data with the secrets blanked out of its free-text
        fields only (``message``, ``reason``, ``error``, ``plan``,
        ``detail``), and this machine's directories taken out of them —
        shard and group names, ids, states and links are identifiers, never
        rewritten."""
        return {
            key: without_locations(entry, self._locations) if key in FREE_TEXT_KEYS else entry
            for key, entry in redact_free_text(data, self._secrets).items()
        }

    @_part
    def _locations(self) -> dict[str, Path]:
        return {
            "<artifact directory>": self.config.artifact_dir,
            "<private directory>": self.config.private_dir,
        }

    @_part
    def _secrets(self) -> frozenset[str]:
        return self.config.secret_values()

    # -- startup --------------------------------------------------------------------

    def validate(self) -> None:
        """§8: fail loudly, now. Builds every configured plugin once — store,
        sources, runners, parsers, observers — and reads the taxonomy, so a
        typo in an option or a malformed tree refuses the start instead of
        surfacing at the first dispatch. Raises ConfigError / TaxonomyError."""
        self.config.validate()
        _ = self.store
        self.catalog.refresh()
        for runner_id in self.config.runner_ids():
            self.runner(runner_id)
        for spec in self.config.plugin_specs("parsers"):
            self.ingestion.parser(spec.key)
        _ = self.bus
        self.taxonomy()
        self._check_presets()

    def _check_presets(self) -> None:
        """A preset that names a runner must name a configured one, with
        parameters that runner accepts — or the UI offers a start that the
        API then refuses."""
        for index, preset in enumerate(self.config.ui.get("presets") or ()):
            runner_id = preset.get("runner")
            if runner_id is None:
                continue
            where = f"ui.presets[{index}] ({preset['name']!r})"
            if runner_id not in self.config.runner_ids():
                raise ConfigError(
                    f"{where}: runner {runner_id!r} is not configured under 'runners:' "
                    f"(configured: {self.config.runner_ids() or 'none'})"
                )
            try:
                validate_parameters(self.describe_runner(runner_id).parameters, preset["parameters"])
            except InvalidRequest as exc:
                raise ConfigError(f"{where}: {exc}") from None

    def recover(self) -> list[DispatchRecord]:
        """Mark dispatches a dead process left executing as STALE."""
        return recover(self)

    def close(self) -> None:
        """Stop what this process is executing and release resources."""
        parts = self.__dict__
        if "dispatcher" in parts:
            self.dispatcher.shutdown()
        if "bus" in parts:
            self.bus.close()
        if "store" in parts:
            self.store.close()

    # -- runners ----------------------------------------------------------------------

    def runner(self, runner_id: str) -> Any:
        """The runner configured under ``runner_id`` — one instance per id.
        Only what is configured under ``runners:`` is dispatchable."""
        with self._runner_lock:
            if runner_id not in self._runners:
                spec = self.config.plugin_spec("runners", runner_id)
                if spec is None:
                    raise UnknownRunner(
                        f"no runner is configured under the id {runner_id!r} "
                        f"(configured: {self.config.runner_ids() or 'none'})"
                    )
                self._runners[runner_id] = self.config.build(spec)
            return self._runners[runner_id]

    def describe_runner(self, runner_id: str) -> RunnerInfo:
        """What the runner under ``runner_id`` says it is. A ``describe()``
        that raises is ``RunnerUnavailable`` naming the runner — the
        exception, with its traceback, goes to the server log, not into an
        answer."""
        runner = self.runner(runner_id)
        try:
            info: RunnerInfo = runner.describe()
        except Exception as exc:
            self._log_failure(f"runner {runner_id!r}: describe() raised", exc)
            raise RunnerUnavailable(
                f"runner {runner_id!r} could not describe itself ({type(exc).__name__}; "
                "the log has the traceback) — nothing can be planned or dispatched with it"
            ) from None
        return info

    def _log_failure(self, what: str, error: BaseException) -> None:
        """A plugin's exception, for the operator (``log_failure``)."""
        log_failure(log, what, error, self.redact)

    def connector_settings(self, name: str) -> Mapping[str, Any]:
        """``connectors.<name>`` as a plugin or a configured callable reads
        it: ``${env:…}`` resolved, read-only, empty when absent."""
        raw = self.config.connector_settings(str(name))
        return MappingProxyType(_resolve_env(raw, f"connectors.{name}"))

    def runner_options(self, runner_id: str) -> dict[str, Any]:
        """The runner's configured options as its constructor received them
        (environment references substituted, converted where the
        constructor annotates a number or a boolean)."""
        spec = self.config.plugin_spec("runners", runner_id)
        if spec is None:
            return {}
        return resolve_options(spec)

    def runner_health(self, runner_id: str, *, refresh: bool = False) -> Health:
        cached = self._health.get(runner_id)
        if cached and not refresh and time.monotonic() - cached[0] < _HEALTH_TTL_S:
            return cached[1]
        try:
            health = self.runner(runner_id).health()
            if not isinstance(health, Health):
                health = Health(status="unknown", detail=str(health))
        except Exception as exc:
            health = Health(status="down", detail=f"{type(exc).__name__}: {exc}")
        health = Health(
            status=health.status,
            detail=str(self.redact(health.detail)),
            data=self.redact(dict(health.data)),
        )
        self._health[runner_id] = (time.monotonic(), health)
        return health

    def runner_infos(self, *, refresh: bool = False, health: bool = True) -> list[dict[str, Any]]:
        """Every configured runner, under the id it is dispatched by, with
        its declared per-run parameters and cached health."""
        infos = []
        for runner_id in self.config.runner_ids():
            entry: dict[str, Any] = {"id": runner_id}
            try:
                info: RunnerInfo = self.describe_runner(runner_id)
            except Exception as exc:  # surfaced, not hidden: the UI lists it with the reason
                entry["error"] = (
                    str(exc) if isinstance(exc, RunComposerError)
                    else f"runner {runner_id!r} could not be built ({type(exc).__name__}; the log has the traceback)"
                )
                if not isinstance(exc, RunComposerError):
                    self._log_failure(f"runner {runner_id!r} could not be built", exc)
                infos.append(entry)
                continue
            entry.update(
                {
                    "plugin": info.id,
                    "title": info.title or runner_id,
                    "description": info.description,
                    "capabilities": list(info.capabilities),
                    "result_format": info.result_format,
                    "parameters": [parameter_json(p) for p in info.parameters],
                }
            )
            if health:
                state = self.runner_health(runner_id, refresh=refresh)
                entry["health"] = {
                    "status": state.status, "detail": state.detail, "data": dict(state.data)
                }
            infos.append(entry)
        return infos

    def configured_plugins(self) -> list[dict[str, Any]]:
        """What this deployment actually loads: every configured entry with
        the class behind it and the distribution that class comes from —
        entry-point plugins and ``module:`` plugins alike."""
        from importlib import metadata

        providers = metadata.packages_distributions()
        by_target = {plugin.target: plugin for plugin in installed_plugins()}
        plugins = []
        for section in ("store", "sources", "runners", "parsers", "observers"):
            for spec in self.config.plugin_specs(section):
                module = str(spec.cls.__module__ or "")
                target = f"{module}:{spec.cls.__qualname__}"
                # An entry-point plugin names its distribution itself; for an
                # import path, the distribution that provides the package.
                registered = by_target.get(target)
                if registered is not None and registered.distribution:
                    distribution, version = registered.distribution, registered.version
                else:
                    distribution = next(iter(providers.get(module.split(".")[0], [])), "")
                    try:
                        version = metadata.version(distribution) if distribution else ""
                    except metadata.PackageNotFoundError:
                        version = ""
                plugins.append(
                    {
                        "section": section,
                        "id": spec.key,
                        "plugin": target,
                        "distribution": distribution,
                        "version": version,
                    }
                )
        return plugins

    # -- compose ------------------------------------------------------------------------

    def preview(self, selection_data: Mapping[str, Any]) -> Preview:
        return self.composer.preview(selection_data)

    def resolve_history(
        self, query: str, *, labels: Mapping[str, str] | None = None
    ) -> tuple[list[str], dict[str, Any]]:
        return self.composer.resolve_history(query, labels=labels)

    def build_spec_document(self, selection_data: Mapping[str, Any], **kwargs: Any) -> ComposeResult:
        return self.composer.build_spec_document(selection_data, **kwargs)

    def compose_run(self, selection_data: Mapping[str, Any], **kwargs: Any) -> ComposeResult:
        return self.composer.compose_run(selection_data, **kwargs)

    def import_spec(self, doc: Mapping[str, Any], *, origin: str) -> str:
        return self.composer.import_spec(doc, origin=origin)

    def export(self, run_id: str, *, format: str = "json") -> ExportResult:
        return self.composer.export(run_id, format=format)

    def dispatch_spec(self, run_id: str, dispatch_id: str) -> tuple[bytes, str, DispatchRecord]:
        """The exact bytes handed out for a dispatch — their sha256 is the
        dispatch's ``spec_sha256``. They never contain the ingest token."""
        dispatch = self._dispatch_of(run_id, dispatch_id)
        stored = self.store.get_dispatch_spec(dispatch_id)
        if stored is None:
            raise NotFound(
                f"dispatch {dispatch_id} has no handed-out document — only export dispatches "
                "and runners that hand a spec to an external executor keep one"
            )
        return stored[0], stored[1], dispatch

    def rotate_token(self, run_id: str, dispatch_id: str) -> str | None:
        """Replace a dispatch's ingest token. The previous one stops working;
        the new one is returned once."""
        self._dispatch_of(run_id, dispatch_id)
        return self.ingestion.mint_token(run_id, dispatch_id)

    # -- dispatch -----------------------------------------------------------------------

    def dispatch(
        self,
        run_id: str,
        runner_id: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        foreground: bool = False,
        document: Mapping[str, Any] | None = None,
    ) -> Outcome:
        """Dispatch a stored run to a configured runner. Returns as soon as
        the dispatch is recorded — or, with ``foreground``, when the runner's
        ``dispatch()`` returned (the CLI's mode). ``document`` is a copy of
        the run's spec handed in for this dispatch (``import_spec`` accepted
        it): its ``runner.<id>`` section states the per-run parameters."""
        return self.dispatcher.start(
            run_id, runner_id, parameters, foreground=foreground, document=document
        )

    def cancel(self, run_id: str, *, force: bool = False) -> DispatchRecord:
        return self.dispatcher.cancel(run_id, force=force)

    def plan(
        self,
        runner_id: str,
        *,
        run_id: str | None = None,
        selection: Mapping[str, Any] | None = None,
        parameters: Mapping[str, Any] | None = None,
        title: str = "Plan preview",
        labels: Mapping[str, str] | None = None,
    ) -> PlanOutcome:
        """What a dispatch to ``runner_id`` would do — for a stored run
        (``run_id``) or for a selection that is not stored and will not be
        (``selection``). Nothing is persisted, executed or triggered. For a
        stored run, ``parameters=None`` means the ones its document carries."""
        if (run_id is None) == (selection is None):
            raise InvalidRequest("a plan is for a stored run or for a selection — give one of them")
        info = self.describe_runner(runner_id)
        if run_id is not None:
            self.require_run(run_id)
            return self.dispatcher.plan(
                runner_id, self.require_spec(run_id), parameters, run_id=run_id
            )
        assert selection is not None
        self.catalog.refresh()
        document = self.build_spec_document(
            selection, title=title, labels=labels, expect_format=info.result_format
        )
        outcome = self.dispatcher.plan(runner_id, document.spec, dict(parameters or {}))
        outcome.warnings = [*document.warnings, *outcome.warnings]
        return outcome

    def expire_awaiting(self, *, now_ms: int | None = None) -> list[DispatchRecord]:
        """Close dispatches that have waited for results longer than
        ``core.dispatch.awaiting_timeout_s`` (DESIGN.md §4; ``app.expiry``)."""
        return expire_awaiting(self, now_ms=now_ms)

    def import_durations(
        self, durations: Mapping[str, float], *, labels: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        """Seed the duration history (``runcomposer history import``): every
        name is resolved through the catalog; what it does not know is
        reported, not stored. Seeds answer only for items no execution has
        measured yet."""
        resolved: dict[str, float] = {}
        unknown: list[str] = []
        zero = 0
        for name, duration in durations.items():
            item_id = self.catalog.resolve(str(name))
            if item_id is None:
                unknown.append(str(name))
            elif float(duration) <= 0:
                zero += 1
            else:
                resolved[item_id] = float(duration)
        stored = int(self.store.seed_durations(resolved, labels=dict(labels or {}) or None))
        return {
            "imported": stored,
            "unknown": sorted(unknown),
            "without_duration": zero,
            "labels": dict(labels or {}),
        }

    def rerun(
        self,
        run_id: str,
        *,
        scope: str = "all",
        runner_id: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        foreground: bool = False,
        title: str | None = None,
    ) -> tuple[str, Outcome]:
        """``all``: a new dispatch of the same run. ``failed``: a new run
        composed from what did not pass (FAIL, ERROR, MISSING) in this one —
        with ``derived_from`` provenance and origin ``rerun`` — and
        dispatched. Its title is ``title``, else the source run's: that it is
        a rerun is data (origin, ``derived_from``), not prose in a title.
        Returns the id of the run that was dispatched and the outcome
        handle."""
        run = self.require_run(run_id)
        latest = run.latest_dispatch
        previous = latest.mode if latest and latest.mode not in ("export", "ingest") else None
        runner = runner_id or previous
        if runner is None:
            raise InvalidRequest(
                f"run {run_id} was never dispatched to a runner — name the runner to rerun it with"
            )
        if parameters is None and latest is not None and latest.mode == runner:
            parameters = dict(latest.parameters)
        if scope == "all":
            return run_id, self.dispatch(run_id, runner, parameters, foreground=foreground)
        if scope != "failed":
            raise InvalidRequest(f"rerun scope must be 'all' or 'failed', got {scope!r}")
        if run.state not in lifecycle.TERMINAL_STATES:
            raise RunActive(f"run {run_id} is {run.state} — its failures are not known yet")
        composed = self.composer.compose_rerun(
            run, runner_id=runner, parameters=parameters, title=title
        )
        assert composed.run is not None
        try:
            outcome = self.dispatch(composed.run.id, runner, parameters, foreground=foreground)
        except Exception:
            self.delete_run(composed.run.id)
            raise
        return composed.run.id, outcome

    def finalize(self, run_id: str) -> RunRecord:
        """Close the run's latest dispatch as it stands: outstanding shards
        are given up, never-reported items become ``MISSING``, completion is
        computed. Refused while something is still executing it."""
        run = self.require_run(run_id)
        dispatch = run.latest_dispatch
        if dispatch is None:
            raise Conflict(
                f"run {run_id} was never dispatched and has no results — there is nothing to "
                "finalize"
            )
        if dispatch.state in ("DISPATCHED", "RUNNING"):
            raise RunActive(
                f"run {run_id} is {dispatch.state} — cancel it, or wait for the runner, "
                "before finalizing"
            )
        if dispatch.state == "CANCELLED":
            raise Conflict(
                f"run {run_id} was cancelled — a cancelled dispatch has no result to settle; "
                "dispatch the run again instead"
            )
        self.ledger.recompute(dispatch.dispatch_id, finalize=True)
        return self.require_run(run_id)

    def wait(
        self, run_id: str, *, timeout: float | None = None, dispatch_id: str | None = None
    ) -> RunRecord:
        """Block until the run (or the given dispatch) is in a terminal
        state, or the timeout passes; returns the run as it is then."""

        def settled() -> bool:
            run = self.store.get_run(run_id)
            if run is None:
                return True
            if dispatch_id is None:
                return run.state in lifecycle.TERMINAL_STATES or run.state == "COMPOSED"
            dispatch = next((d for d in run.dispatches if d.dispatch_id == dispatch_id), None)
            return dispatch is None or dispatch.state in lifecycle.TERMINAL_STATES

        self.bus.wait(settled, timeout)
        return self.require_run(run_id)

    # -- ingestion ----------------------------------------------------------------------

    def ingest(self, bundle_path: str | Path, **kwargs: Any) -> IngestReport:
        return self.ingestion.ingest(bundle_path, **kwargs)

    def ingest_or_quarantine(
        self, bundle_path: str | Path, **kwargs: Any
    ) -> IngestReport | QuarantineReport:
        return self.ingestion.ingest_or_quarantine(bundle_path, **kwargs)

    # -- runs ---------------------------------------------------------------------------

    def require_run(self, run_id: str) -> RunRecord:
        run: RunRecord | None = self.store.get_run(run_id)
        if run is None:
            raise UnknownRun(run_id)
        return run

    def require_spec(self, run_id: str) -> dict[str, Any]:
        spec: dict[str, Any] | None = self.store.get_spec_document(run_id)
        if spec is None:
            raise UnknownRun(run_id)
        return spec

    def _dispatch_of(self, run_id: str, dispatch_id: str) -> DispatchRecord:
        run = self.require_run(run_id)
        for dispatch in run.dispatches:
            if dispatch.dispatch_id == dispatch_id:
                return dispatch
        raise NotFound(f"run {run_id} has no dispatch {dispatch_id!r}")

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
        if state is not None and state not in lifecycle.RUN_STATES:
            raise InvalidRequest(
                f"unknown run state {state!r} (one of {list(lifecycle.RUN_STATES)})"
            )
        if limit < 0 or offset < 0:
            raise InvalidRequest("limit and offset must not be negative")
        page: RunPage = self.store.list_runs(
            state=state,
            labels=labels or None,
            since=normalize_time(since) if since else None,
            until=normalize_time(until) if until else None,
            q=q or None,
            limit=limit,
            offset=offset,
        )
        return page

    def delete_run(self, run_id: str) -> None:
        """Remove a run, everything recorded for it, and its artifact
        directory. Refused while a runner is executing it."""
        # The check and the delete are one unit of the store: a dispatch
        # another process starts in between waits for it — and then finds
        # no run — instead of being deleted under its runner.
        with self.store.atomic():
            run = self.require_run(run_id)
            latest = run.latest_dispatch
            if latest is not None and latest.state in ("DISPATCHED", "RUNNING") and latest.owner:
                raise RunActive(f"run {run_id} is {latest.state} — cancel it before deleting it")
            self.store.delete_run(run_id)
        remove_run_artifacts(self.config.artifact_dir, run_id, self.config.private_dir)
        log.info("run %s deleted", run_id)

    def gc(self) -> dict[str, Any]:
        return collect_garbage(self)

    def export_ctrf(self, run_id: str) -> dict[str, Any]:
        """A normalized cross-tool result document (CTRF) of the run's latest
        dispatch (``app.ctrf``)."""
        return export_ctrf(self, run_id)

    # -- taxonomy -------------------------------------------------------------------------

    def taxonomy(self) -> dict[str, Any]:
        """Read and validate the configured taxonomy file (DESIGN.md §2, §8)
        — on every read, because the file can change under a running server
        (``app.taxonomy``)."""
        return read_taxonomy(self.config)

    def resolved_taxonomy(self) -> dict[str, Any]:
        """The taxonomy resolved against the live catalog (DESIGN.md §2)."""
        return resolve_taxonomy(
            self.taxonomy(), self.catalog.items(), origin=str(self.config.taxonomy_source)
        )
