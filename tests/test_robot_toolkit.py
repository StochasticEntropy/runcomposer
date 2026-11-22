"""``runcomposer.sdk.robot`` — the Robot Framework toolkit — and the
``robot-pool`` switches built on it (DESIGN.md §6.2a).

Three things are shown here, each with real Robot Framework:

1. the building blocks work on their own (selection, the worker and its
   hand-back, lanes, the stop protocol, merging without merge notes, the
   execution mode for roots that mix tests and tasks);
2. ``robot-pool`` can be *configured* into a runner that distributes a
   selection over partitions, runs each partition's chunks as shards of
   their own, prepares the partitions before the hand-off and takes extra
   per-run parameters;
3. the same runner can be *composed* from the toolkit in a few dozen lines —
   by a class that imports ``runcomposer.sdk`` and ``runcomposer.sdk.robot``
   and nothing else, as a connector outside this repository would.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from runcomposer.sdk import (
    DispatchContext,
    PlanContext,
    PlanPreview,
    RunnerBase,
    RunnerInfo,
    plan_chunks,
    split_max,
)
from runcomposer.sdk.robot import (
    RESULT_FILE,
    ExactSelection,
    Execution,
    LiveStatusListener,
    ShardOutcome,
    ShardPayload,
    attribute_roots,
    deliver_shard,
    merge_outputs,
    read_shard_result,
    robot_version,
    run_shard,
    supervise,
)
from runcomposer.testing import ConnectorHarness, FakeDispatchContext, check_runner

from .conftest import needs_robot, wait_until
from .test_robot_pool import (
    FIVE_TESTS,
    QUICK,
    SLOW,
    events_of,
    execute,
    executed_tests,
    pool_service,
    shop_suite,
    staged_suite,
    workdir,
    write_suite,
)

pytestmark = needs_robot

CART = [
    "Shop.Cart.Cart Holds One Item",
    "Shop.Cart.Cart Holds Two Items",
    "Shop.Cart.Cart Rejects A Negative Quantity",
    "Shop.Cart.Cart Survives A Reload",
    "Shop.Cart.Cart Is Emptied On Checkout",
]


def cart_root(tmp_path: Path) -> Path:
    return shop_suite(tmp_path, FIVE_TESTS, name="cart.robot")


def payload(root: Path, out_dir: Path, ids: list[str], **more: Any) -> ShardPayload:
    return ShardPayload(
        shard=more.pop("shard", "default"),
        out_dir=str(out_dir),
        executions=more.pop("executions", (Execution(str(root), tuple(ids)),)),
        **more,
    )


class RecordingChannel:
    """A LiveChannel that keeps what it is told (in-process use only)."""

    def __init__(self) -> None:
        self.statuses: list[tuple[str, str, str]] = []
        self.lines: list[tuple[str, str]] = []

    def live(self, shard: str, item_id: str, status: str, **_more: Any) -> None:
        self.statuses.append((shard, item_id, status))

    def log(self, message: str, level: str = "info") -> None:
        self.lines.append((level, message))


# -- 1. the building blocks -------------------------------------------------------------


class TestTheWorker:
    def test_it_executes_exactly_the_given_ids_and_hands_back_what_it_produced(self, tmp_path):
        channel = RecordingChannel()
        out_dir = tmp_path / "out"
        run_shard(payload(cart_root(tmp_path), out_dir, CART[:2], channel=channel, executed_tag="ran"))

        result = read_shard_result(out_dir)
        assert result is not None and (result.shard, result.error, result.stopped) == ("default", None, False)
        (output,) = result.outputs
        assert (output.attempt, output.selected, output.tests) == (1, 2, 2)
        ran = executed_tests(Path(output.path))
        assert set(ran) == set(CART[:2]) and all("ran" in tags for _status, tags, _msg in ran.values())
        assert (out_dir / "log.html").is_file() and (out_dir / "report.html").is_file()
        # RUNNING when a test starts, its status when it ends — in that order.
        assert channel.statuses == [
            ("default", CART[0], "RUNNING"), ("default", CART[0], "PASS"),
            ("default", CART[1], "RUNNING"), ("default", CART[1], "PASS"),
        ]

    def test_a_selected_id_that_is_no_test_under_the_root_is_counted_not_invented(self, tmp_path):
        out_dir = tmp_path / "out"
        run_shard(payload(cart_root(tmp_path), out_dir, [CART[0], "Shop.Cart.No Such Test"]))
        (output,) = read_shard_result(out_dir).outputs
        assert (output.selected, output.tests) == (2, 1)

    def test_an_id_is_never_read_as_a_pattern(self, tmp_path):
        root = shop_suite(
            tmp_path,
            "*** Test Cases ***\n"
            "Price [EUR] Is Shown\n    Log    ok\n\n"
            "Price E Is Shown\n    Log    ok\n\n"
            "Price * Is Shown\n    Log    ok\n",
            name="prices.robot",
        )
        selection = ExactSelection(["Shop.Prices.Price [EUR] Is Shown"])
        out_dir = tmp_path / "out"
        run_shard(payload(root, out_dir, ["Shop.Prices.Price [EUR] Is Shown"]))
        assert set(executed_tests(out_dir / "output.xml")) == {"Shop.Prices.Price [EUR] Is Shown"}
        assert selection.ids == {"Shop.Prices.Price [EUR] Is Shown"} and selection.kept == 0

    def test_several_executions_are_merged_without_merge_notes(self, tmp_path):
        """Chunks of one run are disjoint: a merge stamps every test of a
        later chunk "added from merged output" — in front of its own message.
        The merged result people read carries the tests' own messages."""
        root, out_dir = cart_root(tmp_path), tmp_path / "out"
        channel = RecordingChannel()
        run_shard(
            payload(
                root, out_dir, [],
                executions=(Execution(str(root), tuple(CART[:2])), Execution(str(root), tuple(CART[2:]))),
                channel=channel,
            )
        )
        result = read_shard_result(out_dir)
        assert [(o.selected, o.tests) for o in result.outputs] == [(2, 2), (3, 3)]
        merged = executed_tests(out_dir / "output.xml")
        assert set(merged) == set(CART)
        assert all("merged output" not in message for _s, _t, message in merged.values())
        assert merged[CART[2]] == ("FAIL", ["Cart"], "quantity must be positive")
        log = (out_dir / "log.html").read_text(encoding="utf-8")
        assert "added from merged output" not in log
        assert ("info", "shard default: execution 2/2: 3 test(s)") in channel.lines

    def test_merge_outputs_alone_restores_plain_and_html_messages(self, tmp_path):
        root = shop_suite(
            tmp_path,
            "*** Test Cases ***\n"
            "First\n    Log    ok\n\n"
            "Plain Failure\n    Fail    total <b>must</b> be 42 & positive\n\n"
            "Html Failure\n    Fail    *HTML* see <a href=\"https://shop.example/help\">the help</a>\n\n"
            "Quiet\n    Log    ok\n",
            name="messages.robot",
        )
        ids = [f"Shop.Messages.{name}" for name in ("First", "Plain Failure", "Html Failure", "Quiet")]
        for index, chunk in enumerate((ids[:1], ids[1:]), start=1):
            run_shard(payload(root, tmp_path / f"piece-{index}", chunk, log=False, report=False))
        merged = merge_outputs(
            [tmp_path / "piece-1" / "output.xml", tmp_path / "piece-2" / "output.xml"],
            tmp_path / "merged", log=False, report=False,
        )
        tests = executed_tests(merged)
        assert tests[ids[1]][2] == "total <b>must</b> be 42 & positive"
        assert tests[ids[2]][2] == '*HTML* see <a href="https://shop.example/help">the help</a>'
        assert tests[ids[3]][2] == ""

    def test_a_reexecuted_test_keeps_the_note_that_it_was(self, tmp_path):
        """What is removed is the noise, not the information: a test that
        really ran twice still says so in the merged result."""
        root, out_dir = cart_root(tmp_path), tmp_path / "out"
        run_shard(payload(root, out_dir, CART[:3], rerun_failed=1))
        result = read_shard_result(out_dir)
        assert [(o.attempt, o.selected) for o in result.outputs] == [(1, 3), (2, 1)]
        merged = executed_tests(out_dir / "output.xml")
        assert "re-executed" in merged[CART[2]][2] and "merged output" not in merged[CART[0]][2]

    def test_robot_options_reach_the_execution_and_the_toolkits_own_win(self, tmp_path):
        root = shop_suite(
            tmp_path,
            "*** Test Cases ***\nGreeting Is Localized\n    Should Be Equal    ${GREETING}    Moin\n",
            name="locale.robot",
        )
        out_dir = tmp_path / "out"
        run_shard(
            payload(
                root, out_dir, ["Shop.Locale.Greeting Is Localized"],
                variables={"GREETING": "Moin"},
                robot_options={"variable": ["GREETING:Hello"], "output": "elsewhere.xml", "loglevel": "DEBUG"},
            )
        )
        (output,) = read_shard_result(out_dir).outputs
        assert Path(output.path).name == "output.xml", "the toolkit's own options win"
        assert executed_tests(Path(output.path))["Shop.Locale.Greeting Is Localized"][0] == "PASS"


MIXED = {
    "checkout.robot": "*** Test Cases ***\nPay By Card\n    [Tags]    Checkout\n    Log    paying\n",
    "housekeeping.robot": "*** Tasks ***\nPurge Abandoned Carts\n    [Tags]    Catalog\n    Log    purging\n",
}
MIXED_IDS = ["Shop.Checkout.Pay By Card", "Shop.Housekeeping.Purge Abandoned Carts"]


def mixed_root(tmp_path: Path) -> Path:
    for name, text in MIXED.items():
        write_suite(tmp_path / "Shop", name, text)
    return tmp_path / "Shop"


class TestExecutionMode:
    """A root may hold suites of tests and suites of tasks. Robot refuses an
    execution that keeps both unless it is told which mode to use."""

    def test_left_to_robot_a_mixed_selection_is_refused_and_the_reason_is_handed_back(self, tmp_path):
        out_dir = tmp_path / "out"
        run_shard(payload(mixed_root(tmp_path), out_dir, MIXED_IDS))
        result = read_shard_result(out_dir)
        assert result.outputs == ()
        assert "Conflicting execution modes" in result.error and "rpa=False" in result.error
        outcome = ShardOutcome(payload(tmp_path, out_dir, MIXED_IDS), 0, result)
        assert "Conflicting execution modes" in outcome.problem

    @pytest.mark.parametrize("rpa", [False, True])
    def test_with_the_mode_set_both_execute(self, tmp_path, rpa):
        out_dir = tmp_path / "out"
        run_shard(payload(mixed_root(tmp_path), out_dir, MIXED_IDS, rpa=rpa))
        (output,) = read_shard_result(out_dir).outputs
        assert {name: status for name, (status, _t, _m) in executed_tests(Path(output.path)).items()} == {
            MIXED_IDS[0]: "PASS", MIXED_IDS[1]: "PASS",
        }

    def test_only_tests_selected_from_a_mixed_root_need_no_mode(self, tmp_path):
        out_dir = tmp_path / "out"
        run_shard(payload(mixed_root(tmp_path), out_dir, MIXED_IDS[:1]))
        assert read_shard_result(out_dir).outputs[0].tests == 1

    def test_robot_pool_takes_the_mode_as_an_option(self, make_service, tmp_path):
        root = mixed_root(tmp_path)
        strict = pool_service(make_service, root, max_workers=1)
        refused = execute(strict, {"tag_filter": {"op": "OR", "items": ["Checkout", "Catalog"]}})
        assert refused.state == "FAILED" and "Conflicting execution modes" in refused.latest_dispatch.error
        assert "rpa" in refused.latest_dispatch.error

    def test_robot_pool_with_rpa_false_executes_a_task_and_a_test_together(self, make_service, tmp_path):
        service = pool_service(make_service, mixed_root(tmp_path), max_workers=1, rpa=False)
        run = execute(service, {"tag_filter": {"op": "OR", "items": ["Checkout", "Catalog"]}})
        assert (run.state, run.completion, dict(run.latest_dispatch.summary)) == ("COMPLETE", "PASS", {"PASS": 2})


class TestTheSupervisor:
    def test_payloads_of_one_lane_never_overlap_and_keep_their_order(self, tmp_path):
        """A lane is something only one execution may use at a time. The
        second payload of a lane starts after the first was handed back; a
        payload of another lane does not wait for either."""
        root = cart_root(tmp_path)
        done_at: dict[str, float] = {}

        def finished(_payload: ShardPayload, outcome: ShardOutcome) -> None:
            done_at[outcome.payload.shard] = time.time()

        setup = ("tests.shop_callables:record_window",)
        payloads = [
            payload(root, tmp_path / "staging-01", CART[:2], shard="staging-01", lane="staging", worker_setup=setup),
            payload(root, tmp_path / "staging-02", CART[2:4], shard="staging-02", lane="staging", worker_setup=setup),
            payload(root, tmp_path / "preview-01", CART[4:], shard="preview-01", lane="preview", worker_setup=setup),
        ]
        outcomes = supervise(payloads, max_workers=3, on_done=finished)

        assert {o.payload.shard for o in outcomes} == {"staging-01", "staging-02", "preview-01"}
        assert all(o.problem is None and not o.timed_out for o in outcomes)
        started = {
            shard: json.loads((tmp_path / shard / "started.json").read_text(encoding="utf-8"))["at"]
            for shard in done_at
        }
        assert started["staging-02"] >= done_at["staging-01"], "the lane's second chunk waited for its first"
        assert started["preview-01"] < done_at["staging-01"], "another lane ran alongside"

    def test_a_cancellation_stops_a_running_worker_through_the_stop_protocol(self, tmp_path):
        root = staged_suite(tmp_path, 120)
        channel = RecordingChannel()
        from runcomposer.app.livechannel import FileLiveChannel, LiveTail

        live_dir = tmp_path / "live"
        live_dir.mkdir()
        tail, seen = LiveTail(live_dir), []

        def slow_is_running() -> bool:
            seen.extend(tail.read())
            return any(e.get("item_id") == SLOW and e.get("status") == "RUNNING" for e in seen)

        started = time.monotonic()
        outcomes = supervise(
            [payload(root, tmp_path / "out", [QUICK, SLOW], channel=FileLiveChannel(str(live_dir)))],
            max_workers=1,
            cancelled=slow_is_running,
        )
        assert time.monotonic() - started < 60, "the 120 s test was not waited for"
        assert outcomes == [] and channel.statuses == []
        result = read_shard_result(tmp_path / "out")
        assert result is not None and result.stopped, "the worker ended its execution and said why"
        assert (tmp_path / "out" / ".stop-requested").exists()

    def test_a_test_cut_short_by_a_stop_is_not_reported_as_having_failed(self, make_service, tmp_path):
        """Robot ends a stopped test as a failure. That is not something the
        test did: after a cancel the last thing streamed about it is that it
        was RUNNING — which is also what its MISSING verdict then says."""
        from runcomposer.app.views import run_detail

        service = pool_service(make_service, staged_suite(tmp_path, 120), max_workers=1)
        run_id = service.compose_run({"tag_filter": "Checkout"}, title="t", origin="test", runner_id="robot-pool").run.id
        outcome = service.dispatch(run_id, "robot-pool")
        wait_until(
            lambda: any(v.item_id == SLOW and v.status == "RUNNING"
                        for v in service.store.live_verdicts(run_id, outcome.dispatch_id)),
            timeout=90, what="the slow test to be running",
        )
        service.cancel(run_id)
        assert outcome.done.wait(60) and service.require_run(run_id).state == "CANCELLED"
        live = {v.item_id: v.status for v in service.store.live_verdicts(run_id, outcome.dispatch_id)}
        assert live == {QUICK: "PASS", SLOW: "RUNNING"}
        missing = {v["item_id"]: v for v in run_detail(service, service.require_run(run_id))["verdicts"]
                   if v["status"] == "MISSING"}
        assert missing[SLOW]["last_live"]["status"] == "RUNNING" and missing[QUICK]["last_live"]["status"] == "PASS"

    def test_a_shard_over_its_timeout_is_stopped_and_reported(self, tmp_path):
        root = staged_suite(tmp_path, 120)
        (outcome,) = supervise([payload(root, tmp_path / "out", [SLOW])], max_workers=1, timeout_s=3)
        assert outcome.timed_out and outcome.problem == "default: exceeded the shard timeout of 3s"

    def test_a_worker_that_dies_without_a_hand_back_is_a_problem_not_a_hang(self, tmp_path):
        (outcome,) = supervise(
            [payload(cart_root(tmp_path), tmp_path / "out", CART[:1],
                     worker_setup=("tests.test_robot_toolkit:die_at_once",))],
            max_workers=1,
        )
        assert outcome.result is None and outcome.exitcode == 3
        assert outcome.problem == "default: the worker process ended (exit code 3) without a result"


def die_at_once(info: dict) -> None:
    """A worker_setup that takes the whole process down, hand-back and all."""
    import os

    Path(info["out_dir"], RESULT_FILE).unlink(missing_ok=True)
    os._exit(3)


class TestSmallPieces:
    def test_robot_version_is_the_installed_one(self):
        import robot

        assert robot_version() == robot.__version__

    def test_attribute_roots_by_source_path_then_by_root_suite_name(self, tmp_path):
        from runcomposer.sdk import Item

        ui, api = tmp_path / "ui" / "Shop", tmp_path / "api" / "Orders"
        ui.mkdir(parents=True)
        api.mkdir(parents=True)
        items = [Item("Renamed.Cart.Add", meta={"source": str(ui / "cart.robot")})]
        assert attribute_roots([str(ui), str(api)], ["Renamed.Cart.Add", "Orders.List.All"], items) == {
            "Renamed.Cart.Add": str(ui), "Orders.List.All": str(api),
        }
        with pytest.raises(LookupError, match=r"'Billing\.Invoice' belongs to none of the configured suite roots"):
            attribute_roots([str(ui), str(api)], ["Billing.Invoice"])
        # A single root is checked as well: its root suite is named "Shop".
        assert attribute_roots([str(ui)], ["Shop.Cart.Add"]) == {"Shop.Cart.Add": str(ui)}
        with pytest.raises(LookupError, match=r"'Anything\.At\.All' is not under the suite root"):
            attribute_roots([str(ui)], ["Anything.At.All"])

    def test_the_listener_never_breaks_the_execution_it_reports_on(self):
        class Broken:
            def live(self, *_a: Any, **_k: Any) -> None:
                raise OSError("disk full")

        from types import SimpleNamespace

        listener = LiveStatusListener(Broken(), "1")
        listener.start_test(None, SimpleNamespace(longname=QUICK))
        listener.end_test(None, SimpleNamespace(longname=QUICK, status="PASS", elapsedtime=1, message=""))


# -- 2. robot-pool, configured -----------------------------------------------------------

SPLIT = {"partitions": ["staging", "preview"], "partition_mode": "split", "max_workers": 2}


def shards_of(service, run) -> dict[str, list[str]]:
    by_shard: dict[str, list[str]] = {}
    for verdict in service.store.verdicts_for(run.id, run.latest_dispatch.dispatch_id):
        by_shard.setdefault(verdict.shard, []).append(verdict.item_id)
    return by_shard


class TestPartitionModeSplit:
    def test_each_test_runs_once_distributed_over_the_partitions(self, make_service, tmp_path):
        service = pool_service(make_service, cart_root(tmp_path), executed_tag="RanOn-{partition}", **SPLIT)
        run = execute(service, "Cart")
        dispatch = run.latest_dispatch
        assert (run.state, run.completion) == ("COMPLETE", "FAIL")
        assert dict(dispatch.summary) == {"PASS": 4, "FAIL": 1}, "five tests, five verdicts — not ten"
        assert dispatch.shards == ("staging", "preview")
        assert dict(dispatch.shard_groups) == {"staging": "staging", "preview": "preview"}
        ran = shards_of(service, run)
        assert sorted(ran["staging"] + ran["preview"]) == sorted(CART)
        assert (len(ran["staging"]), len(ran["preview"])) == (3, 2)
        # Every test ran against exactly the partition its lane is.
        for partition in ("staging", "preview"):
            tests = executed_tests(workdir(service, run) / partition / "output.xml")
            assert set(tests) == set(ran[partition])
            assert all(f"RanOn-{partition}" in tags for _s, tags, _m in tests.values())
        assert "distributed over 2 of 2 partition(s) — each test once" in dispatch.plan

    def test_the_default_mode_still_runs_the_selection_once_per_partition(self, make_service, tmp_path):
        service = pool_service(make_service, cart_root(tmp_path), partitions=["staging", "preview"], max_workers=2)
        run = execute(service, "Cart")
        assert dict(run.latest_dispatch.summary) == {"PASS": 8, "FAIL": 2}
        assert dict(run.latest_dispatch.shard_groups) == {"staging-1": "staging", "preview-1": "preview"}

    def test_fewer_partitions_chosen_means_fewer_lanes(self, make_service, tmp_path):
        service = pool_service(make_service, cart_root(tmp_path), **SPLIT)
        run = execute(service, "Cart", {"partitions": ["preview"]})
        assert run.latest_dispatch.shards == ("preview",) and len(shards_of(service, run)["preview"]) == 5

    def test_history_balances_the_lanes_by_duration(self, make_service, tmp_path):
        service = pool_service(make_service, cart_root(tmp_path), **SPLIT)
        service.import_durations({CART[0]: 9000, CART[1]: 1000, CART[2]: 1000, CART[3]: 1000, CART[4]: 1000})
        preview = service.plan("robot-pool", selection={"tag_filter": "Cart"}).preview
        assert preview.shards == {"staging": [CART[0]], "preview": CART[1:]}
        assert preview.estimates_ms == {"staging": 9000.0, "preview": 4000.0}
        assert "duration-balanced" in preview.text

    def test_an_unknown_mode_is_a_config_error(self, make_service, tmp_path):
        from runcomposer.sdk import ConfigError

        with pytest.raises(ConfigError, match="partition_mode must be one of \\['each', 'split'\\], got 'spread'"):
            pool_service(make_service, cart_root(tmp_path), partition_mode="spread").validate()


CHUNKED = {**SPLIT, "max_tests_per_chunk": 2, "chunk_shards": True}


class TestChunkShards:
    OPTIONS = CHUNKED

    def test_every_piece_is_a_declared_shard_delivered_on_its_own_and_grouped(self, make_service, tmp_path):
        service = pool_service(make_service, cart_root(tmp_path), **self.OPTIONS)
        run = execute(service, "Cart")
        dispatch = run.latest_dispatch
        assert dispatch.shards == ("staging-01", "staging-02", "preview-01")
        assert dict(dispatch.shard_groups) == {
            "staging-01": "staging", "staging-02": "staging", "preview-01": "preview",
        }
        assert {shard: len(items) for shard, items in dispatch.shard_items.items()} == {
            "staging-01": 2, "staging-02": 1, "preview-01": 2,
        }
        delivered = [event["shard"] for event in events_of(service, run.id, "delivery.recorded")]
        assert sorted(delivered) == ["preview-01", "staging-01", "staging-02"]
        assert delivered.index("staging-01") < delivered.index("staging-02")
        assert dict(dispatch.summary) == {"PASS": 4, "FAIL": 1}
        # Each piece has its own readable result; nothing had to be merged.
        for shard in dispatch.shards:
            assert (workdir(service, run) / shard / "log.html").is_file()
            assert not (workdir(service, run) / shard / "execution-1").exists()

    def test_the_run_detail_rolls_up_per_group_as_well_as_per_shard(self, make_service, tmp_path):
        from runcomposer.app.views import run_detail

        service = pool_service(make_service, cart_root(tmp_path), **self.OPTIONS)
        run = execute(service, "Cart")
        detail = run_detail(service, service.require_run(run.id))
        assert detail["shard_groups"] == dict(run.latest_dispatch.shard_groups)
        by_group = {group["group"]: group for group in detail["groups"]}
        assert list(by_group) == ["staging", "preview"]
        assert by_group["staging"]["shards"] == ["staging-01", "staging-02"]
        assert (by_group["staging"]["declared_shards"], by_group["staging"]["delivered_shards"]) == (2, 2)
        assert (by_group["staging"]["planned"], by_group["staging"]["count"]) == (3, 3)
        assert by_group["preview"]["planned"] == 2
        total = {}
        for group in by_group.values():
            for status, count in group["summary"].items():
                total[status] = total.get(status, 0) + count
        assert total == {"PASS": 4, "FAIL": 1}
        failing = next(group for group in by_group.values() if "FAIL" in group["summary"])
        assert failing["completion"] == "FAIL"
        artifacts = {a["name"]: a for a in detail["artifacts"]}
        assert artifacts["log.html (staging-02)"]["group"] == "staging"
        assert artifacts["log.html (staging-02)"]["shard"] == "staging-02"

    def test_chunk_shards_needs_a_chunk_size(self, make_service, tmp_path):
        from runcomposer.sdk import ConfigError

        with pytest.raises(ConfigError, match="chunk_shards makes every max_tests_per_chunk piece a shard"):
            pool_service(make_service, cart_root(tmp_path), chunk_shards=True).validate()

    def test_without_it_the_pieces_are_merged_into_one_shard_per_lane(self, make_service, tmp_path):
        service = pool_service(make_service, cart_root(tmp_path), **{**self.OPTIONS, "chunk_shards": False})
        run = execute(service, "Cart")
        assert run.latest_dispatch.shards == ("staging", "preview")
        assert (workdir(service, run) / "staging" / "execution-2").is_dir()


PREPARED = {
    **SPLIT,
    "pre_dispatch": ["tests.shop_callables:reserve_partitions"],
    "run_parameters": [
        {"name": "frozen", "kind": "string", "default": ""},
        {"name": "variables", "kind": "mapping", "label": "Extra variables"},
        {"name": "browser", "kind": "choice", "choices": ["firefox", "chromium"], "default": "firefox"},
    ],
    "parameter_variables": {"variables": "*", "browser": "BROWSER"},
}
PARAMETERIZED = (
    "*** Test Cases ***\n"
    "Coupon Is Applied\n    [Tags]    Checkout\n    Should Be Equal    ${COUPON}    SPRING\n\n"
    "Browser Is The Chosen One\n    [Tags]    Checkout\n    Should Be Equal    ${BROWSER}    chromium\n\n"
    "Partition Was Reserved\n    [Tags]    Checkout\n    Should Start With    ${BOOKING}    booked-\n"
)


class TestPreDispatchAndRunParameters:
    def test_the_step_runs_in_the_parent_before_the_hand_off_and_knows_the_plan(self, make_service, tmp_path):
        service = pool_service(make_service, shop_suite(tmp_path, PARAMETERIZED), **PREPARED)
        run = execute(
            service, "Checkout",
            {"partitions": ["staging"], "variables": {"COUPON": "SPRING"}, "browser": "chromium"},
        )
        assert (run.state, run.completion) == ("COMPLETE", "PASS"), run.latest_dispatch.error
        dispatch = run.latest_dispatch
        booking = json.loads(
            (service.config.private_dir / run.id / dispatch.dispatch_id / "booking.json").read_text(encoding="utf-8")
        )
        assert booking["partitions"] == ["staging"] and booking["shards"] == {"staging": 3}
        assert booking["groups"] == {"staging": "staging"}
        assert booking["parameters"]["variables"] == {"COUPON": "SPRING"}
        kinds = [event.kind for event in service.store.events(run.id)]
        messages = [str(e.data.get("message", "")) for e in service.store.events(run.id)]
        reserved = messages.index("reserved 1 partition(s) for 3 test(s)")
        assert reserved < kinds.index("run.running"), "the preparation happened before the hand-off"

    def test_a_refusal_by_the_step_leaves_no_dispatch_and_executes_nothing(self, make_service, tmp_path):
        service = pool_service(make_service, shop_suite(tmp_path, PARAMETERIZED), **PREPARED)
        run_id = service.compose_run(
            {"tag_filter": "Checkout"}, title="During the freeze", origin="test",
            runner_id="robot-pool", parameters={"frozen": "preview"},
        ).run.id
        outcome = service.dispatch(run_id, "robot-pool", {"frozen": "preview"}, foreground=True)
        assert outcome.refusal == "partition(s) ['preview'] are frozen for the release — choose others"
        run = service.require_run(run_id)
        assert run.state == "COMPOSED" and run.dispatches == ()
        assert not list(service.config.artifact_dir.glob(f"{run_id}/*/staging"))

    def test_what_it_keeps_in_the_private_directory_cannot_be_downloaded(self, make_client, tmp_path):
        root = shop_suite(tmp_path, PARAMETERIZED)
        client, service = make_client(
            sources={"robotframework": {"root": str(root)}},
            runners={"robot-pool": {"suite_root": str(root), **PREPARED}},
        )
        run = execute(service, "Checkout", {"variables": {"COUPON": "SPRING"}, "browser": "chromium"})
        dispatch_id = run.latest_dispatch.dispatch_id
        assert (service.config.private_dir / run.id / dispatch_id / "booking.json").is_file()
        for path in ("booking.json", "../private/booking.json", "prepared.txt", ".live"):
            response = client.get(f"/artifacts/{run.id}/{dispatch_id}/{path}")
            assert response.status_code == 404 and "k-booking-secret" not in response.text
        # …while what the runner recorded is served — the shard's directory
        # too, but not the worker's own bookkeeping in it.
        assert client.get(f"/artifacts/{run.id}/{dispatch_id}/staging/log.html").status_code == 200
        assert (service.config.artifact_dir / run.id / dispatch_id / "staging" / RESULT_FILE).is_file()
        assert client.get(f"/artifacts/{run.id}/{dispatch_id}/staging/{RESULT_FILE}").status_code == 404
        service.delete_run(run.id)
        assert not (service.config.private_dir / run.id).exists(), "removed with the run"

    def test_the_declared_parameters_are_offered_and_validated(self, make_service, tmp_path):
        from runcomposer.sdk import InvalidParameter

        service = pool_service(make_service, shop_suite(tmp_path, PARAMETERIZED), **PREPARED)
        (runner,) = service.runner_infos(health=False)
        assert [(p["name"], p["kind"]) for p in runner["parameters"]] == [
            ("partitions", "multi_choice"), ("allow_drift", "boolean"),
            ("frozen", "string"), ("variables", "mapping"), ("browser", "choice"),
        ]
        with pytest.raises(InvalidParameter) as caught:
            service.compose_run({"tag_filter": "Checkout"}, title="t", origin="test",
                                runner_id="robot-pool", parameters={"variables": "COUPON=SPRING"})
        assert caught.value.parameter == "variables"

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"run_parameters": [{"name": "partitions"}]}, "declares \\['partitions'\\], which the runner declares itself"),
            ({"parameter_variables": {"browser": "BROWSER"}}, "names 'browser', which run_parameters does not declare"),
            ({"run_parameters": [{"name": "variables", "kind": "mapping"}], "parameter_variables": {"variables": "V"}},
             "a 'mapping' parameter contributes its entries as variables and is written as '\\*'"),
            ({"pre_dispatch": ["not-an-import-path"]}, "pre_dispatch entries are import paths"),
        ],
    )
    def test_misconfiguration_is_refused_at_startup(self, make_service, tmp_path, options, message):
        from runcomposer.sdk import ConfigError

        with pytest.raises(ConfigError, match=message):
            pool_service(make_service, cart_root(tmp_path), **options).validate()


class TestPlanPreview:
    def test_a_preview_shows_what_the_dispatch_then_does_and_touches_nothing(self, make_service, tmp_path):
        marker = tmp_path / "hook-ran"
        service = pool_service(
            make_service, cart_root(tmp_path),
            pre_run_hooks=[f"touch {marker}"], pre_dispatch=["tests.shop_callables:reserve_partitions"],
            **CHUNKED,
        )
        run_id = service.compose_run({"tag_filter": "Cart"}, title="t", origin="test", runner_id="robot-pool").run.id
        outcome = service.plan("robot-pool", run_id=run_id)
        preview = outcome.preview
        assert outcome.refusal is None and outcome.run_id == run_id
        assert list(preview.shards) == ["staging-01", "staging-02", "preview-01"]
        assert preview.groups == {"staging-01": "staging", "staging-02": "staging", "preview-01": "preview"}
        assert sorted(item for items in preview.shards.values() for item in items) == sorted(CART)
        assert any("readiness step(s)" in note and "does not run them" in note for note in preview.notes)
        assert any(note.startswith("cold start") for note in preview.notes)
        # Nothing happened: no hook, no dispatch, no event, no directory.
        run = service.require_run(run_id)
        assert not marker.exists() and run.state == "COMPOSED" and run.dispatches == ()
        assert [e.kind for e in service.store.events(run_id)] == ["run.composed"]
        assert not (service.config.artifact_dir / run_id).exists()
        assert not service.config.private_dir.exists()

        service.dispatch(run_id, "robot-pool", foreground=True)
        dispatch = service.require_run(run_id).latest_dispatch
        assert list(dispatch.shards) == list(preview.shards)
        assert {s: list(i) for s, i in dispatch.shard_items.items()} == {s: list(i) for s, i in preview.shards.items()}
        assert dispatch.plan == preview.text and marker.exists()

    def test_a_drifted_corpus_is_a_refusal_in_the_preview_too(self, make_service, tmp_path):
        root = cart_root(tmp_path)
        service = pool_service(make_service, root)
        run_id = service.compose_run({"tag_filter": "Cart"}, title="t", origin="test", runner_id="robot-pool").run.id
        (root / "cart.robot").write_text(FIVE_TESTS + "\nCart Is New\n    [Tags]    Cart\n    Log    ok\n", encoding="utf-8")
        outcome = service.plan("robot-pool", run_id=run_id)
        assert outcome.preview is None and "drift check" in outcome.refusal
        allowed = service.plan("robot-pool", run_id=run_id, parameters={"allow_drift": True})
        assert allowed.refusal is None
        assert sorted(i for items in allowed.preview.shards.values() for i in items) == sorted(CART)

    def test_history_statuses_restrict_the_durations_the_plan_is_built_on(self, tmp_path):
        from runcomposer.plugins.robot_pool import RobotPoolRunner

        root = cart_root(tmp_path)
        ctx = FakeDispatchContext(CART)
        ctx.preview(RobotPoolRunner(suite_root=str(root), history_statuses=["PASS"], history_depth=9))
        assert ctx.history.calls == [{"labels": {}, "last_n": 9, "statuses": ("PASS",)}]


# -- 3. the same runner, composed from the toolkit ---------------------------------------


class LaneRunner(RunnerBase):
    """A connector's own Robot runner: the selection is distributed over the
    chosen stages (each test once), every stage executes its share in chunks
    of ``chunk_size`` one after another, each chunk a shard of its own.

    Written against ``runcomposer.sdk`` and ``runcomposer.sdk.robot`` only:
    planning and bookkeeping are its own, the execution core is the
    toolkit's.
    """

    PATH_OPTIONS = ("root",)

    def __init__(self, root: str, stages: list[str], chunk_size: int = 2):
        self.root, self.stages, self.chunk_size = root, list(stages), int(chunk_size)

    def describe(self) -> RunnerInfo:
        from runcomposer.sdk import Parameter

        return RunnerInfo(
            id="lanes",
            capabilities=("live_status", "cancel", "plan"),
            parameters=(
                Parameter("stages", kind="multi_choice", choices=tuple(self.stages), default=list(self.stages)),
            ),
            result_format="robot-output-xml",
        )

    def _shards(self, ctx: PlanContext) -> dict[str, tuple[str, list[str]]]:
        """shard → (stage, item ids)."""
        if ctx.source.snapshot() != (ctx.spec.get("source") or {}).get("snapshot"):
            ctx.refuse("the corpus changed since this run was composed")
        stages = list(ctx.parameters.get("stages") or self.stages)
        lanes = plan_chunks(ctx.item_ids, len(stages), ctx.history.durations(statuses=("PASS",)))
        shards: dict[str, tuple[str, list[str]]] = {}
        for stage, lane in zip(stages, lanes.chunks, strict=False):
            for index, piece in enumerate(split_max(lane, self.chunk_size), start=1):
                shards[f"{stage}-{index:02d}"] = (stage, piece)
        return shards

    def plan(self, ctx: PlanContext) -> PlanPreview:
        shards = self._shards(ctx)
        return PlanPreview(
            text=f"{len(ctx.item_ids)} test(s) over {len(shards)} chunk(s)",
            shards={shard: items for shard, (_stage, items) in shards.items()},
            groups={shard: stage for shard, (stage, _items) in shards.items()},
        )

    def dispatch(self, ctx: DispatchContext) -> None:
        shards = self._shards(ctx)
        channel = ctx.live_channel()
        payloads = [
            ShardPayload(
                shard=shard,
                out_dir=str(ctx.workdir / shard),
                executions=(Execution(self.root, tuple(items)),),
                variables={"STAGE": stage},
                partition=stage,
                lane=stage,
                channel=channel,
            )
            for shard, (stage, items) in shards.items()
        ]
        ctx.declare(
            {shard: items for shard, (_stage, items) in shards.items()},
            plan=self.plan(ctx).text,
            groups={shard: stage for shard, (stage, _items) in shards.items()},
        )
        problems: list[str] = []

        def finished(_payload: ShardPayload, outcome: ShardOutcome) -> None:
            problem = deliver_shard(ctx, outcome, group=outcome.payload.partition)
            if problem:
                problems.append(problem)

        supervise(payloads, max_workers=len(self.stages), on_done=finished, cancelled=lambda: ctx.cancelled)
        ctx.raise_if_cancelled()
        if problems:
            raise RuntimeError("; ".join(problems))


LANES = "tests.test_robot_toolkit:LaneRunner"


class TestAComposedRunner:
    def test_it_imports_nothing_of_runcomposer_but_the_sdk_and_its_toolkit(self):
        import ast
        import inspect
        import textwrap

        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        allowed, other = set(), set()
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            names = {alias.asname or alias.name for alias in node.names}
            if node.module in ("runcomposer.sdk", "runcomposer.sdk.robot"):
                allowed |= names
            elif node.level or (node.module or "").startswith("runcomposer"):
                other |= names  # the kit and this suite's helpers: for the tests, not the runner
        source = textwrap.dedent(inspect.getsource(LaneRunner))
        used = {node.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)}
        assert not used & other, f"the runner uses test-side names: {sorted(used & other)}"
        assert {"plan_chunks", "split_max", "ShardPayload", "Execution", "supervise", "deliver_shard"} <= used & allowed
        inner = {
            node.module for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom)
        }
        assert inner <= {"runcomposer.sdk"}

    def test_it_passes_the_conformance_check_against_a_real_catalog(self, tmp_path):
        from runcomposer.plugins.robot_source import RobotFrameworkSource

        root = cart_root(tmp_path)
        source = RobotFrameworkSource(root=str(root))
        ctx = check_runner(
            LaneRunner(str(root), ["staging", "preview"]),
            FakeDispatchContext.from_source(source, "Cart"),
            expect="complete", allow_missing=False,
        )
        assert ctx.declared == ("staging-01", "staging-02", "preview-01")
        assert ctx.shard_groups == {"staging-01": "staging", "staging-02": "staging", "preview-01": "preview"}
        assert ctx.summary() == {"PASS": 4, "FAIL": 1}
        assert ctx.history.calls[-1]["statuses"] == ("PASS",)
        # The whole live stream is there to assert, RUNNING first.
        stream = [(s.item_id, s.status) for s in ctx.live_updates if s.shard == "preview-01"]
        assert stream[0][1] == "RUNNING" and len(stream) == 4
        assert ctx.live_now == [], "every shard delivered"
        assert {a.group for a in ctx.artifacts} == {"staging", "preview"}

    def test_it_runs_in_a_real_deployment_and_can_be_cancelled_mid_chunk(self, tmp_path):
        root = staged_suite(tmp_path, 120)
        with ConnectorHarness(
            sources={"shop": {"plugin": "robotframework", "root": str(root)}},
            runners={"lanes": {"module": LANES, "root": str(root), "stages": ["staging"], "chunk_size": 1}},
        ) as harness:
            plan = harness.plan("Checkout", runner="lanes").preview
            assert list(plan.shards) == ["staging-01", "staging-02"]
            handle = harness.start("Checkout", runner="lanes")
            # The first chunk delivers; the second is in flight on the same lane.
            mid = handle.wait_until(
                lambda run: {v.shard for v in run.verdicts} == {"staging-01"}
                and any(s.status == "RUNNING" for s in run.live),
                timeout=90,
            )
            assert mid.state == "RUNNING" and [s.item_id for s in mid.live] == [SLOW]
            handle.cancel()
            done = handle.wait(timeout=60)
            assert done.state == "CANCELLED"
            assert [(v.item_id, v.status) for v in done.verdicts] == [(QUICK, "PASS")], "what was delivered is kept"


class TestRobotPoolAgainstTheKit:
    def test_the_configured_runner_passes_the_same_check(self, tmp_path):
        from runcomposer.plugins.robot_pool import RobotPoolRunner
        from runcomposer.plugins.robot_source import RobotFrameworkSource

        root = cart_root(tmp_path)
        ctx = check_runner(
            RobotPoolRunner(suite_root=str(root), **CHUNKED),
            FakeDispatchContext.from_source(RobotFrameworkSource(root=str(root)), "Cart"),
            expect="complete",
        )
        assert ctx.declared == ("staging-01", "staging-02", "preview-01")
        assert ctx.shard_groups == {"staging-01": "staging", "staging-02": "staging", "preview-01": "preview"}


# -- merged results per group, and a step when a dispatch ends ------------------------------


class MergingRunner(LaneRunner):
    """``LaneRunner`` with one merged result per stage and one for the run
    (``GroupMerger``), and a shared environment it holds for the whole
    dispatch: taken before the hand-off, given back when the dispatch ends —
    completed, failed or cancelled — in a ``finally``. The pattern for "a
    step when a dispatch ends" in a composed runner: it owns ``dispatch``,
    so it brackets the work itself."""

    def __init__(self, root: str, stages: list[str], chunk_size: int = 2, lock_dir: str = ""):
        super().__init__(root, stages, chunk_size)
        self.lock_dir = lock_dir

    def dispatch(self, ctx: DispatchContext) -> None:
        import os

        from runcomposer.sdk.robot import GroupMerger

        shards = self._shards(ctx)
        lock = Path(self.lock_dir) / "staging-shop.lock"
        try:  # acquire before the hand-off: a refusal still leaves nothing behind
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            ctx.refuse("the staging shop is held by another dispatch")
        try:
            channel = ctx.live_channel()
            payloads = [
                ShardPayload(shard=shard, out_dir=str(ctx.workdir / shard),
                             executions=(Execution(self.root, tuple(items)),),
                             variables={"STAGE": stage}, partition=stage, lane=stage, channel=channel)
                for shard, (stage, items) in shards.items()
            ]
            groups = {shard: stage for shard, (stage, _items) in shards.items()}
            ctx.declare({shard: items for shard, (_stage, items) in shards.items()}, groups=groups)
            merger = GroupMerger(ctx, payloads, groups)

            def deliver(outcome: ShardOutcome) -> None:
                deliver_shard(ctx, outcome, group=outcome.payload.partition)

            def finished(_payload: ShardPayload, outcome: ShardOutcome) -> None:
                for ready in merger.shard_ended(outcome):
                    deliver(ready)

            try:
                supervise(payloads, max_workers=len(self.stages), on_done=finished,
                          cancelled=lambda: ctx.cancelled)
            finally:
                for held in merger.finish():
                    deliver(held)
            ctx.raise_if_cancelled()
        finally:  # the step when the dispatch ends, however it ends
            lock.unlink(missing_ok=True)


MERGING = "tests.test_robot_toolkit:MergingRunner"


class TestGroupMerger:
    def test_each_group_and_the_run_are_merged_and_recorded_before_the_last_delivery(self, tmp_path):
        from runcomposer.plugins.robot_source import RobotFrameworkSource

        root = cart_root(tmp_path)
        locks = tmp_path / "locks"
        locks.mkdir()
        ctx = check_runner(
            MergingRunner(str(root), ["staging", "preview"], lock_dir=str(locks)),
            FakeDispatchContext.from_source(RobotFrameworkSource(root=str(root)), "Cart"),
            expect="complete", allow_missing=False,
        )
        merged = {a.name for a in ctx.artifacts if a.shard is None}
        assert {"output.xml (group staging)", "log.html (group staging)", "output.xml (run)"} <= merged
        assert {a.group for a in ctx.artifacts if a.name.endswith("(group staging)")} == {"staging"}
        assert len(executed_tests(Path(next(
            a.url_or_path for a in ctx.artifacts if a.name == "output.xml (run)"
        )))) == 5
        assert ctx.late_records == [], "every merged result was recorded before the last delivery"
        assert not any(locks.iterdir()), "the environment was given back"

    def test_the_environment_is_given_back_on_cancel_and_a_held_one_refuses(self, tmp_path):
        root = staged_suite(tmp_path, 120)
        locks = tmp_path / "locks"
        locks.mkdir()
        with ConnectorHarness(
            sources={"shop": {"plugin": "robotframework", "root": str(root)}},
            runners={"merging": {"module": MERGING, "root": str(root), "stages": ["staging"],
                                 "chunk_size": 1, "lock_dir": str(locks)}},
        ) as harness:
            handle = harness.start("Checkout", runner="merging")
            handle.wait_until(lambda run: any(s.status == "RUNNING" for s in run.live), timeout=90)
            assert (locks / "staging-shop.lock").exists(), "held while the dispatch executes"
            second = harness.run("Checkout", runner="merging")
            assert second.refusal == "the staging shop is held by another dispatch"
            handle.cancel()
            assert handle.wait(timeout=60).state == "CANCELLED"
            assert not (locks / "staging-shop.lock").exists(), "given back when the dispatch ended"
