"""Every place a filter is configured or handed over as one string reads it
as filter text, like a string ``tag_filter`` over the API: the selections of
the ``ui:`` section, the leaves of a taxonomy, and the testing kit's
``FakeDispatchContext.from_source``. A single pattern that is not filter
text keeps meaning that pattern, with the API's warning. Also the language
the server's own free text is written in (``core.text_language``)."""

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

    def test_a_single_pattern_that_is_not_text_works_and_warns_once_at_load_naming_the_entry(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="runcomposer"):
            config = _load(tmp_path, ui={"quick_filters": [{"label": "Cart", "filter": "regex:^Cart(V2)?$"}]})
        entry = config.ui["quick_filters"][0]
        assert (entry["tag_filter"], entry["kind"], entry["text"]) == ("regex:^Cart(V2)?$", "pattern", '"regex:^Cart(V2)?$"')
        warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "ui.quick_filters[0].filter" in warnings[0] and '"regex:^Cart(V2)?$"' in warnings[0]
        caplog.clear()
        _ = config.ui
        assert caplog.records == [], "the warning is said at load, not on every read of the section"

    def test_text_that_cannot_be_read_is_a_config_error_naming_the_entry(self, tmp_path):
        with pytest.raises(ConfigError, match=r"ui\.presets\[0\]\.filter: cannot read the filter text at column 1"):
            _load(tmp_path, ui={"presets": [{"name": "broken", "filter": "AND Smoke"}]})
        with pytest.raises(ConfigError, match=r"ui\.default_filter: the filter text is empty"):
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
    def test_a_leaf_filter_is_filter_text_naming_one_pattern(self):
        document = {"taxonomy": [{"label": "Sprint 12", "filter": '"Sprint 12"'}, {"label": "Odd", "filter": '"Auth OR Cart"'}]}
        warnings: list[str] = []
        validate_taxonomy(document, warnings=warnings)
        assert warnings == []
        resolved = resolve_taxonomy(document, _items())
        sprint, odd = resolved["taxonomy"][:2]
        # A resolved leaf's filter is the pattern itself — what a client adds as one rule.
        assert (sprint["filter"], sprint["item_count"]) == ("Sprint 12", 1)
        assert (odd["filter"], odd["item_count"]) == ("Auth OR Cart", 1)
        assert resolved["warnings"] == []

    def test_a_leaf_that_reads_as_an_expression_is_refused_with_how_to_write_it(self):
        with pytest.raises(TaxonomyError, match=r"reads as the filter expression 'Auth OR Cart'.*\"Auth OR Cart\" for the tag"):
            validate_taxonomy({"taxonomy": [{"label": "Odd", "filter": "Auth OR Cart"}]})
        with pytest.raises(TaxonomyError, match=r"taxonomy\[0\] \('All'\) has the filter '\*'"):
            validate_taxonomy({"taxonomy": [{"label": "All", "filter": "*"}]})

    def test_unreadable_leaf_text_names_the_node(self):
        with pytest.raises(TaxonomyError, match=r"taxonomy\[0\] \('Cart'\) filter: cannot read the filter text"):
            validate_taxonomy({"taxonomy": [{"label": "Cart", "filter": "Cart AND"}]})

    def test_a_single_pattern_that_is_not_text_still_resolves_and_says_how_to_write_it(self):
        document = {"taxonomy": [{"label": "Cart", "filter": "regex:^Cart(V2)?$"}]}
        resolved = resolve_taxonomy(document, _items())
        cart = resolved["taxonomy"][0]
        assert cart["filter"] == "regex:^Cart(V2)?$" and cart["tag_count"] == 2
        [warning] = resolved["warnings"]
        assert "taxonomy[0] ('Cart')" in warning and '"regex:^Cart(V2)?$"' in warning

    def test_the_resolved_taxonomy_over_http_carries_the_warnings(self, make_client, tmp_path):
        (tmp_path / "taxonomy.yaml").write_text(
            "taxonomy:\n  - { label: Cart, filter: 'regex:^Cart(V2)?$' }\n  - { label: Smoke, filter: Smoke }\n",
            encoding="utf-8",
        )
        client, _service = make_client(core={"taxonomy_file": "taxonomy.yaml"})
        body = client.get(f"{API}/taxonomy", params={"resolve": "true"}).json()
        assert len(body["warnings"]) == 1 and "('Cart')" in body["warnings"][0]
        assert client.get(f"{API}/taxonomy").json()["taxonomy"][0]["filter"] == "regex:^Cart(V2)?$", "the file is served as written"


# -- the testing kit ------------------------------------------------------------------------


class TestFakeFromSource:
    def test_a_string_selection_is_filter_text(self):
        assert FakeDispatchContext.from_source(ListSource(_items()), "Cart OR CartV2").item_ids == ("Shop.Cart.T001", "Shop.Cart.T002")
        assert FakeDispatchContext.from_source(ListSource(_items()), '"Auth OR Cart"').item_ids == ("Shop.Auth.T001",)
        assert len(FakeDispatchContext.from_source(ListSource(_items()), "*").item_ids) == 4

    def test_a_single_pattern_that_is_not_text_selects_it_with_a_warning(self):
        with pytest.warns(UserWarning, match=r'write it as "Sprint 12"'):
            ctx = FakeDispatchContext.from_source(ListSource(_items()), "Sprint 12")
        assert ctx.item_ids == ("Shop.Sprint.T001",)


# -- the SDK ----------------------------------------------------------------------------------


def test_the_sdk_exports_the_text_form():
    assert sdk.parse_filter_text("Auth OR Cart") == {"op": "OR", "items": ["Auth", "Cart"]}
    assert sdk.read_filter_string("regex:^Cart(V2)?$")[1] is not None
    assert sdk.quote_pattern("Sprint 12") == '"Sprint 12"'
    with pytest.raises(sdk.FilterSyntaxError) as caught:
        sdk.parse_filter_text("Auth OR (Cart")
    assert json.dumps(caught.value.extra) == '{"problem": "unbalanced-open", "position": 8}'
    assert issubclass(sdk.FilterSyntaxError, sdk.FilterError)


def test_a_syntax_error_over_http_carries_problem_position_and_the_word_found(client):
    # An operator word in it: filter text, not the fallback for single patterns.
    answer = client.post(f"{API}/selection/compile", json={"tag_filter": "Payments AND Smoke Cart"})
    assert answer.status_code == 400
    body = answer.json()
    assert (body["code"], body["problem"], body["position"], body["token"]) == ("filter-syntax", "missing-operator", 19, "Cart")
    assert body["detail"].startswith("cannot read the filter text at column 20")
    unclosed = client.post(f"{API}/selection/compile", json={"tag_filter": "Auth OR (Cart"}).json()
    assert "token" not in unclosed and (unclosed["problem"], unclosed["position"]) == ("unbalanced-open", 8)
