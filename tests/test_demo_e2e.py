"""End to end through the CLI: `runcomposer demo` boots the web-shop world
(DESIGN.md §12), the commands it prints work when pasted, and the quickstart
round trip of .github/workflows/ci.yml reaches COMPLETE."""

import json
import os
import shlex
from pathlib import Path

import pytest
import yaml

from runcomposer.cli import main

EXAMPLE_SPEC = str(Path(__file__).parent.parent / "examples" / "webshop-regression.runspec.yaml")


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    """`demo` seeds a workspace in the working directory — never the repo."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def split_command_line(line: str) -> list[str]:
    """``line`` as the program it starts receives it: split by POSIX shell
    rules, or on Windows by Windows itself (``CommandLineToArgvW``, the
    rules a program there reads its command line by) — the line a person
    pastes there is not shell syntax, and a backslash is not an escape."""
    if os.name != "nt":
        return shlex.split(line)
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)  # type: ignore[attr-defined]
    shell32.CommandLineToArgvW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
    count = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(line, ctypes.byref(count))
    try:
        return [argv[index] for index in range(count.value)]
    finally:
        kernel32.LocalFree(argv)


def next_steps(out: str) -> list[list[str]]:
    """The commands the demo tells the reader to paste, as argv lists."""
    lines = out.splitlines()
    steps = []
    for line in lines[lines.index("Next steps:") + 1 :]:
        if not line.startswith("  "):
            break
        argv = split_command_line(line.strip())
        assert argv[0] == "runcomposer", argv
        steps.append(argv[1:])
    return steps


@pytest.fixture()
def demo_service(workdir):
    """The Service over the workspace a demo run seeded — closed afterwards."""
    from runcomposer.sdk import Service, load_config

    opened = []

    def open_it(workspace: str = "runcomposer-demo"):
        service = Service(load_config(str(workdir / workspace / "config.yaml")))
        opened.append(service)
        return service

    yield open_it
    for service in opened:
        service.close()


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

    def test_demo_says_that_it_simulates(self, workdir, capsys):
        """Nothing is executed by the demo runner; the demo must not read as
        if something had been."""
        assert main(["demo"]) == 0
        out = capsys.readouterr().out
        assert "Dispatched to the 'demo' runner (simulated)" in out
        assert "simulated failure (demo runner)" in out
        assert "s simulated)" in out

    def test_the_generated_config_configures_the_demo_runner_explicitly(self, workdir):
        """Only what is configured under `runners:` is dispatchable — the demo
        workspace is no exception, so its config says so in the open."""
        assert main(["demo"]) == 0
        text = (workdir / "runcomposer-demo" / "config.yaml").read_text(encoding="utf-8")
        config = yaml.safe_load(text)
        assert config["runners"] == {"demo": {}}
        assert config["store"] == {"sqlite": {"path": "runcomposer.db"}}
        assert "sources" not in config  # the bundled corpus is the default catalog
        assert "SIMULATES" in text

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
            # `run` answers with its exit code whether the run is PASS: the
            # simulated Checkout run has one failure, so the pasted command
            # works and exits 1 — "the run is not PASS", not "it broke".
            assert main(argv) == (1 if argv[0] == "run" else 0), argv
            commands.append(argv[0])
        printed = capsys.readouterr().out

        assert {"runs", "compile", "export", "run"} <= set(commands)
        assert "no runs stored" not in printed
        assert "state: COMPLETE (FAIL) — 11 PASS, 1 FAIL, 1 SKIP" in printed
        # the flagship loop, answered from the store the demo just wrote
        failed = next(line for line in printed.splitlines() if "item(s) FAILED in run" in line)
        assert failed.startswith("# 5 item(s) FAILED in run")

    def test_history_is_persisted_at_both_levels(self, workdir, demo_service):
        assert main(["demo"]) == 0
        service = demo_service()

        runs = service.list_runs(limit=50).runs
        assert len(runs) == 5
        assert {run.state for run in runs} == {"COMPLETE"}
        assert all(run.completed_at for run in runs)
        assert {run.latest_dispatch.mode for run in runs} == {"demo"}
        assert {run.origin for run in runs} == {"demo"}

        nightly = [run for run in runs if run.labels.get("suite") == "nightly"]
        assert len(nightly) == 3
        verdicts = service.store.verdicts_for(nightly[0].id)
        assert len(verdicts) == 46
        assert {v.status for v in verdicts} >= {"PASS", "FAIL"}
        assert all(v.duration_ms > 0 for v in verdicts if v.status != "SKIP")

        # level two: the duration aggregates duration-balanced chunking reads.
        assert len(service.store.duration_aggregates(last_n=5)) >= 46

    def test_the_scoped_history_selector_resolves_out_of_the_store(self, workdir, demo_service):
        assert main(["demo"]) == 0
        item_ids, provenance = demo_service().resolve_history(
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

    def test_re_running_re_seeds_instead_of_piling_up(self, workdir, demo_service):
        assert main(["demo"]) == 0
        assert main(["demo"]) == 0
        assert demo_service().list_runs(limit=50).total == 5

    def test_the_seed_travels_in_the_document_as_a_declared_parameter(self, workdir, demo_service):
        """Three nightly runs differ through ONE configured runner because the
        seed is a per-run parameter, written into the spec's open section."""
        assert main(["demo"]) == 0
        service = demo_service()
        nightly = [r for r in service.list_runs(limit=50).runs if r.labels.get("suite") == "nightly"]
        seeds = {service.require_spec(run.id)["runner"]["demo"]["seed"] for run in nightly}
        assert seeds == {"nightly-1", "nightly-2", "nightly-3"}
        assert {run.latest_dispatch.parameters["seed"] for run in nightly} == seeds

    def test_workspace_flag_puts_it_where_asked(self, workdir, capsys):
        assert main(["demo", "--workspace", "elsewhere/demo"]) == 0
        assert (workdir / "elsewhere" / "demo" / "runcomposer.db").is_file()
        assert not (workdir / "runcomposer-demo").exists()
        assert Path(next_steps(capsys.readouterr().out)[0][-1]) == Path("elsewhere/demo/config.yaml")

    def test_a_workspace_with_a_space_in_its_path_is_one_argument_when_pasted(self, workdir, capsys):
        """Before, the printed path was not quoted: pasted, ``nightly demo``
        was two arguments, and the command failed."""
        assert main(["demo", "--workspace", "nightly demo"]) == 0
        out = capsys.readouterr().out
        steps = next_steps(out)
        for argv in steps:
            assert Path(argv[argv.index("--config") + 1]) == Path("nightly demo/config.yaml"), argv
        assert main(steps[0]) == 0
        removal = next(line for line in out.splitlines() if line.startswith("Delete the workspace"))
        assert split_command_line(removal.split(":  ", 1)[1])[-1] == "nightly demo"

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


class TestValidateAndCatalogCli:
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

    def test_catalog_lists_the_demo_corpus(self, workdir, capsys):
        assert main(["catalog", "--limit", "3"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("# 60 items, 47 distinct tags — snapshot sha256:")
        assert "Shop.Payments.Cards.T001" in out

    def test_catalog_tags_lists_the_tag_world_with_counts(self, workdir, capsys):
        assert main(["catalog", "--tags"]) == 0
        out = capsys.readouterr().out
        assert "# 60 items, 47 distinct tags" in out
        # Every tag, sorted, with the number of items carrying it — the answer
        # to "what can I filter on" before any filter is written.
        assert "    46  Regression" in out
        assert "     1  SHOP-1200" in out
        assert len([line for line in out.splitlines() if not line.startswith("#")]) == 47


    def test_piping_into_head_is_not_an_error(self, tmp_path):
        """`runcomposer catalog | head` must not end in a traceback.

        The reader closing the pipe is it saying "enough" — and Python then
        raises a *second* time while flushing stdout at exit, so an unhandled
        BrokenPipeError costs the user two tracebacks for having used `head`.

        The catalog has to be big enough to overflow the pipe buffer (~64 KB),
        or the writer finishes before the reader closes and nothing breaks —
        which is exactly why the bundled 60-item corpus never surfaced this.
        """
        import json
        import subprocess
        import sys

        manifest = tmp_path / "big.json"
        manifest.write_text(
            json.dumps({"items": [
                {"id": f"Shop.Catalog.Browse{n // 50}.T{n:05d}",
                 "tags": ["Regression", f"Sprint-{n % 30}", f"SHOP-{n:04d}"]}
                for n in range(4000)
            ]}),
            encoding="utf-8",
        )
        run = (
            f'{sys.executable} -c "from runcomposer.cli import main; '
            f'raise SystemExit(main())" catalog --manifest {manifest} --limit 0'
        )
        full = subprocess.run(run, shell=True, capture_output=True)
        assert full.returncode == 0 and full.stderr == b"", full.stderr.decode()
        assert len(full.stdout) > 65536, "corpus too small to close the pipe early"

        piped = subprocess.run(f"{run} | head -1", shell=True, capture_output=True)
        assert b"BrokenPipeError" not in piped.stderr, piped.stderr.decode()
        assert b"Traceback" not in piped.stderr, piped.stderr.decode()
        assert piped.stdout.startswith(b"# 4000 items")


class TestExampleSpecHonesty:
    """Every capability claim in docs is demonstrated, not asserted:
    the shipped example spec must be *true* against the shipped corpus."""

    @pytest.fixture()
    def example(self):
        from runcomposer.core.spec import load_document

        return load_document(EXAMPLE_SPEC)

    @pytest.fixture()
    def source(self):
        from runcomposer.plugins.manifest_source import DemoCorpusSource

        return DemoCorpusSource()

    def test_snapshot_matches_the_bundled_corpus(self, example, source):
        assert example["source"]["snapshot"] == source.snapshot()

    def test_materialized_list_is_the_filter_result(self, example, source):
        from runcomposer.sdk import Selection

        compiled = Selection.from_data({"tag_filter": example["selection"]["tag_filter"]}).compile(
            source.items()
        )
        assert [item.id for item in compiled] == example["selection"]["materialized"]["item_ids"]
        assert example["selection"]["materialized"]["count"] == len(compiled)

    def test_it_is_dispatchable_as_shipped(self, tmp_path, monkeypatch, capsys):
        """The example is not only valid: `dispatch` takes the file and a
        configured runner executes exactly its item list."""
        monkeypatch.chdir(tmp_path)  # built-in defaults: demo corpus, demo runner
        assert main(["dispatch", EXAMPLE_SPEC, "--runner", "demo", "--json"]) in (0, 1)
        detail = json.loads(capsys.readouterr().out)
        assert detail["state"] == "COMPLETE"
        assert detail["planned_count"] == sum(detail["verdict_summary"].values())


class TestQuickstartRoundTrip:
    """The export loop the README and CI's `quickstart` job run:
    spec --export → runcomposer-exec --simulate → ingest → COMPLETE."""

    def test_spec_exec_ingest_reaches_complete(self, tmp_path, capsys):
        import runcomposer_exec

        config = tmp_path / "rt-config.yaml"
        config.write_text("store:\n  sqlite: { path: rt.db }\n", encoding="utf-8")
        spec, bundle = tmp_path / "rt-spec.json", tmp_path / "rt-bundle"
        assert main([
            "spec", "Regression", "--title", "CI round trip", "--format", "json",
            "-o", str(spec), "--export", "--config", str(config),
        ]) == 0
        err = capsys.readouterr().err
        assert "export dispatch:" in err and "state AWAITING_RESULTS" in err

        assert runcomposer_exec.main([str(spec), "--out", str(bundle), "--simulate"]) == 0
        assert main(["ingest", str(bundle), "--config", str(config)]) == 0
        assert "run state: COMPLETE" in capsys.readouterr().out

        assert main(["runs", "--config", str(config)]) == 0
        listing = capsys.readouterr().out
        assert "COMPLETE" in listing and "CI round trip" in listing
        # A config file configures no runner by leaving `runners:` out: this
        # loop needs none, and the store sits next to the config file.
        assert (tmp_path / "rt.db").is_file()

    def test_the_exported_file_is_byte_for_byte_what_the_marker_hashes(self, tmp_path, capsys):
        import hashlib

        import runcomposer_exec

        config = tmp_path / "rt-config.yaml"
        config.write_text("store:\n  sqlite: { path: rt.db }\n", encoding="utf-8")
        spec, bundle = tmp_path / "spec.json", tmp_path / "bundle"
        assert main([
            "spec", "Payments", "--title", "Zahlungen prüfen", "--format", "json",
            "-o", str(spec), "--export", "--config", str(config),
        ]) == 0
        err = capsys.readouterr().err
        sha = hashlib.sha256(spec.read_bytes()).hexdigest()
        assert f"spec sha256: {sha}" in err
        assert "prüfen" in spec.read_text(encoding="utf-8")  # not \u-escaped
        # The token is shown once, next to the document — never inside it.
        assert "ingest token (shown once" in err
        assert "token" not in json.loads(spec.read_text(encoding="utf-8"))["results"]

        assert runcomposer_exec.main([str(spec), "--out", str(bundle), "--simulate"]) == 0
        marker = json.loads((bundle / "runcomposer_run.json").read_text(encoding="utf-8"))
        assert marker["spec_sha256"] == sha
        assert main(["ingest", str(bundle), "--config", str(config)]) == 0
        out = capsys.readouterr().out
        assert "run state: COMPLETE" in out and "warning" not in out
