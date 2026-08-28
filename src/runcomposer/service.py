"""Application services: compose, dispatch, ingest — the wiring layer between
the core, the configured plugins, and the CLI/API surfaces.

This module is deliberately outside ``runcomposer.core``: it imports plugins
via config and owns transport concerns (files, bundles, markers) the core
never sees.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path
from typing import Any, Mapping

import yaml

from runcomposer.config import Config
from runcomposer.core.ids import new_ulid
from runcomposer.core.lifecycle import all_shards_delivered, completion_of
from runcomposer.core.model import DispatchRecord, Item, RunRecord, Verdict
from runcomposer.core.ports import DispatchReservation, ParsedVerdict
from runcomposer.core.registry import PARSER_GROUP, PluginError, resolve_plugin
from runcomposer.core.selection import Selection
from runcomposer.core.spec import build_spec, validate_document

__all__ = ["ComposeResult", "IngestError", "IngestReport", "Service", "ServiceError", "MARKER_FILENAME"]

MARKER_FILENAME = "runcomposer_run.json"
REFERENCE_FORMAT = "runcomposer-verdicts"


class ServiceError(ValueError):
    """Raised for request-level errors (unknown run, bad input)."""


class IngestError(ValueError):
    """Raised when a bundle is refused. ``reason`` classifies the refusal so
    transports can route it: unsolicited | unknown-run | sha-mismatch |
    marker-conflict | bad-bundle."""

    def __init__(self, message: str, *, reason: str = "bad-bundle"):
        super().__init__(message)
        self.reason = reason


# Refusal reasons that §4/§5 route to the quarantine inbox when the bundle
# arrived over a transport (file-drop, push) rather than an explicit CLI call.
QUARANTINE_REASONS = ("unsolicited", "unknown-run", "sha-mismatch", "marker-conflict", "bad-bundle")


@dataclass
class ComposeResult:
    spec: dict[str, Any]
    run: RunRecord | None = None
    dispatch: DispatchRecord | None = None
    items: list[Item] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class IngestReport:
    run_id: str
    dispatch_id: str | None
    shard: str
    outcome: str  # new | duplicate | replaced
    verdict_count: int
    run_state: str
    completion: str | None
    warnings: list[str] = field(default_factory=list)


@dataclass
class QuarantineReport:
    """Outcome of a transport delivery that was quarantined instead of
    ingested. ``entry_id`` is None when an identical bundle was already
    quarantined (deduplicated)."""

    entry_id: str | None
    reason: str
    detail: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _DispatchLedger:
    """Backs one ``DispatchReservation``: mints the dispatch id up front and
    writes its store record when the runner reports the hand-off.

    The id exists from the start (the runner needs it for artifact paths,
    listeners and executor parameters); the *record* appears only when the
    runner calls it — so a refusal raised before the hand-off leaves no
    dispatch row, and everything after it is a real execution attempt."""

    def __init__(self, store, run_id: str, mode: str):
        self._store = store
        self._run_id = run_id
        self._mode = mode
        self.dispatch_id = new_ulid()
        self.record: DispatchRecord | None = None
        self._closed = False

    def __call__(self, *, shards: int | None = None, spec_sha256: str | None = None) -> None:
        if self._closed:
            raise ServiceError(
                f"dispatch {self.dispatch_id} was recorded outside its dispatch() call — "
                "a reservation is only valid while the runner is dispatching"
            )
        if self.record is not None:
            raise ServiceError(
                f"dispatch {self.dispatch_id} is already recorded — one hand-off per "
                "dispatch() call (re-running a spec is a new dispatch, DESIGN.md §4)"
            )
        self.record = self._store.add_dispatch(
            self._run_id,
            dispatch_id=self.dispatch_id,
            mode=self._mode,
            declared_shards=shards,
            spec_sha256=spec_sha256,
        )

    def close(self) -> None:
        self._closed = True


def bundle_content_hash(path: Path) -> str:
    """Content hash over the whole bundle (file names + bytes), the §5
    idempotency key."""
    digest = hashlib.sha256()
    if path.is_file():
        files = [path]
        root = path.parent
    else:
        files = sorted(p for p in path.rglob("*") if p.is_file())
        root = path
    for file in files:
        digest.update(file.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file.read_bytes())
    return "sha256:" + digest.hexdigest()


class Service:
    def __init__(self, config: Config):
        self.config = config
        self.last_dispatch_plan: str = ""

    @cached_property
    def store(self):
        return self.config.build_store()

    @cached_property
    def source(self):
        return self.config.build_source()

    @cached_property
    def quarantine(self):
        from runcomposer.quarantine import Quarantine

        return Quarantine(self.config.quarantine_dir)

    # -- selection & compose -------------------------------------------------

    def preview(self, selection_data: Mapping[str, Any]) -> tuple[list[Item], list[str]]:
        selection_data, _derived, warnings = self._prepare_selection(selection_data)
        selection = Selection.from_data(selection_data)
        items = selection.compile(self.source.items())
        if not items:
            warnings.append("selection matched 0 items")
        return items, warnings

    # -- history-based selection (§7): a compose-time provider ----------------

    _HISTORY_VERDICTS = {"failed": "FAIL", "passed": "PASS", "skipped": "SKIP", "error": "ERROR"}

    def resolve_history(self, query: str) -> tuple[list[str], dict[str, Any]]:
        """Resolve '<verdict>@<selector>' (e.g. 'failed@latest',
        'failed@run:<id>', 'failed@before:<ISO time>') into item ids + the
        §3.1 derived_from provenance entry. The two-level contract (§6.3):
        the RUN is selected by completion status, the ITEMS by verdict."""
        verdict_word, sep, selector = query.partition("@")
        status = self._HISTORY_VERDICTS.get(verdict_word)
        if not sep or status is None:
            raise ServiceError(
                f"history query must be '<verdict>@<selector>' with verdict in "
                f"{sorted(self._HISTORY_VERDICTS)}, got {query!r}"
            )
        if selector == "latest":
            run = self.store.latest_completed_run()
            query_doc: dict[str, Any] = {"run": "LATEST", "verdicts": [status]}
        elif selector.startswith("run:"):
            run = self.store.get_run(selector[len("run:"):])
            query_doc = {"run": selector[len("run:"):], "verdicts": [status]}
        elif selector.startswith("before:"):
            cutoff = selector[len("before:"):]
            run = self.store.latest_completed_run(completed_before=cutoff)
            query_doc = {"run": {"before": cutoff}, "verdicts": [status]}
        else:
            raise ServiceError(
                f"unknown history selector {selector!r} (expected latest, run:<id>, before:<time>)"
            )
        if run is None:
            raise ServiceError(
                f"history selection {query!r} matched no completed run — history "
                "features are dark on a fresh store (DESIGN.md §6.3)"
            )
        latest_dispatch = run.dispatches[-1].dispatch_id if run.dispatches else None
        item_ids = list(
            dict.fromkeys(
                v.item_id
                for v in self.store.verdicts_for(run.id, latest_dispatch)
                if v.status == status
            )
        )
        return item_ids, {"provider": "history", "query": query_doc, "resolved_run_id": run.id}

    def _prepare_selection(
        self, selection_data: Mapping[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
        """Resolve compose-time providers (currently: history) into plain
        selection inputs + provenance. The executed spec stays static (§7)."""
        data = dict(selection_data)
        derived: list[dict[str, Any]] = []
        warnings: list[str] = []
        history_query = data.pop("history", None)
        if history_query:
            ids, provenance = self.resolve_history(history_query)
            if not ids:
                raise ServiceError(
                    f"history selection {history_query!r} matched no items in run "
                    f"{provenance['resolved_run_id']} — nothing to rerun"
                )
            derived.append(provenance)
            known = {item.id for item in self.source.items()}
            gone = [item_id for item_id in ids if item_id not in known]
            for item_id in gone:
                warnings.append(f"history item {item_id!r} is no longer in the catalog — dropped")
            ids = [item_id for item_id in ids if item_id in known]
            if data.get("item_ids"):
                picks = set(data["item_ids"])
                ids = [item_id for item_id in ids if item_id in picks]
            data["item_ids"] = ids
        return data, derived, warnings

    def build_spec_document(
        self,
        selection_data: Mapping[str, Any],
        *,
        title: str,
        labels: Mapping[str, str] | None = None,
        derived_from: list[Mapping[str, Any]] | None = None,
        mint_token: bool = False,
        runner_section: Mapping[str, Any] | None = None,
        expect_format: str = REFERENCE_FORMAT,
    ) -> ComposeResult:
        selection_data, resolved_derived, warnings = self._prepare_selection(selection_data)
        derived_from = [*(derived_from or []), *resolved_derived] or None
        selection = Selection.from_data(selection_data)
        items = selection.compile(self.source.items())
        if not items:
            warnings.append("selection matched 0 items")
        results: dict[str, Any] = {
            "expect": [{"format": expect_format}],
            "shards": 1,
            "deliver": "none",
        }
        token = None
        if mint_token and self.config.ingestion.get("tokens") != "disabled":
            token = "rct_" + secrets.token_urlsafe(24)
            results["token"] = token
        spec = build_spec(
            title=title,
            tag_filter=selection_data.get("tag_filter"),
            item_ids=list(selection_data.get("item_ids") or []) or None,
            materialized_ids=[item.id for item in items],
            derived_from=derived_from,
            source_provider=self.source.provider_id,
            snapshot=self.source.snapshot(),
            results=results,
            runner=dict(runner_section) if runner_section else None,
            labels=labels,
        )
        report = validate_document(spec, for_dispatch=True)
        if not report.ok:  # composing an invalid spec is a bug, not user error
            raise AssertionError(f"composed spec failed validation: {report.errors}")
        return ComposeResult(spec=spec, items=items, warnings=warnings + report.warnings)

    def compose_run(
        self,
        selection_data: Mapping[str, Any],
        *,
        title: str,
        labels: Mapping[str, str] | None = None,
        origin: str,
        runner_section: Mapping[str, Any] | None = None,
        expect_format: str = REFERENCE_FORMAT,
    ) -> ComposeResult:
        result = self.build_spec_document(
            selection_data,
            title=title,
            labels=labels,
            mint_token=True,
            runner_section=runner_section,
            expect_format=expect_format,
        )
        token = result.spec.get("results", {}).get("token")
        token_sha = hashlib.sha256(token.encode()).hexdigest() if token else None
        result.run = self.store.create_run(result.spec, origin=origin, ingest_token_sha256=token_sha)
        return result

    # -- dispatch --------------------------------------------------------------

    def import_spec(self, doc: Mapping[str, Any], *, origin: str) -> str:
        """Register an externally produced spec document (the dispatch CLI's
        path for spec files composed elsewhere). Validates for dispatch;
        no-op when the run id is already stored."""
        report = validate_document(doc, for_dispatch=True)
        if not report.ok:
            raise ServiceError(
                "spec document is not dispatchable: " + "; ".join(report.errors)
            )
        run_id = doc["run"]["id"]
        if self.store.get_run(run_id) is None:
            self.store.create_run(doc, origin=origin)
        return run_id

    def export_dispatch(self, run_id: str, *, spec_bytes: bytes) -> DispatchRecord:
        """An export download mints a dispatch (§4). ``spec_bytes`` are the
        exact bytes handed out; their hash anchors marker verification (§5)."""
        spec = self._require_spec(run_id)
        declared = spec.get("results", {}).get("shards")
        declared = declared if isinstance(declared, int) else 1
        dispatch = self.store.add_dispatch(
            run_id,
            dispatch_id=new_ulid(),
            mode="export",
            declared_shards=declared,
            spec_sha256=hashlib.sha256(spec_bytes).hexdigest(),
        )
        self.store.set_run_state(run_id, "AWAITING_RESULTS")
        return dispatch

    def dispatch_runner(self, run_id: str, runner_id: str) -> DispatchRecord:
        """In-process dispatch: hand the document to a Runner plugin.

        Two optional runner hooks, both discovered by attribute so a runner
        that implements only ``describe``/``dispatch`` keeps working:

        - ``bind`` gets the store/source injected so the runner can stream
          live status, read duration history, and run the §3.3 drift check
          (robot-pool).
        - ``bind_dispatch`` receives a ``DispatchReservation``: runcomposer
          mints the dispatch id (as it does for export dispatches) and the
          runner records the hand-off at the moment it makes it. That is what
          keeps a *running* run visible as a dispatch — ``robot-pool``
          executes inside ``dispatch()``, so a dispatch recorded from the
          returned handle would only appear once the run is over (§4: a
          dispatch is the record of a hand-off, not of its completion).
          Without the hook, the handle's id is recorded on return as before.

        A runner exposing a ``deliveries`` list (the demo runner) gets those
        recorded here.

        Failure semantics (§4): a runner that refuses *before* the hand-off
        leaves no dispatch behind and the run returns to COMPOSED — a refused
        dispatch is not an execution and must not look like one. A failure
        *after* the hand-off keeps the dispatch (it happened) and leaves the
        run AWAITING_RESULTS, since whatever was handed over may still
        deliver — or a human finalizes it.
        """
        spec = self._require_spec(run_id)
        runner = self.config.build_runner(runner_id)
        if hasattr(runner, "bind"):
            # A FRESH source, not the compose-time cached one: the §3.3 drift
            # check compares the spec's snapshot against the corpus as it is NOW.
            runner.bind(
                store=self.store,
                source=self.config.build_source(),
                artifact_root=self.config.artifact_dir,
            )
        ledger = _DispatchLedger(self.store, run_id, runner_id)
        if hasattr(runner, "bind_dispatch"):
            runner.bind_dispatch(
                DispatchReservation(dispatch_id=ledger.dispatch_id, record=ledger)
            )
        self.store.set_run_state(run_id, "DISPATCHED")
        try:
            handle = runner.dispatch(spec)
        except Exception:
            ledger.close()
            if ledger.record is None:
                self.store.set_run_state(run_id, "COMPOSED")  # dispatch never happened
            else:
                self.store.set_run_state(run_id, "AWAITING_RESULTS")  # handed off, then failed
            raise
        ledger.close()
        self.last_dispatch_plan = getattr(runner, "last_plan", "")
        dispatch = self._settle_dispatch(run_id, runner_id, ledger, handle)
        for delivery in getattr(runner, "deliveries", []):
            verdicts = delivery["verdicts"]
            payload = json.dumps(
                [(v.item_id, v.status, v.duration_ms, v.attempt) for v in verdicts]
            ).encode()
            self.store.record_delivery(
                run_id,
                dispatch_id=dispatch.dispatch_id,
                shard=delivery.get("shard", "1"),
                content_hash="sha256:" + hashlib.sha256(payload).hexdigest(),
                format=REFERENCE_FORMAT,
                verdicts=verdicts,
            )
        self._recompute_completion(run_id, dispatch)
        return dispatch

    def _settle_dispatch(
        self, run_id: str, runner_id: str, ledger: "_DispatchLedger", handle
    ) -> DispatchRecord:
        """Reconcile the returned handle with the reservation. The handle is
        the runner's final declaration (§4: the shard set is known after
        planning), so it refines a record written at hand-off time."""
        recorded = ledger.record
        if recorded is None:
            # No reservation used: the runner minted its own dispatch id, and
            # the dispatch becomes visible now — execution is already over.
            return self.store.add_dispatch(
                run_id,
                dispatch_id=handle.dispatch_id,
                mode=runner_id,
                declared_shards=handle.shards,
                spec_sha256=handle.spec_sha256,
            )
        if handle.dispatch_id != recorded.dispatch_id:
            raise ServiceError(
                f"runner {runner_id!r} recorded dispatch {recorded.dispatch_id} but returned "
                f"handle {handle.dispatch_id!r}: the reserved id IS the dispatch id — a "
                "returned marker naming the other one cannot correlate (DESIGN.md §4)"
            )
        spec_sha256 = handle.spec_sha256 or recorded.spec_sha256
        if handle.shards != recorded.declared_shards or spec_sha256 != recorded.spec_sha256:
            return self.store.add_dispatch(  # re-declaration: same row, final numbers
                run_id,
                dispatch_id=recorded.dispatch_id,
                mode=runner_id,
                declared_shards=handle.shards,
                spec_sha256=spec_sha256,
            )
        return recorded

    # -- ingestion (§5) ---------------------------------------------------------

    def ingest(
        self,
        bundle_path: str | Path,
        *,
        run_id: str | None = None,
        dispatch_id: str | None = None,
        shard: str | None = None,
        verify_sha: bool = True,
    ) -> IngestReport:
        """Ingest a results bundle. ``verify_sha=False`` is the explicit
        human-attach override (quarantine attach): the marker's spec hash is
        no longer trusted evidence once a person has decided the binding."""
        bundle = Path(bundle_path)
        if not bundle.exists():
            raise IngestError(f"bundle not found: {bundle}", reason="bad-bundle")
        warnings: list[str] = []

        marker = self._read_marker(bundle)
        if marker is None and run_id is None:
            raise IngestError(
                f"bundle has no {MARKER_FILENAME} marker and no run id was given — "
                "unsolicited bundles are not auto-attached (DESIGN.md §4/§5); "
                "quarantine it, or promote explicitly (--allow-unsolicited)",
                reason="unsolicited",
            )
        marker = marker or {}
        if run_id and marker.get("run_id") and marker["run_id"] != run_id:
            raise IngestError(
                f"run id {run_id} contradicts the bundle marker's run_id {marker['run_id']!r}",
                reason="marker-conflict",
            )
        resolved_run_id = run_id or marker.get("run_id")
        run = self.store.get_run(resolved_run_id)
        if run is None:
            raise IngestError(
                f"unknown run id {resolved_run_id!r} — refusing bundle", reason="unknown-run"
            )

        dispatch = self._resolve_dispatch(run, dispatch_id or marker.get("dispatch_id"), warnings)
        if verify_sha:
            self._verify_marker_sha(marker, dispatch, warnings)

        parsed = self._parse_results(bundle, self._expected_format(resolved_run_id))
        verdicts, resolve_warnings = self._correlate(parsed)
        warnings.extend(resolve_warnings)

        resolved_shard = shard or marker.get("shard") or "1"
        outcome = self.store.record_delivery(
            resolved_run_id,
            dispatch_id=dispatch.dispatch_id if dispatch else None,
            shard=str(resolved_shard),
            content_hash=bundle_content_hash(bundle),
            format=self._expected_format(resolved_run_id),
            verdicts=verdicts,
        )
        if outcome != "duplicate":
            self._recompute_completion(resolved_run_id, dispatch)
        after = self.store.get_run(resolved_run_id)
        assert after is not None
        return IngestReport(
            run_id=resolved_run_id,
            dispatch_id=dispatch.dispatch_id if dispatch else None,
            shard=str(resolved_shard),
            outcome=outcome,
            verdict_count=len(verdicts),
            run_state=after.state,
            completion=after.completion,
            warnings=warnings,
        )

    def ingest_or_quarantine(
        self,
        bundle_path: str | Path,
        *,
        transport: str,
        run_id: str | None = None,
        dispatch_id: str | None = None,
        shard: str | None = None,
    ) -> IngestReport | QuarantineReport:
        """The transport entry point (file-drop, push): a refused bundle is
        never dropped on the floor — it lands in the quarantine inbox
        (DESIGN.md §4/§5) instead of silently entering a watched run."""
        bundle = Path(bundle_path)
        try:
            return self.ingest(bundle, run_id=run_id, dispatch_id=dispatch_id, shard=shard)
        except IngestError as exc:
            if exc.reason not in QUARANTINE_REASONS:
                raise
            marker = {}
            try:
                marker = self._read_marker(bundle) or {}
            except IngestError:
                pass
            entry = self.quarantine.add(
                bundle,
                reason=exc.reason,
                transport=transport,
                content_hash=bundle_content_hash(bundle),
                claimed_run_id=run_id or marker.get("run_id"),
            )
            return QuarantineReport(
                entry_id=entry.entry_id if entry else None,
                reason=exc.reason,
                detail=str(exc),
            )

    def attach_quarantined(self, entry_id: str, run_id: str) -> IngestReport:
        """Explicit human action: bind a quarantined delivery to an existing
        run. Normal §5 delivery semantics apply; the marker's spec hash is not
        re-verified — the human binding overrides it."""
        bundle = self.quarantine.bundle_path(entry_id)
        report = self.ingest(bundle, run_id=run_id, verify_sha=False)
        self.quarantine.remove(entry_id)
        return report

    def promote_quarantined(self, entry_id: str) -> IngestReport:
        """Explicit human action: promote a quarantined delivery to its own
        run with origin 'ingested' (DESIGN.md §4)."""
        entry = self.quarantine.get(entry_id)
        report = self.promote_bundle(
            self.quarantine.bundle_path(entry_id),
            format_id=entry.format,
            claimed_run_id=entry.claimed_run_id,
        )
        self.quarantine.remove(entry_id)
        return report

    def promote_bundle(
        self,
        bundle_path: str | Path,
        *,
        format_id: str = REFERENCE_FORMAT,
        claimed_run_id: str | None = None,
    ) -> IngestReport:
        """Create an `origin: ingested` run from an unsolicited bundle."""
        bundle = Path(bundle_path)
        parsed = self._parse_results(bundle, format_id)
        verdicts, warnings = self._correlate(parsed)
        if not verdicts:
            raise IngestError(
                "bundle contains no verdicts resolvable against the catalog — cannot promote",
                reason="bad-bundle",
            )
        labels = {"transport": "promoted"}
        if claimed_run_id:
            labels["claimed_run_id"] = claimed_run_id
        spec = build_spec(
            title="Ingested delivery",
            materialized_ids=list(dict.fromkeys(v.item_id for v in verdicts)),
            source_provider=self.source.provider_id,
            snapshot=self.source.snapshot(),
            results={"expect": [{"format": format_id}], "shards": 1, "deliver": "none"},
            labels=labels,
        )
        run = self.store.create_run(spec, origin="ingested")
        outcome = self.store.record_delivery(
            run.id,
            dispatch_id=None,
            shard="1",
            content_hash=bundle_content_hash(bundle),
            format=format_id,
            verdicts=verdicts,
        )
        self._recompute_completion(run.id, None)
        after = self.store.get_run(run.id)
        assert after is not None
        return IngestReport(
            run_id=run.id,
            dispatch_id=None,
            shard="1",
            outcome=outcome,
            verdict_count=len(verdicts),
            run_state=after.state,
            completion=after.completion,
            warnings=warnings,
        )

    def verify_ingest_token(self, run_id: str, token: str | None) -> str:
        """§5 security floor. Returns 'ok' | 'disabled' | 'missing' | 'wrong'."""
        if self.config.ingestion.get("tokens") == "disabled":
            return "disabled"
        stored = self.store.get_ingest_token_sha256(run_id)
        if stored is None:
            # Composed while tokens were disabled: nothing to check against.
            return "disabled"
        if not token:
            return "missing"
        if hashlib.sha256(token.encode()).hexdigest() != stored:
            return "wrong"
        return "ok"

    def gc(self) -> dict[str, Any]:
        """§5/§6.4 housekeeping: bound the quarantine, expire processed inbox
        entries and artifacts, prune old runs from the store."""
        import shutil
        import time

        retention = self.config.retention
        max_age_days = retention["max_age_days"]
        cutoff_epoch = time.time() - max_age_days * 86400
        report: dict[str, Any] = {
            "quarantine_removed": self.quarantine.prune(self.config.ingestion["quarantine_max"]),
            "inbox_processed_removed": 0,
            "artifacts_removed": 0,
        }
        inbox = self.config.inbox_dir
        processed = inbox / "processed" if inbox else None
        if processed and processed.is_dir():
            for child in processed.iterdir():
                if child.stat().st_mtime < cutoff_epoch:
                    shutil.rmtree(child, ignore_errors=True)
                    report["inbox_processed_removed"] += 1
        artifacts = self.config.artifact_dir
        if artifacts.is_dir():
            for file in artifacts.rglob("*"):
                if file.is_file() and file.stat().st_mtime < cutoff_epoch:
                    file.unlink(missing_ok=True)
                    report["artifacts_removed"] += 1
            for directory in sorted(artifacts.rglob("*"), reverse=True):
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
        cutoff_iso = datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        report["runs_removed"] = self.store.prune_runs(
            before=cutoff_iso, max_runs=retention.get("max_runs")
        )
        return report

    def export_ctrf(self, run_id: str) -> dict[str, Any]:
        """§14 P3: normalized cross-tool result document (CTRF) — fitting for
        a project whose thesis is document-shaped interfaces (§5)."""
        run = self._require_run(run_id)
        dispatch = run.dispatches[-1] if run.dispatches else None
        verdicts = self.store.verdicts_for(run_id, dispatch.dispatch_id if dispatch else None)
        status_map = {"PASS": "passed", "FAIL": "failed", "SKIP": "skipped", "ERROR": "other"}
        tests = []
        for verdict in verdicts:
            test: dict[str, Any] = {
                "name": verdict.item_id,
                "status": status_map[verdict.status],
                "duration": verdict.duration_ms,
            }
            if verdict.message:
                test["message"] = verdict.message
            tests.append(test)

        def _epoch_ms(iso: str | None) -> int:
            if not iso:
                return 0
            return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
                       .replace(tzinfo=timezone.utc).timestamp() * 1000)

        counts = {name: 0 for name in ("passed", "failed", "skipped", "pending", "other")}
        for test in tests:
            counts[test["status"]] += 1
        return {
            "results": {
                "tool": {"name": "runcomposer"},
                "summary": {
                    "tests": len(tests),
                    **counts,
                    "start": _epoch_ms(run.created_at),
                    "stop": _epoch_ms(run.completed_at),
                },
                "tests": tests,
                "extra": {"run_id": run.id, "state": run.state, "completion": run.completion},
            }
        }

    def finalize(self, run_id: str) -> RunRecord:
        run = self._require_run(run_id)
        dispatch = run.dispatches[-1] if run.dispatches else None
        verdicts = self.store.verdicts_for(run_id, dispatch.dispatch_id if dispatch else None)
        self.store.set_run_state(
            run_id, "COMPLETE", completion=completion_of(verdicts), completed_at=_utc_now()
        )
        refreshed = self.store.get_run(run_id)
        assert refreshed is not None
        return refreshed

    # -- misc surfaces -----------------------------------------------------------

    def taxonomy(self) -> dict[str, Any]:
        return yaml.safe_load(self.config.taxonomy_source.read_text(encoding="utf-8"))

    def runner_infos(self) -> list[dict[str, Any]]:
        infos = []
        for runner_id in self.config.configured_runner_ids():
            try:
                info = self.config.build_runner(runner_id).describe()
                infos.append({"id": info.id, "capabilities": list(info.capabilities)})
            except Exception as exc:  # config names an unloadable runner: surface, don't hide
                infos.append({"id": runner_id, "error": str(exc)})
        return infos

    def verdict_summary(self, run_id: str, dispatch_id: str | None = None) -> dict[str, int]:
        verdicts = self.store.verdicts_for(run_id, dispatch_id)
        summary: dict[str, int] = {}
        for verdict in verdicts:
            summary[verdict.status] = summary.get(verdict.status, 0) + 1
        return summary

    # -- internals ----------------------------------------------------------------

    def _require_run(self, run_id: str) -> RunRecord:
        run = self.store.get_run(run_id)
        if run is None:
            raise ServiceError(f"unknown run id {run_id!r}")
        return run

    def _require_spec(self, run_id: str) -> dict[str, Any]:
        spec = self.store.get_spec_document(run_id)
        if spec is None:
            raise ServiceError(f"unknown run id {run_id!r}")
        return spec

    def _expected_format(self, run_id: str) -> str:
        spec = self._require_spec(run_id)
        expected = spec.get("results", {}).get("expect") or []
        return expected[0]["format"] if expected else REFERENCE_FORMAT

    @staticmethod
    def _read_marker(bundle: Path) -> dict[str, Any] | None:
        marker_path = bundle / MARKER_FILENAME if bundle.is_dir() else None
        if marker_path is None or not marker_path.is_file():
            return None
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise IngestError(
                f"{marker_path}: marker is not valid JSON: {exc}", reason="bad-bundle"
            ) from None
        if not isinstance(marker, dict) or not marker.get("run_id"):
            raise IngestError(
                f"{marker_path}: marker must be an object with a run_id", reason="bad-bundle"
            )
        return marker

    def _resolve_dispatch(self, run: RunRecord, dispatch_id: str | None, warnings: list[str]):
        if dispatch_id:
            for dispatch in run.dispatches:
                if dispatch.dispatch_id == dispatch_id:
                    return dispatch
            raise IngestError(f"unknown dispatch id {dispatch_id!r} for run {run.id}")
        if run.dispatches:
            return run.dispatches[-1]
        warnings.append("run has no dispatch — delivery attached to the run directly")
        return None

    @staticmethod
    def _verify_marker_sha(marker: Mapping[str, Any], dispatch, warnings: list[str]) -> None:
        marker_sha = marker.get("spec_sha256")
        if not marker_sha:
            return
        if dispatch is None or not dispatch.spec_sha256:
            warnings.append(
                "marker carries spec_sha256 but the dispatch has no recorded spec hash — "
                "verification skipped"
            )
            return
        if marker_sha != dispatch.spec_sha256:
            raise IngestError(
                "marker spec_sha256 does not match the spec bytes exported for dispatch "
                f"{dispatch.dispatch_id} — refusing bundle (§5 marker verification)",
                reason="sha-mismatch",
            )

    def _parse_results(self, bundle: Path, format_id: str) -> list[ParsedVerdict]:
        try:
            parser_cls = resolve_plugin(format_id, PARSER_GROUP)
        except PluginError as exc:
            raise IngestError(
                f"no ResultParser registered for format {format_id!r}: {exc}", reason="bad-bundle"
            ) from exc
        parsed = parser_cls().parse(bundle)
        if not parsed:
            raise IngestError(f"bundle contains no {format_id!r} results: {bundle}", reason="bad-bundle")
        return parsed

    def _correlate(self, parsed: list[ParsedVerdict]) -> tuple[list[Verdict], list[str]]:
        """Native name → item id, always through TestSource.resolve (§5)."""
        verdicts: list[Verdict] = []
        warnings: list[str] = []
        for entry in parsed:
            item_id = self.source.resolve(entry.native_name)
            if item_id is None:
                warnings.append(f"unknown native name {entry.native_name!r} — verdict skipped")
                continue
            verdicts.append(
                Verdict(
                    item_id=item_id,
                    status=entry.status,
                    duration_ms=entry.duration_ms,
                    message=entry.message,
                    attempt=entry.attempt,
                )
            )
        return verdicts, warnings

    def _recompute_completion(self, run_id: str, dispatch) -> None:
        dispatch_id = dispatch.dispatch_id if dispatch else None
        declared = dispatch.declared_shards if dispatch else 1
        delivered = self.store.delivered_shards(run_id, dispatch_id)
        if all_shards_delivered(declared, delivered):
            verdicts = self.store.verdicts_for(run_id, dispatch_id)
            self.store.set_run_state(
                run_id, "COMPLETE", completion=completion_of(verdicts), completed_at=_utc_now()
            )
        else:
            self.store.set_run_state(run_id, "AWAITING_RESULTS")
