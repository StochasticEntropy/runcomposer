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

The second half of the module *resolves* a validated document against a
catalog. A written leaf carries one pattern, so a pattern covering a family of
tags renders as a single opaque node: the tags it stands for have no node of
their own and cannot be picked individually. :func:`resolve_taxonomy` expands
each node with the concrete catalog tags its own pattern matches, drops the
nodes that match nothing at all, and appends the tags no node anywhere claims
— all in the *same* three-key node grammar, so the result is itself a valid
taxonomy document and the tree's consumer needs no second vocabulary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .filter import FilterError, parse_filter
from .model import Item

__all__ = ["TaxonomyError", "resolve_taxonomy", "validate_taxonomy"]

NODE_KEYS = ("label", "filter", "children")

#: ``origin`` of a node the taxonomy file wrote.
ORIGIN_FILE = "file"
#: ``origin`` of a node synthesized for one concrete catalog tag.
ORIGIN_TAG = "tag"
#: ``origin`` of the synthetic node holding the tags no written node claims.
ORIGIN_UNASSIGNED = "unassigned"


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
                "must be distinct, they are how a reader tells two nodes apart"
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


# -- resolution against a catalog --------------------------------------------


@dataclass(frozen=True)
class _Resolved:
    """One built node plus the tag set its whole subtree covers.

    The tag set is what the parent needs and the response must not carry, so
    it travels beside the node instead of inside it.
    """

    node: dict[str, Any]
    tags: frozenset[str]


def _tag_pattern(tag: str, *, case_exact: bool = False) -> str:
    """The pattern that selects exactly one concrete tag.

    A bare tag is a literal, which is the readable thing to put in the filter
    builder. Two cases force the escaped-regex spelling instead:

    - a tag that *starts* with a grammar prefix — a tag literally named
      ``regex:…`` would otherwise be read as an expression, not as itself;
    - a tag some other tag differs from only in case. Literals match
      case-insensitively (§3.1), so ``Adapter`` would select ``ADAPTER`` too
      and a node promising one tag would quietly deliver two.
    """
    if case_exact or tag.startswith(("regex:", "prefix:")):
        return f"regex:^{re.escape(tag)}$"
    return tag


def _unique_label(label: str, taken: set[str]) -> str:
    """A sibling-unique label. Synthesized nodes only: the written ones are
    already distinct (the validator refuses siblings that are not), so this
    only ever fires when a catalog tag happens to be spelled like a label its
    author chose. Suffixing keeps the tree valid without dropping the tag."""
    candidate = label
    counter = 2
    while candidate in taken:
        candidate = f"{label} ({counter})"
        counter += 1
    taken.add(candidate)
    return candidate


class _Resolver:
    """Holds the catalog-derived indexes one resolution needs.

    Everything expensive is computed once: the tag universe, tag → items, and
    a memo of pattern → matching tags. A taxonomy repeats patterns (a group's
    alternation and its children's literals cover the same ground), and a
    catalog has far more items than tags, so both matter at corpus scale.
    """

    def __init__(self, items: Sequence[Item]) -> None:
        tag_items: dict[str, set[int]] = {}
        for index, item in enumerate(items):
            for tag in item.tags:
                tag_items.setdefault(tag, set()).add(index)
        self.item_total = len(items)
        # One tag order for the whole result: case-insensitive, ties broken by
        # the exact spelling so two tags differing only in case stay stable.
        self.tags: tuple[str, ...] = tuple(
            sorted(tag_items, key=lambda tag: (tag.casefold(), tag))
        )
        self._tag_items = {tag: frozenset(indexes) for tag, indexes in tag_items.items()}
        self._matches: dict[str, tuple[str, ...]] = {}
        # Tags another tag differs from only in case: their nodes need the
        # case-sensitive spelling to mean themselves alone.
        folded: dict[str, int] = {}
        for tag in self.tags:
            folded[tag.casefold()] = folded.get(tag.casefold(), 0) + 1
        self._case_ambiguous = {tag for tag in self.tags if folded[tag.casefold()] > 1}
        self.claimed: set[str] = set()
        #: Tags some node in the result selects *alone* — the whole point of
        #: resolving, so it is worth counting rather than assuming.
        self.selectable: set[str] = set()
        self.dropped = 0

    def matching_tags(self, pattern: str) -> tuple[str, ...]:
        """Catalog tags this one pattern selects, in the global tag order."""
        hit = self._matches.get(pattern)
        if hit is None:
            node = parse_filter(pattern, path="filter")
            hit = tuple(tag for tag in self.tags if node.matches((tag,)))
            self._matches[pattern] = hit
        return hit

    def item_count(self, tags: frozenset[str]) -> int:
        """How many catalog items carry at least one of these tags."""
        seen: set[int] = set()
        for tag in tags:
            seen |= self._tag_items[tag]
        return len(seen)

    def tag_node(self, tag: str, node_id: str, label: str) -> dict[str, Any]:
        """One synthesized node standing for exactly one concrete tag."""
        self.selectable.add(tag)
        return {
            "id": node_id,
            "label": label,
            "filter": _tag_pattern(tag, case_exact=tag in self._case_ambiguous),
            "origin": ORIGIN_TAG,
            "tag_count": 1,
            "item_count": self.item_count(frozenset((tag,))),
        }

    # -- building ------------------------------------------------------------

    def build(self, nodes: Sequence[Any], path: str) -> list[_Resolved]:
        built: list[_Resolved] = []
        for index, node in enumerate(nodes):
            # The index is the node's position in the *file*, so an id stays
            # the same when a sibling stops matching and collapses away.
            resolved = self.build_node(node, f"{path}[{index}]")
            if resolved is None:
                self.dropped += 1
                continue
            built.append(resolved)
        return built

    def build_node(self, node: Mapping[str, Any], node_path: str) -> _Resolved | None:
        pattern = node.get("filter")
        own = self.matching_tags(pattern) if isinstance(pattern, str) else ()
        self.claimed.update(own)
        if len(own) == 1:
            self.selectable.add(own[0])

        written = self.build(node.get("children") or [], f"{node_path}.children")
        covered: set[str] = set()
        for child in written:
            covered |= child.tags

        children = [child.node for child in written]
        # A tag some descendant already claims belongs there, not here: the
        # more specific node is the one a reader wants to click, and a tag
        # listed twice in one branch reads as two different things.
        loose = [tag for tag in own if tag not in covered]
        # A node whose pattern resolves to exactly one tag and that has no
        # children of its own already *is* that tag's node; giving it a single
        # identical child would be noise.
        if children or len(loose) > 1:
            taken = {child["label"] for child in children}
            for index, tag in enumerate(loose):
                children.append(
                    self.tag_node(tag, f"{node_path}.tags[{index}]", _unique_label(tag, taken))
                )

        subtree = frozenset(own) | covered
        if not subtree:
            # Nothing in the current corpus is under this node. A dead
            # clickable leaf is worse than no leaf: it selects nothing and
            # says nothing about why (`runcomposer taxonomy-check` is where
            # that question is answered).
            return None

        built: dict[str, Any] = {"id": node_path, "label": str(node["label"])}
        if isinstance(pattern, str):
            built["filter"] = pattern
        built["origin"] = ORIGIN_FILE
        built["tag_count"] = len(subtree)
        built["item_count"] = self.item_count(subtree)
        if children:
            built["children"] = children
        return _Resolved(node=built, tags=subtree)


def _count_nodes(nodes: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 + _count_nodes(node.get("children") or []) for node in nodes)


def resolve_taxonomy(
    document: Any,
    items: Sequence[Item],
    *,
    origin: str = "taxonomy",
    unassigned_label: str = "Unassigned tags",
) -> dict[str, Any]:
    """Resolve a taxonomy document against a catalog (DESIGN.md §2).

    A written node carries **one** pattern, which is what makes the format
    worth writing by hand and also what makes a family of tags collapse into
    one opaque node. Resolution puts the family back:

    - every node keeps its own ``label`` and ``filter`` and gains one child
      per concrete catalog tag its own pattern matches, so each tag is
      individually clickable — except where a descendant already claims that
      tag (it belongs to the more specific node) and except where the node's
      pattern resolves to a single tag and has no children of its own, in
      which case the node already *is* that tag's node;
    - a node whose whole subtree matches nothing in the current catalog is
      dropped rather than rendered as a leaf that clicks and selects nothing;
    - the tags **no** node anywhere claims are appended as one synthetic node,
      so a browsing reader can reach every tag the catalog has and a tag that
      appears tomorrow is visible instead of merely absent.

    Nodes come back in the same three-key grammar the file is written in —
    ``label``, optional ``filter``, optional ``children`` — plus additive
    metadata:

    ``id``
        Stable key for this node, spelled as its path in the document
        (``taxonomy[0].children[2]``, the same path the validator's messages
        use), with ``.tags[n]`` for a synthesized tag node. Written nodes are
        numbered by their position in the *file*, so a collapsing sibling does
        not renumber the rest.
    ``origin``
        ``"file"``, ``"tag"`` or ``"unassigned"`` — where the node came from.
    ``tag_count`` / ``item_count``
        Distinct catalog tags and items under this node, its own pattern and
        its whole subtree together. Clicking a node applies **its own**
        pattern, which is the same thing except in the unusual case of a node
        whose pattern is narrower than a child's.

    The result is therefore itself a valid taxonomy document: a consumer that
    can render the file can render this, and only the metadata is new.

    Beside ``taxonomy`` the response carries ``tags`` — every tag in the
    catalog, in the tree's own order. It is not derivable from the nodes (a
    leaf whose pattern resolves to a single tag *is* that tag's node and has no
    tag child, so a tag reached only through a regex is nowhere spelled out),
    and a client that offers tag completion needs all of them.

    ``items`` is the catalog to resolve against — the same list a selection
    compiles over, so the tree and the preview never disagree about what
    exists. ``document`` is validated first, on the same terms as everywhere
    else (§8), so this cannot be reached with a shape the endpoint refuses.
    """
    validated = validate_taxonomy(document, origin=origin)
    resolver = _Resolver(items)
    built = resolver.build(validated["taxonomy"], "taxonomy")
    nodes = [entry.node for entry in built]

    unassigned = [tag for tag in resolver.tags if tag not in resolver.claimed]
    if unassigned:
        taken = {node["label"] for node in nodes}
        tag_nodes = [
            resolver.tag_node(tag, f"taxonomy[unassigned].tags[{index}]", tag)
            for index, tag in enumerate(unassigned)
        ]
        nodes.append(
            {
                "id": "taxonomy[unassigned]",
                "label": _unique_label(unassigned_label, taken),
                "origin": ORIGIN_UNASSIGNED,
                "tag_count": len(unassigned),
                "item_count": resolver.item_count(frozenset(unassigned)),
                "children": tag_nodes,
            }
        )

    claimed = frozenset(resolver.claimed)
    return {
        "taxonomy": nodes,
        # The catalog's whole tag universe, in the same order the tree uses.
        # The tree alone does NOT carry it: a written leaf whose pattern
        # resolves to exactly one tag already *is* that tag's node and gets no
        # tag child, so a tag reached only through `regex:^Cart(V2)?$` appears
        # nowhere in the nodes as itself. A client offering tag completion
        # would otherwise have to re-derive the tag set from the catalog and
        # would silently be missing those, so it is served here rather than
        # guessed at there — one resolution, one source (DESIGN.md §2).
        "tags": list(resolver.tags),
        "resolved": {
            "tags_total": len(resolver.tags),
            "tags_claimed": len(claimed),
            "tags_selectable": len(resolver.selectable),
            "tags_unassigned": len(unassigned),
            "items_total": resolver.item_total,
            "items_claimed": resolver.item_count(claimed),
            "nodes_written": _count_nodes(validated["taxonomy"]),
            "nodes_dropped": resolver.dropped,
            "nodes_total": _count_nodes(nodes),
        },
    }
