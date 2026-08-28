"""Taxonomy documents: the curated tree over tag patterns (DESIGN.md §2).

The taxonomy is data, not code — but data with a shape. Until this module
existed nothing checked that shape, so a wrong-shaped file was parsed, served
as a perfectly successful response, and rendered as an empty panel with
nothing anywhere to say why. That is a bad way to lose an afternoon, and the
format's own documentation had to carry a section about it.

The validator is strict about the three documented node keys and tolerant of
extra ones, matching the spec's versioning stance (unknown fields inside a
known section are ignored, DESIGN.md §3). The one shape refused rather than
ignored is a node that is neither clickable nor openable — no ``filter`` and
no ``children`` — because that is precisely what a misspelled ``pattern:`` or
``tags:`` key produces, and a dead heading is never what anyone meant.

Every message names the offending node by its path in the document and, where
the mistake has an obvious repair, says what to write instead.
"""

from __future__ import annotations

from typing import Any, Mapping

from .filter import FilterError, parse_filter

__all__ = ["TaxonomyError", "validate_taxonomy"]

NODE_KEYS = ("label", "filter", "children")


class TaxonomyError(ValueError):
    """Raised when a taxonomy document does not conform to the format.

    Carries a message naming the offending node — a taxonomy is written by
    hand, so the only useful error is one that says which line to fix.
    """


def _type_name(value: Any) -> str:
    return "null" if value is None else type(value).__name__


def validate_taxonomy(document: Any, *, origin: str = "taxonomy") -> dict[str, Any]:
    """Validate a parsed taxonomy document; return it unchanged, or raise.

    ``origin`` is the file name used in messages — the reader needs to know
    *which* taxonomy is wrong when a deployment has several.
    """
    if document is None:
        raise TaxonomyError(
            f"{origin}: taxonomy file is empty — it needs a top-level 'taxonomy' key "
            "holding a list of nodes (format: docs/taxonomy.md)"
        )
    if not isinstance(document, Mapping):
        raise TaxonomyError(
            f"{origin}: taxonomy document must be a mapping with a top-level 'taxonomy' key, "
            f"got {_type_name(document)}"
            + (
                " — a bare list of nodes needs to be nested under 'taxonomy:'"
                if isinstance(document, list)
                else ""
            )
        )
    if "taxonomy" not in document:
        found = sorted(str(key) for key in document)
        raise TaxonomyError(
            f"{origin}: no top-level 'taxonomy' key (found {found}) — the tree must be nested "
            "under 'taxonomy:'; a document keyed 'nodes:', 'tree:' or 'groups:' parses fine "
            "and renders nothing"
        )
    nodes = document["taxonomy"]
    if not isinstance(nodes, list):
        raise TaxonomyError(
            f"{origin}: 'taxonomy' must be a list of nodes, got {_type_name(nodes)}"
        )
    _validate_nodes(nodes, origin=origin, path="taxonomy")
    return dict(document)


def _validate_nodes(nodes: list[Any], *, origin: str, path: str) -> None:
    seen: dict[str, int] = {}
    for index, node in enumerate(nodes):
        node_path = f"{path}[{index}]"
        if not isinstance(node, Mapping):
            raise TaxonomyError(
                f"{origin}: {node_path} must be a node mapping with a 'label', "
                f"got {_type_name(node)}"
            )
        label = node.get("label")
        if not isinstance(label, str) or not label.strip():
            raise TaxonomyError(
                f"{origin}: {node_path} needs a non-empty 'label' string (the text shown in "
                f"the tree), got {label!r}"
            )
        named = f"{node_path} ({label!r})"
        if label in seen:
            raise TaxonomyError(
                f"{origin}: {named} repeats the label of {path}[{seen[label]}] — sibling labels "
                "must be distinct, the tree keys its nodes by label"
            )
        seen[label] = index

        has_filter = "filter" in node
        if has_filter:
            _validate_filter(node["filter"], origin=origin, named=named)
        has_children = "children" in node
        if has_children:
            children = node["children"]
            if not isinstance(children, list):
                raise TaxonomyError(
                    f"{origin}: {named} has 'children' of type {_type_name(children)} — "
                    "'children' must be a list of nodes"
                )
            _validate_nodes(children, origin=origin, path=f"{node_path}.children")
        if not has_filter and not has_children:
            extra = sorted(str(key) for key in node if key not in NODE_KEYS)
            hint = (
                f" — did you mean 'filter'? (this node's other key(s): {extra})"
                if extra
                else " — a node needs 'filter' (a clickable leaf), 'children' (a group), or both"
            )
            raise TaxonomyError(
                f"{origin}: {named} has neither 'filter' nor 'children', so it would render as "
                f"a heading that never opens{hint}"
            )


def _validate_filter(value: Any, *, origin: str, named: str) -> None:
    if not isinstance(value, str):
        raise TaxonomyError(
            f"{origin}: {named} has a 'filter' of type {_type_name(value)} — a leaf carries "
            "exactly ONE pattern string; write alternatives as one regex, e.g. "
            "'regex:^(Visa|Mastercard)$' (docs/taxonomy.md)"
        )
    try:
        parse_filter(value, path=f"{named} filter")
    except FilterError as exc:
        raise TaxonomyError(f"{origin}: {exc}") from None
