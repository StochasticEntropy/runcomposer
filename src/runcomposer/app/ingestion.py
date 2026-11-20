"""Result ingestion (DESIGN.md §5): one pipeline for every way results come
home — a runner's own delivery, an API push, the file-drop inbox, a manual
``ingest``.

The pipeline is: find the run and the dispatch → verify the marker → parse
with the configured ResultParser → correlate every native name through the
catalog → record the delivery (idempotent per run, dispatch and shard) → let
the ledger recompute completion.

Anything that goes wrong on the way is an :class:`IngestError` with a
``reason`` — including a parser that raised something unexpected — so a
transport can always do the same thing with a bundle it cannot take in:
quarantine it and move on.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from runcomposer.core import events as kinds
from runcomposer.core.errors import (
    AuthRequired,
    ConfigError,
    Forbidden,
    IngestError,
    ParseError,
    PluginError,
    UnknownRun,
)
from runcomposer.core.hashing import bundle_hash, sha256_hex, verdicts_hash
from runcomposer.core.lifecycle import ACTIVE_STATES
from runcomposer.core.marker import read_marker
from runcomposer.core.model import REPORTABLE_STATUSES, DispatchRecord, RunRecord, Verdict
from runcomposer.core.registry import PARSER_GROUP, load_entry_point
from runcomposer.core.spec import build_spec

from .quarantine import QuarantineEntry

if TYPE_CHECKING:
    from .service import Service

__all__ = [
    "REFERENCE_FORMAT",
    "IngestReport",
    "Ingestion",
    "QuarantineReport",
    "expected_format",
]

log = logging.getLogger("runcomposer.ingestion")

REFERENCE_FORMAT = "runcomposer-verdicts"
TOKEN_PREFIX = "rct_"


@dataclass
class IngestReport:
    run_id: str
    dispatch_id: str
    shard: str
    outcome: str  # new | duplicate | replaced
    verdict_count: int
    run_state: str
    completion: str | None
    warnings: list[str] = field(default_factory=list)


@dataclass
class QuarantineReport:
    """A transport delivery that was quarantined instead of ingested.
    ``entry_id`` is None when an identical bundle was already quarantined."""

    entry_id: str | None
    reason: str
    detail: str


def expected_format(spec: Mapping[str, Any] | None) -> str:
    """The result format a run expects back (``results.expect[0].format``)."""
    expect = ((spec or {}).get("results") or {}).get("expect") or []
    if expect and isinstance(expect[0], Mapping) and expect[0].get("format"):
        return str(expect[0]["format"])
    return REFERENCE_FORMAT


class Ingestion:
    def __init__(self, service: Service):
        self._service = service
        self._parsers: dict[str, Any] = {}
        self._lock = threading.Lock()

    # -- parsers -----------------------------------------------------------------

    def parser(self, format_id: str) -> Any:
        """The ResultParser for a format id: the ``parsers:`` entry of that
        name if there is one, else the entry point of that name.

        A format id is a *name*. It is never read as an import path — a run
        spec is a document that travels, and a document must not be able to
        make this process import a module of its choosing.
        """
        if not isinstance(format_id, str) or not format_id or ":" in format_id or "/" in format_id:
            raise IngestError(
                f"{format_id!r} is not a result format id — formats are named, configured "
                "under 'parsers:' or registered as entry points, never given as import paths",
                reason="bad-bundle",
            )
        with self._lock:
            if format_id not in self._parsers:
                config = self._service.config
                try:
                    spec = config.plugin_spec("parsers", format_id)
                    if spec is not None:
                        self._parsers[format_id] = config.build(spec)
                    else:
                        self._parsers[format_id] = load_entry_point(PARSER_GROUP, format_id)()
                except PluginError as exc:
                    raise IngestError(
                        f"no ResultParser for format {format_id!r}: {exc}", reason="bad-bundle"
                    ) from exc
                except ConfigError:
                    raise
            return self._parsers[format_id]

    def parse(self, bundle: Path, format_id: str) -> tuple[list[Verdict], list[str]]:
        """Parse and correlate. Native name → item id always goes through
        ``TestSource.resolve`` (§5); names the catalog does not know are
        reported as warnings, never invented as ids."""
        parser = self.parser(format_id)
        try:
            parsed = list(parser.parse(bundle))
        except ParseError as exc:
            raise IngestError(str(exc), reason="bad-bundle") from exc
        except IngestError:
            raise
        except Exception as exc:  # a parser bug is still a bundle we cannot take in
            log.warning("parser %r raised on %s", format_id, bundle, exc_info=True)
            raise IngestError(
                f"{bundle}: the {format_id!r} parser failed: {type(exc).__name__}: {exc}",
                reason="bad-bundle",
            ) from exc
        if not parsed:
            raise IngestError(
                f"bundle contains no {format_id!r} results: {bundle}", reason="bad-bundle"
            )
        catalog = self._service.catalog
        verdicts: list[Verdict] = []
        warnings: list[str] = []
        for entry in parsed:
            if entry.status not in REPORTABLE_STATUSES:
                raise IngestError(
                    f"{bundle}: result {entry.native_name!r} carries status {entry.status!r} — "
                    f"an executor reports one of {REPORTABLE_STATUSES}",
                    reason="bad-bundle",
                )
            item_id = catalog.resolve(entry.native_name)
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

    # -- the pipeline ---------------------------------------------------------------

    def ingest(
        self,
        bundle_path: str | Path,
        *,
        run_id: str | None = None,
        dispatch_id: str | None = None,
        shard: str | None = None,
        format: str | None = None,
        verify_sha: bool = True,
    ) -> IngestReport:
        """Ingest a results bundle. ``verify_sha=False`` is the explicit
        human-attach override: once a person decided the binding, the
        marker's spec hash is no longer the evidence."""
        bundle = Path(bundle_path)
        if not bundle.exists():
            raise IngestError(f"bundle not found: {bundle}", reason="bad-bundle")
        warnings: list[str] = []

        marker = read_marker(bundle)
        if marker is None and run_id is None:
            raise IngestError(
                "bundle has no runcomposer_run.json marker and no run id was given — "
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
        resolved_run_id = str(run_id or marker.get("run_id"))
        store = self._service.store
        run: RunRecord | None = store.get_run(resolved_run_id)
        if run is None:
            raise IngestError(
                f"unknown run id {resolved_run_id!r} — refusing bundle", reason="unknown-run"
            )
        if dispatch_id and marker.get("dispatch_id") and marker["dispatch_id"] != dispatch_id:
            raise IngestError(
                f"dispatch id {dispatch_id} contradicts the bundle marker's dispatch_id "
                f"{marker['dispatch_id']!r}",
                reason="marker-conflict",
            )
        resolved_shard = str(shard or marker.get("shard") or "1")
        spec = store.get_spec_document(resolved_run_id)
        format_id = format or expected_format(spec)

        dispatch = self._resolve_dispatch(run, dispatch_id or marker.get("dispatch_id"))
        if dispatch is not None and verify_sha:
            self._verify_marker_sha(marker, dispatch, warnings)

        # Parse before anything is written: a bundle that cannot be read
        # leaves no trace on the run.
        verdicts, parse_warnings = self.parse(bundle, format_id)
        warnings.extend(parse_warnings)

        if dispatch is None:
            # Results for a run nothing was dispatched for: they still belong
            # to a dispatch, so one is opened for them.
            dispatch = self._service.ledger.open(
                resolved_run_id, "ingest", state="AWAITING_RESULTS", shards=[resolved_shard]
            )
            warnings.append("run had no dispatch — an 'ingest' dispatch was opened for the delivery")
        outcome = self.record(
            dispatch,
            resolved_shard,
            verdicts,
            content_hash=bundle_hash(bundle),
            format_id=format_id,
            warnings=warnings,
        )
        after = store.get_run(resolved_run_id)
        assert after is not None
        return IngestReport(
            run_id=resolved_run_id,
            dispatch_id=dispatch.dispatch_id,
            shard=resolved_shard,
            outcome=outcome,
            verdict_count=len(verdicts),
            run_state=after.state,
            completion=after.completion,
            warnings=warnings,
        )

    def record(
        self,
        dispatch: DispatchRecord,
        shard: str,
        verdicts: Sequence[Verdict],
        *,
        content_hash: str | None = None,
        format_id: str,
        warnings: list[str] | None = None,
    ) -> str:
        """Record one shard's delivery and let the ledger recompute. The
        single write path for results, whoever brings them."""
        service = self._service
        if dispatch.state == "CANCELLED":
            raise IngestError(
                f"dispatch {dispatch.dispatch_id} was cancelled — results for it are not taken in",
                reason="dispatch-cancelled",
            )
        notes = warnings if warnings is not None else []
        if shard not in dispatch.shards:
            notes.append(
                f"shard {shard!r} was not declared by dispatch {dispatch.dispatch_id} "
                f"(declared: {list(dispatch.shards)}) — recorded, but it completes nothing"
            )
        planned = set(service.ledger.planned_ids(dispatch.run_id))
        unplanned = sorted({v.item_id for v in verdicts if v.item_id not in planned})
        if unplanned:
            notes.append(
                f"{len(unplanned)} reported item(s) were not planned for this run "
                f"(first: {unplanned[0]!r})"
            )
        outcome: str = service.store.record_delivery(
            dispatch.run_id,
            dispatch_id=dispatch.dispatch_id,
            shard=shard,
            content_hash=content_hash or verdicts_hash(verdicts),
            format=format_id,
            verdicts=verdicts,
        )
        for note in notes:
            service.bus.emit(
                dispatch.run_id,
                kinds.WARNING,
                dispatch_id=dispatch.dispatch_id,
                data={"message": note, "shard": shard},
            )
        if outcome != "duplicate":
            service.bus.emit(
                dispatch.run_id,
                kinds.DELIVERY_RECORDED,
                dispatch_id=dispatch.dispatch_id,
                data={
                    "shard": shard,
                    "outcome": outcome,
                    "verdicts": len(verdicts),
                    "format": format_id,
                },
            )
            service.ledger.recompute(dispatch.dispatch_id)
        return outcome

    def ingest_or_quarantine(
        self,
        bundle_path: str | Path,
        *,
        transport: str,
        run_id: str | None = None,
        dispatch_id: str | None = None,
        shard: str | None = None,
        format: str | None = None,
    ) -> IngestReport | QuarantineReport:
        """The transport entry point (file-drop, push): a bundle that cannot
        be taken in is never dropped on the floor and never raises — it lands
        in the quarantine with the reason."""
        bundle = Path(bundle_path)
        try:
            return self.ingest(
                bundle, run_id=run_id, dispatch_id=dispatch_id, shard=shard, format=format
            )
        except Exception as exc:
            reason = exc.reason if isinstance(exc, IngestError) else "bad-bundle"
            if not isinstance(exc, IngestError):
                log.exception("ingestion of %s failed unexpectedly", bundle)
            claimed = run_id
            if claimed is None:
                try:
                    claimed = (read_marker(bundle) or {}).get("run_id")
                except IngestError:
                    claimed = None
            entry = self._quarantine(
                bundle, reason=reason, transport=transport, claimed_run_id=claimed,
                format=format, detail=str(exc),
            )
            return QuarantineReport(
                entry_id=entry.entry_id if entry else None, reason=reason, detail=str(exc)
            )

    def _quarantine(
        self,
        bundle: Path,
        *,
        reason: str,
        transport: str,
        claimed_run_id: str | None,
        format: str | None,
        detail: str,
    ) -> QuarantineEntry | None:
        if format is None and claimed_run_id:
            spec = self._service.store.get_spec_document(claimed_run_id)
            format = expected_format(spec) if spec else None
        return self._service.quarantine.add(
            bundle,
            reason=reason,
            transport=transport,
            content_hash=bundle_hash(bundle),
            claimed_run_id=claimed_run_id,
            format=format,
            detail=detail,
        )

    # -- quarantine actions: explicit, logged, human ------------------------------------

    def attach_quarantined(
        self, entry_id: str, run_id: str, *, format: str | None = None
    ) -> IngestReport:
        """Bind a quarantined delivery to an existing run. Normal delivery
        semantics apply; the marker's spec hash is not re-verified — the
        human binding overrides it — and the action is recorded on the run."""
        service = self._service
        entry = service.quarantine.get(entry_id)
        if service.store.get_run(run_id) is None:
            raise UnknownRun(run_id)
        report = self._attach(entry, run_id, format)
        service.quarantine.remove(entry_id)
        service.bus.emit(
            run_id,
            kinds.DELIVERY_ATTACHED,
            dispatch_id=report.dispatch_id,
            data={
                "quarantine_entry": entry_id,
                "reason": entry.reason,
                "transport": entry.transport,
                "claimed_run_id": entry.claimed_run_id,
                "shard": report.shard,
                "message": "quarantined delivery attached by an explicit action",
            },
        )
        return report

    def _attach(self, entry: QuarantineEntry, run_id: str, format: str | None) -> IngestReport:
        bundle = self._service.quarantine.bundle_path(entry.entry_id)
        try:
            marker = read_marker(bundle) or {}
        except IngestError:
            marker = {}
        # The person binding it overrides what the marker claims about run
        # and dispatch; the shard label is the bundle's own.
        return self._ingest_as(bundle, run_id, shard=marker.get("shard"), format=format)

    def _ingest_as(
        self, bundle: Path, run_id: str, *, shard: str | None, format: str | None
    ) -> IngestReport:
        store = self._service.store
        run = store.get_run(run_id)
        assert run is not None
        resolved_shard = str(shard or "1")
        format_id = format or expected_format(store.get_spec_document(run_id))
        warnings: list[str] = []
        verdicts, parse_warnings = self.parse(bundle, format_id)
        warnings.extend(parse_warnings)
        dispatch = run.latest_dispatch
        if dispatch is None:
            dispatch = self._service.ledger.open(
                run_id, "ingest", state="AWAITING_RESULTS", shards=[resolved_shard]
            )
        outcome = self.record(
            dispatch, resolved_shard, verdicts, content_hash=bundle_hash(bundle),
            format_id=format_id, warnings=warnings,
        )
        after = store.get_run(run_id)
        assert after is not None
        return IngestReport(
            run_id=run_id,
            dispatch_id=dispatch.dispatch_id,
            shard=resolved_shard,
            outcome=outcome,
            verdict_count=len(verdicts),
            run_state=after.state,
            completion=after.completion,
            warnings=warnings,
        )

    def promote_quarantined(self, entry_id: str, *, format: str | None = None) -> IngestReport:
        """Promote a quarantined delivery to its own run, origin
        ``ingested`` (DESIGN.md §4)."""
        quarantine = self._service.quarantine
        entry = quarantine.get(entry_id)
        format_id = format or entry.format
        if not format_id:
            raise IngestError(
                f"quarantine entry {entry_id} does not say which result format it is in — "
                "name the format to promote it (`--format` on the CLI, `format` in the API)",
                reason="bad-bundle",
            )
        report = self.promote_bundle(
            quarantine.bundle_path(entry_id),
            format_id=format_id,
            claimed_run_id=entry.claimed_run_id,
        )
        quarantine.remove(entry_id)
        return report

    def promote_bundle(
        self,
        bundle_path: str | Path,
        *,
        format_id: str = REFERENCE_FORMAT,
        claimed_run_id: str | None = None,
    ) -> IngestReport:
        """Create an ``origin: ingested`` run from an unsolicited bundle. Its
        planned set is what the bundle reported."""
        service = self._service
        bundle = Path(bundle_path)
        verdicts, warnings = self.parse(bundle, format_id)
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
            source_provider=service.catalog.provider_id,
            snapshot=service.catalog.snapshot(),
            results={"expect": [{"format": format_id}], "shards": 1, "deliver": "none"},
            labels=labels,
        )
        run = service.store.create_run(spec, origin="ingested")
        service.bus.emit(run.id, kinds.RUN_COMPOSED, data={"title": run.title, "origin": "ingested"})
        dispatch = service.ledger.open(run.id, "ingest", state="AWAITING_RESULTS", shards=["1"])
        outcome = self.record(
            dispatch, "1", verdicts, content_hash=bundle_hash(bundle), format_id=format_id,
            warnings=warnings,
        )
        after = service.store.get_run(run.id)
        assert after is not None
        return IngestReport(
            run_id=run.id,
            dispatch_id=dispatch.dispatch_id,
            shard="1",
            outcome=outcome,
            verdict_count=len(verdicts),
            run_state=after.state,
            completion=after.completion,
            warnings=warnings,
        )

    # -- the ingest token (§5 security floor) -------------------------------------------

    def mint_token(self, run_id: str, dispatch_id: str | None) -> str | None:
        """Mint a token for a dispatch (or for the run). Only its hash is
        stored; the plaintext exists in the return value and nowhere else.
        ``None`` when the deployment disabled tokens."""
        if not self._service.config.tokens_required:
            return None
        token = TOKEN_PREFIX + secrets.token_urlsafe(24)
        self._service.store.set_ingest_token(run_id, dispatch_id, sha256_hex(token.encode("utf-8")))
        return token

    def authorize_push(self, run_id: str, token: str | None) -> str | None:
        """Check a push's token **before anything is read or written**.
        Returns the dispatch the token is bound to (``None`` for a run-level
        token, or when tokens are disabled)."""
        if not self._service.config.tokens_required:
            return None
        if not token:
            raise AuthRequired("an ingest token is required to push results (DESIGN.md §5)")
        presented = sha256_hex(token.encode("utf-8"))
        for stored, dispatch_id in self._service.store.ingest_tokens(run_id).items():
            if hmac.compare_digest(stored, presented):
                return dispatch_id
        raise Forbidden("the ingest token does not match this run")

    # -- internals ------------------------------------------------------------------------

    @staticmethod
    def _resolve_dispatch(run: RunRecord, dispatch_id: str | None) -> DispatchRecord | None:
        if dispatch_id:
            for dispatch in run.dispatches:
                if dispatch.dispatch_id == dispatch_id:
                    return dispatch
            raise IngestError(
                f"unknown dispatch id {dispatch_id!r} for run {run.id}", reason="unknown-dispatch"
            )
        # No dispatch named: the one still waiting for results, newest first;
        # failing that, the latest.
        for dispatch in reversed(run.dispatches):
            if dispatch.state in ACTIVE_STATES:
                return dispatch
        return run.latest_dispatch

    @staticmethod
    def _verify_marker_sha(
        marker: Mapping[str, Any], dispatch: DispatchRecord, warnings: list[str]
    ) -> None:
        marker_sha = marker.get("spec_sha256")
        if not marker_sha:
            return
        if not dispatch.spec_sha256:
            warnings.append(
                "marker carries spec_sha256 but the dispatch has no recorded spec hash — "
                "verification skipped"
            )
            return
        if marker_sha != dispatch.spec_sha256:
            raise IngestError(
                "marker spec_sha256 does not match the spec bytes handed out for dispatch "
                f"{dispatch.dispatch_id} — refusing bundle (§5 marker verification)",
                reason="sha-mismatch",
            )
