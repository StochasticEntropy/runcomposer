"""Live status once a dispatch is over (DESIGN.md §4, "Live status" and
"What a MISSING verdict last saw").

``RUNNING`` is streamed while an item executes. When the dispatch ends
without that item's shard delivering — the process executing it was killed
and recovery marked it ``STALE``, the runner failed, it was cancelled —
nothing executes any more, so no item is listed as ``RUNNING``:
``status=RUNNING`` selects what is executing now. What was last seen stays
readable, as the ``last_live`` of the item's ``MISSING`` row, and in the
store.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from runcomposer.cli import main
from runcomposer.sdk import DispatchContext, RunnerBase, RunnerInfo, Service, load_config

from .conftest import compose, wait_until, write_config

API = "/api/v1"
ROOT = Path(__file__).resolve().parents[1]
SSO = ["Shop.Auth.SSO.T001", "Shop.Auth.SSO.T002", "Shop.Auth.SSO.T003"]


class StreamingRunner(RunnerBase):
    """Declares one shard carrying every item, streams the first item as
    PASS and the second as RUNNING, touches ``ready`` — then raises
    (``end: raise``) or waits until it is cancelled or killed."""

    def __init__(self, end: str = "wait", ready: str = ""):
        self.end = end
        self.ready = ready

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="streaming")

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare({"eu-1": list(ctx.item_ids)})
        ctx.live("eu-1", ctx.item_ids[0], "PASS", duration_ms=310)
        ctx.live("eu-1", ctx.item_ids[1], "RUNNING")
        if self.ready:
            Path(self.ready).touch()
        if self.end == "raise":
            raise RuntimeError("the worker pool collapsed")
        while not ctx.cancelled:
            time.sleep(0.01)
        ctx.raise_if_cancelled()


def runners(tmp_path: Path, end: str = "wait") -> dict[str, Any]:
    return {"streaming": {"module": "tests.test_live_rows_after_dispatch:StreamingRunner",
                          "end": end, "ready": str(tmp_path / "streamed")}}


def assert_nothing_is_running_and_last_live_is_kept(client: Any, service: Service, run_id: str) -> None:
    running = client.get(f"{API}/runs/{run_id}/verdicts", params={"status": "RUNNING"}).json()
    assert running["total"] == 0 and running["verdicts"] == []
    detail = client.get(f"{API}/runs/{run_id}").json()
    assert [v for v in detail["verdicts"] if v["status"] == "RUNNING"] == []
    assert all("RUNNING" not in counts for counts in detail["live_summary"].values())
    missing = {v["item_id"]: v for v in detail["verdicts"] if v["status"] == "MISSING"}
    assert set(missing) == set(SSO)
    assert missing[SSO[1]]["last_live"] == {"status": "RUNNING", "duration_ms": 0, "message": "",
                                            "shard": "eu-1"}
    assert missing[SSO[0]]["last_live"] == {"status": "PASS", "duration_ms": 310, "message": "",
                                            "shard": "eu-1"}
    assert missing[SSO[2]]["last_live"] is None, "never started"
    # The store keeps what was streamed; only its reading changed.
    dispatch_id = detail["dispatch_id"]
    assert {(s.item_id, s.status) for s in service.store.live_verdicts(run_id, dispatch_id)} == {
        (SSO[0], "PASS"), (SSO[1], "RUNNING")}


_CHILD = """
import sys
from runcomposer.sdk import Service, load_config

service = Service(load_config(sys.argv[1]))
service.dispatch(sys.argv[2], "streaming", foreground=True)
"""


def test_stale_after_a_killed_process_was_recovered(make_client, tmp_path):
    """The process executing the dispatch is killed (SIGKILL) mid-item; a
    server starting on the store marks the dispatch STALE."""
    config = write_config(tmp_path, runners=runners(tmp_path))
    with Service(load_config(str(config))) as before:
        run_id = compose(before, "Auth-SSO", runner_id="streaming")
    child = subprocess.Popen([sys.executable, "-c", _CHILD, str(config), run_id], cwd=ROOT)
    try:
        wait_until(lambda: (tmp_path / "streamed").exists() or child.poll() is not None,
                   what="the child's live status", timeout=60)
        assert child.poll() is None, f"the child ended early ({child.returncode})"
    finally:
        child.kill()
        child.wait(60)

    client, service = make_client(runners=runners(tmp_path))

    assert service.require_run(run_id).state == "STALE"
    assert_nothing_is_running_and_last_live_is_kept(client, service, run_id)


def test_failed(make_client, tmp_path, capsys):
    client, service = make_client(runners=runners(tmp_path, end="raise"))
    run_id = compose(service, "Auth-SSO", runner_id="streaming")
    service.dispatch(run_id, "streaming", foreground=True)
    assert service.require_run(run_id).state == "FAILED"
    assert_nothing_is_running_and_last_live_is_kept(client, service, run_id)
    # `show` still counts both items something was streamed for.
    capsys.readouterr()
    assert main(["show", run_id, "--config", str(tmp_path / "config.yaml")]) == 0
    out = capsys.readouterr().out
    assert "last live status of shards that never delivered: 2 item(s)" in out
    assert "running now" not in out


def test_cancelled(make_client, tmp_path):
    client, service = make_client(runners=runners(tmp_path))
    run_id = compose(service, "Auth-SSO", runner_id="streaming")
    service.dispatch(run_id, "streaming")
    wait_until(lambda: (tmp_path / "streamed").exists(), what="live status", timeout=60)
    # While it executes, the running item is what status=RUNNING selects.
    executing = client.get(f"{API}/runs/{run_id}/verdicts", params={"status": "RUNNING"}).json()
    assert [v["item_id"] for v in executing["verdicts"]] == [SSO[1]]
    service.cancel(run_id)
    assert service.wait(run_id, timeout=30).state == "CANCELLED"
    assert_nothing_is_running_and_last_live_is_kept(client, service, run_id)
