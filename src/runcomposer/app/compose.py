"""Composing (DESIGN.md §3, §7): from a selection to a stored run spec.

Preview, history-based selection, spec building, import of a document
composed elsewhere, and export — the hand-out of the exact document bytes for
somebody else to execute.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import yaml

from runcomposer.core import events as kinds
from runcomposer.core.errors import (
    InvalidRequest,
    RunActive,
    SpecConflict,
    UnknownRun,
)
from runcomposer.core.filtertext import read_filter_string
from runcomposer.core.hashing import canonical_json, sha256_hex
from runcomposer.core.lifecycle import ACTIVE_STATES, effective_verdicts
from runcomposer.core.model import DispatchRecord, Item, RunRecord
from runcomposer.core.parameters import validate_parameters
from runcomposer.core.selection import Selection
from runcomposer.core.spec import build_spec, validate_document
from runcomposer.core.timeutil import normalize_time

from .ingestion import REFERENCE_FORMAT

if TYPE_CHECKING:
    from .service import Service

__all__ = ["ComposeResult", "Composer", "ExportResult", "Preview"]

_HISTORY_VERDICTS = {
    "failed": "FAIL",
    "passed": "PASS",
    "skipped": "SKIP",
    "error": "ERROR",
    "missing": "MISSING",
}
_MEDIA_TYPES = {"json": "application/json", "yaml": "application/yaml"}


@dataclass
class Preview:
    items: list[Item]
    warnings: list[str]
    #: Provenance of compose-time providers (history) — which run it resolved to.
    derived_from: list[dict[str, Any]]
    catalog_size: int
    snapshot: str


@dataclass
class ComposeResult:
    spec: dict[str, Any]
    run: RunRecord | None = None
    items: list[Item] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ExportResult:
    """One export: the dispatch, the exact bytes handed out (their sha256 is
    the dispatch's ``spec_sha256``), and the ingest token — returned here and
    nowhere else; only its hash is stored."""

    dispatch: DispatchRecord
    content: bytes
    media_type: str
    token: str | None


class Composer:
    def __init__(self, service: Service):
        self._service = service

    # -- selection ------------------------------------------------------------------

    def preview(self, selection_data: Mapping[str, Any]) -> Preview:
        """Compile a selection against the catalog. Read-only and cheap: it
        touches the cached catalog and, for a history selection, the store —
        safe to call on every keystroke and from several clients at once."""
        catalog = self._service.catalog
        all_items = catalog.items()
        data, derived, warnings, emptied = self._prepare_selection(selection_data, all_items)
        items = [] if emptied else Selection.from_data(data).compile(all_items)
        if not items:
            warnings.append("selection matched 0 items")
        items = _planned_once(items, warnings)
        return Preview(
            items=items,
            warnings=warnings,
            derived_from=derived,
            catalog_size=len(all_items),
            snapshot=catalog.snapshot(),
        )

    # -- history-based selection (§7): a compose-time provider ------------------------

    def resolve_history(
        self, query: str, *, labels: Mapping[str, str] | None = None
    ) -> tuple[list[str], dict[str, Any]]:
        """Resolve ``<verdict>@<selector>`` into item ids plus the §3.1
        ``derived_from`` provenance entry.

        - verdict: ``failed``, ``passed``, ``skipped``, ``error``, ``missing``
          — or several joined with ``+`` (``failed+error+missing``);
        - selector: ``latest``, ``run:<id>``, ``before:<ISO time>``;
        - a **label scope** as a query-string suffix,
          ``failed@latest?suite=nightly&env=staging`` — without one, "latest"
          ranges over every completed run in the store. ``labels``
          is the programmatic equivalent and merges with the inline scope; a
          key given twice with different values is refused.

        The RUN is selected by completion, the ITEMS by their effective
        verdict in that run's latest dispatch.
        """
        if not isinstance(query, str):
            raise InvalidRequest(f"history query must be a string, got {type(query).__name__}")
        verdict_text, sep, selector_text = query.partition("@")
        words = verdict_text.split("+") if verdict_text else []
        statuses = [_HISTORY_VERDICTS.get(word) for word in words]
        if not sep or not statuses or None in statuses:
            raise InvalidRequest(
                f"history query must be '<verdict>@<selector>' with verdict in "
                f"{sorted(_HISTORY_VERDICTS)} (several joined by '+'), got {query!r}"
            )
        wanted = [status for status in statuses if status]
        selector, scope = _split_history_scope(selector_text, query)
        for key, value in (labels or {}).items():
            if key in scope and scope[key] != value:
                raise InvalidRequest(
                    f"label scope {key!r} is {scope[key]!r} in the history query {query!r} but "
                    f"{value!r} was passed alongside it — refusing to guess which one is meant"
                )
            scope[key] = value

        store = self._service.store
        run: RunRecord | None
        if selector == "latest":
            run = store.latest_completed_run(labels=scope or None)
            query_doc: dict[str, Any] = {"run": "LATEST", "verdicts": wanted}
        elif selector.startswith("run:"):
            if scope:
                raise InvalidRequest(
                    f"history selector {selector!r} already names one run, so the label scope "
                    f"{scope!r} could only be ignored — drop one of the two"
                )
            run = store.get_run(selector[len("run:"):])
            query_doc = {"run": selector[len("run:"):], "verdicts": wanted}
        elif selector.startswith("before:"):
            cutoff = normalize_time(selector[len("before:"):])
            run = store.latest_completed_run(completed_before=cutoff, labels=scope or None)
            query_doc = {"run": {"before": cutoff}, "verdicts": wanted}
        else:
            raise InvalidRequest(
                f"unknown history selector {selector!r} (expected latest, run:<id>, "
                "before:<time>, each optionally scoped with '?key=value&…')"
            )
        if scope:
            query_doc["labels"] = dict(scope)
        if run is None:
            scoped = f" scoped to labels {dict(scope)}" if scope else ""
            raise InvalidRequest(
                f"history selection {query!r} matched no completed run{scoped} — history "
                "features are dark on a fresh store (DESIGN.md §6.3)"
            )
        latest = run.latest_dispatch
        verdicts = (
            effective_verdicts(self._service.ledger.scored_verdicts(latest)) if latest else []
        )
        item_ids = list(dict.fromkeys(v.item_id for v in verdicts if v.status in wanted))
        provenance: dict[str, Any] = {
            "provider": "history",
            "query": query_doc,
            "resolved_run_id": run.id,
        }
        if run.completed_at:
            provenance["resolved_run_completed_at"] = run.completed_at
        if run.labels:
            provenance["resolved_run_labels"] = dict(run.labels)
        if run.title:
            provenance["resolved_run_title"] = run.title
        return item_ids, provenance

    def _prepare_selection(
        self, selection_data: Mapping[str, Any], all_items: list[Item]
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], bool]:
        """Resolve compose-time providers (history) into plain selection
        inputs + provenance, and a ``tag_filter`` given as text into its AST.
        The executed spec stays static (§7). The last
        value says the history narrowed the selection to nothing — which is
        an empty result, not "no explicit picks"."""
        if not isinstance(selection_data, Mapping):
            raise InvalidRequest(
                f"selection must be a mapping, got {type(selection_data).__name__}"
            )
        data = dict(selection_data)
        derived: list[dict[str, Any]] = []
        warnings: list[str] = []
        emptied = False
        # A filter given as one string is filter text (`Auth OR Cart`); what
        # the spec stores, and what is compiled, is the AST it reads to.
        if isinstance(data.get("tag_filter"), str):
            data["tag_filter"], _ = read_filter_string(data["tag_filter"])
        history = data.pop("history", None)
        if history:
            if isinstance(history, Mapping):
                query, scope = history.get("query"), history.get("labels")
                if scope is not None and not isinstance(scope, Mapping):
                    raise InvalidRequest("selection.history.labels must be a mapping")
            else:
                query, scope = history, None
            ids, provenance = self.resolve_history(query, labels=scope)  # type: ignore[arg-type]
            if not ids:
                raise InvalidRequest(
                    f"history selection {query!r} matched no items in run "
                    f"{provenance['resolved_run_id']} — nothing to rerun"
                )
            derived.append(provenance)
            known = {item.id for item in all_items}
            for item_id in ids:
                if item_id not in known:
                    warnings.append(
                        f"history item {item_id!r} is no longer in the catalog — dropped"
                    )
            ids = [item_id for item_id in ids if item_id in known]
            if data.get("item_ids"):
                picks = set(data["item_ids"])
                ids = [item_id for item_id in ids if item_id in picks]
            data["item_ids"] = ids
            emptied = not ids
        return data, derived, warnings, emptied

    # -- building the document -----------------------------------------------------------

    def build_spec_document(
        self,
        selection_data: Mapping[str, Any],
        *,
        title: str,
        labels: Mapping[str, str] | None = None,
        runner_section: Mapping[str, Any] | None = None,
        expect_format: str | None = None,
    ) -> ComposeResult:
        """Compile and assemble the document — nothing is stored."""
        catalog = self._service.catalog
        all_items = catalog.items()
        data, derived, warnings, emptied = self._prepare_selection(selection_data, all_items)
        items = [] if emptied else Selection.from_data(data).compile(all_items)
        if not items:
            warnings.append("selection matched 0 items")
        items = _planned_once(items, warnings)
        spec = build_spec(
            title=title,
            tag_filter=data.get("tag_filter"),
            item_ids=list(data.get("item_ids") or []) or None,
            materialized_ids=[item.id for item in items],
            derived_from=list(derived) or None,
            source_provider=catalog.provider_id,
            snapshot=catalog.snapshot(),
            results={
                "expect": [{"format": expect_format or REFERENCE_FORMAT}],
                "shards": 1,
                "deliver": "none",
            },
            runner=dict(runner_section) if runner_section else None,
            labels=labels,
        )
        report = validate_document(spec, for_dispatch=True)
        if not report.ok:
            raise InvalidRequest("composed spec is not valid: " + "; ".join(report.errors))
        return ComposeResult(spec=spec, items=items, warnings=warnings + report.warnings)

    def compose_run(
        self,
        selection_data: Mapping[str, Any],
        *,
        title: str,
        labels: Mapping[str, str] | None = None,
        origin: str,
        runner_section: Mapping[str, Any] | None = None,
        expect_format: str | None = None,
        runner_id: str | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> ComposeResult:
        """Compose and store a run. Composing *for* a runner (``runner_id``)
        validates its per-run parameters first — a bad runner id or parameter
        refuses the request before any run exists — and writes the accepted
        values into ``runner.<id>`` of the document."""
        section = dict(runner_section or {})
        if runner_id is not None:
            info = self._service.runner(runner_id).describe()
            section[runner_id] = validate_parameters(info.parameters, parameters)
            expect_format = expect_format or info.result_format
        elif parameters:
            raise InvalidRequest("per-run parameters need a runner to belong to")
        # A stored run records the corpus's snapshot, and a dispatch checks the
        # corpus against it. So composing looks at the corpus as it is now —
        # not as it was at the last throttled check — or a run composed right
        # after an edit would be refused as drift by its own dispatch.
        self._service.catalog.refresh()
        result = self.build_spec_document(
            selection_data,
            title=title,
            labels=labels,
            runner_section=section or None,
            expect_format=expect_format,
        )
        result.run = self._service.store.create_run(result.spec, origin=origin)
        # The names the items had when the run was composed: what a reader of
        # the run sees later, whatever the catalog says by then.
        names = {item.id: item.display_name for item in result.items if item.display_name != item.id}
        if names:
            self._service.store.set_item_names(result.run.id, names)
        self._service.bus.emit(
            result.run.id,
            kinds.RUN_COMPOSED,
            data={
                "title": title,
                "labels": dict(labels or {}),
                "origin": origin,
                "planned": len(result.items),
            },
        )
        return result

    def compose_rerun(
        self,
        run: RunRecord,
        *,
        runner_id: str,
        parameters: Mapping[str, Any] | None = None,
        title: str | None = None,
    ) -> ComposeResult:
        """The run a failed-rerun dispatches: composed from what did not pass
        (FAIL, ERROR, MISSING) in ``run`` — with ``derived_from`` provenance
        and origin ``rerun``. Its title is ``title``, else the source run's:
        that it is a rerun is data (origin, ``derived_from``), not prose in a
        title."""
        return self.compose_run(
            {"history": f"failed+error+missing@run:{run.id}"},
            title=title or run.title,
            labels=dict(run.labels),
            origin="rerun",
            runner_id=runner_id,
            parameters=parameters,
        )

    def import_spec(self, doc: Mapping[str, Any], *, origin: str) -> str:
        """Register a spec document composed elsewhere. A document carrying a
        ``results.token`` (as 0.1 wrote them) has the token's hash kept for
        the run and the plaintext dropped. A document whose run id is already
        stored must *be* that run's document — an edited copy is refused."""
        report = validate_document(doc, for_dispatch=True)
        if not report.ok:
            raise InvalidRequest("spec document is not dispatchable: " + "; ".join(report.errors))
        document = json.loads(json.dumps(doc))  # a private, JSON-shaped copy
        token = document.get("results", {}).pop("token", None)
        run_id = str(document["run"]["id"])
        store = self._service.store
        stored = store.get_spec_document(run_id)
        if stored is None:
            store.create_run(document, origin=origin)
            if isinstance(token, str) and token:
                store.set_ingest_token(run_id, None, sha256_hex(token.encode("utf-8")))
            self._service.bus.emit(
                run_id, kinds.RUN_COMPOSED,
                data={"title": document["run"].get("title", ""), "origin": origin, "imported": True},
            )
        elif canonical_json(_comparable(stored)) != canonical_json(_comparable(document)):
            raise SpecConflict(
                f"run {run_id} is already stored and the given document differs from it — "
                "a run's spec does not change after composing. Compose a new run for a "
                "different selection (a spec file is a record, not an editable input)"
            )
        # The one open section, `runner`, may differ: it is the file's to
        # state the per-run parameters of the dispatch it is handed in for
        # (`Service.dispatch(..., document=...)` reads them from it). Nothing
        # is written here — only an accepted dispatch changes the stored spec.
        return run_id

    # -- export ------------------------------------------------------------------------------

    def export(self, run_id: str, *, format: str = "json") -> ExportResult:
        """Hand the run's document out for execution elsewhere: mints an
        ``export`` dispatch, keeps the exact bytes (re-downloadable, and what
        a returned marker's ``spec_sha256`` is verified against), and mints
        the dispatch's ingest token."""
        if format not in _MEDIA_TYPES:
            raise InvalidRequest(f"export format must be json or yaml, got {format!r}")
        service = self._service
        run = service.store.get_run(run_id)
        spec = service.store.get_spec_document(run_id)
        if run is None or spec is None:
            raise UnknownRun(run_id)
        content = serialize_spec(spec, format)
        declared = (spec.get("results") or {}).get("shards")
        count = declared if isinstance(declared, int) and declared > 0 else 1
        # Under the same lock a runner dispatch is recorded under: an export
        # and a runner dispatch must not both find the run idle.
        with service.dispatcher.exclusive():
            run = service.store.get_run(run_id)
            latest = run.latest_dispatch if run is not None else None
            if latest is not None and latest.state in ACTIVE_STATES and latest.owner:
                raise RunActive(
                    f"run {run_id} is being executed (dispatch {latest.dispatch_id}, "
                    f"{latest.state}) — cancel it or wait for it before exporting"
                )
            dispatch = service.ledger.open(
                run_id,
                "export",
                state="AWAITING_RESULTS",
                shards=[str(index) for index in range(1, count + 1)],
            )
        service.store.set_dispatch_spec(dispatch.dispatch_id, content, _MEDIA_TYPES[format])
        dispatch = service.store.update_dispatch(
            dispatch.dispatch_id, spec_sha256=sha256_hex(content)
        )
        token = service.ingestion.mint_token(run_id, dispatch.dispatch_id)
        return ExportResult(
            dispatch=dispatch, content=content, media_type=_MEDIA_TYPES[format], token=token
        )


def _planned_once(items: list[Item], warnings: list[str]) -> list[Item]:
    """An id carried by more than one item (a defect of the corpus, reported
    by `catalog` and `doctor`) is planned once: a run can only ever get one
    result for it."""
    unique = list({item.id: item for item in items}.values())
    if len(unique) != len(items):
        warnings.append(
            f"{len(items) - len(unique)} selected item(s) share an id with another item "
            "— each id is planned once"
        )
    return unique


def serialize_spec(spec: Mapping[str, Any], format: str = "json") -> bytes:
    """THE serialization of a spec document: UTF-8, non-ASCII kept as is."""
    if format == "yaml":
        text = yaml.safe_dump(dict(spec), sort_keys=False, allow_unicode=True)
    else:
        text = json.dumps(spec, indent=2, ensure_ascii=False) + "\n"
    return text.encode("utf-8")


def _comparable(spec: Mapping[str, Any]) -> dict[str, Any]:
    """A spec reduced to what must be identical between a stored document and
    a copy of it — everything but the token (never stored) and the open
    ``runner`` section (per-run values, taken from the copy)."""
    clone = json.loads(json.dumps(spec))
    clone.get("results", {}).pop("token", None)
    clone.pop("runner", None)
    comparable: dict[str, Any] = clone
    return comparable


def _split_history_scope(selector_text: str, query: str) -> tuple[str, dict[str, str]]:
    """Split ``<selector>?key=value&…``. '?' and '&' appear in neither
    ISO-8601 timestamps nor run ids, so the suffix is unambiguous;
    percent-escapes are decoded, so a label value may contain either."""
    selector, sep, scope_text = selector_text.partition("?")
    if not sep:
        return selector, {}
    scope: dict[str, str] = {}
    for pair in scope_text.split("&"):
        if not pair:
            continue
        key, has_value, value = pair.partition("=")
        if not has_value or not key:
            raise InvalidRequest(
                f"history label scope must be 'key=value' pairs joined by '&', got {pair!r} "
                f"in {query!r}"
            )
        scope[unquote(key)] = unquote(value)
    if not scope:
        raise InvalidRequest(f"history query {query!r} has an empty label scope after '?'")
    return selector, scope
