"""Restart recovery (DESIGN.md §4): what a dead process left executing.

A dispatch names the process executing it. When that process is gone the
dispatch can never finish by itself, so on startup it is marked ``STALE`` —
with an event saying why, its partial results kept, and still completable by
results that arrive late.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from runcomposer.app.liveness import owner_alive, parse_owner, pid_alive
from runcomposer.sdk import Service, load_config

from .conftest import compose, wait_until, write_config

SSO = ["Shop.Auth.SSO.T001", "Shop.Auth.SSO.T002", "Shop.Auth.SSO.T003"]


def dead_pid() -> int:
    """The pid of a process that has just ended."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=60)
    assert not pid_alive(process.pid)
    return process.pid


def owner(pid: int, host: str | None = None) -> str:
    return f"{host or socket.gethostname()}:{pid}:01TOKENOFTHATPROCESS000000"


def open_dispatch(service: Service, run_id: str, *, owned_by: str, state: str = "RUNNING"):
    """A dispatch as another process would have left it."""
    dispatch = service.ledger.open(run_id, "demo", owner=owned_by)
    if state != "DISPATCHED":
        service.ledger.declare(
            dispatch.dispatch_id, shards=["1"], shard_items={}, plan="", links={}
        )
    if state == "AWAITING_RESULTS":
        service.ledger.move(dispatch.dispatch_id, "AWAITING_RESULTS")
        service.store.update_dispatch(dispatch.dispatch_id, owner=owned_by)
    return service.store.get_dispatch(dispatch.dispatch_id)


def results_bundle(directory: Path, item_ids: list[str], status: str = "PASS") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "results.json").write_text(
        json.dumps(
            {
                "format": "runcomposer-verdicts",
                "verdicts": [{"name": i, "status": status, "duration_ms": 40} for i in item_ids],
            }
        ),
        encoding="utf-8",
    )
    return directory


class TestOwnerIdentity:
    def test_an_owner_identity_names_host_and_pid(self, service):
        host, pid = parse_owner(service.dispatcher.owner)
        import os

        assert host == socket.gethostname() and pid == os.getpid()
        assert owner_alive(service.dispatcher.owner)

    def test_two_services_in_one_process_are_two_owners(self, service, tmp_path):
        with Service(load_config(str(tmp_path / "config.yaml"))) as other:
            assert other.dispatcher.owner != service.dispatcher.owner

    def test_liveness(self, service):
        me = service.dispatcher.owner
        assert not owner_alive(owner(dead_pid()), me)
        assert not pid_alive(0) and not pid_alive(-5)
        # Another live process on this host owns what it is executing.
        other = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                 stdin=subprocess.PIPE)
        try:
            assert owner_alive(owner(other.pid), me)
        finally:
            other.communicate(b"")
        # One instance owns a store: an owner on another host is a replaced
        # container, not somebody to wait for.
        assert not owner_alive(owner(1, host="another-host.shop.example"), me)
        # This very process id under another token is an earlier life of it —
        # process id 1 of a restarted container — unless it is a dispatch
        # manager living in this process right now.
        import os

        assert not owner_alive(owner(os.getpid()), me)
        assert owner_alive(me, me) and owner_alive(me)


class TestRecover:
    @pytest.mark.parametrize("state", ["DISPATCHED", "RUNNING"])
    def test_a_dispatch_of_a_dead_process_becomes_stale(self, service, state):
        run_id = compose(service, "Auth-SSO")
        gone = owner(dead_pid())
        dispatch = open_dispatch(service, run_id, owned_by=gone, state=state)
        assert service.require_run(run_id).state == state

        stale = service.recover()

        assert [d.dispatch_id for d in stale] == [dispatch.dispatch_id]
        run = service.require_run(run_id)
        assert run.state == "STALE" and run.completion is None
        recovered = run.latest_dispatch
        assert recovered.owner is None
        assert f"interrupted while {state}" in recovered.error and gone in recovered.error
        event = service.store.events(run_id)[-1]
        assert event.kind == "run.stale"
        assert event.data["was"] == state and event.data["owner"] == gone
        assert "is gone" in event.data["error"]
        # A second pass finds nothing left to do.
        assert service.recover() == []

    def test_a_dispatch_owned_by_a_live_process_is_left_alone(self, service):
        run_id = compose(service, "Auth-SSO")
        alive = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
        )
        try:
            dispatch = open_dispatch(service, run_id, owned_by=owner(alive.pid))
            assert service.recover() == []
            after = service.store.get_dispatch(dispatch.dispatch_id)
            assert after.state == "RUNNING" and after.owner == owner(alive.pid)
        finally:
            alive.communicate(b"", timeout=60)
        # Now that process is gone too.
        assert [d.dispatch_id for d in service.recover()] == [dispatch.dispatch_id]

    def test_a_dispatch_of_this_process_or_of_another_live_one_is_left_alone(
        self, service, tmp_path
    ):
        mine, sibling, theirs = (compose(service, "Auth-SSO") for _ in range(3))
        open_dispatch(service, mine, owned_by=service.dispatcher.owner)
        other = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                 stdin=subprocess.PIPE)
        try:
            open_dispatch(service, theirs, owned_by=owner(other.pid))
            with Service(load_config(str(tmp_path / "config.yaml"))) as second:
                open_dispatch(service, sibling, owned_by=second.dispatcher.owner)
                assert service.recover() == []
        finally:
            other.communicate(b"")
        assert service.require_run(mine).state == "RUNNING"
        assert service.require_run(theirs).state == "RUNNING"

    def test_a_restarted_container_does_not_leave_its_run_running_forever(self, service):
        """The process that owned the dispatch was process id N on this host
        — and so is this one (pid 1 before and after a container restart) —
        or it ran under another hostname (the container was replaced). Either
        way nobody is executing it any more."""
        import os

        same_pid, other_host = compose(service, "Auth-SSO"), compose(service, "Auth-SSO")
        open_dispatch(service, same_pid, owned_by=owner(os.getpid()))
        open_dispatch(service, other_host, owned_by=owner(1, host="3f9a1c2b7d10"))
        stale = service.recover()
        assert {d.run_id for d in stale} == {same_pid, other_host}
        for run_id in (same_pid, other_host):
            run = service.require_run(run_id)
            assert run.state == "STALE" and "is gone" in run.latest_dispatch.error
            service.delete_run(run_id)  # …and it is not wedged: it can be removed

    def test_force_is_the_way_out_of_a_dispatch_whose_owner_cannot_be_reached(self, service):
        """A process id that is alive but is not the runcomposer that owned
        the dispatch (the id was reused): a normal cancel can only leave a
        request nobody will honour; `force` closes the dispatch."""
        from runcomposer.sdk import RunActive

        run_id = compose(service, "Auth-SSO")
        impostor = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                    stdin=subprocess.PIPE)
        try:
            dispatch = open_dispatch(service, run_id, owned_by=owner(impostor.pid))
            assert service.recover() == []
            requested = service.cancel(run_id)
            assert requested.state == "RUNNING" and requested.cancel_requested
            with pytest.raises(RunActive):
                service.delete_run(run_id)
            forced = service.cancel(run_id, force=True)
        finally:
            impostor.communicate(b"")
        assert forced.state == "CANCELLED" and forced.dispatch_id == dispatch.dispatch_id
        assert service.require_run(run_id).state == "CANCELLED"
        service.dispatch(run_id, "demo", foreground=True)  # usable again
        assert service.require_run(run_id).state == "COMPLETE"

    def test_a_dispatch_awaiting_results_only_loses_its_owner_mark(self, service):
        run_id = compose(service, "Auth-SSO")
        dispatch = open_dispatch(
            service, run_id, owned_by=owner(dead_pid()), state="AWAITING_RESULTS"
        )
        assert service.store.get_dispatch(dispatch.dispatch_id).owner is not None
        assert service.recover() == []
        after = service.store.get_dispatch(dispatch.dispatch_id)
        assert after.state == "AWAITING_RESULTS" and after.owner is None
        assert "run.stale" not in [event.kind for event in service.store.events(run_id)]

    def test_a_late_delivery_can_still_complete_a_stale_dispatch(self, service, tmp_path):
        run_id = compose(service, "Auth-SSO")
        open_dispatch(service, run_id, owned_by=owner(dead_pid()))
        service.recover()
        assert service.require_run(run_id).state == "STALE"

        report = service.ingest(results_bundle(tmp_path / "late", SSO), run_id=run_id)
        assert (report.run_state, report.completion) == ("COMPLETE", "PASS")
        dispatch = service.require_run(run_id).latest_dispatch
        assert dispatch.error == "", "the interruption note does not outlive the completion"

    def test_a_stale_run_can_be_finalized_or_dispatched_again(self, service, tmp_path):
        first, second = compose(service, "Auth-SSO"), compose(service, "Auth-SSO")
        for run_id in (first, second):
            open_dispatch(service, run_id, owned_by=owner(dead_pid()))
        service.recover()
        finalized = service.finalize(first)
        assert (finalized.state, finalized.completion) == ("COMPLETE", "INCOMPLETE")
        service.dispatch(second, "demo", {"seed": "a"}, foreground=True)
        again = service.require_run(second)
        assert [d.state for d in again.dispatches] == ["STALE", "COMPLETE"]
        assert again.completion == "PASS"


_CHILD = """
import sys
from runcomposer.sdk import Service, load_config

service = Service(load_config(sys.argv[1]))
service.dispatch(sys.argv[2], "demo", foreground=True)
"""


def test_a_killed_process_leaves_a_run_that_recovery_marks_stale(tmp_path):
    """The real thing: a process dispatching in the foreground is killed
    (SIGKILL — no chance to clean up) in the middle of a run."""
    config_path = write_config(tmp_path, runners={"demo": {"delay_s": 0.2}})
    with Service(load_config(str(config_path))) as service:
        run_id = compose(service, "Regression")  # 46 items × 0.2 s: far from finished
        child = subprocess.Popen([sys.executable, "-c", _CHILD, str(config_path), run_id])
        try:
            dispatch = wait_until(
                lambda: service.require_run(run_id).latest_dispatch, what="the child's dispatch"
            )
            # Mid-run: declared, and at least one item streamed its status.
            wait_until(
                lambda: service.store.live_verdicts(run_id, dispatch.dispatch_id),
                what="live status from the child process",
                timeout=60,
            )
            running = service.require_run(run_id)
            assert running.state == "RUNNING"
            assert parse_owner(running.latest_dispatch.owner)[1] == child.pid
            # While the owner lives, recovery leaves the run alone.
            assert service.recover() == []
        finally:
            child.kill()
            child.wait(timeout=60)

        stale = service.recover()

        assert [d.run_id for d in stale] == [run_id]
        run = service.require_run(run_id)
        assert run.state == "STALE"
        assert str(child.pid) in run.latest_dispatch.error
        assert run.latest_dispatch.summary == {"MISSING": 46}
        events = service.store.events(run_id)
        assert events[-1].kind == "run.stale" and events[-1].data["was"] == "RUNNING"
        # What had been streamed before the kill is still readable.
        assert service.store.live_verdicts(run_id, dispatch.dispatch_id)


def test_the_server_recovers_at_startup(make_client, tmp_path):
    config_path = write_config(tmp_path)
    with Service(load_config(str(config_path))) as before:
        run_id = compose(before, "Auth-SSO")
        open_dispatch(before, run_id, owned_by=owner(dead_pid()))
        assert before.require_run(run_id).state == "RUNNING"

    client, _service = make_client()
    detail = client.get(f"/api/v1/runs/{run_id}").json()
    assert detail["state"] == "STALE"
    assert detail["events"][-1]["kind"] == "run.stale"
    assert "is gone" in detail["dispatches"][-1]["error"]
    assert detail["dispatches"][-1]["executing"] is False
