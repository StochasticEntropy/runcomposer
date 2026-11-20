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
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, Union

from .errors import InvalidRequest

__all__ = [
    "BoolOp",
    "FilterError",
    "FilterNode",
    "NotNode",
    "TagPattern",
    "format_filter",
    "parse_filter",
    "quote_pattern",
    "to_data",
]


class FilterError(InvalidRequest):
    """Raised when filter data does not conform to the grammar."""

    code = "invalid-filter"


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


def _parse_pattern(text: str, path: str) -> TagPattern:
    if not text:
        raise FilterError(f"{path}: pattern must be a non-empty string")
    if text.startswith("regex:"):
        body = text[len("regex:"):]
    elif text.startswith("prefix:"):
        # Pure sugar for regex:^<text> — by definition, not by escaping.
        body = "^" + text[len("prefix:"):]
    else:
        return TagPattern(raw=text)
    if body in ("", "^"):
        raise FilterError(f"{path}: empty pattern in {text!r}")
    try:
        compiled = re.compile(body)
    except re.error as exc:
        raise FilterError(f"{path}: invalid regular expression {text!r}: {exc}") from None
    return TagPattern(raw=text, regex=compiled)


def parse_filter(data: Any, path: str = "tag_filter") -> FilterNode:
    """Parse filter data (as it appears in a runspec) into an AST node."""
    if isinstance(data, str):
        return _parse_pattern(data, path)
    if isinstance(data, dict):
        keys = set(data)
        if keys == {"not"}:
            return NotNode(item=parse_filter(data["not"], f"{path}.not"))
        if keys == {"op", "items"}:
            op = data["op"]
            if op not in _OPS:
                raise FilterError(f"{path}.op: unknown operator {op!r} (expected 'AND' or 'OR')")
            items = data["items"]
            if not isinstance(items, list) or not items:
                raise FilterError(f"{path}.items: must be a non-empty list")
            return BoolOp(
                op=op,
                items=tuple(
                    parse_filter(child, f"{path}.items[{index}]") for index, child in enumerate(items)
                ),
            )
        raise FilterError(
            f"{path}: node must be a pattern string, {{op, items}}, or {{not}} — got keys {sorted(keys)}"
        )
    raise FilterError(f"{path}: node must be a string or mapping, got {type(data).__name__}")


def to_data(node: FilterNode) -> Any:
    """Inverse of :func:`parse_filter` — reproduces the original data exactly."""
    if isinstance(node, TagPattern):
        return node.raw
    if isinstance(node, NotNode):
        return {"not": to_data(node.item)}
    if isinstance(node, BoolOp):
        return {"op": node.op, "items": [to_data(child) for child in node.items]}
    raise TypeError(f"not a filter node: {node!r}")


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
    pattern that is an operator word (``"AND"``), is quoted."""
    if pattern and pattern not in OPERATOR_WORDS and not any(ch in _NEEDS_QUOTES for ch in pattern):
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
    """
    if isinstance(node, TagPattern):
        return quote_pattern(node.raw)
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
