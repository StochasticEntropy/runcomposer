"""The one-line text form of a tag filter (DESIGN.md §3.1): reading it into
the AST, writing the AST as text, and the shared conformance cases that the
web UI's reader is held to as well (``tests/fixtures/filter-text.json``)."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from runcomposer.core.errors import InvalidRequest
from runcomposer.core.filter import FilterError, format_filter, parse_filter, quote_pattern
from runcomposer.core.filtertext import (
    FilterSyntaxError,
    format_filter_text,
    parse_filter_text,
    read_filter_string,
)

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "filter-text.json").read_text(encoding="utf-8")
)


def _label(case: dict[str, Any]) -> str:
    return json.dumps(case.get("text", case.get("ast")), ensure_ascii=True)


# -- the shared cases ---------------------------------------------------------------


@pytest.mark.parametrize("case", CASES["parse"], ids=_label)
def test_reading_text_gives_the_ast(case):
    assert parse_filter_text(case["text"]) == case["ast"]


@pytest.mark.parametrize("case", CASES["format"], ids=_label)
def test_writing_the_ast_gives_its_canonical_text_and_reads_back(case):
    assert format_filter_text(case["ast"]) == case["text"]
    assert parse_filter_text(case["text"]) == case["ast"]


@pytest.mark.parametrize("case", CASES["errors"], ids=_label)
def test_unreadable_text_names_the_problem_and_where(case):
    with pytest.raises(FilterSyntaxError) as caught:
        parse_filter_text(case["text"])
    want = case["error"]
    assert (caught.value.problem, caught.value.position) == (want["problem"], want["position"])
    if "token" in want:
        assert caught.value.token == want["token"]


def test_the_shared_cases_cover_every_problem_and_node_shape():
    problems = {case["error"]["problem"] for case in CASES["errors"]}
    assert problems == {
        "empty", "unexpected-end", "unexpected-operator", "missing-operator",
        "unbalanced-open", "unbalanced-close", "unterminated-quote", "needs-quotes",
    }
    shapes = json.dumps([case["ast"] for case in CASES["format"]])
    assert '"not"' in shapes and '"AND"' in shapes and '"OR"' in shapes
    assert any(
        isinstance(case["ast"], dict) and len(case["ast"].get("items", [0, 0])) == 1
        for case in CASES["format"]
    ), "a group of one item is written and read back too"


# -- the grammar in prose -----------------------------------------------------------


def test_not_binds_tighter_than_and_and_and_tighter_than_or():
    assert parse_filter_text("NOT Auth AND Cart OR Checkout") == {
        "op": "OR",
        "items": [{"op": "AND", "items": [{"not": "Auth"}, "Cart"]}, "Checkout"],
    }


def test_operators_are_upper_case_words_only():
    assert parse_filter_text("and") == "and"
    with pytest.raises(FilterSyntaxError) as caught:
        parse_filter_text("Auth or Cart")
    assert caught.value.problem == "missing-operator" and caught.value.token == "or"


def test_a_tag_with_spaces_parentheses_quotes_or_an_operator_name_is_written_quoted():
    for tag in ("Cart V2", "Checkout (Gift)", 'Cart "V2"', "Auth OR Cart", "NOT", "Cart\\V2"):
        text = quote_pattern(tag)
        assert text.startswith('"') and parse_filter_text(text) == tag
    assert quote_pattern("Checkout:Gift") == "Checkout:Gift"  # a colon needs nothing


def test_a_syntax_error_is_an_invalid_request_with_its_position_for_a_transport():
    with pytest.raises(FilterSyntaxError) as caught:
        parse_filter_text("Auth OR (Cart")
    error = caught.value
    assert isinstance(error, InvalidRequest) and isinstance(error, FilterError)
    assert error.code == "filter-syntax"
    assert error.extra == {"problem": "unbalanced-open", "position": 8}
    assert "column 9" in str(error) and "expected AND, OR or ')'" in str(error)


def test_patterns_are_checked_after_the_grammar_with_the_node_path():
    data = parse_filter_text('Auth OR "regex:["')
    with pytest.raises(FilterError, match=r"tag_filter\.items\[1\]: invalid regular expression"):
        parse_filter(data)


def test_deep_nesting_is_refused_as_a_filter_error_not_a_crash():
    with pytest.raises(FilterError, match="nested too deeply"):
        parse_filter_text("(" * 5000 + "Auth" + ")" * 5000)


# -- round trip over generated filters ------------------------------------------------

_PIECES = [
    "Auth", "Cart", "Checkout", "Payments-Cards", "Auth-SSO", "Catalog", "and", "or", "not",
    "AND", "OR", "NOT", " ", "  ", "(", ")", '"', "\\", ":", "\t", "\n", " ", "　",
    "﻿", "\u001c", "\U0001f6d2", "-", "V2",
]
_REGEXES = ["regex:^Cart(V2)?$", "regex:^Auth-", "regex:(?i)^cart", 'regex:^"Cart"$', "regex:^Cart\\d+$"]
_PREFIXES = ["prefix:Checkout-", "prefix:Payments \\(V2\\)", "prefix:Auth\\d"]


def _pattern(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.15:
        return rng.choice(_REGEXES)
    if roll < 0.25:
        return rng.choice(_PREFIXES)
    text = "".join(rng.choice(_PIECES) for _ in range(rng.randint(1, 4)))
    return text if not text.startswith(("regex:", "prefix:")) else "Auth" + text


def _node(rng: random.Random, depth: int) -> Any:
    roll = rng.random()
    if depth >= 4 or roll < 0.4:
        return _pattern(rng)
    if roll < 0.55:
        return {"not": _node(rng, depth + 1)}
    count = 1 if rng.random() < 0.15 else rng.randint(2, 4)
    return {"op": rng.choice(["AND", "OR"]), "items": [_node(rng, depth + 1) for _ in range(count)]}


def test_every_filter_written_as_text_reads_back_as_the_same_ast():
    rng = random.Random(20261120)
    for _ in range(3000):
        ast = _node(rng, 0)
        text = format_filter(parse_filter(ast))
        assert parse_filter_text(text) == ast, text
        assert format_filter_text(parse_filter_text(text)) == text


# -- a filter given as one string -----------------------------------------------------


def test_a_string_is_read_as_filter_text():
    assert read_filter_string("Auth OR Cart") == ({"op": "OR", "items": ["Auth", "Cart"]}, None)
    assert read_filter_string('"Auth OR Cart"') == ("Auth OR Cart", None)
    assert read_filter_string("Payments-Cards") == ("Payments-Cards", None)


@pytest.mark.parametrize(
    "text",
    ["Sprint 12", "regex:^Cart(V2)?$", "regex:^(Auth|Cart)-", "Checkout (Gift)", " Cart V2 ", ")"],
)
def test_a_single_pattern_that_is_not_filter_text_keeps_meaning_that_pattern(text):
    data, warning = read_filter_string(text)
    assert data == text.strip()
    assert warning is not None and quote_pattern(text.strip()) in warning


@pytest.mark.parametrize(
    "text, problem",
    [
        ("Auth OR (Cart", "unbalanced-open"),
        ("Auth AND", "unexpected-end"),
        ('"Auth', "unterminated-quote"),
        ('Auth "Cart"', "missing-operator"),
        ("NOT regex:^Cart(V2)?$", "needs-quotes"),
    ],
)
def test_a_string_with_an_operator_or_a_quote_must_be_filter_text(text, problem):
    with pytest.raises(FilterSyntaxError) as caught:
        read_filter_string(text)
    assert caught.value.problem == problem


@pytest.mark.parametrize("text", ["", "   ", "\t\n"])
def test_blank_text_is_refused_rather_than_read_as_no_filter(text):
    with pytest.raises(FilterError, match="empty"):
        read_filter_string(text)
