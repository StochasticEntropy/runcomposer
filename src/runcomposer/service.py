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
from runcomposer.core.ports import ParsedVerdict
from runcomposer.core.registry import PARSER_GROUP, PluginError, resolve_plugin
from runcomposer.core.selection import Selection
from runcomposer.core.spec import build_spec, validate_document

__all__ = ["ComposeResult", "IngestError", "IngestReport", "Service", "ServiceError", "MARKER_FILENAME"]

MARKER_FILENAME = "runcomposer_run.json"
REFERENCE_FORMAT = "runcomposer-verdicts"


class ServiceError(ValueError):
    """Raised for request-level errors (unknown run, bad input)."""


class IngestError(ValueError):
    """Raised when a bundle is refused (marker mismatch, unknown run)."""


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    @cached_property
    def store(self):
        return self.config.build_store()

    @cached_property
    def source(self):
        return self.config.build_source()

    # -- selection & compose -------------------------------------------------

    def preview(self, selection_data: Mapping[str, Any]) -> tuple[list[Item], list[str]]:
        selection = Selection.from_data(selection_data)
        items = selection.compile(self.source.items())
        warnings = []
        if not items:
            warnings.append("selection matched 0 items")
        return items, warnings

    def build_spec_document(
        self,
        selection_data: Mapping[str, Any],
        *,
        title: str,
        labels: Mapping[str, str] | None = None,
        derived_from: list[Mapping[str, Any]] | None = None,
        mint_token: bool = False,
    ) -> ComposeResult:
        items, warnings = self.preview(selection_data)
        results: dict[str, Any] = {
            "expect": [{"format": REFERENCE_FORMAT}],
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
    ) -> ComposeResult:
        result = self.build_spec_document(selection_data, title=title, labels=labels, mint_token=True)
        token = result.spec.get("results", {}).get("token")
        token_sha = hashlib.sha256(token.encode()).hexdigest() if token else None
        result.run = self.store.create_run(result.spec, origin=origin, ingest_token_sha256=token_sha)
        return result

    # -- dispatch --------------------------------------------------------------

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
        """In-process dispatch: hand the document to a Runner plugin. A runner
        that delivers synchronously (the demo runner) gets its deliveries
        recorded immediately; real transports arrive with P2."""
        spec = self._require_spec(run_id)
        runner = self.config.build_runner(runner_id)
        handle = runner.dispatch(spec)
        dispatch = self.store.add_dispatch(
            run_id,
            dispatch_id=handle.dispatch_id,
            mode=runner_id,
            declared_shards=handle.shards,
        )
        self.store.set_run_state(run_id, "DISPATCHED")
        for delivery in getattr(runner, "deliveries", []):
            verdicts = delivery["verdicts"]
            payload = json.dumps(
                [(v.item_id, v.status, v.duration_ms, v.attempt) for v in verdicts]
            ).encode()
            self.store.record_delivery(
                run_id,
                dispatch_id=handle.dispatch_id,
                shard=delivery.get("shard", "1"),
                content_hash="sha256:" + hashlib.sha256(payload).hexdigest(),
                format=REFERENCE_FORMAT,
                verdicts=verdicts,
            )
        self._recompute_completion(run_id, dispatch)
        return dispatch

    # -- ingestion (§5) ---------------------------------------------------------

    def ingest(
        self,
        bundle_path: str | Path,
        *,
        run_id: str | None = None,
        dispatch_id: str | None = None,
        shard: str | None = None,
    ) -> IngestReport:
        bundle = Path(bundle_path)
        if not bundle.exists():
            raise IngestError(f"bundle not found: {bundle}")
        warnings: list[str] = []

        marker = self._read_marker(bundle)
        if marker is None and run_id is None:
            raise IngestError(
                f"bundle has no {MARKER_FILENAME} marker and no --run was given — "
                "unsolicited bundles are not auto-attached (DESIGN.md §4/§5; "
                "the quarantine inbox arrives with P2)"
            )
        marker = marker or {}
        if run_id and marker.get("run_id") and marker["run_id"] != run_id:
            raise IngestError(
                f"--run {run_id} contradicts the bundle marker's run_id {marker['run_id']!r}"
            )
        resolved_run_id = run_id or marker.get("run_id")
        run = self.store.get_run(resolved_run_id)
        if run is None:
            raise IngestError(f"unknown run id {resolved_run_id!r} — refusing bundle")

        dispatch = self._resolve_dispatch(run, dispatch_id or marker.get("dispatch_id"), warnings)
        self._verify_marker_sha(marker, dispatch, warnings)

        parsed = self._parse_results(resolved_run_id, bundle)
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
            raise IngestError(f"{marker_path}: marker is not valid JSON: {exc}") from None
        if not isinstance(marker, dict) or not marker.get("run_id"):
            raise IngestError(f"{marker_path}: marker must be an object with a run_id")
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
                f"{dispatch.dispatch_id} — refusing bundle (§5 marker verification)"
            )

    def _parse_results(self, run_id: str, bundle: Path) -> list[ParsedVerdict]:
        format_id = self._expected_format(run_id)
        try:
            parser_cls = resolve_plugin(format_id, PARSER_GROUP)
        except PluginError as exc:
            raise IngestError(f"no ResultParser registered for format {format_id!r}: {exc}") from exc
        parsed = parser_cls().parse(bundle)
        if not parsed:
            raise IngestError(f"bundle contains no {format_id!r} results: {bundle}")
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
