"""Export round-trip tests (DESIGN.md §14 P1): compose → spec → runcomposer-exec
→ marker bundle → ingest → COMPLETE — plus §5 idempotency and marker
verification, and the §6.2c single-file/stdlib-only construction guarantees."""

import ast
import json
import sys
from pathlib import Path

import pytest

import runcomposer_exec
from runcomposer.cli import main as cli_main
from runcomposer.config import Config
from runcomposer.service import IngestError, Service

REPO = Path(__file__).parent.parent


@pytest.fixture()
def config_path(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"store:\n  sqlite: {{ path: {tmp_path / 'roundtrip.db'} }}\n", encoding="utf-8"
    )
    return str(path)


@pytest.fixture()
def service(config_path):
    from runcomposer.config import load_config

    return Service(load_config(config_path))


def compose_exported_spec(tmp_path, config_path):
    """CLI: runcomposer spec --export -o spec.json — returns (spec_path, run_id, dispatch_id)."""
    spec_path = tmp_path / "spec.json"
    exit_code = cli_main(
        [
            "spec",
            "Regression",
            "--title",
            "Round trip",
            "--format",
            "json",
            "-o",
            str(spec_path),
            "--export",
            "--config",
            config_path,
        ]
    )
    assert exit_code == 0
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    return spec_path, spec["run"]["id"]


class TestRoundTrip:
    def test_compose_exec_ingest_reaches_complete(self, tmp_path, config_path, service, capsys):
        spec_path, run_id = compose_exported_spec(tmp_path, config_path)
        run = service.store.get_run(run_id)
        assert run.state == "AWAITING_RESULTS"
        dispatch = run.dispatches[-1]

        bundle = tmp_path / "bundle"
        assert runcomposer_exec.main(
            [str(spec_path), "--out", str(bundle), "--simulate", "--dispatch", dispatch.dispatch_id]
        ) == 0
        marker = json.loads((bundle / "runcomposer_run.json").read_text())
        assert marker["run_id"] == run_id
        assert marker["spec_sha256"] == dispatch.spec_sha256

        assert cli_main(["ingest", str(bundle), "--config", config_path]) == 0
        out = capsys.readouterr().out
        assert "delivery recorded" in out and "COMPLETE" in out

        after = service.store.get_run(run_id)
        assert after.state == "COMPLETE"
        assert after.completion in ("PASS", "FAIL")
        spec_doc = service.store.get_spec_document(run_id)
        assert len(service.store.verdicts_for(run_id)) == spec_doc["selection"]["materialized"]["count"]

    def test_reingesting_identical_bundle_is_noop(self, tmp_path, config_path, service, capsys):
        spec_path, run_id = compose_exported_spec(tmp_path, config_path)
        bundle = tmp_path / "bundle"
        runcomposer_exec.main([str(spec_path), "--out", str(bundle), "--simulate"])
        assert cli_main(["ingest", str(bundle), "--config", config_path]) == 0
        verdicts_before = service.store.verdicts_for(run_id)

        assert cli_main(["ingest", str(bundle), "--config", config_path]) == 0
        assert "no-op" in capsys.readouterr().out
        assert service.store.verdicts_for(run_id) == verdicts_before
        assert len(service.store.get_run(run_id).deliveries) == 1

    def test_corrected_bundle_replaces_shard_verdicts(self, tmp_path, config_path, service):
        spec_path, run_id = compose_exported_spec(tmp_path, config_path)
        bundle = tmp_path / "bundle"
        runcomposer_exec.main([str(spec_path), "--out", str(bundle), "--simulate", "--seed", "s1"])
        cli_main(["ingest", str(bundle), "--config", config_path])
        runcomposer_exec.main([str(spec_path), "--out", str(bundle), "--simulate", "--seed", "s2"])
        assert cli_main(["ingest", str(bundle), "--config", config_path]) == 0
        assert len(service.store.get_run(run_id).deliveries) == 1  # replaced, not accumulated

    def test_mismatched_spec_sha_is_refused(self, tmp_path, config_path, service, capsys):
        spec_path, run_id = compose_exported_spec(tmp_path, config_path)
        bundle = tmp_path / "bundle"
        runcomposer_exec.main([str(spec_path), "--out", str(bundle), "--simulate"])
        marker_path = bundle / "runcomposer_run.json"
        marker = json.loads(marker_path.read_text())
        marker["spec_sha256"] = "0" * 64  # tampered / different spec bytes
        marker_path.write_text(json.dumps(marker), encoding="utf-8")

        assert cli_main(["ingest", str(bundle), "--config", config_path]) == 1
        assert "refusing bundle" in capsys.readouterr().err
        assert service.store.verdicts_for(run_id) == []  # nothing landed
        assert service.store.get_run(run_id).state == "AWAITING_RESULTS"

    def test_markerless_bundle_without_run_is_refused(self, tmp_path, config_path, service):
        results = tmp_path / "loose"
        results.mkdir()
        (results / "results.json").write_text(
            json.dumps({"format": "runcomposer-verdicts", "verdicts": []}), encoding="utf-8"
        )
        with pytest.raises(IngestError, match="no run id was given"):
            service.ingest(results)

    def test_unknown_native_names_warn_but_do_not_invent_items(self, tmp_path, service):
        result = service.compose_run({"tag_filter": "Smoke"}, title="T", origin="test")
        bundle = tmp_path / "b"
        bundle.mkdir()
        (bundle / "results.json").write_text(
            json.dumps(
                {
                    "format": "runcomposer-verdicts",
                    "verdicts": [
                        {"name": result.items[0].id, "status": "PASS"},
                        {"name": "Totally.Unknown.Test", "status": "FAIL"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        report = service.ingest(bundle, run_id=result.run.id)
        assert report.verdict_count == 1
        assert any("Totally.Unknown.Test" in w for w in report.warnings)


class TestExecConstruction:
    """§6.2c: single self-contained stdlib-only file — verified, not asserted."""

    EXEC_FILE = REPO / "src" / "runcomposer_exec.py"

    def test_is_a_single_file_module(self):
        assert self.EXEC_FILE.is_file()
        assert not (REPO / "src" / "runcomposer_exec").exists()  # no package sibling

    def test_imports_are_stdlib_only(self):
        tree = ast.parse(self.EXEC_FILE.read_text(encoding="utf-8"))
        stdlib = set(sys.stdlib_module_names)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                assert top in stdlib or top == "yaml", (
                    f"runcomposer_exec.py imports {name!r} — it must stay stdlib-only "
                    "(yaml is allowed only inside the guarded best-effort import)"
                )

    def test_yaml_import_is_guarded_best_effort(self):
        text = self.EXEC_FILE.read_text(encoding="utf-8")
        guarded = "try:\n        import yaml"
        assert guarded in text, "yaml must only be imported inside try/except ImportError"

    def test_refuses_higher_major(self, tmp_path):
        spec = {"runspec": "2.0", "run": {"id": "x"}}
        path = tmp_path / "future.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        with pytest.raises(SystemExit, match="refuses"):
            runcomposer_exec.main([str(path), "--simulate"])

    def test_refuses_unmaterialized_spec(self, tmp_path):
        spec = {"runspec": "1.0", "run": {"id": "x"}, "selection": {"tag_filter": "a"}}
        path = tmp_path / "preview.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        with pytest.raises(SystemExit, match="materialized"):
            runcomposer_exec.main([str(path), "--simulate"])

    def test_command_mode_renders_ids_file(self, tmp_path, config_path):
        spec_path, _ = compose_exported_spec(tmp_path, config_path)
        out_dir = tmp_path / "cmd-out"
        sentinel = tmp_path / "seen.txt"
        exit_code = runcomposer_exec.main(
            [str(spec_path), "--out", str(out_dir), "--command", f"cp {{ids_file}} {sentinel}"]
        )
        assert exit_code == 0
        ids = sentinel.read_text(encoding="utf-8").strip().splitlines()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        assert ids == spec["selection"]["materialized"]["item_ids"]
        assert (out_dir / "runcomposer_run.json").is_file()
