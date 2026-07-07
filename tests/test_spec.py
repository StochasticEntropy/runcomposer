"""Runspec build/load/validate tests (DESIGN.md §3, versioning policy)."""

import json

import pytest
from jsonschema import Draft202012Validator

from runcomposer.core.spec import (
    build_spec,
    load_document,
    runspec_schema,
    validate_document,
)


def make_spec(**overrides):
    spec = build_spec(
        title="Test run",
        tag_filter={"op": "AND", "items": ["Payments", {"not": "prefix:Quarantine-"}]},
        materialized_ids=["A.T001", "A.T002"],
        source_provider="manifest",
        source_root="corpus.json",
        snapshot="sha256:" + "ab12cd34" * 8,
        results={"expect": [{"format": "demo-verdicts"}], "shards": 1, "deliver": "none"},
        runner={"demo": {"seed": "s"}},
        labels={"origin": "test"},
    )
    spec.update(overrides)
    return spec


def test_schema_file_is_a_valid_draft_2020_12_schema():
    Draft202012Validator.check_schema(runspec_schema())


def test_built_spec_is_valid_for_dispatch():
    report = validate_document(make_spec(), for_dispatch=True)
    assert report.ok, report.errors
    assert report.warnings == []


def test_built_spec_shape_matches_design():
    spec = make_spec()
    assert list(spec) == ["runspec", "run", "selection", "source", "results", "runner"]
    assert spec["runspec"] == "1.0"
    assert spec["selection"]["materialized"]["count"] == 2
    assert len(spec["run"]["id"]) == 26  # ULID


class TestVersioningPolicy:
    def test_higher_major_is_refused(self):
        report = validate_document(make_spec(runspec="2.0"))
        assert not report.ok
        assert "refuses" in report.errors[0]

    def test_higher_minor_of_same_major_is_accepted(self):
        report = validate_document(make_spec(runspec="1.7"))
        assert report.ok, report.errors

    def test_unknown_top_level_section_warns_but_validates(self):
        report = validate_document(make_spec(x_custom={"anything": True}))
        assert report.ok, report.errors
        assert any("x_custom" in w for w in report.warnings)

    def test_unknown_field_inside_known_section_is_ignored(self):
        spec = make_spec()
        spec["run"]["x_future_field"] = "from a 1.1 producer"
        spec["selection"]["x_other"] = 3
        report = validate_document(spec, for_dispatch=True)
        assert report.ok, report.errors

    def test_missing_or_malformed_version_is_an_error(self):
        spec = make_spec()
        del spec["runspec"]
        assert not validate_document(spec).ok
        assert not validate_document(make_spec(runspec="1")).ok
        assert not validate_document(make_spec(runspec=1.0)).ok


class TestSchemaErrors:
    def test_missing_required_sections(self):
        spec = make_spec()
        del spec["source"]
        report = validate_document(spec)
        assert any("source" in e for e in report.errors)

    def test_bad_snapshot_format(self):
        spec = make_spec()
        spec["source"]["snapshot"] = "md5:abc"
        assert not validate_document(spec).ok

    def test_bad_filter_node_shape(self):
        spec = make_spec()
        spec["selection"]["tag_filter"] = {"op": "XOR", "items": ["a"]}
        assert not validate_document(spec).ok

    def test_invalid_regex_in_filter_is_caught_beyond_schema(self):
        # Shape-valid per JSON Schema, semantically invalid — the parser check catches it.
        spec = make_spec()
        spec["selection"]["tag_filter"] = "regex:["
        report = validate_document(spec)
        assert any("regular expression" in e for e in report.errors)

    def test_materialized_count_mismatch(self):
        spec = make_spec()
        spec["selection"]["materialized"]["count"] = 99
        report = validate_document(spec)
        assert any("count is 99" in e for e in report.errors)

    def test_non_mapping_document(self):
        assert not validate_document(["not", "a", "spec"]).ok


class TestDispatchProfile:
    def test_preview_without_materialized_validates_base_profile_only(self):
        spec = make_spec()
        del spec["selection"]["materialized"]
        assert validate_document(spec).ok
        report = validate_document(spec, for_dispatch=True)
        assert any("materialized" in e for e in report.errors)

    def test_dispatch_requires_results_section(self):
        spec = make_spec()
        del spec["results"]
        assert validate_document(spec).ok
        report = validate_document(spec, for_dispatch=True)
        assert any("results" in e for e in report.errors)

    def test_empty_materialized_list_warns(self):
        spec = make_spec()
        spec["selection"]["materialized"] = {"item_ids": [], "at": "2026-07-07T00:00:00Z", "count": 0}
        report = validate_document(spec, for_dispatch=True)
        assert report.ok
        assert any("nothing to execute" in w for w in report.warnings)


class TestLoading:
    def test_yaml_and_json_are_isomorphic(self, tmp_path):
        spec = make_spec()
        json_path = tmp_path / "spec.json"
        json_path.write_text(json.dumps(spec), encoding="utf-8")
        yaml_path = tmp_path / "spec.yaml"
        import yaml

        yaml_path.write_text(yaml.safe_dump(spec), encoding="utf-8")
        assert load_document(json_path) == load_document(yaml_path) == spec

    def test_parse_errors_are_reported_with_the_path(self, tmp_path):
        from runcomposer.core.spec import SpecLoadError

        bad = tmp_path / "bad.json"
        bad.write_text("{nope", encoding="utf-8")
        with pytest.raises(SpecLoadError, match="bad.json"):
            load_document(bad)
