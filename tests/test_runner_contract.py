"""The runner contract beyond dispatch → deliver (DESIGN.md §6.2):

- configured options and per-run parameters are two things;
- a plan preview, over the API and the CLI, that records nothing;
- live status ``RUNNING``, log lines from worker processes, and progress
  pushed by an executor that runs elsewhere;
- shard groups and their roll-up;
- a runner's read access to its own dispatch, links set after the hand-off,
  a private directory;
- expiry of dispatches left waiting for results;
- duration history: by status, and seeded from a file.
"""

from __future__ import annotations

import json
import pickle
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from runcomposer.cli import main as cli_main
from runcomposer.sdk import (
    LIVE_STATUSES,
    ConfigError,
    DispatchContext,
    LiveStatus,
    PlanContext,
    PlanPreview,
    RemoteLiveChannel,
    RunnerBase,
    RunnerInfo,
    Verdict,
    parameters_from_config,
    plan_chunks,
)
from runcomposer.testing import ConnectorHarness

from .conftest import compose, wait_until, write_config
from .exec_loader import runcomposer_exec

API = "/api/v1"
SSO = ["Shop.Auth.SSO.T001", "Shop.Auth.SSO.T002", "Shop.Auth.SSO.T003"]
HERE = "tests.test_runner_contract"

#: What each configured ScriptRunner does, by the name it was configured with.
SCRIPTS: dict[str, Callable[[DispatchContext], None]] = {}
TOKENS: dict[str, str | None] = {}


class ScriptRunner(RunnerBase):
    """A runner whose ``dispatch`` is whatever the test registered; its plan
    is one shard per configured stage, grouped by region."""

    def __init__(
        self,
        script: str = "",
        stages: list[str] | None = None,
        parameters: list[dict[str, Any]] | None = None,
        plans: bool = True,
    ):
        self.script = script
        self.stages = list(stages or ["eu-1", "eu-2", "us-1"])
        self._parameters = parameters_from_config(parameters, "script: parameters")
        self.plans = plans

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="script",
            capabilities=("cancel", "external_delivery") + (("plan",) if self.plans else ()),
            parameters=self._parameters,
            result_format="runcomposer-verdicts",
        )

    def shards(self, ctx: PlanContext) -> dict[str, list[str]]:
        stages = list(ctx.parameters.get("stages") or ctx.options.get("stages") or self.stages)
        if "closed" in stages:
            ctx.refuse("stage 'closed' takes no runs")
        chunks = plan_chunks(ctx.item_ids, len(stages), ctx.history.durations()).chunks
        return {stage: list(chunk) for stage, chunk in zip(stages, chunks, strict=False)}

    def plan(self, ctx: PlanContext) -> PlanPreview:
        shards = self.shards(ctx)
        return PlanPreview(
            text=f"{len(ctx.item_ids)} item(s) over {list(shards)}",
            shards=shards,
            groups={stage: stage.split("-")[0] for stage in shards},
            estimates_ms={stage: 1000.0 * len(items) for stage, items in shards.items()},
            notes=("stages are booked at dispatch",),
            data={"spec_bytes": len(ctx.export_spec())},
        )

    def dispatch(self, ctx: DispatchContext) -> None:
        SCRIPTS[self.script](ctx)


def declare_staged(ctx: DispatchContext) -> dict[str, list[str]]:
    """Declare one shard per stage, grouped by region; mint the token."""
    shards = ScriptRunner().shards(ctx)
    ctx.export_spec()
    ctx.declare(shards, plan="staged", groups={stage: stage.split("-")[0] for stage in shards})
    TOKENS[ctx.run_id] = ctx.ingest_token()
    return shards


def hand_off(ctx: DispatchContext) -> None:
    """Declare and return: results come from outside."""
    declare_staged(ctx)


@pytest.fixture()
def scripted(make_service, make_client):
    """``scripted(fn, client=False, **runner_options)`` → a Service (or a
    ``(client, service)`` pair) whose runner ``staged`` runs ``fn(ctx)``."""
    names: list[str] = []

    def factory(fn: Callable[[DispatchContext], None], *, client: bool = False,
                config: dict[str, Any] | None = None, **options: Any) -> Any:
        name = f"script-{len(SCRIPTS)}-{time.monotonic_ns()}"
        names.append(name)
        SCRIPTS[name] = fn
        sections = {"runners": {"staged": {"module": f"{HERE}:ScriptRunner", "script": name, **options}},
                    **(config or {})}
        return make_client(sections) if client else make_service(sections)

    yield factory
    for name in names:
        SCRIPTS.pop(name, None)


def kinds_of(service: Any, run_id: str) -> list[str]:
    return [event.kind for event in service.store.events(run_id)]


def messages_of(service: Any, run_id: str, kind: str = "log") -> list[str]:
    return [str(e.data.get("message", "")) for e in service.store.events(run_id) if e.kind == kind]


def results_bundle(directory: Path, item_ids: list[str], status: str = "PASS") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "results.json").write_text(
        json.dumps({"format": "runcomposer-verdicts",
                    "verdicts": [{"name": i, "status": status, "duration_ms": 30} for i in item_ids]}),
        encoding="utf-8",
    )
    return directory


# -- options and parameters are two things ------------------------------------------------

STAGES_PARAMETER = [{"name": "stages", "kind": "multi_choice", "choices": ["eu-1", "eu-2", "us-1", "closed"]}]


class TestOptionsAndParameters:
    def test_a_parameter_never_shadows_the_option_of_the_same_name(self, scripted):
        """The runner has an *option* ``stages`` (what exists) and a
        *parameter* ``stages`` (what was chosen). Each is read where it
        lives; neither stands in for the other."""
        seen: list[tuple[dict, dict]] = []
        frozen: list[str] = []

        def script(ctx: DispatchContext) -> None:
            seen.append((dict(ctx.options), dict(ctx.parameters)))
            try:
                ctx.options["stages"] = []  # type: ignore[index]
            except TypeError:
                frozen.append("options are read-only")
            ctx.declare(["1"])
            ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        service = scripted(script, stages=["eu-1", "eu-2"], parameters=STAGES_PARAMETER)
        chosen = compose(service, "Auth-SSO", runner_id="staged", parameters={"stages": ["eu-2"]})
        service.dispatch(chosen, "staged", {"stages": ["eu-2"]}, foreground=True)
        nothing_chosen = compose(service, "Auth-SSO", runner_id="staged")
        service.dispatch(nothing_chosen, "staged", foreground=True)

        (options, parameters), (options_again, no_parameters) = seen
        assert options["stages"] == ["eu-1", "eu-2"] and parameters == {"stages": ["eu-2"]}
        # Not submitted, no default: it is simply absent — and the option is
        # still the option, not a stand-in that reads as "all of them".
        assert no_parameters == {} and options_again["stages"] == ["eu-1", "eu-2"]
        assert frozen == ["options are read-only"] * 2

    def test_defaults_are_applied_to_parameters_not_to_options(self, scripted):
        seen: dict[str, Any] = {}

        def script(ctx: DispatchContext) -> None:
            seen.update(options=dict(ctx.options), parameters=dict(ctx.parameters))
            ctx.declare(["1"])
            ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        service = scripted(script, parameters=[
            {"name": "retries", "kind": "integer", "default": 2},
            {"name": "variables", "kind": "mapping", "default": {"LOCALE": "de"}},
        ])
        run_id = compose(service, "Auth-SSO", runner_id="staged")
        service.dispatch(run_id, "staged", foreground=True)
        assert seen["parameters"] == {"retries": 2, "variables": {"LOCALE": "de"}}
        assert "retries" not in seen["options"] and "variables" not in seen["options"]


# -- plan previews ---------------------------------------------------------------------


class TestPlanApi:
    def test_a_plan_for_a_selection_stores_nothing(self, scripted):
        client, service = scripted(hand_off, client=True, parameters=STAGES_PARAMETER)
        response = client.post(f"{API}/plan", json={
            "selection": {"tag_filter": "Auth-SSO"}, "runner": "staged", "parameters": {"stages": ["eu-1", "us-1"]},
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["runner"] == "staged" and body["run_id"] is None and body["item_count"] == 3
        assert body["parameters"] == {"stages": ["eu-1", "us-1"]}
        assert (body["refused"], body["refusal"], body["warnings"]) == (False, None, [])
        plan = body["plan"]
        assert plan["text"] == "3 item(s) over ['eu-1', 'us-1']"
        assert plan["shards"] == [
            {"shard": "eu-1", "group": "eu", "count": 2, "item_ids": [SSO[0], SSO[2]], "estimate_ms": 2000.0},
            {"shard": "us-1", "group": "us", "count": 1, "item_ids": [SSO[1]], "estimate_ms": 1000.0},
        ]
        assert plan["groups"] == [
            {"group": "eu", "shards": ["eu-1"], "count": 2, "estimate_ms": 2000.0},
            {"group": "us", "shards": ["us-1"], "count": 1, "estimate_ms": 1000.0},
        ]
        assert plan["notes"] == ["stages are booked at dispatch"] and plan["data"]["spec_bytes"] > 100
        assert service.list_runs().total == 0 and not service.config.artifact_dir.exists()

    def test_a_plan_for_a_stored_run_uses_its_parameters_and_leaves_it_alone(self, scripted):
        client, service = scripted(hand_off, client=True, parameters=STAGES_PARAMETER)
        run_id = compose(service, "Auth-SSO", runner_id="staged", parameters={"stages": ["eu-2"]})
        before = kinds_of(service, run_id)
        body = client.post(f"{API}/runs/{run_id}/plan", json={"runner": "staged"}).json()
        assert body["run_id"] == run_id and body["parameters"] == {"stages": ["eu-2"]}
        assert [shard["shard"] for shard in body["plan"]["shards"]] == ["eu-2"]
        other = client.post(f"{API}/runs/{run_id}/plan", json={"runner": "staged", "parameters": {"stages": ["us-1"]}})
        assert [shard["shard"] for shard in other.json()["plan"]["shards"]] == ["us-1"]
        run = service.require_run(run_id)
        assert run.state == "COMPOSED" and run.dispatches == () and kinds_of(service, run_id) == before
        assert service.require_spec(run_id)["runner"]["staged"] == {"stages": ["eu-2"]}, "the document is untouched"

    def test_a_refusal_is_the_answer_not_an_error(self, scripted):
        client, _service = scripted(hand_off, client=True, parameters=STAGES_PARAMETER)
        body = client.post(f"{API}/plan", json={
            "selection": {"tag_filter": "Auth-SSO"}, "runner": "staged", "parameters": {"stages": ["closed"]},
        }).json()
        assert (body["refused"], body["refusal"], body["plan"]) == (True, "stage 'closed' takes no runs", None)

    def test_what_a_dispatch_request_is_refused_for_up_front_a_plan_request_is_too(self, scripted):
        client, _service = scripted(hand_off, client=True, parameters=STAGES_PARAMETER)
        selection = {"tag_filter": "Auth-SSO"}
        bad_parameter = client.post(f"{API}/plan", json={
            "selection": selection, "runner": "staged", "parameters": {"stages": ["mars-1"]}})
        assert (bad_parameter.status_code, bad_parameter.json()["code"]) == (400, "invalid-parameter")
        unknown = client.post(f"{API}/plan", json={"selection": selection, "runner": "nope"})
        assert (unknown.status_code, unknown.json()["code"]) == (400, "unknown-runner")
        empty = client.post(f"{API}/plan", json={"selection": {"tag_filter": "No-Such-Tag"}, "runner": "staged"})
        assert empty.status_code == 400 and "nothing to execute" in empty.json()["detail"]
        assert client.post(f"{API}/runs/01NOSUCHRUN000000000000000/plan", json={"runner": "staged"}).status_code == 404
        incomplete = client.post(f"{API}/plan", json={"selection": selection})
        assert incomplete.status_code == 400 and incomplete.json()["fields"][0]["field"] == "runner"

    def test_a_runner_without_the_capability_says_so(self, scripted):
        client, _service = scripted(hand_off, client=True, plans=False)
        response = client.post(f"{API}/plan", json={"selection": {"tag_filter": "Auth-SSO"}, "runner": "staged"})
        assert response.status_code == 400 and "does not offer a plan preview" in response.json()["detail"]
        (listed,) = client.get(f"{API}/runners").json()
        assert "plan" not in listed["capabilities"]

    def test_a_plan_that_raises_is_a_refusal_with_the_reason(self, make_client):
        client, _service = make_client(runners={"broken": {"module": f"{HERE}:BrokenPlanner"}})
        body = client.post(f"{API}/plan", json={"selection": {"tag_filter": "Auth-SSO"}, "runner": "broken"}).json()
        assert body["refused"] and body["refusal"] == "KeyError: 'stage_map'"

    def test_the_plan_context_has_nothing_to_act_with(self, make_service):
        service = make_service(runners={"nosy": {"module": f"{HERE}:NosyPlanner"}})
        outcome = service.plan("nosy", selection={"tag_filter": "Auth-SSO"})
        assert outcome.preview.data == {
            "missing": ["add_artifact", "declare", "deliver", "ingest_token", "live", "live_channel",
                        "log", "private_dir", "set_link", "workdir"],
        }

    def test_every_reference_runner_previews(self, make_service):
        service = make_service(runners={
            "demo": {},
            "pytest": {"plugin": "command", "command": "true", "shards": 2},
        })
        demo = service.plan("demo", selection={"tag_filter": "Auth-SSO"}, parameters={"seed": "x"}).preview
        assert demo.shards == {"1": SSO} and "seed 'x'" in demo.text and demo.notes
        command = service.plan("pytest", selection={"tag_filter": "Auth-SSO"}).preview
        assert command.shards == {"1": [SSO[0], SSO[2]], "2": [SSO[1]]} and "2 shard(s)" in command.text


class BrokenPlanner(RunnerBase):
    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="broken", capabilities=("plan",))

    def plan(self, ctx: PlanContext) -> PlanPreview:
        raise KeyError("stage_map")

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.refuse("never")


class NosyPlanner(RunnerBase):
    """Reports which acting members of a dispatch context a plan context lacks."""

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="nosy", capabilities=("plan",))

    def plan(self, ctx: PlanContext) -> PlanPreview:
        acting = ("declare", "deliver", "live", "live_channel", "add_artifact", "log", "ingest_token",
                  "set_link", "workdir", "private_dir")
        missing = []
        for name in acting:
            try:
                object.__getattribute__(ctx, name)
            except AttributeError:
                missing.append(name)
        return PlanPreview(text="looked", shards={"1": list(ctx.item_ids)}, data={"missing": sorted(missing)})

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.refuse("never")


class TestPlanCommand:
    def config(self, tmp_path: Path) -> str:
        SCRIPTS["cli"] = hand_off
        return str(write_config(tmp_path, runners={
            "staged": {"module": f"{HERE}:ScriptRunner", "script": "cli", "parameters": STAGES_PARAMETER}}))

    def test_it_prints_the_plan_and_says_that_nothing_happened(self, tmp_path, capsys):
        code = cli_main(["plan", "Auth-SSO", "--runner", "staged", "--param", "stages=[eu-1,us-1]",
                         "--items", "--config", self.config(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "plan for this selection on runner staged: 3 item(s)" in out
        assert "group eu: 1 shard(s), 2 item(s), est 2.0s" in out
        assert "shard us-1 [us]: 1 item(s), est 1.0s" in out and f"    {SSO[1]}" in out
        assert "note: stages are booked at dispatch" in out
        assert out.rstrip().endswith("nothing was executed, stored or triggered")
        assert cli_main(["runs", "--json", "--config", self.config(tmp_path)]) == 0
        assert json.loads(capsys.readouterr().out)["total"] == 0

    def test_a_refusal_is_exit_code_1_and_json_is_the_api_s_shape(self, tmp_path, capsys):
        config = self.config(tmp_path)
        assert cli_main(["plan", "Auth-SSO", "--runner", "staged", "--param", "stages=[closed]", "--config", config]) == 1
        assert "would be refused: stage 'closed' takes no runs" in capsys.readouterr().out
        assert cli_main(["plan", "Auth-SSO", "--runner", "staged", "--json", "--config", config]) == 0
        body = json.loads(capsys.readouterr().out)
        assert set(body) == {"runner", "run_id", "item_count", "parameters", "refused", "refusal", "plan", "warnings"}
        assert [s["shard"] for s in body["plan"]["shards"]] == ["eu-1", "eu-2", "us-1"]

    def test_a_stored_run_is_planned_by_id(self, tmp_path, capsys):
        config = self.config(tmp_path)
        assert cli_main(["spec", "Auth-SSO", "--runner", "staged", "--param", "stages=[eu-2]", "--format", "json",
                         "--config", config]) == 0
        run_id = json.loads(capsys.readouterr().out)["run"]["id"]
        assert cli_main(["plan", "--run", run_id, "--runner", "staged", "--config", config]) == 0
        out = capsys.readouterr().out
        assert f"plan for run {run_id} on runner staged" in out and "shard eu-2 [eu]: 3 item(s)" in out
        assert cli_main(["plan", "Auth-SSO", "--run", run_id, "--runner", "staged", "--config", config]) == 2


# -- live status: RUNNING, worker log lines -----------------------------------------------

_WORKER = """
import pickle, sys
channel = pickle.loads(bytes.fromhex(sys.argv[1]))
channel.live("eu-1", sys.argv[2], "RUNNING")
channel.log("worker: booting the browser")
channel.log("worker: the proxy answered slowly", "warning")
"""


class TestRunningAndWorkerLogs:
    def test_running_is_a_live_status_and_never_a_verdict(self):
        assert LIVE_STATUSES == ("RUNNING", "PASS", "FAIL", "SKIP", "ERROR")
        assert LiveStatus(SSO[0], "RUNNING").duration_ms == 0
        with pytest.raises(ValueError, match="verdict status must be one of"):
            Verdict(SSO[0], "RUNNING")
        with pytest.raises(ValueError, match="live status must be one of"):
            LiveStatus(SSO[0], "MISSING")

    def test_a_test_is_running_then_done_and_the_delivery_clears_both(self, scripted):
        started, release = threading.Event(), threading.Event()
        steps: list[threading.Event] = [threading.Event(), threading.Event()]

        def script(ctx: DispatchContext) -> None:
            shards = declare_staged(ctx)
            first, second = shards["eu-1"][0], shards["eu-2"][0]
            ctx.live("eu-1", first, "RUNNING")
            ctx.live("eu-2", second, "RUNNING")
            started.set()
            assert steps[0].wait(30)
            ctx.live("eu-1", first, "PASS", duration_ms=120)
            steps[1].set()
            assert release.wait(30)
            for shard, items in shards.items():
                ctx.deliver(shard, verdicts=[Verdict(i, "PASS", 120) for i in items])

        client, service = scripted(script, client=True)
        run_id = compose(service, "Auth-SSO", runner_id="staged")
        service.dispatch(run_id, "staged")
        assert started.wait(30)
        detail = client.get(f"{API}/runs/{run_id}").json()
        assert sorted((v["shard"], v["status"], v["live"]) for v in detail["verdicts"]) == [
            ("eu-1", "RUNNING", True), ("eu-2", "RUNNING", True),
        ]
        assert detail["live_summary"] == {"eu-1": {"RUNNING": 1}, "eu-2": {"RUNNING": 1}}
        assert detail["verdict_summary"] == {} and detail["completion"] is None, "nothing is a result yet"
        steps[0].set()
        assert steps[1].wait(30)
        detail = client.get(f"{API}/runs/{run_id}").json()
        assert detail["live_summary"] == {"eu-1": {"PASS": 1}, "eu-2": {"RUNNING": 1}}
        by_group = {group["group"]: group for group in detail["groups"]}
        assert by_group["eu"]["live"] == {"PASS": 1, "RUNNING": 1} and by_group["eu"]["completion"] is None
        running = client.get(f"{API}/runs/{run_id}/verdicts", params={"status": "RUNNING"})
        assert running.status_code == 200 and [v["shard"] for v in running.json()["verdicts"]] == ["eu-2"]
        release.set()
        done = service.wait(run_id, timeout=30)
        assert (done.state, done.completion) == ("COMPLETE", "PASS")
        final = client.get(f"{API}/runs/{run_id}").json()
        assert final["live_summary"] == {} and not [v for v in final["verdicts"] if v["live"]]

    def test_running_cannot_be_delivered_by_a_runner_or_pushed_as_a_result(self, scripted, tmp_path):
        errors: list[str] = []

        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            try:
                ctx.deliver("1", verdicts=[LiveStatus(i, "RUNNING") for i in ctx.item_ids])  # type: ignore[misc]
            except ValueError as exc:
                errors.append(str(exc))
            TOKENS[ctx.run_id] = ctx.ingest_token()

        service = scripted(script)
        run_id = compose(service, "Auth-SSO", runner_id="staged")
        service.dispatch(run_id, "staged", foreground=True)
        assert errors and "carries status 'RUNNING'" in errors[0]
        from runcomposer.sdk import IngestError

        with pytest.raises(IngestError, match=r"must be one of .*got 'RUNNING'"):
            service.ingest(results_bundle(tmp_path / "bundle", SSO, status="RUNNING"), run_id=run_id)
        assert service.store.verdicts_for(run_id) == []

    def test_a_worker_process_logs_into_the_event_stream(self, scripted):
        def script(ctx: DispatchContext) -> None:
            shards = declare_staged(ctx)
            channel = pickle.dumps(ctx.live_channel()).hex()
            subprocess.run([sys.executable, "-c", _WORKER, channel, shards["eu-1"][0]], check=True, timeout=60)
            # Seen by the dispatching process before it goes on (a delivery drains the channel).
            for shard, items in shards.items():
                ctx.deliver(shard, verdicts=[Verdict(i, "PASS") for i in items])

        service = scripted(script)
        run_id = compose(service, "Auth-SSO", runner_id="staged")
        service.dispatch(run_id, "staged", foreground=True)
        assert "worker: booting the browser" in messages_of(service, run_id, "log")
        assert "worker: the proxy answered slowly" in messages_of(service, run_id, "warning")
        kinds = kinds_of(service, run_id)
        assert kinds.index("log") < kinds.index("delivery.recorded"), "while it ran, not afterwards"


# -- progress pushed by an executor that runs elsewhere --------------------------------------


@pytest.fixture()
def awaiting(scripted):
    """A dispatch that waits for results, with its ingest token."""
    client, service = scripted(hand_off, client=True)
    run_id = compose(service, "Auth-SSO", runner_id="staged")
    outcome = service.dispatch(run_id, "staged", foreground=True)
    return client, service, run_id, outcome.dispatch_id, TOKENS[run_id]


def token(value: str | None) -> dict[str, str]:
    return {"X-Runcomposer-Token": value} if value else {}


class TestPushedProgress:
    def test_live_status_is_taken_with_the_dispatch_s_token(self, awaiting):
        client, service, run_id, dispatch_id, secret = awaiting
        response = client.post(f"{API}/runs/{run_id}/live", headers=token(secret), json={"updates": [
            {"shard": "eu-1", "item_id": SSO[0], "status": "RUNNING"},
            {"shard": "eu-2", "item_id": SSO[1], "status": "PASS", "duration_ms": 80, "message": "ok"},
            {"shard": "eu-1", "item_id": "Shop.Not.Planned", "status": "PASS"},
        ]})
        assert response.status_code == 200, response.text
        assert response.json() == {"dispatch_id": dispatch_id, "accepted": 2, "ignored": 1, "reason": None}
        live = {(v.shard, v.item_id): v for v in service.store.live_verdicts(run_id, dispatch_id)}
        assert live[("eu-1", SSO[0])].status == "RUNNING"
        assert (live[("eu-2", SSO[1])].status, live[("eu-2", SSO[1])].duration_ms) == ("PASS", 80)
        assert client.get(f"{API}/runs/{run_id}").json()["live_summary"] == {
            "eu-1": {"RUNNING": 1}, "eu-2": {"PASS": 1},
        }
        assert service.store.verdicts_for(run_id) == [], "progress is not a result"

    def test_nothing_is_taken_without_the_right_token(self, awaiting, scripted):
        client, service, run_id, dispatch_id, secret = awaiting
        body = {"updates": [{"shard": "eu-1", "item_id": SSO[0], "status": "RUNNING"}]}
        for path, payload in (("live", body), ("log", {"lines": [{"message": "hello"}]})):
            assert client.post(f"{API}/runs/{run_id}/{path}", json=payload).status_code == 401
            assert client.post(f"{API}/runs/{run_id}/{path}", headers=token("rct_wrong"), json=payload).status_code == 403
            other = client.post(f"{API}/runs/{run_id}/{path}", headers=token(secret),
                                json={**payload, "dispatch": "01SOMEOTHERDISPATCH0000000"})
            assert other.status_code == 403 and "reports for that dispatch only" in other.json()["detail"]
        assert client.post(f"{API}/runs/01NOSUCHRUN000000000000000/live", headers=token(secret), json=body).status_code == 404
        assert service.store.live_verdicts(run_id, dispatch_id) == []
        assert "hello" not in messages_of(service, run_id)

    def test_a_status_nobody_can_report_and_a_malformed_body_are_400s(self, awaiting):
        client, _service, run_id, _dispatch_id, secret = awaiting
        missing = client.post(f"{API}/runs/{run_id}/live", headers=token(secret), json={"updates": [
            {"shard": "eu-1", "item_id": SSO[0], "status": "MISSING"}]})
        assert missing.status_code == 400 and "live status must be one of" in missing.json()["detail"]
        assert client.post(f"{API}/runs/{run_id}/live", headers=token(secret), json={"updates": []}).status_code == 400
        assert client.post(f"{API}/runs/{run_id}/live", headers=token(secret),
                           json={"updates": [{"shard": "eu-1", "status": "PASS"}]}).status_code == 400

    def test_a_delivered_shard_and_a_finished_dispatch_take_no_more_live_status(self, awaiting, tmp_path):
        client, service, run_id, dispatch_id, secret = awaiting
        service.ingest(results_bundle(tmp_path / "eu-1", [SSO[0]]), run_id=run_id, shard="eu-1")
        update = {"updates": [{"shard": "eu-1", "item_id": SSO[0], "status": "RUNNING"},
                              {"shard": "eu-2", "item_id": SSO[1], "status": "RUNNING"}]}
        assert client.post(f"{API}/runs/{run_id}/live", headers=token(secret), json=update).json() == {
            "dispatch_id": dispatch_id, "accepted": 1, "ignored": 1, "reason": None,
        }
        service.cancel(run_id)
        late = client.post(f"{API}/runs/{run_id}/live", headers=token(secret), json=update).json()
        assert (late["accepted"], late["ignored"]) == (0, 2) and "is CANCELLED" in late["reason"]

    def test_log_lines_become_events_marked_as_pushed(self, awaiting):
        client, service, run_id, dispatch_id, secret = awaiting
        response = client.post(f"{API}/runs/{run_id}/log", headers={"Authorization": f"Bearer {secret}"}, json={
            "lines": [{"message": "agent: checkout started"}, {"message": "agent: disk almost full", "level": "warning"}]})
        assert response.json() == {"dispatch_id": dispatch_id, "accepted": 2}
        events = [e for e in service.store.events(run_id) if e.data.get("source") == "push"]
        assert [(e.kind, e.data["message"], e.dispatch_id) for e in events] == [
            ("log", "agent: checkout started", dispatch_id),
            ("warning", "agent: disk almost full", dispatch_id),
        ]


class TestTheRemoteChannelAndTheConsumer:
    """Over real HTTP: a serving deployment, and the client side an agent uses."""

    @pytest.fixture()
    def served(self):
        SCRIPTS["served"] = hand_off
        with ConnectorHarness(
            runners={"staged": {"module": f"{HERE}:ScriptRunner", "script": "served", "stages": ["1"]}}, serve=True,
        ) as harness:
            result = harness.run("Auth-SSO", runner="staged")
            assert result.state == "AWAITING_RESULTS"
            yield harness, result.run_id, result.dispatch.dispatch_id, TOKENS[result.run_id]

    def test_remote_live_channel_streams_status_and_log_lines(self, served):
        harness, run_id, dispatch_id, secret = served
        channel = pickle.loads(pickle.dumps(
            RemoteLiveChannel(f"{harness.api}/runs/{run_id}/results", secret, dispatch_id=dispatch_id)))
        channel.live("1", SSO[0], "RUNNING")
        channel.live("1", SSO[0], "PASS", duration_ms=40)
        channel.log("agent: first test done")
        run = harness.read(run_id)
        assert [(s.item_id, s.status, s.duration_ms) for s in run.live] == [(SSO[0], "PASS", 40)]
        assert "agent: first test done" in run.logs() and channel.failures == 0
        # Best-effort by design: what cannot be pushed is counted, never raised.
        RemoteLiveChannel(f"{harness.api}/runs/{run_id}/results", "rct_wrong").live("1", SSO[0], "PASS")
        lost = RemoteLiveChannel("http://127.0.0.1:9/api/v1/runs/x/results", secret, timeout_s=0.5)
        lost.log("into the void")
        assert lost.failures == 1

    def test_the_robot_listener_for_agents_reports_through_it(self, served, monkeypatch):
        from types import SimpleNamespace

        from runcomposer.sdk.robot import RemoteLiveStatusListener

        harness, run_id, _dispatch_id, secret = served
        monkeypatch.setenv("RUNCOMPOSER_CALLBACK_URL", f"{harness.api}/runs/{run_id}/results")
        monkeypatch.setenv("RUNCOMPOSER_INGEST_TOKEN", secret)
        listener = RemoteLiveStatusListener()
        listener.start_test(None, SimpleNamespace(longname=SSO[1]))
        assert [(s.shard, s.item_id, s.status) for s in harness.read(run_id).live] == [("1", SSO[1], "RUNNING")]
        listener.end_test(None, SimpleNamespace(longname=SSO[1], status="FAIL", elapsedtime=7, message="declined"))
        assert [(s.status, s.message) for s in harness.read(run_id).live] == [("FAIL", "declined")]
        monkeypatch.delenv("RUNCOMPOSER_CALLBACK_URL")
        RemoteLiveStatusListener().start_test(None, SimpleNamespace(longname=SSO[1]))  # nowhere to report: a no-op

    def test_runcomposer_exec_reports_while_it_works_and_pushes_its_bundle(self, served, tmp_path):
        harness, run_id, dispatch_id, secret = served
        with urllib.request.urlopen(f"{harness.api}/runs/{run_id}/dispatches/{dispatch_id}/spec", timeout=10) as response:
            (tmp_path / "spec.json").write_bytes(response.read())
        code = runcomposer_exec.main([
            str(tmp_path / "spec.json"), "--out", str(tmp_path / "results"), "--simulate", "--seed", "calm",
            "--dispatch", dispatch_id, "--shard", "1",
            "--callback", f"{harness.api}/runs/{run_id}/results", "--token", secret, "--push",
        ])
        assert code == 0
        run = harness.wait(run_id, timeout=30)
        assert run.state == "COMPLETE" and len(run.verdicts) == 3
        assert any("3 item(s) to execute" in line for line in run.logs())
        assert run.live == [], "the delivery replaced what was streamed"

    def test_a_push_that_is_refused_is_an_error_of_the_consumer(self, served, tmp_path, capsys):
        harness, run_id, _dispatch_id, _secret = served
        with urllib.request.urlopen(f"{harness.api}/runs/{run_id}/spec", timeout=10) as response:
            (tmp_path / "spec.json").write_bytes(response.read())
        code = runcomposer_exec.main([
            str(tmp_path / "spec.json"), "--out", str(tmp_path / "results"), "--simulate",
            "--callback", f"{harness.api}/runs/{run_id}/results", "--token", "rct_wrong", "--push",
        ])
        assert code == 4 and "the push was refused (403)" in capsys.readouterr().err
        assert harness.read(run_id).state == "AWAITING_RESULTS"
        with pytest.raises(SystemExit, match="--push needs --callback"):
            runcomposer_exec.main([str(tmp_path / "spec.json"), "--simulate", "--push"])


# -- shard groups ------------------------------------------------------------------------


class TestShardGroups:
    def test_groups_are_recorded_and_rolled_up_as_shards_deliver(self, awaiting, tmp_path):
        client, service, run_id, dispatch_id, _secret = awaiting
        record = service.store.get_dispatch(dispatch_id)
        assert dict(record.shard_groups) == {"eu-1": "eu", "eu-2": "eu", "us-1": "us"}
        declared = next(e for e in service.store.events(run_id) if e.kind == "run.running")
        assert declared.data["groups"] == {"eu-1": "eu", "eu-2": "eu", "us-1": "us"}
        service.ingest(results_bundle(tmp_path / "eu-1", [SSO[0]], "FAIL"), run_id=run_id, shard="eu-1")
        detail = client.get(f"{API}/runs/{run_id}").json()
        assert detail["shard_groups"] == {"eu-1": "eu", "eu-2": "eu", "us-1": "us"}
        assert detail["dispatches"][0]["shard_groups"] == detail["shard_groups"]
        eu, us = detail["groups"]
        assert eu == {
            "group": "eu", "shards": ["eu-1", "eu-2"], "declared_shards": 2, "delivered_shards": 1,
            "planned": 2, "count": 1, "summary": {"FAIL": 1}, "live": {}, "completion": None,
        }
        assert (us["group"], us["delivered_shards"], us["completion"]) == ("us", 0, None)
        service.ingest(results_bundle(tmp_path / "eu-2", [SSO[1]]), run_id=run_id, shard="eu-2")
        eu = client.get(f"{API}/runs/{run_id}").json()["groups"][0]
        assert (eu["delivered_shards"], eu["summary"], eu["completion"]) == (2, {"FAIL": 1, "PASS": 1}, "FAIL")

    def test_an_artifact_may_belong_to_a_group_and_a_label_needs_a_declared_shard(self, scripted):
        problems: list[str] = []

        def script(ctx: DispatchContext) -> None:
            for groups in ({"mars-1": "mars"}, ["eu"]):
                try:
                    ctx.declare(["eu-1"], groups=groups)  # type: ignore[arg-type]
                except (ValueError, TypeError) as exc:
                    problems.append(f"{type(exc).__name__}: {exc}")
            ctx.declare(["eu-1", "eu-2"], groups={"eu-1": "eu", "eu-2": "eu"})
            log = ctx.workdir / "eu.html"
            log.write_text("<html>one log for the whole region</html>", encoding="utf-8")
            ctx.add_artifact("log (eu)", log, group="eu")
            for shard in ("eu-1", "eu-2"):
                ctx.deliver(shard, verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        client, service = scripted(script, client=True)
        run_id = compose(service, "Auth-SSO", runner_id="staged")
        service.dispatch(run_id, "staged", foreground=True)
        assert problems == [
            "ValueError: groups names shard(s) that are not declared: ['mars-1']",
            "TypeError: declare(groups=…) takes a mapping of shard name → group label",
        ]
        (artifact,) = client.get(f"{API}/runs/{run_id}").json()["artifacts"]
        assert (artifact["name"], artifact["group"], artifact["shard"], artifact["kind"]) == ("log (eu)", "eu", None, "local")
        assert client.get(artifact["href"]).status_code == 200

    def test_a_dispatch_without_groups_has_none(self, service):
        from runcomposer.app.views import run_detail

        run_id = compose(service, "Auth-SSO", runner_id="demo")
        service.dispatch(run_id, "demo", foreground=True)
        detail = run_detail(service, service.require_run(run_id))
        assert (detail["shard_groups"], detail["groups"]) == ({}, [])


# -- a runner reads its own dispatch --------------------------------------------------------


class TestOwnDispatch:
    def test_delivered_and_outstanding_shards_include_what_arrived_from_outside(self, scripted, tmp_path):
        """Results may be pushed while ``dispatch`` is still running — by a
        job that reports for itself. The runner sees it, and may find its
        dispatch complete before it has delivered anything."""
        readings: list[Any] = []
        pushed, release = threading.Event(), threading.Event()

        def script(ctx: DispatchContext) -> None:
            shards = declare_staged(ctx)
            readings.append((ctx.delivered_shards(), ctx.outstanding_shards()))
            pushed.set()
            assert release.wait(30)
            readings.append((ctx.delivered_shards(), ctx.outstanding_shards()))
            # Delivering what is already in is harmless: idempotent by content.
            readings.append(ctx.deliver("eu-1", verdicts=[Verdict(i, "PASS", 30) for i in shards["eu-1"]]))

        service = scripted(script)
        run_id = compose(service, "Auth-SSO", runner_id="staged")
        outcome = service.dispatch(run_id, "staged")
        assert pushed.wait(30)
        for index, stage in enumerate(("eu-1", "eu-2", "us-1")):
            service.ingest(results_bundle(tmp_path / stage, [SSO[index]]), run_id=run_id, shard=stage)
        assert service.require_run(run_id).state == "COMPLETE", "complete while the runner is still in dispatch()"
        release.set()
        assert outcome.done.wait(30)
        assert readings[0] == (set(), ["eu-1", "eu-2", "us-1"])
        assert readings[1] == ({"eu-1", "eu-2", "us-1"}, [])
        assert readings[2] in ("duplicate", "replaced")
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "PASS")

    def test_a_link_set_after_the_hand_off_is_stored_and_survives_into_a_cancel(self, make_service):
        seen: dict[str, Any] = {}
        SCRIPTS["linked"] = lambda ctx: (
            ctx.set_link("ticket", "https://desk.shop.example/t/1"),   # before: part of the hand-off
            ctx.declare(["1"], links={"queue": "https://ci.shop.example/queue/item/7/"}),
            ctx.set_link("build", "https://ci.shop.example/job/nightly/42"),
            seen.update(in_dispatch=dict(ctx.links)),
        )
        service = make_service(runners={"staged": {"module": f"{HERE}:LinkedRunner", "script": "linked"}})
        LinkedRunner.cancelled_with = None
        run_id = compose(service, "Auth-SSO", runner_id="staged")
        service.dispatch(run_id, "staged", foreground=True)
        links = {
            "ticket": "https://desk.shop.example/t/1",
            "queue": "https://ci.shop.example/queue/item/7/",
            "build": "https://ci.shop.example/job/nightly/42",
        }
        assert seen["in_dispatch"] == links
        assert dict(service.require_run(run_id).latest_dispatch.links) == links
        assert "link build: https://ci.shop.example/job/nightly/42" in messages_of(service, run_id)
        service.cancel(run_id)  # nothing executes it any more: a context rebuilt from the record
        assert LinkedRunner.cancelled_with == links

    def test_the_private_directory_is_outside_the_artifact_directory_and_goes_with_the_run(self, scripted):
        where: dict[str, Path] = {}

        def script(ctx: DispatchContext) -> None:
            where.update(private=ctx.private_dir, work=ctx.workdir)
            (ctx.private_dir / "environment.bak").write_text("SHOP_API_KEY=k-123456", encoding="utf-8")
            ctx.declare(["1"])
            ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        client, service = scripted(script, client=True)
        run_id = compose(service, "Auth-SSO", runner_id="staged")
        service.dispatch(run_id, "staged", foreground=True)
        artifact_root = service.config.artifact_dir.resolve()
        assert where["private"].is_dir() and artifact_root not in where["private"].parents
        assert where["private"].parent.name == run_id and artifact_root in where["work"].parents
        dispatch_id = where["work"].name
        for guess in ("environment.bak", "../../../private/" + run_id + "/" + dispatch_id + "/environment.bak"):
            assert client.get(f"/artifacts/{run_id}/{dispatch_id}/{guess}").status_code == 404
        service.delete_run(run_id)
        assert not where["private"].exists()

    def test_a_private_directory_inside_the_artifact_directory_is_a_config_error(self, tmp_path):
        from runcomposer.sdk import load_config

        with pytest.raises(ConfigError, match=r"core\.private_dir .* must lie outside core\.artifact_dir"):
            load_config(str(write_config(tmp_path, core={"artifact_dir": "files", "private_dir": "files/private"})))
        config = load_config(str(write_config(tmp_path, core={"private_dir": "vault"})))
        assert config.private_dir == tmp_path / "vault"

    def test_gc_sweeps_private_directories_of_runs_that_are_gone(self, service):
        stray = service.config.private_dir / "01ABCDEFGHJKMNPQRSTVWXYZ00" / "d"
        stray.mkdir(parents=True)
        service.gc()
        assert not stray.parent.exists()


class LinkedRunner(RunnerBase):
    cancelled_with: dict[str, str] | None = None

    def __init__(self, script: str):
        self.script = script

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="linked", capabilities=("cancel", "external_delivery"))

    def dispatch(self, ctx: DispatchContext) -> None:
        SCRIPTS[self.script](ctx)

    def cancel(self, ctx: DispatchContext) -> None:
        type(self).cancelled_with = dict(ctx.links)


# -- dispatches that wait too long ------------------------------------------------------------

EXPIRY = {"config": {"core": {"dispatch": {"awaiting_timeout_s": 600}}}}


def minutes_from_now(minutes: float) -> int:
    return int((time.time() + minutes * 60) * 1000)


class TestAwaitingExpiry:
    def test_nothing_expires_unless_the_deployment_says_how_long_to_wait(self, scripted):
        service = scripted(hand_off)
        run_id = compose(service, "Auth-SSO", runner_id="staged")
        service.dispatch(run_id, "staged", foreground=True)
        assert service.expire_awaiting(now_ms=minutes_from_now(60 * 24 * 365)) == []
        assert service.require_run(run_id).state == "AWAITING_RESULTS"

    def test_a_dispatch_left_waiting_is_closed_with_an_event_that_says_why(self, scripted):
        service = scripted(hand_off, **EXPIRY)
        run_id = compose(service, "Auth-SSO", runner_id="staged")
        service.dispatch(run_id, "staged", foreground=True)
        assert service.expire_awaiting(now_ms=minutes_from_now(9)) == [], "nine of ten minutes: still waiting"
        (expired,) = service.expire_awaiting(now_ms=minutes_from_now(11))
        assert (expired.state, expired.owner) == ("FAILED", None)
        assert "longer than core.dispatch.awaiting_timeout_s (600s)" in expired.error
        assert "outstanding shard(s): ['eu-1', 'eu-2', 'us-1']" in expired.error
        event = [e for e in service.store.events(run_id) if e.kind == "run.failed"][-1]
        assert event.data["expired"] is True and event.data["awaiting_timeout_s"] == 600.0
        assert event.data["outstanding_shards"] == ["eu-1", "eu-2", "us-1"] and event.data["waited_s"] >= 600
        run = service.require_run(run_id)
        assert run.state == "FAILED" and dict(run.latest_dispatch.summary) == {"MISSING": 3}
        assert service.expire_awaiting(now_ms=minutes_from_now(30)) == [], "once"

    def test_every_delivery_restarts_the_clock_and_late_results_still_complete_it(self, scripted, tmp_path):
        service = scripted(hand_off, **EXPIRY)
        run_id = compose(service, "Auth-SSO", runner_id="staged")
        service.dispatch(run_id, "staged", foreground=True)
        service.ingest(results_bundle(tmp_path / "eu-1", [SSO[0]]), run_id=run_id, shard="eu-1")
        delivered_at = int(time.time() * 1000)
        assert service.expire_awaiting(now_ms=delivered_at + 9 * 60 * 1000) == []
        (expired,) = service.expire_awaiting(now_ms=delivered_at + 11 * 60 * 1000)
        assert "outstanding shard(s): ['eu-2', 'us-1']" in expired.error
        assert dict(service.require_run(run_id).latest_dispatch.summary) == {"PASS": 1, "MISSING": 2}
        # The job was only slow: what it still sends is taken in.
        service.ingest(results_bundle(tmp_path / "eu-2", [SSO[1]]), run_id=run_id, shard="eu-2")
        service.ingest(results_bundle(tmp_path / "us-1", [SSO[2]]), run_id=run_id, shard="us-1")
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "PASS")

    def test_a_dispatch_that_is_still_executing_is_not_what_expires(self, scripted):
        release = threading.Event()

        def script(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            assert release.wait(30)
            ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        service = scripted(script, **EXPIRY)
        run_id = compose(service, "Auth-SSO", runner_id="staged")
        outcome = service.dispatch(run_id, "staged")
        wait_until(lambda: service.require_run(run_id).state == "RUNNING", what="the hand-off")
        assert service.expire_awaiting(now_ms=minutes_from_now(600)) == []
        release.set()
        assert outcome.done.wait(30)

    def test_a_served_deployment_expires_on_its_own_and_gc_does_it_once(self, scripted):
        client, service = scripted(hand_off, client=True,
                                   config={"core": {"dispatch": {"awaiting_timeout_s": 1}}})
        run_id = compose(service, "Auth-SSO", runner_id="staged")
        service.dispatch(run_id, "staged", foreground=True)
        failed = wait_until(
            lambda: (d := client.get(f"{API}/runs/{run_id}").json())["state"] == "FAILED" and d,
            timeout=30, what="the waiting dispatch to expire",
        )
        assert "expired" in failed["dispatches"][0]["error"]
        assert service.gc()["expired_dispatches"] == []

    @pytest.mark.parametrize("value", [0, -5, "soon", True])
    def test_the_timeout_is_a_positive_number_or_null(self, tmp_path, value):
        from runcomposer.sdk import load_config

        with pytest.raises(ConfigError, match=r"core\.dispatch\.awaiting_timeout_s must be a positive number"):
            load_config(str(write_config(tmp_path, core={"dispatch": {"awaiting_timeout_s": value}})))


# -- duration history ---------------------------------------------------------------------


class TestDurationHistory:
    def run_once(self, service: Any, verdicts: list[Verdict]) -> None:
        SCRIPTS["history"] = lambda ctx: (ctx.declare(["1"]), ctx.deliver("1", verdicts=verdicts))
        run_id = compose(service, "Auth-SSO", runner_id="staged")
        service.dispatch(run_id, "staged", foreground=True)

    @pytest.fixture()
    def history(self, make_service):
        service = make_service(runners={"staged": {"module": f"{HERE}:ScriptRunner", "script": "history"}})
        self.run_once(service, [Verdict(SSO[0], "PASS", 4000), Verdict(SSO[1], "FAIL", 200), Verdict(SSO[2], "PASS", 0)])
        self.run_once(service, [Verdict(SSO[0], "PASS", 6000), Verdict(SSO[1], "PASS", 9000), Verdict(SSO[2], "SKIP", 0)])
        return service

    def test_durations_can_be_restricted_to_executions_that_passed(self, history):
        readings: list[dict[str, float]] = []
        SCRIPTS["history"] = lambda ctx: (
            readings.append(ctx.history.durations()),
            readings.append(ctx.history.durations(statuses=("PASS",))),
            readings.append(ctx.history.durations(statuses=("FAIL", "ERROR"))),
            ctx.refuse("only reading"),
        )
        run_id = compose(history, "Auth-SSO", runner_id="staged")
        history.dispatch(run_id, "staged", foreground=True)
        everything, passed, failed = readings
        # A failing execution often ends early: 200 ms would halve the mean.
        assert everything == {SSO[0]: 5000.0, SSO[1]: 4600.0}
        assert passed == {SSO[0]: 5000.0, SSO[1]: 9000.0}
        assert failed == {SSO[1]: 200.0}

    def test_zero_milliseconds_means_unknown_to_the_store_and_to_the_planner(self, history):
        assert SSO[2] not in history.store.duration_aggregates()
        plan = plan_chunks(SSO, 2, {SSO[0]: 5000.0, SSO[1]: 4600.0, SSO[2]: 0.0})
        assert (plan.known, plan.total) == (2, 3), "an item reported with 0 ms has no known duration"
        assert sum(plan.estimates_ms) == 9600.0

    def test_history_import_seeds_what_no_execution_has_measured(self, history, tmp_path, capsys):
        config = str(history.config.path)
        csv_file = tmp_path / "durations.csv"
        csv_file.write_text(
            "item,duration_ms,team\n"
            f"{SSO[0]},60000,auth\n"
            f"{SSO[2]},1500,auth\n"
            "Shop.Auth.SSO.T003,2500,auth\n"
            "Shop.Retired.Checkout.T900,800,checkout\n"
            f"{SSO[1]},0,auth\n",
            encoding="utf-8",
        )
        assert cli_main(["history", "import", str(csv_file), "--json", "--config", config]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report == {"imported": 2, "unknown": ["Shop.Retired.Checkout.T900"], "without_duration": 1, "labels": {}}
        assert cli_main(["history", "durations", "--json", "--config", config]) == 0
        durations = json.loads(capsys.readouterr().out)["durations"]
        # Measured items keep their measurement; the seed answers for the rest
        # (given twice in the file: the mean).
        assert durations == {SSO[0]: 5000.0, SSO[1]: 4600.0, SSO[2]: 2000.0}
        assert cli_main(["history", "durations", "--config", config]) == 0
        out = capsys.readouterr().out
        assert f"      2000 ms  {SSO[2]}" in out and "3 item(s) with a known duration" in out

    def test_json_files_and_labels(self, make_service, tmp_path, capsys):
        service = make_service()
        config = str(service.config.path)
        (tmp_path / "d.json").write_text(json.dumps({"durations": {SSO[0]: 700, SSO[1]: 300}}), encoding="utf-8")
        (tmp_path / "list.json").write_text(
            json.dumps([{"item": SSO[2], "duration_s": 1.5}]), encoding="utf-8")
        assert cli_main(["history", "import", str(tmp_path / "d.json"), "--label", "suite=nightly", "--config", config]) == 0
        assert "imported 2 duration(s) under labels {'suite': 'nightly'}" in capsys.readouterr().out
        assert cli_main(["history", "import", str(tmp_path / "list.json"), "--config", config]) == 0
        capsys.readouterr()
        with_label = service.store.duration_aggregates(labels={"suite": "nightly"})
        assert with_label == {SSO[0]: 700.0, SSO[1]: 300.0}
        assert service.store.duration_aggregates() == {SSO[0]: 700.0, SSO[1]: 300.0, SSO[2]: 1500.0}
        assert service.store.duration_aggregates(labels={"suite": "adhoc"}) == {}

    @pytest.mark.parametrize(
        ("name", "content", "message"),
        [
            ("d.csv", "test,ms\nShop.Auth.SSO.T001,5\n", "the first row must name the columns"),
            ("d.csv", "item,duration_ms\nShop.Auth.SSO.T001,soon\n", "line 2: the duration is not a number"),
            ("d.json", "{not json", "not valid JSON"),
            ("d.json", '{"durations": {"Shop.Auth.SSO.T001": -1}}', "a duration cannot be negative"),
            ("d.json", '"just a string"', "expected"),
        ],
    )
    def test_a_file_that_cannot_be_read_as_durations_says_where(self, make_service, tmp_path, capsys, name, content, message):
        service = make_service()
        (tmp_path / name).write_text(content, encoding="utf-8")
        assert cli_main(["history", "import", str(tmp_path / name), "--config", str(service.config.path)]) == 1
        assert message in capsys.readouterr().err
        assert service.store.duration_aggregates() == {}

    def test_only_unknown_names_is_exit_code_1(self, make_service, tmp_path, capsys):
        service = make_service()
        (tmp_path / "d.csv").write_text("item,duration_ms\nShop.Retired.T1,5\n", encoding="utf-8")
        assert cli_main(["history", "import", str(tmp_path / "d.csv"), "--config", str(service.config.path)]) == 1
        assert "not in the catalog, skipped: Shop.Retired.T1" in capsys.readouterr().out

    def test_the_sample_file_of_the_example_world_imports(self, make_service, capsys):
        from .conftest import REPO

        service = make_service()
        sample = REPO / "examples" / "history" / "durations.csv"
        assert cli_main(["history", "import", str(sample), "--config", str(service.config.path)]) == 0
        assert "imported" in capsys.readouterr().out
        seeded = service.store.duration_aggregates()
        assert len(seeded) >= 5 and all(item.startswith("Shop.") for item in seeded)


# -- what the run detail says about a run ------------------------------------------------------


class TestRunDetailSemantics:
    def test_duration_describes_the_latest_dispatch_and_each_dispatch_has_its_own(self, scripted):
        from runcomposer.app.views import run_row

        service = scripted(lambda ctx: (ctx.declare(["1"]),
                                        ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])))
        run_id = compose(service, "Auth-SSO", runner_id="staged")
        service.dispatch(run_id, "staged", foreground=True)
        first = service.require_run(run_id).dispatches[0]
        # The same run, dispatched again much later: back-date the first dispatch.
        service.store._connection().execute(
            "UPDATE dispatches SET created_at = '2026-01-01T08:00:00Z', completed_at = '2026-01-01T08:00:42Z'"
            " WHERE dispatch_id = ?", (first.dispatch_id,))
        service.dispatch(run_id, "staged", foreground=True)
        row = run_row(service.require_run(run_id))
        old, new = row["dispatches"]
        assert old["duration_s"] == 42 and new["duration_s"] is not None and new["duration_s"] < 30
        assert row["duration_s"] == new["duration_s"], "the run took seconds, not the months between its dispatches"
        in_flight = scripted(hand_off)
        waiting = compose(in_flight, "Auth-SSO", runner_id="staged")
        in_flight.dispatch(waiting, "staged", foreground=True)
        row = run_row(in_flight.require_run(waiting))
        assert row["duration_s"] is None and row["dispatches"][0]["duration_s"] is None

    def test_after_a_cancel_a_missing_verdict_says_what_was_last_seen_live(self, scripted):
        """Nothing was delivered for a cancelled shard, so its items are
        MISSING — also the one that was seen passing. That is the truth about
        results; what was streamed is stated next to it."""
        streamed = threading.Event()

        def script(ctx: DispatchContext) -> None:
            ctx.declare({"eu-1": list(ctx.item_ids)})
            ctx.live("eu-1", ctx.item_ids[0], "PASS", duration_ms=310)
            ctx.live("eu-1", ctx.item_ids[1], "RUNNING")
            streamed.set()
            while not ctx.cancelled:
                time.sleep(0.01)
            ctx.raise_if_cancelled()

        client, service = scripted(script, client=True)
        run_id = compose(service, "Auth-SSO", runner_id="staged")
        service.dispatch(run_id, "staged")
        assert streamed.wait(30)
        service.cancel(run_id)
        assert service.wait(run_id, timeout=30).state == "CANCELLED"
        detail = client.get(f"{API}/runs/{run_id}").json()
        missing = {v["item_id"]: v for v in detail["verdicts"] if v["status"] == "MISSING"}
        assert set(missing) == set(SSO) and detail["verdict_summary"] == {"MISSING": 3}
        assert missing[SSO[0]]["last_live"] == {"status": "PASS", "duration_ms": 310, "message": "", "shard": "eu-1"}
        assert missing[SSO[1]]["last_live"]["status"] == "RUNNING"
        assert missing[SSO[2]]["last_live"] is None, "never started"
        # A rerun of what did not pass therefore takes all three — nothing was delivered.
        again = service.compose_run(
            {"history": f"failed+error+missing@run:{run_id}"}, title="What did not pass", origin="test")
        assert [item.id for item in again.items] == SSO

    def test_a_rerun_keeps_the_title_unless_given_one_and_names_its_source_as_data(self, make_client):
        client, service = make_client()
        run_id = compose(service, "Payments-Cards", runner_id="demo", title="Karten prüfen")
        service.dispatch(run_id, "demo", foreground=True)
        assert service.require_run(run_id).completion == "FAIL", "the demo seed fails some"
        default = client.post(f"{API}/runs/{run_id}/rerun", json={"scope": "failed"}).json()
        assert default["run"]["title"] == "Karten prüfen" and default["run"]["origin"] == "rerun"
        assert default["rerun_of"] == {"run_id": run_id, "scope": "failed"} and default["source_run_id"] == run_id
        titled = client.post(f"{API}/runs/{run_id}/rerun",
                             json={"scope": "failed", "title": "Karten, zweiter Versuch"}).json()
        assert titled["run"]["title"] == "Karten, zweiter Versuch"
        service.wait(titled["run"]["id"], timeout=30)
        assert client.get(f"{API}/runs/{titled['run']['id']}").json()["rerun_of"] == {"run_id": run_id, "scope": "failed"}
        assert client.get(f"{API}/runs/{run_id}").json()["rerun_of"] is None
        same = client.post(f"{API}/runs/{run_id}/rerun", json={"scope": "all"}).json()
        assert same["run"]["id"] == run_id and same["rerun_of"] == {"run_id": run_id, "scope": "all"}

    def test_attaching_a_delivery_that_holds_nothing_of_the_run_changes_nothing(self, make_client, tmp_path):
        client, service = make_client()
        run_id = compose(service, "Auth-SSO")
        stray = results_bundle(tmp_path / "stray", ["Shop.Cart.Core.T001", "Shop.Cart.Core.T002"])
        entry = service.ingest_or_quarantine(stray, transport="file-drop").entry_id
        before = kinds_of(service, run_id)
        response = client.post(f"{API}/quarantine/{entry}/attach", json={"run_id": run_id})
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "nothing-planned"
        assert "none of the 2 result(s) in this delivery is for an item run" in body["detail"]
        run = service.require_run(run_id)
        assert (run.state, run.completion, run.dispatches) == ("COMPOSED", None, ())
        assert kinds_of(service, run_id) == before
        assert [e.entry_id for e in service.quarantine.entries()] == [entry], "still there to bind elsewhere"
        # A delivery that holds something of the run is attached as before —
        # what it holds beyond the run is reported.
        partial = results_bundle(tmp_path / "partial", [SSO[0], "Shop.Cart.Core.T001"])
        entry = service.ingest_or_quarantine(partial, transport="file-drop").entry_id
        attached = client.post(f"{API}/quarantine/{entry}/attach", json={"run_id": run_id})
        assert attached.status_code == 200 and attached.json()["run_state"] == "COMPLETE"
        assert any("were not planned for this run" in w for w in attached.json()["warnings"])
