"""``runcomposer.testing`` — what the kit gained with the grown runner
contract: the plan and group rules of the checks, the fake's whole live
stream and its view of "now", a fake over a real catalog, and a harness that
dispatches in the background or serves the API."""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from typing import Any

import pytest

from runcomposer.plugins.sqlite_store import SqliteRunStore
from runcomposer.sdk import (
    DispatchContext,
    InvalidParameter,
    Item,
    Parameter,
    PlanContext,
    PlanPreview,
    RunnerBase,
    RunnerInfo,
    Verdict,
)
from runcomposer.testing import (
    ConnectorHarness,
    DispatchHandle,
    FakeDispatchContext,
    ListSource,
    check_runner,
    check_store,
)

CART = ["Shop.Cart.Core.T001", "Shop.Cart.Core.T002", "Shop.Cart.Core.T003"]
HERE = "tests.test_kit_additions"


def rule_of(error: pytest.ExceptionInfo[AssertionError]) -> str:
    return str(error.value).split("]", 1)[0].lstrip("[")


# -- check_runner: the plan rules ------------------------------------------------------


class _Planner(RunnerBase):
    shards = ("eu", "us")

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="planner", capabilities=("plan",))

    def split(self, ctx: PlanContext) -> dict[str, list[str]]:
        return {"eu": list(ctx.item_ids[::2]), "us": list(ctx.item_ids[1::2])}

    def plan(self, ctx: PlanContext) -> PlanPreview:
        return PlanPreview(text="two regions", shards=self.split(ctx), groups={"eu": "emea", "us": "amer"})

    def dispatch(self, ctx: DispatchContext) -> None:
        shards = self.split(ctx)
        ctx.declare(shards, groups={"eu": "emea", "us": "amer"})
        for shard, items in shards.items():
            ctx.deliver(shard, verdicts=[Verdict(item, "PASS") for item in items])


class PlannerThatDeclares(_Planner):
    def plan(self, ctx: PlanContext) -> PlanPreview:
        ctx.declare(["eu"])  # type: ignore[attr-defined]
        return super().plan(ctx)


class PlannerThatLogs(_Planner):
    def plan(self, ctx: PlanContext) -> PlanPreview:
        ctx.log("planning")  # type: ignore[attr-defined]
        return super().plan(ctx)


class PlannerPreviewingOtherShards(_Planner):
    def plan(self, ctx: PlanContext) -> PlanPreview:
        return PlanPreview(text="one lane", shards={"all": list(ctx.item_ids)})


class PlannerListingStrangers(_Planner):
    def plan(self, ctx: PlanContext) -> PlanPreview:
        return PlanPreview(shards={"eu": ["Shop.Not.Planned"], "us": []})


class PlannerLabellingUnknownShards(_Planner):
    def plan(self, ctx: PlanContext) -> PlanPreview:
        return PlanPreview(shards=self.split(ctx), groups={"apac": "apac"})


class PlannerThatRaises(_Planner):
    def plan(self, ctx: PlanContext) -> PlanPreview:
        raise KeyError("region_map")


class PlannerReturningText(_Planner):
    def plan(self, ctx: PlanContext) -> PlanPreview:
        return "two regions"  # type: ignore[return-value]


class PlannerRefusingWhatItThenRuns(_Planner):
    def plan(self, ctx: PlanContext) -> PlanPreview:
        ctx.refuse("the regions are closed")


class NoPlanCapability(_Planner):
    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="quiet")

    def plan(self, ctx: PlanContext) -> PlanPreview:
        raise AssertionError("a runner that does not declare `plan` is not asked for one")


class TestThePlanRules:
    def test_a_well_behaved_planner_passes_and_its_groups_are_recorded(self):
        ctx = check_runner(_Planner(), expect="complete")
        assert ctx.shard_groups == {"eu": "emea", "us": "amer"}

    @pytest.mark.parametrize(
        ("runner", "rule", "says"),
        [
            (PlannerThatDeclares, "runner.plan-is-read-only", "no declare, deliver, artifacts, token or work directory"),
            (PlannerThatLogs, "runner.plan-is-read-only", "reached for something a preview does not have"),
            (PlannerPreviewingOtherShards, "runner.plan-matches-dispatch",
             "previewed the shards ['all'], the dispatch declared ['eu', 'us']"),
            (PlannerListingStrangers, "runner.plan", "lists item(s) that were not planned: ['Shop.Not.Planned']"),
            (PlannerLabellingUnknownShards, "runner.plan", "labels shard(s) the preview does not list: ['apac']"),
            (PlannerThatRaises, "runner.plan", "plan() raised KeyError: 'region_map'"),
            (PlannerReturningText, "runner.plan", "plan() must return a PlanPreview, got str"),
        ],
    )
    def test_each_broken_planner_is_caught_with_its_rule_named(self, runner, rule, says):
        with pytest.raises(AssertionError) as caught:
            check_runner(runner())
        assert rule_of(caught) == rule and says in str(caught.value)

    def test_a_plan_may_refuse_and_a_runner_without_the_capability_is_not_asked(self):
        check_runner(PlannerRefusingWhatItThenRuns(), expect="complete")
        check_runner(NoPlanCapability(), expect="complete")


# -- check_observer: an observer that passes on only some kinds ----------------------------


class FinalOnly:
    """Keeps a line per event that ends a dispatch — and nothing else."""

    OPTIONS = None
    CONNECTOR = None

    def __init__(self, drop: bool = False) -> None:
        self.lines: list[str] = []
        self.drop = drop
        self._lock = threading.Lock()

    @classmethod
    def resolve_config_paths(cls, options: dict[str, Any], resolve: Any) -> dict[str, Any]:
        return options

    def notify(self, event: Any) -> None:
        if event.kind in ("run.completed", "run.failed") and not (self.drop and self.lines):
            with self._lock:
                self.lines.append(event.kind)


class TestObserverKinds:
    def test_an_observer_that_filters_is_held_to_exactly_what_it_passes_on(self):
        from runcomposer.testing import check_observer

        observer = FinalOnly()
        check_observer(observer, seen=lambda: observer.lines, kinds=["run.completed", "run.failed"], threads=3)
        assert len(observer.lines) == 2 * 4
        patterned = FinalOnly()
        with pytest.raises(AssertionError) as caught:   # run.* is more than it passes on
            check_observer(patterned, seen=lambda: patterned.lines, kinds=["run.*"])
        assert rule_of(caught) == "observer.delivers-each" and "of the kinds ['run.*']" in str(caught.value)

    def test_without_kinds_a_filtering_observer_cannot_pass_and_a_dropping_one_is_caught_with_them(self):
        from runcomposer.testing import check_observer

        observer = FinalOnly()
        with pytest.raises(AssertionError) as caught:
            check_observer(observer, seen=lambda: observer.lines)
        assert rule_of(caught) == "observer.delivers-each"
        dropping = FinalOnly(drop=True)
        with pytest.raises(AssertionError) as caught:
            check_observer(dropping, seen=lambda: dropping.lines, kinds=["run.completed", "run.failed"])
        assert rule_of(caught) == "observer.delivers-each"
        with pytest.raises(AssertionError) as caught:
            check_observer(FinalOnly(), seen=lambda: [], kinds=["shop.restocked"])
        assert rule_of(caught) == "observer.kinds"


# -- check_store: groups, RUNNING, statuses, seeds -----------------------------------------


def make_sqlite() -> SqliteRunStore:
    return SqliteRunStore(":memory:")


def wrapped(**overrides: Any):
    """A sqlite store with some methods replaced — a store broken in one way."""

    def make() -> Any:
        store = make_sqlite()
        for name, replacement in overrides.items():
            setattr(store, name, replacement.__get__(store))
        return store

    return make


def _dropping_groups(self: Any, dispatch_id: str, **changes: Any) -> Any:
    changes.pop("shard_groups", None)
    return type(self).update_dispatch(self, dispatch_id, **changes)


def _refusing_running(self: Any, run_id: str, dispatch_id: str, shard: str, verdict: Any) -> None:
    if verdict.status == "RUNNING":
        return
    type(self).record_live_verdict(self, run_id, dispatch_id, shard, verdict)


def _ignoring_statuses(self: Any, *, labels: Any = None, last_n: int = 5, statuses: Any = None) -> dict:
    return type(self).duration_aggregates(self, labels=labels, last_n=last_n)


def _forgetting_seeds(self: Any, durations: Any, *, labels: Any = None) -> int:
    return len(durations)


def _dropping_artifact_groups(self: Any, run_id: str, dispatch_id: Any, **fields: Any) -> None:
    fields["group"] = None
    type(self).add_artifact_ref(self, run_id, dispatch_id, **fields)


class TestTheStoreRules:
    def test_the_reference_store_passes(self):
        check_store(make_sqlite)

    @pytest.mark.parametrize(
        ("overrides", "rule"),
        [
            ({"update_dispatch": _dropping_groups}, "store.shard-groups"),
            ({"record_live_verdict": _refusing_running}, "store.live-running"),
            ({"duration_aggregates": _ignoring_statuses}, "store.duration-statuses"),
            ({"seed_durations": _forgetting_seeds}, "store.duration-seeds"),
            ({"add_artifact_ref": _dropping_artifact_groups}, "store.artifacts"),
        ],
    )
    def test_each_store_that_falls_short_is_caught_with_its_rule_named(self, overrides, rule):
        with pytest.raises(AssertionError) as caught:
            check_store(wrapped(**overrides))
        assert rule_of(caught) == rule


# -- FakeDispatchContext ----------------------------------------------------------------

_WORKER = """
import pickle, sys
channel = pickle.loads(bytes.fromhex(sys.argv[1]))
channel.live("a", sys.argv[2], "RUNNING")
channel.log("worker: started")
channel.live("a", sys.argv[2], "PASS", duration_ms=9)
"""


class _SlowRunner(RunnerBase):
    """Declares, then waits for the flag; its cancel(ctx) is counted by the kit."""

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="cancellable", capabilities=("cancel",))

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        while not ctx.cancelled:
            threading.Event().wait(0.01)
        ctx.raise_if_cancelled()

    def cancel(self, ctx: DispatchContext) -> None:
        return None


class _RefusingRunner(RunnerBase):
    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="refusing-a-value")

    def dispatch(self, ctx: DispatchContext) -> None:
        raise InvalidParameter("stock", "no planned pick is for this value", problem="runner")


class TestTheFake:
    def test_cancel_calls_counts_the_kits_calls_of_cancel_not_the_flag(self):
        ctx = check_runner(_SlowRunner(), FakeDispatchContext(CART), cancel_after_declare=True, expect="cancelled")
        assert ctx.cancel_calls == 1
        other = FakeDispatchContext(CART)
        other.request_cancel()
        assert other.cancelled and other.cancel_calls == 0

    def test_run_raises_an_invalid_parameter_the_runner_raises(self):
        ctx = FakeDispatchContext(CART)
        with pytest.raises(InvalidParameter) as raised:
            ctx.run(_RefusingRunner())
        assert raised.value.parameter == "stock" and ctx.refusal is None

    def test_the_whole_live_stream_is_kept_and_live_now_is_what_a_store_holds(self):
        """Every status a runner streams stays readable — also for a shard
        that has delivered meanwhile, whose live status a store drops."""
        ctx = FakeDispatchContext(CART)
        ctx.declare({"a": CART[:2], "b": CART[2:]})
        channel = pickle.dumps(ctx.live_channel()).hex()
        subprocess.run([sys.executable, "-c", _WORKER, channel, CART[0]], check=True, timeout=60)
        ctx.live("a", CART[1], "RUNNING")
        ctx.live("b", CART[2], "RUNNING")
        ctx.live("b", "Shop.Not.Planned", "RUNNING")
        ctx.live("b", CART[2], "EXPLODED")
        assert [(s.shard, s.item_id, s.status) for s in ctx.live_now] == [
            ("a", CART[0], "PASS"), ("a", CART[1], "RUNNING"), ("b", CART[2], "RUNNING"),
        ]
        ctx.deliver("a", verdicts=[Verdict(CART[0], "PASS"), Verdict(CART[1], "PASS")])
        assert [(s.shard, s.item_id, s.status) for s in ctx.live_now] == [("b", CART[2], "RUNNING")]
        assert [(s.shard, s.item_id, s.status) for s in ctx.live_updates] == [
            ("a", CART[0], "RUNNING"), ("a", CART[0], "PASS"), ("a", CART[1], "RUNNING"), ("b", CART[2], "RUNNING"),
        ]
        assert ("info", "worker: started") in ctx.logs
        ctx.close()

    def test_options_and_parameters_are_apart_as_in_runcomposer(self):
        ctx = FakeDispatchContext(CART, options={"stages": ["eu", "us"], "retries": 1}, parameters={"stages": ["eu"]})
        assert dict(ctx.options) == {"stages": ["eu", "us"], "retries": 1}
        assert dict(ctx.parameters) == {"stages": ["eu"]}
        assert ctx.spec["runner"]["runner"] == {"stages": ["eu"]}

    def test_parameters_are_validated_and_defaulted_for_the_runner(self):
        """``ctx.parameters`` holds in the fake what it holds in runcomposer:
        the declared defaults applied, and nothing the runner does not accept."""
        from runcomposer.sdk import InvalidParameter

        seen: list[dict[str, Any]] = []

        class Regional(RunnerBase):
            def describe(self) -> RunnerInfo:
                return RunnerInfo(id="regional", capabilities=("plan",), parameters=(
                    Parameter("region", kind="choice", choices=("eu", "us"), default="eu"),
                    Parameter("retries", kind="integer", default=1),
                ))

            def plan(self, ctx: PlanContext) -> PlanPreview:
                return PlanPreview(shards={ctx.parameters["region"]: list(ctx.item_ids)})

            def dispatch(self, ctx: DispatchContext) -> None:
                seen.append(dict(ctx.parameters))
                ctx.declare([ctx.parameters["region"]])
                ctx.deliver(ctx.parameters["region"], verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        ctx = FakeDispatchContext(CART, parameters={"retries": 3})
        assert ctx.preview(Regional()).shards == {"eu": CART} and ctx.run(Regional()) == "complete"
        assert seen == [{"region": "eu", "retries": 3}] and ctx.spec["runner"]["runner"] == seen[0]
        check_runner(Regional(), expect="complete")
        with pytest.raises(InvalidParameter, match="unknown parameter 'speed'"):
            FakeDispatchContext(CART, parameters={"speed": "fast"}).run(Regional())

    def test_groups_links_and_the_private_directory(self):
        ctx = FakeDispatchContext(CART)
        ctx.set_link("ticket", "https://desk.shop.example/t/1")
        with pytest.raises(ValueError, match="groups names shard"):
            ctx.declare(["a"], groups={"b": "x"})
        ctx.declare(["a", "b"], links={"queue": "https://ci.shop.example/queue/item/7/"}, groups={"a": "eu", "b": "eu"})
        ctx.set_link("build", "https://ci.shop.example/job/nightly/42")
        assert ctx.shard_groups == {"a": "eu", "b": "eu"}
        assert dict(ctx.links) == {
            "ticket": "https://desk.shop.example/t/1",
            "queue": "https://ci.shop.example/queue/item/7/",
            "build": "https://ci.shop.example/job/nightly/42",
        }
        private, work = ctx.private_dir, ctx.workdir
        assert private.is_dir() and private != work and work not in private.parents
        ctx.add_artifact("log (eu)", work / "eu.html", group="eu")
        assert (ctx.artifacts[0].group, ctx.artifacts[0].shard) == ("eu", None)
        assert ctx.delivered_shards() == set() and ctx.outstanding_shards() == ["a", "b"]
        ctx.deliver("b", verdicts=[Verdict(CART[0], "PASS")])
        assert ctx.delivered_shards() == {"b"} and ctx.outstanding_shards() == ["a"] and ctx.outstanding == ["a"]
        ctx.close()
        assert not private.exists() and not work.exists()

    def test_a_preview_is_given_a_plan_context_and_records_a_refusal(self):
        ctx = FakeDispatchContext(CART)
        preview = ctx.preview(_Planner())
        assert preview.shards == {"eu": [CART[0], CART[2]], "us": [CART[1]]}
        assert not ctx.is_declared and ctx.refusal is None
        assert ctx.preview(PlannerRefusingWhatItThenRuns()) is None and ctx.refusal == "the regions are closed"
        with pytest.raises(AttributeError, match="declare"):
            ctx.preview(PlannerThatDeclares())

    def test_from_source_gives_a_runner_the_real_catalog(self):
        """A runner that reads ``Item.meta`` or attributes items to where
        they came from needs the source's own items, not bare ids."""
        items = [
            Item("shop.cart.add", tags=("Cart", "Smoke"), meta={"path": "/cart", "method": "POST"}),
            Item("shop.cart.remove", tags=("Cart",), meta={"path": "/cart/1", "method": "DELETE"}),
            Item("shop.checkout.pay", tags=("Checkout", "Smoke"), meta={"path": "/pay", "method": "POST"}),
        ]
        source = ListSource(items)
        everything = FakeDispatchContext.from_source(source)
        assert everything.item_ids == ("shop.cart.add", "shop.cart.remove", "shop.checkout.pay")
        assert everything.source is source
        smoke = FakeDispatchContext.from_source(source, "Smoke", parameters={"region": "eu"})
        assert smoke.item_ids == ("shop.cart.add", "shop.checkout.pay") and dict(smoke.parameters) == {"region": "eu"}
        expression = FakeDispatchContext.from_source(source, {"op": "AND", "items": ["Cart", {"not": "Smoke"}]})
        assert expression.item_ids == ("shop.cart.remove",)
        picked = FakeDispatchContext.from_source(source, ["shop.checkout.pay", "shop.cart.add"])
        assert picked.item_ids == ("shop.checkout.pay", "shop.cart.add")
        assert {i.id: i.meta["method"] for i in picked.source.items()}["shop.cart.remove"] == "DELETE"
        assert picked.spec["source"]["snapshot"] == source.snapshot(), "no drift between the document and the source"
        with pytest.raises(ValueError, match="the source has no item"):
            FakeDispatchContext.from_source(source, ["shop.cart.gone"])


class TestSmallHelpers:
    def test_plugin_builds_an_installed_plugin_by_its_config_name(self, tmp_path):
        from runcomposer.testing import plugin

        manifest = tmp_path / "catalog.json"
        manifest.write_text('{"items": [{"id": "shop.cart.add", "tags": ["Cart"]}]}', encoding="utf-8")
        source = plugin("sources", "manifest", path=str(manifest))
        assert [item.id for item in source.items()] == ["shop.cart.add"]
        assert FakeDispatchContext.from_source(source, "Cart").item_ids == ("shop.cart.add",)
        assert plugin("parsers", "junit-xml").format_id == "junit-xml"
        with pytest.raises(ValueError, match="unknown section 'plugins'"):
            plugin("plugins", "manifest")
        # The entry-point group is `.stores`; the section a config writes is `store`.
        with pytest.raises(ValueError, match="unknown section 'stores' — did you mean 'store'"):
            plugin("stores", "sqlite")
        with pytest.raises(ValueError, match="did you mean 'sources'"):
            plugin("source", "manifest")

    def test_corpus_drifted_asks_the_question_every_runner_asks(self):
        from runcomposer.sdk import corpus_drifted

        ctx = FakeDispatchContext(CART)
        assert corpus_drifted(ctx) is False
        moved = FakeDispatchContext(spec=dict(ctx.spec), source=ListSource([Item(CART[0]), Item("Shop.Cart.Core.T009")]))
        assert corpus_drifted(moved) is True

    def test_the_jenkins_stand_in_is_part_of_the_kit(self):
        """A connector that drives Jenkins tests its runner against the same
        stand-in the reference runner is tested with."""
        import runcomposer.testing.jenkins as stand_in
        from runcomposer.sdk.jenkins import JenkinsClient

        assert set(stand_in.__all__) == {"FakeJenkins", "Build", "QueueItem", "Seen"}
        fake = stand_in.FakeJenkins()
        try:
            fake.add_job("shop-nightly", lambda build: build.pages.update(consoleText=b"3 passed"))
            client = JenkinsClient(fake.url, retry_backoff_s=0.01)
            build = client.follow("shop-nightly", client.trigger("shop-nightly", {"REGION": "eu"}),
                                  timeout_s=10, poll_interval_s=0.01)
            assert fake.triggers()[0].form == {"REGION": "eu"} and build["result"] == "SUCCESS"
            assert fake.builds[("shop-nightly", 1)].params == {"REGION": "eu"}
        finally:
            fake.close()

    def test_the_stand_in_answers_as_the_jenkins_version_the_real_check_runs_against(self):
        import re

        from runcomposer.testing.jenkins import FakeJenkins

        from .conftest import REPO

        pinned = re.search(r"^FROM jenkins/jenkins:([0-9.]+)", (REPO / "ci/jenkins/Dockerfile").read_text(), re.M)
        assert pinned and pinned.group(1) == FakeJenkins.VERSION

    def test_the_kit_and_its_stand_in_import_without_pytest(self):
        code = ("import sys, runcomposer.testing, runcomposer.testing.jenkins; "
                "assert 'pytest' not in sys.modules and '_pytest' not in sys.modules")
        subprocess.run([sys.executable, "-c", code], check=True, timeout=60)


# -- ConnectorHarness: in the background, and served ---------------------------------------

GATES: dict[str, threading.Event] = {}


class GatedRunner(RunnerBase):
    """Declares, streams one RUNNING, then waits for its gate (or a cancel)."""

    def __init__(self, gate: str = "gate", callback_base: str = ""):
        self.gate, self.callback_base = gate, callback_base

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="gated",
            capabilities=("cancel", "live_status", "plan"),
            parameters=(Parameter("refuse", kind="boolean", default=False),),
        )

    def plan(self, ctx: PlanContext) -> PlanPreview:
        return PlanPreview(text="one shard", shards={"1": list(ctx.item_ids)})

    def dispatch(self, ctx: DispatchContext) -> None:
        if ctx.parameters.get("refuse"):
            ctx.refuse("not today")
        ctx.declare({"1": list(ctx.item_ids)}, links={"callback": self.callback_base or "none"})
        ctx.live("1", ctx.item_ids[0], "RUNNING")
        while not GATES[self.gate].wait(0.01):
            ctx.raise_if_cancelled()
        ctx.deliver("1", verdicts=[Verdict(item, "PASS") for item in ctx.item_ids])


def gated(name: str, **options: Any) -> dict[str, Any]:
    GATES[name] = threading.Event()
    return {"gated": {"module": f"{HERE}:GatedRunner", "gate": name, **options}}


class TestTheHarnessInTheBackground:
    def test_start_returns_a_handle_to_wait_on_a_condition_then_let_it_end(self):
        with ConnectorHarness(runners=gated("end")) as harness:
            handle = harness.start("Cart", runner="gated")
            assert isinstance(handle, DispatchHandle) and not handle.done
            mid = handle.wait_until(lambda run: any(s.status == "RUNNING" for s in run.live), timeout=30)
            assert mid.state == "RUNNING" and mid.links == {"callback": "none"}
            assert mid.dispatch.dispatch_id == handle.dispatch_id and mid.verdicts == []
            GATES["end"].set()
            done = handle.wait(timeout=30)
            assert handle.done and (done.state, done.completion) == ("COMPLETE", "PASS") and done.live == []

    def test_cancel_through_the_handle(self):
        with ConnectorHarness(runners=gated("cancel")) as harness:
            handle = harness.start("Cart", runner="gated")
            handle.wait_until(lambda run: run.live, timeout=30)
            assert handle.cancel().cancel_requested
            assert handle.wait(timeout=30).state == "CANCELLED"

    def test_a_condition_that_is_never_met_fails_saying_where_the_run_stood(self):
        with ConnectorHarness(runners=gated("never")) as harness:
            handle = harness.start("Cart", runner="gated")
            with pytest.raises(AssertionError) as caught:
                handle.wait_until(lambda run: "build" in run.links, timeout=0.5)
            message = str(caught.value)
            assert "was not met within 0.5s" in message and "is RUNNING, runner still executing" in message
            assert "run.running" in message
            handle.cancel()
            handle.wait(timeout=30)

    def test_a_refusal_ends_the_wait_at_once(self):
        with ConnectorHarness(runners=gated("refused")) as harness:
            handle = harness.start("Cart", runner="gated", parameters={"refuse": True})
            result = handle.wait(timeout=30)
            assert handle.done and handle.refusal == "not today" and result.refusal == "not today"
            assert result.state == "COMPOSED" and result.dispatch is None

    def test_a_stored_run_can_be_started_and_planned(self):
        with ConnectorHarness(runners=gated("refuse")) as harness:
            run_id = harness.compose("Cart", runner="gated")
            handle = harness.start(runner="gated", run_id=run_id, parameters=None)
            GATES["refuse"].set()
            assert handle.wait(timeout=30).state == "COMPLETE" and handle.refusal is None
            assert harness.plan(runner="gated", run_id=run_id).preview.shards["1"]
            assert harness.plan("Cart", runner="gated").preview.text == "one shard"


class TestTheServingHarness:
    def test_the_api_is_reachable_and_its_address_is_known_to_the_plugins(self):
        before = os.environ.get("RUNCOMPOSER_HARNESS_URL")
        with ConnectorHarness(
            runners=gated("served", callback_base="${env:RUNCOMPOSER_HARNESS_URL}"), serve=True
        ) as harness:
            assert harness.url.startswith("http://127.0.0.1:") and harness.api == f"{harness.url}/api/v1"
            assert harness.artifacts("01NOSUCHRUN000000000000000") == []
            assert harness.service.runner("gated").callback_base == harness.url
            with urllib.request.urlopen(f"{harness.api}/health", timeout=10) as response:
                assert response.status == 200
            handle = harness.start("Cart", runner="gated")
            handle.wait_until(lambda run: run.live, timeout=30)
            import json

            with urllib.request.urlopen(f"{harness.api}/runs/{handle.run_id}", timeout=10) as response:
                detail = json.loads(response.read())
            assert detail["state"] == "RUNNING" and detail["live_summary"] == {"1": {"RUNNING": 1}}
            GATES["served"].set()
            handle.wait(timeout=30)
            address = harness.url
        assert os.environ.get("RUNCOMPOSER_HARNESS_URL") == before
        with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):
            urllib.request.urlopen(f"{address}/api/v1/health", timeout=2)

    def test_a_harness_that_does_not_serve_has_no_address(self):
        with ConnectorHarness() as harness:
            assert harness.url is None and harness.api is None
