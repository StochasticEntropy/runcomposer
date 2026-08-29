"""`manifest` TestSource — a JSON/YAML catalog of items (DESIGN.md §6.1).

The zero-dependency adopter path: the manifest requires only ``id`` + ``tags``
per item; ``name``, ``hierarchy``, and ``meta`` are optional. Document shape::

    {"items": [{"id": "...", "tags": ["..."], ...}, ...]}

Ids are opaque strings minted by whatever produced the manifest. This source
resolves native names by identity — and additionally through per-item
``aliases``: since the manifest's author owns the id space (§2), the manifest
itself may declare which result-artifact spellings map onto each id (e.g. a
pytest manifest with nodeid ids and junit ``classname.name`` aliases). Alias
collisions are refused at load time so every native name resolves to at most
one id.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from runcomposer.core.model import Item

__all__ = ["ManifestError", "ManifestSource"]


class ManifestError(ValueError):
    """Raised when a manifest file is malformed."""


class ManifestSource:
    provider_id = "manifest"

    @staticmethod
    def resolve_config_paths(options, resolve):
        """§8 opt-in: ``path`` is the catalog file, relative to the config
        file's directory. Absent (the zero-config default) it stays absent —
        the core then supplies the bundled demo corpus as a package
        resource, which is not a configured path at all."""
        if "path" in options:
            options["path"] = resolve(options["path"])
        return options

    def __init__(self, path: Any):
        """``path``: a filesystem path, or any object with ``read_text()``
        (e.g. an ``importlib.resources`` traversable)."""
        source = path
        if hasattr(source, "read_text"):
            text = source.read_text(encoding="utf-8")
            label = getattr(source, "name", str(source))
        else:
            text = Path(source).read_text(encoding="utf-8")
            label = str(source)
        self._label = label
        data = self._parse(text, label)
        self._items = self._build_items(data, label)
        self._ids = {item.id for item in self._items}
        self._aliases = self._build_aliases(data, label, self._ids)
        self._snapshot = self._hash_catalog(data)

    @staticmethod
    def _parse(text: str, label: str) -> Mapping[str, Any]:
        try:
            if label.endswith(".json"):
                data = json.loads(text)
            else:
                data = yaml.safe_load(text)
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            raise ManifestError(f"{label}: cannot parse manifest: {exc}") from None
        if not isinstance(data, Mapping) or not isinstance(data.get("items"), list):
            raise ManifestError(f"{label}: manifest must be a mapping with an 'items' list")
        return data

    @staticmethod
    def _build_items(data: Mapping[str, Any], label: str) -> list[Item]:
        items: list[Item] = []
        seen: set[str] = set()
        for index, entry in enumerate(data["items"]):
            where = f"{label}: items[{index}]"
            if not isinstance(entry, Mapping):
                raise ManifestError(f"{where}: item must be a mapping")
            item_id = entry.get("id")
            if not isinstance(item_id, str) or not item_id:
                raise ManifestError(f"{where}: 'id' is required and must be a non-empty string")
            if item_id in seen:
                raise ManifestError(f"{where}: duplicate item id {item_id!r}")
            seen.add(item_id)
            tags = entry.get("tags")
            if not isinstance(tags, list) or not all(isinstance(t, str) and t for t in tags):
                raise ManifestError(f"{where}: 'tags' is required and must be a list of non-empty strings")
            items.append(
                Item(
                    id=item_id,
                    tags=tuple(tags),
                    name=entry.get("name", ""),
                    hierarchy=tuple(entry.get("hierarchy") or ()),
                    meta=dict(entry.get("meta") or {}),
                )
            )
        return items

    @staticmethod
    def _hash_catalog(data: Mapping[str, Any]) -> str:
        canonical = json.dumps(data["items"], sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _build_aliases(data: Mapping[str, Any], label: str, ids: set[str]) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for index, entry in enumerate(data["items"]):
            for alias in entry.get("aliases") or []:
                if not isinstance(alias, str) or not alias:
                    raise ManifestError(f"{label}: items[{index}]: aliases must be non-empty strings")
                if alias in aliases or alias in ids:
                    raise ManifestError(
                        f"{label}: items[{index}]: alias {alias!r} collides — every native "
                        "name must resolve to exactly one item id (DESIGN.md §2)"
                    )
                aliases[alias] = entry["id"]
        return aliases

    def items(self) -> list[Item]:
        return list(self._items)

    def snapshot(self) -> str:
        return self._snapshot

    def resolve(self, native_name: str) -> str | None:
        if native_name in self._ids:
            return native_name
        return self._aliases.get(native_name)
