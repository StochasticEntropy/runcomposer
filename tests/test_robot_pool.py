"""The `robot-pool` runner (DESIGN.md §6.2a), with REAL Robot Framework runs
through the real dispatch manager — nothing here is mocked.

Needs Robot Framework (`pip install 'runcomposer[robot]'`) and skips cleanly
without it. Suites are tiny and written into ``tmp_path`` unless a test is
about the shipped corpus; tests that need a run in flight wait on its *state*
(``wait_until`` / ``Service.wait``), never on the clock.
"""

from __future__ import annotations

import dataclasses
import json
import multiprocessing
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runcomposer.sdk import ConfigError, InvalidParameter, Service

from .conftest import REPO, ROBOT_SHOP, compose, needs_robot, run_to_end, wait_until

pytestmark = needs_robot

RUNNER = "robot-pool"


# -- helpers --------------------------------------------------------------------


def pool_service(make_service, corpus: Path = ROBOT_SHOP, *, source: Any = None, **options: Any):
    """A Service over a Robot corpus with `robot-pool` configured on it."""
    runner = {"suite_root": str(corpus), "max_workers": 2, **options}
    if "suite_roots" in options:
        runner.pop("suite_root")
    extra = {key: runner.pop(key) for key in ("parsers",) if key in runner}
    return make_service(
        sources=source or {"robotframework": {"root": str(corpus)}},
        runners={RUNNER: runner},
        **extra,
    )


def execute(service: Service, selection: Any, parameters: dict | None = None, **kwargs: Any):
    """Compose for the runner and dispatch in the foreground; the run after."""
    run_id = compose(service, selection, runner_id=RUNNER, parameters=parameters, **kwargs)
    return run_to_end(service, run_id, RUNNER, parameters)


def write_suite(root: Path, name: str, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


def shop_suite(tmp_path: Path, text: str, name: str = "checkout.robot") -> Path:
    """A one-file suite tree named ``Shop`` — ids are ``Shop.<File>.<Test>``."""
    write_suite(tmp_path / "Shop", name, text)
    return tmp_path / "Shop"


def workdir(service: Service, run) -> Path:
    return service.config.artifact_dir / run.id / run.latest_dispatch.dispatch_id


def executed_tests(output: Path) -> dict[str, tuple[str, list[str], str]]:
    """What an output.xml says was executed: ``{longname: (status, tags, message)}``."""
    from robot.api import ExecutionResult, ResultVisitor

    found: dict[str, tuple[str, list[str], str]] = {}

    class Collect(ResultVisitor):
        def visit_test(self, test):
            found[test.longname] = (test.status, [str(tag) for tag in test.tags], test.message)

    ExecutionResult(str(output)).visit(Collect())
    return found


def verdict_map(service: Service, run) -> dict[tuple[str, str, int], str]:
    """``{(shard, item, attempt): status}`` of the latest dispatch."""
    return {
        (v.shard, v.item_id, v.attempt): v.status
        for v in service.store.verdicts_for(run.id, run.latest_dispatch.dispatch_id)
    }


def events_of(service: Service, run_id: str, kind: str) -> list[dict]:
    return [dict(e.data) for e in service.store.events(run_id) if e.kind == kind]


FIVE_TESTS = """\
*** Test Cases ***
Cart Holds One Item
    [Tags]    Cart
    Log    ok

Cart Holds Two Items
    [Tags]    Cart
    Log    ok

Cart Rejects A Negative Quantity
    [Tags]    Cart
    Fail    quantity must be positive

Cart Survives A Reload
    [Tags]    Cart
    Log    ok

Cart Is Emptied On Checkout
    [Tags]    Cart
    Log    ok
"""


# -- the whole loop ---------------------------------------------------------------


class TestEndToEnd:
    def test_a_dispatch_runs_real_robot_to_complete(self, make_service):
        service = pool_service(make_service, variables={"STAGE": "test"})
        run = execute(service, "Payments", title="Robot end to end")
        assert (run.state, run.completion) == ("COMPLETE", "FAIL")  # one deliberately failing test
        dispatch = run.latest_dispatch
        assert dispatch.mode == RUNNER and dispatch.owner is None
        verdicts = service.store.verdicts_for(run.id, dispatch.dispatch_id)
        assert len(verdicts) == 13  # every Payments-tagged test in the corpus
        statuses = {v.item_id: v.status for v in verdicts}
        assert statuses["Tests.Payments.Expired Card Is Rejected Loudly"] == "FAIL"
        assert statuses["Tests.Payments.Visa Payment Succeeds"] == "PASS"
        assert dict(dispatch.summary) == {"PASS": 12, "FAIL": 1}
        # per-dispatch isolation under the artifact root, one output per shard
        assert sorted(p.parent.name for p in workdir(service, run).glob("*/output.xml")) == [
            "default-1", "default-2",
        ]
        assert dispatch.shards == ("default-1", "default-2")
        assert {d.format for d in run.deliveries} == {"robot-output-xml"}
        assert service.require_spec(run.id)["results"]["expect"] == [{"format": "robot-output-xml"}]
        assert "robot-pool plan: 13 item(s) × 1 partition(s)" in dispatch.plan
        assert "cold start" in dispatch.plan
        kinds = [e.kind for e in service.store.events(run.id)]
        assert kinds == [
            "run.composed", "run.dispatched", "run.running",
            "delivery.recorded", "delivery.recorded", "run.completed",
        ]

    def test_the_declared_shards_say_which_items_each_reports(self, make_service):
        service = pool_service(make_service)
        run = execute(service, "Payments")
        dispatch = run.latest_dispatch
        planned = service.require_spec(run.id)["selection"]["materialized"]["item_ids"]
        assert sorted(i for items in dispatch.shard_items.values() for i in items) == sorted(planned)
        for shard, items in dispatch.shard_items.items():
            reported = {v.item_id for v in service.store.verdicts_for(run.id, dispatch.dispatch_id, shard=shard)}
            assert reported == set(items)

    def test_configured_variables_reach_the_tests(self, make_service):
        # The VarCheck test passes ONLY when STAGE=test overrides the suite's default.
        service = pool_service(make_service, variables={"STAGE": "test"})
        assert execute(service, "VarCheck").completion == "PASS"
        without = pool_service(make_service)
        assert execute(without, "VarCheck").completion == "FAIL"

    def test_a_dispatch_without_a_suite_root_is_refused(self, make_service):
        service = make_service(
            sources={"robotframework": {"root": str(ROBOT_SHOP)}}, runners={RUNNER: {}}
        )
        run_id = compose(service, "Smoke", runner_id=RUNNER)
        outcome = service.dispatch(run_id, RUNNER, foreground=True)
        assert "suite_root" in outcome.refusal
        run = service.require_run(run_id)
        assert run.state == "COMPOSED" and run.dispatches == ()
        assert events_of(service, run_id, "dispatch.refused")[0]["reason"] == outcome.refusal

    def test_health(self, make_service, tmp_path):
        import robot

        service = pool_service(make_service)
        health = service.runner_health(RUNNER, refresh=True)
        assert health.status == "ok" and robot.__version__ in health.detail
        assert health.data["suite_roots"] == [str(ROBOT_SHOP)]

        from runcomposer.plugins.robot_pool import RobotPoolRunner

        assert RobotPoolRunner().health().status == "down"
        gone = RobotPoolRunner(suite_root=str(tmp_path / "gone")).health()
        assert gone.status == "degraded" and "gone" in gone.detail

    def test_the_runner_describes_itself(self, make_service):
        (info,) = pool_service(make_service).runner_infos(health=False)
        assert info["id"] == RUNNER and info["plugin"] == "robot-pool"
        assert {"live_status", "partitions", "cancel"} <= set(info["capabilities"])
        assert info["result_format"] == "robot-output-xml"
        # no partitions configured: nothing to choose per run
        assert [p["name"] for p in info["parameters"]] == ["allow_drift"]


# -- partitions -------------------------------------------------------------------

GREEN_ON_ENV1 = """\
*** Test Cases ***
Checkout Works On The First Environment Only
    [Tags]    Checkout
    Should Be Equal    ${PARTITION}    env1

Checkout Works Everywhere
    [Tags]    Checkout
    Log    running on ${PARTITION}
"""


class TestPartitions:
    def test_two_partitions_over_the_shipped_corpus(self, make_service):
        service = pool_service(make_service, partitions=["env1", "env2"], variables={"STAGE": "test"})
        run = execute(service, "VarCheck")
        assert run.completion == "PASS"
        assert run.latest_dispatch.shards == ("env1-1", "env2-1")
        assert {d.shard for d in run.deliveries} == {"env1-1", "env2-1"}
        item = "Tests.Checkout.Stage Variable Reaches The Test"
        assert verdict_map(service, run) == {("env1-1", item, 1): "PASS", ("env2-1", item, 1): "PASS"}

    def test_a_larger_selection_fans_out_into_chunks_per_partition(self, make_service):
        from runcomposer.app.views import run_detail

        service = pool_service(make_service, partitions=["env1", "env2"], max_workers=4)
        run = execute(service, "Payments")
        assert (run.state, run.completion) == ("COMPLETE", "FAIL")
        assert run.latest_dispatch.shards == ("env1-1", "env1-2", "env2-1", "env2-2")
        assert "13 item(s) × 2 partition(s) → 2 chunk(s)/partition" in run.latest_dispatch.plan
        detail = run_detail(service, run)
        # 13 tests on each of two partitions; the one failing test fails on both.
        assert detail["verdict_summary"] == {"PASS": 24, "FAIL": 2}
        assert len(detail["verdicts"]) == 26
        per_partition = {"env1": 0, "env2": 0}
        for shard in detail["shards"]:
            assert shard["declared"] and shard["delivered"]
            per_partition[shard["shard"].split("-")[0]] += shard["count"]
        assert per_partition == {"env1": 13, "env2": 13}
        assert sorted(s["completion"] for s in detail["shards"]) == ["FAIL", "FAIL", "PASS", "PASS"]

    def test_green_here_red_there_is_read_from_the_roll_up(self, make_service, tmp_path):
        """The question fan-out exists to answer."""
        from runcomposer.app.views import run_detail

        root = shop_suite(tmp_path, GREEN_ON_ENV1)
        service = pool_service(make_service, root, partitions=["env1", "env2"])
        run = execute(service, "Checkout")
        assert (run.state, run.completion) == ("COMPLETE", "FAIL")
        detail = run_detail(service, run)
        roll_up = {s["shard"]: (s["completion"], s["summary"]) for s in detail["shards"]}
        assert roll_up == {
            "env1-1": ("PASS", {"PASS": 2}),
            "env2-1": ("FAIL", {"PASS": 1, "FAIL": 1}),
        }
        assert all(s["declared"] and s["delivered"] for s in detail["shards"])
        failed = [v for v in detail["verdicts"] if v["status"] == "FAIL"]
        assert [(v["shard"], v["item_id"]) for v in failed] == [
            ("env2-1", "Shop.Checkout.Checkout Works On The First Environment Only")
        ]
        assert detail["verdict_summary"] == {"PASS": 3, "FAIL": 1}

    def test_partitions_are_a_per_run_multi_choice_parameter(self, make_service, tmp_path):
        root = shop_suite(tmp_path, GREEN_ON_ENV1)
        service = pool_service(make_service, root, partitions=["env1", "env2"])
        (info,) = service.runner_infos(health=False)
        parameter = next(p for p in info["parameters"] if p["name"] == "partitions")
        assert parameter["kind"] == "multi_choice"
        assert parameter["choices"] == ["env1", "env2"] and parameter["default"] == ["env1", "env2"]

        # A subset selects: only the environment where everything is green.
        run = execute(service, "Checkout", parameters={"partitions": ["env1"]})
        assert run.completion == "PASS"
        # One partition instead of two: the workers go to chunks of that one.
        assert run.latest_dispatch.shards == ("env1-1", "env1-2")
        assert dict(run.latest_dispatch.parameters)["partitions"] == ["env1"]
        # The accepted value is part of the document (§3: runner.*).
        assert service.require_spec(run.id)["runner"][RUNNER]["partitions"] == ["env1"]

        only_red = execute(service, "Checkout", parameters={"partitions": ["env2"]})
        assert only_red.completion == "FAIL"
        assert only_red.latest_dispatch.shards == ("env2-1", "env2-2")

    def test_a_partition_that_is_not_configured_is_an_invalid_parameter(self, make_service, tmp_path):
        root = shop_suite(tmp_path, GREEN_ON_ENV1)
        service = pool_service(make_service, root, partitions=["env1", "env2"])
        for bad in (["env3"], "env1", [1]):
            with pytest.raises(InvalidParameter) as excinfo:
                compose(service, "Checkout", runner_id=RUNNER, parameters={"partitions": bad})
            assert excinfo.value.parameter == "partitions"
        assert service.list_runs().total == 0  # refused before any run existed

    def test_partition_variable_and_partition_variables(self, make_service, tmp_path):
        """`partition_variable` renames the variable the partition arrives in;
        `partition_variables` adds variables per partition. The test passes
        only if both reach it — and the default name is NOT defined."""
        root = shop_suite(
            tmp_path,
            "*** Test Cases ***\n"
            "Shop Url Matches The Target Environment\n"
            "    [Tags]    Checkout\n"
            "    Should Be Equal    ${SHOP_URL}    https://${TARGET_ENV}.shop.example\n"
            "    Variable Should Not Exist    ${PARTITION}\n",
        )
        service = pool_service(
            make_service,
            root,
            partitions=["env1", "env2"],
            partition_variable="TARGET_ENV",
            partition_variables={
                "env1": {"SHOP_URL": "https://env1.shop.example"},
                "env2": {"SHOP_URL": "https://env2.shop.example"},
            },
        )
        run = execute(service, "Checkout")
        assert run.completion == "PASS", [v.message for v in service.store.verdicts_for(run.id)]
        assert len(service.store.verdicts_for(run.id)) == 2

    def test_partition_variables_for_an_unknown_partition_refuse_startup(self, make_service):
        service = pool_service(
            make_service, partitions=["env1"], partition_variables={"env9": {"SHOP_URL": "x"}}
        )
        with pytest.raises(ConfigError, match=r"runners\.robot-pool.*env9"):
            service.validate()

    def test_executed_tag_is_stamped_on_every_executed_test(self, make_service, tmp_path):
        root = shop_suite(tmp_path, GREEN_ON_ENV1)
        service = pool_service(
            make_service, root, partitions=["env1", "env2"], executed_tag="Executed_{partition}"
        )
        run = execute(service, "Checkout")
        for partition in ("env1", "env2"):
            tests = executed_tests(workdir(service, run) / f"{partition}-1" / "output.xml")
            assert len(tests) == 2
            for _status, tags, _message in tests.values():
                assert f"Executed_{partition}" in tags and "Checkout" in tags
                assert not any(tag.startswith("Executed_") and tag != f"Executed_{partition}" for tag in tags)

    def test_a_malformed_executed_tag_template_refuses_startup(self, make_service):
        with pytest.raises(ConfigError, match="executed_tag"):
            pool_service(make_service, executed_tag="Executed_{environment}").validate()


# -- exact ids and several roots ----------------------------------------

GLOB_SHAPED = """\
*** Test Cases ***
Refund [partial] Works
    [Tags]    Payments
    Log    the bracketed one

Refund p Works
    [Tags]    Payments
    Log    what the brackets would match as a character class

Search * Wildcard
    [Tags]    Catalog
    Log    the starred one

Search Any Wildcard
    [Tags]    Catalog
    Log    what the star would match as a glob
"""


class TestExactIds:
    """Items are selected by exact id. Robot's own `--test` reads names as
    patterns, where `[partial]` is a character class and `*` matches anything:
    an id carrying either would run its neighbour, or nothing at all."""

    @pytest.mark.parametrize(
        "picked",
        ["Shop.Payments.Refund [partial] Works", "Shop.Payments.Search * Wildcard"],
    )
    def test_an_id_with_glob_characters_runs_exactly_that_test(self, make_service, tmp_path, picked):
        root = shop_suite(tmp_path, GLOB_SHAPED, name="payments.robot")
        service = pool_service(make_service, root)
        run = execute(service, {"item_ids": [picked]})
        assert (run.state, run.completion) == ("COMPLETE", "PASS")
        assert [v.item_id for v in service.store.verdicts_for(run.id)] == [picked]
        # …and Robot itself executed that one test and no other.
        (output,) = workdir(service, run).glob("*/output.xml")
        assert list(executed_tests(output)) == [picked]

    def test_the_plain_neighbours_are_selectable_on_their_own_too(self, make_service, tmp_path):
        root = shop_suite(tmp_path, GLOB_SHAPED, name="payments.robot")
        service = pool_service(make_service, root)
        picked = ["Shop.Payments.Refund p Works", "Shop.Payments.Search Any Wildcard"]
        run = execute(service, {"item_ids": picked})
        assert sorted(v.item_id for v in service.store.verdicts_for(run.id)) == sorted(picked)

    def test_ids_differing_only_in_case_and_spacing_are_different_tests(self, make_service, tmp_path):
        """Robot's name matching also ignores case, spaces and underscores."""
        root = shop_suite(
            tmp_path,
            "*** Test Cases ***\nCart Total\n    Log    one\n\nCart_Total Net\n    Log    two\n\n"
            "CARTTOTAL\n    Log    three\n",
            name="cart.robot",
        )
        service = pool_service(make_service, root)
        run = execute(service, {"item_ids": ["Shop.Cart.Cart Total"]})
        assert [v.item_id for v in service.store.verdicts_for(run.id)] == ["Shop.Cart.Cart Total"]
        assert run.completion == "PASS"


def two_roots(tmp_path: Path) -> tuple[Path, Path]:
    api = tmp_path / "Api"
    storefront = tmp_path / "Storefront"
    write_suite(
        api, "cart.robot",
        "*** Test Cases ***\nCart Total Responds\n    [Tags]    Smoke\n    Log    api\n",
    )
    write_suite(
        storefront, "login.robot",
        "*** Test Cases ***\nLogin Works\n    [Tags]    Smoke\n    Log    storefront\n",
    )
    return api, storefront


class TestSeveralRoots:
    def test_items_of_two_roots_each_run_from_their_own_root(self, make_service, tmp_path):
        api, storefront = two_roots(tmp_path)
        service = pool_service(
            make_service,
            source={"robotframework": {"roots": [str(api), str(storefront)]}},
            suite_roots=[str(api), str(storefront)],
            max_workers=1,
        )
        run = execute(service, "Smoke")
        assert (run.state, run.completion) == ("COMPLETE", "PASS")
        assert sorted(v.item_id for v in service.store.verdicts_for(run.id)) == [
            "Api.Cart.Cart Total Responds",
            "Storefront.Login.Login Works",
        ]
        # One shard, two executions — one per root, each seeing only its own.
        shard = workdir(service, run) / "default"
        per_execution = sorted(
            list(executed_tests(output)) for output in shard.glob("execution-*/output.xml")
        )
        assert per_execution == [["Api.Cart.Cart Total Responds"], ["Storefront.Login.Login Works"]]
        # …and one merged result for people.
        assert len(executed_tests(shard / "output.xml")) == 2
        assert (shard / "log.html").is_file()

    def test_relative_suite_roots_resolve_against_the_config_file(self, make_service, tmp_path):
        two_roots(tmp_path)
        service = pool_service(
            make_service,
            source={"robotframework": {"roots": ["Api", "Storefront"]}},
            suite_roots=["Api", "Storefront"],
        )
        assert execute(service, "Smoke").completion == "PASS"

    def test_a_catalog_wider_than_the_single_suite_root_is_not_green(self, make_service, tmp_path):
        """H2: a two-root catalog met a single `suite_root`, the other root's
        items were silently not executed, and the run completed PASS."""
        api, storefront = two_roots(tmp_path)
        service = pool_service(
            make_service, api, source={"robotframework": {"roots": [str(api), str(storefront)]}}
        )
        run = execute(service, "Smoke")
        assert (run.state, run.completion) == ("COMPLETE", "INCOMPLETE")
        assert dict(run.latest_dispatch.summary) == {"PASS": 1, "MISSING": 1}
        missing = [v for v in service.ledger.scored_verdicts(run.latest_dispatch) if v.status == "MISSING"]
        assert [v.item_id for v in missing] == ["Storefront.Login.Login Works"]
        # …and the run says why, instead of leaving it to be worked out.
        (warning,) = [w["message"] for w in events_of(service, run.id, "warning")]
        assert "1 of 1 selected test(s) do not exist under" in warning and str(api) in warning

    def test_the_same_holds_when_both_items_share_one_shard(self, make_service, tmp_path):
        """Whether the stray item sits alone in a shard or next to one that
        runs must not change the outcome."""
        api, storefront = two_roots(tmp_path)
        service = pool_service(
            make_service, api, max_workers=1,
            source={"robotframework": {"roots": [str(api), str(storefront)]}},
        )
        run = execute(service, "Smoke")
        assert (run.state, run.completion) == ("COMPLETE", "INCOMPLETE")
        assert dict(run.latest_dispatch.summary) == {"PASS": 1, "MISSING": 1}
        (warning,) = [w["message"] for w in events_of(service, run.id, "warning")]
        assert "1 of 2 selected test(s) do not exist under" in warning

    def test_an_item_that_belongs_to_no_configured_root_refuses_the_dispatch(self, make_service, tmp_path):
        api, storefront = two_roots(tmp_path)
        other = tmp_path / "Backoffice"
        write_suite(other, "s.robot", "*** Test Cases ***\nStock Is Counted\n    Log    ok\n")
        service = pool_service(
            make_service,
            source={"robotframework": {"roots": [str(api), str(storefront)]}},
            suite_roots=[str(api), str(other)],
        )
        run_id = compose(service, "Smoke", runner_id=RUNNER)
        outcome = service.dispatch(run_id, RUNNER, foreground=True)
        assert "Storefront.Login.Login Works" in outcome.refusal
        assert "none of the configured suite roots" in outcome.refusal
        assert service.require_run(run_id).dispatches == ()


# -- artifacts ----------------------------------------------------------------------


class TestArtifacts:
    def test_log_and_report_are_written_recorded_and_served(self, make_client, tmp_path):
        """Over HTTP, end to end: the POST returns, the run completes, and a
        tester reaches log.html from the run detail."""
        root = shop_suite(tmp_path, FIVE_TESTS, name="cart.robot")
        client, service = make_client(
            sources={"robotframework": {"root": str(root)}},
            runners={RUNNER: {"suite_root": str(root), "max_workers": 1}},
        )
        response = client.post(
            "/api/v1/runs",
            json={"selection": {"tag_filter": "Cart"}, "title": "Cart regression",
                  "dispatch": {"runner": RUNNER}},
        )
        assert response.status_code == 201, response.text
        run_id = response.json()["run"]["id"]
        detail = wait_until(
            lambda: (d := client.get(f"/api/v1/runs/{run_id}").json())["state"] == "COMPLETE" and d,
            what="the run to complete",
        )
        assert detail["completion"] == "FAIL" and detail["verdict_summary"] == {"PASS": 4, "FAIL": 1}
        artifacts = {a["name"]: a for a in detail["artifacts"]}
        assert set(artifacts) == {"log.html (default)", "report.html (default)", "output.xml (default)"}
        for artifact in artifacts.values():
            assert artifact["kind"] == "local" and artifact["shard"] == "default"
            assert artifact["href"].startswith(f"/artifacts/{run_id}/{detail['dispatch_id']}/default/")
        log = client.get(artifacts["log.html (default)"]["href"])
        assert log.status_code == 200 and log.headers["content-type"].startswith("text/html")
        assert "Cart regression" not in log.text and "<html" in log.text.lower()
        # Untrusted bytes on the API's origin: sandboxed, never sniffed.
        assert "sandbox" in log.headers["content-security-policy"]
        assert "default-src 'none'" in log.headers["content-security-policy"]
        assert log.headers["x-content-type-options"] == "nosniff"
        output = client.get(artifacts["output.xml (default)"]["href"])
        assert output.status_code == 200 and b"Cart Rejects A Negative Quantity" in output.content
        # Deleting the run takes its artifacts with it.
        assert client.delete(f"/api/v1/runs/{run_id}").status_code == 204
        assert client.get(artifacts["log.html (default)"]["href"]).status_code == 404
        assert not (service.config.artifact_dir / run_id).exists()

    def test_log_and_report_can_be_turned_off(self, make_service, tmp_path):
        root = shop_suite(tmp_path, FIVE_TESTS, name="cart.robot")
        service = pool_service(make_service, root, max_workers=1, log=False, report=False)
        run = execute(service, "Cart")
        assert [a["name"] for a in service.artifacts.list(run.id)] == ["output.xml (default)"]
        shard = workdir(service, run) / "default"
        assert (shard / "output.xml").is_file()
        assert not (shard / "log.html").exists() and not (shard / "report.html").exists()


# -- bounded executions, merged per shard ------------------------------------------


class TestMaxTestsPerChunk:
    def test_a_shard_is_cut_into_executions_and_merged_back(self, make_service, tmp_path):
        root = shop_suite(tmp_path, FIVE_TESTS, name="cart.robot")
        service = pool_service(make_service, root, max_workers=1, max_tests_per_chunk=2)
        run = execute(service, "Cart")
        assert (run.state, run.completion) == ("COMPLETE", "FAIL")
        assert run.latest_dispatch.shards == ("default",)  # still ONE shard
        assert "at most 2 test(s) per execution" in run.latest_dispatch.plan
        shard = workdir(service, run) / "default"
        sizes = sorted(
            len(executed_tests(output)) for output in shard.glob("execution-*/output.xml")
        )
        assert sizes == [1, 2, 2]  # 5 tests, at most 2 per execution

        # The delivered verdicts are complete, once each, with Robot's own messages…
        verdicts = service.store.verdicts_for(run.id)
        assert sorted(v.item_id for v in verdicts) == sorted(
            f"Shop.Cart.{name}" for name in (
                "Cart Holds One Item", "Cart Holds Two Items", "Cart Rejects A Negative Quantity",
                "Cart Survives A Reload", "Cart Is Emptied On Checkout",
            )
        )
        assert {v.attempt for v in verdicts} == {1}
        messages = {v.item_id.rsplit(".", 1)[1]: v.message for v in verdicts}
        assert messages["Cart Rejects A Negative Quantity"] == "quantity must be positive"
        assert all(m == "" for name, m in messages.items() if name != "Cart Rejects A Negative Quantity")
        # …while people get one merged output and one log for the shard.
        merged = executed_tests(shard / "output.xml")
        assert len(merged) == 5
        assert merged["Shop.Cart.Cart Rejects A Negative Quantity"][0] == "FAIL"
        assert (shard / "log.html").is_file() and (shard / "report.html").is_file()
        assert [a["name"] for a in service.artifacts.list(run.id)] == [
            "log.html (default)", "report.html (default)", "output.xml (default)",
        ]

    def test_without_the_bound_a_shard_is_one_execution(self, make_service, tmp_path):
        root = shop_suite(tmp_path, FIVE_TESTS, name="cart.robot")
        service = pool_service(make_service, root, max_workers=1)
        run = execute(service, "Cart")
        shard = workdir(service, run) / "default"
        assert list(shard.glob("execution-*")) == []
        assert len(executed_tests(shard / "output.xml")) == 5


# -- worker preparation ---------------------------------------------------------------

#: Set by a test in THIS process before dispatching. A forked worker would
#: inherit it; a spawned one imports this module afresh and sees the default.
_SET_IN_THE_PARENT = False


def setup_fn(info: dict) -> dict:
    """A `worker_setup` callable: runs inside the worker before Robot starts,
    records where it ran, and contributes a Robot variable."""
    record = {
        "pid": os.getpid(),
        "process_name": multiprocessing.current_process().name,
        "inherited_parent_state": _SET_IN_THE_PARENT,
        "info": {key: info[key] for key in ("shard", "partition", "run_id", "dispatch_id")},
        "variables": info["variables"],
    }
    Path(info["out_dir"], "worker-setup.json").write_text(json.dumps(record), encoding="utf-8")
    return {"PREPARED_BY": f"setup-{info['partition']}"}


def setup_without_variables(info: dict) -> None:
    Path(info["out_dir"], "second-setup.txt").write_text(info["shard"], encoding="utf-8")


class TestWorkerSetup:
    SUITE = (
        "*** Test Cases ***\n"
        "Checkout Sees What The Worker Prepared\n"
        "    [Tags]    Checkout\n"
        "    Should Be Equal    ${PREPARED_BY}    setup-default\n"
    )

    def test_setup_callables_run_in_a_spawned_worker_and_contribute_variables(
        self, make_service, tmp_path, monkeypatch
    ):
        root = shop_suite(tmp_path, self.SUITE)
        service = pool_service(
            make_service,
            root,
            variables={"STAGE": "test"},
            worker_setup=[
                "tests.test_robot_pool:setup_fn",
                "tests.test_robot_pool:setup_without_variables",
            ],
            pythonpath=[str(REPO)],
        )
        monkeypatch.setattr("tests.test_robot_pool._SET_IN_THE_PARENT", True)
        contexts: list[str] = []
        real_get_context = multiprocessing.get_context

        def recording(method=None):
            contexts.append(method)
            return real_get_context(method)

        monkeypatch.setattr(multiprocessing, "get_context", recording)
        run = execute(service, "Checkout")
        assert contexts == ["spawn"]  # asked for explicitly, whatever the platform default
        assert run.completion == "PASS", [v.message for v in service.store.verdicts_for(run.id)]

        shard = workdir(service, run) / "default"
        record = json.loads((shard / "worker-setup.json").read_text(encoding="utf-8"))
        assert record["pid"] != os.getpid()  # in the worker, not here
        assert record["process_name"] == "robot-pool-default"
        # The pool uses the `spawn` start method: nothing of this process's
        # state is inherited (a fork from a server's worker thread would).
        assert record["inherited_parent_state"] is False
        assert record["info"] == {
            "shard": "default", "partition": "default", "run_id": run.id,
            "dispatch_id": run.latest_dispatch.dispatch_id,
        }
        assert record["variables"] == {"STAGE": "test", "PARTITION": "default"}
        assert (shard / "second-setup.txt").read_text(encoding="utf-8") == "default"

    def test_a_setup_that_raises_fails_the_dispatch_with_its_message(self, make_service, tmp_path):
        root = shop_suite(tmp_path, self.SUITE)
        service = pool_service(
            make_service, root, worker_setup=["tests.test_robot_pool:setup_that_fails"],
            pythonpath=[str(REPO)],
        )
        run = execute(service, "Checkout")
        assert run.state == "FAILED" and run.completion is None
        assert "produced no results" in run.latest_dispatch.error
        assert "the staging database is not reachable" in run.latest_dispatch.error
        assert dict(run.latest_dispatch.summary) == {"MISSING": 1}

    def test_worker_setup_must_be_an_import_path(self, make_service):
        with pytest.raises(ConfigError, match=r"worker_setup.*package\.module:callable"):
            pool_service(make_service, worker_setup=["prepare_environment"]).validate()

    def test_pythonpath_makes_a_keyword_library_importable(self, make_service, tmp_path):
        """Relative, so it also proves the option is anchored to the config file."""
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "ShopKeywords.py").write_text(
            "def cart_total_should_be(expected):\n"
            "    assert int(expected) == 3, f'cart total is 3, not {expected}'\n",
            encoding="utf-8",
        )
        root = shop_suite(
            tmp_path,
            "*** Settings ***\nLibrary    ShopKeywords\n\n"
            "*** Test Cases ***\nCart Total Is Three\n    [Tags]    Cart\n    Cart Total Should Be    3\n",
            name="cart.robot",
        )
        service = pool_service(make_service, root, pythonpath=["lib"])
        run = execute(service, "Cart")
        assert run.completion == "PASS", [v.message for v in service.store.verdicts_for(run.id)]
        without = pool_service(make_service, root)
        assert execute(without, "Cart").completion == "FAIL"  # the import fails, the test with it


def setup_that_fails(info: dict) -> None:
    raise RuntimeError("the staging database is not reachable")


# -- re-executing failures -----------------------------------------------------------

FLAKY = """\
*** Settings ***
Library    OperatingSystem

*** Test Cases ***
Flaky Checkout Recovers
    [Tags]    Checkout
    ${seen}=    Run Keyword And Return Status    File Should Exist    ${STATE_FILE}
    Create File    ${STATE_FILE}    seen
    Should Be True    ${seen}    the first execution always fails

Steady Checkout Passes
    [Tags]    Checkout
    Log    ok
"""


class TestRerunFailed:
    def test_a_test_that_passes_on_its_second_execution_ends_pass(self, make_service, tmp_path):
        root = shop_suite(tmp_path, FLAKY)
        service = pool_service(
            make_service, root, max_workers=1, rerun_failed=1,
            variables={"STATE_FILE": str(tmp_path / "flaky.state")},
        )
        run = execute(service, "Checkout")
        assert (run.state, run.completion) == ("COMPLETE", "PASS")
        flaky, steady = "Shop.Checkout.Flaky Checkout Recovers", "Shop.Checkout.Steady Checkout Passes"
        # Both attempts are recorded; the last one counts.
        assert verdict_map(service, run) == {
            ("default", flaky, 1): "FAIL",
            ("default", steady, 1): "PASS",
            ("default", flaky, 2): "PASS",
        }
        assert dict(run.latest_dispatch.summary) == {"PASS": 2}
        assert "re-executed up to 1 time(s)" in run.latest_dispatch.plan
        first = next(v for v in service.store.verdicts_for(run.id) if v.attempt == 1 and v.item_id == flaky)
        assert "the first execution always fails" in first.message
        # The merged output people read shows the final state: both green.
        shard = workdir(service, run) / "default"
        assert {name: status for name, (status, _t, _m) in executed_tests(shard / "output.xml").items()} == {
            flaky: "PASS", steady: "PASS",
        }
        assert list(executed_tests(shard / "attempt-2" / "output.xml")) == [flaky]  # only the failure reran

    def test_without_rerun_the_same_suite_fails(self, make_service, tmp_path):
        root = shop_suite(tmp_path, FLAKY)
        service = pool_service(
            make_service, root, max_workers=1,
            variables={"STATE_FILE": str(tmp_path / "flaky.state")},
        )
        run = execute(service, "Checkout")
        assert run.completion == "FAIL"
        assert {attempt for _s, _i, attempt in verdict_map(service, run)} == {1}

    def test_a_test_that_keeps_failing_is_tried_the_configured_number_of_times(self, make_service, tmp_path):
        root = shop_suite(tmp_path, FIVE_TESTS, name="cart.robot")
        service = pool_service(make_service, root, max_workers=1, rerun_failed=2)
        run = execute(service, "Cart")
        assert run.completion == "FAIL"
        failing = "Shop.Cart.Cart Rejects A Negative Quantity"
        attempts = sorted(a for (_s, item, a), status in verdict_map(service, run).items()
                          if item == failing and status == "FAIL")
        assert attempts == [1, 2, 3]
        assert dict(run.latest_dispatch.summary) == {"PASS": 4, "FAIL": 1}

    def test_a_negative_count_refuses_startup(self, make_service):
        with pytest.raises(ConfigError, match="rerun_failed"):
            pool_service(make_service, rerun_failed=-1).validate()


# -- a run in flight: live status, cancel, timeout -------------------------------------


def staged_suite(tmp_path: Path, slow_seconds: int) -> Path:
    """Two Checkout tests: one that finishes at once and one that stays in
    flight for ``slow_seconds`` — long enough to be looked at, and (for the
    tests that stop it) far longer than any machine needs to notice."""
    return shop_suite(
        tmp_path,
        "*** Test Cases ***\n"
        "Quick Checkout Finishes\n    [Tags]    Checkout\n    Log    done at once\n\n"
        f"Slow Checkout Stays In Flight\n    [Tags]    Checkout\n    Sleep    {slow_seconds}s\n",
    )


QUICK = "Shop.Checkout.Quick Checkout Finishes"
SLOW = "Shop.Checkout.Slow Checkout Stays In Flight"


class TestLiveStatus:
    def test_a_running_run_shows_its_dispatch_and_what_already_reported(
        self, make_service, slow_lane_suite
    ):
        """Two shards, one much slower: there is a stretch in which exactly
        one has delivered while the dispatch is RUNNING. The test waits for
        that state — it does not sleep into it."""
        from runcomposer.app.views import run_detail

        service = pool_service(make_service, slow_lane_suite)
        run_id = compose(service, "SlowLane", runner_id=RUNNER)
        outcome = service.dispatch(run_id, RUNNER)  # in the background, as the server does

        def one_shard_in() -> dict | None:
            detail = run_detail(service, service.require_run(run_id))
            delivered = [s for s in detail["shards"] if s["delivered"]]
            return detail if detail["state"] == "RUNNING" and len(delivered) == 1 else None

        mid = wait_until(one_shard_in, what="exactly one delivered shard while RUNNING")
        # The hand-off is recorded when it happens, not when it finishes (§4).
        assert [d["dispatch_id"] for d in mid["dispatches"]] == [outcome.dispatch_id]
        assert mid["dispatches"][0]["executing"] is True and mid["completion"] is None
        assert [s["shard"] for s in mid["shards"]] == ["default-1", "default-2"]
        assert [v["status"] for v in mid["verdicts"] if not v["live"]] == ["PASS"]

        done = service.wait(run_id, timeout=60)
        assert (done.state, done.completion) == ("COMPLETE", "PASS")
        assert [d.dispatch_id for d in done.dispatches] == [outcome.dispatch_id]
        assert len(service.store.verdicts_for(run_id)) == 2
        assert service.store.live_verdicts(run_id, outcome.dispatch_id) == []

    def test_items_stream_while_their_shard_is_still_executing(self, make_service, tmp_path):
        """One shard, a quick test then a slow one: the quick test's status is
        visible — flagged live — while the shard has delivered nothing."""
        from runcomposer.app.views import run_detail

        service = pool_service(make_service, staged_suite(tmp_path, 3), max_workers=1)
        run_id = compose(service, "Checkout", runner_id=RUNNER)
        outcome = service.dispatch(run_id, RUNNER)

        live = wait_until(
            lambda: service.store.live_verdicts(run_id, outcome.dispatch_id),
            what="a live verdict from the executing shard",
        )
        assert [(v.item_id, v.status, v.shard) for v in live] == [(QUICK, "PASS", "default")]
        mid = run_detail(service, service.require_run(run_id))
        assert mid["state"] == "RUNNING"
        assert mid["shards"] == [
            {"shard": "default", "declared": True, "delivered": False, "count": 0,
             "planned": 2, "summary": {}, "completion": None}
        ]
        assert [(v["item_id"], v["live"]) for v in mid["verdicts"]] == [(QUICK, True)]
        assert service.store.verdicts_for(run_id) == []  # live status is not a result

        done = service.wait(run_id, timeout=60)
        assert (done.state, done.completion) == ("COMPLETE", "PASS")
        # The shard's delivery replaced its live rows.
        assert service.store.live_verdicts(run_id, outcome.dispatch_id) == []
        final = run_detail(service, done)
        assert sorted((v["item_id"], v["live"]) for v in final["verdicts"]) == [(QUICK, False), (SLOW, False)]

    def test_without_the_listener_there_are_only_final_results(self, make_service, tmp_path):
        root = shop_suite(tmp_path, FIVE_TESTS, name="cart.robot")
        service = pool_service(make_service, root, live_status=False)
        run = execute(service, "Cart")
        assert run.state == "COMPLETE"
        assert len(service.store.verdicts_for(run.id)) == 5
        assert not (workdir(service, run) / ".live").exists()  # no channel was ever opened

    def test_the_listener_reports_through_the_file_live_channel(self, tmp_path):
        """The listener needs no store: it writes to the picklable channel the
        context hands out, and the dispatching process reads that back."""
        import pickle

        from runcomposer.app.livechannel import FileLiveChannel, LiveTail
        from runcomposer.plugins.robot_listener import LiveStatusListener

        channel = pickle.loads(pickle.dumps(FileLiveChannel(str(tmp_path))))
        listener = LiveStatusListener(channel, "env1-1")
        tail = LiveTail(tmp_path)
        assert list(tail.read()) == []
        listener.end_test(
            None, SimpleNamespace(longname=QUICK, status="PASS", elapsedtime=12, message="")
        )
        listener.end_test(
            None, SimpleNamespace(longname=SLOW, status="NOT RUN", elapsedtime=0, message="skipped")
        )
        assert list(tail.read()) == [
            {"shard": "env1-1", "item_id": QUICK, "status": "PASS", "duration_ms": 12, "message": ""},
            {"shard": "env1-1", "item_id": SLOW, "status": "SKIP", "duration_ms": 0, "message": "skipped"},
        ]
        assert list(tail.read()) == []  # each line is read once
        # A channel that cannot write never breaks the test run it reports on.
        LiveStatusListener(FileLiveChannel(str(tmp_path / "gone")), "x").end_test(
            None, SimpleNamespace(longname=QUICK, status="PASS", elapsedtime=1, message="")
        )


class TestCancel:
    def test_cancel_mid_run_stops_the_outstanding_shard(self, make_service, tmp_path):
        """The slow test would sleep for a minute; cancelling ends the run in
        a fraction of that, keeps what was delivered, and says what is missing."""
        service = pool_service(make_service, staged_suite(tmp_path, 60))
        run_id = compose(service, "Checkout", runner_id=RUNNER)
        outcome = service.dispatch(run_id, RUNNER)
        wait_until(
            lambda: service.store.delivered_shards(run_id, outcome.dispatch_id)
            and service.require_run(run_id).state == "RUNNING",
            what="the quick shard to deliver while the slow one runs",
        )
        cancelled_at = time.monotonic()
        requested = service.cancel(run_id)
        assert requested.cancel_requested is True
        run = service.wait(run_id, timeout=30)
        assert time.monotonic() - cancelled_at < 20  # not the minute the slow test wanted
        assert (run.state, run.completion) == ("CANCELLED", None)
        dispatch = run.latest_dispatch
        assert dispatch.owner is None and "cancelled" in dispatch.error
        # The delivered shard is kept; the item that never reported is MISSING.
        assert [(v.item_id, v.status) for v in service.store.verdicts_for(run_id)] == [(QUICK, "PASS")]
        assert dict(dispatch.summary) == {"PASS": 1, "MISSING": 1}
        scored = {v.item_id: v.status for v in service.ledger.scored_verdicts(dispatch)}
        assert scored == {QUICK: "PASS", SLOW: "MISSING"}
        kinds = [e.kind for e in service.store.events(run_id)]
        assert kinds.index("run.cancel_requested") < kinds.index("run.cancelled")
        assert "run.completed" not in kinds

        # Nothing is executing any more, and the stopped shard delivered nothing.
        assert service.dispatcher.join(timeout=10)
        assert service.dispatcher.active_dispatch_ids() == []
        slow_shard = next(s for s in dispatch.shards if SLOW in dispatch.shard_items[s])
        assert slow_shard not in service.store.delivered_shards(run_id, dispatch.dispatch_id)
        assert [a["shard"] for a in service.artifacts.list(run_id)].count(slow_shard) == 0

    def test_cancel_does_not_go_on_to_the_shard_s_next_execution(self, make_service, tmp_path):
        """A shard cut into several executions: stopping the one in flight
        must not be followed by starting the next."""
        root = shop_suite(
            tmp_path,
            "*** Test Cases ***\n"
            "Slow Checkout Stays In Flight\n    [Tags]    Checkout\n    Sleep    60s\n\n"
            "Quick Checkout Finishes\n    [Tags]    Checkout\n    Log    never reached\n",
        )
        service = pool_service(make_service, root, max_workers=1, max_tests_per_chunk=1)
        run_id = compose(service, "Checkout", runner_id=RUNNER)
        outcome = service.dispatch(run_id, RUNNER)
        shard = service.config.artifact_dir / run_id / outcome.dispatch_id / "default"
        # Robot opens its output file when an execution starts.
        wait_until(
            lambda: (shard / "execution-1" / "output.xml").exists(),
            what="the first execution to be in flight",
        )
        service.cancel(run_id)
        run = service.wait(run_id, timeout=30)
        assert run.state == "CANCELLED"
        assert not (shard / "execution-2").exists()
        assert service.store.verdicts_for(run_id) == []
        assert dict(run.latest_dispatch.summary) == {"MISSING": 2}

    def test_a_shard_that_overruns_its_timeout_fails_the_dispatch(self, make_service, tmp_path):
        service = pool_service(make_service, staged_suite(tmp_path, 60), shard_timeout_s=4)
        started = time.monotonic()
        run = execute(service, "Checkout")
        assert time.monotonic() - started < 40
        assert (run.state, run.completion) == ("FAILED", None)
        dispatch = run.latest_dispatch
        assert "exceeded shard_timeout_s=4" in dispatch.error and "1 of 2 shard(s)" in dispatch.error
        # Partial results are kept: the finished shard's verdict is there.
        assert [(v.item_id, v.status) for v in service.store.verdicts_for(run.id)] == [(QUICK, "PASS")]
        assert dict(dispatch.summary) == {"PASS": 1, "MISSING": 1}
        assert events_of(service, run.id, "run.failed")[0]["error"] == dispatch.error


# -- §3.3 drift -------------------------------------------------------------------------


def drop_test_case(path: Path, name: str) -> None:
    """Delete one test case from a .robot file: its name line plus its body."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.rstrip("\n") == name)
    after = range(start + 1, len(lines))
    end = next((i for i in after if lines[i].strip() and not lines[i][:1].isspace()), len(lines))
    path.write_text("".join(lines[:start] + lines[end:]), encoding="utf-8")


class TestDrift:
    """Refuse by default; `allow_drift` executes the intersection and reports
    the difference as SKIP verdicts with reason drift."""

    def composed_then_edited(self, make_service, tmp_path, **options):
        corpus = tmp_path / "tests"
        shutil.copytree(ROBOT_SHOP, corpus)
        service = pool_service(make_service, corpus, **options)
        run_id = compose(service, "Catalog", runner_id=RUNNER)
        planned = service.require_spec(run_id)["selection"]["materialized"]["item_ids"]
        assert len(planned) == 8
        (corpus / "catalog.robot").unlink()  # the corpus drifts after composing
        return service, run_id, planned

    def test_drift_is_refused_and_leaves_no_dispatch(self, make_service, tmp_path):
        service, run_id, _planned = self.composed_then_edited(make_service, tmp_path)
        outcome = service.dispatch(run_id, RUNNER, foreground=True)
        assert "drift" in outcome.refusal and "allow_drift" in outcome.refusal
        run = service.require_run(run_id)
        assert run.state == "COMPOSED"
        # The drift check runs before the hand-off: a refusal leaves no
        # dispatch to masquerade as an execution (§4) — the reason is an event.
        assert run.dispatches == ()
        assert service.store.verdicts_for(run_id) == []
        (refused,) = events_of(service, run_id, "dispatch.refused")
        assert refused["reason"] == outcome.refusal and refused["crashed"] is False
        assert not (service.config.artifact_dir / run_id).exists() or not list(
            (service.config.artifact_dir / run_id).rglob("output.xml")
        )

    def test_a_refused_redispatch_does_not_erase_the_finished_run(self, make_service, tmp_path):
        """H4: the refusal used to reset a COMPLETE (FAIL) run to COMPOSED."""
        corpus = tmp_path / "tests"
        shutil.copytree(ROBOT_SHOP, corpus)
        service = pool_service(make_service, corpus)
        run = execute(service, "Payments-Cards")
        assert (run.state, run.completion) == ("COMPLETE", "FAIL")
        drop_test_case(corpus / "catalog.robot", "Sort By Price Ascending")
        outcome = service.dispatch(run.id, RUNNER, foreground=True)
        assert "drift" in outcome.refusal
        after = service.require_run(run.id)
        assert (after.state, after.completion) == ("COMPLETE", "FAIL")
        assert [d.dispatch_id for d in after.dispatches] == [run.latest_dispatch.dispatch_id]

    def test_configured_allow_drift_executes_the_intersection(self, make_service, tmp_path):
        service, run_id, planned = self.composed_then_edited(make_service, tmp_path, allow_drift=True)
        run = run_to_end(service, run_id, RUNNER)
        assert run.state == "COMPLETE"
        verdicts = service.store.verdicts_for(run_id)
        drifted = [v for v in verdicts if v.status == "SKIP" and v.message == "drift"]
        assert {v.item_id for v in drifted} == set(planned)  # every Catalog test lived in that file
        assert {v.shard for v in drifted} == {"drift"} and run.latest_dispatch.shards == ("drift",)
        assert "drift: 8 planned item(s) no longer exist" in run.latest_dispatch.plan

    def test_allow_drift_as_a_per_run_parameter(self, make_service, tmp_path):
        """The person starting the run may decide it — the runner declares
        `allow_drift` as a parameter; the deployment default stays "refuse"."""
        corpus = tmp_path / "tests"
        shutil.copytree(ROBOT_SHOP, corpus)
        service = pool_service(make_service, corpus)
        run_id = compose(service, "Payments-Refunds", runner_id=RUNNER, parameters={"allow_drift": True})
        planned = service.require_spec(run_id)["selection"]["materialized"]["item_ids"]
        gone = "Tests.Payments.Refund Restores Balance"
        assert gone in planned and len(planned) == 3
        drop_test_case(corpus / "payments.robot", "Refund Restores Balance")

        refused = service.dispatch(run_id, RUNNER, {"allow_drift": False}, foreground=True)
        assert "drift" in refused.refusal

        run = run_to_end(service, run_id, RUNNER, {"allow_drift": True})
        assert (run.state, run.completion) == ("COMPLETE", "PASS")
        by_item = {v.item_id: (v.status, v.message, v.shard) for v in service.store.verdicts_for(run_id)}
        assert by_item[gone] == ("SKIP", "drift", "drift")
        assert sorted(s for i, (s, _m, _sh) in by_item.items() if i != gone) == ["PASS", "PASS"]
        assert dict(run.latest_dispatch.summary) == {"PASS": 2, "SKIP": 1}


# -- hooks and listeners ------------------------------------------------------------------


class TestHooksAndListeners:
    def test_pre_run_hooks_run_before_anything_executes(self, make_service, tmp_path):
        sentinel = tmp_path / "hook-ran.txt"
        root = shop_suite(
            tmp_path,
            "*** Settings ***\nLibrary    OperatingSystem\n\n"
            "*** Test Cases ***\nCheckout Finds The Prepared Environment\n"
            f"    [Tags]    Checkout\n    File Should Exist    {sentinel}\n",
        )
        service = pool_service(make_service, root, pre_run_hooks=[f"echo prepared > '{sentinel}'"])
        assert execute(service, "Checkout").completion == "PASS"
        assert sentinel.read_text(encoding="utf-8").strip() == "prepared"

    def test_a_failing_hook_refuses_the_dispatch(self, make_service):
        service = pool_service(make_service, pre_run_hooks=["echo doomed >&2; exit 3"])
        run_id = compose(service, "Smoke", runner_id=RUNNER)
        outcome = service.dispatch(run_id, RUNNER, foreground=True)
        assert "pre_run_hook failed (rc 3)" in outcome.refusal and "doomed" in outcome.refusal
        run = service.require_run(run_id)
        assert run.state == "COMPOSED" and run.dispatches == ()
        assert service.store.verdicts_for(run_id) == []

    def test_a_hook_that_overruns_its_timeout_refuses_promptly(self, make_service, tmp_path):
        """The hook starts something that would run for a minute and then
        report. The refusal does not wait for that — and what the hook
        started is stopped with it, not left running."""
        pid_file = tmp_path / "hook-child.pid"
        hook = f"sleep 60 & echo $! > '{pid_file}'; wait; echo too late"
        service = pool_service(make_service, pre_run_hooks=[hook], hook_timeout_s=1)
        run_id = compose(service, "Smoke", runner_id=RUNNER)
        started = time.monotonic()
        outcome = service.dispatch(run_id, RUNNER, foreground=True)
        assert time.monotonic() - started < 30
        assert "pre_run_hook did not finish within 1s" in outcome.refusal
        assert service.require_run(run_id).dispatches == ()

        child = int(pid_file.read_text(encoding="utf-8"))

        def gone() -> bool:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return True
            return False

        wait_until(gone, timeout=10, what="the hook's child process to be gone")

    def test_a_user_listener_receives_the_run(self, make_service, tmp_path):
        listener_file = tmp_path / "SentinelListener.py"
        seen = tmp_path / "seen.txt"
        listener_file.write_text(
            "class SentinelListener:\n"
            "    ROBOT_LISTENER_API_VERSION = 3\n"
            "    def __init__(self, out):\n"
            "        self.out = out\n"
            "    def end_test(self, data, result):\n"
            "        with open(self.out, 'a') as fh:\n"
            "            fh.write(result.name + '\\n')\n",
            encoding="utf-8",
        )
        root = shop_suite(tmp_path, FIVE_TESTS, name="cart.robot")
        service = pool_service(make_service, root, listener=f"{listener_file}:{seen}")
        run = execute(service, "Cart")
        assert run.state == "COMPLETE"
        assert sorted(seen.read_text(encoding="utf-8").splitlines()) == sorted(
            v.item_id.rsplit(".", 1)[1] for v in service.store.verdicts_for(run.id)
        )
        # …next to runcomposer's own listener, not instead of it.
        assert (workdir(service, run) / ".live").is_dir()


# -- the configured parser is honoured ------------------------------------------------


class TaggingParser:
    """A deployment's own parser for Robot output: the stock one, with every
    message marked — so a test can see WHICH parser produced a verdict."""

    format_id = "robot-output-xml"

    def __init__(self, mark: str = "[parsed by the shop]"):
        from runcomposer.plugins.robot_output_parser import RobotOutputXmlParser

        self._inner = RobotOutputXmlParser()
        self.mark = mark

    def parse(self, path: Any) -> list:
        return [
            dataclasses.replace(parsed, message=f"{self.mark} {parsed.message}".strip())
            for parsed in self._inner.parse(path)
        ]


class TestConfiguredParser:
    def test_the_parsers_section_decides_how_the_runner_s_output_is_read(self, make_service, tmp_path):
        """The runner used to hardcode its parser, so a configured one could
        only ever act on the ingestion path."""
        root = shop_suite(tmp_path, FIVE_TESTS, name="cart.robot")
        service = pool_service(
            make_service,
            root,
            parsers={
                "robot-output-xml": {"module": "tests.test_robot_pool:TaggingParser", "mark": "[shop]"}
            },
        )
        run = execute(service, "Cart")
        assert run.completion == "FAIL"
        messages = {v.item_id.rsplit(".", 1)[1]: v.message for v in service.store.verdicts_for(run.id)}
        assert messages["Cart Rejects A Negative Quantity"] == "[shop] quantity must be positive"
        assert set(messages.values()) == {"[shop]", "[shop] quantity must be positive"}

    def test_the_stock_parser_is_the_default(self, make_service, tmp_path):
        root = shop_suite(tmp_path, FIVE_TESTS, name="cart.robot")
        run_service = pool_service(make_service, root)
        run = execute(run_service, "Cart")
        assert {v.message for v in run_service.store.verdicts_for(run.id)} == {"", "quantity must be positive"}


# -- duration history ---------------------------------------------------------------------


class TestDurationHistory:
    SUITE = (
        "*** Test Cases ***\n"
        "Slow Cart Sync\n    [Tags]    Cart\n    Sleep    0.6s\n\n"
        "Cart Add\n    [Tags]    Cart\n    Sleep    0.05s\n\n"
        "Cart Remove\n    [Tags]    Cart\n    Sleep    0.05s\n\n"
        "Cart Clear\n    [Tags]    Cart\n    Sleep    0.05s\n"
    )

    def test_the_second_run_plans_duration_balanced_and_says_so(self, make_service, tmp_path):
        root = shop_suite(tmp_path, self.SUITE, name="cart.robot")
        service = pool_service(make_service, root)
        first = execute(service, "Cart")
        assert "cold start: no duration history" in first.latest_dispatch.plan
        assert sorted(len(items) for items in first.latest_dispatch.shard_items.values()) == [2, 2]

        second = execute(service, "Cart")
        plan = second.latest_dispatch.plan
        assert "duration-balanced chunking: history for 4/4 item(s)" in plan
        assert "cold start" not in plan
        # Longest first onto the lightest chunk: the slow test ends up alone.
        chunks = sorted(second.latest_dispatch.shard_items.values(), key=len)
        assert chunks[0] == ("Shop.Cart.Slow Cart Sync",) and len(chunks[1]) == 3
        assert second.completion == "PASS"

    def test_the_history_selector_scopes_whose_durations_count(self, make_service, tmp_path):
        root = shop_suite(tmp_path, self.SUITE, name="cart.robot")
        service = pool_service(
            make_service, root, history_selector={"labels": {"suite": "nightly"}}
        )
        adhoc = execute(service, "Cart", labels={"suite": "adhoc"})
        assert "cold start" in adhoc.latest_dispatch.plan
        # History exists now — but not for runs labelled suite=nightly.
        nightly = execute(service, "Cart", labels={"suite": "nightly"})
        assert "cold start" in nightly.latest_dispatch.plan
        again = execute(service, "Cart", labels={"suite": "nightly"})
        assert "duration-balanced chunking" in again.latest_dispatch.plan
