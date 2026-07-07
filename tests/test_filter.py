"""Tag-filter grammar unit tests (DESIGN.md §3.1)."""

import pytest

from runcomposer.core.filter import (
    BoolOp,
    FilterError,
    NotNode,
    TagPattern,
    format_filter,
    parse_filter,
    to_data,
)

TAGS = ("Payments", "Checkout-Express", "Sprint-12", "CartV2")


def matches(data, tags=TAGS):
    return parse_filter(data).matches(tags)


class TestPatterns:
    def test_literal_matches_exactly(self):
        assert matches("Payments")
        assert not matches("Payment")

    def test_literal_is_case_insensitive(self):
        assert matches("payments")
        assert matches("PAYMENTS")
        assert parse_filter("Payments").matches(("payments",))

    def test_prefix_is_sugar_for_anchored_regex(self):
        assert matches("prefix:Checkout-")
        assert not matches("prefix:heckout")  # anchored at the start
        node = parse_filter("prefix:Checkout-")
        assert isinstance(node, TagPattern) and node.regex.pattern == "^Checkout-"

    def test_prefix_is_case_sensitive_like_regex(self):
        assert not matches("prefix:checkout-")

    def test_regex_uses_search_with_explicit_anchors(self):
        assert matches("regex:^Cart(V2)?$")
        assert matches("regex:Sprint")  # unanchored: substring
        assert not matches("regex:^print")

    def test_empty_and_invalid_patterns_are_errors(self):
        for bad in ("", "regex:", "prefix:", "regex:["):
            with pytest.raises(FilterError):
                parse_filter(bad)


class TestBoolOps:
    def test_and_or_not(self):
        assert matches({"op": "AND", "items": ["Payments", "Sprint-12"]})
        assert not matches({"op": "AND", "items": ["Payments", "Sprint-99"]})
        assert matches({"op": "OR", "items": ["Sprint-99", "Payments"]})
        assert not matches({"op": "OR", "items": ["Sprint-99", "Nope"]})
        assert matches({"not": "Sprint-99"})
        assert not matches({"not": "Payments"})

    def test_nesting(self):
        node = {
            "op": "AND",
            "items": [
                {"op": "OR", "items": ["Payments", "prefix:Cart"]},
                {"not": "regex:^Quarantine-"},
            ],
        }
        assert matches(node)
        assert not matches(node, tags=("Payments", "Quarantine-Flaky"))

    def test_grammar_errors(self):
        with pytest.raises(FilterError):
            parse_filter({"op": "XOR", "items": ["a"]})
        with pytest.raises(FilterError):
            parse_filter({"op": "AND", "items": []})
        with pytest.raises(FilterError):
            parse_filter({"op": "AND"})
        with pytest.raises(FilterError):
            parse_filter({"not": "a", "op": "AND", "items": ["b"]})
        with pytest.raises(FilterError):
            parse_filter(42)

    def test_error_messages_carry_the_path(self):
        with pytest.raises(FilterError, match=r"tag_filter\.items\[1\]\.not"):
            parse_filter({"op": "AND", "items": ["ok", {"not": ""}]})


class TestLosslessness:
    ROUNDTRIP_CASES = [
        "Payments",
        "prefix:Checkout-",
        "regex:^Cart(V2)?$",
        {"not": "prefix:Quarantine-"},
        {
            "op": "AND",
            "items": ["Payments", {"op": "OR", "items": ["prefix:Checkout-", "regex:^Cart(V2)?$"]}],
        },
    ]

    @pytest.mark.parametrize("data", ROUNDTRIP_CASES, ids=str)
    def test_roundtrip_is_exact(self, data):
        assert to_data(parse_filter(data)) == data

    def test_prefix_stays_prefix(self):
        # prefix: is sugar for regex:^ semantically, but the document form
        # must survive a parse/serialize cycle unchanged.
        assert to_data(parse_filter("prefix:X")) == "prefix:X"


def test_format_filter_renders_readably():
    node = parse_filter(
        {
            "op": "AND",
            "items": [
                {"op": "OR", "items": ["Payments", "prefix:Checkout-"]},
                {"not": "prefix:Quarantine-"},
            ],
        }
    )
    assert format_filter(node) == "(Payments OR prefix:Checkout-) AND NOT prefix:Quarantine-"
