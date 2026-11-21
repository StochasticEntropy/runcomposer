"""Values from the environment (DESIGN.md §8): what is a secret, what is
blanked where, and what an option receives.

- ``${env:NAME}`` is a secret; ``${env-public:NAME}`` is the same value, not
  a secret; a reference used as a path, or converted to a number or a
  boolean, is not a secret either.
- Secrets are blanked out of free text and out of the string values of
  runner-defined data — never out of identifiers: shard, group and runner
  names, item and dispatch ids, states, mapping keys.
- A secret too short to blank without mangling text is refused at startup.
- An option annotated ``int`` / ``float`` / ``bool`` receives the value
  converted; one that does not convert is refused, naming option and
  variable.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from runcomposer.app.config import REDACTED
from runcomposer.cli.main import main as cli_main
from runcomposer.sdk import (
    ConfigError,
    DispatchContext,
    Health,
    InvalidParameter,
    Parameter,
    PlanPreview,
    RunnerBase,
    RunnerInfo,
    Service,
    Verdict,
    load_config,
)

from .conftest import compose, write_config

API = "/api/v1"
HERE = "tests.test_env_values"


class RegionRunner(RunnerBase):
    """Declares one shard per stage, grouped by stage, and quotes its
    options in its health, its plan and its log lines."""

    PATH_OPTIONS = ("root",)

    def __init__(self, stage: str = "staging", ci_url: str = "https://ci.shop.example",
                 root: str = "", token: str = ""):
        self.stage, self.ci_url, self.root, self.token = stage, ci_url, root, token

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="regional", capabilities=("plan",))

    def health(self) -> Health:
        return Health(
            status="ok",
            detail=f"reachable at {self.ci_url}, suites in {self.root}",
            data={"job_url": f"{self.ci_url}/job/shop", "stages": [self.stage, "preview"],
                  self.stage: {"root": self.root}, "auth": f"Bearer {self.token}"},
        )

    def shards(self, item_ids: tuple[str, ...]) -> dict[str, list[str]]:
        half = max(1, len(item_ids) // 2)
        return {self.stage: list(item_ids[:half]), "preview": list(item_ids[half:])}

    def plan(self, ctx: Any) -> PlanPreview:
        shards = self.shards(ctx.item_ids)
        return PlanPreview(
            text=f"{self.stage} and preview against {self.ci_url}",
            shards=shards,
            groups={name: name for name in shards},
            data={"job_url": f"{self.ci_url}/job/shop", self.stage: len(shards[self.stage])},
        )

    def dispatch(self, ctx: DispatchContext) -> None:
        shards = self.shards(ctx.item_ids)
        ctx.log(f"suites from {self.root}, results at {self.ci_url}")
        ctx.declare(shards, plan=f"{self.stage} and preview", groups={n: n for n in shards})
        for name, items in shards.items():
            ctx.deliver(name, verdicts=[Verdict(item, "PASS", 5) for item in items])


def regional(make_service, **options: Any) -> Service:
    service: Service = make_service(runners={"staging": {"module": f"{HERE}:RegionRunner", **options}})
    return service


def run_events(service: Service, run_id: str) -> list[tuple[str, dict[str, Any]]]:
    return [(event.kind, dict(event.data)) for event in service.store.events(run_id)]


class TestIdentifiersAreNeverRewritten:
    def test_a_shard_a_group_and_a_runner_named_like_an_environment_value(
        self, make_service, monkeypatch
    ):
        """The secret ``staging`` (it came through ``${env:…}``) is also the
        name of the runner, of a shard and of its group: those are
        identifiers, and stay what they are wherever they are recorded."""
        monkeypatch.setenv("SHOP_STAGE", "staging")
        service = regional(make_service, stage="${env:SHOP_STAGE}")
        run_id = compose(service, "Checkout")
        service.dispatch(run_id, "staging", foreground=True)
        events = run_events(service, run_id)
        declared = next(data for kind, data in events if kind == "run.running")
        assert declared["shards"] == ["staging", "preview"]
        assert declared["groups"] == {"staging": "staging", "preview": "preview"}
        assert declared["mode"] == "staging"
        assert declared["plan"] == f"{REDACTED} and preview", "free text is still blanked"
        delivered = [data["shard"] for kind, data in events if kind == "delivery.recorded"]
        assert delivered == ["staging", "preview"]
        completed = next(data for kind, data in events if kind == "run.completed")
        assert completed["mode"] == "staging" and completed["state"] == "COMPLETE"

        health = service.runner_health("staging", refresh=True)
        assert health.data["stages"] == [REDACTED, "preview"], "a runner's data value is blanked"
        assert "staging" in health.data, "a key of runner data is never rewritten"
        preview = service.plan("staging", run_id=run_id).preview
        assert preview is not None
        assert list(preview.shards) == ["staging", "preview"]
        assert preview.groups == {"staging": "staging", "preview": "preview"}
        assert "staging" in preview.data

    def test_the_item_ids_and_states_in_events_are_untouched(self, make_service, monkeypatch):
        monkeypatch.setenv("SHOP_STAGE", "Shop.Checkout")
        service = regional(make_service, stage="${env:SHOP_STAGE}")
        run_id = compose(service, "Checkout")
        service.dispatch(run_id, "staging", foreground=True)
        detail = service.require_run(run_id)
        assert detail.state == "COMPLETE"
        planned = service.ledger.planned_ids(run_id)
        assert all(item.startswith("Shop.Checkout.") for item in planned)
        declared = next(data for kind, data in run_events(service, run_id) if kind == "run.running")
        assert declared["shards"] == ["Shop.Checkout", "preview"]


class TestWhatIsNoSecret:
    def test_a_public_reference_is_substituted_and_never_blanked(self, make_service, monkeypatch):
        """The server's base URL is no secret: given as ``${env-public:…}``,
        the health data built from it reads as it is."""
        monkeypatch.setenv("SHOP_CI_URL", "https://ci.shop.example")
        service = regional(make_service, ci_url="${env-public:SHOP_CI_URL}")
        assert service.runner("staging").ci_url == "https://ci.shop.example"
        health = service.runner_health("staging", refresh=True)
        assert health.data["job_url"] == "https://ci.shop.example/job/shop"
        assert health.detail.startswith("reachable at https://ci.shop.example,")

    def test_the_same_value_as_a_secret_is_blanked(self, make_service, monkeypatch):
        monkeypatch.setenv("SHOP_CI_URL", "https://ci.shop.example")
        service = regional(make_service, ci_url="${env:SHOP_CI_URL}")
        health = service.runner_health("staging", refresh=True)
        assert health.data["job_url"] == f"{REDACTED}/job/shop"

    def test_a_public_reference_takes_a_default(self, make_service, monkeypatch):
        monkeypatch.delenv("SHOP_CI_URL", raising=False)
        service = regional(make_service, ci_url="${env-public:SHOP_CI_URL:-https://ci.shop.example}")
        assert service.runner("staging").ci_url == "https://ci.shop.example"

    def test_a_missing_public_variable_is_named_in_its_own_spelling(self, make_service, monkeypatch):
        monkeypatch.delenv("SHOP_CI_URL", raising=False)
        service = regional(make_service, ci_url="${env-public:SHOP_CI_URL}")
        with pytest.raises(ConfigError, match=r"'SHOP_CI_URL' is not set.*\$\{env-public:SHOP_CI_URL\}"):
            service.runner("staging")

    def test_a_path_from_the_environment_is_not_blanked(self, make_service, monkeypatch, tmp_path):
        monkeypatch.setenv("SHOP_CHECKOUT", str(tmp_path / "checkout"))
        service = regional(make_service, root="${env:SHOP_CHECKOUT}/suites")
        health = service.runner_health("staging", refresh=True)
        assert health.detail.endswith(f"suites in {tmp_path / 'checkout'}/suites")
        assert str(tmp_path / "checkout") not in service.config.secret_values()

    def test_a_short_public_value_is_fine_and_a_short_secret_is_refused(self, make_service, monkeypatch):
        monkeypatch.setenv("SHOP_REGION", "eu")
        service = regional(make_service, stage="${env-public:SHOP_REGION}")
        run_id = compose(service, "Checkout")
        service.dispatch(run_id, "staging", foreground=True)
        assert service.require_run(run_id).state == "COMPLETE"
        refused = regional(make_service, stage="${env:SHOP_REGION}")
        with pytest.raises(ConfigError) as caught:
            refused.validate()
        message = str(caught.value)
        assert message.startswith("runners.staging.stage: the value of environment variable 'SHOP_REGION'")
        assert "2 character(s)" in message and "${env-public:SHOP_REGION}" in message


class TestNumbersAndBooleansFromTheEnvironment:
    def test_the_reference_runners_numeric_and_boolean_options(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHOP_WORKERS", "4")
        monkeypatch.setenv("SHOP_CHUNK_SHARDS", "false")
        monkeypatch.setenv("SHOP_HOOK_TIMEOUT", "90")
        monkeypatch.setenv("SHOP_CI_TIMEOUT", "120.5")
        monkeypatch.setenv("SHOP_CI_RETRIES", "5")
        monkeypatch.setenv("SHOP_CI_MAX_BYTES", "5000")
        monkeypatch.setenv("SHOP_CI_VERIFY", "no")
        (tmp_path / "suites").mkdir()
        config = load_config(str(write_config(tmp_path, runners={
            "robot-pool": {"suite_root": "suites", "max_workers": "${env:SHOP_WORKERS}",
                           "chunk_shards": "${env:SHOP_CHUNK_SHARDS}",
                           "hook_timeout_s": "${env:SHOP_HOOK_TIMEOUT}"},
            "jenkins": {"base_url": "https://ci.shop.example", "job": "shop/nightly",
                        "timeout_s": "${env:SHOP_CI_TIMEOUT}", "retries": "${env:SHOP_CI_RETRIES}",
                        "max_parameter_bytes": "${env:SHOP_CI_MAX_BYTES}",
                        "verify_tls": "${env:SHOP_CI_VERIFY}"},
        })))
        with Service(config) as service:
            pool = service.runner_options("robot-pool")
            assert (pool["max_workers"], pool["chunk_shards"], pool["hook_timeout_s"]) == (4, False, 90.0)
            ci = service.runner_options("jenkins")
            assert ci["timeout_s"] == 120.5 and ci["retries"] == 5
            assert ci["max_parameter_bytes"] == 5000 and ci["verify_tls"] is False
            assert service.runner("robot-pool").max_workers == 4
            assert service.runner("jenkins").timeout_s == 120.5
            # A number is no secret: "4" would otherwise be refused as too short.
            assert "4" not in service.config.secret_values()

    def test_a_value_that_does_not_convert_names_the_option_and_the_variable(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("SHOP_WORKERS", "four")
        config = load_config(str(write_config(tmp_path, runners={
            "robot-pool": {"suite_root": "suites", "max_workers": "${env:SHOP_WORKERS}"}})))
        with Service(config) as service, pytest.raises(ConfigError) as caught:
            service.validate()
        message = str(caught.value)
        assert message.startswith("runners.robot-pool.max_workers: the option takes an integer")
        assert "'SHOP_WORKERS'" in message and "four" not in message

    def test_a_boolean_that_does_not_convert(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHOP_CI_VERIFY", "maybe")
        config = load_config(str(write_config(tmp_path, runners={
            "jenkins": {"base_url": "https://ci.shop.example", "job": "shop/nightly",
                        "verify_tls": "${env:SHOP_CI_VERIFY}"}})))
        with Service(config) as service, pytest.raises(ConfigError, match="verify_tls: the option takes true or false"):
            service.validate()

    def test_an_optional_number_left_empty_is_null(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHOP_CI_MAX_BYTES", "")
        config = load_config(str(write_config(tmp_path, runners={
            "jenkins": {"base_url": "https://ci.shop.example", "job": "shop/nightly",
                        "max_parameter_bytes": "${env-public:SHOP_CI_MAX_BYTES}"}})))
        with Service(config) as service:
            assert service.runner_options("jenkins")["max_parameter_bytes"] is None

    def test_an_option_without_a_numeric_annotation_stays_text(self, make_service, monkeypatch):
        monkeypatch.setenv("SHOP_STAGE", "1234")
        service = regional(make_service, stage="${env:SHOP_STAGE}")
        assert service.runner("staging").stage == "1234"
        assert "1234" in service.config.secret_values()


# -- one secret, every surface ----------------------------------------------------------------

SECRET = "k-shop-gateway-7f3e9a"


class LeakyRunner(RunnerBase):
    """Quotes its configured token everywhere a runner can put text."""

    def __init__(self, token: str):
        self.token = token

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="leaky",
            capabilities=("plan",),
            parameters=(Parameter("mode", kind="choice", default="ok",
                                  choices=("ok", "refuse", "raise", "invalid")),),
        )

    def health(self) -> Health:
        return Health(status="degraded", detail=f"probe with {self.token} was slow",
                      data={"auth": f"Bearer {self.token}", "probes": [self.token]})

    def _misbehave(self, ctx: Any) -> None:
        mode = ctx.parameters["mode"]
        if mode == "refuse":
            ctx.refuse(f"the gateway refused {self.token}")
        if mode == "invalid":
            raise InvalidParameter("mode", f"not with key {self.token}")

    def plan(self, ctx: Any) -> PlanPreview:
        self._misbehave(ctx)
        return PlanPreview(text=f"one shard, key {self.token}", shards={"1": list(ctx.item_ids)},
                           notes=(f"checked with {self.token}",), data={"auth": self.token})

    def dispatch(self, ctx: DispatchContext) -> None:
        self._misbehave(ctx)
        ctx.log(f"authorizing with {self.token}")
        ctx.declare(["1"], plan=f"one shard, key {self.token}")
        if ctx.parameters["mode"] == "raise":
            raise RuntimeError(f"the gateway refused key {self.token}")
        ctx.deliver("1", verdicts=[Verdict(item, "PASS") for item in ctx.item_ids])


@pytest.fixture(scope="module")
def surfaces(tmp_path_factory) -> dict[str, str]:
    """Everything a deployment answers, records or logs, gathered over a run
    that completes, one that fails after the hand-off, a refusal and a
    rejected parameter — as text, keyed by surface."""
    from fastapi.testclient import TestClient

    from runcomposer.http import create_app

    tmp_path: Path = tmp_path_factory.mktemp("leaky")
    collected: dict[str, str] = {}
    records = io.StringIO()
    handler = logging.StreamHandler(records)
    handler.setLevel(logging.DEBUG)
    logger = logging.getLogger("runcomposer")
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("SHOP_GATEWAY_KEY", SECRET)
        path = write_config(tmp_path, runners={
            "leaky": {"module": f"{HERE}:LeakyRunner", "token": "${env:SHOP_GATEWAY_KEY}"}})
        app = create_app(load_config(str(path)))
        try:
            with TestClient(app) as client:
                service = app.state.service
                answers: dict[str, Any] = {}
                for mode in ("ok", "raise"):
                    started = client.post(f"{API}/runs", json={
                        "selection": {"tag_filter": "Auth-SSO"},
                        "dispatch": {"runner": "leaky", "parameters": {"mode": mode}}})
                    assert started.status_code == 201, started.text
                    answers[mode] = started.json()["run"]["id"]
                service.dispatcher.join(30)
                for mode in ("refuse", "invalid"):
                    answer = client.post(f"{API}/runs", json={
                        "selection": {"tag_filter": "Auth-SSO"}, "keep_run": True,
                        "dispatch": {"runner": "leaky", "parameters": {"mode": mode}}})
                    assert answer.status_code == {"refuse": 409, "invalid": 400}[mode], answer.text
                    collected[f"error document ({mode})"] = answer.text
                for mode, run_id in answers.items():
                    collected[f"run detail ({mode})"] = client.get(f"{API}/runs/{run_id}").text
                    collected[f"events ({mode})"] = client.get(f"{API}/runs/{run_id}/events").text
                    collected[f"stored spec ({mode})"] = json.dumps(service.require_spec(run_id))
                collected["run list"] = client.get(f"{API}/runs").text
                collected["health"] = client.get(f"{API}/runners", params={"refresh": "true"}).text
                plan = {"runner": "leaky", "selection": {"tag_filter": "Auth-SSO"}}
                collected["plan answer"] = client.post(f"{API}/plan", json=plan).text
                collected["plan answer (refused)"] = client.post(
                    f"{API}/plan", json={**plan, "parameters": {"mode": "refuse"}}).text
                collected["plan answer (invalid)"] = client.post(
                    f"{API}/plan", json={**plan, "parameters": {"mode": "invalid"}}).text
                service.dispatcher.join(30)
                for name, argv in {
                    "CLI show": ["show", answers["raise"]],
                    "CLI plan": ["plan", "Auth-SSO", "--runner", "leaky"],
                    "CLI plan (invalid)": ["plan", "Auth-SSO", "--runner", "leaky", "--param", "mode=invalid"],
                    "CLI run (refused)": ["run", "Auth-SSO", "--runner", "leaky", "--param", "mode=refuse"],
                    "CLI doctor": ["doctor"],
                }.items():
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        cli_main([*argv, "--config", str(path)])
                    collected[name] = out.getvalue() + err.getvalue()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
    collected["log lines"] = records.getvalue()
    return collected


SURFACES = (
    "run detail (ok)", "run detail (raise)", "run list", "events (ok)", "events (raise)",
    "stored spec (ok)", "stored spec (raise)", "health", "plan answer", "plan answer (refused)",
    "plan answer (invalid)", "error document (refuse)", "error document (invalid)", "log lines",
    "CLI show", "CLI plan", "CLI plan (invalid)", "CLI run (refused)", "CLI doctor",
)
#: Where the runner's quote is visible — blanked, not dropped.
QUOTED = set(SURFACES) - {"stored spec (ok)", "stored spec (raise)", "run list"}


@pytest.mark.parametrize("surface", SURFACES)
def test_a_secret_from_the_environment_reaches_no_surface(surfaces, surface):
    text = surfaces[surface]
    assert text, f"{surface}: nothing was collected"
    assert SECRET not in text
    if surface in QUOTED:
        assert REDACTED in text or json.dumps(REDACTED)[1:-1] in text, (
            f"{surface}: the runner quoted the secret here — it should be blanked: {text[:400]}"
        )
