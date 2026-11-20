"""The command line (docs/cli.md): every command through
``runcomposer.cli.main(argv)``, against configs in a temp directory.

The exit codes are part of the interface and are the same everywhere:
0 done / run PASS · 1 the answer is no · 2 usage or configuration error ·
3 the run ended without a result.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from runcomposer import __version__
from runcomposer.cli import main
from runcomposer.sdk import DispatchContext, RunnerBase, RunnerInfo, Service, load_config

from .conftest import compose, write_config
from .exec_loader import runcomposer_exec

SSO = ["Shop.Auth.SSO.T001", "Shop.Auth.SSO.T002", "Shop.Auth.SSO.T003"]


class RefusingRunner(RunnerBase):
    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="refusing")

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.refuse("the corpus changed since this run was composed")


class HandOffRunner(RunnerBase):
    """Hands the work off and returns: results arrive later, by ingestion."""

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="hand-off", capabilities=("external_delivery",))

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"], plan="handed off to the shop's CI")


class BrokenRunner(RunnerBase):
    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="broken")

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        raise RuntimeError("the worker pool collapsed")


_RUNNERS = {
    "demo": {},
    "closed": {"module": "tests.test_cli:RefusingRunner"},
    "ci": {"module": "tests.test_cli:HandOffRunner"},
    "broken": {"module": "tests.test_cli:BrokenRunner"},
}


class Cli:
    """``cli(*argv)`` runs one command against this test's config and
    returns ``(exit code, stdout, stderr)``."""

    def __init__(self, config: Path, capsys: Any):
        self.config = config
        self._capsys = capsys

    def __call__(
        self, *argv: str, config: bool = True, runner_crash: bool = False
    ) -> tuple[int, str, str]:
        args = [*argv, "--config", str(self.config)] if config else list(argv)
        try:
            code = main(args)
        except SystemExit as exit_:  # argparse, and the CLI's own usage errors
            code = exit_.code if isinstance(exit_.code, int) else 1
        captured = self._capsys.readouterr()
        # No command ends in a traceback of runcomposer's own. The one
        # traceback that IS printed on purpose is a runner's, when it crashed
        # mid-run: that is the operator's diagnostic.
        if not runner_crash:
            assert "Traceback" not in captured.err, captured.err
        return code, captured.out, captured.err

    def json(self, *argv: str) -> Any:
        code, out, _err = self(*argv, "--json")
        assert code in (0, 1), out
        return json.loads(out)

    def service(self) -> Service:
        return Service(load_config(str(self.config)))


@pytest.fixture()
def cli(tmp_path: Path, capsys) -> Cli:
    return Cli(write_config(tmp_path, runners=_RUNNERS), capsys)


def run_id_from(err: str) -> str:
    line = next(line for line in err.splitlines() if line.startswith("run: "))
    return line.split()[1]


def stored_runs(cli: Cli) -> list[Any]:
    with cli.service() as service:
        return list(service.list_runs(limit=100).runs)


def bundle(directory: Path, names: dict[str, str] | list[str], marker: dict | None = None) -> Path:
    if isinstance(names, list):
        names = dict.fromkeys(names, "PASS")
    directory.mkdir(parents=True)
    (directory / "results.json").write_text(
        json.dumps(
            {
                "format": "runcomposer-verdicts",
                "verdicts": [{"name": n, "status": s, "duration_ms": 15} for n, s in names.items()],
            }
        ),
        encoding="utf-8",
    )
    if marker:
        (directory / "runcomposer_run.json").write_text(json.dumps(marker), encoding="utf-8")
    return directory


# -- common ground -------------------------------------------------------------------


class TestCommonGround:
    def test_version(self, cli):
        code, out, _err = cli("--version", config=False)
        assert code == 0 and out.strip() == f"runcomposer {__version__}"

    def test_a_missing_config_file_is_exit_2_with_one_line(self, cli, tmp_path):
        code, out, err = cli("runs", "--config", str(tmp_path / "nowhere.yaml"), config=False)
        assert (code, out) == (2, "") and err.strip().startswith("error: config file not found")
        assert len(err.strip().splitlines()) == 1

    def test_an_unknown_top_level_section_names_connectors(self, tmp_path, capsys):
        path = tmp_path / "config.yaml"
        path.write_text("shop_alerts: {channel: checkout}\n", encoding="utf-8")
        code, _out, err = Cli(path, capsys)("runs")
        assert code == 2 and "unknown top-level section(s) ['shop_alerts']" in err
        assert "'connectors:'" in err

    def test_an_unknown_plugin_is_a_clean_one_liner(self, tmp_path, capsys):
        path = write_config(tmp_path, runners={"nonexistent-runner": {}})
        code, _out, err = Cli(path, capsys)("run", "Auth-SSO", "--runner", "nonexistent-runner")
        assert code == 2 and err.startswith("error: runners.nonexistent-runner")

    def test_serve_refuses_a_broken_deployment_before_it_binds(self, tmp_path, capsys):
        option_typo = Cli(write_config(tmp_path, runners={"demo": {"sead": 1}}), capsys)
        code, _out, err = option_typo("serve")
        assert code == 2 and "unknown option(s) ['sead']" in err and "accepts: ['delay_s', 'seed']" in err
        (tmp_path / "tree.yaml").write_text("nodes: []\n", encoding="utf-8")
        bad_tree = Cli(write_config(tmp_path, core={"taxonomy_file": "tree.yaml"}), capsys)
        code, _out, err = bad_tree("serve")
        assert code == 2 and "no top-level 'taxonomy' key" in err

    def test_usage_errors_are_exit_2(self, cli):
        assert cli("no-such-command", config=False)[0] == 2
        assert cli("run", "Auth-SSO")[0] == 2, "--runner is required"
        code, _out, err = cli("compile")
        assert code == 2 and "provide a filter" in err
        code, _out, err = cli("runs", "--label", "suite")
        assert code == 2 and "--label must be KEY=VALUE" in err
        code, _out, err = cli("compile", "{op: AND")
        assert code == 2 and "cannot parse filter" in err


# -- looking: catalog, compile, taxonomy-check, validate ---------------------------------


class TestLooking:
    def test_catalog_lists_items_tags_and_snapshot(self, cli):
        code, out, _err = cli("catalog", "--limit", "3")
        lines = out.splitlines()
        assert code == 0 and lines[0].startswith("# 60 items, 47 distinct tags — snapshot sha256:")
        assert lines[1].startswith("Shop.Payments.Cards.T001  [Payments, Payments-Cards, Smoke")
        assert lines[-1] == "# … 57 more (use --limit 0 for all)"

        code, out, _err = cli("catalog", "--tags")
        assert code == 0 and "    17  Payments" in out and "     3  Auth-SSO" in out
        assert len(out.splitlines()) == 48

    def test_catalog_json(self, cli):
        items = cli.json("catalog", "--limit", "2")
        assert (items["items"], items["tags"], items["duplicate_ids"]) == (60, 47, [])
        assert items["entries"] == [
            {"id": "Shop.Payments.Cards.T001", "tags": ["Payments", "Payments-Cards", "Smoke", "Sprint-12", "SHOP-1200"]},
            {"id": "Shop.Payments.Cards.T002", "tags": items["entries"][1]["tags"]},
        ]
        tags = cli.json("catalog", "--tags")
        assert {"tag": "Auth-SSO", "count": 3} in tags["entries"] and len(tags["entries"]) == 47

    def test_catalog_of_one_manifest_file(self, cli, tmp_path):
        manifest = tmp_path / "gifts.json"
        manifest.write_text(
            json.dumps({"items": [{"id": "Shop.Gift.T001", "tags": ["Checkout-Gift"]}]}), encoding="utf-8"
        )
        code, out, _err = cli("catalog", "--manifest", str(manifest), config=False)
        assert code == 0 and out.splitlines()[0].startswith("# 1 items, 1 distinct tags")
        assert "Shop.Gift.T001  [Checkout-Gift]" in out
        code, _out, err = cli("catalog", "--manifest", str(tmp_path / "missing.json"), config=False)
        assert code == 2 and "cannot read manifest" in err

    def test_compile_previews_a_selection(self, cli):
        code, out, _err = cli("compile", "{op: AND, items: [Auth, {not: prefix:Quarantine-}]}")
        assert code == 0 and out.splitlines()[0] == "# 11 item(s) matched"
        code, out, _err = cli("compile", "Auth", "--id", SSO[0], "--id", "Shop.Payments.Cards.T001")
        assert out.splitlines() == ["# 1 item(s) matched", f"{SSO[0]}  [Auth, Auth-SSO, Smoke, Sprint-12, SHOP-1257]"]
        code, out, _err = cli("compile", "regex:^NoSuchTagAnywhere$")
        assert code == 0 and out.splitlines() == ["warning: selection matched 0 items", "# 0 item(s) matched"]
        assert stored_runs(cli) == [], "compile stores nothing"

    def test_compile_errors_are_exit_1_one_liners(self, cli):
        code, _out, err = cli("compile", "{op: XOR, items: [Auth]}")
        assert code == 1 and err.startswith("error: tag_filter.op: unknown operator 'XOR'")
        code, _out, err = cli("compile", "--id", "Shop.Ghost.T404")
        assert code == 1 and "unknown item ids" in err
        code, _out, err = cli("compile", "regex:(")
        assert code == 1 and "invalid regular expression" in err

    def test_compile_from_history_says_which_run(self, cli):
        cli("run", "Auth-SSO", "--runner", "demo", "--param", "seed=b", "--label", "suite=nightly")
        (run,) = stored_runs(cli)
        code, out, _err = cli("compile", "--from-history", "failed@latest?suite=nightly")
        assert code == 0
        assert out.splitlines()[:2] == [f"# from history: run {run.id}", "# 2 item(s) matched"]
        body = cli.json("compile", "--from-history", "failed@latest?suite=nightly")
        assert (body["count"], body["total"]) == (2, 60)
        assert body["derived_from"][0]["resolved_run_id"] == run.id
        code, _out, err = cli("compile", "--from-history", "failed@latest?suite=weekly")
        assert code == 1 and "matched no completed run scoped to labels" in err

    def test_taxonomy_check_reports_drift_in_both_directions(self, tmp_path, capsys):
        (tmp_path / "tree.yaml").write_text(
            "taxonomy:\n"
            "  - {label: Payments, filter: Payments}\n"
            "  - {label: Returns, filter: Returns}\n",
            encoding="utf-8",
        )
        cli = Cli(write_config(tmp_path, core={"taxonomy_file": "tree.yaml"}), capsys)
        code, out, _err = cli("taxonomy-check", "--limit", "3")
        assert code == 1
        assert "# 2 taxonomy leaf pattern(s) over 47 distinct catalog tag(s)" in out
        assert "tags no leaf claims (invisible in the tree): 46" in out and "… 43 more" in out
        assert "leaves that match nothing (clickable, selects nothing): 1" in out
        assert "Returns  →  Returns" in out
        assert cli("taxonomy-check", "--warn-only")[0] == 0
        code, out, _err = cli("taxonomy-check", "--tree")
        assert code == 0 and out.splitlines()[0].startswith("Payments  Payments  (17 item(s)")

    def test_validate(self, cli, tmp_path):
        _code, out, _err = cli("spec", "Auth-SSO", "--dry-run")
        good = tmp_path / "good.yaml"
        good.write_text(out, encoding="utf-8")
        code, out, _err = cli("validate", str(good), "--for-dispatch", config=False)
        assert code == 0 and "valid runspec document (dispatch profile)" in out

        document = yaml.safe_load(good.read_text())
        document["selection"]["materialized"]["count"] = 99
        document["run"]["created_at"] = "last tuesday"
        bad = tmp_path / "bad.yaml"
        bad.write_text(yaml.safe_dump(document), encoding="utf-8")
        code, _out, err = cli("validate", str(bad), config=False)
        assert code == 1 and "count is 99 but item_ids has 3 entries" in err
        assert "$.run.created_at" in err, "date-time is enforced, whatever packages are installed"
        assert "INVALID (2 error(s))" in err
        assert cli("validate", str(tmp_path / "missing.yaml"), config=False)[0] == 2


# -- spec ----------------------------------------------------------------------------------


class TestSpec:
    def test_spec_composes_stores_and_prints_the_document(self, cli):
        code, out, err = cli("spec", "Auth-SSO", "--title", "Anmeldung prüfen", "--label", "suite=adhoc")
        assert code == 0
        document = yaml.safe_load(out)
        assert document["run"]["title"] == "Anmeldung prüfen" and "prüfen" in out
        assert document["run"]["labels"] == {"suite": "adhoc"}
        assert document["selection"]["materialized"]["item_ids"] == SSO
        assert "token" not in document["results"]
        (run,) = stored_runs(cli)
        assert run.id == document["run"]["id"] == run_id_from(err)
        assert (run.state, run.origin) == ("COMPOSED", "cli")
        assert "ingest token" not in err, "nothing was handed out, so nothing was minted"

    def test_json_format(self, cli):
        code, out, _err = cli("spec", "Auth-SSO", "--format", "json")
        assert code == 0 and json.loads(out)["runspec"] == "1.0"

    def test_dry_run_stores_nothing(self, cli):
        """`spec` always persisted a run; previewing a document should not."""
        code, out, err = cli("spec", "Auth-SSO", "--dry-run", "--runner", "demo", "--param", "seed=a")
        assert code == 0 and err.splitlines() == ["dry run: nothing was stored"]
        document = yaml.safe_load(out)
        assert document["runner"] == {"demo": {"seed": "a"}}
        assert stored_runs(cli) == []
        code, _out, err = cli("spec", "Auth-SSO", "--dry-run", "--export")
        assert code == 1 and "there is no run to --export" in err

    def test_composing_for_a_runner_writes_its_parameters_and_its_format(self, cli):
        code, out, _err = cli("spec", "Auth-SSO", "--runner", "demo", "--param", "seed=nightly-3")
        document = yaml.safe_load(out)
        assert code == 0 and document["runner"] == {"demo": {"seed": "nightly-3"}}
        assert document["results"]["expect"] == [{"format": "runcomposer-verdicts"}]
        code, out, _err = cli("spec", "Auth-SSO", "--expect-format", "junit-xml")
        assert yaml.safe_load(out)["results"]["expect"] == [{"format": "junit-xml"}]

    @pytest.mark.parametrize(
        ("argv", "message"),
        [
            (["{op: XOR, items: [Auth]}"], "unknown operator 'XOR'"),
            (["--id", "Shop.Ghost.T404"], "unknown item ids"),
            (["Auth-SSO", "--runner", "ghost"], "no runner is configured under the id 'ghost'"),
            (["Auth-SSO", "--runner", "demo", "--param", "sed=a"], "unknown parameter 'sed'"),
            (["Auth-SSO", "--runner", "demo", "--param", "seed=7"], "parameter 'seed' must be a string"),
            (["Auth-SSO", "--param", "seed=a"], "--param needs --runner"),
            (["--from-history", "failed@latest"], "matched no completed run"),
        ],
    )
    def test_errors_are_one_line_and_leave_no_run(self, cli, argv, message):
        """Regression: these used to escape as tracebacks."""
        code, out, err = cli("spec", *argv)
        assert code == 1 and out == ""
        assert err.strip().startswith("error: ") and message in err
        assert len(err.strip().splitlines()) == 1
        assert stored_runs(cli) == []

    def test_export_writes_exactly_the_bytes_whose_hash_is_recorded(self, cli, tmp_path):
        spec_file, token_file = tmp_path / "spec.json", tmp_path / "token"
        code, out, err = cli(
            "spec", "Auth-SSO", "--title", "Anmeldung prüfen", "--format", "json",
            "-o", str(spec_file), "--export", "--token-file", str(token_file),
        )
        assert code == 0 and out == ""
        printed = next(line for line in err.splitlines() if line.startswith("spec sha256: ")).split(": ")[1]
        assert printed == hashlib.sha256(spec_file.read_bytes()).hexdigest()
        assert "(1 shard(s) declared, state AWAITING_RESULTS)" in err
        token = token_file.read_text().strip()
        assert token.startswith("rct_") and token not in err
        if os.name != "nt":
            assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        assert token.encode() not in spec_file.read_bytes()

        (run,) = stored_runs(cli)
        assert run.state == "AWAITING_RESULTS" and run.latest_dispatch.spec_sha256 == printed
        with cli.service() as service:
            assert service.dispatch_spec(run.id, run.latest_dispatch.dispatch_id)[0] == spec_file.read_bytes()
            assert service.ingestion.authorize_push(run.id, token) == run.latest_dispatch.dispatch_id

    def test_export_prints_the_token_once_when_no_file_is_named(self, cli):
        code, out, err = cli("spec", "Auth-SSO", "--export")
        assert code == 0
        line = next(line for line in err.splitlines() if line.startswith("ingest token (shown once"))
        assert line.rsplit(" ", 1)[1].startswith("rct_")
        assert "rct_" not in out, "the document never carries the token"
        printed = next(line for line in err.splitlines() if line.startswith("spec sha256: ")).split(": ")[1]
        assert printed == hashlib.sha256(out.encode("utf-8")).hexdigest()

    def test_the_export_loop_end_to_end(self, cli, tmp_path):
        """spec --export → runcomposer-exec → ingest → COMPLETE."""
        spec_file = tmp_path / "spec.json"
        cli("spec", "Auth-SSO", "--format", "json", "-o", str(spec_file), "--export")
        out_dir = tmp_path / "results"
        assert runcomposer_exec.main([str(spec_file), "--out", str(out_dir), "--simulate", "--seed", "a"]) == 0
        code, out, _err = cli("ingest", str(out_dir))
        (run,) = stored_runs(cli)
        assert code == 0
        assert out.splitlines() == [f"{run.id} shard 1: delivery recorded (3 verdict(s))", "run state: COMPLETE (PASS)"]
        code, out, _err = cli("ingest", str(out_dir))
        assert code == 0 and "already ingested — no-op" in out

    def test_export_spec_hands_out_a_stored_run(self, cli, tmp_path):
        _code, _out, err = cli("spec", "Auth-SSO")
        run_id = run_id_from(err)
        target = tmp_path / "handout.yaml"
        code, out, err = cli("export-spec", run_id, "--format", "yaml", "-o", str(target))
        assert code == 0 and out == "" and "export dispatch: " in err and "ingest token (shown once" in err
        assert yaml.safe_load(target.read_text(encoding="utf-8"))["run"]["id"] == run_id
        printed = next(line for line in err.splitlines() if line.startswith("spec sha256: ")).split(": ")[1]
        assert printed == hashlib.sha256(target.read_bytes()).hexdigest()
        code, out, _err = cli("export-spec", run_id)
        assert code == 0 and json.loads(out)["run"]["id"] == run_id
        assert len(stored_runs(cli)) == 1, "exporting never composes another run"
        assert cli("export-spec", "01NOSUCHRUN000000000000000")[0] == 1


# -- run, dispatch, rerun --------------------------------------------------------------------


class TestRun:
    def test_run_composes_dispatches_waits_and_summarises(self, cli):
        code, out, err = cli("run", "Auth-SSO", "--runner", "demo", "--param", "seed=a", "--title", "Smoke")
        assert code == 0, err
        lines = out.splitlines()
        assert lines[0] == "demo runner: simulating 3 item(s) with seed 'a'"
        assert " via 'demo' (1 shard(s))" in lines[1]
        assert lines[2] == "state: COMPLETE (PASS) — 3 PASS"
        assert lines[3] == "  shard 1: PASS — 3 PASS"
        (run,) = stored_runs(cli)
        assert (run.title, run.origin, run.completion) == ("Smoke", "cli", "PASS")

    def test_a_run_that_is_not_pass_exits_1(self, cli):
        """Regression: COMPLETE (FAIL) used to exit 0."""
        code, out, _err = cli("run", "Auth-SSO", "--runner", "demo", "--param", "seed=b")
        assert code == 1 and "state: COMPLETE (FAIL) — 1 PASS, 2 FAIL" in out

    def test_run_json_is_the_run_detail(self, cli):
        detail = cli.json("run", "Auth-SSO", "--runner", "demo", "--param", "seed=a")
        assert (detail["state"], detail["completion"], detail["verdict_summary"]) == ("COMPLETE", "PASS", {"PASS": 3})
        assert len(detail["verdicts"]) == 3 and detail["shards"][0]["shard"] == "1"

    def test_a_refused_run_exits_3_and_leaves_no_run(self, cli):
        code, out, err = cli("run", "Auth-SSO", "--runner", "closed")
        assert code == 3 and out == ""
        assert err.strip() == "refused: the corpus changed since this run was composed"
        assert stored_runs(cli) == []

    def test_a_run_that_cannot_start_exits_1_and_leaves_no_run(self, cli):
        for argv, message in [
            (["Auth-SSO", "--runner", "ghost"], "no runner is configured"),
            (["Auth-SSO", "--runner", "demo", "--param", "sed=a"], "unknown parameter 'sed'"),
            (["regex:^NoSuchTagAnywhere$", "--runner", "demo"], "nothing to execute"),
        ]:
            code, _out, err = cli("run", *argv)
            assert code == 1 and message in err
        code, _out, err = cli("run", "Auth-SSO", "--runner", "demo", "--param", "seed")
        assert code == 2 and "--param must be NAME=VALUE" in err
        assert stored_runs(cli) == []

    def test_a_failed_run_exits_3(self, cli):
        code, out, err = cli("run", "Auth-SSO", "--runner", "broken", runner_crash=True)
        assert code == 3
        assert "state: FAILED — 3 MISSING" in out and "error: RuntimeError: the worker pool collapsed" in out
        # The runner's own traceback is logged for whoever has to fix it.
        assert "runner 'broken' failed after the hand-off" in err and "in dispatch" in err

    def test_param_values_are_read_as_yaml(self, tmp_path, capsys):
        seen: dict[str, Any] = {}

        class Recording(RunnerBase):
            def describe(self) -> RunnerInfo:
                from runcomposer.sdk import Parameter

                return RunnerInfo(
                    id="recording",
                    parameters=(
                        Parameter("partitions", kind="multi_choice", choices=("env1", "env2")),
                        Parameter("allow_drift", kind="boolean"),
                        Parameter("retries", kind="integer"),
                        Parameter("note", kind="string"),
                    ),
                )

            def dispatch(self, ctx: DispatchContext) -> None:
                seen.update(ctx.parameters)
                ctx.refuse("recorded")

        globals()["_Recording"] = Recording
        cli = Cli(write_config(tmp_path, runners={"rec": {"module": "tests.test_cli:_Recording"}}), capsys)
        code, _out, _err = cli(
            "run", "Auth-SSO", "--runner", "rec",
            "--param", "partitions=[env1, env2]", "--param", "allow_drift=true",
            "--param", "retries=2", "--param", "note=a=b",
        )
        assert code == 3
        assert seen == {"partitions": ["env1", "env2"], "allow_drift": True, "retries": 2, "note": "a=b"}
        code, _out, err = cli("run", "Auth-SSO", "--runner", "rec", "--param", "partitions=[env9]")
        assert code == 1 and "parameter 'partitions' must be a list drawn from ['env1', 'env2']" in err

    def test_a_handed_off_run_waits_for_results_or_returns_with_no_wait(self, cli):
        code, out, err = cli("run", "Auth-SSO", "--runner", "ci", "--no-wait")
        assert code == 0 and "state: AWAITING_RESULTS — no verdicts" in out
        assert "handed off to the shop's CI" in out and "shard 1: outstanding" in out
        code, out, err = cli("run", "Auth-SSO", "--runner", "ci", "--timeout", "0.2")
        assert code == 3
        assert "handed off, waiting for results" in err and "timed out waiting for results" in err
        assert [run.state for run in stored_runs(cli)] == ["AWAITING_RESULTS"] * 2


class TestDispatch:
    def _spec(self, cli: Cli, tmp_path: Path, *extra: str) -> tuple[Path, str]:
        spec_file = tmp_path / "spec.yaml"
        _code, _out, err = cli("spec", "Auth-SSO", "-o", str(spec_file), *extra)
        return spec_file, run_id_from(err)

    def test_dispatching_a_spec_file_exits_by_the_result(self, cli, tmp_path):
        spec_file, run_id = self._spec(cli, tmp_path, "--runner", "demo", "--param", "seed=b")
        code, out, _err = cli("dispatch", str(spec_file), "--runner", "demo")
        assert code == 1, "COMPLETE (FAIL) is not success"
        assert f"run {run_id}: dispatch " in out and "state: COMPLETE (FAIL) — 1 PASS, 2 FAIL" in out
        # --param overrides what the document carries; PASS is exit 0.
        code, out, _err = cli("dispatch", str(spec_file), "--runner", "demo", "--param", "seed=a")
        assert code == 0 and "state: COMPLETE (PASS) — 3 PASS" in out
        (run,) = stored_runs(cli)
        assert [d.completion for d in run.dispatches] == ["FAIL", "PASS"]

    def test_a_spec_composed_elsewhere_is_imported(self, cli, tmp_path):
        _code, out, _err = cli("spec", "Auth-SSO", "--dry-run", "--format", "json")
        spec_file = tmp_path / "elsewhere.json"
        spec_file.write_text(out, encoding="utf-8")
        assert stored_runs(cli) == []
        code, _out, _err = cli("dispatch", str(spec_file), "--runner", "demo", "--param", "seed=a")
        assert code == 0
        (run,) = stored_runs(cli)
        assert run.id == json.loads(out)["run"]["id"] and run.origin == "cli"

    def test_an_edited_copy_of_a_stored_run_is_refused(self, cli, tmp_path):
        """Regression: the edit used to be silently ignored — the stored run
        was dispatched and the file's changes were never looked at."""
        spec_file, run_id = self._spec(cli, tmp_path)
        document = yaml.safe_load(spec_file.read_text())
        document["selection"]["materialized"] = {**document["selection"]["materialized"], "item_ids": SSO[:1], "count": 1}
        spec_file.write_text(yaml.safe_dump(document), encoding="utf-8")
        code, out, err = cli("dispatch", str(spec_file), "--runner", "demo")
        assert code == 1 and out == ""
        assert f"run {run_id} is already stored and the given document differs" in err
        (run,) = stored_runs(cli)
        assert run.state == "COMPOSED" and run.dispatches == ()

    def test_dispatching_a_stored_run_by_id(self, cli):
        _code, _out, err = cli("spec", "Auth-SSO")
        run_id = run_id_from(err)
        code, out, _err = cli("dispatch", "--run", run_id, "--runner", "demo", "--param", "seed=a")
        assert code == 0 and f"run {run_id}: dispatch" in out
        assert cli("dispatch", "--run", "01NOSUCHRUN000000000000000", "--runner", "demo")[0] == 1

    def test_refused_failed_and_misused(self, cli, tmp_path):
        spec_file, run_id = self._spec(cli, tmp_path)
        code, _out, err = cli("dispatch", str(spec_file), "--runner", "closed")
        assert code == 3 and "refused: the corpus changed" in err
        assert stored_runs(cli)[0].state == "COMPOSED", "a stored run survives a refusal"
        assert cli("dispatch", str(spec_file), "--runner", "ghost")[0] == 1
        code, _out, err = cli("dispatch", "--runner", "demo")
        assert code == 2 and "give a spec file or --run RUN_ID" in err
        assert cli("dispatch", str(spec_file), "--run", run_id, "--runner", "demo")[0] == 2
        code, _out, err = cli("dispatch", str(tmp_path / "missing.yaml"), "--runner", "demo")
        assert code == 2 and err.startswith("error: ")
        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        code, _out, err = cli("dispatch", str(broken), "--runner", "demo")
        assert code == 2 and "not valid JSON" in err
        not_dispatchable = tmp_path / "preview.yaml"
        not_dispatchable.write_text("runspec: '1.0'\nrun: {id: x, title: t, created_at: '2026-07-06T09:14:03Z'}\n", encoding="utf-8")
        code, _out, err = cli("dispatch", str(not_dispatchable), "--runner", "demo")
        assert code == 1 and "not dispatchable" in err


class TestRerunCancelWatchShow:
    def _finished(self, cli: Cli, seed: str = "b", *extra: str) -> str:
        cli("run", "Auth-SSO", "--runner", "demo", "--param", f"seed={seed}", *extra)
        return stored_runs(cli)[0].id

    def test_rerun_all_is_a_new_dispatch_of_the_same_run(self, cli):
        run_id = self._finished(cli)
        code, out, _err = cli("rerun", run_id)
        assert code == 1 and f"run {run_id}: dispatch" in out, "same seed, same failures"
        code, _out, _err = cli("rerun", run_id, "--param", "seed=a")
        assert code == 0
        assert [d.completion for d in stored_runs(cli)[0].dispatches] == ["FAIL", "FAIL", "PASS"]

    def test_rerun_failed_composes_a_run_from_what_did_not_pass(self, cli):
        run_id = self._finished(cli, "b", "--label", "suite=nightly")
        code, out, err = cli("rerun", run_id, "--scope", "failed", "--param", "seed=a")
        assert code == 0
        new_id = next(line for line in err.splitlines() if line.startswith(f"rerun of {run_id}: composed run ")).split()[-1]
        assert new_id != run_id and f"run {new_id}: dispatch" in out and "2 PASS" in out
        runs = {run.id: run for run in stored_runs(cli)}
        assert runs[new_id].planned_count == 2 and dict(runs[new_id].labels) == {"suite": "nightly"}
        assert len(runs[run_id].dispatches) == 1

    def test_rerun_failed_of_a_green_run_composes_nothing(self, cli):
        run_id = self._finished(cli, "a")
        code, _out, err = cli("rerun", run_id, "--scope", "failed")
        assert code == 1 and "nothing to rerun" in err
        assert len(stored_runs(cli)) == 1
        assert cli("rerun", "01NOSUCHRUN000000000000000")[0] == 1

    def test_cancel(self, cli):
        cli("run", "Auth-SSO", "--runner", "ci", "--no-wait")
        run_id = stored_runs(cli)[0].id
        code, out, _err = cli("cancel", run_id)
        assert code == 0 and out.strip().endswith("CANCELLED") and run_id in out
        assert stored_runs(cli)[0].state == "CANCELLED"
        code, out, _err = cli("watch", run_id)
        assert code == 3 and "run.cancelled" in out, "a cancelled run ended without a result"
        code, _out, err = cli("cancel", run_id)
        assert code == 1 and "nothing in flight to cancel (state CANCELLED)" in err
        assert cli("cancel", "01NOSUCHRUN000000000000000")[0] == 1

    def test_cancel_json(self, cli):
        cli("spec", "Auth-SSO", "--export")
        run_id = stored_runs(cli)[0].id
        body = cli.json("cancel", run_id)
        assert (body["state"], body["mode"], body["cancel_requested"]) == ("CANCELLED", "export", True)

    def test_watch_prints_the_events_of_a_finished_run(self, cli):
        run_id = self._finished(cli, "a")
        code, out, _err = cli("watch", run_id, "--no-follow")
        lines = out.splitlines()
        assert code == 0 and len(lines) == 5
        assert [line.split()[2] for line in lines] == [
            "run.composed", "run.dispatched", "run.running", "delivery.recorded", "run.completed",
        ]
        assert lines[-1].endswith("state=COMPLETE  completion=PASS  3 PASS")
        code, out, _err = cli("watch", run_id, "--no-follow", "--after", "3")
        assert code == 0 and len(out.splitlines()) == 2

    def test_watch_follows_to_the_end_and_exits_by_the_result(self, cli):
        passed = self._finished(cli, "a")
        code, out, _err = cli("watch", passed, "--json")
        events = [json.loads(line) for line in out.splitlines()]
        assert code == 0 and [event["kind"] for event in events][-1] == "run.completed"
        assert all(set(event) == {"seq", "run_id", "at", "kind", "dispatch_id", "data"} for event in events)
        cli("run", "Auth-SSO", "--runner", "demo", "--param", "seed=b")
        failed = stored_runs(cli)[0].id
        assert cli("watch", failed)[0] == 1

    def test_watch_times_out_on_a_run_that_is_still_waiting(self, cli):
        cli("run", "Auth-SSO", "--runner", "ci", "--no-wait")
        run_id = stored_runs(cli)[0].id
        code, out, err = cli("watch", run_id, "--timeout", "0.2")
        assert code == 3 and "run.awaiting_results" in out and "timed out" in err
        assert cli("watch", "01NOSUCHRUN000000000000000")[0] == 1

    def test_show(self, cli, tmp_path):
        run_id = self._finished(cli, "b", "--title", "Nightly", "--label", "suite=nightly")
        with cli.service() as service:
            run = service.require_run(run_id)
            log = tmp_path / "artifacts" / run_id / run.latest_dispatch.dispatch_id / "log.html"
            log.parent.mkdir(parents=True)
            log.write_text("<html/>", encoding="utf-8")
            service.store.add_artifact_ref(
                run_id, run.latest_dispatch.dispatch_id, name="log.html", media_type="text/html", url_or_path=str(log)
            )
        code, out, _err = cli("show", run_id)
        assert code == 0
        lines = out.splitlines()
        assert lines[0] == f"run {run_id}  Nightly"
        assert lines[1] == "state: COMPLETE (FAIL) — 1 PASS, 2 FAIL"
        assert "planned: 3 item(s)" in lines[2] and "{'suite': 'nightly'}" in lines[2]
        assert lines[3].startswith(f"dispatch {run.latest_dispatch.dispatch_id}  demo  COMPLETE (FAIL)")
        assert "  shard 1: FAIL — 1 PASS, 2 FAIL" in lines
        assert f"  artifact log.html: /artifacts/{run_id}/{run.latest_dispatch.dispatch_id}/log.html" in lines
        detail = cli.json("show", run_id)
        assert detail["id"] == run_id and detail["selection"]["tag_filter"] == "Auth-SSO"
        assert cli("show", "01NOSUCHRUN000000000000000")[0] == 1


# -- runs, finalize, delete, export -------------------------------------------------------------


class TestRunsAndHousekeeping:
    @pytest.fixture()
    def history(self, cli) -> dict[str, str]:
        ids = {}
        for title, selection, seed, label in [
            ("Nightly regression", "Auth-SSO", "b", "suite=nightly"),
            ("Zahlungen prüfen", "Payments-Refunds", "demo", "suite=nightly"),
            ("A one-off catalog check", "Catalog-Search", "a", "suite=adhoc"),
        ]:
            cli("run", selection, "--runner", "demo", "--param", f"seed={seed}", "--title", title, "--label", label)
            ids[title] = stored_runs(cli)[0].id
        return ids

    def test_the_listing(self, cli, history):
        code, out, _err = cli("runs")
        lines = out.splitlines()
        assert code == 0 and lines[0].split() == ["RUN", "ID", "STATE", "RESULT", "MODE", "CREATED", "TITLE"]
        assert len(lines) == 4
        assert {line.split()[0] for line in lines[1:]} == set(history.values())
        nightly = next(line for line in lines if history["Nightly regression"] in line)
        assert nightly.split()[1:4] == ["COMPLETE", "FAIL", "demo"] and nightly.endswith("Nightly regression")

    def test_filters_and_paging(self, cli, history):
        def ids(*argv: str) -> set[str]:
            return {line.split()[0] for line in cli("runs", *argv)[1].splitlines()[1:] if not line.startswith("#")}

        assert ids("--label", "suite=nightly") == {history["Nightly regression"], history["Zahlungen prüfen"]}
        assert ids("-q", "PRÜFEN") == {history["Zahlungen prüfen"]}
        assert ids("--state", "COMPLETE", "--label", "suite=adhoc") == {history["A one-off catalog check"]}
        assert ids("--since", "2999-01-01T00:00:00Z") == set()
        code, out, _err = cli("runs", "--state", "STALE")
        assert code == 0 and out.strip() == "no runs stored in state STALE"
        code, out, _err = cli("runs", "--limit", "2")
        assert out.splitlines()[-1] == "# 2 of 3 run(s) — page with --limit / --offset"
        assert len(ids("--limit", "2")) == 2 and len(ids("--limit", "2", "--offset", "2")) == 1
        code, _out, err = cli("runs", "--state", "DONE")
        assert code == 1 and "unknown run state 'DONE'" in err
        code, _out, err = cli("runs", "--since", "yesterday")
        assert code == 1 and "not an ISO-8601 time" in err

    def test_runs_json_rows(self, cli, history):
        body = cli.json("runs", "--label", "suite=nightly")
        assert (body["total"], body["limit"], body["offset"]) == (2, 20, 0)
        row = next(r for r in body["runs"] if r["id"] == history["Nightly regression"])
        assert (row["mode"], row["completion"], row["verdict_summary"]) == ("demo", "FAIL", {"FAIL": 2, "PASS": 1})
        assert row["planned_count"] == 3 and row["labels"] == {"suite": "nightly"}

    def test_failed_in_needs_a_scope_to_mean_the_right_run(self, cli, history):
        """Unscoped, "latest" ranges over every completed run —
        here, a one-off run of something else."""
        code, out, _err = cli("runs", "--failed-in", "latest")
        adhoc = history["A one-off catalog check"]
        assert code == 0 and out.splitlines()[0].startswith(f"# 1 item(s) FAILED in run {adhoc}")
        nightly_latest = history["Zahlungen prüfen"]
        code, out, _err = cli("runs", "--failed-in", "latest", "--label", "suite=nightly")
        assert out.splitlines()[0] == f"# 0 item(s) FAILED in run {nightly_latest} (scope: {{'suite': 'nightly'}})"
        code, out, _err = cli("runs", "--failed-in", f"run:{history['Nightly regression']}")
        lines = out.splitlines()
        assert lines[0] == f"# 2 item(s) FAILED in run {history['Nightly regression']}"
        assert len(lines) == 3 and set(lines[1:]) < set(SSO)
        inline = cli.json("runs", "--failed-in", "latest?suite=nightly")
        assert inline["derived_from"]["resolved_run_id"] == nightly_latest and inline["item_ids"] == []
        code, _out, err = cli("runs", "--failed-in", "latest?suite=nightly", "--label", "suite=adhoc")
        assert code == 1 and "refusing to guess" in err
        code, _out, err = cli("runs", "--failed-in", f"run:{nightly_latest}", "--label", "suite=nightly")
        assert code == 1 and "already names one run" in err

    def test_finalize_exits_by_the_result(self, cli):
        cli("spec", "Auth-SSO", "--export")
        run_id = stored_runs(cli)[0].id
        code, out, _err = cli("finalize", run_id)
        assert code == 1 and out.strip() == f"run {run_id}: COMPLETE (INCOMPLETE) — 3 MISSING"
        assert cli.json("finalize", run_id)["completion"] == "INCOMPLETE"
        cli("spec", "Auth-SSO")
        composed = stored_runs(cli)[0].id
        code, _out, err = cli("finalize", composed)
        assert code == 1 and "there is nothing to finalize" in " ".join(err.split())
        assert cli("finalize", "01NOSUCHRUN000000000000000")[0] == 1

    def test_delete(self, cli, history, tmp_path):
        run_id = history["Nightly regression"]
        (tmp_path / "artifacts" / run_id).mkdir(parents=True)
        code, out, _err = cli("delete", run_id)
        assert code == 0 and out.strip() == f"run {run_id} deleted"
        assert run_id not in {run.id for run in stored_runs(cli)}
        assert not (tmp_path / "artifacts" / run_id).exists()
        code, _out, err = cli("delete", run_id)
        assert code == 1 and "unknown run id" in err

    def test_gc(self, tmp_path, capsys):
        cli = Cli(
            write_config(tmp_path, core={"retention": {"max_age_days": None, "max_runs": 1}}), capsys
        )
        for seed in ("a", "b"):
            cli("run", "Auth-SSO", "--runner", "demo", "--param", f"seed={seed}")
        code, out, _err = cli("gc")
        assert code == 0, "max_age_days: null means no age limit — it used to be a TypeError"
        assert "store: pruned 1 run(s) past retention" in out and "quarantine: removed 0" in out
        assert len(stored_runs(cli)) == 1
        report = cli.json("gc")
        assert report["runs_removed"] == [] and report["quarantine_removed"] == []

    def test_export_ctrf(self, cli, history, tmp_path):
        run_id = history["Nightly regression"]
        code, out, _err = cli("export", run_id, "--format", "ctrf")
        assert code == 0
        results = json.loads(out)["results"]
        assert results["tool"] == {"name": "runcomposer"}
        assert results["summary"]["tests"] == 3
        assert (results["summary"]["passed"], results["summary"]["failed"]) == (1, 2)
        assert results["summary"]["stop"] >= results["summary"]["start"] > 0
        assert {test["status"] for test in results["tests"]} == {"passed", "failed"}
        assert results["tests"][0]["extra"] == {"shard": "1", "attempt": 1}
        assert results["extra"] == {"run_id": run_id, "state": "COMPLETE", "completion": "FAIL"}
        target = tmp_path / "report.json"
        code, out, err = cli("export", run_id, "--format", "ctrf", "-o", str(target))
        assert code == 0 and out == "" and "CTRF report written" in err
        assert json.loads(target.read_text())["results"]["summary"]["tests"] == 3
        code, _out, err = cli("export", run_id, "--format", "xunit")
        assert code == 2 and "unknown export format 'xunit'" in err
        assert cli("export", "01NOSUCHRUN000000000000000", "--format", "ctrf")[0] == 1

    def test_export_ctrf_reads_times_with_an_offset_and_reports_missing(self, cli, tmp_path):
        """A document composed elsewhere may spell its times with an offset;
        a fixed-format strptime raised ValueError on these."""
        _code, out, _err = cli("spec", "Auth-SSO", "--dry-run", "--format", "json")
        document = json.loads(out)
        document["run"]["created_at"] = "2026-07-06T11:14:03+02:00"
        spec_file = tmp_path / "offset.json"
        spec_file.write_text(json.dumps(document), encoding="utf-8")
        code, _out, _err = cli("dispatch", str(spec_file), "--runner", "ci", "--no-wait")
        assert code == 0
        run_id = document["run"]["id"]
        cli("ingest", str(bundle(tmp_path / "partial", {SSO[0]: "ERROR"})), "--run", run_id)
        code, out, _err = cli("export", run_id, "--format", "ctrf")
        assert code == 0
        results = json.loads(out)["results"]
        assert results["summary"]["start"] == 1783329243000  # 2026-07-06T09:14:03Z
        assert (results["summary"]["tests"], results["summary"]["other"]) == (3, 3)
        assert sorted(test.get("rawStatus") for test in results["tests"]) == ["ERROR", "MISSING", "MISSING"]


# -- ingest and quarantine ------------------------------------------------------------------------


class TestIngestAndQuarantine:
    @pytest.fixture()
    def awaiting(self, cli) -> str:
        cli("spec", "Auth-SSO", "--export")
        return stored_runs(cli)[0].id

    def test_ingest_by_run_id_with_options(self, cli, awaiting, tmp_path):
        code, out, _err = cli(
            "ingest", str(bundle(tmp_path / "b", {SSO[0]: "FAIL", SSO[1]: "PASS", SSO[2]: "PASS"})),
            "--run", awaiting, "--shard", "1", "--format", "runcomposer-verdicts",
        )
        assert code == 0
        assert out.splitlines() == [f"{awaiting} shard 1: delivery recorded (3 verdict(s))", "run state: COMPLETE (FAIL)"]
        report = cli.json("ingest", str(bundle(tmp_path / "green", SSO)), "--run", awaiting)
        assert (report["outcome"], report["completion"], report["verdict_count"]) == ("replaced", "PASS", 3)
        assert report["dispatch_id"] == stored_runs(cli)[0].latest_dispatch.dispatch_id

    def test_warnings_are_printed(self, cli, awaiting, tmp_path):
        code, out, _err = cli("ingest", str(bundle(tmp_path / "b", [SSO[0], "Shop.Ghost.T404"])), "--run", awaiting)
        assert code == 0
        assert out.splitlines()[0] == "warning: unknown native name 'Shop.Ghost.T404' — verdict skipped"
        assert out.splitlines()[-1] == "run state: COMPLETE (INCOMPLETE)"

    def test_refused_bundles_are_exit_1(self, cli, awaiting, tmp_path):
        stray = bundle(tmp_path / "stray", SSO)
        code, _out, err = cli("ingest", str(stray))
        assert code == 1 and "unsolicited bundles are not auto-attached" in err and "--allow-unsolicited" in err
        code, _out, err = cli("ingest", str(stray), "--run", "01NOSUCHRUN000000000000000")
        assert code == 1 and "unknown run id" in err
        code, _out, err = cli("ingest", str(stray), "--run", awaiting, "--format", "pkg.mod:Parser")
        assert code == 1 and "is not a result format id" in err
        code, _out, err = cli("ingest", str(stray), "--run", awaiting, "--dispatch", "01NOPE")
        assert code == 1 and "unknown dispatch id" in err
        code, _out, err = cli("ingest", str(tmp_path / "missing"), "--run", awaiting)
        assert code == 1 and "bundle not found" in err
        assert stored_runs(cli)[0].state == "AWAITING_RESULTS"

    def test_allow_unsolicited_promotes_a_stray_bundle(self, cli, tmp_path):
        stray = bundle(tmp_path / "stray", SSO, marker={"run_id": "01SOMEBODYELSESRUN00000000"})
        assert cli("ingest", str(stray))[0] == 1
        code, out, _err = cli("ingest", str(stray), "--allow-unsolicited")
        assert code == 0
        (run,) = stored_runs(cli)
        assert out.splitlines()[0] == f"unsolicited bundle promoted to run {run.id} (origin: ingested)"
        assert (run.origin, run.state, run.completion) == ("ingested", "COMPLETE", "PASS")
        # --allow-unsolicited never overrides a real refusal.
        cli("spec", "Auth-SSO", "--export")
        awaiting = stored_runs(cli)[0].id
        conflicting = bundle(tmp_path / "c", SSO, marker={"run_id": awaiting, "spec_sha256": "0" * 64})
        code, _out, err = cli("ingest", str(conflicting), "--allow-unsolicited")
        assert code == 1 and "does not match the spec bytes" in err

    def test_quarantine_commands(self, cli, tmp_path):
        """Quarantine and finalize used to be reachable over HTTP only."""
        code, out, _err = cli("quarantine", "list")
        assert code == 0 and out.strip() == "quarantine is empty"
        with cli.service() as service:
            claimed = service.ingest_or_quarantine(
                bundle(tmp_path / "claimed", SSO, marker={"run_id": "01SOMEBODYELSESRUN00000000"}), transport="file-drop"
            ).entry_id
            bare = service.ingest_or_quarantine(bundle(tmp_path / "bare", SSO[:2]), transport="file-drop").entry_id
            extra = service.ingest_or_quarantine(bundle(tmp_path / "extra", SSO[:1]), transport="file-drop").entry_id

        code, out, _err = cli("quarantine", "list")
        lines = out.splitlines()
        assert code == 0 and lines[0].split()[0] == "ENTRY" and len(lines) == 4
        row = next(line for line in lines if line.startswith(claimed))
        assert row.split()[2:] == ["unknown-run", "file-drop", "01SOMEBODYELSESRUN00000000"]
        listed = cli.json("quarantine", "list")
        assert {entry["entry_id"] for entry in listed["entries"]} == {claimed, bare, extra}

        cli("spec", "Auth-SSO")
        run_id = stored_runs(cli)[0].id
        code, out, _err = cli("quarantine", "attach", claimed, run_id)
        assert code == 0 and out.splitlines()[-1] == "run state: COMPLETE (PASS)"
        with cli.service() as service:
            assert "delivery.attached" in [event.kind for event in service.store.events(run_id)]

        code, _out, err = cli("quarantine", "promote", bare)
        assert code == 1 and "does not say which result format" in err
        promoted = cli.json("quarantine", "promote", bare, "--format", "runcomposer-verdicts")
        assert (promoted["run_state"], promoted["completion"], promoted["verdict_count"]) == ("COMPLETE", "PASS", 2)

        code, out, _err = cli("quarantine", "discard", extra)
        assert code == 0 and out.strip() == f"quarantine entry {extra} discarded"
        assert cli("quarantine", "list")[1].strip() == "quarantine is empty"
        code, _out, err = cli("quarantine", "discard", extra)
        assert code == 1 and "unknown quarantine entry" in err
        assert cli("quarantine", "attach", "q-nope", run_id)[0] == 1
        assert cli("quarantine")[0] == 2, "an action is required"


# -- doctor ---------------------------------------------------------------------------------------


class TestDoctor:
    def test_a_healthy_deployment(self, cli):
        code, out, _err = cli("doctor")
        assert code == 0, out
        assert out.splitlines()[0] == f"runcomposer {__version__}"
        assert f"config: {cli.config}" in out
        # Which distribution provides each plugin, at which version.
        assert any(line.split() == ["runners", "robot-pool", "runcomposer", __version__] for line in out.splitlines())
        assert any(line.split() == ["observers", "webhook", "runcomposer", __version__] for line in out.splitlines())
        assert "catalog: 60 items, 47 tags, sources ['demo-corpus']" in out
        assert "  demo (demo): ok — simulated — always available" in out
        assert "      --param seed=<string>  default 'demo'" in out
        assert "  closed (refusing): unknown" in out
        assert "taxonomy: bundled demo taxonomy, 11 leaf pattern(s)" in out
        assert out.rstrip().endswith("no problems found")

    def test_json(self, cli):
        report = cli.json("doctor")
        assert report["valid"] is True and report["problems"] == []
        assert report["version"] == __version__ and report["catalog"]["items"] == 60
        assert [runner["id"] for runner in report["runners"]] == ["demo", "closed", "ci", "broken"]
        assert report["runners"][0]["health"]["status"] == "ok"
        plugin = next(p for p in report["plugins"] if (p["group"], p["name"]) == ("runcomposer.stores", "sqlite"))
        assert (plugin["distribution"], plugin["version"]) == ("runcomposer", __version__)
        assert report["taxonomy"]["configured"] is False and report["recovered_stale"] == []

    def test_a_mistyped_runner_option_is_a_problem_naming_the_accepted_keys(self, tmp_path, capsys):
        cli = Cli(write_config(tmp_path, runners={"demo": {"sead": "x"}}), capsys)
        code, out, _err = cli("doctor")
        assert code == 1 and "1 problem(s):" in out
        assert "configuration: runners.demo: unknown option(s) ['sead']" in out
        assert "accepts: ['delay_s', 'seed']" in out
        report = cli.json("doctor")
        assert report["valid"] is False and len(report["problems"]) == 1

    def test_a_runner_that_is_down_and_a_drifted_taxonomy_are_problems(self, tmp_path, capsys):
        (tmp_path / "tree.yaml").write_text("taxonomy:\n  - {label: Returns, filter: Returns}\n", encoding="utf-8")
        cli = Cli(
            write_config(
                tmp_path,
                core={"taxonomy_file": "tree.yaml"},
                runners={"shell": {"plugin": "command", "command": "true", "cwd": "no-such-directory"}},
            ),
            capsys,
        )
        code, out, _err = cli("doctor")
        assert code == 1
        assert "runner shell: down — working directory not found" in out
        assert "taxonomy: 47 tag(s) no leaf claims, 1 leaf/leaves matching nothing" in out

    def test_doctor_reports_interrupted_runs(self, cli):
        import socket
        import subprocess
        import sys

        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait(timeout=60)
        with cli.service() as service:
            run_id = compose(service, "Auth-SSO")
            service.ledger.open(run_id, "demo", owner=f"{socket.gethostname()}:{gone.pid}:01TOKEN")
        code, out, _err = cli("doctor")
        assert code == 0 and "recovered: 1 interrupted run(s) marked STALE" in out
        assert stored_runs(cli)[0].state == "STALE"
        code, out, _err = cli("watch", run_id)
        assert code == 3 and "run.stale" in out, "a stale run ended without a result"

    def test_without_a_config_file_it_says_so(self, cli, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path / "..")
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)
        code, out, _err = cli("doctor", config=False)
        assert code == 0 and "config: (built-in defaults — no config file)" in out
        assert "  demo (demo): ok" in out, "the zero-config mode configures the demo runner"
        assert (empty / "runcomposer.db").is_file()


# -- output details ------------------------------------------------------------------


class TestOutputDetails:
    def test_a_reader_that_closes_the_pipe_early_gets_no_traceback(self, tmp_path):
        """`runcomposer catalog | head -1`: the reader has enough after one
        line. That is not an error and must not print one — including when
        the interpreter flushes stdout on its way out."""
        import subprocess
        import sys

        config = write_config(tmp_path)
        process = subprocess.Popen(
            [sys.executable, "-m", "runcomposer.cli", "catalog", "--config", str(config)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        # The reader is gone before the command has written anything, so its
        # output — whenever it is flushed — meets a closed pipe.
        process.stdout.close()
        stderr = process.stderr.read().decode()
        assert process.wait(timeout=30) in (0, 141)
        assert stderr == "", stderr

    def test_show_names_the_items_that_no_shard_reported(self, cli, tmp_path):
        """A run finalized with one of its items never reported: the item is
        MISSING, belongs to no shard, and `show` says so instead of printing
        a shard line that looks complete."""
        code, _out, err = cli("spec", "Auth-SSO", "--export", "--format", "json",
                              "-o", str(tmp_path / "spec.json"))
        assert code == 0
        run_id = run_id_from(err)
        planned = json.loads((tmp_path / "spec.json").read_text())["selection"]["materialized"]["item_ids"]
        assert len(planned) >= 2
        delivered = bundle(tmp_path / "bundle", planned[:-1], marker={"run_id": run_id})
        code, out, _err = cli("ingest", str(delivered))
        assert code == 0 and "INCOMPLETE" in out
        code, out, _err = cli("show", run_id)
        assert code == 0
        assert f"shard 1: PASS — {len(planned) - 1} PASS" in out
        assert "reported by no shard: 1 MISSING" in out
