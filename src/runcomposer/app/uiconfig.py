"""The ``ui:`` section's selections (DESIGN.md §8): ``quick_filters``,
``default_filter`` and ``presets``, validated against the filter grammar and
normalized into the one shape clients are served (``Config.ui``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from runcomposer.core.errors import ConfigError
from runcomposer.core.filter import FilterError, format_filter, parse_filter, to_data
from runcomposer.core.filtertext import read_filter_string

__all__ = ["ui_selections"]

_PRESET_KEYS = ("name", "label", "description", "filter", "runner", "parameters", "title", "labels")


def _selection_entry(raw: Any, where: str, warnings: list[str] | None = None) -> dict[str, Any]:
    """One configured filter — filter text or a whole expression — as
    ``{tag_filter, text, kind}``: the expression in the grammar a run spec
    uses, its canonical one-line text, and whether it is a single pattern.
    A string is filter text (``Auth OR Cart``, ``"Sprint 12"``), read like a
    string ``tag_filter`` over the API — a single pattern that is not text
    still means that pattern, and what to write instead goes to
    ``warnings``."""
    try:
        if isinstance(raw, str):
            raw, note = read_filter_string(raw)
            if note and warnings is not None:
                warnings.append(f"{where}: {note}")
        node = parse_filter(raw, where)
    except FilterError as exc:
        raise ConfigError(f"{where}: {exc}" if not str(exc).startswith(where) else str(exc)) from None
    return {
        "tag_filter": to_data(node),
        "text": format_filter(node),
        "kind": "pattern" if isinstance(raw, str) else "expression",
    }


def ui_selections(section: Mapping[str, Any], warnings: list[str] | None = None) -> dict[str, Any]:
    """Validate and normalize the selections of a ``ui:`` section.

    - ``quick_filters``: ``[{label, filter}]`` — ``filter`` is filter text
      (``Checkout``, ``prefix:Payments-``, ``Auth OR Cart``, ``"Sprint
      12"``) or a full filter expression (``{op: AND, items: […]}``,
      ``{not: …}``); a bare string is short for a filter labelled with
      itself (a single pattern: the pattern). Served as ``{label, filter}``
      with ``filter`` as written plus ``tag_filter`` / ``text`` / ``kind``.
    - ``default_filter``: the expression a new selection starts from.
    - ``presets``: named, complete starting points — ``name`` and ``filter``,
      optionally ``label``, ``description``, ``title``, ``labels``, and a
      ``runner`` with its per-run ``parameters``.

    A ``filter`` string is read as a string ``tag_filter`` is over the API;
    one that only the fallback for single patterns reads appends what to
    write instead to ``warnings``.
    """
    result: dict[str, Any] = {}
    quick = section.get("quick_filters")
    if quick is not None:
        if not isinstance(quick, list):
            raise ConfigError("ui.quick_filters must be a list of {label, filter} entries")
        entries = []
        for index, entry in enumerate(quick):
            where = f"ui.quick_filters[{index}]"
            if isinstance(entry, str):
                entry = {"filter": entry}
            if not isinstance(entry, Mapping) or "filter" not in entry:
                raise ConfigError(f"{where} must be a mapping with a 'filter' (and a 'label')")
            normalized = _selection_entry(entry["filter"], f"{where}.filter", warnings)
            pattern = normalized["tag_filter"] if normalized["kind"] == "pattern" else None
            entries.append(
                {**entry, "label": str(entry.get("label") or pattern or normalized["text"]), **normalized}
            )
        result["quick_filters"] = entries
    default = section.get("default_filter")
    if default is not None:
        result["default_filter"] = _selection_entry(default, "ui.default_filter", warnings)
    presets = section.get("presets")
    if presets is not None:
        if not isinstance(presets, list):
            raise ConfigError("ui.presets must be a list of {name, filter, …} entries")
        seen: set[str] = set()
        entries = []
        for index, entry in enumerate(presets):
            where = f"ui.presets[{index}]"
            if not isinstance(entry, Mapping) or not entry.get("name") or "filter" not in entry:
                raise ConfigError(f"{where} must be a mapping with a 'name' and a 'filter'")
            unknown = sorted(set(entry) - set(_PRESET_KEYS))
            if unknown:
                raise ConfigError(f"{where} has unknown key(s) {unknown} (known: {list(_PRESET_KEYS)})")
            name = str(entry["name"])
            if name in seen:
                raise ConfigError(f"{where}: preset name {name!r} is used twice")
            seen.add(name)
            for key in ("parameters", "labels"):
                if entry.get(key) is not None and not isinstance(entry[key], Mapping):
                    raise ConfigError(f"{where}.{key} must be a mapping")
            if entry.get("parameters") and not entry.get("runner"):
                raise ConfigError(f"{where}: 'parameters' need a 'runner' to belong to")
            entries.append(
                {
                    "name": name,
                    "label": str(entry.get("label") or name),
                    "description": str(entry.get("description") or ""),
                    **_selection_entry(entry["filter"], f"{where}.filter", warnings),
                    "runner": str(entry["runner"]) if entry.get("runner") else None,
                    "parameters": dict(entry.get("parameters") or {}),
                    "title": str(entry.get("title") or entry.get("label") or name),
                    "labels": {str(k): str(v) for k, v in (entry.get("labels") or {}).items()},
                }
            )
        result["presets"] = entries
    return result
