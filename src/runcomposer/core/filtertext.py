"""The one-line text form of a tag filter (DESIGN.md §3.1) — what a person
types, reads and shares::

    Payments AND (Smoke OR Regression) AND NOT prefix:Quarantine-

Grammar::

    expr    := and ("OR" and)*
    and     := unary ("AND" unary)*
    unary   := "NOT" unary | "(" expr ")" | ("AND" | "OR") "(" expr ")" | "*" | pattern
    pattern := bare | '"' quoted '"'

- NOT binds tighter than AND, AND tighter than OR; parentheses group. A run
  of one operator is one group: ``A AND B AND C`` is one AND of three.
- The operators are the upper-case words ``AND``, ``OR`` and ``NOT``; a tag
  spelled ``and`` is an ordinary tag.
- A bare pattern runs up to whitespace, a parenthesis or a double quote. A
  quoted pattern may contain anything; inside it a backslash takes the next
  character literally (``\\"``, ``\\\\``). A pattern that is an operator
  word, or contains whitespace, a parenthesis or a quote, is written quoted.
- A bare ``regex:``/``prefix:`` pattern that runs into a parenthesis or a
  quote is refused rather than cut short: ``"regex:^Cart(V2)?$"`` is quoted.
  A ``regex:``/``prefix:`` with nothing after it is refused, bare or quoted.
- ``AND (x)`` / ``OR (x)`` is a group of exactly one item — the one AST shape
  infix notation cannot spell otherwise.
- ``*`` is every item of the catalog. The AST has no node of its own for
  that; ``*`` reads as :data:`.filter.MATCH_ALL` (``{"not": "regex:^$"}``,
  true of every item), and that node is written ``*``. Only the bare word
  ``*`` is: ``Cart*`` is a pattern, and ``"*"`` is the tag spelled ``*``.
- Empty or blank text is no filter (``None``).

The text is only an input and a display: what a run spec stores is the AST
(:mod:`.filter`), and ``parse_filter_text(format_filter(n)) == to_data(n)``
for every node. Errors name the offset where reading stopped (``position``,
counted in Unicode code points from 0 — what a Python ``str`` indexes; the
web UI converts its UTF-16 offsets to this unit) and what was expected there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .filter import (
    EVERYTHING,
    MATCH_ALL,
    MAX_FILTER_DEPTH,
    MAX_FILTER_PATTERNS,
    OPERATOR_WORDS,
    TEXT_WHITESPACE,
    FilterError,
    FilterTooDeep,
    format_filter,
    parse_filter,
    too_many_patterns,
)

__all__ = [
    "FilterSyntaxError",
    "format_filter_text",
    "parse_filter_text",
    "read_filter_string",
]

_BREAKS = frozenset(TEXT_WHITESPACE + '()"')
_SPACE = frozenset(TEXT_WHITESPACE)


class FilterSyntaxError(FilterError):
    """The filter text cannot be read. ``problem`` classifies it (the same
    identifiers the UI's reader uses), ``position`` is the 0-based offset in
    characters where reading stopped, ``token`` the word found there."""

    code = "filter-syntax"

    def __init__(self, problem: str, position: int, message: str, token: str = ""):
        super().__init__(f"cannot read the filter text at column {position + 1}: {message}")
        self.problem = problem
        self.position = position
        self.token = token
        # What a transport adds to its error body; the word found there only
        # where there is one (`missing-operator`, `needs-quotes`, …).
        self.extra = {"problem": problem, "position": position}
        if token:
            self.extra["token"] = token


class FilterTextTooDeep(FilterSyntaxError, FilterTooDeep):
    """Filter text nested deeper than ``MAX_FILTER_DEPTH`` levels: problem
    ``too-deep`` at the NOT or parenthesis that goes one level too far, and
    the code ``filter-too-deep`` an AST nested too deeply has as well."""

    code = "filter-too-deep"


@dataclass(frozen=True)
class _Token:
    kind: str  # "op" | "open" | "close" | "all" | "pattern"
    at: int
    value: str = ""


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in _SPACE:
            index += 1
        elif char == "(":
            tokens.append(_Token("open", index))
            index += 1
        elif char == ")":
            tokens.append(_Token("close", index))
            index += 1
        elif char == '"':
            start = index
            index += 1
            value: list[str] = []
            closed = False
            while index < length:
                current = text[index]
                if current == "\\" and index + 1 < length:
                    value.append(text[index + 1])
                    index += 2
                elif current == '"':
                    closed = True
                    index += 1
                    break
                else:
                    value.append(current)
                    index += 1
            if not closed:
                raise FilterSyntaxError(
                    "unterminated-quote", start, 'the quote opened here is never closed — expected a closing \'"\''
                )
            tokens.append(_Token("pattern", start, "".join(value)))
        else:
            start = index
            while index < length and text[index] not in _BREAKS:
                index += 1
            word = text[start:index]
            if word.startswith(("regex:", "prefix:")) and index < length and text[index] in '("':
                raise FilterSyntaxError(
                    "needs-quotes",
                    start,
                    f"the pattern {word!r} runs into {text[index]!r} — a pattern containing a "
                    'parenthesis or a quote is written in double quotes, e.g. "regex:^Cart(V2)?$"',
                    word,
                )
            kind = "all" if word == EVERYTHING else "op" if word in OPERATOR_WORDS else "pattern"
            tokens.append(_Token(kind, start, word))
    return tokens


_OPERAND = 'a pattern, *, NOT or "("'
#: A pattern kind's marker on its own: ``prefix:`` / ``regex:`` with nothing
#: after it is no pattern.
_BARE_KINDS = ("prefix:", "regex:")


class _Parser:
    def __init__(self, text: str, tokens: list[_Token]):
        self.end = len(text)
        self.tokens = tokens
        self.cursor = 0
        self.depth = 0

    def deeper(self, token: _Token) -> None:
        """One level more (a NOT, a group); refused past MAX_FILTER_DEPTH."""
        self.depth += 1
        if self.depth > MAX_FILTER_DEPTH:
            raise FilterTextTooDeep(
                "too-deep", token.at,
                f"the filter nests deeper than {MAX_FILTER_DEPTH} levels — write it flatter "
                "(a run of one operator is one group)",
            )

    def peek(self) -> _Token | None:
        return self.tokens[self.cursor] if self.cursor < len(self.tokens) else None

    def unary(self) -> Any:
        token = self.peek()
        if token is None:
            raise FilterSyntaxError("unexpected-end", self.end, f"the text ends where {_OPERAND} was expected")
        self.cursor += 1
        if token.kind == "all":
            return dict(MATCH_ALL)
        if token.kind == "pattern":
            if token.value == "":
                raise FilterSyntaxError("empty", token.at, 'an empty pattern ("") — a pattern needs a character')
            if token.value in _BARE_KINDS:
                raise FilterSyntaxError(
                    "empty", token.at,
                    f"{token.value!r} with nothing after it — a {token.value[:-1]} pattern needs "
                    f"{'an expression' if token.value == 'regex:' else 'a text'}",
                    token.value,
                )
            return token.value
        if token.kind == "op":
            if token.value == "NOT":
                self.deeper(token)
                inner = self.unary()
                self.depth -= 1
                return {"not": inner}
            following = self.peek()
            if following is not None and following.kind == "open":
                # `AND (x)`: a group of one item.
                self.cursor += 1
                return {"op": token.value, "items": [self.group(following)]}
            raise FilterSyntaxError(
                "unexpected-operator", token.at, f"{token.value!r} where {_OPERAND} was expected", token.value
            )
        if token.kind == "open":
            return self.group(token)
        raise FilterSyntaxError("unbalanced-close", token.at, f"')' where {_OPERAND} was expected")

    def group(self, opening: _Token) -> Any:
        self.deeper(opening)
        inner = self.expression()
        self.depth -= 1
        closing = self.peek()
        if closing is None or closing.kind != "close":
            raise FilterSyntaxError(
                "unbalanced-open", opening.at, "the '(' opened here is not closed — expected AND, OR or ')'"
            )
        self.cursor += 1
        return inner

    def chain(self, op: str, operand: Any) -> Any:
        items = [operand()]
        while (token := self.peek()) is not None and token.kind == "op" and token.value == op:
            self.cursor += 1
            items.append(operand())
        return items[0] if len(items) == 1 else {"op": op, "items": items}

    def conjunction(self) -> Any:
        return self.chain("AND", self.unary)

    def expression(self) -> Any:
        return self.chain("OR", self.conjunction)


def parse_filter_text(text: str) -> Any:
    """Text → filter AST data (the shape a run spec's ``tag_filter`` holds),
    or ``None`` for empty or blank text. Only the grammar is checked here;
    :func:`.filter.parse_filter` checks the patterns themselves (a regular
    expression that does not compile). Raises :class:`FilterSyntaxError`."""
    if not isinstance(text, str):
        raise FilterError(f"filter text must be a string, got {type(text).__name__}")
    tokens = _tokenize(text)
    if not tokens:
        return None
    # Counted before anything is built: the bound of the AST, the same number.
    if sum(1 for token in tokens if token.kind in ("pattern", "all")) > MAX_FILTER_PATTERNS:
        raise too_many_patterns("filter text")
    parser = _Parser(text, tokens)
    try:
        ast = parser.expression()
    except RecursionError:
        raise FilterError("the filter text is nested too deeply to read") from None
    rest = parser.peek()
    if rest is not None:
        if rest.kind == "close":
            raise FilterSyntaxError("unbalanced-close", rest.at, "this ')' closes no '('")
        found = rest.value if rest.kind in ("pattern", "op", "all") else "("
        raise FilterSyntaxError(
            "missing-operator", rest.at, f"{found!r} where AND, OR or the end of the text was expected", found
        )
    return ast


def format_filter_text(data: Any) -> str:
    """Filter AST data → its canonical text; ``""`` for no filter. The
    inverse of :func:`parse_filter_text`."""
    if data is None:
        return ""
    return format_filter(parse_filter(data))


def read_filter_string(text: str) -> tuple[Any, str | None]:
    """A filter given as one string where a filter is taken (a selection's
    ``tag_filter``, the command line's filter argument, a configured quick
    filter, preset or default filter) → ``(AST data, None)``. A taxonomy
    leaf's ``filter`` is not one: it is one pattern, written as itself.

    The string is filter text, read by :func:`parse_filter_text` and by
    nothing else — one grammar, one answer, the same as the web UI's text
    editor: a single tag or ``prefix:``/``regex:`` pattern is a single
    token of that grammar and reads as itself; anything that does not parse
    (``Sprint 12``, ``(Payments``, a bare ``regex:^Cart(V2)?$``) is refused
    with :class:`FilterSyntaxError`, naming the position and what was
    expected there — quote a pattern that holds whitespace, a parenthesis or
    a quote (:func:`.filter.quote_pattern`). Empty or blank text is refused
    too (problem ``empty``): leave the filter out to select without one.

    The second element is always ``None``: there is no reading with a
    warning any more. It stays so that the signature does not change."""
    if not isinstance(text, str):
        raise FilterError(f"filter text must be a string, got {type(text).__name__}")
    data = parse_filter_text(text)
    if data is None:
        raise FilterSyntaxError(
            "empty", len(text), "the filter text is empty — leave the filter out to select without one"
        )
    return data, None
