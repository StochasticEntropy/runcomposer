"""The dispatch queue is bounded (DESIGN.md §4): behind
``core.dispatch.max_concurrent`` executing dispatches, at most
``core.dispatch.max_queued`` wait for a slot; one more is refused with
``503 dispatch-queue-full`` before anything is recorded."""

from __future__ import annotations

import threading

import pytest

from runcomposer.sdk import ConfigError, DispatchContext, RunnerBase, RunnerInfo, Verdict, load_config

from .conftest import wait_until, write_config

API = "/api/v1"
HERE = "tests.test_dispatch_queue"
RELEASE = threading.Event()


class Holding(RunnerBase):
    """Executes until the test releases it."""

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="holding")

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        RELEASE.wait(60)
        ctx.deliver("1", verdicts=[Verdict(item, "PASS") for item in ctx.item_ids])


def test_one_more_than_the_queue_holds_is_a_503(make_client):
    RELEASE.clear()
    client, service = make_client(
        runners={"holding": {"module": f"{HERE}:Holding"}},
        core={"dispatch": {"max_concurrent": 1, "max_queued": 1}},
    )
    body = {"selection": {"tag_filter": "Auth-SSO"}, "dispatch": {"runner": "holding"}}
    try:
        first = client.post(f"{API}/runs", json=body)
        assert first.status_code == 201
        wait_until(lambda: service.require_run(first.json()["run"]["id"]).state == "RUNNING", what="the first to run")
        second = client.post(f"{API}/runs", json=body)
        assert second.status_code == 201, second.text  # waits for the slot
        third = client.post(f"{API}/runs", json=body)
        assert third.status_code == 503, third.text
        answer = third.json()
        assert answer["code"] == "dispatch-queue-full" and answer["run_kept"] is False
        assert "max_queued: 1" in answer["detail"]
        assert service.list_runs().total == 2, "the refused request left no run behind"
    finally:
        RELEASE.set()
    for run in (first.json()["run"]["id"], second.json()["run"]["id"]):
        assert service.wait(run, timeout=30).state == "COMPLETE"


@pytest.mark.parametrize("value", [-1, 1.5, True, "10"])
def test_the_bound_is_a_count(tmp_path, value):
    with pytest.raises(ConfigError, match=r"core\.dispatch\.max_queued must be an integer ≥ 0"):
        load_config(str(write_config(tmp_path, core={"dispatch": {"max_queued": value}})))
