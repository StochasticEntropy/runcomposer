"""The dispatch manager and the ``DispatchContext`` (DESIGN.md §4, §6.2),
exercised through ``Service`` with small scripted runners.

The runners here are real plugins as far as runcomposer is concerned: they
are configured under ``runners:`` with ``module: "tests.test_dispatch:…"``
and receive nothing but the context. What each one does is a function a test
registers — with ``threading.Event`` gates where a test needs to look at a
dispatch that is still in flight, so nothing here waits on the clock.
"""

from __future__ import annotations

import json
import pickle
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from runcomposer.app.views import run_detail
from runcomposer.plugins.sqlite_store import SqliteRunStore
from runcomposer.sdk import (
    Conflict,
    DispatchContext,
    Forbidden,
    IngestError,
    InvalidParameter,
    InvalidRequest,
    NotFound,
    ObserverBase,
    ParsedVerdict,
    ParserBase,
    RunActive,
    RunnerBase,
    RunnerInfo,
    RunStore,
    Service,
    StoreBase,
    UnknownRunner,
    Verdict,
    contract_members,
    load_config,
    parameters_from_config,
    sha256_hex,
)

from .conftest import compose, wait_until, write_config
from .exec_loader import runcomposer_exec

SSO = ["Shop.Auth.SSO.T001", "Shop.Auth.SSO.T002", "Shop.Auth.SSO.T003"]

#: What each configured ScriptedRunner does, by the name it was configured with.
SCRIPTS: dict[str, Callable[[DispatchContext], None]] = {}
CANCELS: dict[str, Callable[[DispatchContext], None]] = {}


class ScriptedRunner(RunnerBase):
    """A runner whose ``dispatch`` is whatever the test registered."""

    def __init__(
        self,
        script: str,
        fixed: str = "configured",
        parameters: list[dict[str, Any]] | None = None,
    ):
        self.script = script
        self.fixed = fixed
        self._parameters = parameters_from_config(parameters, "scripted: parameters")

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="scripted",
            capabilities=("cancel",),
            parameters=self._parameters,
            result_format="runcomposer-verdicts",
        )

    def dispatch(self, ctx: DispatchContext) -> None:
        SCRIPTS[self.script](ctx)

    def cancel(self, ctx: DispatchContext) -> None:
        CANCELS.get(self.script, lambda _ctx: None)(ctx)


class DelegatingStore(StoreBase):
    """A RunStore that is not the sqlite class and exposes nothing of it:
    every call goes to an inner store through ``__getattr__``. There is no
    ``_path`` here for anyone to reach for."""

    store_id = "delegating"
    PATH_OPTIONS = ("path",)

    def __init__(self, path: str):
        self.__inner = SqliteRunStore(path)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.__inner, name)


def _forward(name: str) -> Any:
    def forwarded(self: DelegatingStore, *args: Any, **kwargs: Any) -> Any:
        return getattr(self._DelegatingStore__inner, name)(*args, **kwargs)  # type: ignore[attr-defined]

    forwarded.__name__ = name
    return forwarded


# The RunStore contract is checked at startup by member presence, so every
# member is forwarded explicitly rather than only through __getattr__.
for _member in contract_members(RunStore):
    if _member != "store_id":
        setattr(DelegatingStore, _member, _forward(_member))


class ShoutingParser(ParserBase):
    """A parser configured under ``parsers:`` with an option of its own."""

    format_id = "shop-lines"

    def __init__(self, status: str = "PASS"):
        self.status = status

    def parse(self, path: Any) -> list[ParsedVerdict]:
        parsed = []
        for file in sorted(Path(path).glob("*.lines")):
            for line in file.read_text(encoding="utf-8").split():
                parsed.append(ParsedVerdict(native_name=line, status=self.status, duration_ms=7))
        return parsed


def all_pass(ctx: DispatchContext) -> None:
    ctx.declare(["1"])
    ctx.deliver("1", verdicts=[Verdict(item_id, "PASS", 10) for item_id in ctx.item_ids])


@pytest.fixture()
def scripted(make_service):
    """``scripted(fn, cancel=…, **runner_options)`` → a Service whose runner
    ``fake`` runs ``fn(ctx)``. Extra config sections go in ``config=``."""
    names: list[str] = []

    def factory(
        fn: Callable[[DispatchContext], None],
        *,
        cancel: Callable[[DispatchContext], None] | None = None,
        config: dict[str, Any] | None = None,
        **options: Any,
    ) -> Service:
        name = uuid.uuid4().hex
        names.append(name)
        SCRIPTS[name] = fn
        if cancel is not None:
            CANCELS[name] = cancel
        runners = {
            "fake": {"module": "tests.test_dispatch:ScriptedRunner", "script": name, **options}
        }
        return make_service({"runners": runners, **(config or {})})

    yield factory
    for name in names:
        SCRIPTS.pop(name, None)
        CANCELS.pop(name, None)


def kinds_of(service: Service, run_id: str) -> list[str]:
    return [event.kind for event in service.store.events(run_id)]


def events_of(service: Service, run_id: str, kind: str) -> list[Any]:
    return [event for event in service.store.events(run_id) if event.kind == kind]


# -- the lifecycle ---------------------------------------------------------------


class TestLifecycle:
    def test_foreground_dispatch_walks_every_state_in_order(self, scripted):
        seen: list[str] = []

        def script(ctx: DispatchContext) -> None:
            seen.append(ctx_service.require_run(ctx.run_id).state)
            ctx.declare(["1"], plan="one shard")
            seen.append(ctx_service.require_run(ctx.run_id).state)
            ctx.deliver("1", verdicts=[Verdict(i, "PASS", 5) for i in ctx.item_ids])
            seen.append(ctx_service.require_run(ctx.run_id).state)

        ctx_service = service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        assert service.require_run(run_id).state == "COMPOSED"
        outcome = service.dispatch(run_id, "fake", foreground=True)

        assert seen == ["DISPATCHED", "RUNNING", "COMPLETE"]
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "PASS")
        dispatch = run.latest_dispatch
        assert dispatch.dispatch_id == outcome.dispatch_id
        assert dispatch.mode == "fake" and dispatch.shards == ("1",)
        assert dispatch.plan == "one shard" and dict(dispatch.summary) == {"PASS": 3}
        assert dispatch.owner is None, "nothing is executing it any more"
        assert outcome.refusal is None and outcome.done.is_set()

    def test_events_tell_the_whole_run_in_order(self, scripted):
        service = scripted(all_pass)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        assert kinds_of(service, run_id) == [
            "run.composed",
            "run.dispatched",
            "run.running",
            "delivery.recorded",
            "run.completed",
        ]
        completed = events_of(service, run_id, "run.completed")[0]
        assert completed.data["completion"] == "PASS"
        assert completed.data["summary"] == {"PASS": 3}
        assert completed.data["title"] == "Test run" and completed.data["latest"] is True
        seqs = [event.seq for event in service.store.events(run_id)]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    def test_background_dispatch_returns_at_once_and_is_observable(self, scripted):
        started, release = threading.Event(), threading.Event()

        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            started.set()
            assert release.wait(30)
            ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        outcome = service.dispatch(run_id, "fake")
        # The call is back while the runner is still inside dispatch().
        assert started.wait(30)
        assert not outcome.done.is_set()
        run = service.require_run(run_id)
        assert run.state == "RUNNING"
        assert run.latest_dispatch.owner == service.dispatcher.owner
        assert service.wait(run_id, timeout=0.05).state == "RUNNING"

        release.set()
        finished = service.wait(run_id, timeout=30)
        assert (finished.state, finished.completion) == ("COMPLETE", "PASS")
        assert outcome.done.wait(30)

    def test_returning_with_shards_outstanding_awaits_results(self, scripted):
        def script(ctx: DispatchContext) -> None:
            ctx.declare(["a", "b"])
            ctx.deliver("a", verdicts=[Verdict(ctx.item_ids[0], "PASS")])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        run = service.require_run(run_id)
        assert run.state == "AWAITING_RESULTS" and run.completion is None
        assert run.latest_dispatch.owner is None
        assert kinds_of(service, run_id)[-1] == "run.awaiting_results"

    def test_a_shard_under_another_label_does_not_stand_in_for_a_declared_one(self, scripted):
        """Completion is computed against the declared shard SET: 'a-retry'
        is two delivered labels, and still not shard 'b'."""

        def script(ctx: DispatchContext) -> None:
            ctx.declare(["a", "b"])
            ctx.deliver("a", verdicts=[Verdict(ctx.item_ids[0], "PASS")])
            ctx.deliver("a-retry", verdicts=[Verdict(ctx.item_ids[1], "PASS")])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        run = service.require_run(run_id)
        assert run.state == "AWAITING_RESULTS"
        dispatch_id = run.latest_dispatch.dispatch_id
        assert service.store.delivered_shards(run_id, dispatch_id) == {"a", "a-retry"}
        warnings = [e.data["message"] for e in events_of(service, run_id, "warning")]
        assert any("'a-retry' was not declared" in message for message in warnings)

        # The real shard 'b' arrives later, by ingestion → now it is complete.
        bundle = Path(service.config.artifact_dir).parent / "late"
        bundle.mkdir()
        (bundle / "results.json").write_text(
            json.dumps(
                {
                    "format": "runcomposer-verdicts",
                    "verdicts": [{"name": SSO[2], "status": "PASS"}],
                }
            ),
            encoding="utf-8",
        )
        report = service.ingest(bundle, run_id=run_id, shard="b")
        assert (report.run_state, report.completion) == ("COMPLETE", "PASS")

    def test_zero_declared_shards_settle_at_once_and_are_not_a_pass(self, scripted):
        service = scripted(lambda ctx: ctx.declare([]))
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        # Nothing was declared, so there is nothing to wait for: the dispatch
        # is settled when the runner returns — as INCOMPLETE, with every
        # planned item MISSING — rather than left awaiting results forever.
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "INCOMPLETE")
        assert dict(run.latest_dispatch.summary) == {"MISSING": 3}

    def test_identical_results_on_a_second_dispatch_complete_again(self, scripted):
        """Regression: the idempotency key is (run, dispatch, shard) — the
        same verdicts under a NEW dispatch are a new delivery, not a
        duplicate of the first one."""
        service = scripted(all_pass)
        run_id = compose(service, "Auth-SSO")
        first = service.dispatch(run_id, "fake", foreground=True)
        second = service.dispatch(run_id, "fake", foreground=True)
        run = service.require_run(run_id)
        assert [d.state for d in run.dispatches] == ["COMPLETE", "COMPLETE"]
        assert first.dispatch_id != second.dispatch_id
        assert len(run.deliveries) == 2
        assert run.deliveries[0].content_hash == run.deliveries[1].content_hash
        assert len(service.store.verdicts_for(run_id, second.dispatch_id)) == 3
        assert kinds_of(service, run_id).count("run.completed") == 2


# -- planned but never reported ------------------------------------------


class TestMissingItems:
    def test_one_of_three_reported_is_incomplete_never_pass(self, scripted):
        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            ctx.deliver("1", verdicts=[Verdict(ctx.item_ids[0], "PASS")])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "INCOMPLETE")
        assert dict(run.latest_dispatch.summary) == {"PASS": 1, "MISSING": 2}
        rows = run_detail(service, run)["verdicts"]
        missing = [row for row in rows if row["status"] == "MISSING"]
        assert {row["item_id"] for row in missing} == set(SSO[1:])
        assert all(row["message"] == "not-reported" for row in missing)
        # MISSING is derived, never stored.
        assert len(service.store.verdicts_for(run_id)) == 1

    def test_an_empty_delivery_is_not_a_pass(self, scripted):
        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            ctx.deliver("1", verdicts=[])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "INCOMPLETE")

    def test_a_failure_outranks_missing(self, scripted):
        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            ctx.deliver("1", verdicts=[Verdict(ctx.item_ids[0], "FAIL", message="boom")])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        run = service.require_run(run_id)
        assert run.completion == "FAIL"
        assert dict(run.latest_dispatch.summary) == {"FAIL": 1, "MISSING": 2}

    def test_with_a_per_shard_plan_missing_is_asked_per_shard(self, scripted):
        """A selection fanned out over two partitions: every item reported in
        env1, one item never reported in env2 → missing THERE."""

        def script(ctx: DispatchContext) -> None:
            ctx.declare({"env1": list(ctx.item_ids), "env2": list(ctx.item_ids)})
            ctx.deliver("env1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])
            ctx.deliver("env2", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids[:2]])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "INCOMPLETE")
        assert dict(run.latest_dispatch.summary) == {"PASS": 5, "MISSING": 1}
        detail = run_detail(service, run)
        shards = {entry["shard"]: entry for entry in detail["shards"]}
        assert shards["env1"]["completion"] == "PASS"
        assert shards["env2"]["completion"] == "INCOMPLETE"
        assert shards["env2"]["summary"] == {"PASS": 2, "MISSING": 1}
        missing = [row for row in detail["verdicts"] if row["status"] == "MISSING"]
        assert [(row["shard"], row["item_id"]) for row in missing] == [("env2", SSO[2])]

    def test_the_highest_attempt_decides(self, scripted):
        """A retried item that finally passed is a pass; the earlier attempt
        stays readable."""

        def script(ctx: DispatchContext) -> None:
            first, second, third = ctx.item_ids
            ctx.declare(["1"])
            ctx.deliver(
                "1",
                verdicts=[
                    Verdict(first, "FAIL", 10, "flaky", attempt=1),
                    Verdict(second, "PASS", 10),
                    Verdict(third, "PASS", 10),
                    Verdict(first, "PASS", 12, attempt=2),
                ],
            )

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        run = service.require_run(run_id)
        assert run.completion == "PASS" and dict(run.latest_dispatch.summary) == {"PASS": 3}
        stored = service.store.verdicts_for(run_id)
        assert [(v.item_id, v.attempt, v.status) for v in stored if v.item_id == SSO[0]] == [
            (SSO[0], 1, "FAIL"),
            (SSO[0], 2, "PASS"),
        ]

    def test_an_unplanned_item_is_recorded_and_flagged(self, scripted):
        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            ctx.deliver(
                "1",
                verdicts=[Verdict(i, "PASS") for i in ctx.item_ids]
                + [Verdict("Shop.Auth.Login.T001", "PASS")],
            )

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        warnings = [e.data["message"] for e in events_of(service, run_id, "warning")]
        assert any("were not planned" in message for message in warnings)
        assert service.require_run(run_id).completion == "PASS"


# -- refusing, failing, breaking the contract --------------------------------------------


class TestRefusalAndFailure:
    def test_a_refusal_before_the_hand_off_leaves_no_dispatch(self, scripted):
        service = scripted(lambda ctx: ctx.refuse("the shop is closed"))
        run_id = compose(service, "Auth-SSO")
        outcome = service.dispatch(run_id, "fake", foreground=True)

        assert outcome.refusal == "the shop is closed"
        run = service.require_run(run_id)
        assert run.state == "COMPOSED" and run.dispatches == ()
        assert service.store.get_dispatch(outcome.dispatch_id) is None
        refused = events_of(service, run_id, "dispatch.refused")
        assert len(refused) == 1
        assert refused[0].data["reason"] == "the shop is closed"
        assert refused[0].data["crashed"] is False
        assert refused[0].dispatch_id == outcome.dispatch_id

    def test_a_refused_redispatch_does_not_erase_a_finished_run(self, scripted):
        """Regression: COMPLETE (FAIL) stays COMPLETE (FAIL)."""
        refuse = threading.Event()

        def script(ctx: DispatchContext) -> None:
            if refuse.is_set():
                ctx.refuse("corpus drifted")
            ctx.declare(["1"])
            ctx.deliver("1", verdicts=[Verdict(i, "FAIL", 5, "red") for i in ctx.item_ids])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        before = service.require_run(run_id)
        assert (before.state, before.completion) == ("COMPLETE", "FAIL")

        refuse.set()
        outcome = service.dispatch(run_id, "fake", foreground=True)
        assert outcome.refusal == "corpus drifted"
        after = service.require_run(run_id)
        assert (after.state, after.completion, after.completed_at) == (
            "COMPLETE",
            "FAIL",
            before.completed_at,
        )
        assert [d.dispatch_id for d in after.dispatches] == [before.latest_dispatch.dispatch_id]
        assert len(service.store.verdicts_for(run_id)) == 3

    def test_an_exception_before_the_hand_off_behaves_like_a_refusal(self, scripted):
        def script(ctx: DispatchContext) -> None:
            raise OSError("the till is jammed")

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        outcome = service.dispatch(run_id, "fake", foreground=True)
        assert outcome.refusal == "OSError: the till is jammed"
        run = service.require_run(run_id)
        assert run.state == "COMPOSED" and run.dispatches == ()
        refused = events_of(service, run_id, "dispatch.refused")[0]
        assert refused.data["crashed"] is True

    def test_an_exception_after_the_hand_off_fails_and_keeps_what_was_delivered(self, scripted):
        def script(ctx: DispatchContext) -> None:
            ctx.declare(["a", "b"])
            ctx.deliver("a", verdicts=[Verdict(ctx.item_ids[0], "PASS", 5)])
            raise RuntimeError("worker lost")

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        outcome = service.dispatch(run_id, "fake", foreground=True)
        assert outcome.refusal is None
        run = service.require_run(run_id)
        assert run.state == "FAILED" and run.completion is None
        dispatch = run.latest_dispatch
        assert dispatch.error == "RuntimeError: worker lost" and dispatch.owner is None
        assert [v.item_id for v in service.store.verdicts_for(run_id)] == [SSO[0]]
        assert dict(dispatch.summary) == {"PASS": 1, "MISSING": 2}
        failed = events_of(service, run_id, "run.failed")[0]
        assert failed.data["error"] == "RuntimeError: worker lost"

    def test_a_refusal_after_the_hand_off_is_a_failure(self, scripted):
        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            ctx.refuse("too late to refuse")

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        run = service.require_run(run_id)
        assert run.state == "FAILED" and "too late to refuse" in run.latest_dispatch.error

    def test_returning_without_declaring_is_reported(self, scripted):
        service = scripted(lambda ctx: None)
        run_id = compose(service, "Auth-SSO")
        outcome = service.dispatch(run_id, "fake", foreground=True)
        assert outcome.refusal is not None and "without calling ctx.declare()" in outcome.refusal
        assert service.require_run(run_id).state == "COMPOSED"

    def test_declaring_twice_raises(self, scripted):
        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            ctx.declare(["2"])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        run = service.require_run(run_id)
        assert run.state == "FAILED" and "already declared" in run.latest_dispatch.error
        assert run.latest_dispatch.shards == ("1",)

    def test_delivering_before_declaring_raises(self, scripted):
        def script(ctx: DispatchContext) -> None:
            ctx.deliver("1", verdicts=[])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        outcome = service.dispatch(run_id, "fake", foreground=True)
        assert outcome.refusal is not None and "before declare()" in outcome.refusal

    @pytest.mark.parametrize(
        ("shards", "error"),
        [(["a", "a"], "must be distinct"), ("ab", "sequence of shard names")],
    )
    def test_a_malformed_declaration_is_refused(self, scripted, shards, error):
        service = scripted(lambda ctx: ctx.declare(shards))
        run_id = compose(service, "Auth-SSO")
        outcome = service.dispatch(run_id, "fake", foreground=True)
        assert outcome.refusal is not None and error in outcome.refusal

    def test_a_delivered_verdict_cannot_claim_missing(self, scripted):
        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            ctx.deliver("1", verdicts=[Verdict(ctx.item_ids[0], "MISSING")])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        run = service.require_run(run_id)
        assert run.state == "FAILED" and "MISSING" in run.latest_dispatch.error

    def test_a_runner_error_after_everything_was_delivered_keeps_the_result(self, scripted):
        def script(ctx: DispatchContext) -> None:
            all_pass(ctx)
            raise RuntimeError("cleanup failed")

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "PASS")
        warnings = [e.data["message"] for e in events_of(service, run_id, "warning")]
        assert any("cleanup failed" in message for message in warnings)


# -- what may be dispatched, and with what -------------------------------------------------


class TestPreconditions:
    def test_only_configured_runner_ids_are_dispatchable(self, scripted):
        """Regression: the bundled `demo` runner is installed, but it is not
        configured here — so it cannot be dispatched to."""
        service = scripted(all_pass)
        run_id = compose(service, "Auth-SSO")
        with pytest.raises(UnknownRunner, match=r"no runner is configured under the id 'demo'"):
            service.dispatch(run_id, "demo", foreground=True)
        run = service.require_run(run_id)
        assert run.state == "COMPOSED" and run.dispatches == ()
        assert [info["id"] for info in service.runner_infos()] == ["fake"]

    def test_the_config_key_is_the_runner_id_and_one_plugin_may_be_configured_twice(
        self, make_service
    ):
        seen: list[tuple[str, str]] = []

        def script(ctx: DispatchContext) -> None:
            seen.append((ctx.runner_id, str(ctx.options["fixed"])))
            all_pass(ctx)

        SCRIPTS["twice"] = script
        try:
            entry = {"module": "tests.test_dispatch:ScriptedRunner", "script": "twice"}
            service = make_service(
                runners={"staging": {**entry, "fixed": "s"}, "production": {**entry, "fixed": "p"}}
            )
            assert [info["id"] for info in service.runner_infos()] == ["staging", "production"]
            assert {info["plugin"] for info in service.runner_infos()} == {"scripted"}
            for runner_id in ("staging", "production"):
                service.dispatch(compose(service, "Auth-SSO"), runner_id, foreground=True)
            assert seen == [("staging", "s"), ("production", "p")]
            modes = [run.latest_dispatch.mode for run in service.list_runs().runs]
            assert sorted(modes) == ["production", "staging"]
        finally:
            del SCRIPTS["twice"]

    def test_a_second_dispatch_while_one_is_in_flight_is_refused(self, scripted):
        started, release = threading.Event(), threading.Event()

        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            started.set()
            assert release.wait(30)
            ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake")
        assert started.wait(30)
        with pytest.raises(RunActive, match="already has a dispatch in flight"):
            service.dispatch(run_id, "fake")
        release.set()
        assert service.wait(run_id, timeout=30).state == "COMPLETE"
        assert len(service.require_run(run_id).dispatches) == 1

    def test_two_processes_cannot_both_dispatch_one_run(self, scripted, tmp_path):
        """Regression: the in-process check runs under the dispatch manager's
        lock, which another process does not share. Two Services on one store
        file stand for two processes; the second is put exactly into the race
        window (its own check passed before the first recorded anything) —
        and the store's exclusive insert still refuses it."""
        started, release = threading.Event(), threading.Event()

        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            started.set()
            assert release.wait(30)
            ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        first = scripted(script)
        second = Service(load_config(str(tmp_path / "config.yaml")))
        try:
            run_id = compose(first, "Auth-SSO")
            first.dispatch(run_id, "fake")
            assert started.wait(30)
            second.dispatcher._refuse_if_in_flight = lambda _run_id: None  # the race window
            with pytest.raises(RunActive, match="already has a dispatch in flight"):
                second.dispatch(run_id, "fake")
            release.set()
            assert first.wait(run_id, timeout=30).state == "COMPLETE"
            assert len(first.require_run(run_id).dispatches) == 1
        finally:
            release.set()
            second.close()

    def test_an_empty_selection_is_not_dispatched(self, scripted):
        service = scripted(all_pass)
        run_id = compose(service, {"tag_filter": "regex:^NoSuchTagAnywhere$"})
        assert service.require_run(run_id).planned_count == 0
        with pytest.raises(InvalidRequest, match="nothing to execute"):
            service.dispatch(run_id, "fake", foreground=True)
        assert service.require_run(run_id).dispatches == ()

    def test_max_concurrent_bounds_parallel_dispatches(self, scripted):
        """Three dispatches, a bound of two: the third does not enter its
        runner until one of the first two has left."""
        entered = threading.Semaphore(0)
        release = threading.Event()
        inside: list[str] = []
        lock = threading.Lock()

        def script(ctx: DispatchContext) -> None:
            with lock:
                inside.append(ctx.run_id)
            entered.release()
            assert release.wait(30)
            all_pass(ctx)

        service = scripted(script, config={"core": {"dispatch": {"max_concurrent": 2}}})
        run_ids = [compose(service, "Auth-SSO") for _ in range(3)]
        outcomes = [service.dispatch(run_id, "fake") for run_id in run_ids]
        assert entered.acquire(timeout=30) and entered.acquire(timeout=30)
        # Both workers are held inside the runner; the third dispatch is
        # recorded (DISPATCHED) but cannot have started.
        assert len(inside) == 2
        queued = next(run_id for run_id in run_ids if run_id not in inside)
        assert service.require_run(queued).state == "DISPATCHED"
        assert not entered.acquire(blocking=False)

        release.set()
        for outcome in outcomes:
            assert outcome.done.wait(30)
        assert len(inside) == 3
        assert {service.require_run(r).state for r in run_ids} == {"COMPLETE"}


# -- per-run parameters ------------------------------------------------------

_PARAMETERS = [
    {"name": "partition", "kind": "choice", "choices": ["env1", "env2"], "default": "env1"},
    {"name": "retries", "kind": "integer"},
]


class TestParameters:
    def test_validated_values_reach_the_context_and_the_document(self, scripted):
        seen: dict[str, Any] = {}

        def script(ctx: DispatchContext) -> None:
            seen["options"] = dict(ctx.options)
            seen["parameters"] = dict(ctx.parameters)
            all_pass(ctx)

        service = scripted(script, parameters=_PARAMETERS, fixed="from-config")
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", {"partition": "env2", "retries": 2}, foreground=True)

        assert seen["parameters"] == {"partition": "env2", "retries": 2}
        # Options are the configured ones and nothing else: a per-run value
        # never shows up among them (so it cannot shadow one).
        assert "partition" not in seen["options"] and "retries" not in seen["options"]
        assert seen["options"]["fixed"] == "from-config"
        spec = service.require_spec(run_id)
        assert spec["runner"]["fake"] == {"partition": "env2", "retries": 2}
        assert dict(service.require_run(run_id).latest_dispatch.parameters) == {
            "partition": "env2",
            "retries": 2,
        }

    def test_defaults_are_filled_in(self, scripted):
        seen: dict[str, Any] = {}

        def script(ctx: DispatchContext) -> None:
            seen.update(ctx.parameters)
            all_pass(ctx)

        service = scripted(script, parameters=_PARAMETERS)
        service.dispatch(compose(service, "Auth-SSO"), "fake", {}, foreground=True)
        assert seen == {"partition": "env1"}

    @pytest.mark.parametrize(
        ("values", "named"),
        [
            ({"partition": "env9"}, "partition"),
            ({"retries": "two"}, "retries"),
            ({"retries": True}, "retries"),
            ({"sharding": 4}, "sharding"),
        ],
    )
    def test_a_bad_value_is_refused_naming_the_parameter(self, scripted, values, named):
        service = scripted(all_pass, parameters=_PARAMETERS)
        run_id = compose(service, "Auth-SSO")
        with pytest.raises(InvalidParameter) as caught:
            service.dispatch(run_id, "fake", values, foreground=True)
        assert caught.value.parameter == named and named in str(caught.value)
        assert service.require_run(run_id).dispatches == ()

    def test_composing_for_a_runner_checks_parameters_before_a_run_exists(self, scripted):
        service = scripted(all_pass, parameters=_PARAMETERS)
        with pytest.raises(InvalidParameter):
            service.compose_run(
                {"tag_filter": "Auth-SSO"},
                title="t",
                origin="test",
                runner_id="fake",
                parameters={"partition": "nowhere"},
            )
        with pytest.raises(UnknownRunner):
            service.compose_run(
                {"tag_filter": "Auth-SSO"}, title="t", origin="test", runner_id="ghost"
            )
        assert service.list_runs().total == 0

    def test_a_document_cannot_override_a_configured_option(self, scripted):
        """The spec's ``runner.fake`` carries `fixed` (a configured option,
        not a declared parameter) and a key nobody knows. Both are ignored
        with a warning event; the declared parameter in it is honoured."""
        seen: dict[str, Any] = {}

        def script(ctx: DispatchContext) -> None:
            seen["options"] = dict(ctx.options)
            seen["parameters"] = dict(ctx.parameters)
            all_pass(ctx)

        service = scripted(script, parameters=_PARAMETERS, fixed="from-config")
        composed = service.build_spec_document(
            {"tag_filter": "Auth-SSO"},
            title="Imported",
            runner_section={
                "fake": {"partition": "env2", "fixed": "from-document", "pre_run_hooks": ["x"]}
            },
        )
        run_id = service.import_spec(composed.spec, origin="test")
        service.dispatch(run_id, "fake", foreground=True)  # parameters come from the document

        assert seen["parameters"] == {"partition": "env2"}
        assert seen["options"]["fixed"] == "from-config"
        assert "pre_run_hooks" not in seen["options"]
        warnings = [e.data["message"] for e in events_of(service, run_id, "warning")]
        assert len(warnings) == 1
        assert "['fixed', 'pre_run_hooks']" in warnings[0] and "ignored" in warnings[0]
        assert service.require_run(run_id).completion == "PASS"

    def test_a_bad_parameter_value_in_a_document_is_refused(self, scripted):
        service = scripted(all_pass, parameters=_PARAMETERS)
        composed = service.build_spec_document(
            {"tag_filter": "Auth-SSO"},
            title="Imported",
            runner_section={"fake": {"partition": "the-moon"}},
        )
        run_id = service.import_spec(composed.spec, origin="test")
        with pytest.raises(InvalidParameter, match="partition"):
            service.dispatch(run_id, "fake", foreground=True)


# -- cancelling ----------------------------------------------------------------------------


class TestCancel:
    def test_a_running_dispatch_is_told_and_becomes_cancelled_when_it_returns(self, scripted):
        started = threading.Event()
        cancel_calls: list[tuple[str, bool]] = []
        observed: list[bool] = []

        def script(ctx: DispatchContext) -> None:
            ctx.declare(["a", "b"], links={"queue": "https://ci.shop.example/queue/7"})
            ctx.deliver("a", verdicts=[Verdict(ctx.item_ids[0], "PASS")])
            started.set()
            wait_until(lambda: ctx.cancelled, what="the cancel request")
            observed.append(ctx.cancelled)
            ctx.raise_if_cancelled()

        def cancel(ctx: DispatchContext) -> None:
            cancel_calls.append((ctx.dispatch_id, ctx.cancelled))

        service = scripted(script, cancel=cancel)
        run_id = compose(service, "Auth-SSO")
        outcome = service.dispatch(run_id, "fake")
        assert started.wait(30)
        requested = service.cancel(run_id)
        assert requested.cancel_requested is True

        assert outcome.done.wait(30)
        run = service.require_run(run_id)
        assert run.state == "CANCELLED" and run.completion is None
        assert observed == [True]
        assert cancel_calls == [(outcome.dispatch_id, True)]
        # What was delivered before the cancel is kept.
        assert [v.item_id for v in service.store.verdicts_for(run_id)] == [SSO[0]]
        kinds = kinds_of(service, run_id)
        assert kinds.index("run.cancel_requested") < kinds.index("run.cancelled")

    def test_a_queued_dispatch_is_cancelled_without_its_runner_starting(self, scripted):
        started, release = threading.Event(), threading.Event()
        entered: list[str] = []

        def script(ctx: DispatchContext) -> None:
            entered.append(ctx.run_id)
            started.set()
            assert release.wait(30)
            all_pass(ctx)

        service = scripted(script, config={"core": {"dispatch": {"max_concurrent": 1}}})
        first, second = compose(service, "Auth-SSO"), compose(service, "Auth-SSO")
        first_outcome = service.dispatch(first, "fake")
        assert started.wait(30)
        second_outcome = service.dispatch(second, "fake")  # queued behind the first
        assert service.require_run(second).state == "DISPATCHED"

        service.cancel(second)
        assert second_outcome.done.wait(30)
        release.set()
        assert first_outcome.done.wait(30)

        assert entered == [first]
        cancelled = service.require_run(second)
        assert cancelled.state == "CANCELLED"
        assert "queued" in cancelled.latest_dispatch.error
        assert service.require_run(first).state == "COMPLETE"

    def test_a_dispatch_awaiting_results_is_cancelled_at_once_through_a_rebuilt_context(
        self, scripted
    ):
        """Nothing is executing an AWAITING_RESULTS dispatch, so the runner's
        cancel gets a context rebuilt from the stored record — with the links
        it declared, which is how it finds its executor."""
        cancel_calls: list[dict[str, Any]] = []

        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"], links={"queue": "https://ci.shop.example/queue/42"})

        def cancel(ctx: DispatchContext) -> None:
            cancel_calls.append(
                {
                    "links": dict(ctx.links),
                    "cancelled": ctx.cancelled,
                    "parameters": dict(ctx.parameters),
                    "dispatch": ctx.dispatch_id,
                }
            )

        service = scripted(script, cancel=cancel, parameters=_PARAMETERS)
        run_id = compose(service, "Auth-SSO")
        outcome = service.dispatch(run_id, "fake", {"partition": "env2"}, foreground=True)
        assert service.require_run(run_id).state == "AWAITING_RESULTS"

        dispatch = service.cancel(run_id)
        assert dispatch.state == "CANCELLED"
        assert cancel_calls == [
            {
                "links": {"queue": "https://ci.shop.example/queue/42"},
                "cancelled": True,
                "parameters": {"partition": "env2"},
                "dispatch": outcome.dispatch_id,
            }
        ]
        assert service.require_run(run_id).state == "CANCELLED"

    def test_an_export_dispatch_can_be_cancelled(self, service):
        run_id = compose(service, "Auth-SSO")
        service.export(run_id)
        assert service.cancel(run_id).state == "CANCELLED"

    def test_a_cancel_requested_by_another_process_is_honoured_by_the_owner(
        self, scripted, tmp_path
    ):
        """The CLI's `cancel` against a server: a second Service on the same
        database only writes the request; the Service that is executing the
        dispatch picks it up and stops its runner.

        The runner here never looks at ``ctx.cancelled`` on its own — it
        stops only when its ``cancel`` is called — so reaching CANCELLED
        proves the owner was the one who told it."""
        started, told = threading.Event(), threading.Event()
        cancel_calls: list[tuple[str, str]] = []

        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            started.set()
            assert told.wait(60), "runner.cancel was never called"
            ctx.raise_if_cancelled()

        def cancel(ctx: DispatchContext) -> None:
            cancel_calls.append((ctx.dispatch_id, threading.current_thread().name))
            told.set()

        owner = scripted(script, cancel=cancel)
        run_id = compose(owner, "Auth-SSO")
        outcome = owner.dispatch(run_id, "fake")
        assert started.wait(30)

        with Service(load_config(str(tmp_path / "config.yaml"))) as other:
            assert other.dispatcher.owner != owner.dispatcher.owner
            requested = other.cancel(run_id)
            assert requested.cancel_requested is True
            assert other.dispatcher.active_dispatch_ids() == [], "it executes nothing itself"

        assert outcome.done.wait(60)
        assert owner.require_run(run_id).state == "CANCELLED"
        # Told once, by the owner's own watcher thread — not by the Service
        # that merely recorded the request.
        assert cancel_calls == [(outcome.dispatch_id, "runcomposer-cancel")]

    def test_nothing_in_flight_is_a_conflict(self, scripted):
        service = scripted(all_pass)
        run_id = compose(service, "Auth-SSO")
        with pytest.raises(Conflict, match="nothing in flight to cancel"):
            service.cancel(run_id)
        service.dispatch(run_id, "fake", foreground=True)
        with pytest.raises(Conflict, match="state COMPLETE"):
            service.cancel(run_id)

    def test_a_cancel_from_another_process_reaches_a_foreground_dispatch(self, scripted, tmp_path):
        """Regression: the CLI's `run` executes in the foreground, and a
        cancel written to the store by another process (a second shell's
        `runcomposer cancel`) used to be seen only through `ctx.cancelled` —
        the runner's cancel(ctx), which stops its executor, was never called.
        Here the runner never looks at the flag: only cancel(ctx) frees it."""
        declared, released = threading.Event(), threading.Event()

        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            declared.set()
            assert released.wait(60)

        service = scripted(script, cancel=lambda ctx: released.set())
        other = Service(load_config(str(tmp_path / "config.yaml")))  # stands for another process
        try:
            run_id = compose(service, "Auth-SSO")
            worker = threading.Thread(
                target=service.dispatch, args=(run_id, "fake"), kwargs={"foreground": True}
            )
            worker.start()
            assert declared.wait(30)
            other.cancel(run_id)
            worker.join(20)
            assert not worker.is_alive(), "cancel(ctx) was not called for the foreground dispatch"
            assert service.require_run(run_id).state == "CANCELLED"
        finally:
            released.set()
            other.close()

    def test_results_for_a_cancelled_dispatch_are_not_taken_in(self, scripted):
        service = scripted(lambda ctx: ctx.declare(["1"]))
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        service.cancel(run_id)
        bundle = Path(service.config.artifact_dir).parent / "late"
        bundle.mkdir()
        (bundle / "results.json").write_text(
            json.dumps(
                {
                    "format": "runcomposer-verdicts",
                    "verdicts": [{"name": item_id, "status": "PASS"} for item_id in SSO],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(IngestError, match="was cancelled") as refused:
            service.ingest(bundle, run_id=run_id)
        assert refused.value.reason == "dispatch-cancelled"
        with pytest.raises(Conflict, match="was cancelled"):
            service.finalize(run_id)
        run = service.require_run(run_id)
        assert run.state == "CANCELLED" and service.store.verdicts_for(run_id) == []


# -- live status -------------------------------------------------------------

_CHILD = """
import pickle, sys
channel = pickle.loads(bytes.fromhex(sys.argv[1]))
channel.live("1", sys.argv[2], "PASS", duration_ms=321, message="from a worker process")
channel.live("1", "Shop.Not.Planned", "PASS")
channel.live("1", sys.argv[3], "EXPLODED")
channel.log("the worker is done", "warning")
"""


class TestLiveStatus:
    def test_a_worker_process_streams_through_any_store(self, scripted):
        """The channel is pickled, handed to a REAL child process, and what
        the child writes shows up through a store that is not the sqlite
        class and has no `_path` to reach for."""
        streamed, release = threading.Event(), threading.Event()

        def script(ctx: DispatchContext) -> None:
            ctx.declare({"1": list(ctx.item_ids)})
            channel = pickle.dumps(ctx.live_channel()).hex()
            subprocess.run(
                [sys.executable, "-c", _CHILD, channel, ctx.item_ids[0], ctx.item_ids[1]],
                check=True,
                timeout=60,
            )
            ctx.live("1", ctx.item_ids[2], "FAIL", duration_ms=5, message="from the parent")
            streamed.set()
            assert release.wait(30)
            ctx.deliver("1", verdicts=[Verdict(i, "PASS", 9) for i in ctx.item_ids])

        service = scripted(
            script,
            config={
                "store": {
                    "delegating": {
                        "module": "tests.test_dispatch:DelegatingStore",
                        "path": "runcomposer.db",
                    }
                }
            },
        )
        assert type(service.store) is DelegatingStore and not hasattr(service.store, "_path")
        run_id = compose(service, "Auth-SSO")
        outcome = service.dispatch(run_id, "fake")
        assert streamed.wait(60)

        def live_rows() -> list[Verdict]:
            return service.store.live_verdicts(run_id, outcome.dispatch_id)

        wait_until(lambda: len(live_rows()) == 2, what="both live rows")
        rows = {v.item_id: v for v in live_rows()}
        # The unplanned id and the unreportable status never became rows.
        assert set(rows) == {SSO[0], SSO[2]}
        assert (rows[SSO[0]].status, rows[SSO[0]].duration_ms) == ("PASS", 321)
        assert rows[SSO[0]].message == "from a worker process"
        assert rows[SSO[2]].status == "FAIL"
        # Live status is not a result: nothing is delivered yet.
        assert service.store.verdicts_for(run_id) == []
        detail = run_detail(service, service.require_run(run_id))
        assert detail["state"] == "RUNNING"
        assert sorted((row["item_id"], row["live"]) for row in detail["verdicts"]) == [
            (SSO[0], True),
            (SSO[2], True),
        ]

        release.set()
        assert outcome.done.wait(30)
        # The delivery replaces the shard's live rows.
        assert live_rows() == []
        detail = run_detail(service, service.require_run(run_id))
        assert [row["live"] for row in detail["verdicts"]] == [False, False, False]
        assert {row["status"] for row in detail["verdicts"]} == {"PASS"}

    def test_live_status_never_invents_an_item_or_a_status(self, scripted):
        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            ctx.live("1", "Shop.Somewhere.Else", "PASS")
            ctx.live("1", ctx.item_ids[0], "MISSING")
            assert service.store.live_verdicts(ctx.run_id, ctx.dispatch_id) == []
            ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        assert service.require_run(run_id).completion == "PASS"


# -- delivering bundles, artifacts, log, hand-out -------------------------------------------


class TestContextServices:
    def test_deliver_bundle_uses_the_configured_parser(self, scripted):
        """`parsers:` configures a parser — class and options — under a
        format id; `ctx.deliver(bundle=…)` and `ctx.parse` go through it."""
        parsed: list[Verdict] = []

        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            bundle = ctx.workdir / "out"
            bundle.mkdir()
            (bundle / "names.lines").write_text(
                "\n".join([*ctx.item_ids, "Shop.Unknown.T999"]), encoding="utf-8"
            )
            parsed.extend(ctx.parse(bundle, format="shop-lines"))
            assert ctx.deliver("1", bundle=bundle, format="shop-lines") == "new"
            assert ctx.deliver("1", bundle=bundle, format="shop-lines") == "duplicate"

        service = scripted(
            script,
            config={
                "parsers": {
                    "shop-lines": {
                        "module": "tests.test_dispatch:ShoutingParser",
                        "status": "FAIL",
                    }
                }
            },
        )
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "FAIL")
        assert [v.item_id for v in parsed] == SSO and {v.status for v in parsed} == {"FAIL"}
        assert run.deliveries[0].format == "shop-lines"
        warnings = [e.data["message"] for e in events_of(service, run_id, "warning")]
        assert any("unknown native name 'Shop.Unknown.T999'" in m for m in warnings)

    def test_a_bundle_carrying_another_runs_marker_is_not_delivered(self, scripted):
        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            bundle = ctx.workdir / "out"
            bundle.mkdir()
            (bundle / "results.json").write_text(
                json.dumps(
                    {
                        "format": "runcomposer-verdicts",
                        "verdicts": [{"name": i, "status": "PASS"} for i in ctx.item_ids],
                    }
                ),
                encoding="utf-8",
            )
            (bundle / "runcomposer_run.json").write_text(
                json.dumps({"run_id": "01SOMEBODYELSESRUN00000000"}), encoding="utf-8"
            )
            ctx.deliver("1", bundle=bundle)

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        run = service.require_run(run_id)
        assert run.state == "FAILED" and "another run's results" in run.latest_dispatch.error
        assert service.store.verdicts_for(run_id) == []

    def test_a_marker_written_from_the_handed_out_spec_verifies(self, scripted):
        exec_main = runcomposer_exec.main

        def script(ctx: DispatchContext) -> None:
            spec_file = ctx.workdir / "spec.json"
            spec_file.write_bytes(ctx.export_spec())
            ctx.declare(["1"])
            out = ctx.workdir / "out"
            assert exec_main([str(spec_file), "--out", str(out), "--simulate", "--seed", "a"]) == 0
            marker = json.loads((out / "runcomposer_run.json").read_text())
            assert marker["spec_sha256"] == sha256_hex(ctx.export_spec())
            ctx.deliver("1", bundle=out)
            # A marker from a DIFFERENT document is refused.
            marker["spec_sha256"] = "0" * 64
            (out / "runcomposer_run.json").write_text(json.dumps(marker))
            with pytest.raises(RuntimeError, match="does not match the spec handed out"):
                ctx.deliver("1", bundle=out)

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "PASS")

    def test_export_spec_bytes_are_recorded_and_re_readable(self, scripted):
        handed: dict[str, bytes] = {}

        def script(ctx: DispatchContext) -> None:
            handed["bytes"] = ctx.export_spec()
            assert ctx.export_spec() is handed["bytes"], "one hand-out per dispatch"
            all_pass(ctx)

        service = scripted(script)
        run_id = compose(service, "Auth-SSO", title="Anmeldung prüfen")
        outcome = service.dispatch(run_id, "fake", foreground=True)
        dispatch = service.store.get_dispatch(outcome.dispatch_id)
        assert dispatch.spec_sha256 == sha256_hex(handed["bytes"])
        content, media_type, record = service.dispatch_spec(run_id, outcome.dispatch_id)
        assert content == handed["bytes"] and media_type == "application/json"
        assert record.dispatch_id == outcome.dispatch_id
        document = json.loads(content.decode("utf-8"))
        assert document["run"]["title"] == "Anmeldung prüfen"
        assert "prüfen".encode() in content, "non-ASCII is kept as UTF-8, not escaped"
        assert "token" not in document["results"]

    def test_a_dispatch_without_a_hand_out_has_no_spec_to_download(self, scripted):
        service = scripted(all_pass)
        run_id = compose(service, "Auth-SSO")
        outcome = service.dispatch(run_id, "fake", foreground=True)
        with pytest.raises(NotFound, match="no handed-out document"):
            service.dispatch_spec(run_id, outcome.dispatch_id)
        with pytest.raises(NotFound, match="has no dispatch"):
            service.dispatch_spec(run_id, "01NOSUCHDISPATCH0000000000")

    def test_the_ingest_token_opens_this_dispatch_only(self, scripted):
        tokens: list[str | None] = []

        def script(ctx: DispatchContext) -> None:
            tokens.append(ctx.ingest_token())
            assert ctx.ingest_token() == tokens[-1], "minted once per dispatch"
            ctx.declare(["1"])

        service = scripted(script)
        first, second = compose(service, "Auth-SSO"), compose(service, "Auth-SSO")
        first_outcome = service.dispatch(first, "fake", foreground=True)
        second_outcome = service.dispatch(second, "fake", foreground=True)
        token_one, token_two = tokens
        assert token_one and token_two and token_one != token_two
        assert token_one.startswith("rct_")
        assert service.ingestion.authorize_push(first, token_one) == first_outcome.dispatch_id
        assert service.ingestion.authorize_push(second, token_two) == second_outcome.dispatch_id
        with pytest.raises(Forbidden):
            service.ingestion.authorize_push(second, token_one)
        # Only the hash is stored.
        assert service.store.ingest_tokens(first) == {
            sha256_hex(token_one.encode()): first_outcome.dispatch_id
        }
        assert token_one not in json.dumps(service.require_spec(first))

    def test_no_token_is_minted_when_tokens_are_disabled(self, scripted):
        tokens: list[str | None] = []

        def script(ctx: DispatchContext) -> None:
            tokens.append(ctx.ingest_token())
            all_pass(ctx)

        service = scripted(script, config={"core": {"ingestion": {"tokens": "disabled"}}})
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        assert tokens == [None] and service.store.ingest_tokens(run_id) == {}

    def test_artifacts_log_and_workdir(self, scripted):
        def script(ctx: DispatchContext) -> None:
            assert ctx.workdir.is_dir()
            assert ctx.workdir.parent.name == ctx.run_id and ctx.workdir.name == ctx.dispatch_id
            ctx.declare(["1"])
            report = ctx.workdir / "1" / "log.html"
            report.parent.mkdir()
            report.write_text("<html>checkout log</html>", encoding="utf-8")
            ctx.add_artifact("log.html (1)", report, shard="1")
            ctx.add_artifact("Build", "https://ci.shop.example/job/7/")
            ctx.log("about to deliver")
            ctx.log("something looks odd", level="warning")
            ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        outcome = service.dispatch(run_id, "fake", foreground=True)
        artifacts = service.artifacts.list(run_id)
        assert [(a["name"], a["kind"], a["shard"], a["media_type"]) for a in artifacts] == [
            ("log.html (1)", "local", "1", "text/html"),
            ("Build", "url", None, "application/octet-stream"),
        ]
        assert artifacts[0]["href"] == f"/artifacts/{run_id}/{outcome.dispatch_id}/1/log.html"
        assert artifacts[1]["href"] == "https://ci.shop.example/job/7/"
        assert service.artifacts.resolve_file(artifacts[0]["href"][len("/artifacts/"):]).is_file()
        logs = events_of(service, run_id, "log")
        assert [(e.data["message"], e.data["level"]) for e in logs] == [("about to deliver", "info")]
        assert [e.data["message"] for e in events_of(service, run_id, "warning")] == [
            "something looks odd"
        ]

    def test_the_context_gives_the_spec_items_source_and_history(self, scripted):
        seen: dict[str, Any] = {}

        def script(ctx: DispatchContext) -> None:
            seen["items"] = list(ctx.item_ids)
            seen["title"] = ctx.spec["run"]["title"]
            seen["snapshot_matches"] = ctx.source.snapshot() == ctx.spec["source"]["snapshot"]
            seen["resolves"] = ctx.source.resolve(ctx.item_ids[0])
            seen["durations"] = ctx.history.durations(last_n=5)
            with pytest.raises(TypeError):
                ctx.spec["run"] = {}  # read-only
            all_pass(ctx)

        service = scripted(script)
        first = compose(service, "Auth-SSO")
        service.dispatch(first, "fake", foreground=True)
        assert seen["items"] == SSO and seen["title"] == "Test run"
        assert seen["snapshot_matches"] is True and seen["resolves"] == SSO[0]
        assert seen["durations"] == {}, "cold start: no history yet"
        second = compose(service, "Auth-SSO")
        service.dispatch(second, "fake", foreground=True)
        assert seen["durations"] == dict.fromkeys(SSO, 10.0)


# -- observers see the run -------------------------------------------------------------------

NOTIFIED: list[tuple[str, str]] = []


class RecordingObserver(ObserverBase):
    def __init__(self, name: str = "recorder"):
        self.name = name

    def notify(self, event: Any) -> None:
        NOTIFIED.append((self.name, event.kind))


def test_observers_are_told_the_lifecycle_in_order(scripted):
    NOTIFIED.clear()
    service = scripted(
        all_pass,
        config={"observers": {"recorder": {"module": "tests.test_dispatch:RecordingObserver"}}},
    )
    run_id = compose(service, "Auth-SSO")
    service.dispatch(run_id, "fake", foreground=True)
    assert service.bus.flush(30)
    assert NOTIFIED == [
        ("recorder", "run.composed"),
        ("recorder", "run.dispatched"),
        ("recorder", "run.running"),
        ("recorder", "delivery.recorded"),
        ("recorder", "run.completed"),
    ]


# -- rerun and finalize ---------------------------------------------------------------------


class TestRerun:
    def test_scope_all_is_a_new_dispatch_of_the_same_run(self, make_service):
        service = make_service()
        run_id = compose(service, "Auth-SSO", runner_id="demo", parameters={"seed": "b"})
        service.dispatch(run_id, "demo", {"seed": "b"}, foreground=True)
        target, outcome = service.rerun(run_id, foreground=True)
        assert target == run_id
        run = service.require_run(run_id)
        assert [d.mode for d in run.dispatches] == ["demo", "demo"]
        assert run.latest_dispatch.dispatch_id == outcome.dispatch_id
        # The same runner and the same per-run parameters, unless told otherwise.
        assert dict(run.latest_dispatch.parameters) == {"seed": "b"}
        assert run.dispatches[0].summary == run.dispatches[1].summary

    def test_scope_failed_composes_a_new_run_from_what_did_not_pass(self, scripted):
        """FAIL, ERROR and MISSING all count as "did not pass"."""
        first_time = threading.Event()

        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            if not first_time.is_set():
                first_time.set()
                one, two, three, four, _five = ctx.item_ids
                ctx.deliver(
                    "1",
                    verdicts=[
                        Verdict(one, "PASS"),
                        Verdict(two, "FAIL", message="red"),
                        Verdict(three, "ERROR", message="crashed"),
                        Verdict(four, "SKIP"),
                    ],  # the fifth is never reported
                )
            else:
                ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        service = scripted(script)
        run_id = compose(service, "Auth-Login", labels={"suite": "nightly"})
        service.dispatch(run_id, "fake", foreground=True)
        original = service.require_run(run_id)
        assert original.completion == "ERROR"

        target, _outcome = service.rerun(run_id, scope="failed", foreground=True)
        assert target != run_id
        rerun = service.require_run(target)
        assert (rerun.state, rerun.completion) == ("COMPLETE", "PASS")
        assert rerun.origin == "rerun" and dict(rerun.labels) == {"suite": "nightly"}
        # The title is the source run's: that this is a rerun is data, not prose.
        assert rerun.title == "Test run"
        assert run_detail(service, rerun)["rerun_of"] == {"run_id": run_id, "scope": "failed"}
        spec = service.require_spec(target)
        assert spec["selection"]["materialized"]["item_ids"] == [
            "Shop.Auth.Login.T002",
            "Shop.Auth.Login.T003",
            "Shop.Auth.Login.T005",
        ]
        provenance = spec["selection"]["derived_from"][0]
        assert provenance["provider"] == "history"
        assert provenance["resolved_run_id"] == run_id
        assert provenance["query"] == {"run": run_id, "verdicts": ["FAIL", "ERROR", "MISSING"]}
        # The original run is untouched.
        assert len(service.require_run(run_id).dispatches) == 1

    def test_rerun_failed_of_a_green_run_has_nothing_to_do(self, scripted):
        service = scripted(all_pass)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        with pytest.raises(InvalidRequest, match="nothing to rerun"):
            service.rerun(run_id, scope="failed", foreground=True)
        assert service.list_runs().total == 1

    def test_rerun_needs_a_runner_and_a_finished_run(self, service):
        run_id = compose(service, "Auth-SSO")
        with pytest.raises(InvalidRequest, match="never dispatched to a runner"):
            service.rerun(run_id)
        service.export(run_id)
        with pytest.raises(InvalidRequest, match="name the runner"):
            service.rerun(run_id)
        with pytest.raises(RunActive, match="failures are not known yet"):
            service.rerun(run_id, scope="failed", runner_id="demo")
        with pytest.raises(InvalidRequest, match="'all' or 'failed'"):
            service.rerun(run_id, scope="some", runner_id="demo")


class TestFinalize:
    def test_a_never_dispatched_run_has_nothing_to_finalize(self, service):
        """Regression: this used to produce COMPLETE (PASS) out of nothing."""
        run_id = compose(service, "Auth-SSO")
        with pytest.raises(Conflict, match=r"nothing to\s+finalize"):
            service.finalize(run_id)
        assert service.require_run(run_id).state == "COMPOSED"

    def test_a_running_dispatch_cannot_be_finalized(self, scripted):
        started, release = threading.Event(), threading.Event()

        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            started.set()
            assert release.wait(30)
            all_pass_delivery(ctx)

        def all_pass_delivery(ctx: DispatchContext) -> None:
            ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        outcome = service.dispatch(run_id, "fake")
        assert started.wait(30)
        with pytest.raises(RunActive, match="is RUNNING"):
            service.finalize(run_id)
        release.set()
        assert outcome.done.wait(30)

    def test_finalizing_an_awaiting_run_gives_up_the_outstanding_shards(self, scripted):
        def script(ctx: DispatchContext) -> None:
            ctx.declare(["a", "b"])
            ctx.deliver("a", verdicts=[Verdict(ctx.item_ids[0], "PASS")])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "fake", foreground=True)
        run = service.finalize(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "INCOMPLETE")
        assert dict(run.latest_dispatch.summary) == {"PASS": 1, "MISSING": 2}
        completed = events_of(service, run_id, "run.completed")[0]
        assert completed.data["finalized"] is True
        assert completed.data["outstanding_shards"] == ["b"]

    def test_finalizing_an_export_nobody_answered_is_incomplete(self, service):
        run_id = compose(service, "Auth-SSO")
        service.export(run_id)
        run = service.finalize(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "INCOMPLETE")
        assert dict(run.latest_dispatch.summary) == {"MISSING": 3}


def test_configured_options_reach_the_context_with_secrets_resolved(scripted, monkeypatch):
    monkeypatch.setenv("SHOP_FIXED_VALUE", "from-the-environment")
    seen: dict[str, Any] = {}

    def script(ctx: DispatchContext) -> None:
        seen.update(ctx.options)
        all_pass(ctx)

    service = scripted(script, fixed="${env:SHOP_FIXED_VALUE}")
    run_id = compose(service, "Auth-SSO")
    service.dispatch(run_id, "fake", foreground=True)
    assert seen["fixed"] == "from-the-environment"
    # …and never travel: not in the document, not in the runner listing.
    assert "from-the-environment" not in json.dumps(service.require_spec(run_id))
    assert "from-the-environment" not in json.dumps(service.runner_infos())


def test_write_config_helper_is_what_these_tests_think_it_is(tmp_path):
    path = write_config(tmp_path, core={"dispatch": {"max_concurrent": 3}})
    config = load_config(str(path))
    assert config.dispatch["max_concurrent"] == 3
    assert config.inbox_dir is None and config.runner_ids() == ["demo"]


def test_run_detail_reads_a_bounded_tail_of_the_event_stream(scripted, monkeypatch):
    """Regression: the run detail shows the last 200 events — and reads only
    those (``RunStore.latest_events``), never the whole stream."""
    service = scripted(all_pass)
    run_id = compose(service, "Auth-SSO")
    for n in range(260):
        service.store.append_event(run_id, kind="log", at="2026-07-07T10:00:00Z", data={"n": n})
    every = service.store.events(run_id, limit=10_000)

    def whole_stream(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the run detail read the event stream unbounded")

    monkeypatch.setattr(service.store, "events", whole_stream)
    detail = run_detail(service, service.require_run(run_id))
    assert len(detail["events"]) == 200
    assert [e["seq"] for e in detail["events"]] == [e.seq for e in every[-200:]]
    assert detail["last_event_seq"] == every[-1].seq
