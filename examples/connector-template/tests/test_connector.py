"""The connector's tests — written with runcomposer's conformance kit.

Three layers, each one call into ``runcomposer.testing``:

1. ``check_source`` / ``check_runner`` / ``check_observer`` run the contract's
   invariants against the plugins themselves (for a runner with the ``plan``
   capability, ``check_runner`` also holds its preview to the dispatch).
2. ``FakeDispatchContext`` unit-tests the runner's own behaviour (refusals,
   failures, the per-run parameter, the plan preview, the live stream,
   cancellation) without a deployment.
3. ``ConnectorHarness`` runs the plugins inside a real runcomposer, loaded by
   their entry-point ids — the way a config file names them — and, with
   ``harness.start``, meets a dispatch while it is still executing.

The target is the example shop in ``example/shop_server.py``, started on a
free port: every verdict below is the answer to a real HTTP request.
"""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

import pytest
from runcomposer.sdk import ConfigError, Item
from runcomposer.testing import (
    ConnectorHarness,
    FakeDispatchContext,
    check_observer,
    check_runner,
    check_source,
)

from shop_connector.checks_source import HttpChecksSource
from shop_connector.http_runner import HttpCheckRunner
from shop_connector.summary_observer import RunSummaryObserver

EXAMPLE = Path(__file__).resolve().parent.parent / "example"
CHECKS = EXAMPLE / "checks.yaml"


def _load_shop_server():
    spec = importlib.util.spec_from_file_location("example_shop_server", EXAMPLE / "shop_server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shop_server = _load_shop_server()


@pytest.fixture()
def shop():
    """A running example shop: its base URL."""
    server, url = shop_server.start_in_background()
    yield url
    server.shutdown()
    server.server_close()


@pytest.fixture()
def held_shop():
    """A shop whose checkout page answers only once the test releases it."""
    release = threading.Event()
    server, url = shop_server.start_in_background(held=("checkout",), release=release)
    yield url, release
    release.set()
    server.shutdown()
    server.server_close()


@pytest.fixture()
def broken_shop():
    server, url = shop_server.start_in_background(broken=("checkout",))
    yield url
    server.shutdown()
    server.server_close()


def settings(url: str, **more: str) -> dict:
    return {"environments": {"local": url, **more}, "user_agent": "shop-connector-tests"}


def context(source: HttpChecksSource, ids: list[str] | None = None, **kwargs) -> FakeDispatchContext:
    items = [item for item in source.items() if ids is None or item.id in ids]
    return FakeDispatchContext(items=items, source=source, **kwargs)


# -- 1. the contract ----------------------------------------------------------------


def test_the_source_meets_the_contract():
    items = check_source(HttpChecksSource(str(CHECKS)), rebuild=lambda: HttpChecksSource(str(CHECKS)))
    assert {item.id for item in items} >= {"shop.home", "shop.cart.add"}


def test_the_runner_meets_the_contract(shop):
    source = HttpChecksSource(str(CHECKS))
    # The runner declares `plan`, so this also checks that its preview is
    # read-only and names the shards the dispatch then declares.
    ctx = check_runner(HttpCheckRunner(connector_settings=settings(shop)), context(source), expect="complete")
    assert ctx.summary() == {"PASS": 5}
    assert ctx.declared == ("local",)
    assert [a.name for a in ctx.artifacts] == ["report.json"]
    # The whole live stream: every check was RUNNING before it had a verdict…
    for item_id in ctx.item_ids:
        assert [s.status for s in ctx.live_updates if s.item_id == item_id] == ["RUNNING", "PASS"]
    # …and nothing of it is left once the shard delivered.
    assert ctx.live_now == []


def test_the_observer_meets_the_contract(tmp_path):
    path = tmp_path / "summary.txt"
    check_observer(RunSummaryObserver(str(path)), threads=2)
    # it writes for the five kinds that end a dispatch, once per notification
    assert len(path.read_text(encoding="utf-8").splitlines()) == 5 * 3


def test_cancelling_stops_further_checks(shop):
    source = HttpChecksSource(str(CHECKS))
    gate = threading.Event()

    class SlowRunner(HttpCheckRunner):
        def _check(self, base, item: Item):
            gate.wait(10)  # hold every check until the test has cancelled
            return super()._check(base, item)

    runner = SlowRunner(workers=1, connector_settings=settings(shop))
    ctx = context(source)
    threading.Timer(0.2, gate.set).start()
    check_runner(runner, ctx, cancel_after_declare=True, expect="cancelled", timeout=30)
    assert ctx.deliveries == []  # a cancelled dispatch delivers nothing


# -- 2. the runner's own behaviour, against the fake ---------------------------------------


def test_a_failing_endpoint_is_a_fail_with_the_reason(broken_shop):
    source = HttpChecksSource(str(CHECKS))
    ctx = context(source)
    assert ctx.run(HttpCheckRunner(connector_settings=settings(broken_shop))) == "complete"
    by_id = {v.item_id: v for v in ctx.verdicts}
    assert by_id["shop.checkout.page"].status == "FAIL"
    assert by_id["shop.checkout.page"].message == "expected HTTP 200, got 500"
    assert ctx.summary() == {"PASS": 4, "FAIL": 1}


def test_an_unreachable_shop_refuses_before_the_hand_off():
    source = HttpChecksSource(str(CHECKS))
    ctx = context(source)
    runner = HttpCheckRunner(timeout_s=2, connector_settings=settings("http://127.0.0.1:9"))
    assert ctx.run(runner) == "refused"
    assert "not reachable" in (ctx.refusal or "") and ctx.declared is None


def test_a_changed_checks_file_is_drift(shop, tmp_path):
    original = HttpChecksSource(str(CHECKS))
    planned = context(original).spec
    edited = tmp_path / "checks.yaml"
    text = CHECKS.read_text(encoding="utf-8").replace("[Smoke, Catalog]", "[Smoke]")
    edited.write_text(text, encoding="utf-8")
    ctx = FakeDispatchContext(spec=planned, source=HttpChecksSource(str(edited)))
    assert ctx.run(HttpCheckRunner(connector_settings=settings(shop))) == "refused"
    assert "drift" in (ctx.refusal or "")


def test_the_environment_is_a_per_run_parameter(shop):
    runner = HttpCheckRunner(connector_settings=settings("http://127.0.0.1:9", qa=shop))
    (parameter,) = runner.describe().parameters
    assert (parameter.name, parameter.kind, parameter.choices) == ("environment", "choice", ("local", "qa"))
    ctx = context(HttpChecksSource(str(CHECKS)), parameters={"environment": "qa"})
    assert ctx.run(runner) == "complete" and ctx.declared == ("qa",)


def test_the_plan_preview_is_what_the_dispatch_does_and_contacts_nothing():
    # Nothing listens on port 9: a preview that probed the shop would fail.
    runner = HttpCheckRunner(connector_settings=settings("http://127.0.0.1:9", qa="http://127.0.0.1:9"))
    source = HttpChecksSource(str(CHECKS))
    ctx = context(source, ["shop.home", "shop.cart.add"], parameters={"environment": "qa"})
    preview = ctx.preview(runner)
    assert preview.shards == {"qa": ["shop.home", "shop.cart.add"]}
    assert preview.text == "2 HTTP check(s) against http://127.0.0.1:9, 4 at a time"
    assert ctx.declared is None and ctx.deliveries == [] and ctx.logs == []


def test_a_plan_preview_refuses_what_the_dispatch_would(tmp_path):
    planned = context(HttpChecksSource(str(CHECKS))).spec
    edited = tmp_path / "checks.yaml"
    text = CHECKS.read_text(encoding="utf-8").replace("[Smoke, Catalog]", "[Smoke]")
    edited.write_text(text, encoding="utf-8")
    ctx = FakeDispatchContext(spec=planned, source=HttpChecksSource(str(edited)))
    assert ctx.preview(HttpCheckRunner(connector_settings=settings("http://127.0.0.1:9"))) is None
    assert "drift" in (ctx.refusal or "")


def test_settings_are_checked_when_the_runner_is_built():
    with pytest.raises(ConfigError, match="environments must map"):
        HttpCheckRunner(connector_settings={})
    with pytest.raises(ConfigError, match="'prod' is not one of"):
        HttpCheckRunner(environment="prod", connector_settings=settings("http://127.0.0.1:1"))


def test_health_asks_the_shop(shop):
    assert HttpCheckRunner(connector_settings=settings(shop)).health().status == "ok"
    down = HttpCheckRunner(timeout_s=2, connector_settings=settings("http://127.0.0.1:9")).health()
    assert down.status == "down" and "not reachable" in down.detail


# -- 3. inside a real runcomposer, plugins named by entry point ------------------------------


def test_end_to_end_in_a_real_deployment(shop, tmp_path):
    with ConnectorHarness(
        sources={"shop-checks": {"plugin": "http-checks", "path": str(CHECKS)}},
        runners={"shop": {"plugin": "http-checks"}},
        observers={"summary": {"plugin": "run-summary", "path": str(tmp_path / "summary.txt")}},
        connectors={"shop-checks": {"environments": {"local": "${env:SHOP_TEST_URL}"}}},
        env={"SHOP_TEST_URL": shop},
    ) as harness:
        preview = harness.plan("Smoke", runner="shop").preview  # `runcomposer plan`: nothing is stored
        assert preview.shards == {"local": ["shop.home", "shop.cart.add"]}
        result = harness.run("Smoke", runner="shop", title="Smoke against the local shop")
        assert (result.state, result.completion) == ("COMPLETE", "PASS")
        assert {v.item_id for v in result.verdicts} == {"shop.home", "shop.cart.add"}
        assert result.event_kinds[0] == "run.composed" and result.event_kinds[-1] == "run.completed"
        assert any("check(s) executed against" in line for line in result.logs())
        assert [a.name for a in result.artifacts] == ["report.json"]
        assert harness.flush_observers()
    summary = (tmp_path / "summary.txt").read_text(encoding="utf-8")
    assert f"{result.run_id}  run.completed" in summary and "PASS" in summary


def test_a_check_is_seen_running_and_the_run_cancelled_in_a_real_deployment(held_shop):
    url, release = held_shop
    with ConnectorHarness(
        sources={"shop-checks": {"plugin": "http-checks", "path": str(CHECKS)}},
        runners={"shop": {"plugin": "http-checks", "workers": 1}},
        connectors={"shop-checks": {"environments": {"local": url}}},
    ) as harness:
        handle = harness.start("Regression", runner="shop")  # returns at once
        mid = handle.wait_until(
            lambda run: any(s.status == "RUNNING" and s.item_id == "shop.checkout.page" for s in run.live),
            timeout=30,
        )
        assert mid.state == "RUNNING" and mid.verdicts == []
        handle.cancel()
        release.set()  # the request in flight finishes; no further check starts
        done = handle.wait(timeout=30)
        assert done.state == "CANCELLED" and done.verdicts == []  # a cancelled dispatch delivers nothing


def test_a_typo_in_the_runner_entry_fails_startup(tmp_path):
    with pytest.raises(ConfigError, match="unknown option"):
        ConnectorHarness(
            sources={"shop-checks": {"plugin": "http-checks", "path": str(CHECKS)}},
            runners={"shop": {"plugin": "http-checks", "wrokers": 2}},
            connectors={"shop-checks": {"environments": {"local": "http://127.0.0.1:1"}}},
        )
