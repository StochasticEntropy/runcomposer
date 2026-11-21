"""`robot-pool` (DESIGN.md §6.2a), with real Robot Framework:

- ``robot_options: {name: …}`` renames the top-level suite: the single-root
  check, the exact selection and ``doctor`` read the ids under that name —
  and a catalog that really does not match is still refused;
- every ``health_checks`` callable answers within
  ``health_check_timeout_s``, or is a failed row saying so;
- the preview's note on items without duration history reads right for
  one item.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from runcomposer.sdk import plan_chunks

from .conftest import compose, needs_robot, write_config
from .test_robot_pool import FIVE_TESTS, RUNNER, execute, pool_service, verdict_map, write_suite

pytestmark = needs_robot

HERE = "tests.test_robot_pool_suite_name_and_health"

STOREFRONT = [
    "Storefront.Cart.Cart Holds One Item",
    "Storefront.Cart.Cart Holds Two Items",
    "Storefront.Cart.Cart Rejects A Negative Quantity",
    "Storefront.Cart.Cart Survives A Reload",
    "Storefront.Cart.Cart Is Emptied On Checkout",
]


def web_root(tmp_path: Path) -> Path:
    """A suite directory named ``web`` — Robot calls its suite ``Web``."""
    root = tmp_path / "web"
    write_suite(root, "cart.robot", FIVE_TESTS)
    return root


def manifest(tmp_path: Path, ids: list[str]) -> dict:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"items": [{"id": item, "tags": ["Cart"]} for item in ids]}), encoding="utf-8")
    return {"manifest": {"path": str(path)}}


class TestARenamedTopSuite:
    def test_the_configured_name_is_honoured_by_the_check_the_selection_and_the_reruns(
        self, make_service, tmp_path
    ):
        service = pool_service(
            make_service, web_root(tmp_path), source=manifest(tmp_path, STOREFRONT), max_workers=1,
            robot_options={"name": "Storefront"}, rerun_failed=1,
        )
        planned = service.plan(RUNNER, selection={"tag_filter": "Cart"})
        assert planned.refusal is None, planned.refusal
        run = execute(service, "Cart")
        assert run.state == "COMPLETE" and run.completion == "FAIL"
        statuses = verdict_map(service, run)
        assert {item for (_shard, item, _attempt) in statuses} == set(STOREFRONT)
        assert statuses[("default", STOREFRONT[2], 2)] == "FAIL"  # re-executed under the name
        assert sorted(s for (_sh, item, attempt), s in statuses.items() if attempt == 1).count("PASS") == 4

    def test_a_catalog_that_really_does_not_match_is_still_refused(self, make_service, tmp_path):
        service = pool_service(
            make_service, web_root(tmp_path), robot_options={"name": "Storefront"},
            source=manifest(tmp_path, [STOREFRONT[0], "Web.Cart.Cart Holds One Item"]),
        )
        refusal = service.plan(RUNNER, selection={"tag_filter": "Cart"}).refusal
        assert refusal is not None
        assert "'Web.Cart.Cart Holds One Item' is not under the suite root" in refusal
        assert "every test's id begins with 'Storefront.'" in refusal
        assert STOREFRONT[0] not in refusal

    def test_without_the_name_the_ids_are_the_directory_s(self, make_service, tmp_path):
        service = pool_service(make_service, web_root(tmp_path), source=manifest(tmp_path, STOREFRONT))
        refusal = service.plan(RUNNER, selection={"tag_filter": "Cart"}).refusal
        assert "every test's id begins with 'Web.'" in refusal

    def test_doctor_checks_the_catalog_under_the_configured_name(self, tmp_path, capsys):
        from runcomposer.cli.main import main

        root = web_root(tmp_path)
        config = write_config(
            tmp_path, sources=manifest(tmp_path, STOREFRONT),
            runners={RUNNER: {"suite_root": str(root), "robot_options": {"name": "Storefront"}}},
        )
        code = main(["doctor", "-v", "--config", str(config)])
        out = capsys.readouterr().out
        assert "[ok  ] catalog items under the suite roots — all 5 catalog item(s)" in out, out
        assert code == 0, out


# -- health checks within a time limit ---------------------------------------------------

#: Released by the test once it has its answer: the check blocks until then.
RELEASE = threading.Event()


def blocks_until_released(info: dict) -> dict:
    RELEASE.wait(timeout=60)
    return {"check": "payment gateway", "ok": True, "detail": "answered late"}


def answers_at_once(info: dict) -> dict:
    return {"check": "eu storefront", "ok": True, "detail": "ok"}


class TestHealthCheckTimeout:
    def test_a_check_that_does_not_answer_in_time_is_a_failed_row_and_health_returns(
        self, make_service, tmp_path
    ):
        RELEASE.clear()
        service = pool_service(
            make_service, web_root(tmp_path), health_check_timeout_s=0.3,
            health_checks=[f"{HERE}:blocks_until_released", f"{HERE}:answers_at_once"],
        )
        started = time.monotonic()
        try:
            health = service.runner_health(RUNNER, refresh=True)
            took = time.monotonic() - started
        finally:
            RELEASE.set()
        assert took < 5.0, took  # bounded by the time limit, not by the check
        assert health.status == "degraded"
        assert health.detail == f"health check(s) failed: {HERE}:blocks_until_released"
        rows = [(c["check"], c["ok"], c["detail"]) for c in health.data["checks"]][-2:]
        assert rows == [
            (f"{HERE}:blocks_until_released", False, "did not answer within 0.3s (health_check_timeout_s)"),
            ("eu storefront", True, "ok"),
        ]

    def test_the_default_limit_is_five_seconds_and_a_wrong_one_refuses_startup(self, make_service, tmp_path):
        import pytest

        from runcomposer.plugins.robot_pool import RobotPoolRunner
        from runcomposer.sdk import ConfigError

        assert RobotPoolRunner(suite_root=str(tmp_path)).health_check_timeout_s == 5.0
        for wrong in (0, -1, True):
            with pytest.raises(ConfigError, match="health_check_timeout_s must be a positive number"):
                RobotPoolRunner(suite_root=str(tmp_path), health_check_timeout_s=wrong)


# -- the preview's wording for one item ------------------------------------------------------


class TestOneItemWithoutHistory:
    def test_the_plan_and_the_preview_note_read_right_for_one(self, make_service, tmp_path):
        chunks = plan_chunks(["a", "b", "c"], 2, {"a": 1000.0, "b": 3000.0})
        assert "; the other one weighs the mean of the known durations, 2000ms" in chunks.describe()
        assert "the other 1 weigh" not in chunks.describe()

        root = tmp_path / "Shop"
        write_suite(root, "cart.robot", FIVE_TESTS)
        service = pool_service(make_service, root)
        run_id = compose(service, "Cart", runner_id=RUNNER)
        service.dispatch(run_id, RUNNER, foreground=True)
        # Four of the five become known; the fifth is new to the history.
        source = root / "cart.robot"
        source.write_text(
            source.read_text(encoding="utf-8")
            + "\nCart Keeps A Coupon\n    [Tags]    Cart\n    Log    ok\n", encoding="utf-8",
        )
        fresh = pool_service(make_service, root)
        notes = fresh.plan(RUNNER, selection={"tag_filter": "Cart"}).preview.notes
        (note,) = [n for n in notes if n.startswith("duration history for")]
        assert note.startswith("duration history for 5 of 6 item(s); the other one is weighed with the mean")
        assert note.endswith("— the estimates per partition assume it takes that long")
