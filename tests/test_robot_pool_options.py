"""`robot-pool` switches (DESIGN.md §6.2a), each with real Robot Framework
and real worker processes:

- configured callables take ``{call, with}`` — settings reach the callable,
  also across the process boundary of a worker;
- the partition mode as a per-run choice;
- a partition that is one environment never executes two chunks at once;
- per-run Robot variables cannot override protected ones, in any spelling;
- health checks of the deployment's own;
- one merged result per group and for the run;
- an item the suite root would not find is refused before the hand-off;
- artifacts are recorded before the delivery that completes the run;
- checks that run for a preview as well as for a dispatch.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, ClassVar

import pytest

from runcomposer.sdk import ConfigError, InvalidParameter, Item, Service

from .conftest import REPO, compose, needs_robot, wait_until, write_config
from .test_robot_pool import (
    FIVE_TESTS,
    RUNNER,
    events_of,
    execute,
    executed_tests,
    pool_service,
    shop_suite,
    staged_suite,
    workdir,
    write_suite,
)
from .test_robot_toolkit import CART

pytestmark = needs_robot

HERE = "tests.test_robot_pool_options"


def cart_root(tmp_path: Path) -> Path:
    return shop_suite(tmp_path, FIVE_TESTS, name="cart.robot")


def preview(service: Service, parameters: dict | None = None, selection: str = "Cart"):
    return service.plan(RUNNER, selection={"tag_filter": selection}, parameters=parameters)


# -- configured callables with settings ----------------------------------------------


def setup_with_settings(info: dict, *, settings: dict) -> dict:
    """A `worker_setup` entry with `with:` settings: runs in the worker."""
    Path(info["out_dir"], "setup.json").write_text(
        json.dumps({"pid": os.getpid(), "settings": settings}), encoding="utf-8"
    )
    return {"STOREFRONT": settings["storefront"]}


def reserve_with_settings(ctx: Any, info: dict, *, settings: dict) -> dict:
    """A `pre_dispatch` entry with settings, in the dispatching process."""
    return {"HOLD_MINUTES": str(settings["hold_minutes"])}


SETTINGS_SUITE = (
    "*** Test Cases ***\n"
    "Checkout Sees Its Storefront\n"
    "    [Tags]    Checkout\n"
    "    Should Be Equal    ${STOREFRONT}    eu.shop.example\n"
    "    Should Be Equal    ${HOLD_MINUTES}    30\n"
)


class TestConfiguredCallablesWithSettings:
    def test_settings_reach_a_worker_setup_in_a_spawned_worker_and_a_pre_dispatch(
        self, make_service, tmp_path
    ):
        root = shop_suite(tmp_path, SETTINGS_SUITE)
        service = pool_service(
            make_service, root,
            worker_setup=[{"call": f"{HERE}:setup_with_settings", "with": {"storefront": "eu.shop.example"}}],
            pre_dispatch=[{"call": f"{HERE}:reserve_with_settings", "with": {"hold_minutes": 30}}],
            pythonpath=[str(REPO)],
        )
        run = execute(service, "Checkout")
        assert run.completion == "PASS", [v.message for v in service.store.verdicts_for(run.id)]
        record = json.loads((workdir(service, run) / "default" / "setup.json").read_text(encoding="utf-8"))
        assert record["pid"] != os.getpid()  # in the worker, not here
        assert record["settings"] == {"storefront": "eu.shop.example"}

    @pytest.mark.parametrize("option", ["pre_dispatch", "worker_setup", "plan_checks", "health_checks"])
    @pytest.mark.parametrize(
        ("entry", "message"),
        [
            ({"call": "no_colon_here"}, "a configured callable is an import path"),
            ({"cal": f"{HERE}:setup_with_settings"}, "got keys \\['cal'\\]"),
            ({"call": f"{HERE}:setup_with_settings", "with": ["x"]}, "'with' must be a mapping"),
            ("no_colon_here", "entries are import paths"),
        ],
    )
    def test_a_malformed_entry_refuses_startup(self, make_service, tmp_path, option, entry, message):
        with pytest.raises(ConfigError, match=message):
            pool_service(make_service, cart_root(tmp_path), **{option: [entry]}).validate()


# -- the partition mode per run --------------------------------------------------------


class TestPartitionModePerRun:
    OPTIONS: ClassVar[dict[str, Any]] = {"partitions": ["eu", "us"], "max_workers": 2}

    def test_without_the_opt_in_the_mode_is_not_a_parameter(self, make_service, tmp_path):
        service = pool_service(make_service, cart_root(tmp_path), **self.OPTIONS)
        (info,) = service.runner_infos(health=False)
        assert [p["name"] for p in info["parameters"]] == ["partitions", "allow_drift"]
        with pytest.raises(InvalidParameter) as raised:
            preview(service, {"partition_mode": "split"})
        assert raised.value.parameter == "partition_mode"

    def test_opted_in_the_preview_and_the_dispatch_follow_the_choice(self, make_service, tmp_path):
        service = pool_service(
            make_service, cart_root(tmp_path), partition_mode_per_run=True, **self.OPTIONS
        )
        (info,) = service.runner_infos(health=False)
        (mode,) = [p for p in info["parameters"] if p["name"] == "partition_mode"]
        assert (mode["kind"], mode["choices"], mode["default"]) == ("choice", ["split", "each"], "each")

        each = preview(service).preview
        assert sorted(each.shards) == ["eu-1", "us-1"] and each.data["partition_mode"] == "each"
        assert all(sorted(items) == sorted(CART) for items in each.shards.values())

        split = preview(service, {"partition_mode": "split"}).preview
        assert split.data["partition_mode"] == "split"
        assert sorted(i for items in split.shards.values() for i in items) == sorted(CART)
        assert "each test once" in split.text

        run = execute(service, "Cart", {"partition_mode": "split"})
        dispatch = run.latest_dispatch
        assert {s: sorted(i) for s, i in dispatch.shard_items.items()} == {
            s: sorted(i) for s, i in split.shards.items()
        }
        assert dict(dispatch.summary) == {"PASS": 4, "FAIL": 1}, "each test once — not twice"

    def test_a_mode_that_does_not_exist_is_an_invalid_parameter(self, make_service, tmp_path):
        service = pool_service(make_service, cart_root(tmp_path), partition_mode_per_run=True, **self.OPTIONS)
        with pytest.raises(InvalidParameter) as raised:
            preview(service, {"partition_mode": "spread"})
        assert raised.value.parameter == "partition_mode"

    def test_the_runner_reserves_the_name_when_opted_in(self, make_service, tmp_path):
        with pytest.raises(ConfigError, match="declares \\['partition_mode'\\], which the runner declares"):
            pool_service(
                make_service, cart_root(tmp_path), partition_mode_per_run=True,
                run_parameters=[{"name": "partition_mode"}], **self.OPTIONS,
            ).validate()


# -- one partition, one chunk at a time -----------------------------------------------

MARKERS_LIBRARY = '''\
import time
from pathlib import Path


def enter_partition(log, partition, name):
    with open(log, "a", encoding="utf-8") as file:
        file.write(f"enter {partition} {name}\\n")


def leave_partition(log, partition, name):
    with open(log, "a", encoding="utf-8") as file:
        file.write(f"leave {partition} {name}\\n")


def wait_for_another_test_on(log, partition, name, timeout="3"):
    """Give a test of another chunk of this partition the chance to start
    (it does, at once, when chunks of one partition run side by side)."""
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        lines = Path(log).read_text(encoding="utf-8").splitlines() if Path(log).exists() else []
        if any(line.split(" ", 2)[:2] == ["enter", partition] and line.split(" ", 2)[2] != name
               for line in lines):
            return
        time.sleep(0.05)
'''

MARKED_SUITE = """\
*** Settings ***
Library    Markers

*** Test Cases ***
Checkout On The Environment One
    [Tags]    Checkout
    Visit The Partition

Checkout On The Environment Two
    [Tags]    Checkout
    Visit The Partition

*** Keywords ***
Visit The Partition
    Enter Partition    ${MARKERS}    ${PARTITION}    ${TEST NAME}
    Wait For Another Test On    ${MARKERS}    ${PARTITION}    ${TEST NAME}
    Leave Partition    ${MARKERS}    ${PARTITION}    ${TEST NAME}
"""


def overlaps(markers: Path) -> dict[str, int]:
    """Per partition: how often a test entered while another one of the
    same partition was inside — read from the order of the markers alone."""
    inside: dict[str, set[str]] = {}
    found: dict[str, int] = {}
    for line in markers.read_text(encoding="utf-8").splitlines():
        what, partition, name = line.split(" ", 2)
        current = inside.setdefault(partition, set())
        if what == "enter":
            found[partition] = found.get(partition, 0) + (1 if current else 0)
            current.add(name)
        else:
            current.discard(name)
    return found


class TestExclusivePartitions:
    def service(self, make_service, tmp_path, **options: Any) -> tuple[Service, Path]:
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "Markers.py").write_text(MARKERS_LIBRARY, encoding="utf-8")
        root = shop_suite(tmp_path, MARKED_SUITE)
        markers = tmp_path / "markers.log"
        service = pool_service(
            make_service, root, partitions=["eu", "us"], max_workers=4,
            pythonpath=[str(tmp_path / "lib")], variables={"MARKERS": str(markers)}, **options,
        )
        return service, markers

    def test_without_a_bound_chunks_of_one_partition_run_side_by_side(self, make_service, tmp_path):
        """Today's default, shown: four workers over two partitions cut each
        partition into two chunks, and both execute at once."""
        service, markers = self.service(make_service, tmp_path)
        run = execute(service, "Checkout")
        assert run.completion == "PASS", [v.message for v in service.store.verdicts_for(run.id)]
        assert sorted(run.latest_dispatch.shards) == ["eu-1", "eu-2", "us-1", "us-2"]
        assert overlaps(markers) == {"eu": 1, "us": 1}

    def test_max_chunks_per_partition_1_never_overlaps_one_partition(self, make_service, tmp_path):
        service, markers = self.service(
            make_service, tmp_path, max_chunks_per_partition=1, max_tests_per_chunk=1, chunk_shards=True,
        )
        planned = preview(service, selection="Checkout").preview
        assert planned.data["max_chunks_per_partition"] == 1
        assert "at most 1 chunk(s) of one partition at a time" in planned.text
        run = execute(service, "Checkout")
        assert run.completion == "PASS", [v.message for v in service.store.verdicts_for(run.id)]
        # Every test on every partition, one shard per test — and still never two at once.
        assert sorted(run.latest_dispatch.shards) == ["eu-1-01", "eu-1-02", "us-1-01", "us-1-02"]
        lines = markers.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 8 and overlaps(markers) == {"eu": 0, "us": 0}

    @pytest.mark.parametrize("value", [0, -1, True])
    def test_a_bound_below_one_refuses_startup(self, make_service, tmp_path, value):
        with pytest.raises(ConfigError, match="max_chunks_per_partition must be at least 1"):
            pool_service(make_service, cart_root(tmp_path), max_chunks_per_partition=value).validate()


# -- protected variables -----------------------------------------------------------------

PROTECTED_SUITE = (
    "*** Test Cases ***\n"
    "Checkout Talks To The Configured Shop\n"
    "    [Tags]    Checkout\n"
    "    Should Be Equal    ${BASE_URL}    https://eu.shop.example\n"
    "    Should Be Equal    ${LOCALE}    de\n"
)


def protected_service(make_service, tmp_path):
    return pool_service(
        make_service, shop_suite(tmp_path, PROTECTED_SUITE),
        variables={"BASE_URL": "https://eu.shop.example"},
        run_parameters=[{"name": "variables", "kind": "mapping"}],
        parameter_variables={"variables": "*"},
        protected_variables=["BASE_URL"],
    )


SPELLINGS = ["BASE_URL", "base_url", "Base Url", "baseurl", "${base url}", "${BASE_URL}", " B a_S e URL ", "&{Base_Url}"]


class TestProtectedVariables:
    @pytest.mark.parametrize("spelling", SPELLINGS)
    def test_a_per_run_mapping_naming_one_is_refused_in_preview_and_dispatch(
        self, make_service, tmp_path, spelling
    ):
        service = protected_service(make_service, tmp_path)
        parameters = {"variables": {spelling: "https://attacker.shop.example", "LOCALE": "de"}}
        with pytest.raises(InvalidParameter) as raised:
            preview(service, parameters, selection="Checkout")
        assert raised.value.parameter == "variables"
        assert repr(spelling) in str(raised.value) and "'BASE_URL'" in str(raised.value)

        run_id = compose(service, "Checkout", runner_id=RUNNER, parameters=parameters)
        with pytest.raises(InvalidParameter) as raised:
            service.dispatch(run_id, RUNNER, parameters, foreground=True)
        assert raised.value.parameter == "variables"
        assert service.require_run(run_id).dispatches == ()

    def test_other_names_still_reach_the_tests(self, make_service, tmp_path):
        service = protected_service(make_service, tmp_path)
        run = execute(service, "Checkout", {"variables": {"LOCALE": "de", "BASE_URLS": "x"}})
        assert run.completion == "PASS", [v.message for v in service.store.verdicts_for(run.id)]

    @pytest.mark.parametrize(
        "name", ["BASE_URL", "base url", "Base_Url", "b_a_s_e u_r_l", "ÄPFEL Straße", "SHOP-12XX", "x y\tz"]
    )
    def test_the_rule_is_robots_own(self, name):
        from robot.utils import normalize

        from runcomposer.plugins.robot_pool import variable_key

        assert variable_key(name) == normalize(name, ignore="_")
        assert variable_key("${" + name + "}") == normalize(name, ignore="_")

    def test_a_configured_parameter_aimed_at_a_protected_variable_refuses_startup(self, make_service, tmp_path):
        with pytest.raises(ConfigError, match=r"parameter_variables\.region sets 'base_url', which protected"):
            pool_service(
                make_service, cart_root(tmp_path),
                run_parameters=[{"name": "region"}], parameter_variables={"region": "base_url"},
                protected_variables=["BASE_URL"],
            ).validate()


# -- health checks -----------------------------------------------------------------------


def health_ok(info: dict, *, settings: dict) -> dict:
    assert info["options"]["suite_root"] == info["suite_roots"][0]
    return {"check": "staging reachable", "ok": True, "detail": f"{settings['host']} answers"}


def health_rows(info: dict) -> list[dict]:
    return [
        {"check": "eu storefront", "ok": True, "detail": "ok"},
        {"check": "us storefront", "ok": False, "detail": "certificate expired"},
    ]


def health_raises(info: dict) -> dict:
    raise ConnectionError("preview.shop.example refused the connection")


def health_answers_nonsense(info: dict) -> str:
    return "fine"


class TestHealthChecks:
    def test_rows_follow_the_built_in_ones_and_a_failure_degrades(self, make_service, tmp_path):
        root = cart_root(tmp_path)
        service = pool_service(
            make_service, root,
            health_checks=[
                {"call": f"{HERE}:health_ok", "with": {"host": "staging.shop.example"}},
                f"{HERE}:health_rows",
                f"{HERE}:health_raises",
                f"{HERE}:health_answers_nonsense",
            ],
        )
        health = service.runner_health(RUNNER, refresh=True)
        assert health.status == "degraded"
        assert health.detail == (
            f"health check(s) failed: us storefront, {HERE}:health_raises, {HERE}:health_answers_nonsense"
        )
        checks = health.data["checks"]
        assert [(c["check"], c["ok"]) for c in checks] == [
            ("Robot Framework installed", True),
            (f"suite root {root}", True),
            ("staging reachable", True),
            ("eu storefront", True),
            ("us storefront", False),
            (f"{HERE}:health_raises", False),
            (f"{HERE}:health_answers_nonsense", False),
        ]
        assert checks[2]["detail"] == "staging.shop.example answers"
        assert checks[5]["detail"] == "ConnectionError: preview.shop.example refused the connection"
        assert "a health check returns {check, ok, detail} rows" in checks[6]["detail"]

    def test_all_passing_is_ok(self, make_service, tmp_path):
        service = pool_service(
            make_service, cart_root(tmp_path),
            health_checks=[{"call": f"{HERE}:health_ok", "with": {"host": "staging.shop.example"}}],
        )
        health = service.runner_health(RUNNER, refresh=True)
        assert health.status == "ok" and health.data["checks"][-1]["check"] == "staging reachable"

    def test_doctor_shows_the_rows_and_a_raising_check_is_no_crash(self, tmp_path, capsys):
        from runcomposer.cli.main import main

        root = cart_root(tmp_path)
        config = write_config(
            tmp_path,
            sources={"robotframework": {"root": str(root)}},
            runners={RUNNER: {"suite_root": str(root), "health_checks": [f"{HERE}:health_raises"]}},
        )
        code = main(["doctor", "-v", "--config", str(config)])
        out = capsys.readouterr().out
        assert code == 1
        assert f"robot-pool (robot-pool): degraded — health check(s) failed: {HERE}:health_raises" in out
        assert (
            f"[FAIL] {HERE}:health_raises — ConnectionError: preview.shop.example refused the connection"
            in out
        )
        assert "[ok  ] Robot Framework installed" in out


# -- merged results per group and for the run -------------------------------------------


def artifact_names(service: Service, run_id: str) -> dict[str, dict]:
    return {a["name"]: a for a in service.artifacts.list(run_id, server_paths=True)}


class TestMergeGroups:
    def test_split_one_merged_result_per_group_and_one_for_the_run(self, make_service, tmp_path):
        service = pool_service(
            make_service, cart_root(tmp_path), partitions=["staging", "preview"], partition_mode="split",
            max_tests_per_chunk=1, chunk_shards=True, merge_groups=True,
        )
        run = execute(service, "Cart")
        assert (run.state, run.completion) == ("COMPLETE", "FAIL")
        artifacts = artifact_names(service, run.id)
        for label, group in (("group staging", "staging"), ("group preview", "preview"), ("run", None)):
            for name in ("log.html", "report.html", "output.xml"):
                artifact = artifacts[f"{name} ({label})"]
                assert artifact["group"] == group and artifact["shard"] is None
                assert artifact["kind"] == "local" and artifact["href"]
        shards = run.latest_dispatch.shard_items
        by_group = {"staging": set(), "preview": set()}
        for shard, items in shards.items():
            by_group[run.latest_dispatch.shard_groups[shard]].update(items)
        for group in ("staging", "preview"):
            merged = Path(artifacts[f"output.xml (group {group})"]["url_or_path"])
            assert set(executed_tests(merged)) == by_group[group]
        whole = executed_tests(Path(artifacts["output.xml (run)"]["url_or_path"]))
        assert set(whole) == set(CART) and whole["Shop.Cart.Cart Rejects A Negative Quantity"][0] == "FAIL"
        # The merges never ran Robot in the dispatching process: they are logged as spawned results.
        logs = [e["message"] for e in events_of(service, run.id, "log")]
        assert any(m.startswith("merged result of the run recorded") for m in logs)

    def test_each_the_run_puts_the_partitions_side_by_side(self, make_service, tmp_path):
        service = pool_service(
            make_service, cart_root(tmp_path), partitions=["staging", "preview"], max_workers=4,
            merge_groups=True,
        )
        run = execute(service, "Cart")
        artifacts = artifact_names(service, run.id)
        whole = executed_tests(Path(artifacts["output.xml (run)"]["url_or_path"]))
        assert len(whole) == 10
        assert {name.split(".")[1] for name in whole} == {"staging", "preview"}
        group = artifacts["output.xml (group staging)"]["url_or_path"]
        assert set(executed_tests(Path(group))) == set(CART)

    def test_without_partitions_there_is_one_merged_result_for_the_run(self, make_service, tmp_path):
        service = pool_service(make_service, cart_root(tmp_path), max_workers=2, merge_groups=True)
        run = execute(service, "Cart")
        assert run.latest_dispatch.shards == ("default-1", "default-2")
        artifacts = artifact_names(service, run.id)
        assert not [name for name in artifacts if "(group " in name]
        whole = executed_tests(Path(artifacts["output.xml (run)"]["url_or_path"]))
        assert set(whole) == set(CART)

    def test_without_the_option_nothing_is_merged(self, make_service, tmp_path):
        service = pool_service(make_service, cart_root(tmp_path), partitions=["staging", "preview"])
        run = execute(service, "Cart")
        assert not [name for name in artifact_names(service, run.id) if "(group " in name or "(run)" in name]
        assert not (workdir(service, run) / "_merged").exists()

    def test_a_cancelled_dispatch_merges_nothing(self, make_service, tmp_path):
        service = pool_service(
            make_service, staged_suite(tmp_path, 60), partitions=["staging", "preview"],
            partition_mode="split", merge_groups=True,
        )
        run_id = compose(service, "Checkout", runner_id=RUNNER)
        outcome = service.dispatch(run_id, RUNNER)
        wait_until(
            lambda: service.store.delivered_shards(run_id, outcome.dispatch_id),
            what="the quick shard to deliver while the slow one runs",
        )
        service.cancel(run_id)
        run = service.wait(run_id, timeout=30)
        assert run.state == "CANCELLED"
        assert service.dispatcher.join(timeout=10)
        # The group that was complete before the cancel has its result
        # (its one shard's); the cancelled group and the run have none.
        merged = sorted(n for n in artifact_names(service, run_id) if "(group " in n or "(run)" in n)
        assert merged == ["log.html (group staging)", "output.xml (group staging)",
                          "report.html (group staging)"]
        assert not (workdir(service, run) / "_merged" / "run").exists()

    def test_a_failed_merge_is_a_log_line_and_no_artifact(self, tmp_path):
        from runcomposer.sdk.robot import GroupMerger, MergeResult
        from runcomposer.testing import FakeDispatchContext

        ctx = FakeDispatchContext(CART)
        merger = GroupMerger(ctx, [], {})
        merger._record("eu", MergeResult(error="rebot wrote no output.xml: [ ERROR ] boom"), 2)
        assert ctx.artifacts == []
        level, message = ctx.logs[-1]
        assert level == "warning"
        assert message.startswith("merged result of group eu not produced: rebot wrote no output.xml")


# -- a single suite root is checked against the catalog ---------------------------------


class TestASingleRootIsChecked:
    def test_a_catalog_built_from_the_parent_directory_is_refused_before_the_hand_off(
        self, make_service, tmp_path
    ):
        root = cart_root(tmp_path)  # tmp/Shop — executed from here, ids begin "Shop."
        service = pool_service(make_service, root, source={"robotframework": {"root": str(tmp_path)}})
        planned = preview(service)
        assert planned.preview is None
        assert "5 item(s) do not belong to the configured suite roots" in planned.refusal
        assert f"is not under the suite root {str(root)!r}" in planned.refusal
        assert "every test's id begins with 'Shop.'" in planned.refusal
        assert "… and 2 more" in planned.refusal and "checked before anything is handed off" in planned.refusal

        run_id = compose(service, "Cart", runner_id=RUNNER)
        outcome = service.dispatch(run_id, RUNNER, foreground=True)
        assert outcome.refusal == planned.refusal
        assert service.require_run(run_id).dispatches == ()

    def test_a_catalog_from_another_copy_of_the_suites_is_refused_by_its_source(self, make_service, tmp_path):
        """Same root suite name, other files: the ids look right, but what
        would run is not what the catalog describes."""
        root = cart_root(tmp_path / "here")
        copy = cart_root(tmp_path / "there")
        service = pool_service(make_service, root, source={"robotframework": {"root": str(copy)}})
        refusal = preview(service).refusal
        assert f"comes from {copy / 'cart.robot'}, which is not under the suite root {str(root)!r}" in refusal

    def test_a_catalog_without_sources_is_checked_by_name(self, make_service, tmp_path):
        root = cart_root(tmp_path)
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"items": [
            {"id": CART[0], "tags": ["Cart"]},
            {"id": "Storefront.Cart.Cart Holds One Item", "tags": ["Cart"]},
        ]}), encoding="utf-8")
        service = pool_service(make_service, root, source={"manifest": {"path": str(manifest)}})
        refusal = preview(service).refusal
        assert "'Storefront.Cart.Cart Holds One Item' is not under the suite root" in refusal
        assert CART[0] not in refusal

    def test_a_suite_that_renames_itself_is_not_refused(self, make_service, tmp_path):
        root = tmp_path / "web"
        write_suite(root, "__init__.robot", "*** Settings ***\nName    Storefront\n")
        write_suite(root, "cart.robot", FIVE_TESTS)
        service = pool_service(make_service, root)
        planned = preview(service).preview
        assert sorted(i for items in planned.shards.values() for i in items)[0].startswith("Storefront.")
        assert execute(service, "Cart").completion == "FAIL"  # executed: four PASS, one FAIL

    def test_root_mismatches_names_every_stray_item(self, tmp_path):
        from runcomposer.sdk.robot import root_mismatches

        root = cart_root(tmp_path)
        items = [Item(CART[0], meta={"source": str(root / "cart.robot")}),
                 Item("Shop.Cart.Elsewhere", meta={"source": str(tmp_path / "other" / "cart.robot")})]
        assert root_mismatches([str(root)], [CART[0]], items) == []
        (problem,) = root_mismatches([str(root)], [CART[0], "Shop.Cart.Elsewhere"], items)
        assert problem.startswith("item 'Shop.Cart.Elsewhere' comes from ")

    def test_doctor_runs_the_same_check_over_the_whole_catalog(self, tmp_path, capsys):
        from runcomposer.cli.main import main

        root = cart_root(tmp_path)
        bad = write_config(
            tmp_path,
            sources={"robotframework": {"root": str(tmp_path)}},
            runners={RUNNER: {"suite_root": str(root)}},
        )
        code = main(["doctor", "-v", "--config", str(bad)])
        out = capsys.readouterr().out
        assert code == 1
        assert "[FAIL] catalog items under the suite roots — 5 of 5 catalog item(s) are not under" in out
        assert "… and 2 more" in out and "1 problem(s):" in out

        good = write_config(
            tmp_path,
            sources={"robotframework": {"root": str(root)}},
            runners={RUNNER: {"suite_root": str(root)}},
        )
        code = main(["doctor", "-v", "--config", str(good)])
        out = capsys.readouterr().out
        assert code == 0, out
        assert "[ok  ] catalog items under the suite roots — all 5 catalog item(s) are under the suite roots" in out


# -- artifacts are recorded before the run completes ------------------------------------


def artifacts_when_completed(service: Service) -> list[list[str]]:
    """Wrap the store: at the moment ``run.completed`` is written, which
    artifacts are recorded? (What an observer reacting to it could read.)"""
    seen: list[list[str]] = []
    store = service.store
    append = store.append_event

    def append_event(run_id: str, **kwargs: Any) -> Any:
        if kwargs["kind"] == "run.completed":
            seen.append(sorted(ref.name for ref in store.artifact_refs(run_id)))
        return append(run_id, **kwargs)

    store.append_event = append_event  # type: ignore[method-assign]
    return seen


class TestRecordBeforeDelivering:
    def test_every_shard_artifact_is_recorded_when_the_run_completes(self, make_service, tmp_path):
        service = pool_service(make_service, cart_root(tmp_path), max_workers=2)
        seen = artifacts_when_completed(service)
        run = execute(service, "Cart")
        assert run.state == "COMPLETE"
        final = sorted(a["name"] for a in service.artifacts.list(run.id))
        assert len(final) == 8  # two shards × log, report, output, files
        assert seen == [final]

    def test_with_merge_groups_the_merged_results_are_recorded_too(self, make_service, tmp_path):
        service = pool_service(
            make_service, cart_root(tmp_path), partitions=["staging", "preview"], partition_mode="split",
            max_tests_per_chunk=2, chunk_shards=True, merge_groups=True,
        )
        seen = artifacts_when_completed(service)
        run = execute(service, "Cart")
        final = sorted(a["name"] for a in service.artifacts.list(run.id))
        assert "output.xml (run)" in final and "log.html (group preview)" in final
        assert seen == [final]


# -- checks that run for a preview and a dispatch ---------------------------------------

CALLS: list[dict] = []


def no_preview_on_weekends(ctx: Any, info: dict, *, settings: dict) -> None:
    CALLS.append({"partitions": info["partitions"], "shards": sorted(info["shards"]),
                  "settings": settings, "parameters": dict(ctx.parameters)})
    if any(partition in settings["closed"] for partition in info["partitions"]):
        raise InvalidParameter(
            f"parameter 'partitions': {settings['closed']} is closed for maintenance",
            parameter="partitions",
        )


def refuse_big_runs(ctx: Any, info: dict) -> None:
    if len(info["item_ids"]) > 3:
        ctx.refuse(f"{len(info['item_ids'])} items is more than this environment takes at once")


def mark_pre_dispatch(ctx: Any, info: dict) -> None:
    Path(ctx.workdir, "..", "pre-dispatch-ran").resolve().touch()


class TestPlanChecks:
    def service(self, make_service, tmp_path, **options: Any) -> Service:
        CALLS.clear()
        return pool_service(
            make_service, cart_root(tmp_path), partitions=["staging", "preview"],
            plan_checks=[{"call": f"{HERE}:no_preview_on_weekends", "with": {"closed": ["preview"]}}],
            **options,
        )

    def test_a_check_refusing_a_parameter_answers_preview_and_dispatch_alike(self, make_service, tmp_path):
        service = self.service(make_service, tmp_path, pre_dispatch=[f"{HERE}:mark_pre_dispatch"])
        with pytest.raises(InvalidParameter) as raised:
            preview(service)
        assert raised.value.parameter == "partitions" and "closed for maintenance" in str(raised.value)
        assert CALLS[-1]["settings"] == {"closed": ["preview"]}
        assert CALLS[-1]["partitions"] == ["staging", "preview"]

        run_id = compose(service, "Cart", runner_id=RUNNER)
        with pytest.raises(InvalidParameter) as raised:
            service.dispatch(run_id, RUNNER, foreground=True)
        assert raised.value.parameter == "partitions"
        assert service.require_run(run_id).dispatches == ()
        # It ran before pre_dispatch, which never did.
        assert not list(tmp_path.rglob("pre-dispatch-ran"))

        # A choice the check accepts goes through, previewed and executed alike.
        allowed = preview(service, {"partitions": ["staging"]})
        assert allowed.refusal is None and "1 plan check(s) (plan_checks) passed" in allowed.preview.notes
        assert CALLS[-1]["shards"] == ["staging-1", "staging-2"]  # two workers, one partition
        run = execute(service, "Cart", {"partitions": ["staging"]})
        assert run.state == "COMPLETE" and CALLS[-1]["parameters"]["partitions"] == ["staging"]

    def test_a_check_may_refuse_the_dispatch_as_planned(self, make_service, tmp_path):
        service = pool_service(make_service, cart_root(tmp_path), plan_checks=[f"{HERE}:refuse_big_runs"])
        planned = preview(service)
        assert planned.preview is None and "5 items is more than this environment takes" in planned.refusal
        run_id = compose(service, "Cart", runner_id=RUNNER)
        outcome = service.dispatch(run_id, RUNNER, foreground=True)
        assert outcome.refusal == planned.refusal
        assert service.require_run(run_id).dispatches == ()


# -- the shipped example ----------------------------------------------------------------


class TestTheShippedExample:
    def test_robot_regions_end_to_end(self, tmp_path, capsys):
        """`examples/robot-pool/config.yaml`, entry `robot-regions`: planned
        both ways, refused when a run tries to redirect a region, executed
        with one merged result per region and one for the run — and the
        doctor finds its catalog under its suite root."""
        import shutil

        from runcomposer.cli.main import main

        root = tmp_path / "examples"
        for name in ("robot-pool", "robot-shop"):
            shutil.copytree(REPO / "examples" / name, root / name,
                            ignore=shutil.ignore_patterns("state", "__pycache__"))
        config = str(root / "robot-pool" / "config.yaml")

        def cli(*args: str) -> tuple[int, Any]:
            code = main([*args, "--config", config, "--json"])
            return code, json.loads(capsys.readouterr().out)

        code, each = cli("plan", "Cart-Basics", "--runner", "robot-regions")
        assert code == 0 and [s["shard"] for s in each["plan"]["shards"]] == ["eu-1", "us-1"]
        code, split = cli("plan", "Cart-Basics", "--runner", "robot-regions", "--param", "partition_mode=split")
        planned = [item for shard in split["plan"]["shards"] for item in shard["item_ids"]]
        assert code == 0 and len(planned) == len(set(planned)) == split["item_count"]

        code = main(["plan", "Cart-Basics", "--runner", "robot-regions", "--param",
                     "variables={shop url: https://elsewhere.shop.example}", "--config", config])
        out = capsys.readouterr()
        assert code != 0 and "'shop url'" in out.err + out.out

        code, run = cli("run", "Cart-Basics", "--runner", "robot-regions")
        assert (run["state"], run["completion"]) == ("COMPLETE", "PASS"), run
        names = {a["name"] for a in run["artifacts"]}
        assert {"log.html (group eu)", "log.html (group us)", "log.html (run)"} <= names

        code = main(["doctor", "-v", "--config", config])
        out = capsys.readouterr().out
        assert "[ok  ] catalog items under the suite roots — all 58 catalog item(s)" in out
