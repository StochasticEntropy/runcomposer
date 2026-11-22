"""The dispatch queue is bounded (DESIGN.md §4): behind
``core.dispatch.max_concurrent`` executing dispatches, at most
``core.dispatch.max_queued`` wait for a slot; one more is refused with
``503 dispatch-queue-full`` before anything is recorded."""

from __future__ import annotations

import threading

import pytest

from runcomposer.core.errors import DispatchQueueFull
from runcomposer.sdk import ConfigError, DispatchContext, RunnerBase, RunnerInfo, Verdict, load_config

from .conftest import compose, wait_until, write_config

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


def test_with_max_queued_0_a_free_slot_still_runs_and_nothing_waits(make_client):
    RELEASE.clear()
    client, service = make_client(
        runners={"holding": {"module": f"{HERE}:Holding"}},
        core={"dispatch": {"max_concurrent": 1, "max_queued": 0}},
    )
    body = {"selection": {"tag_filter": "Auth-SSO"}, "dispatch": {"runner": "holding"}}
    try:
        first = client.post(f"{API}/runs", json=body)
        assert first.status_code == 201, first.text  # the slot is free
        second = client.post(f"{API}/runs", json=body)
        assert second.status_code == 503, second.text  # it would have to wait
        assert "max_queued: 0" in second.json()["detail"]
    finally:
        RELEASE.set()
    assert service.wait(first.json()["run"]["id"], timeout=30).state == "COMPLETE"


@pytest.mark.parametrize("max_queued", [0, 2])
def test_a_burst_fills_every_slot_and_the_queue_then_one_more_is_refused(make_client, max_queued):
    """Dispatches started back to back, before any worker thread picked one
    up: a dispatch just handed to the pool takes a free slot — it is not
    counted as waiting."""
    RELEASE.clear()
    _client, service = make_client(
        runners={"holding": {"module": f"{HERE}:Holding"}},
        core={"dispatch": {"max_concurrent": 3, "max_queued": max_queued}},
    )
    runs = [compose(service, "Auth-SSO") for _ in range(3 + max_queued + 1)]
    started = []
    try:
        for run_id in runs[:-1]:
            started.append(service.dispatcher.start(run_id, "holding"))
        with pytest.raises(DispatchQueueFull, match=f"max_queued: {max_queued}"):
            service.dispatcher.start(runs[-1], "holding")
        assert service.require_run(runs[-1]).latest_dispatch is None, "the refused one recorded nothing"
    finally:
        RELEASE.set()
    for run_id in runs[:-1]:
        assert service.wait(run_id, timeout=30).state == "COMPLETE"


@pytest.mark.parametrize("value", [-1, 1.5, True, "10"])
def test_the_bound_is_a_count(tmp_path, value):
    with pytest.raises(ConfigError, match=r"core\.dispatch\.max_queued must be an integer ≥ 0"):
        load_config(str(write_config(tmp_path, core={"dispatch": {"max_queued": value}})))
