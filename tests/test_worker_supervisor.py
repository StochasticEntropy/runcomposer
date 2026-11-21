"""``supervise_workers`` — shards in spawned worker processes, for any
framework (DESIGN.md §6.6): lanes, the hand-back of a result, workers that
raise or die, the stop protocol and what it reaches, cancellation, the
start and end lines in the event stream, a worker's working directory and
environment — and a runner composed from it, run through the kit and a real
deployment.

Every test starts real processes; the targets are in
``tests/supervised_targets.py``. Nothing here waits by sleeping: a worker
waits for a condition with a deadline, the test for an outcome.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from runcomposer.sdk import WorkerOutcome, WorkerPayload, supervise_workers
from runcomposer.testing import ConnectorHarness, FakeDispatchContext, check_runner

from . import supervised_targets as targets
from .conftest import posix_only, process_gone, wait_until
from .shop_check_runner import DECLINED, CheckRunner

CARDS = [f"Shop.Payments.Cards.T{n:03d}" for n in range(1, 7)]
GRACE = 0.5


def payload(tmp_path: Path, shard: str, **more: Any) -> WorkerPayload:
    return WorkerPayload(shard=shard, out_dir=str(tmp_path / shard), **more)


def process_state(pid: int) -> str | None:
    """The state letter of ``pid`` (``Z`` for a process nobody reaped), or
    ``None`` when there is no such process at all."""
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_bytes()
        except OSError:
            return None
        return stat.rpartition(b")")[2].split()[0].decode()
    shown = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return shown.stdout.strip() or None


class Recorder:
    """``on_done`` and ``log`` for a test: what was reported, in which thread."""

    def __init__(self) -> None:
        self.done: list[tuple[str, WorkerOutcome]] = []
        self.threads: set[int] = set()
        self.lines: list[tuple[str, str]] = []

    def on_done(self, payload: WorkerPayload, outcome: WorkerOutcome) -> None:
        assert outcome.payload == payload
        self.done.append((payload.shard, outcome))
        self.threads.add(threading.get_ident())

    def log(self, message: str, level: str) -> None:
        self.lines.append((level, message))


# -- lanes ---------------------------------------------------------------------------------


class TestLanes:
    @posix_only
    def test_lanes_run_side_by_side_and_a_lane_keeps_its_order(self, tmp_path):
        """``staging`` holds three shards, ``preview`` one. The first shard of
        each lane can only finish by meeting the other — both running at
        once. Each later staging shard finds the process of the one before
        it gone: one after another, in the order given."""
        meeting = tmp_path / "meeting"
        meeting.mkdir()
        payloads = [
            payload(tmp_path, "staging-01", lane="staging", data={"meeting": str(meeting), "meet": ["preview-01"]}),
            payload(tmp_path, "staging-02", lane="staging", data={"meeting": str(meeting), "after": "staging-01"}),
            payload(tmp_path, "staging-03", lane="staging", data={"meeting": str(meeting), "after": "staging-02"}),
            payload(tmp_path, "preview-01", lane="preview", data={"meeting": str(meeting), "meet": ["staging-01"]}),
        ]
        record = Recorder()
        outcomes = supervise_workers(payloads, targets.lane_step, max_workers=4, on_done=record.on_done)

        results = {o.payload.shard: o.result for o in outcomes}
        assert all(o.status == "finished" for o in outcomes), [o.error for o in outcomes]
        assert results["staging-01"]["met"] and results["preview-01"]["met"], "two lanes ran at once"
        for shard in ("staging-02", "staging-03"):
            assert results[shard]["previous_ended"] and not results[shard]["previous_still_running"]
        staging = [shard for shard, _o in record.done if shard.startswith("staging")]
        assert staging == ["staging-01", "staging-02", "staging-03"]

    def test_max_workers_bounds_what_runs_at_once(self, tmp_path):
        """Two shards of no lane and one worker: the second starts only
        after the first has ended."""
        record = Recorder()
        supervise_workers(
            [payload(tmp_path, "1"), payload(tmp_path, "2")], targets.hand_back,
            max_workers=1, log=record.log,
        )
        assert [message.split(":")[1].strip().split()[0] for _l, message in record.lines] == [
            "started", "finished", "started", "finished",
        ]


# -- what comes back -------------------------------------------------------------------------


class TestTheHandBack:
    def test_the_target_s_return_value_comes_back_from_its_own_process(self, tmp_path):
        record = Recorder()
        (outcome,) = supervise_workers(
            [payload(tmp_path, "eu", item_ids=tuple(CARDS[:2]), data={"region": "eu"})],
            targets.hand_back, on_done=record.on_done,
        )
        assert (outcome.status, outcome.exitcode, outcome.error, outcome.problem) == ("finished", 0, None, None)
        assert outcome.result["items"] == CARDS[:2] and outcome.result["data"] == {"region": "eu"}
        assert outcome.result["pid"] != os.getpid()
        assert record.threads == {threading.get_ident()}, "on_done runs in the calling thread"
        assert (tmp_path / "eu").is_dir(), "the shard's directory is created before its worker starts"

    def test_a_target_that_raises_is_reported_with_its_exception(self, tmp_path):
        (outcome,) = supervise_workers([payload(tmp_path, "1", item_ids=(CARDS[0],))], targets.raises)
        assert (outcome.status, outcome.exitcode, outcome.result) == ("raised", 1, None)
        assert outcome.error.startswith(f"ValueError: no such product: {CARDS[0]}")
        assert "Traceback" in outcome.error and "supervised_targets.py" in outcome.error
        assert outcome.problem == f"1: ValueError: no such product: {CARDS[0]}"

    def test_a_worker_that_dies_is_reported_with_its_exit_code(self, tmp_path):
        (outcome,) = supervise_workers([payload(tmp_path, "1")], targets.dies)
        assert (outcome.status, outcome.exitcode, outcome.result, outcome.error) == ("died", 7, None, None)
        assert outcome.problem == "1: the worker process ended (exit code 7) without a result"

    def test_on_done_is_called_once_per_shard_and_the_result_lists_them_in_order_of_ending(self, tmp_path):
        record = Recorder()
        payloads = [payload(tmp_path, str(n)) for n in range(1, 5)]
        outcomes = supervise_workers(payloads, targets.hand_back, max_workers=2, on_done=record.on_done)
        assert sorted(shard for shard, _o in record.done) == ["1", "2", "3", "4"]
        assert [shard for shard, _o in record.done] == [o.payload.shard for o in outcomes]

    def test_an_exception_from_on_done_stops_the_running_workers_and_propagates(self, tmp_path):
        def refuse(done: WorkerPayload, outcome: WorkerOutcome) -> None:
            raise RuntimeError(f"cannot deliver {done.shard}")

        blocked = payload(tmp_path, "blocked")
        with pytest.raises(RuntimeError, match="cannot deliver quick"):
            supervise_workers(
                [payload(tmp_path, "quick"), blocked], targets.quick_or_blocking, max_workers=2,
                on_done=refuse, grace_s=GRACE,
            )
        pid = int((tmp_path / "blocked" / "running").read_text(encoding="utf-8"))
        assert process_state(pid) is None, "stopped and reaped"


# -- the stop protocol ---------------------------------------------------------------------


def running_marker(tmp_path: Path, shard: str = "1") -> Any:
    return lambda: (tmp_path / shard / "running").exists()


class TestStopping:
    def test_a_worker_is_asked_first_and_ends_on_its_own(self, tmp_path):
        record = Recorder()
        outcomes = supervise_workers(
            [payload(tmp_path, "1")], targets.stops_when_asked,
            cancelled=running_marker(tmp_path), on_done=record.on_done, log=record.log, grace_s=30,
        )
        assert outcomes == [] and record.done == [], "a stopped shard is not reported as ended"
        assert (tmp_path / "1" / "saw-stop").read_text(encoding="utf-8") == "True True"
        assert (tmp_path / "1" / ".stop-requested").is_file()
        assert record.lines[-1][0] == "info" and re.fullmatch(
            r"shard 1: stopped after \d+\.\ds", record.lines[-1][1]
        )

    @posix_only
    def test_a_worker_that_ignores_the_request_is_terminated(self, tmp_path):
        started = time.monotonic()
        supervise_workers(
            [payload(tmp_path, "1")], targets.ignores_the_request,
            cancelled=running_marker(tmp_path), grace_s=GRACE,
        )
        assert (tmp_path / "1" / "ended-by").read_text(encoding="utf-8") == "SystemExit"
        assert time.monotonic() - started < 30
        pid = int((tmp_path / "1" / "running").read_text(encoding="utf-8"))
        assert process_state(pid) is None, "terminated and reaped by the supervisor"

    @posix_only
    def test_a_worker_that_ignores_every_signal_is_killed(self, tmp_path):
        supervise_workers(
            [payload(tmp_path, "1")], targets.ignores_every_signal,
            cancelled=running_marker(tmp_path), grace_s=GRACE,
        )
        pid = int((tmp_path / "1" / "running").read_text(encoding="utf-8"))
        assert process_state(pid) is None, "killed and reaped by the supervisor"

    @posix_only
    def test_the_stop_reaches_what_the_worker_started(self, tmp_path):
        """The worker's children are in its process group: one ends on the
        termination signal, the other ignores it and is killed once the
        worker is gone."""
        supervise_workers(
            [payload(tmp_path, "1")], targets.starts_children,
            cancelled=running_marker(tmp_path), grace_s=GRACE,
        )
        for kind in ("plain", "stubborn"):
            child = int((tmp_path / "1" / f"child-{kind}.pid").read_text(encoding="utf-8"))
            wait_until(lambda child=child: process_gone(child), timeout=10, what=f"the {kind} child to be gone")

    @posix_only
    def test_a_shard_over_its_timeout_is_stopped_and_reported(self, tmp_path):
        record = Recorder()
        (outcome,) = supervise_workers(
            [payload(tmp_path, "1")], targets.ignores_every_signal,
            timeout_s=1, grace_s=GRACE, on_done=record.on_done, log=record.log,
        )
        assert outcome.status == "timed out" and outcome.timed_out and outcome.exitcode != 0
        assert outcome.problem == "1: exceeded its timeout and was stopped"
        assert [shard for shard, _o in record.done] == ["1"]
        assert record.lines[-1][0] == "warning"
        assert re.fullmatch(r"shard 1: exceeded the timeout of 1s and was stopped after \d+\.\ds", record.lines[-1][1])


# -- cancellation ----------------------------------------------------------------------------


class TestCancellation:
    def test_cancelled_before_anything_starts_nothing_starts(self, tmp_path):
        record = Recorder()
        outcomes = supervise_workers(
            [payload(tmp_path, "1"), payload(tmp_path, "2")], targets.hand_back,
            cancelled=lambda: True, on_done=record.on_done, log=record.log,
        )
        assert outcomes == [] and record.done == [] and record.lines == []
        assert not (tmp_path / "1").exists() and not (tmp_path / "2").exists()

    def test_cancelled_in_the_middle_of_a_lane_no_further_shard_starts(self, tmp_path):
        record = Recorder()
        payloads = [
            payload(tmp_path, "staging-01", lane="staging"),
            payload(tmp_path, "staging-02", lane="staging"),
        ]
        outcomes = supervise_workers(
            payloads, targets.stops_when_asked,
            cancelled=running_marker(tmp_path, "staging-01"), on_done=record.on_done, log=record.log, grace_s=30,
        )
        assert outcomes == [] and record.done == []
        assert (tmp_path / "staging-01" / "saw-stop").read_text(encoding="utf-8") == "True True"
        assert not (tmp_path / "staging-02").exists(), "the lane's next shard never started"
        assert [message for _l, message in record.lines if "staging-02" in message] == []

    def test_the_stop_marker_is_the_one_the_robot_worker_looks_for(self, tmp_path):
        """The Robot toolkit's worker also stops when the marker is there; it
        relies on this name."""
        from runcomposer.sdk.robot import STOP_FILE

        supervise_workers(
            [payload(tmp_path, "1")], targets.stops_when_asked, cancelled=running_marker(tmp_path), grace_s=30,
        )
        assert (tmp_path / "1" / STOP_FILE).is_file()


# -- the event stream, the working directory, refusals ------------------------------------------


class TestLogLines:
    def test_one_line_when_a_shard_starts_and_one_when_it_ends(self, tmp_path):
        record = Recorder()
        supervise_workers(
            [payload(tmp_path, "staging-01", lane="staging"), payload(tmp_path, "2")],
            targets.hand_back, max_workers=1, log=record.log,
        )
        assert record.lines[0] == ("info", "shard staging-01 (lane staging): started")
        assert record.lines[1][0] == "info"
        assert re.fullmatch(r"shard staging-01 \(lane staging\): finished after \d+\.\ds", record.lines[1][1])
        assert record.lines[2] == ("info", "shard 2: started")
        assert re.fullmatch(r"shard 2: finished after \d+\.\ds", record.lines[3][1])
        assert len(record.lines) == 4

    def test_a_shard_that_did_not_finish_ends_with_a_warning_saying_why(self, tmp_path):
        record = Recorder()
        supervise_workers([payload(tmp_path, "1")], targets.dies, log=record.log)
        supervise_workers([payload(tmp_path, "2", item_ids=(CARDS[0],))], targets.raises, log=record.log)
        ends = [line for line in record.lines if "started" not in line[1]]
        assert ends[0][0] == "warning" and ends[0][1].startswith("shard 1: ended without a result (exit code 7) after ")
        assert ends[1][0] == "warning"
        assert ends[1][1].startswith(f"shard 2: raised ValueError: no such product: {CARDS[0]} after ")

    def test_a_log_that_fails_does_not_stop_the_supervision(self, tmp_path):
        def broken(message: str, level: str) -> None:
            raise OSError("the event stream is gone")

        (outcome,) = supervise_workers([payload(tmp_path, "1")], targets.hand_back, log=broken)
        assert outcome.status == "finished"


class TestWhereTheWorkerRuns:
    def test_the_worker_runs_in_its_working_directory_with_its_environment(self, tmp_path, monkeypatch):
        checkout = tmp_path / "shop checkout"
        (checkout / "data").mkdir(parents=True)
        (checkout / "data" / "coupons.txt").write_text("spring10", encoding="utf-8")
        monkeypatch.delenv("SHOP_REGION", raising=False)
        (outcome,) = supervise_workers(
            [payload(tmp_path, "1", cwd=str(checkout), env={"SHOP_REGION": "eu"})], targets.where_am_i,
        )
        assert outcome.status == "finished", outcome.error
        assert Path(outcome.result["cwd"]).resolve() == checkout.resolve()
        assert outcome.result["region"] == "eu" and outcome.result["coupons"] == "spring10"
        child_cwd, child_region = outcome.result["child"][0], outcome.result["child"][-1]
        assert child_region == "eu", "what the worker starts inherits its environment"
        assert Path(child_cwd).resolve() == checkout.resolve()
        assert "SHOP_REGION" not in os.environ, "the supervising process is untouched"


class TestRefusals:
    def test_a_payload_that_cannot_be_pickled_is_refused_before_anything_starts(self, tmp_path):
        record = Recorder()
        payloads = [payload(tmp_path, "1"), payload(tmp_path, "2", data={"lock": threading.Lock()})]
        with pytest.raises(TypeError, match=r"the payload of shard '2' cannot be sent to a worker process"):
            supervise_workers(payloads, targets.hand_back, log=record.log)
        assert record.lines == [] and not (tmp_path / "1").exists()

    def test_a_target_that_is_not_module_level_is_refused(self, tmp_path):
        with pytest.raises(TypeError, match="must be a module-level callable"):
            supervise_workers([payload(tmp_path, "1")], lambda work, stop: None)


class TestReaping:
    def test_every_worker_is_reaped_by_the_supervisor(self, tmp_path):
        """No worker is left behind, running or as a zombie — finished,
        raised, died or timed out."""
        outcomes = supervise_workers(
            [payload(tmp_path, str(n)) for n in range(1, 4)], targets.hand_back, max_workers=3,
        )
        for outcome in outcomes:
            assert process_state(outcome.result["pid"]) is None
        supervise_workers([payload(tmp_path, "dies")], targets.dies)
        supervise_workers([payload(tmp_path, "slow")], targets.stops_when_asked, timeout_s=1, grace_s=30)
        pid = int((tmp_path / "slow" / "running").read_text(encoding="utf-8"))
        assert process_state(pid) is None

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
    def test_a_process_that_ended_but_was_not_reaped_does_not_count_as_running(self):
        """A zombie still answers kill(pid, 0). Under a parent that never
        collects it — a container whose process 1 is not an init — it stays
        a zombie, and a liveness check must not take it for a running
        process (an owner of a dispatch, a command that was stopped)."""
        from runcomposer.app.liveness import pid_alive

        child = subprocess.Popen([sys.executable, "-c", "pass"])
        wait_until(lambda: process_state(child.pid) == "Z", what="the child to have ended, unreaped")
        try:
            assert not pid_alive(child.pid)
        finally:
            child.wait()


# -- a runner composed from it ------------------------------------------------------------------

CHECKS = "tests.shop_check_runner:CheckRunner"


class TestAComposedRunner:
    def test_it_imports_nothing_of_runcomposer_but_the_sdk(self):
        import ast

        source = Path(__file__).with_name("shop_check_runner.py").read_text(encoding="utf-8")
        imported = {
            node.module for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("runcomposer")
        }
        assert imported == {"runcomposer.sdk"}

    def test_it_passes_the_conformance_check(self):
        ctx = check_runner(
            CheckRunner("tests.shop_check_runner:card_check"), FakeDispatchContext(CARDS), expect="complete",
        )
        assert ctx.declared == ("1", "2") and ctx.summary() == {"PASS": 5, "FAIL": 1}
        assert sum("started" in message for _level, message in ctx.logs) == 2
        assert sum("finished after" in message for _level, message in ctx.logs) == 2

    def test_it_runs_in_a_real_deployment(self):
        with ConnectorHarness(
            runners={"checks": {"module": CHECKS, "check": "tests.shop_check_runner:card_check"}}
        ) as harness:
            result = harness.run("Payments-Cards", runner="checks")
            assert (result.state, result.completion) == ("COMPLETE", "FAIL")
            assert result.summary == {"PASS": 5, "FAIL": 1}
            failed = [v for v in result.verdicts if v.status == "FAIL"]
            assert [(v.item_id, v.message) for v in failed] == [(DECLINED, "the payment provider declined the card")]
            assert sum("finished after" in line for line in result.logs()) == 2

    def test_it_is_cancelled_while_a_check_hangs_and_keeps_what_was_delivered(self, tmp_path):
        with ConnectorHarness(
            runners={"checks": {"module": CHECKS, "check": "tests.shop_check_runner:card_check_that_hangs"}},
            env={"SHOP_CHECKS_DIR": str(tmp_path)},
        ) as harness:
            handle = harness.start("Payments-Cards", runner="checks")
            mid = handle.wait_until(
                lambda run: (tmp_path / "hanging").exists() and len({v.shard for v in run.verdicts}) == 1,
                timeout=60,
            )
            hanging_pid = int((tmp_path / "hanging").read_text(encoding="utf-8"))
            delivered = {v.item_id for v in mid.verdicts}
            assert "Shop.Payments.Cards.T004" not in delivered
            handle.cancel()
            done = handle.wait(timeout=60)
            assert done.state == "CANCELLED"
            assert {v.item_id for v in done.verdicts} == delivered, "what was delivered is kept"
            wait_until(lambda: process_gone(hanging_pid), timeout=10, what="the hanging worker to be gone")
