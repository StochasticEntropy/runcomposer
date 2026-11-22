"""Per-run parameters a runner refuses, and the keys of a mapping parameter
(DESIGN.md §6.2).

- ``key_pattern`` constrains every key of a ``mapping`` parameter as
  ``pattern`` constrains every value: declared the same way, checked when
  the run is requested, served with the declaration, the error naming the
  parameter and the key.
- A runner answers "this value cannot be carried out" with
  ``InvalidParameter``: raised from ``plan``, or from ``dispatch`` before
  ``declare``, it is the request's ``400 invalid-parameter`` and no dispatch
  remains; raised after the hand-off it can no longer be an answer, and the
  dispatch fails with its message.
"""

from __future__ import annotations

from typing import Any

import pytest

from runcomposer.sdk import (
    ConfigError,
    DispatchContext,
    InvalidParameter,
    Parameter,
    PlanPreview,
    RunnerBase,
    RunnerInfo,
    Verdict,
    parameters_from_config,
    validate_parameters,
)

from .conftest import compose, wait_until

API = "/api/v1"
HERE = "tests.test_parameter_refusals"


class TestKeyPattern:
    VARIABLES = Parameter("variables", kind="mapping", key_pattern=r"[A-Z][A-Z0-9_]*",
                          pattern=r"[^\n]*")

    def test_every_key_must_match(self):
        accepted = validate_parameters([self.VARIABLES], {"variables": {"LOCALE": "de", "RETRIES": "3"}})
        assert accepted == {"variables": {"LOCALE": "de", "RETRIES": "3"}}
        with pytest.raises(InvalidParameter) as caught:
            validate_parameters([self.VARIABLES], {"variables": {"LOCALE": "de", "locale": "fr"}})
        assert caught.value.parameter == "variables"
        assert str(caught.value) == (
            "parameter 'variables': key 'locale' does not match the key pattern '[A-Z][A-Z0-9_]*'"
        )

    def test_values_are_still_checked_by_pattern(self):
        with pytest.raises(InvalidParameter, match=r"parameter 'variables'\['LOCALE'\]"):
            validate_parameters([self.VARIABLES], {"variables": {"LOCALE": "de\nfr"}})

    def test_it_applies_to_a_mapping_only_and_must_compile(self):
        with pytest.raises(ValueError, match="'key_pattern' applies to the kinds"):
            Parameter("coupon", kind="string", key_pattern="[A-Z]+")
        with pytest.raises(ValueError, match=r"key_pattern '\[' is not a regular expression"):
            Parameter("variables", kind="mapping", key_pattern="[")

    def test_it_is_declared_in_configuration_the_same_way(self):
        (declared,) = parameters_from_config(
            [{"name": "variables", "kind": "mapping", "key_pattern": "[A-Z_]+"}], "runners.x.run_parameters"
        )
        assert declared.key_pattern == "[A-Z_]+"
        with pytest.raises(ConfigError, match=r"runners\.x\.run_parameters\[0\]: .*key_pattern"):
            parameters_from_config([{"name": "coupon", "key_pattern": "[A-Z_]+"}], "runners.x.run_parameters")

    def test_it_is_served_with_the_declaration_and_refused_over_http(self, make_client):
        client, _service = make_client(runners={"careful": {"module": f"{HERE}:CarefulRunner"}})
        (runner,) = client.get(f"{API}/runners").json()
        served = {parameter["name"]: parameter for parameter in runner["parameters"]}
        assert served["variables"]["key_pattern"] == r"[A-Z][A-Z0-9_]*"
        assert served["region"]["key_pattern"] is None
        answer = client.post(f"{API}/runs", json={
            "selection": {"tag_filter": "Checkout"},
            "dispatch": {"runner": "careful", "parameters": {"variables": {"locale": "de"}}}})
        assert answer.status_code == 400
        assert answer.json()["code"] == "invalid-parameter" and answer.json()["parameter"] == "variables"
        assert "key 'locale'" in answer.json()["detail"]


class TestTheMessageShowsThePatternAsWritten:
    """A pattern is shown as the declaration writes it — one backslash where
    it has one — not as a Python repr, which doubles every backslash."""

    BUILD = Parameter("build", pattern=r"\d{4}\.\d{1,2}")
    HEADERS = Parameter("headers", kind="mapping", key_pattern=r"X-[A-Za-z]+\d?")

    def test_a_value_pattern(self):
        with pytest.raises(InvalidParameter) as caught:
            validate_parameters([self.BUILD], {"build": "2026-12"})
        assert str(caught.value) == r"parameter 'build': '2026-12' does not match the pattern '\d{4}\.\d{1,2}'"

    def test_a_key_pattern(self):
        with pytest.raises(InvalidParameter) as caught:
            validate_parameters([self.HEADERS], {"headers": {"Accept": "json"}})
        assert str(caught.value) == r"parameter 'headers': key 'Accept' does not match the key pattern 'X-[A-Za-z]+\d?'"

    def test_over_http_the_detail_is_the_same_sentence(self, make_client):
        client, _service = make_client(runners={"versioned": {"module": f"{HERE}:VersionedRunner"}})
        answer = client.post(f"{API}/runs", json={
            "selection": {"tag_filter": "Checkout"},
            "dispatch": {"runner": "versioned", "parameters": {"build": "2026-12"}}})
        assert answer.status_code == 400
        assert answer.json()["detail"] == r"parameter 'build': '2026-12' does not match the pattern '\d{4}\.\d{1,2}'"


class VersionedRunner(RunnerBase):
    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="versioned", parameters=(Parameter("build", pattern=r"\d{4}\.\d{1,2}"),))

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        ctx.deliver("1", verdicts=[Verdict(item, "PASS") for item in ctx.item_ids])


class CarefulRunner(RunnerBase):
    """Knows two regions; ``ap`` passes the declaration (it is a string) but
    has no build agent. ``when`` says where the refusal is raised."""

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="careful",
            capabilities=("plan",),
            parameters=(
                Parameter("region", default="eu"),
                Parameter("when", kind="choice", choices=("before", "after"), default="before"),
                Parameter("variables", kind="mapping", key_pattern=r"[A-Z][A-Z0-9_]*"),
            ),
        )

    def check(self, ctx: Any) -> None:
        if ctx.parameters["region"] not in ("eu", "us"):
            raise InvalidParameter("region", f"no build agent serves {ctx.parameters['region']!r}")

    def plan(self, ctx: Any) -> PlanPreview:
        self.check(ctx)
        return PlanPreview(text="one shard", shards={"1": list(ctx.item_ids)})

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.workdir.joinpath("prepared.txt").write_text("ready", encoding="utf-8")
        ctx.private_dir.joinpath("hook.env").write_text("SHOP_LOCALE=de", encoding="utf-8")
        if ctx.parameters["when"] == "before":
            self.check(ctx)
        ctx.declare(["1"])
        self.check(ctx)
        ctx.deliver("1", verdicts=[Verdict(item, "PASS") for item in ctx.item_ids])


@pytest.fixture()
def careful(make_client):
    return make_client(runners={"careful": {"module": f"{HERE}:CarefulRunner"}})


def refused_as_invalid(answer: Any) -> dict[str, Any]:
    assert answer.status_code == 400, answer.text
    body: dict[str, Any] = answer.json()
    assert body["code"] == "invalid-parameter" and body["parameter"] == "region"
    assert body["detail"] == "parameter 'region': no build agent serves 'ap'"
    return body


class TestARunnerRefusesAParameter:
    def test_the_constructor_takes_the_name_and_what_is_wrong(self):
        error = InvalidParameter("region", "no build agent serves 'ap'")
        assert (error.parameter, str(error)) == ("region", "parameter 'region': no build agent serves 'ap'")
        compatible = InvalidParameter("parameter 'region' is required", parameter="region")
        assert (compatible.parameter, str(compatible)) == ("region", "parameter 'region' is required")

    def test_from_plan_it_is_a_400(self, careful):
        client, _service = careful
        refused_as_invalid(client.post(f"{API}/plan", json={
            "runner": "careful", "selection": {"tag_filter": "Checkout"}, "parameters": {"region": "ap"}}))
        ok = client.post(f"{API}/plan", json={
            "runner": "careful", "selection": {"tag_filter": "Checkout"}, "parameters": {"region": "us"}})
        assert ok.status_code == 200 and ok.json()["refused"] is False

    def test_from_plan_of_a_stored_run_it_is_a_400(self, careful):
        client, service = careful
        run_id = compose(service, "Checkout")
        refused_as_invalid(client.post(f"{API}/runs/{run_id}/plan", json={
            "runner": "careful", "parameters": {"region": "ap"}}))

    def test_from_dispatch_before_the_hand_off_it_is_a_400_and_no_dispatch_remains(self, careful):
        client, service = careful
        body = refused_as_invalid(client.post(f"{API}/runs", json={
            "selection": {"tag_filter": "Checkout"}, "keep_run": True,
            "dispatch": {"runner": "careful", "parameters": {"region": "ap"}}}))
        assert body["run_kept"] is True
        run = service.require_run(body["run_id"])
        assert run.state == "COMPOSED" and run.dispatches == ()
        refusal = [e for e in service.store.events(run.id) if e.kind == "dispatch.refused"]
        assert refusal[-1].data["reason"] == "parameter 'region': no build agent serves 'ap'"
        assert refusal[-1].data["crashed"] is False

        refused_as_invalid(client.post(f"{API}/runs/{run.id}/dispatch", json={
            "runner": "careful", "parameters": {"region": "ap"}}))
        assert service.require_run(run.id).dispatches == ()

    def test_a_run_composed_for_the_request_is_removed_unless_kept(self, careful):
        client, service = careful
        body = refused_as_invalid(client.post(f"{API}/runs", json={
            "selection": {"tag_filter": "Checkout"},
            "dispatch": {"runner": "careful", "parameters": {"region": "ap"}}}))
        assert body["run_kept"] is False and body["run_id"] is None
        assert service.list_runs().total == 0

    def test_from_a_rerun_before_the_hand_off_it_is_a_400(self, careful):
        client, service = careful
        run_id = compose(service, "Checkout")
        service.dispatch(run_id, "careful", {"region": "eu"}, foreground=True)
        assert service.require_run(run_id).state == "COMPLETE"
        refused_as_invalid(client.post(f"{API}/runs/{run_id}/rerun", json={
            "runner": "careful", "parameters": {"region": "ap"}}))
        assert len(service.require_run(run_id).dispatches) == 1

    def test_from_dispatch_after_the_hand_off_the_dispatch_fails(self, careful):
        client, _service = careful
        started = client.post(f"{API}/runs", json={
            "selection": {"tag_filter": "Checkout"},
            "dispatch": {"runner": "careful", "parameters": {"region": "ap", "when": "after"}}})
        assert started.status_code == 201, started.text
        run_id = started.json()["run"]["id"]
        detail = wait_until(
            lambda: (d := client.get(f"{API}/runs/{run_id}").json())["state"] == "FAILED" and d,
            what="the dispatch to fail",
        )
        assert detail["dispatches"][-1]["error"] == (
            "InvalidParameter: parameter 'region': no build agent serves 'ap'"
        )

    def test_the_cli_says_it_like_a_parameter_the_declaration_refuses(self, tmp_path, capsys):
        from runcomposer.cli.main import main as cli_main

        from .conftest import write_config

        config = write_config(tmp_path, runners={"careful": {"module": f"{HERE}:CarefulRunner"}})
        code = cli_main(["run", "Checkout", "--runner", "careful", "--param", "region=ap",
                         "--config", str(config)])
        err = capsys.readouterr().err
        assert code == 1 and "error: parameter 'region': no build agent serves 'ap'" in err
        code = cli_main(["runs", "--json", "--config", str(config)])
        assert '"total": 0' in capsys.readouterr().out, "the composed run is removed again"
        code = cli_main(["plan", "Checkout", "--runner", "careful", "--param", "region=ap",
                         "--config", str(config)])
        assert code == 1 and "error: parameter 'region': no build agent serves 'ap'" in capsys.readouterr().err


class TestTheHarnessSaysSo:
    """The conformance harness reports a runner-raised ``InvalidParameter``
    on the run it hands back — not as a refusal, and not only in the
    events."""

    @pytest.fixture()
    def harness(self):
        from runcomposer.testing import ConnectorHarness

        with ConnectorHarness(runners={"careful": {"module": f"{HERE}:CarefulRunner"}}) as harness:
            yield harness

    def test_run_carries_the_parameter_and_the_message(self, harness):
        run = harness.run("Checkout", runner="careful", parameters={"region": "ap"})
        assert run.invalid is not None and run.refusal is None
        assert run.invalid.parameter == "region"
        assert str(run.invalid) == "parameter 'region': no build agent serves 'ap'"
        assert run.dispatch is None and run.state == "COMPOSED"

    def test_a_started_dispatch_says_it_on_the_handle_and_its_reading(self, harness):
        handle = harness.start("Checkout", runner="careful", parameters={"region": "ap"})
        run = handle.wait()
        assert handle.invalid is not None and handle.invalid.parameter == "region"
        assert run.invalid is not None and run.refusal is None

    def test_a_valid_run_has_neither(self, harness):
        run = harness.run("Checkout", runner="careful", parameters={"region": "eu"})
        assert (run.invalid, run.refusal, run.state) == (None, None, "COMPLETE")


PARAMETER_CASES = [
    ({"nope": "x"}, "nope", "unknown", None),
    ({"build": 7}, "build", "type", None),
    ({"build": "2026-12"}, "build", "pattern", None),
    ({"lane": "warp"}, "lane", "choice", None),
    ({"headers": {"Accept": "json"}}, "headers", "key-pattern", "Accept"),
    ({"headers": {"X-Tenant": "x y"}}, "headers", "pattern", "X-Tenant"),
    ({"region": "ap"}, "region", "runner", None),
]


class TestTheReasonIsAField:
    """``invalid-parameter`` answers say what kind of refusal it is —
    ``problem``, one of ``PARAMETER_PROBLEMS`` — and for a mapping which of
    its names, ``key``: a form marks the field without reading the text."""


    def test_the_vocabulary_is_closed(self):
        from runcomposer.sdk import PARAMETER_PROBLEMS

        assert PARAMETER_PROBLEMS == ("unknown", "required", "type", "choice", "pattern", "key-pattern", "runner")
        with pytest.raises(ValueError, match="unknown parameter problem"):
            InvalidParameter("region", "x", problem="mood")

    @pytest.mark.parametrize(("given", "parameter", "problem", "key"), PARAMETER_CASES)
    def test_over_http(self, make_client, given, parameter, problem, key):
        client, _service = make_client(runners={"formed": {"module": f"{HERE}:FormedRunner"}})
        answer = client.post(f"{API}/runs", json={
            "selection": {"tag_filter": "Checkout"}, "dispatch": {"runner": "formed", "parameters": given}})
        assert answer.status_code == 400, answer.text
        body = answer.json()
        assert (body["code"], body["parameter"], body.get("problem")) == ("invalid-parameter", parameter, problem), body
        assert body.get("key") == key

    def test_a_missing_required_parameter(self, make_client):
        client, _service = make_client(runners={"formed": {"module": f"{HERE}:FormedRunner", "strict": True}})
        body = client.post(f"{API}/runs", json={
            "selection": {"tag_filter": "Checkout"}, "dispatch": {"runner": "formed"}}).json()
        assert (body["parameter"], body["problem"]) == ("ticket", "required")


class FormedRunner(RunnerBase):
    def __init__(self, strict: bool = False):
        self.strict = strict

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="formed", parameters=(
            Parameter("build", pattern=r"\d{4}\.\d{1,2}"),
            Parameter("lane", kind="choice", choices=("eu", "us"), default="eu"),
            Parameter("headers", kind="mapping", key_pattern=r"X-[A-Za-z-]+", pattern=r"\S+"),
            Parameter("region", default="eu"),
            *((Parameter("ticket", required=True),) if self.strict else ()),
        ))

    def dispatch(self, ctx: DispatchContext) -> None:
        if ctx.parameters["region"] not in ("eu", "us"):
            raise InvalidParameter("region", f"no build agent serves {ctx.parameters['region']!r}")
        ctx.declare(["1"])
        ctx.deliver("1", verdicts=[Verdict(item, "PASS") for item in ctx.item_ids])
