"""The `command` runner (DESIGN.md §6.2d): a configured command per shard,
its output delivered through the configured parser.

Every test starts REAL subprocesses. The "test framework" being connected is
a small script written into ``tmp_path`` (it reads the ids file, writes a
result file, and can be told to fail, block or misbehave through its
environment) — plus, once, real pytest over ``examples/pytest-shop``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from runcomposer.sdk import ConfigError, InvalidParameter, ParserBase, Service

from .conftest import REPO, compose, posix_only, process_gone, run_to_end, wait_until

RUNNER = "shop-tests"
PY = sys.executable
PYTEST_SHOP = REPO / "examples" / "pytest-shop"


#: The stand-in for somebody's test framework: `shop_runner.py IDS_FILE OUT_DIR`.
SHOP_RUNNER = r'''
import json, os, pathlib, sys, time

ids_file, out_dir = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
ids = [line for line in ids_file.read_text(encoding="utf-8").split("\n") if line]
shard = os.environ.get("RUNCOMPOSER_SHARD", "")
print(f"shop runner: {len(ids)} test(s) in shard {shard}", flush=True)

(out_dir / "environment.json").write_text(json.dumps({
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "env": {k: v for k, v in os.environ.items() if k.startswith(("RUNCOMPOSER_", "SHOP_"))},
}), encoding="utf-8")

rendezvous = os.environ.get("SHOP_RENDEZVOUS")
if rendezvous:  # prove two shards are in flight at once: each waits for the other
    here = pathlib.Path(rendezvous)
    (here / f"started-{shard}").write_text(str(os.getpid()), encoding="utf-8")
    deadline = time.time() + 30
    while len(list(here.glob("started-*"))) < 2:
        if time.time() > deadline:
            sys.exit(7)
        time.sleep(0.02)

block = os.environ.get("SHOP_BLOCK")
if block and shard in os.environ.get("SHOP_BLOCK_SHARDS", shard).split(","):
    pathlib.Path(block).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(600)  # until somebody stops us

failing = set(filter(None, os.environ.get("SHOP_FAILING", "").split(",")))
if os.environ.get("SHOP_FORMAT") == "junit":
    cases = []
    for item in ids:
        classname, _, name = item.rpartition(".")
        inner = '<failure message="out of stock"/>' if item in failing else ""
        cases.append(f'<testcase classname="{classname}" name="{name}" time="0.04">{inner}</testcase>')
    (out_dir / "junit.xml").write_text(
        f'<testsuite name="shop" tests="{len(cases)}">' + "".join(cases) + "</testsuite>", encoding="utf-8")
elif os.environ.get("SHOP_FORMAT") != "nothing":
    verdicts = [{"name": i, "status": "FAIL" if i in failing else "PASS", "duration_ms": 40,
                 "message": "out of stock" if i in failing else ""} for i in ids]
    (out_dir / "results.json").write_text(
        json.dumps({"format": "runcomposer-verdicts", "verdicts": verdicts}), encoding="utf-8")

marker_run = os.environ.get("SHOP_MARKER_RUN")
if marker_run:
    (out_dir / "runcomposer_run.json").write_text(json.dumps({"run_id": marker_run}), encoding="utf-8")

exit_shards = os.environ.get("SHOP_EXIT_SHARDS")
if exit_shards is None or shard in exit_shards.split(","):
    sys.exit(int(os.environ.get("SHOP_EXIT", "0")))
'''


@pytest.fixture()
def script(tmp_path: Path) -> Path:
    """The stand-in framework, in a directory whose name has a space — a
    command line that is not quoted properly does not survive it."""
    tools = tmp_path / "shop tools"
    tools.mkdir()
    path = tools / "shop_runner.py"
    path.write_text(SHOP_RUNNER, encoding="utf-8")
    return path


def command_service(make_service, command: Any, **options: Any) -> Service:
    extra = {key: options.pop(key) for key in ("sources", "parsers") if key in options}
    return make_service(runners={RUNNER: {"plugin": "command", "command": command, **options}}, **extra)


def argv(script: Path) -> list[str]:
    return [PY, str(script), "{ids_file}", "{out_dir}"]


def execute(service: Service, selection: Any = "Payments-Cards", parameters: dict | None = None, **kw: Any):
    run_id = compose(service, selection, runner_id=RUNNER, parameters=parameters, **kw)
    return run_to_end(service, run_id, RUNNER, parameters)


def shard_dir(service: Service, run, shard: str = "1") -> Path:
    return service.config.artifact_dir / run.id / run.latest_dispatch.dispatch_id / f"shard-{shard}"


def environment(service: Service, run, shard: str = "1") -> dict:
    return json.loads((shard_dir(service, run, shard) / "environment.json").read_text(encoding="utf-8"))


def events_of(service: Service, run_id: str, kind: str) -> list[dict]:
    return [dict(e.data) for e in service.store.events(run_id) if e.kind == kind]


# -- the loop ---------------------------------------------------------------------


class TestRunsACommand:
    def test_an_argv_list_runs_to_complete(self, make_service, script):
        service = command_service(make_service, argv(script))
        run = execute(service, title="Card payments")
        assert (run.state, run.completion) == ("COMPLETE", "PASS")
        dispatch = run.latest_dispatch
        assert dispatch.mode == RUNNER and dispatch.shards == ("1",)
        planned = service.require_spec(run.id)["selection"]["materialized"]["item_ids"]
        assert [v.item_id for v in service.store.verdicts_for(run.id)] == planned
        assert dict(dispatch.summary) == {"PASS": len(planned)}
        assert f"command runner: {len(planned)} item(s) in 1 shard(s)" in dispatch.plan
        assert [e.kind for e in service.store.events(run.id)] == [
            "run.composed", "run.dispatched", "run.running", "log", "delivery.recorded",
            "run.completed",
        ]
        # What the command wrote is there to be read: results and its own output.
        names = [a["name"] for a in service.artifacts.list(run.id)]
        assert "results.json (shard 1)" in names and "command.log (shard 1)" in names
        assert all(a["kind"] == "local" and a["href"] for a in service.artifacts.list(run.id))
        log = (shard_dir(service, run) / "command.log").read_text(encoding="utf-8")
        assert f"shop runner: {len(planned)} test(s) in shard 1" in log

    @posix_only
    def test_a_command_line_string_runs_through_the_shell(self, make_service, script):
        """Placeholder values are shell-quoted for it; the rest of the line
        is the author's own shell, pipes and all."""
        service = command_service(
            make_service, f"'{PY}' '{script}' {{ids_file}} {{out_dir}} | tee {{out_dir}}/tee.txt"
        )
        run = execute(service)
        assert (run.state, run.completion) == ("COMPLETE", "PASS")
        assert "shop runner:" in (shard_dir(service, run) / "tee.txt").read_text(encoding="utf-8")

    def test_a_command_line_string_runs_through_this_platform_s_shell(self, make_service, script, tmp_path):
        """The same line on every platform — ``sh`` on POSIX, ``cmd`` on
        Windows: the placeholders are quoted for that shell (the work
        directory may hold a space too), and ``&&`` is the shell's own."""
        quote = subprocess.list2cmdline if os.name == "nt" else shlex.join
        command = f"{quote([PY, str(script)])} {{ids_file}} {{out_dir}} && echo the shell ran on"
        service = make_service(
            runners={RUNNER: {"plugin": "command", "command": command}},
            core={"artifact_dir": str(tmp_path / "work dir")},
        )
        run = execute(service)
        assert (run.state, run.completion) == ("COMPLETE", "PASS"), run.latest_dispatch.error
        log = (shard_dir(service, run) / "command.log").read_text(encoding="utf-8")
        assert "shop runner:" in log and "the shell ran on" in log
        assert " " in str(shard_dir(service, run))

    def test_placeholders_and_the_environment_say_the_same(self, make_service, script):
        service = command_service(
            make_service,
            [*argv(script), "--spec", "{spec_file}", "--run", "{run_id}", "--dispatch",
             "{dispatch_id}", "--shard", "{shard}"],
            env={"SHOP_STAGE": "staging"},
        )
        run = execute(service)
        dispatch = run.latest_dispatch
        seen = environment(service, run)
        out_dir = shard_dir(service, run).resolve()
        ids_file, spec_file = out_dir / "item_ids.txt", out_dir.parent / "spec.json"
        assert seen["argv"] == [
            str(ids_file), str(out_dir), "--spec", str(spec_file), "--run", run.id,
            "--dispatch", dispatch.dispatch_id, "--shard", "1",
        ]
        assert {k: v for k, v in seen["env"].items() if k.startswith("RUNCOMPOSER_")} == {
            "RUNCOMPOSER_IDS_FILE": str(ids_file),
            "RUNCOMPOSER_OUT_DIR": str(out_dir),
            "RUNCOMPOSER_SPEC_FILE": str(spec_file),
            "RUNCOMPOSER_RUN_ID": run.id,
            "RUNCOMPOSER_DISPATCH_ID": dispatch.dispatch_id,
            "RUNCOMPOSER_SHARD": "1",
        }
        assert seen["env"]["SHOP_STAGE"] == "staging"  # configured `env:` reaches the command
        # The ids file is the materialized selection, one id per line…
        planned = service.require_spec(run.id)["selection"]["materialized"]["item_ids"]
        assert ids_file.read_text(encoding="utf-8") == "\n".join(planned) + "\n"
        # …and the spec file is the document, byte for byte what the dispatch hashed.
        assert hashlib.sha256(spec_file.read_bytes()).hexdigest() == dispatch.spec_sha256
        assert json.loads(spec_file.read_text(encoding="utf-8"))["run"]["id"] == run.id

    @posix_only
    def test_braces_that_are_not_placeholders_are_left_alone(self, make_service, script):
        """A command line is shell, and shell is full of braces: an awk
        program and a ${VARIABLE} must arrive exactly as written."""
        service = command_service(
            make_service,
            f"'{PY}' '{script}' {{ids_file}} {{out_dir}}"
            " && echo checkout cart | awk '{print $2}' > {out_dir}/awk.txt"
            " && echo ${HOME} > {out_dir}/home.txt"
            " && echo '{not_a_placeholder}' > {out_dir}/other.txt",
        )
        run = execute(service)
        assert run.completion == "PASS", run.latest_dispatch.error
        out_dir = shard_dir(service, run)
        assert (out_dir / "awk.txt").read_text().strip() == "cart"
        assert (out_dir / "home.txt").read_text().strip() == os.environ["HOME"]
        assert (out_dir / "other.txt").read_text().strip() == "{not_a_placeholder}"

    def test_substitution_replaces_the_known_names_and_nothing_else(self):
        from runcomposer.plugins.command_runner import PLACEHOLDERS, substitute

        values = {name: f"<{name}>" for name in PLACEHOLDERS}
        assert substitute("{print $1} {ids_file} {unknown} {{out_dir}} {}", values, quote=False) == (
            "{print $1} <ids_file> {unknown} {<out_dir>} {}"
        )
        if os.name != "nt":
            values["out_dir"] = "/srv/shop results/it's here"
            quoted = substitute("ls {out_dir}", values, quote=True)
            assert quoted == "ls '/srv/shop results/it'\"'\"'s here'"

    def test_the_working_directory_is_configurable_and_relative_to_the_config(
        self, make_service, script, tmp_path
    ):
        (tmp_path / "checkout").mkdir()
        service = command_service(make_service, argv(script), cwd="checkout")
        run = execute(service)
        assert Path(environment(service, run)["cwd"]).resolve() == (tmp_path / "checkout").resolve()
        assert service.runner_health(RUNNER, refresh=True).status == "ok"

    def test_a_missing_working_directory_refuses_the_dispatch(self, make_service, script):
        service = command_service(make_service, argv(script), cwd="not-checked-out")
        run_id = compose(service, "Payments-Cards", runner_id=RUNNER)
        outcome = service.dispatch(run_id, RUNNER, foreground=True)
        assert "working directory not found" in outcome.refusal
        assert service.require_run(run_id).dispatches == ()
        health = service.runner_health(RUNNER, refresh=True)
        assert health.status == "down" and "not-checked-out" in health.detail

    def test_it_describes_itself_under_its_configured_key(self, make_service, script):
        service = command_service(make_service, argv(script), format="junit-xml", shards=2)
        (info,) = service.runner_infos(health=False)
        assert (info["id"], info["plugin"]) == (RUNNER, "command")
        assert info["result_format"] == "junit-xml"
        assert {"cancel", "shards"} <= set(info["capabilities"])

    @pytest.mark.parametrize(
        "options, complaint",
        [
            ({"command": ""}, "'command' must be a string or an argv list"),
            ({"command": []}, "argv list must be non-empty"),
            ({"command": ["run", 3]}, "argv list must be non-empty strings"),
            ({"command": "true", "shards": 0}, "at least 1"),
            ({"command": "true", "retries": 2}, "unknown option"),
            ({"command": "true", "parameters": [{"label": "Stage"}]}, "with a 'name'"),
            ({"command": "true", "parameters": [{"name": "s", "kind": "date"}]}, "kind must be one of"),
        ],
    )
    def test_bad_options_refuse_startup(self, make_service, options, complaint):
        command = options.pop("command")
        with pytest.raises(ConfigError, match=complaint) as excinfo:
            command_service(make_service, command, **options).validate()
        assert f"runners.{RUNNER}" in str(excinfo.value)

    def test_without_a_command_the_entry_is_refused(self, make_service):
        with pytest.raises(ConfigError, match=r"missing required option.*command"):
            make_service(runners={RUNNER: {"plugin": "command"}}).validate()


# -- shards -----------------------------------------------------------------------


class TestShards:
    def test_several_shards_split_the_selection(self, make_service, script):
        service = command_service(make_service, argv(script), shards=3)
        run = execute(service, "Payments")
        assert (run.state, run.completion) == ("COMPLETE", "PASS")
        dispatch = run.latest_dispatch
        assert dispatch.shards == ("1", "2", "3")
        planned = service.require_spec(run.id)["selection"]["materialized"]["item_ids"]
        assert sorted(i for items in dispatch.shard_items.values() for i in items) == sorted(planned)
        for shard, items in dispatch.shard_items.items():
            carried = (shard_dir(service, run, shard) / "item_ids.txt").read_text().split()
            assert carried == list(items)
            assert environment(service, run, shard)["env"]["RUNCOMPOSER_SHARD"] == shard
            reported = service.store.verdicts_for(run.id, dispatch.dispatch_id, shard=shard)
            assert [v.item_id for v in reported] == list(items)
        assert "max_parallel=1" in dispatch.plan and "cold start" in dispatch.plan

    def test_max_parallel_really_runs_shards_side_by_side(self, make_service, script, tmp_path):
        """Each shard's process waits until it sees the other one running —
        which only ever happens if both are in flight at once."""
        meeting = tmp_path / "meeting"
        meeting.mkdir()
        service = command_service(
            make_service, argv(script), shards=2, max_parallel=2,
            env={"SHOP_RENDEZVOUS": str(meeting)},
        )
        run = execute(service, "Payments")
        assert (run.state, run.completion) == ("COMPLETE", "PASS"), run.latest_dispatch.error
        assert sorted(p.name for p in meeting.iterdir()) == ["started-1", "started-2"]

    def test_the_second_run_balances_shards_by_duration(self, make_service, script):
        service = command_service(make_service, argv(script), shards=2)
        assert "cold start" in execute(service, "Payments").latest_dispatch.plan
        again = execute(service, "Payments")
        assert "duration-balanced chunking: history for 17/17 item(s)" in again.latest_dispatch.plan


# -- exit codes, timeouts, things going wrong ------------------------------------------


class TestFailures:
    def test_a_non_accepted_exit_code_fails_the_dispatch_and_keeps_the_rest(self, make_service, script):
        service = command_service(
            make_service, argv(script), shards=2, env={"SHOP_EXIT": "3", "SHOP_EXIT_SHARDS": "2"},
        )
        run = execute(service, "Payments")
        assert (run.state, run.completion) == ("FAILED", None)
        dispatch = run.latest_dispatch
        assert dispatch.error.endswith(
            "shard 2: the command exited 3, which is not an accepted exit code ([0]) — see command.log"
        )
        # Shard 1 was delivered and is kept; shard 2's items never reported.
        delivered = len(dispatch.shard_items["1"])
        assert len(service.store.verdicts_for(run.id)) == delivered
        assert dict(dispatch.summary) == {"PASS": delivered, "MISSING": len(dispatch.shard_items["2"])}
        # The failed shard's output is recorded, so "see command.log" leads somewhere.
        artifacts = {a["name"]: a for a in service.artifacts.list(run.id, server_paths=True)}
        failed_log = artifacts["command.log (shard 2)"]
        assert failed_log["shard"] == "2" and failed_log["href"]
        assert "shop runner:" in Path(failed_log["url_or_path"]).read_text(encoding="utf-8")
        assert events_of(service, run.id, "run.failed")[0]["error"] == dispatch.error

    def test_ok_exit_codes_say_which_codes_still_mean_results(self, make_service, script):
        """Many frameworks exit non-zero when tests fail. That is a result."""
        failing = "Shop.Payments.Cards.T002"
        service = command_service(
            make_service, argv(script), ok_exit_codes=[0, 1],
            env={"SHOP_EXIT": "1", "SHOP_FAILING": failing},
        )
        run = execute(service)
        assert (run.state, run.completion) == ("COMPLETE", "FAIL")
        by_item = {v.item_id: v for v in service.store.verdicts_for(run.id)}
        assert (by_item[failing].status, by_item[failing].message) == ("FAIL", "out of stock")

    def test_an_accepted_exit_without_results_is_still_a_failure(self, make_service, script):
        service = command_service(make_service, argv(script), env={"SHOP_FORMAT": "nothing"})
        run = execute(service)
        assert run.state == "FAILED"
        assert "shard 1:" in run.latest_dispatch.error
        assert "contains no 'runcomposer-verdicts' results" in run.latest_dispatch.error

    def test_a_command_that_cannot_be_started_fails_the_dispatch(self, make_service):
        service = command_service(make_service, ["shop-test-runner-that-is-not-installed", "{ids_file}"])
        run = execute(service)
        assert run.state == "FAILED"
        assert "shard 1: the command could not be started" in run.latest_dispatch.error

    def test_a_shard_that_overruns_timeout_s_is_stopped_and_fails(self, make_service, script, tmp_path):
        started = tmp_path / "blocked.pid"
        service = command_service(
            make_service, argv(script), timeout_s=1, env={"SHOP_BLOCK": str(started)}
        )
        began = time.monotonic()
        run = execute(service)
        assert time.monotonic() - began < 60  # not the ten minutes the command wanted
        assert run.state == "FAILED"
        assert run.latest_dispatch.error == "RuntimeError: shard 1: exceeded timeout_s=1"
        wait_until(lambda: process_gone(int(started.read_text())), what="the command to be gone")
        # What the command printed before it was stopped is still readable.
        names = [artifact["name"] for artifact in service.artifacts.list(run.id)]
        assert "command.log (shard 1)" in names

    def test_a_bundle_carrying_another_run_s_marker_is_not_delivered(self, make_service, script):
        service = command_service(
            make_service, argv(script), env={"SHOP_MARKER_RUN": "01SOMEOTHERRUN0000000000000"}
        )
        run = execute(service)
        assert run.state == "FAILED" and "another run's results" in run.latest_dispatch.error
        assert service.store.verdicts_for(run.id) == []


class TestCancel:
    @posix_only
    def test_cancel_stops_the_command_and_what_it_started(self, make_service, script, tmp_path):
        """Through a shell on purpose: the process to stop is the shell's
        child, not the process the runner started."""
        started = tmp_path / "blocked.pid"
        service = command_service(
            make_service,
            f"'{PY}' '{script}' {{ids_file}} {{out_dir}}; echo finished",
            shards=2, max_parallel=2,
            env={"SHOP_BLOCK": str(started), "SHOP_BLOCK_SHARDS": "2"},
        )
        run_id = compose(service, "Payments", runner_id=RUNNER)
        outcome = service.dispatch(run_id, RUNNER)  # in the background
        wait_until(
            lambda: started.exists()
            and "1" in service.store.delivered_shards(run_id, outcome.dispatch_id),
            what="shard 1 delivered and shard 2 blocking",
        )
        pid = int(started.read_text())
        assert not process_gone(pid)
        assert service.require_run(run_id).state == "RUNNING"

        service.cancel(run_id)
        run = service.wait(run_id, timeout=30)
        assert (run.state, run.completion) == ("CANCELLED", None)
        wait_until(lambda: process_gone(pid), timeout=10, what="the blocked command to be gone")
        dispatch = run.latest_dispatch
        assert dict(dispatch.summary) == {
            "PASS": len(dispatch.shard_items["1"]), "MISSING": len(dispatch.shard_items["2"]),
        }
        assert service.dispatcher.join(timeout=10)  # nothing is executing any more
        assert service.dispatcher.active_dispatch_ids() == []
        # A cancelled run can be started again.
        again = command_service(make_service, argv(script))
        assert run_to_end(again, run_id, RUNNER).state == "COMPLETE"


#: A command that starts a process of its own and waits: each writes its
#: process id into a file of its own (no two processes append to one file).
STARTS_A_CHILD = r'''
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
pathlib.Path(sys.argv[2]).write_text(str(__import__("os").getpid()), encoding="utf-8")
time.sleep(600)
'''


class TestWhatTheCommandStarted:
    """On every platform — through the process group on POSIX, the worker's
    job on Windows — what the command started ends with it."""

    def pids(self, tmp_path: Path) -> tuple[Path, Path]:
        return tmp_path / "grandchild.pid", tmp_path / "command.pid"

    def service(self, make_service, tmp_path: Path, **options: Any) -> Service:
        parent = tmp_path / "starts_a_child.py"
        parent.write_text(STARTS_A_CHILD, encoding="utf-8")
        grandchild, command = self.pids(tmp_path)
        return command_service(make_service, [PY, str(parent), str(grandchild), str(command)], **options)

    def written(self, path: Path) -> int:
        return int(wait_until(lambda: path.exists() and path.read_text(encoding="utf-8").strip(),
                              timeout=60, what=f"{path.name} to be written"))

    def test_a_cancel_ends_the_command_and_what_it_started(self, make_service, tmp_path):
        service = self.service(make_service, tmp_path)
        run_id = compose(service, "Payments", runner_id=RUNNER)
        service.dispatch(run_id, RUNNER)  # in the background
        pids = [self.written(path) for path in self.pids(tmp_path)]
        assert not any(process_gone(pid) for pid in pids)
        service.cancel(run_id)
        assert service.wait(run_id, timeout=60).state == "CANCELLED"
        for pid in pids:
            wait_until(lambda pid=pid: process_gone(pid), timeout=30, what=f"process {pid} to be gone")

    def test_a_timeout_ends_the_command_and_what_it_started(self, make_service, tmp_path):
        """The timeout leaves the command time to start its child (fifteen
        seconds: a new interpreter on a slow machine), then stops both."""
        service = self.service(make_service, tmp_path, timeout_s=15)
        run_id = compose(service, "Payments", runner_id=RUNNER)
        service.dispatch(run_id, RUNNER)
        pids = [self.written(path) for path in self.pids(tmp_path)]
        run = service.wait(run_id, timeout=90)
        assert run.state == "FAILED" and "exceeded timeout_s=15" in (run.latest_dispatch.error or "")
        for pid in pids:
            wait_until(lambda pid=pid: process_gone(pid), timeout=30, what=f"process {pid} to be gone")


# -- formats and parsers ----------------------------------------------------------------


class ShoutingJunitParser(ParserBase):
    """A deployment's own reading of JUnit reports, configured under
    `parsers:` — it upper-cases messages so a test can tell it was used."""

    format_id = "junit-xml"

    def __init__(self, pattern: str = "junit*.xml"):
        from runcomposer.plugins.junit_parser import JunitXmlParser

        self._inner = JunitXmlParser(pattern)

    def parse(self, path: Any) -> list:
        import dataclasses

        return [dataclasses.replace(p, message=p.message.upper()) for p in self._inner.parse(path)]


class TestFormats:
    def test_a_junit_report_goes_through_the_junit_parser(self, make_service, script):
        failing = "Shop.Payments.Cards.T002"
        service = command_service(
            make_service, argv(script), format="junit-xml",
            env={"SHOP_FORMAT": "junit", "SHOP_FAILING": failing},
        )
        run = execute(service)
        assert (run.state, run.completion) == ("COMPLETE", "FAIL")
        by_item = {v.item_id: v for v in service.store.verdicts_for(run.id)}
        assert len(by_item) == run.planned_count
        assert (by_item[failing].status, by_item[failing].message) == ("FAIL", "out of stock")
        assert by_item["Shop.Payments.Cards.T001"].duration_ms == 40
        assert {d.format for d in run.deliveries} == {"junit-xml"}
        # Composing for the runner stamped what it produces into the document.
        assert service.require_spec(run.id)["results"]["expect"] == [{"format": "junit-xml"}]

    def test_the_configured_parser_for_the_format_is_the_one_used(self, make_service, script):
        failing = "Shop.Payments.Cards.T002"
        service = command_service(
            make_service, argv(script), format="junit-xml",
            env={"SHOP_FORMAT": "junit", "SHOP_FAILING": failing},
            parsers={"junit-xml": {"module": "tests.test_command_runner:ShoutingJunitParser"}},
        )
        run = execute(service)
        message = next(v.message for v in service.store.verdicts_for(run.id) if v.item_id == failing)
        assert message == "OUT OF STOCK"

    def test_real_pytest_is_connected_with_configuration_alone(self, make_service):
        """The framework-agnosticism proof (DESIGN.md §6.1), with REAL pytest:
        a manifest of nodeids, the example's command, the junit parser — and
        no code written for the occasion."""
        service = command_service(
            make_service,
            [PY, str(PYTEST_SHOP / "run_pytest.py"), "{ids_file}", "{out_dir}"],
            format="junit-xml",
            sources={"manifest": {"path": str(PYTEST_SHOP / "manifest.json")}},
        )
        run = execute(service, "Cart")
        assert (run.state, run.completion) == ("COMPLETE", "FAIL"), run.latest_dispatch.error
        statuses = {v.item_id: v.status for v in service.store.verdicts_for(run.id)}
        assert len(statuses) == 5
        assert statuses["test_cart.py::test_total_never_negative"] == "FAIL"
        assert statuses["test_cart.py::test_checkout[visa]"] == "PASS"
        assert events_of(service, run.id, "warning") == []

    def test_every_name_pytest_reports_resolves_to_exactly_one_id(self, make_service):
        """The id-space invariant (§2), end to end: the whole example corpus
        through pytest and back — nothing unknown, nothing missing, no two
        reports for one id."""
        service = command_service(
            make_service,
            [PY, str(PYTEST_SHOP / "run_pytest.py"), "{ids_file}", "{out_dir}"],
            format="junit-xml",
            sources={"manifest": {"path": str(PYTEST_SHOP / "manifest.json")}},
        )
        run = execute(service, "regex:.")
        assert run.state == "COMPLETE", run.latest_dispatch.error
        verdicts = service.store.verdicts_for(run.id)
        assert len(verdicts) == len({v.item_id for v in verdicts}) == run.planned_count == 7
        assert "MISSING" not in run.latest_dispatch.summary
        assert events_of(service, run.id, "warning") == []

    def test_runcomposer_exec_itself_as_the_command(self, make_service):
        """The vendorable consumer under the command runner: it executes the
        spec file it is handed and writes the marker — which is verified
        against the very bytes the runner handed out."""
        service = command_service(
            make_service,
            [PY, str(REPO / "src" / "runcomposer_exec.py"), "{spec_file}", "--out", "{out_dir}",
             "--simulate", "--dispatch", "{dispatch_id}", "--shard", "{shard}"],
        )
        run = execute(service, title="Zahlungen prüfen")
        assert run.state == "COMPLETE", run.latest_dispatch.error
        dispatch = run.latest_dispatch
        marker = json.loads((shard_dir(service, run) / "runcomposer_run.json").read_text("utf-8"))
        assert marker == {
            "run_id": run.id, "spec_sha256": dispatch.spec_sha256,
            "dispatch_id": dispatch.dispatch_id, "shard": "1",
        }
        assert sum(dispatch.summary.values()) == run.planned_count
        assert "MISSING" not in dispatch.summary
        assert events_of(service, run.id, "warning") == []


# -- per-run parameters ---------------------------------------------------------------------

STAGE_PARAMETERS = [
    {"name": "stage", "kind": "choice", "choices": ["staging", "production"], "default": "staging",
     "label": "Stage", "help": "Which shop to run against."},
    {"name": "browsers", "kind": "multi_choice", "choices": ["firefox", "chromium"]},
    {"name": "headless", "kind": "boolean", "default": True},
]


class TestParameters:
    def test_declared_parameters_arrive_in_the_environment(self, make_service, script):
        service = command_service(make_service, argv(script), parameters=STAGE_PARAMETERS)
        run = execute(service, parameters={"stage": "production", "browsers": ["firefox", "chromium"]})
        env = environment(service, run)["env"]
        assert env["RUNCOMPOSER_PARAM_STAGE"] == "production"
        assert env["RUNCOMPOSER_PARAM_BROWSERS"] == "firefox,chromium"
        assert env["RUNCOMPOSER_PARAM_HEADLESS"] == "True"  # the default, filled in
        assert dict(run.latest_dispatch.parameters) == {
            "stage": "production", "browsers": ["firefox", "chromium"], "headless": True,
        }
        assert service.require_spec(run.id)["runner"][RUNNER]["stage"] == "production"

    def test_defaults_apply_when_nothing_is_given(self, make_service, script):
        service = command_service(make_service, argv(script), parameters=STAGE_PARAMETERS)
        env = environment(service, run := execute(service))["env"]
        assert env["RUNCOMPOSER_PARAM_STAGE"] == "staging"
        assert "RUNCOMPOSER_PARAM_BROWSERS" not in env  # no default, not given: not set
        assert dict(run.latest_dispatch.parameters) == {"stage": "staging", "headless": True}

    def test_they_are_listed_for_a_form_and_validated_before_a_run_exists(self, make_service, script):
        service = command_service(make_service, argv(script), parameters=STAGE_PARAMETERS)
        (info,) = service.runner_infos(health=False)
        assert [(p["name"], p["kind"], p["label"]) for p in info["parameters"]] == [
            ("stage", "choice", "Stage"), ("browsers", "multi_choice", "browsers"),
            ("headless", "boolean", "headless"),
        ]
        for bad in ({"stage": "moon"}, {"headless": "yes"}, {"speed": "fast"}):
            with pytest.raises(InvalidParameter) as excinfo:
                compose(service, "Payments-Cards", runner_id=RUNNER, parameters=bad)
            assert excinfo.value.parameter == next(iter(bad))
        assert service.list_runs().total == 0

    def test_a_document_cannot_set_what_was_not_declared(self, make_service, script):
        """Regression: `runner.<id>` of a document may carry declared per-run
        parameters and nothing else — not `command`, not `env`, not `cwd`."""
        service = command_service(make_service, argv(script), parameters=STAGE_PARAMETERS)
        run_id = compose(
            service, "Payments-Cards",
            runner_section={RUNNER: {"stage": "production", "command": "rm -rf /", "cwd": "/",
                                     "env": {"SHOP_EXIT": "9"}}},
        )
        run = run_to_end(service, run_id, RUNNER)
        assert (run.state, run.completion) == ("COMPLETE", "PASS")
        seen = environment(service, run)
        assert seen["env"]["RUNCOMPOSER_PARAM_STAGE"] == "production"  # declared: honoured
        assert "SHOP_EXIT" not in seen["env"] and Path(seen["cwd"]) != Path("/")
        (warning,) = [w["message"] for w in events_of(service, run_id, "warning")]
        assert "['command', 'cwd', 'env']" in warning and "ignored" in warning


# -- §3.3 drift ---------------------------------------------------------------------------


@pytest.fixture()
def manifest(tmp_path: Path) -> Path:
    """A small catalog of the shop's cart tests, in a file a test can edit."""
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {"items": [{"id": f"Shop.Cart.Core.T00{n}", "tags": ["Cart"]} for n in (1, 2, 3)]}
        ),
        encoding="utf-8",
    )
    return path


def remove_last_item(manifest: Path) -> str:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    gone = data["items"].pop()["id"]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    return gone


class TestDrift:
    def test_a_changed_corpus_refuses_the_dispatch(self, make_service, script, manifest):
        service = command_service(
            make_service, argv(script), sources={"manifest": {"path": str(manifest)}}
        )
        run_id = compose(service, "Cart", runner_id=RUNNER)
        remove_last_item(manifest)
        outcome = service.dispatch(run_id, RUNNER, foreground=True)
        assert "drift" in outcome.refusal and "allow_drift" in outcome.refusal
        run = service.require_run(run_id)
        assert run.state == "COMPOSED" and run.dispatches == ()
        # Nothing was started: no work directory, no command.
        assert not list(service.config.artifact_dir.rglob("command.log"))

    def test_allow_drift_runs_the_intersection_and_reports_the_rest_as_skipped(
        self, make_service, script, manifest
    ):
        service = command_service(
            make_service, argv(script), allow_drift=True,
            sources={"manifest": {"path": str(manifest)}},
        )
        run_id = compose(service, "Cart", runner_id=RUNNER)
        gone = remove_last_item(manifest)
        run = run_to_end(service, run_id, RUNNER)
        assert (run.state, run.completion) == ("COMPLETE", "PASS")
        assert run.latest_dispatch.shards == ("1", "drift")
        by_item = {v.item_id: (v.status, v.message, v.shard) for v in service.store.verdicts_for(run_id)}
        assert by_item[gone] == ("SKIP", "drift", "drift")
        assert [s for i, (s, _m, _sh) in by_item.items() if i != gone] == ["PASS", "PASS"]
        # The command was handed only what still exists.
        carried = (shard_dir(service, run) / "item_ids.txt").read_text().split()
        assert gone not in carried and len(carried) == 2

    def test_an_unchanged_corpus_is_not_drift(self, make_service, script, manifest):
        service = command_service(
            make_service, argv(script), sources={"manifest": {"path": str(manifest)}}
        )
        run_id = compose(service, "Cart", runner_id=RUNNER)
        shutil.copy(manifest, manifest.with_suffix(".bak"))  # the directory changed, the catalog did not
        assert run_to_end(service, run_id, RUNNER).completion == "PASS"


class TestSecretsInTheLog:
    def test_a_secret_the_command_prints_is_blanked_in_the_recorded_log(self, make_service, script, monkeypatch):
        """``command.log`` is served: a configured ``${env:…}`` value the
        command echoes is blanked in it, as in every other recorded text."""
        secret = "shop-deploy-key-9f3e"
        monkeypatch.setenv("SHOP_DEPLOY_KEY", secret)
        echo = [PY, "-c", "import os, sys; print('key is', os.environ['SHOP_KEY']); sys.exit(0)"]
        service = command_service(
            make_service, echo, env={"SHOP_KEY": "${env:SHOP_DEPLOY_KEY}"}, format="runcomposer-verdicts",
        )
        run = execute(service)
        log = (shard_dir(service, run) / "command.log").read_text(encoding="utf-8")
        assert log.startswith("key is ") and secret not in log
        recorded = [a for a in service.store.artifact_refs(run.id) if a.name.startswith("command.log")]
        assert recorded
