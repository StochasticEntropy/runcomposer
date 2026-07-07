"""`manifest` TestSource tests, incl. the bundled demo corpus (DESIGN.md §6.1, §12)."""

import json
from importlib import resources

import pytest

from runcomposer.plugins.manifest_source import ManifestError, ManifestSource


def demo_corpus_path():
    return resources.files("runcomposer.demo") / "corpus.json"


class TestDemoCorpus:
    def test_loads_sixty_items(self):
        source = ManifestSource(demo_corpus_path())
        assert len(source.items()) == 60

    def test_ids_are_unique_and_areas_are_neutral(self):
        items = ManifestSource(demo_corpus_path()).items()
        ids = [item.id for item in items]
        assert len(set(ids)) == len(ids)
        areas = {item.hierarchy[1] for item in items}
        assert areas == {"Payments", "Checkout", "Cart", "Catalog", "Auth"}

    def test_tag_patterns_from_design_12_exist(self):
        items = ManifestSource(demo_corpus_path()).items()
        all_tags = {tag for item in items for tag in item.tags}
        assert any(tag.startswith("Sprint-") for tag in all_tags)
        assert any(tag.startswith("SHOP-") for tag in all_tags)  # ticket pattern
        assert any(tag.startswith("Quarantine-") for tag in all_tags)
        assert {"Smoke", "Regression", "CartV2"} <= all_tags

    def test_resolve_is_identity_over_ids(self):
        source = ManifestSource(demo_corpus_path())
        some_id = source.items()[0].id
        assert source.resolve(some_id) == some_id
        assert source.resolve("Not.An.Item") is None


class TestSnapshot:
    def test_snapshot_format_and_stability(self, tmp_path):
        data = {"items": [{"id": "a", "tags": ["x"]}, {"id": "b", "tags": ["y"]}]}
        path = tmp_path / "m.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        first = ManifestSource(path).snapshot()
        assert first.startswith("sha256:") and len(first) == 7 + 64
        assert ManifestSource(path).snapshot() == first

    def test_snapshot_changes_when_content_changes(self, tmp_path):
        path = tmp_path / "m.json"
        path.write_text(json.dumps({"items": [{"id": "a", "tags": ["x"]}]}), encoding="utf-8")
        before = ManifestSource(path).snapshot()
        path.write_text(json.dumps({"items": [{"id": "a", "tags": ["x", "y"]}]}), encoding="utf-8")
        assert ManifestSource(path).snapshot() != before


class TestValidation:
    @pytest.mark.parametrize(
        "data, match",
        [
            ([], "'items' list"),
            ({"items": [{"tags": ["x"]}]}, "'id' is required"),
            ({"items": [{"id": "a"}]}, "'tags' is required"),
            ({"items": [{"id": "a", "tags": "x"}]}, "list of non-empty strings"),
            ({"items": [{"id": "a", "tags": ["x"]}, {"id": "a", "tags": ["y"]}]}, "duplicate"),
        ],
        ids=["no-items", "missing-id", "missing-tags", "tags-not-list", "duplicate-id"],
    )
    def test_malformed_manifests_are_refused(self, tmp_path, data, match):
        path = tmp_path / "m.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ManifestError, match=match):
            ManifestSource(path)

    def test_minimal_items_need_only_id_and_tags(self, tmp_path):
        # The zero-dependency adopter path (DESIGN.md §6.1).
        path = tmp_path / "m.yaml"
        path.write_text("items:\n  - id: only.id.and.tags\n    tags: [t1]\n", encoding="utf-8")
        item = ManifestSource(path).items()[0]
        assert item.id == "only.id.and.tags"
        assert item.display_name == "only.id.and.tags"


def test_pytest_flavored_manifest_example_loads():
    """The framework-agnostic claim, demonstrated (DESIGN.md §6.1)."""
    from pathlib import Path

    example = Path(__file__).parent.parent / "examples" / "manifest-pytest.json"
    source = ManifestSource(example)
    items = source.items()
    assert len(items) >= 5
    assert all("::" in item.id for item in items)  # pytest nodeids, opaque to the core
