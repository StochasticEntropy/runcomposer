"""The worker live channel is transport, not a record (DESIGN.md §8): what
workers write to it is recorded — blanked — through the one live write path
as it is read, and the files are gone once the dispatch's context has
closed, whichever way the dispatch ended; a server killed mid-run leaves
them to restart recovery, which removes them. Nothing of it stays anywhere
under the state directory, and no line written before a worker ended is
lost by the removal."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from runcomposer.app.redaction import REDACTED
from runcomposer.sdk import DispatchContext, RunnerBase, RunnerInfo, Verdict, WorkerPayload, supervise_workers

from .conftest import compose, wait_until
from .test_recovery import dead_pid, open_dispatch, owner

SECRET = "k-shop-gateway-7f3e9a"
HERE = "tests.test_live_transport"
LINES = 40


def chatty_worker(payload: WorkerPayload, stop: Any) -> list[str]:
    """Reports every item, quoting the token, then ends at once: its last
    lines are written just before the process exits."""
    channel = payload.data["channel"]
    for index in range(LINES):
        item = payload.item_ids[index % len(payload.item_ids)]
        channel.live(payload.shard, item, "RUNNING", message=f"step {index} with {payload.data['token']}")
        channel.log(f"worker line {index} with {payload.data['token']}", "info")
    return list(payload.item_ids)


def waiting_worker(payload: WorkerPayload, stop: Any) -> None:
    channel = payload.data["channel"]
    channel.live(payload.shard, payload.item_ids[0], "RUNNING", message=f"waiting with {payload.data['token']}")
    channel.log(f"waiting with {payload.data['token']}", "info")
    idle = threading.Event()
    while not stop():
        idle.wait(0.02)


class ChannelRunner(RunnerBase):
    """One supervised worker that writes through the live channel. ``mode``:
    ``complete`` delivers, ``fail`` raises after the worker, ``cancel`` waits
    for a cancel, ``await`` returns with the shard outstanding."""

    def __init__(self, token: str, mode: str = "complete"):
        self.token, self.mode = token, mode

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="channel", capabilities=("cancel",))

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["s1"])
        payload = WorkerPayload(shard="s1", out_dir=str(ctx.workdir / "s1"), item_ids=tuple(ctx.item_ids),
                                data={"channel": ctx.live_channel(), "token": self.token})
        target = waiting_worker if self.mode == "cancel" else chatty_worker
        supervise_workers([payload], target, max_workers=1, cancelled=lambda: ctx.cancelled, log=ctx.log)
        ctx.raise_if_cancelled()
        if self.mode == "fail":
            raise RuntimeError("the shop went away")
        if self.mode == "complete":
            ctx.deliver("s1", verdicts=[Verdict(item, "PASS") for item in ctx.item_ids])


def secret_files(state: Path) -> list[str]:
    """Every file under the state directory that holds the secret — the
    database aside, which other tests read through the API."""
    found = []
    for path in state.rglob("*"):
        if path.is_file() and not path.name.startswith("runcomposer.db") and SECRET.encode() in path.read_bytes():
            found.append(str(path.relative_to(state)))
    return found


def transport_left(state: Path) -> list[str]:
    return [str(path.relative_to(state)) for path in state.rglob(".live")]


@pytest.fixture()
def channel_service(make_service, monkeypatch):
    monkeypatch.setenv("SHOP_GATEWAY_KEY", SECRET)

    def build(mode: str):
        return make_service(
            runners={"channel": {"module": f"{HERE}:ChannelRunner", "token": "${env:SHOP_GATEWAY_KEY}", "mode": mode}},
        )

    return build


class TestTheChannelIsRemoved:
    @pytest.mark.parametrize(("mode", "state"), [("complete", "COMPLETE"), ("fail", "FAILED")])
    def test_after_a_dispatch_that_ends_by_itself(self, channel_service, tmp_path, mode, state):
        service = channel_service(mode)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "channel", foreground=True)
        assert service.require_run(run_id).latest_dispatch.state == state
        assert transport_left(tmp_path) == [] and secret_files(tmp_path) == []

    def test_after_a_cancel(self, channel_service, tmp_path):
        service = channel_service("cancel")
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "channel")
        wait_until(lambda: transport_left(tmp_path) and any(
            "waiting with" in event.data.get("message", "") for event in service.store.events(run_id)),
            what="the worker to report")
        service.cancel(run_id)
        assert service.wait(run_id, timeout=60).state == "CANCELLED"
        service.dispatcher.join(30)
        assert transport_left(tmp_path) == [] and secret_files(tmp_path) == []

    def test_after_a_runner_returns_while_results_are_awaited(self, channel_service, tmp_path):
        """The dispatch goes on (results come from outside, or it expires),
        but nobody reads the channel any more: it is gone with the context."""
        service = channel_service("await")
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "channel", foreground=True)
        assert service.require_run(run_id).latest_dispatch.state in ("RUNNING", "AWAITING_RESULTS")
        assert transport_left(tmp_path) == [] and secret_files(tmp_path) == []

    def test_after_a_server_killed_mid_run_restart_recovery_removes_it(self, make_service, tmp_path):
        service = make_service()
        run_id = compose(service, "Auth-SSO")
        dispatch = open_dispatch(service, run_id, owned_by=owner(dead_pid()))
        left = []
        for root in (service.config.private_dir, service.config.artifact_dir):  # artifact_dir: an older layout
            directory = root / run_id / dispatch.dispatch_id / ".live"
            directory.mkdir(parents=True)
            (directory / "live-4242.jsonl").write_text(f'{{"log": "token {SECRET}"}}\n', encoding="utf-8")
            left.append(directory)
        # And one of a dispatch settled before its process was killed.
        settled = service.config.private_dir / run_id / "01SETTLEDBEFOREKILLED00000" / ".live"
        settled.mkdir(parents=True)
        (settled / "live-4243.jsonl").write_text(f'{{"log": "token {SECRET}"}}\n', encoding="utf-8")
        assert secret_files(tmp_path)
        stale = service.recover()
        assert [d.dispatch_id for d in stale] == [dispatch.dispatch_id]
        assert transport_left(tmp_path) == [] and secret_files(tmp_path) == []


class TestEveryLineArrives:
    def test_every_line_written_before_the_worker_ended_is_recorded_blanked(self, channel_service, tmp_path):
        service = channel_service("complete")
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "channel", foreground=True)
        logged = [event.data["message"] for event in service.store.events(run_id, limit=5000)
                  if event.kind == "log" and "worker line" in event.data.get("message", "")]
        assert logged == [f"worker line {index} with {REDACTED}" for index in range(LINES)]
        assert transport_left(tmp_path) == []

    def test_a_worker_writing_after_the_context_closed_loses_the_line_quietly(self, tmp_path):
        from runcomposer.app.livechannel import FileLiveChannel

        gone = FileLiveChannel(str(tmp_path / "closed" / ".live"))
        gone.live("s1", "Shop.Auth.SSO.T001", "RUNNING", message="late")
        gone.log("late")
        assert not (tmp_path / "closed").exists()
