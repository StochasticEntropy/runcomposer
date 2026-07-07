"""Selection: lossless filter + explicit picks, compiled to a materialized list.

Rules (DESIGN.md §3.1):

- ``tag_filter`` and ``item_ids`` both present → intersection (fixed AND).
- Explicit ids unknown to the catalog are a compile-time error; execution-time
  drift is governed by the executor's drift policy (§3.3), not compose-time
  slack.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .filter import FilterNode, parse_filter, to_data
from .model import Item

__all__ = ["Selection", "SelectionError"]


class SelectionError(ValueError):
    """Raised for invalid or uncompilable selections."""


@dataclass(frozen=True)
class Selection:
    tag_filter: FilterNode | None = None
    item_ids: tuple[str, ...] = ()

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> "Selection":
        """Build from the selection *inputs* of a spec's ``selection`` section.

        Derived keys (``materialized``, ``derived_from``) are outputs of a
        compile and are ignored here.
        """
        if not isinstance(data, Mapping):
            raise SelectionError(f"selection must be a mapping, got {type(data).__name__}")
        node = None
        if data.get("tag_filter") is not None:
            node = parse_filter(data["tag_filter"])
        raw_ids = data.get("item_ids") or ()
        if not isinstance(raw_ids, (list, tuple)):
            raise SelectionError("selection.item_ids must be a list of strings")
        for entry in raw_ids:
            if not isinstance(entry, str) or not entry:
                raise SelectionError(f"selection.item_ids entries must be non-empty strings, got {entry!r}")
        if node is None and not raw_ids:
            raise SelectionError("selection is empty: provide tag_filter and/or item_ids")
        return cls(tag_filter=node, item_ids=tuple(raw_ids))

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.tag_filter is not None:
            data["tag_filter"] = to_data(self.tag_filter)
        if self.item_ids:
            data["item_ids"] = list(self.item_ids)
        return data

    def compile(self, items: Sequence[Item]) -> list[Item]:
        """Materialize against a catalog. Preserves catalog order."""
        if self.tag_filter is None and not self.item_ids:
            raise SelectionError("selection is empty: provide tag_filter and/or item_ids")
        known = {item.id for item in items}
        unknown = [item_id for item_id in self.item_ids if item_id not in known]
        if unknown:
            raise SelectionError(
                "unknown item ids (not in the catalog snapshot): " + ", ".join(sorted(unknown))
            )
        matched = [
            item for item in items if self.tag_filter is None or self.tag_filter.matches(item.tags)
        ]
        if self.item_ids:
            picks = frozenset(self.item_ids)
            matched = [item for item in matched if item.id in picks]
        return matched
