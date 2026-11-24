"""The optional fields of a ``manifest`` item, as ADOPTING.md lists them:
``name``, ``hierarchy``, ``meta`` and ``aliases`` — and what each does."""

from __future__ import annotations

import json

import pytest

from runcomposer.plugins.manifest_source import ManifestError, ManifestSource

from .conftest import compose

ITEM = {"id": "shop.cart.add", "tags": ["Cart", "Smoke"], "name": "Add an item to the cart",
        "hierarchy": ["Shop", "Cart"], "meta": {"owner": "team-checkout"}, "aliases": ["Cart.Add Item"]}


def manifest(tmp_path, *items):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"items": list(items)}), encoding="utf-8")
    return path


def test_every_optional_field_reaches_the_item(tmp_path):
    (item,) = ManifestSource(manifest(tmp_path, ITEM)).items()
    assert (item.name, item.display_name) == ("Add an item to the cart", "Add an item to the cart")
    assert item.hierarchy == ("Shop", "Cart") and dict(item.meta) == {"owner": "team-checkout"}
    source = ManifestSource(manifest(tmp_path, ITEM))
    assert source.resolve("Cart.Add Item") == "shop.cart.add" and source.resolve("shop.cart.add") == "shop.cart.add"


def test_without_a_name_an_item_is_called_by_its_id(tmp_path):
    (item,) = ManifestSource(manifest(tmp_path, {"id": "shop.cart.add", "tags": ["Cart"]})).items()
    assert item.display_name == "shop.cart.add"


def test_a_colliding_alias_is_refused_at_load(tmp_path):
    other = {"id": "shop.cart.remove", "tags": ["Cart"], "aliases": ["Cart.Add Item"]}
    with pytest.raises(ManifestError, match="collides"):
        ManifestSource(manifest(tmp_path, ITEM, other))


def test_renaming_an_item_is_no_drift_and_a_run_keeps_the_name_it_was_composed_with(tmp_path, make_service):
    before = ManifestSource(manifest(tmp_path, ITEM)).snapshot()
    after = ManifestSource(manifest(tmp_path, {**ITEM, "name": "Put an item in the cart", "meta": {}})).snapshot()
    assert before == after
    (tmp_path / "deployment").mkdir()
    path = manifest(tmp_path / "deployment", ITEM)
    service = make_service(sources={"manifest": {"path": str(path)}})
    run_id = compose(service, "Cart")
    assert service.store.item_names(run_id) == {"shop.cart.add": "Add an item to the cart"}
