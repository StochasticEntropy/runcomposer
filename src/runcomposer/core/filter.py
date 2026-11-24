"""Tag-filter AST — the lossless selection grammar (DESIGN.md §3.1).

Grammar::

    node    := "pattern" | {"op": "AND"|"OR", "items": [node, ...]} | {"not": node}
    pattern := "regex:<expression>" | "prefix:<text>" | "<literal tag>"

Semantics:

- A bare string is a literal tag, matched case-insensitively.
- ``regex:<e>`` matches an item when any of its tags contains a match for
  ``<e>`` (``re.search`` — anchor explicitly with ``^``/``$``). Regex matching
  is case-sensitive; use ``(?i)`` inside the expression to opt out.
- ``prefix:<t>`` is pure sugar for ``regex:^<t>``.
- Parsing is lossless: ``to_data(parse_filter(d)) == d``.

The same filter has a one-line text form for people (``Payments AND NOT
prefix:Quarantine-``): :func:`format_filter` writes it, and
:mod:`.filtertext` reads it back into this AST.

"Every item" has no node of its own: an empty ``AND`` is refused, and the
run spec's closed ``runspec/1.0`` grammar has no other shape for it. It is
the one expression true of every item, :data:`MATCH_ALL` —
``{"not": "regex:^$"}``, NOT of a pattern no tag can match (a tag is never
empty). That is what a run spec stores and the API returns; the text form
writes it, and reads it, as ``*`` (:data:`EVERYTHING`).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any, Union

from .errors import InvalidRequest

__all__ = [
    "EVERYTHING",
    "MATCH_ALL",
    "MAX_FILTER_DEPTH",
    "MAX_FILTER_PATTERNS",
    "BoolOp",
    "FilterError",
    "FilterNode",
    "FilterTooDeep",
    "FilterTooLarge",
    "NotNode",
    "TagPattern",
    "format_filter",
    "is_everything",
    "parse_filter",
    "quote_pattern",
    "to_data",
]


class FilterError(InvalidRequest):
    """Raised when filter data does not conform to the grammar. ``at``, when
    given, is where in the filter: the keys and indices from its root
    (``["items", 1, "not"]``) — served as ``path`` beside the message."""

    code = "invalid-filter"

    def __init__(self, message: str, *, at: Sequence[str | int] | None = None, reason: str | None = None):
        super().__init__(message)
        if at is not None:
            self.extra = {**(self.extra or {}), "path": list(at)}
        if reason is not None:
            self.extra = {**(self.extra or {}), "reason": reason}


def _refuse(path: str, at: Sequence[str | int], reason: str, kind: type[FilterError] | None = None) -> FilterError:
    """The error for the node at ``at`` (written ``path``): the message is
    ``"<path>: <reason>"``; ``path`` and ``reason`` are also served apart,
    so a client marks the node without reading the sentence."""
    return (kind or FilterError)(f"{path}: {reason}", at=at, reason=reason)


#: How deeply a filter may nest — NOT and AND/OR groups counted alike, in
#: the AST and in the text form. Deeper filters are refused
#: (:class:`FilterTooDeep`) before anything recurses over them.
MAX_FILTER_DEPTH = 40


class FilterTooDeep(FilterError):
    """A filter nested deeper than :data:`MAX_FILTER_DEPTH` levels."""

    code = "filter-too-deep"


#: How many patterns one filter may hold — in the AST and in the text form.
#: Matching costs a pattern per item and tag: a filter that lists a
#: 1000 tags is one that wants a ``prefix:`` or ``regex:`` pattern.
MAX_FILTER_PATTERNS = 1000


class FilterTooLarge(FilterError):
    """A filter of more than :data:`MAX_FILTER_PATTERNS` patterns — an
    ``invalid-filter`` like any other filter that cannot be used."""


def too_many_patterns(path: str, at: Sequence[str | int] = ()) -> FilterTooLarge:
    reason = (
        f"the filter holds more than {MAX_FILTER_PATTERNS} patterns — select with fewer patterns "
        "that each match more tags (a prefix: or regex: pattern)"
    )
    return FilterTooLarge(f"{path}: {reason}", at=at, reason=reason)


@dataclass(frozen=True)
class TagPattern:
    """A leaf pattern. ``regex`` is None for a literal tag."""

    raw: str
    regex: re.Pattern[str] | None = field(default=None, compare=False, repr=False)

    def matches(self, tags: Collection[str]) -> bool:
        if self.regex is None:
            needle = self.raw.casefold()
            return any(tag.casefold() == needle for tag in tags)
        return any(self.regex.search(tag) is not None for tag in tags)


@dataclass(frozen=True)
class BoolOp:
    op: str  # "AND" | "OR"
    items: tuple[FilterNode, ...]

    def matches(self, tags: Collection[str]) -> bool:
        if self.op == "AND":
            return all(child.matches(tags) for child in self.items)
        return any(child.matches(tags) for child in self.items)


@dataclass(frozen=True)
class NotNode:
    item: FilterNode

    def matches(self, tags: Collection[str]) -> bool:
        return not self.item.matches(tags)


FilterNode = Union[TagPattern, BoolOp, NotNode]

_OPS = ("AND", "OR")


def _parse_pattern(text: str, path: str, at: tuple[str | int, ...] = ()) -> TagPattern:
    if not text:
        raise _refuse(path, at, "pattern must be a non-empty string")
    if text.startswith("regex:"):
        body = text[len("regex:"):]
    elif text.startswith("prefix:"):
        # Pure sugar for regex:^<text> — by definition, not by escaping.
        body = "^" + text[len("prefix:"):]
    else:
        return TagPattern(raw=text)
    if body in ("", "^"):
        raise _refuse(path, at, f"empty pattern in {text!r}")
    try:
        compiled = re.compile(body)
    except re.error as exc:
        raise _refuse(path, at, f"invalid regular expression {text!r}: {exc}") from None
    return TagPattern(raw=text, regex=compiled)


def parse_filter(data: Any, path: str = "tag_filter") -> FilterNode:
    """Parse filter data (as it appears in a runspec) into an AST node. A
    :class:`FilterError` carries where (``path``, the keys and indices from
    the root); a filter nested deeper than :data:`MAX_FILTER_DEPTH` is a
    :class:`FilterTooDeep`."""
    return _parse_node(data, path, (), 0, [0])


def _parse_node(
    data: Any, path: str, at: tuple[str | int, ...], depth: int, patterns: list[int]
) -> FilterNode:
    if isinstance(data, str):
        patterns[0] += 1
        if patterns[0] > MAX_FILTER_PATTERNS:
            raise too_many_patterns(path, at)
        return _parse_pattern(data, path, at)
    if depth >= MAX_FILTER_DEPTH:
        raise _refuse(
            path, at,
            f"the filter nests deeper than {MAX_FILTER_DEPTH} levels — write it flatter "
            "(a run of one operator is one group)",
            FilterTooDeep,
        )
    if isinstance(data, dict):
        keys = set(data)
        if keys == {"not"}:
            return NotNode(item=_parse_node(data["not"], f"{path}.not", (*at, "not"), depth + 1, patterns))
        if keys == {"op", "items"}:
            op = data["op"]
            if op not in _OPS:
                raise _refuse(f"{path}.op", (*at, "op"), f"unknown operator {op!r} (expected 'AND' or 'OR')")
            items = data["items"]
            if not isinstance(items, list) or not items:
                raise _refuse(f"{path}.items", (*at, "items"), "must be a non-empty list")
            return BoolOp(
                op=op,
                items=tuple(
                    _parse_node(child, f"{path}.items[{index}]", (*at, "items", index), depth + 1, patterns)
                    for index, child in enumerate(items)
                ),
            )
        raise _refuse(
            path, at, f"node must be a pattern string, {{op, items}}, or {{not}} — got keys {sorted(keys)}"
        )
    raise _refuse(path, at, f"node must be a string or mapping, got {type(data).__name__}")


def to_data(node: FilterNode) -> Any:
    """Inverse of :func:`parse_filter` — reproduces the original data exactly."""
    if isinstance(node, TagPattern):
        return node.raw
    if isinstance(node, NotNode):
        return {"not": to_data(node.item)}
    if isinstance(node, BoolOp):
        return {"op": node.op, "items": [to_data(child) for child in node.items]}
    raise TypeError(f"not a filter node: {node!r}")


#: The pattern no tag matches: a tag is never the empty string.
_NO_TAG = "regex:^$"
#: "Every item", as a run spec stores it — the AST data of the only node
#: that is true for every item. Always the same data, so it is recognized
#: by equality: :func:`is_everything`.
MATCH_ALL: dict[str, Any] = {"not": _NO_TAG}
#: "Every item", as the text form writes and reads it.
EVERYTHING = "*"


def is_everything(node: FilterNode) -> bool:
    """``node`` is :data:`MATCH_ALL` — written ``*`` in the text form."""
    return isinstance(node, NotNode) and isinstance(node.item, TagPattern) and node.item.raw == _NO_TAG


#: What the one-line text form (``core.filtertext``) reads as an operator.
OPERATOR_WORDS = frozenset({"AND", "OR", "NOT"})
#: The whitespace of the text form: exactly what ``\\s`` matches in the
#: browser's regular expressions, so both readers split a text alike.
TEXT_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_NEEDS_QUOTES = frozenset(TEXT_WHITESPACE + '()"\\')


def quote_pattern(pattern: str) -> str:
    """A pattern as the text form writes it: bare when it reads back as
    itself, else in double quotes with ``\\`` and ``"`` escaped by a
    backslash — a space, a parenthesis, a quote or a backslash in it, or a
    pattern that is an operator word (``"AND"``) or the word for every item
    (``"*"``, the tag literally spelled so), is quoted."""
    if (
        pattern
        and pattern not in OPERATOR_WORDS
        and pattern != EVERYTHING
        and not any(ch in _NEEDS_QUOTES for ch in pattern)
    ):
        return pattern
    escaped = pattern.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def format_filter(node: FilterNode) -> str:
    """The one-line text form of a filter — the canonical spelling, and
    readable back: ``parse_filter_text(format_filter(n))`` is ``to_data(n)``
    for every node (``core.filtertext``).

    AND binds tighter than OR, so a group inside a group, and a group under
    NOT, is parenthesized. A group of exactly one item has no infix spelling
    and is written as its operator applied to it: ``AND (Checkout)``.
    :data:`MATCH_ALL` is written ``*``.
    """
    if isinstance(node, TagPattern):
        return quote_pattern(node.raw)
    if is_everything(node):
        return EVERYTHING
    if isinstance(node, NotNode):
        return f"NOT {_operand(node.item)}"
    if len(node.items) == 1:
        return f"{node.op} ({format_filter(node.items[0])})"
    return f" {node.op} ".join(_operand(child) for child in node.items)


def _operand(node: FilterNode) -> str:
    text = format_filter(node)
    if isinstance(node, BoolOp) and len(node.items) > 1:
        return f"({text})"
    return text
