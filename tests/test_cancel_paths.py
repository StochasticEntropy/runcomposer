"""However a dispatch comes to be cancelled, its runner's ``cancel(ctx)`` is
called — exactly once, and before the dispatch is closed as ``CANCELLED``
(DESIGN.md §4, §6.2).

A runner does two things about a cancellation: it checks ``ctx.cancelled``
between units of work, and it stops what it started in ``cancel(ctx)``. The
second must never depend on who noticed the request first. There is one way
a dispatch learns of a cancel — the dispatch manager signals it — and that
way always includes the call.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from runcomposer.sdk import (
    DispatchCancelled,
    DispatchContext,
    RunnerBase,
    RunnerInfo,
    Service,
    Verdict,
    load_config,
)

from .conftest import compose, wait_until

HERE = "tests.test_cancel_paths"

#: What the runners of this module saw, by dispatch id.
STARTED: dict[str, float] = {}
CANCELS: dict[str, list[str]] = {}
SERVICES: dict[str, Service] = {}


class PollingRunner(RunnerBase):
    """Checks the flag *and* stops its executor in ``cancel`` — as the guide
    tells a runner to. ``phase_s`` shifts its first look at the flag, the way
    real work between two checks would."""

    def __init__(self, phase_s: float = 0.0, mode: str = "poll"):
        self.phase_s, self.mode = float(phase_s), mode

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="polling", capabilities=("cancel",))

    def dispatch(self, ctx: DispatchContext) -> None:
        STARTED[ctx.dispatch_id] = time.monotonic()
        ctx.declare(["1"])
        if self.mode == "gives-up":
            raise DispatchCancelled("the executor went away")
        if self.mode == "hands-off":
            return  # an executor elsewhere will push the results
        time.sleep(self.phase_s)
        deadline = time.monotonic() + 30
        while not ctx.cancelled and time.monotonic() < deadline:
            time.sleep(0.002)
        ctx.raise_if_cancelled()
        ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

    def cancel(self, ctx: DispatchContext) -> None:
        # What the store says the dispatch is at the moment cancel() runs.
        service = SERVICES.get(ctx.run_id)
        state = service.store.get_dispatch(ctx.dispatch_id).state if service else "?"
        CANCELS.setdefault(ctx.dispatch_id, []).append(state)


@pytest.fixture()
def polling(make_service):
    def factory(**options: Any) -> Service:
        return make_service(runners={"polling": {"module": f"{HERE}:PollingRunner", **options}})

    return factory


def start(service: Service) -> tuple[str, Any]:
    run_id = compose(service, "Auth-SSO", runner_id="polling")
    SERVICES[run_id] = service
    outcome = service.dispatch(run_id, "polling")
    wait_until(
        lambda: outcome.dispatch_id in STARTED and service.require_run(run_id).state == "RUNNING",
        what="the runner to have handed off",
    )
    return run_id, outcome


class TestACancelFromAnotherProcess:
    @pytest.mark.parametrize("phase_s", [0.0, 0.12, 0.25, 0.37])
    @pytest.mark.parametrize("after_s", [0.55, 0.65])
    def test_it_reaches_cancel_whoever_is_looking_at_the_flag_at_that_moment(self, polling, phase_s, after_s):
        """The request is a flag another process wrote into the store. The
        runner is checking ``ctx.cancelled`` all the time, out of step with
        the manager's own look at the store by ``phase_s`` — every phase ends
        the same way: cancel(ctx) once, while the dispatch is still RUNNING."""
        service = polling(phase_s=phase_s)
        run_id, outcome = start(service)
        with Service(load_config(str(service.config.path))) as other:   # stands for the other process
            delay = STARTED[outcome.dispatch_id] + after_s - time.monotonic()
            time.sleep(max(0.0, delay))
            other.cancel(run_id)
        assert outcome.done.wait(30)
        assert service.require_run(run_id).state == "CANCELLED"
        assert CANCELS.get(outcome.dispatch_id) == ["RUNNING"], (
            "cancel(ctx) is called exactly once, before the dispatch is closed"
        )

    def test_runcomposer_cancel_in_a_real_second_process(self, polling):
        service = polling(phase_s=0.25)
        run_id, outcome = start(service)
        done = subprocess.run(
            [sys.executable, "-m", "runcomposer.cli", "cancel", run_id, "--config", str(service.config.path)],
            capture_output=True, text=True, timeout=60,
        )
        assert done.returncode == 0, done.stderr
        assert outcome.done.wait(30)
        assert service.require_run(run_id).state == "CANCELLED"
        assert CANCELS.get(outcome.dispatch_id) == ["RUNNING"]


class TestEveryOtherWayToEndCancelled:
    def test_a_cancel_made_in_this_process_is_one_call_too(self, polling):
        service = polling()
        run_id, outcome = start(service)
        service.cancel(run_id)
        assert outcome.done.wait(30)
        time.sleep(0.6)   # longer than the manager's look at the store: it finds nothing left to signal
        assert CANCELS.get(outcome.dispatch_id) == ["RUNNING"]

    def test_a_runner_that_gives_up_as_cancelled_by_itself_has_its_cancel_called(self, polling):
        service = polling(mode="gives-up")
        run_id = compose(service, "Auth-SSO", runner_id="polling")
        SERVICES[run_id] = service
        outcome = service.dispatch(run_id, "polling", foreground=True)
        assert service.require_run(run_id).state == "CANCELLED"
        assert CANCELS.get(outcome.dispatch_id) == ["RUNNING"]

    def test_a_dispatch_that_was_never_cancelled_never_has_cancel_called(self, make_service):
        release = threading.Event()

        class Gated(PollingRunner):
            def dispatch(self, ctx: DispatchContext) -> None:
                STARTED[ctx.dispatch_id] = time.monotonic()
                ctx.declare(["1"])
                assert release.wait(30)
                ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        globals()["Gated"] = Gated
        service = make_service(runners={"polling": {"module": f"{HERE}:Gated"}})
        run_id, outcome = start(service)
        time.sleep(0.6)   # the manager looked at the store at least twice
        release.set()
        assert outcome.done.wait(30)
        assert service.require_run(run_id).state == "COMPLETE" and outcome.dispatch_id not in CANCELS

    def test_the_context_itself_never_reads_the_request_from_the_store(self, polling):
        """The flag has one writer. If a context could find the request by
        itself, a runner could come back cancelled before anyone called its
        ``cancel`` — the store says "requested", the context says "not yet"
        until the manager has signalled it."""
        seen: list[bool] = []

        class Watching(PollingRunner):
            def dispatch(self, ctx: DispatchContext) -> None:
                STARTED[ctx.dispatch_id] = time.monotonic()
                ctx.declare(["1"])
                service = SERVICES[ctx.run_id]
                service.store.update_dispatch(ctx.dispatch_id, cancel_requested=True)
                seen.append(ctx.cancelled)                      # asked at once: not signalled yet
                deadline = time.monotonic() + 30
                while not ctx.cancelled and time.monotonic() < deadline:
                    time.sleep(0.002)
                seen.append(bool(CANCELS.get(ctx.dispatch_id)))  # by the time the flag is up, cancel is under way
                ctx.raise_if_cancelled()

        globals()["Watching"] = Watching
        service = polling()
        service._runners["polling"] = Watching()
        run_id = compose(service, "Auth-SSO", runner_id="polling")
        SERVICES[run_id] = service
        outcome = service.dispatch(run_id, "polling")
        assert outcome.done.wait(30)
        assert seen[0] is False
        assert service.require_run(run_id).state == "CANCELLED"
        assert CANCELS.get(outcome.dispatch_id) == ["RUNNING"]


class TestACancelWhileTheDispatchSettles:
    """The runner has handed off and returned; the manager decides how
    the dispatch ends (here: it will wait for results). A cancel that comes
    in that moment still finds the dispatch executing here — and must not be
    left with a dispatch that stays in flight, its executor stopped."""

    def settling(self, polling, monkeypatch, then: Any) -> tuple[Service, str, Any]:
        """A dispatch of the hands-off runner; ``then(service, run_id)`` is
        called once, in the manager's thread, after it has decided that the
        dispatch was not cancelled and before it is done with it."""
        service = polling(mode="hands-off")
        run_id = compose(service, "Auth-SSO", runner_id="polling")
        SERVICES[run_id] = service
        recompute = service.ledger.recompute
        called: list[str] = []

        def recompute_then_cancel(dispatch_id: str, **options: Any) -> Any:
            if not called:
                called.append(dispatch_id)
                then(service, run_id)
            return recompute(dispatch_id, **options)

        monkeypatch.setattr(service.ledger, "recompute", recompute_then_cancel)
        outcome = service.dispatch(run_id, "polling")
        assert outcome.done.wait(30) and called == [outcome.dispatch_id]
        return service, run_id, outcome

    def test_a_cancel_made_in_this_process_closes_it(self, polling, monkeypatch):
        answers: list[str] = []
        service, run_id, outcome = self.settling(
            polling, monkeypatch, lambda service, run_id: answers.append(service.cancel(run_id).state)
        )
        dispatch = service.require_run(run_id).latest_dispatch
        assert (dispatch.state, dispatch.error_code) == ("CANCELLED", "cancelled")
        assert answers == ["RUNNING"], "the request found the dispatch still executing"
        assert CANCELS.get(outcome.dispatch_id) == ["RUNNING"], "cancel(ctx) once, before it was closed"
        assert outcome.dispatch_id not in service.dispatcher.active_dispatch_ids()

    def test_a_request_another_process_wrote_closes_it(self, polling, monkeypatch):
        """Only the flag in the store — nobody in this process was told."""

        def write_the_flag(service: Service, run_id: str) -> None:
            dispatch = service.require_run(run_id).latest_dispatch
            service.store.update_dispatch(dispatch.dispatch_id, cancel_requested=True)

        service, run_id, outcome = self.settling(polling, monkeypatch, write_the_flag)
        assert service.require_run(run_id).state == "CANCELLED"
        assert len(CANCELS.get(outcome.dispatch_id, [])) == 1, "cancel(ctx) exactly once"
        assert outcome.dispatch_id not in service.dispatcher.active_dispatch_ids()

    def test_a_cancel_after_it_is_settled_closes_it_itself(self, polling):
        """The other side of the same moment: the dispatch is no longer
        listed as executing, so the cancel closes it — and calls cancel(ctx)
        once."""
        service = polling(mode="hands-off")
        run_id = compose(service, "Auth-SSO", runner_id="polling")
        SERVICES[run_id] = service
        outcome = service.dispatch(run_id, "polling")
        assert outcome.done.wait(30)
        assert service.require_run(run_id).state == "AWAITING_RESULTS"
        assert service.cancel(run_id).state == "CANCELLED"
        assert CANCELS.get(outcome.dispatch_id) == ["AWAITING_RESULTS"]

    def test_a_dispatch_nobody_cancelled_waits_for_its_results(self, polling, monkeypatch):
        service, run_id, outcome = self.settling(polling, monkeypatch, lambda service, run_id: None)
        assert service.require_run(run_id).state == "AWAITING_RESULTS"
        assert outcome.dispatch_id not in CANCELS
        assert outcome.dispatch_id not in service.dispatcher.active_dispatch_ids()
