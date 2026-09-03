"""End-to-end tests through the CLI: demo boots, validate validates (DESIGN.md §12, §14 P0)."""

import json
import shlex
from pathlib import Path

import pytest

from runcomposer.cli import main

EXAMPLE_SPEC = str(Path(__file__).parent.parent / "examples" / "webshop-regression.runspec.yaml")


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    """`demo` seeds a workspace in the working directory — never the repo."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def next_steps(out: str) -> list[list[str]]:
    """The commands the demo tells the reader to paste, as argv lists."""
    lines = out.splitlines()
    steps = []
    for line in lines[lines.index("Next steps:") + 1 :]:
        if not line.startswith("  "):
            break
        argv = shlex.split(line.strip())
        assert argv[0] == "runcomposer", argv
        steps.append(argv[1:])
    return steps


def demo_service(workdir: Path, workspace: str = "runcomposer-demo"):
    from runcomposer.config import load_config
    from runcomposer.service import Service

    return Service(load_config(str(workdir / workspace / "config.yaml")))


class TestDemo:
    def test_demo_runs_end_to_end(self, workdir, capsys):
        assert main(["demo"]) == 0
        out = capsys.readouterr().out
        assert "runcomposer demo — fictional web-shop corpus" in out
        assert "Corpus: 60 items" in out
        assert "validates against the runspec-1.0 schema" in out
        assert "Results:" in out and "PASS" in out
        assert "Rerun what failed" in out
        assert "derived_from" in out

    def test_demo_is_deterministic(self, workdir, capsys):
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


class TestDemoSeedsRealHistory:
    """§12/§6.3: the demo pre-seeds history at BOTH levels — completed runs and
    per-item verdicts/durations — into a real store, so failed-rerun selection
    and duration-balanced planning are demonstrable immediately.

    The regression: it narrated all of that and persisted nothing, so the next
    command a reader tried ('runs --failed-in latest') contradicted the demo.
    """

    def test_the_printed_next_steps_actually_run(self, workdir, capsys):
        assert main(["demo"]) == 0
        steps = next_steps(capsys.readouterr().out)
        assert len(steps) >= 4

        commands = []
        for argv in steps:
            if argv[0] == "serve":  # a blocking server, not runnable in-process
                assert Path(argv[argv.index("--config") + 1]).is_file()
                continue
            assert main(argv) == 0, argv
            commands.append(argv[0])
        printed = capsys.readouterr().out

        assert {"runs", "compile", "export"} <= set(commands)
        assert "no runs stored" not in printed
        # the flagship loop, answered from the store the demo just wrote
        failed = next(line for line in printed.splitlines() if "item(s) FAILED in run" in line)
        assert failed.startswith("# 5 item(s) FAILED in run")

    def test_history_is_persisted_at_both_levels(self, workdir):
        assert main(["demo"]) == 0
        service = demo_service(workdir)

        runs = service.store.list_runs(limit=50)
        assert len(runs) == 5
        assert {run.state for run in runs} == {"COMPLETE"}
        assert all(run.completed_at for run in runs)

        nightly = [run for run in runs if run.labels.get("suite") == "nightly"]
        assert len(nightly) == 3
        verdicts = service.store.verdicts_for(nightly[0].id)
        assert len(verdicts) == 46
        assert {v.status for v in verdicts} >= {"PASS", "FAIL"}
        assert all(v.duration_ms > 0 for v in verdicts if v.status != "SKIP")

        # level two: the duration aggregates duration-balanced chunking reads.
        assert len(service.store.duration_aggregates(last_n=5)) >= 46

    def test_the_scoped_history_selector_resolves_out_of_the_store(self, workdir):
        assert main(["demo"]) == 0
        item_ids, provenance = demo_service(workdir).resolve_history(
            "failed@latest?suite=nightly"
        )
        assert len(item_ids) == 5
        assert provenance["query"]["labels"] == {"suite": "nightly"}

    def test_nothing_is_written_outside_the_workspace(self, workdir):
        assert main(["demo"]) == 0
        assert [p.name for p in workdir.iterdir()] == ["runcomposer-demo"]
        # in particular NOT the zero-config default store, which a later real
        # run in this directory would then share with fake data.
        assert not (workdir / "runcomposer.db").exists()

    def test_re_running_re_seeds_instead_of_piling_up(self, workdir):
        assert main(["demo"]) == 0
        assert main(["demo"]) == 0
        assert len(demo_service(workdir).store.list_runs(limit=50)) == 5

    def test_workspace_flag_puts_it_where_asked(self, workdir, capsys):
        assert main(["demo", "--workspace", "elsewhere/demo"]) == 0
        assert (workdir / "elsewhere" / "demo" / "runcomposer.db").is_file()
        assert not (workdir / "runcomposer-demo").exists()
        assert next_steps(capsys.readouterr().out)[0][-1] == "elsewhere/demo/config.yaml"

    def test_it_refuses_to_seed_into_somebody_elses_config_directory(self, workdir, capsys):
        deployment = workdir / "deployment"
        deployment.mkdir()
        real = deployment / "config.yaml"
        real.write_text("store: {sqlite: {path: real.db}}\n", encoding="utf-8")

        assert main(["demo", "--workspace", str(deployment)]) == 2
        assert "refusing to overwrite" in capsys.readouterr().err
        assert real.read_text(encoding="utf-8") == "store: {sqlite: {path: real.db}}\n"

    def test_an_unwritable_working_directory_falls_back_and_says_so(self, workdir, capsys):
        """A container or an installed package tree is not a reason to fail:
        the printed next steps carry the path either way."""
        import os

        workdir.chmod(0o555)
        try:
            if os.access(workdir, os.W_OK):  # running as root: the guard is moot
                pytest.skip("working directory is writable despite mode 0555")
            assert main(["demo"]) == 0
        finally:
            workdir.chmod(0o755)

        out = capsys.readouterr().out
        assert "could not create ./runcomposer-demo" in out
        config = Path(next_steps(out)[0][-1])
        assert config.is_absolute() and config.is_file()
        assert not (workdir / "runcomposer-demo").exists()

    def test_it_refuses_a_non_empty_directory(self, workdir, capsys):
        occupied = workdir / "occupied"
        occupied.mkdir()
        (occupied / "notes.txt").write_text("mine", encoding="utf-8")

        assert main(["demo", "--workspace", str(occupied)]) == 2
        assert "not a demo workspace" in capsys.readouterr().err


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
        assert out.startswith("# 60 items, 47 distinct tags — snapshot sha256:")
        assert "Shop.Payments.Cards.T001" in out

    def test_catalog_tags_lists_the_tag_world_with_counts(self, capsys):
        assert main(["catalog", "--tags"]) == 0
        out = capsys.readouterr().out
        assert "# 60 items, 47 distinct tags" in out
        # Every tag, sorted, with the number of items carrying it — the answer
        # to "what can I filter on" before any filter is written.
        assert "    46  Regression" in out
        assert "     1  SHOP-1200" in out
        assert len([line for line in out.splitlines() if not line.startswith("#")]) == 47


    def test_taxonomy_check_names_tags_no_leaf_claims(self, capsys):
        # The bundled taxonomy is a demo tree, not a complete one — so the
        # check has something to report, which is the point: drift between a
        # hand-written tree and a moving catalog is invisible otherwise.
        assert main(["taxonomy-check"]) == 1
        out = capsys.readouterr().out
        assert "taxonomy leaf pattern(s) over 47 distinct catalog tag(s)" in out
        assert "tags no leaf claims" in out
        assert "Payments-Cards" in out
        assert "every taxonomy leaf matches at least one tag" in out

    def test_taxonomy_check_warn_only_still_reports(self, capsys):
        assert main(["taxonomy-check", "--warn-only"]) == 0
        assert "tags no leaf claims" in capsys.readouterr().out

    def test_taxonomy_check_names_leaves_that_match_nothing(self, tmp_path, capsys):
        (tmp_path / "taxonomy.yaml").write_text(
            "taxonomy:\n"
            "  - label: Areas\n"
            "    children:\n"
            "      - label: Payments\n"
            "        filter: Payments\n"
            "      - label: Renamed away\n"
            "        filter: Billing\n",
            encoding="utf-8",
        )
        (tmp_path / "config.yaml").write_text(
            f"core:\n  taxonomy_file: taxonomy.yaml\nstore:\n"
            f"  sqlite: {{ path: {tmp_path / 'runs.db'} }}\n",
            encoding="utf-8",
        )
        assert main(["taxonomy-check", "--config", str(tmp_path / "config.yaml")]) == 1
        out = capsys.readouterr().out
        # A leaf whose tag was renamed stays clickable and selects nothing —
        # the failure mode nothing else surfaces.
        assert "leaves that match nothing" in out
        assert "Areas › Renamed away  →  Billing" in out


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
