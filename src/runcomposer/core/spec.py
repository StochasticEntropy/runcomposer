"""Run-spec documents: build, load, validate (DESIGN.md §3).

Validation implements the §3 versioning policy: known fields validate
strictly, unknown fields inside known sections are ignored, unknown top-level
sections are ignored with a warning, and a higher MAJOR is refused.
``for_dispatch=True`` adds the dispatch/export profile: the materialized
selection and the results contract must be present (§3.1).

``yaml`` and ``jsonschema`` are imported where a document is read from YAML
or validated, not when this module is: building a document and reading JSON
need neither, so a machine that only executes (the SDK, its toolkits,
``runcomposer-exec``) runs without them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .errors import InvalidRequest
from .filter import FilterError, parse_filter
from .ids import new_ulid
from .timeutil import parse_time, utc_now

__all__ = [
    "KNOWN_SECTIONS",
    "SPEC_VERSION",
    "SpecLoadError",
    "ValidationReport",
    "build_spec",
    "load_document",
    "require_module",
    "runspec_schema",
    "validate_document",
]

#: The run spec version this runcomposer writes (``runspec: "1.0"``).
SPEC_VERSION = "1.0"
KNOWN_SECTIONS = ("runspec", "run", "selection", "source", "results", "runner")

_VERSION_RE = re.compile(r"\d+\.\d+")


class SpecLoadError(InvalidRequest):
    """Raised when a spec file cannot be read or parsed."""

    code = "spec-unreadable"


def require_module(name: str, needed_for: str) -> Any:
    """Import ``name`` for ``needed_for``, or raise ``ModuleNotFoundError``
    saying which package is missing and what needed it — a minimal install
    (``pip install --no-deps runcomposer``) leaves out what only reading,
    validating and serving need."""
    from importlib import import_module

    try:
        return import_module(name)
    except ImportError as exc:
        raise ModuleNotFoundError(
            f"{needed_for} needs the {name!r} package, which is not installed — "
            f"`pip install runcomposer` installs it (a `--no-deps` install leaves it out)",
            name=name,
        ) from exc


def _is_date_time(value: Any) -> bool:
    if isinstance(value, str):
        parse_time(value)
    return True


_format_checker: Any = None


def _formats() -> Any:
    """`date-time` is enforced by this checker, not left to whether an
    optional format-validation package happens to be installed next to
    jsonschema."""
    global _format_checker
    if _format_checker is None:
        jsonschema = require_module("jsonschema", "validating a run-spec document")
        checker = jsonschema.FormatChecker(formats=())
        checker.checks("date-time", raises=InvalidRequest)(_is_date_time)
        _format_checker = checker
    return _format_checker


_schema_cache: dict[str, Any] | None = None


def runspec_schema() -> dict[str, Any]:
    """The bundled JSON Schema for the newest runspec version this build knows."""
    global _schema_cache
    if _schema_cache is None:
        text = resources.files("runcomposer.schemas").joinpath("runspec-1.0.json").read_text("utf-8")
        _schema_cache = json.loads(text)
    return _schema_cache


def build_spec(
    *,
    title: str,
    materialized_ids: list[str],
    source_provider: str,
    snapshot: str,
    source_root: str | None = None,
    tag_filter: Any = None,
    item_ids: list[str] | None = None,
    derived_from: list[Mapping[str, Any]] | None = None,
    results: Mapping[str, Any] | None = None,
    runner: Mapping[str, Any] | None = None,
    labels: Mapping[str, str] | None = None,
    run_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Assemble a runspec document from compile results.

    ``tag_filter`` is the raw filter data as it should appear in the document
    (kept for provenance); it is parsed here so an invalid AST is refused at
    build time rather than embedded.
    """
    if tag_filter is not None:
        parse_filter(tag_filter)
    now = created_at or utc_now()

    run: dict[str, Any] = {"id": run_id or new_ulid(), "title": title, "created_at": now}
    if labels:
        run["labels"] = dict(labels)

    selection: dict[str, Any] = {}
    if tag_filter is not None:
        selection["tag_filter"] = tag_filter
    if item_ids:
        selection["item_ids"] = list(item_ids)
    selection["materialized"] = {
        "item_ids": list(materialized_ids),
        "at": now,
        "count": len(materialized_ids),
    }
    if derived_from:
        selection["derived_from"] = [dict(entry) for entry in derived_from]

    source: dict[str, Any] = {"provider": source_provider}
    if source_root is not None:
        source["root"] = source_root
    source["snapshot"] = snapshot

    doc: dict[str, Any] = {
        "runspec": SPEC_VERSION,
        "run": run,
        "selection": selection,
        "source": source,
    }
    if results is not None:
        doc["results"] = dict(results)
    if runner is not None:
        doc["runner"] = dict(runner)
    return doc


def load_document(path: str | Path) -> Any:
    """Load a spec document from a YAML or JSON file (§3: JSON-isomorphic)."""
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    if file_path.suffix.lower() == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SpecLoadError(f"{path}: not valid JSON: {exc}") from None
    yaml = require_module("yaml", f"reading the YAML spec file {path}")
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecLoadError(f"{path}: not valid YAML: {exc}") from None


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_document(doc: Any, *, for_dispatch: bool = False) -> ValidationReport:
    """Check a run spec document against the schema and the rules beyond it;
    the report lists errors and warnings instead of raising. With
    ``for_dispatch``, also what a document handed to an executor needs."""
    report = ValidationReport()
    if not isinstance(doc, Mapping):
        report.errors.append(f"document must be a mapping, got {type(doc).__name__}")
        return report

    # Version gate first — a consumer MUST refuse a higher MAJOR (§3).
    version = doc.get("runspec")
    if version is None:
        report.errors.append("missing required top-level field 'runspec'")
        return report
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        report.errors.append(f"'runspec' must be a version string like '1.0', got {version!r}")
        return report
    major = int(version.split(".", 1)[0])
    if major != 1:
        report.errors.append(
            f"unsupported runspec version {version!r}: this validator knows runspec 1.x "
            "and refuses any other MAJOR (versioning policy, DESIGN.md §3)"
        )
        return report

    jsonschema = require_module("jsonschema", "validating a run-spec document")
    validator = jsonschema.Draft202012Validator(runspec_schema(), format_checker=_formats())
    for error in sorted(validator.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path))):
        report.errors.append(f"{error.json_path}: {error.message}")

    for key in doc:
        if key not in KNOWN_SECTIONS:
            report.warnings.append(
                f"unknown top-level section {key!r} ignored (versioning policy, DESIGN.md §3)"
            )

    selection = doc.get("selection")
    materialized = selection.get("materialized") if isinstance(selection, Mapping) else None
    if (
        isinstance(materialized, Mapping)
        and isinstance(materialized.get("item_ids"), list)
        and isinstance(materialized.get("count"), int)
        and materialized["count"] != len(materialized["item_ids"])
    ):
        report.errors.append(
            f"$.selection.materialized: count is {materialized['count']} "
            f"but item_ids has {len(materialized['item_ids'])} entries"
        )
    if isinstance(materialized, Mapping) and isinstance(materialized.get("item_ids"), list):
        ids = [entry for entry in materialized["item_ids"] if isinstance(entry, str)]
        if len(set(ids)) != len(ids):
            report.errors.append(
                "$.selection.materialized.item_ids: an id is listed more than once — "
                "an item is planned once"
            )
    if isinstance(selection, Mapping) and selection.get("tag_filter") is not None:
        try:
            parse_filter(selection["tag_filter"])
        except FilterError as exc:
            report.errors.append(f"$.selection.tag_filter: {exc}")

    if for_dispatch:
        if not isinstance(materialized, Mapping):
            report.errors.append(
                "dispatch/export requires selection.materialized — the embedded item list "
                "is the authoritative executed set (DESIGN.md §3.1)"
            )
        elif not materialized.get("item_ids"):
            report.warnings.append("selection.materialized.item_ids is empty — nothing to execute")
        if "results" not in doc:
            report.errors.append("dispatch/export requires a results section (DESIGN.md §3.1)")

    return report
