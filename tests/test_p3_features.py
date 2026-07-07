"""P3 tests: junit-xml parser + pytest example (§6.1/§5), history-based
selection (§7/§6.3), CTRF export (§14)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from runcomposer.cli import main as cli_main
from runcomposer.config import Config
from runcomposer.core.model import Verdict
from runcomposer.core.spec import build_spec
from runcomposer.plugins.junit_parser import JunitXmlParser, ParseError
from runcomposer.plugins.manifest_source import ManifestError, ManifestSource
from runcomposer.plugins.sqlite_store import SqliteRunStore
from runcomposer.service import Service, ServiceError

REPO = Path(__file__).parent.parent
PYTEST_SHOP = REPO / "examples" / "pytest-shop"

JUNIT_SAMPLE = """<?xml version="1.0"?>
<testsuite name="s" tests="4">
  <testcase classname="test_cart" name="test_add_item" time="0.12"/>
  <testcase classname="test_cart" name="test_broken" time="0.5">
    <failure message="assert failed">trace</failure>
  </testcase>
  <testcase classname="test_cart" name="test_error" time="0.1">
    <error message="boom"/>
  </testcase>
  <testcase name="orphan_no_classname" time="0">
    <skipped message="not today"/>
  </testcase>
</testsuite>
"""


class TestJunitParser:
    def test_statuses_names_durations(self, tmp_path):
        file = tmp_path / "junit.xml"
        file.write_text(JUNIT_SAMPLE, encoding="utf-8")
        parsed = {p.native_name: p for p in JunitXmlParser().parse(file)}
        assert parsed["test_cart.test_add_item"].status == "PASS"
        assert parsed["test_cart.test_add_item"].duration_ms == 120
        assert parsed["test_cart.test_broken"].status == "FAIL"
        assert parsed["test_cart.test_broken"].message == "assert failed"
        assert parsed["test_cart.test_error"].status == "ERROR"
        assert parsed["orphan_no_classname"].status == "SKIP"

    @pytest.mark.parametrize("payload", ["<!DOCTYPE", "<!ENTITY"])
    def test_doctype_entity_refused(self, tmp_path, payload):
        file = tmp_path / "junit.xml"
        file.write_text(f'{payload} x [ <!ENTITY e SYSTEM "file:///etc/passwd"> ]>\n{JUNIT_SAMPLE}',
                        encoding="utf-8")
        with pytest.raises(ParseError, match="defused"):
            JunitXmlParser().parse(file)

    def test_malformed_and_wrong_root_refused(self, tmp_path):
        bad = tmp_path / "junit.xml"
        bad.write_text("<testsuite><unclosed>", encoding="utf-8")
        with pytest.raises(ParseError, match="not parsable"):
            JunitXmlParser().parse(bad)
        wrong = tmp_path / "junit2.xml"
        wrong.write_text("<robot><test/></robot>", encoding="utf-8")
        with pytest.raises(ParseError, match="not a JUnit"):
            JunitXmlParser().parse(wrong)


class TestManifestAliases:
    def test_alias_resolves_to_id(self):
        source = ManifestSource(PYTEST_SHOP / "manifest.json")
        assert source.resolve("test_cart.test_add_item") == "test_cart.py::test_add_item"
        assert source.resolve("test_cart.py::test_add_item") == "test_cart.py::test_add_item"
        assert source.resolve("nope") is None

    def test_alias_collision_refused(self, tmp_path):
        path = tmp_path / "m.json"
        path.write_text(json.dumps({"items": [
            {"id": "a", "tags": ["x"], "aliases": ["dup"]},
            {"id": "b", "tags": ["x"], "aliases": ["dup"]},
        ]}), encoding="utf-8")
        with pytest.raises(ManifestError, match="exactly one item id"):
            ManifestSource(path)


@pytest.fixture()
def pytest_config(tmp_path):
    return Config(data={
        "store": {"sqlite": {"path": str(tmp_path / "p3.db")}},
        "core": {"ingestion": {"inbox": None, "quarantine_dir": str(tmp_path / "q")}},
        "sources": {"manifest": {"path": str(PYTEST_SHOP / "manifest.json")}},
    })


class TestPytestLoopEndToEnd:
    """§6.1/§14: the framework-agnosticism proof with REAL pytest."""

    def test_compose_exec_ingest_with_real_pytest(self, tmp_path, pytest_config):
        import runcomposer_exec

        service = Service(pytest_config)
        result = service.compose_run(
            {"tag_filter": "Cart"}, title="pytest loop", origin="test", expect_format="junit-xml"
        )
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(service.store.get_spec_document(result.run.id)), encoding="utf-8")
        service.export_dispatch(result.run.id, spec_bytes=spec_path.read_bytes())
        bundle = tmp_path / "bundle"
        command = f'"{sys.executable}" "{PYTEST_SHOP / "run_pytest.py"}" {{ids_file}} {{out_dir}}'
        assert runcomposer_exec.main([str(spec_path), "--out", str(bundle), "--command", command]) == 0
        report = service.ingest(bundle)
        assert report.verdict_count == 5 and report.warnings == []
        run = service.store.get_run(result.run.id)
        assert run.state == "COMPLETE" and run.completion == "FAIL"
        statuses = {v.item_id: v.status for v in service.store.verdicts_for(run.id)}
        assert statuses["test_cart.py::test_total_never_negative"] == "FAIL"
        assert statuses["test_cart.py::test_checkout[visa]"] == "PASS"

    def test_id_space_invariant(self, tmp_path):
        """§2: every native name the parser emits resolves to exactly ONE id."""
        out_dir = tmp_path / "junit-out"
        subprocess.run(
            [sys.executable, str(PYTEST_SHOP / "run_pytest.py"),
             "/dev/stdin", str(out_dir)],
            input="\n".join(i.id for i in ManifestSource(PYTEST_SHOP / "manifest.json").items()),
            text=True, check=False,
        )
        source = ManifestSource(PYTEST_SHOP / "manifest.json")
        parsed = JunitXmlParser().parse(out_dir / "junit.xml")
        assert len(parsed) == 7
        resolutions = [source.resolve(p.native_name) for p in parsed]
        assert all(r is not None for r in resolutions), resolutions
        assert len(set(resolutions)) == len(resolutions)  # exactly one id each, no collisions


class TestHistorySelection:
    def _seed_completed(self, store, run_id_suffix, statuses, completed_at, state="COMPLETE"):
        spec = build_spec(
            title=f"seed {run_id_suffix}", tag_filter="Smoke",
            materialized_ids=list(statuses), source_provider="manifest",
            snapshot="sha256:" + "ab" * 32,
            created_at=completed_at,
        )
        run = store.create_run(spec, origin="test")
        store.add_dispatch(run.id, dispatch_id=f"D-{run.id}", mode="export", declared_shards=1)
        store.record_delivery(
            run.id, dispatch_id=f"D-{run.id}", shard="1",
            content_hash=f"sha256:{run.id}", format="runcomposer-verdicts",
            verdicts=[Verdict(i, s) for i, s in statuses.items()],
        )
        store.set_run_state(run.id, state,
                            completion="FAIL" if state == "COMPLETE" else None,
                            completed_at=completed_at if state == "COMPLETE" else None)
        return run

    @pytest.fixture()
    def service(self, tmp_path):
        # corpus = the demo web-shop manifest (ids Shop.*)
        return Service(Config(data={
            "store": {"sqlite": {"path": str(tmp_path / "h.db")}},
            "core": {"ingestion": {"inbox": None, "quarantine_dir": str(tmp_path / "q")}},
        }))

    def test_latest_means_latest_completed(self, service):
        store = service.store
        self._seed_completed(store, "old", {"Shop.Payments.Cards.T001": "FAIL"}, "2026-07-01T00:00:00Z")
        newest_completed = self._seed_completed(
            store, "new", {"Shop.Payments.Cards.T002": "FAIL", "Shop.Payments.Cards.T003": "PASS"},
            "2026-07-02T00:00:00Z",
        )
        # an even NEWER run that is only AWAITING_RESULTS must not be selected
        self._seed_completed(store, "await", {"Shop.Payments.Cards.T004": "FAIL"},
                             "2026-07-03T00:00:00Z", state="AWAITING_RESULTS")
        ids, provenance = service.resolve_history("failed@latest")
        assert ids == ["Shop.Payments.Cards.T002"]
        assert provenance["resolved_run_id"] == newest_completed.id
        assert provenance["query"] == {"run": "LATEST", "verdicts": ["FAIL"]}

    def test_before_selector_and_run_selector(self, service):
        store = service.store
        early = self._seed_completed(store, "e", {"Shop.Auth.Login.T001": "FAIL"}, "2026-07-01T00:00:00Z")
        self._seed_completed(store, "l", {"Shop.Auth.Login.T002": "FAIL"}, "2026-07-05T00:00:00Z")
        ids, provenance = service.resolve_history("failed@before:2026-07-02T00:00:00Z")
        assert ids == ["Shop.Auth.Login.T001"]
        assert provenance["query"]["run"] == {"before": "2026-07-02T00:00:00Z"}
        ids, provenance = service.resolve_history(f"passed@run:{early.id}")
        assert ids == [] and provenance["resolved_run_id"] == early.id

    def test_empty_store_goes_dark_not_wrong(self, service):
        with pytest.raises(ServiceError, match="dark on a fresh store"):
            service.resolve_history("failed@latest")

    def test_bad_queries_rejected(self, service):
        with pytest.raises(ServiceError, match="verdict"):
            service.resolve_history("exploded@latest")
        with pytest.raises(ServiceError, match="selector"):
            service.resolve_history("failed@yesterday")

    def test_compose_embeds_provenance_and_materializes_only_failures(self, service):
        self._seed_completed(service.store, "s",
                             {"Shop.Payments.Cards.T001": "FAIL", "Shop.Payments.Cards.T002": "PASS",
                              "Gone.Item.T999": "FAIL"},
                             "2026-07-01T00:00:00Z")
        result = service.compose_run({"history": "failed@latest"}, title="rerun", origin="test")
        spec = result.spec
        assert spec["selection"]["materialized"]["item_ids"] == ["Shop.Payments.Cards.T001"]
        assert spec["selection"]["derived_from"][0]["provider"] == "history"
        assert any("Gone.Item.T999" in w for w in result.warnings)  # dropped, not invented

    def test_cli_failed_in_latest(self, tmp_path, capsys):
        config = tmp_path / "config.yaml"
        config.write_text(f"store:\n  sqlite: {{ path: {tmp_path / 'c.db'} }}\n", encoding="utf-8")
        assert cli_main(["runs", "--failed-in", "latest", "--config", str(config)]) == 1
        assert "dark on a fresh store" in capsys.readouterr().err


class TestCtrfExport:
    def test_structure_and_mapping(self, tmp_path):
        store = SqliteRunStore(path=tmp_path / "ctrf.db")
        service = Service(Config(data={"store": {"sqlite": {"path": str(tmp_path / "ctrf.db")}},
                                       "core": {"ingestion": {"inbox": None}}}))
        spec = build_spec(title="ctrf", tag_filter="Smoke", materialized_ids=["A", "B", "C"],
                          source_provider="manifest", snapshot="sha256:" + "ab" * 32,
                          created_at="2026-07-07T10:00:00Z")
        run = service.store.create_run(spec, origin="test")
        service.store.add_dispatch(run.id, dispatch_id="D1", mode="export", declared_shards=1)
        service.store.record_delivery(
            run.id, dispatch_id="D1", shard="1", content_hash="sha256:x",
            format="runcomposer-verdicts",
            verdicts=[Verdict("A", "PASS", 100), Verdict("B", "FAIL", 200, message="boom"),
                      Verdict("C", "SKIP", 0)],
        )
        service.store.set_run_state(run.id, "COMPLETE", completion="FAIL",
                                    completed_at="2026-07-07T10:05:00Z")
        document = service.export_ctrf(run.id)
        results = document["results"]
        assert results["tool"]["name"] == "runcomposer"
        summary = results["summary"]
        assert (summary["tests"], summary["passed"], summary["failed"], summary["skipped"]) == (3, 1, 1, 1)
        assert summary["stop"] - summary["start"] == 5 * 60 * 1000
        by_name = {t["name"]: t for t in results["tests"]}
        assert by_name["B"]["status"] == "failed" and by_name["B"]["message"] == "boom"
        assert by_name["A"]["duration"] == 100

    def test_cli_unknown_format_rejected(self, tmp_path, capsys):
        config = tmp_path / "config.yaml"
        config.write_text(f"store:\n  sqlite: {{ path: {tmp_path / 'c.db'} }}\n", encoding="utf-8")
        assert cli_main(["export", "some-run", "--format", "junit", "--config", str(config)]) == 2
        assert "unknown export format" in capsys.readouterr().err
