"""Every place a whole filter is configured or handed over as one string
reads it as filter text, like a string ``tag_filter`` over the API: the
selections of the ``ui:`` section and the testing kit's
``FakeDispatchContext.from_source``. A string that does not parse is
refused there as it is over the API, naming the entry and the column.
Where exactly one pattern is expected — a taxonomy leaf's ``filter`` — the
string is that pattern as written, as in 0.1. Also the language the
server's own free text is written in (``core.text_language``)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from runcomposer import sdk
from runcomposer.core.errors import ConfigError
from runcomposer.core.filter import MATCH_ALL
from runcomposer.core.model import Item
from runcomposer.core.taxonomy import TaxonomyError, resolve_taxonomy, validate_taxonomy
from runcomposer.sdk import load_config
from runcomposer.testing import FakeDispatchContext, ListSource

from .conftest import write_config

API = "/api/v1"


def _load(tmp_path: Path, **sections: Any):
    return load_config(str(write_config(tmp_path, **sections)))


# -- the ui section ---------------------------------------------------------------------


class TestConfiguredSelections:
    def test_a_quick_filter_string_is_filter_text(self, tmp_path):
        ui = _load(tmp_path, ui={"quick_filters": ["Auth OR Cart", '"Auth OR Cart"', "Smoke"]}).ui
        union, tag, smoke = ui["quick_filters"]
        assert union["tag_filter"] == {"op": "OR", "items": ["Auth", "Cart"]}
        assert (union["kind"], union["text"], union["label"]) == ("expression", "Auth OR Cart", "Auth OR Cart")
        assert (tag["tag_filter"], tag["kind"], tag["text"]) == ("Auth OR Cart", "pattern", '"Auth OR Cart"')
        assert tag["label"] == "Auth OR Cart", "a single pattern is labelled with the pattern, not its quoted text"
        assert (smoke["tag_filter"], smoke["kind"], smoke["text"], smoke["label"]) == ("Smoke", "pattern", "Smoke", "Smoke")

    def test_presets_and_the_default_filter_read_text_and_serve_canonical_text(self, tmp_path):
        ui = _load(
            tmp_path,
            ui={
                "default_filter": "Smoke AND NOT prefix:Quarantine-",
                "presets": [{"name": "everything", "filter": "*"}, {"name": "cards", "filter": "AND (Payments-Cards)"}],
            },
        ).ui
        assert ui["default_filter"] == {
            "tag_filter": {"op": "AND", "items": ["Smoke", {"not": "prefix:Quarantine-"}]},
            "text": "Smoke AND NOT prefix:Quarantine-",
            "kind": "expression",
        }
        everything, cards = ui["presets"]
        assert (everything["tag_filter"], everything["text"]) == (MATCH_ALL, "*")
        assert (cards["tag_filter"], cards["text"]) == ({"op": "AND", "items": ["Payments-Cards"]}, "AND (Payments-Cards)")

    def test_a_pattern_that_does_not_parse_unquoted_refuses_the_start_naming_the_entry(self, tmp_path, caplog):
        with pytest.raises(ConfigError, match=r"ui\.quick_filters\[0\]\.filter: cannot read the filter text at column 1: "
                                               r"the pattern 'regex:\^Cart' runs into '\('"):
            _load(tmp_path, ui={"quick_filters": [{"label": "Cart", "filter": "regex:^Cart(V2)?$"}]})
        with caplog.at_level(logging.WARNING, logger="runcomposer"):
            config = _load(tmp_path, ui={"quick_filters": [{"label": "Cart", "filter": '"regex:^Cart(V2)?$"'}]})
        entry = config.ui["quick_filters"][0]
        assert (entry["tag_filter"], entry["kind"], entry["text"]) == ("regex:^Cart(V2)?$", "pattern", '"regex:^Cart(V2)?$"')
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == [], "quoted, it is simply read"

    def test_text_that_cannot_be_read_is_a_config_error_naming_the_entry(self, tmp_path):
        with pytest.raises(ConfigError, match=r"ui\.presets\[0\]\.filter: cannot read the filter text at column 1"):
            _load(tmp_path, ui={"presets": [{"name": "broken", "filter": "AND Smoke"}]})
        with pytest.raises(ConfigError, match=r"ui\.default_filter: cannot read the filter text at column 3: the filter text is empty"):
            _load(tmp_path, ui={"default_filter": "  "})

    def test_the_api_serves_the_normalized_entries(self, make_client):
        client, _service = make_client(ui={"quick_filters": ["Auth OR Cart"], "default_filter": '"Sprint 12"'})
        body = client.get(f"{API}/ui-config").json()
        assert body["quick_filters"][0]["tag_filter"] == {"op": "OR", "items": ["Auth", "Cart"]}
        assert body["default_filter"] == {"tag_filter": "Sprint 12", "text": '"Sprint 12"', "kind": "pattern"}
        presets = client.get(f"{API}/presets").json()
        assert presets["quick_filters"] == body["quick_filters"] and presets["default_filter"] == body["default_filter"]


# -- the language of the server's free text -----------------------------------------------


class TestTextLanguage:
    def test_english_by_default(self, tmp_path):
        assert _load(tmp_path).text_language == "en"

    @pytest.mark.parametrize("tag", ["de", "de-CH", "pt-BR", "zh-Hant-TW", "es-419", "EN"])
    def test_a_language_tag_is_taken_as_written(self, tmp_path, tag):
        assert _load(tmp_path, core={"text_language": tag}).text_language == tag

    @pytest.mark.parametrize("value", ["de_CH", "de--CH", "e", "", "de-", "english1", 7, None, ["de"]])
    def test_anything_else_is_a_config_error(self, tmp_path, value):
        with pytest.raises(ConfigError, match=r"core\.text_language must be a BCP 47 language tag"):
            _load(tmp_path, core={"text_language": value})

    def test_ui_config_and_meta_serve_it(self, make_client):
        client, _service = make_client(core={"text_language": "de"})
        assert client.get(f"{API}/ui-config").json()["text_language"] == "de"
        assert client.get(f"{API}/meta").json()["text_language"] == "de"
        default, _ = make_client()
        assert default.get(f"{API}/ui-config").json()["text_language"] == "en"


# -- taxonomy leaves ------------------------------------------------------------------------


def _items() -> list[Item]:
    return [
        Item("Shop.Cart.T001", tags=("Cart",)),
        Item("Shop.Cart.T002", tags=("CartV2",)),
        Item("Shop.Sprint.T001", tags=("Sprint 12",)),
        Item("Shop.Auth.T001", tags=("Auth OR Cart",)),
    ]


class TestTaxonomyLeaves:
    """A leaf's ``filter`` is one pattern written as itself — the taxonomy
    file format of 0.1, unchanged — not filter text: nothing is lexed or
    unquoted, spaces and parentheses are characters of the pattern."""

    def test_a_leaf_filter_is_the_pattern_as_written(self):
        document = {"taxonomy": [
            {"label": "Sprint 12", "filter": "Sprint 12"},
            {"label": "Cart", "filter": "regex:^Cart(V2)?$"},
        ]}
        warnings: list[str] = []
        validate_taxonomy(document, warnings=warnings)
        assert warnings == []
        resolved = resolve_taxonomy(document, _items())
        sprint, cart = resolved["taxonomy"][:2]
        # A resolved leaf's filter is the pattern itself — what a client adds as one rule.
        assert (sprint["filter"], sprint["item_count"]) == ("Sprint 12", 1)
        assert (cart["filter"], cart["tag_count"]) == ("regex:^Cart(V2)?$", 2)
        assert resolved["warnings"] == []
        # And the resolved tree is a taxonomy document again.
        assert validate_taxonomy({"taxonomy": resolved["taxonomy"]})

    def test_a_pattern_in_double_quotes_is_refused_saying_to_write_it_without(self):
        with pytest.raises(TaxonomyError, match=r"\('Cart'\) has the filter '\"regex:\^Cart\(V2\)\?\$\"', a pattern "
                                                r"wrapped in double quotes .* write 'regex:\^Cart\(V2\)\?\$' without the inner quotes"):
            validate_taxonomy({"taxonomy": [{"label": "Cart", "filter": '"regex:^Cart(V2)?$"'}]})

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("", r"has an empty 'filter'"),
            ("   ", r"has an empty 'filter'"),
            ("prefix:", r"empty pattern in 'prefix:'"),
            ("regex:", r"empty pattern in 'regex:'"),
            ("regex:^(unclosed", r"invalid regular expression"),
        ],
    )
    def test_what_names_no_pattern_is_refused_naming_the_node(self, value, message):
        with pytest.raises(TaxonomyError, match=message):
            validate_taxonomy({"taxonomy": [{"label": "Cart", "filter": value}]})

    def test_a_non_string_filter_is_refused_with_the_plain_example(self):
        with pytest.raises(TaxonomyError, match=r'write alternatives as one regex, e\.g\. filter: "regex:\^\(Visa\|Mastercard\)\$"'):
            validate_taxonomy({"taxonomy": [{"label": "Cards", "filter": ["Visa", "Mastercard"]}]})

    @pytest.mark.parametrize("value", ["Cart OR Checkout", "NOT Smoke", "Payments AND Smoke", "*"])
    def test_a_leaf_that_looks_like_an_expression_is_the_tag_spelled_so_with_a_warning(self, value):
        warnings: list[str] = []
        validate_taxonomy({"taxonomy": [{"label": "Odd", "filter": value}]}, warnings=warnings)
        (warning,) = warnings
        assert f"('Odd'): the filter {value!r} is read as the single tag spelled so" in warning
        assert "write alternatives as one regex" in warning
        resolved = resolve_taxonomy({"taxonomy": [{"label": "Odd", "filter": "Auth OR Cart"}]}, _items())
        assert resolved["taxonomy"][0]["filter"] == "Auth OR Cart"  # the tag of that name
        assert len(resolved["warnings"]) == 1

    def test_the_documented_taxonomy_of_0_1_7_loads_unchanged(self):
        """The example of 0.1.7's docs/taxonomy.md, as it was written then."""
        import yaml

        document = yaml.safe_load(OLD_DOCUMENTED_TAXONOMY)
        warnings: list[str] = []
        validate_taxonomy(document, warnings=warnings)
        assert warnings == []

    def test_the_resolved_taxonomy_over_http_serves_the_pattern_as_written(self, make_client, tmp_path):
        (tmp_path / "taxonomy.yaml").write_text(
            "taxonomy:\n  - { label: Cart, filter: 'regex:^Cart(V2)?$' }\n  - { label: Smoke, filter: Smoke }\n",
            encoding="utf-8",
        )
        client, _service = make_client(core={"taxonomy_file": "taxonomy.yaml"})
        body = client.get(f"{API}/taxonomy", params={"resolve": "true"}).json()
        assert body["warnings"] == [] and body["taxonomy"][0]["filter"] == "regex:^Cart(V2)?$"
        assert body["taxonomy"][0]["tag_count"] >= 1
        assert client.get(f"{API}/taxonomy").json()["taxonomy"][0]["filter"] == "regex:^Cart(V2)?$"


#: The example taxonomy of 0.1.7's docs/taxonomy.md (``git show v0.1.7:docs/taxonomy.md``).
OLD_DOCUMENTED_TAXONOMY = """\
taxonomy:
  - label: Areas                                     # group: a heading, not clickable
    children:
      - { label: Payments, filter: "Payments" }      # literal tag — matched case-INsensitively
      - { label: Checkout, filter: "prefix:Checkout-" }   # every tag starting "Checkout-" (case-sensitive)
      - label: Cart                                  # both: clickable AND a parent
        filter: "regex:^Cart(V2)?$"
        children:
          - { label: Cart v2 only, filter: "CartV2" }
  - label: Suites
    children:
      - { label: Smoke, filter: "Smoke" }
      - { label: Regression, filter: "Regression" }
      - { label: Payment methods, filter: "regex:^(Visa|Mastercard|Paypal)$" }   # "any of these" — one regex
  - label: Quarantine                                # a leaf needs no children, at any level
    filter: "prefix:Quarantine-"
"""


# -- the testing kit ------------------------------------------------------------------------


class TestFakeFromSource:
    def test_a_string_selection_is_filter_text(self):
        assert FakeDispatchContext.from_source(ListSource(_items()), "Cart OR CartV2").item_ids == ("Shop.Cart.T001", "Shop.Cart.T002")
        assert FakeDispatchContext.from_source(ListSource(_items()), '"Auth OR Cart"').item_ids == ("Shop.Auth.T001",)
        assert len(FakeDispatchContext.from_source(ListSource(_items()), "*").item_ids) == 4

    def test_a_string_that_does_not_parse_is_refused_as_over_the_api(self):
        with pytest.raises(sdk.FilterSyntaxError) as caught:
            FakeDispatchContext.from_source(ListSource(_items()), "Sprint 12")
        assert (caught.value.problem, caught.value.position) == ("missing-operator", 7)
        assert FakeDispatchContext.from_source(ListSource(_items()), '"Sprint 12"').item_ids == ("Shop.Sprint.T001",)


# -- the SDK ----------------------------------------------------------------------------------


def test_the_sdk_exports_the_text_form():
    assert sdk.parse_filter_text("Auth OR Cart") == {"op": "OR", "items": ["Auth", "Cart"]}
    assert sdk.read_filter_string('"regex:^Cart(V2)?$"') == ("regex:^Cart(V2)?$", None)
    assert sdk.quote_pattern("Sprint 12") == '"Sprint 12"'
    with pytest.raises(sdk.FilterSyntaxError) as caught:
        sdk.parse_filter_text("Auth OR (Cart")
    assert json.dumps(caught.value.extra) == '{"problem": "unbalanced-open", "position": 8}'
    assert issubclass(sdk.FilterSyntaxError, sdk.FilterError)


def test_a_syntax_error_over_http_carries_problem_position_and_the_word_found(client):
    answer = client.post(f"{API}/selection/compile", json={"tag_filter": "Payments AND Smoke Cart"})
    assert answer.status_code == 400
    body = answer.json()
    assert (body["code"], body["problem"], body["position"], body["token"]) == ("filter-syntax", "missing-operator", 19, "Cart")
    assert body["detail"].startswith("cannot read the filter text at column 20")
    unclosed = client.post(f"{API}/selection/compile", json={"tag_filter": "Auth OR (Cart"}).json()
    assert "token" not in unclosed and (unclosed["problem"], unclosed["position"]) == ("unbalanced-open", 8)


class TestTheDriftCheckReadsLeavesAsTheTreeDoes:
    """`taxonomy-check` holds the leaves against the catalog's tags: it reads
    a leaf's filter as the resolved tree does — the pattern as written."""

    def test_a_leaf_claims_its_tags_and_an_expression_lookalike_is_noted_once(self, tmp_path, capsys):
        from runcomposer.cli import main

        from .conftest import write_config

        (tmp_path / "tree.yaml").write_text(
            "taxonomy:\n"
            "  - {label: Cart, filter: 'regex:^Cart(V2)?$'}\n"
            "  - {label: Sprint, filter: Sprint-12}\n"
            "  - {label: Returns, filter: Returns}\n"
            "  - {label: Either, filter: Cart OR Checkout}\n",
            encoding="utf-8",
        )
        config = write_config(tmp_path, core={"taxonomy_file": "tree.yaml"})
        code = main(["taxonomy-check", "--limit", "0", "--config", str(config)])
        out = capsys.readouterr().out
        assert code == 1
        unclaimed = out.split("tags no leaf claims")[1].split("leaves that match nothing")[0].split()
        assert "Cart" not in unclaimed and "CartV2" not in unclaimed and "Sprint-12" not in unclaimed
        assert "leaves that match nothing (clickable, selects nothing): 2" in out
        assert "Returns  →  Returns\n" in out
        # The lookalike matches nothing: its note is on its own row, and nowhere else.
        assert out.count("is read as the single tag spelled so") == 1
        assert "Either  →  Cart OR Checkout  (the filter 'Cart OR Checkout' is read as the single tag" in out

    def test_the_bundled_demo_taxonomy_reads_without_a_warning(self, tmp_path):
        from runcomposer.sdk import Service, load_config

        from .conftest import write_config

        service = Service(load_config(str(write_config(tmp_path))))
        try:
            assert service.resolved_taxonomy().get("warnings", []) == []
        finally:
            service.close()
