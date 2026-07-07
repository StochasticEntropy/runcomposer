"""End-to-end tests through the CLI: demo boots, validate validates (DESIGN.md §12, §14 P0)."""

import json
from pathlib import Path

import pytest

from runcomposer.cli import main

EXAMPLE_SPEC = str(Path(__file__).parent.parent / "examples" / "webshop-regression.runspec.yaml")


class TestDemo:
    def test_demo_runs_end_to_end(self, capsys):
        assert main(["demo"]) == 0
        out = capsys.readouterr().out
        assert "runcomposer demo — fictional web-shop corpus" in out
        assert "Corpus: 60 items" in out
        assert "validates against the runspec-1.0 schema" in out
        assert "Results:" in out and "PASS" in out
        assert "Rerun what failed" in out
        assert "derived_from" in out

    def test_demo_is_deterministic(self, capsys):
        """Seeded runner: verdict lines must be identical across invocations
        (only minted ids and timestamps differ)."""

        def result_lines():
            main(["demo"])
            return [
                line
                for line in capsys.readouterr().out.splitlines()
                if line.startswith(("Results:", "  FAIL", "Selection", "  matched"))
            ]

        assert result_lines() == result_lines()


class TestValidateCli:
    def test_validate_accepts_the_shipped_example(self, capsys):
        assert main(["validate", EXAMPLE_SPEC]) == 0
        assert main(["validate", "--for-dispatch", EXAMPLE_SPEC]) == 0
        assert "valid runspec document (dispatch profile)" in capsys.readouterr().out

    def test_validate_rejects_a_broken_spec(self, tmp_path, capsys):
        bad = {"runspec": "1.0", "run": {"id": "x"}}  # missing created_at, selection, source
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        assert main(["validate", str(path)]) == 1
        assert "INVALID" in capsys.readouterr().err

    def test_validate_refuses_higher_major(self, tmp_path, capsys):
        path = tmp_path / "future.json"
        path.write_text(json.dumps({"runspec": "2.0"}), encoding="utf-8")
        assert main(["validate", str(path)]) == 1
        assert "refuses" in capsys.readouterr().err

    def test_validate_reports_unreadable_files(self, capsys):
        assert main(["validate", "does-not-exist.yaml"]) == 2

    def test_catalog_lists_the_demo_corpus(self, capsys):
        assert main(["catalog", "--limit", "3"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("# 60 items — snapshot sha256:")
        assert "Shop.Payments.Cards.T001" in out


class TestExampleSpecHonesty:
    """Every capability claim in docs is demonstrated, not asserted:
    the shipped example spec must be *true* against the shipped corpus."""

    @pytest.fixture()
    def example(self):
        from runcomposer.core.spec import load_document

        return load_document(EXAMPLE_SPEC)

    @pytest.fixture()
    def source(self):
        from importlib import resources

        from runcomposer.plugins.manifest_source import ManifestSource

        return ManifestSource(resources.files("runcomposer.demo") / "corpus.json")

    def test_snapshot_matches_the_bundled_corpus(self, example, source):
        assert example["source"]["snapshot"] == source.snapshot()

    def test_materialized_list_is_the_filter_result(self, example, source):
        from runcomposer.core.selection import Selection

        compiled = Selection.from_data({"tag_filter": example["selection"]["tag_filter"]}).compile(
            source.items()
        )
        assert [item.id for item in compiled] == example["selection"]["materialized"]["item_ids"]
        assert example["selection"]["materialized"]["count"] == len(compiled)
