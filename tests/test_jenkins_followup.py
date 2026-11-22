"""More of the Jenkins side, against the protocol stand-in:

- configured callables with settings (``{call: …, with: {…}}``) in the
  ``jenkins`` runner, and ``plan_provider`` — a plan of a connector's own:
  shards with their items, groups, template fields;
- ``check_parameters`` — Jenkins drops a build parameter its job does not
  declare, silently; the health check says so beforehand
  (``JenkinsClient.job_parameters``, ``FakeJenkins.add_job(parameters=…)``);
- following a build through an outage of the server (``outage_tolerance_s``,
  ``FakeJenkins.down`` / ``up``), and a queue item cancelled in Jenkins
  (``JenkinsQueueCancelled``);
- ``push_results`` — the results push of an executor, over real HTTP;
- a served harness that an executor in a container can reach
  (``advertise_host``, ``RUNCOMPOSER_HARNESS_PORT``).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from runcomposer.core.remote import ResultsPushRefused, push_results
from runcomposer.sdk import ConfigError, DispatchCancelled, DispatchContext, PlanContext
from runcomposer.sdk.jenkins import (
    JenkinsClient,
    JenkinsError,
    JenkinsQueueCancelled,
    JenkinsTimeout,
    JobParameter,
)
from runcomposer.testing import ConnectorHarness

from .conftest import compose, wait_until
from .test_jenkins import (
    JOB,
    POLL,
    RUNNER,
    Build,
    FakeJenkins,
    closed_port,
    dispatch,
    events_of,
    jenkins_service,
)
from .test_jenkins_toolkit import CARDS, FAN_OUT, regional_job, results_json

HERE = "tests.test_jenkins_followup"


@pytest.fixture()
def jenkins():
    fake = FakeJenkins()
    fake.add_job(JOB)
    yield fake
    fake.close()


# -- callables a deployment names, with settings ---------------------------------------------

#: What ``regional_plan`` was asked, call by call: ``(dispatch id, fields)``.
ASKED: list[tuple[str, dict[str, Any]]] = []


def regional_plan(ctx: PlanContext, fields: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """A ``plan_provider``: the shop's nightly job runs one partition per
    region, and the connector decides which test goes where — round robin
    over the regions its settings name, each region on its cluster."""
    ASKED.append((str(fields["dispatch_id"]), dict(fields)))
    regions = list(settings["regions"])
    return {
        "shards": {region: list(ctx.item_ids[index::len(regions)]) for index, region in enumerate(regions)},
        "groups": {region: settings["clusters"][region] for region in regions},
        "fields": {"partitions": str(len(regions)), "pool": settings.get("pool", "")},
    }


def fields_only(ctx: PlanContext, fields: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """A ``plan_provider`` that only works out template fields."""
    return {"fields": {"pool": f"{settings['pool']}-{fields['shards'].replace(',', '+')}"}}


def verbatim(ctx: PlanContext, fields: dict[str, Any], settings: dict[str, Any]) -> Any:
    """A ``plan_provider`` returning whatever its settings say — or refusing."""
    if "refuse" in settings:
        ctx.refuse(settings["refuse"])
    return settings["answer"]


def counted(ctx: PlanContext, fields: dict[str, Any], settings: dict[str, Any]) -> str:
    """A field provider that sees what ran before it."""
    return f"{settings['label']}:{fields.get('partitions', '?')}:{fields['shards']}"


def console_results(ctx: DispatchContext, shard: str, directory: Path, settings: dict[str, Any]) -> Path:
    """A ``postprocess`` with settings: the shop's console extraction, noting
    which operator team owns the shard."""
    from tests.shop_callables import results_from_console

    ctx.log(f"shard {shard} belongs to {settings['team']}")
    return results_from_console(ctx, shard, directory)


CLUSTERS = {"eu": "cluster-west", "us": "cluster-east", "apac": "cluster-east"}
PLANNED = {"call": f"{HERE}:regional_plan", "with": {"regions": ["eu", "us"], "clusters": CLUSTERS, "pool": "staging"}}


class TestConfiguredCallables:
    def test_a_field_provider_and_a_postprocess_receive_their_settings(self, make_service, jenkins):
        jenkins.add_job(JOB, regional_job())
        options = {
            **FAN_OUT,
            "field_providers": {
                **FAN_OUT["field_providers"],
                "label": {"call": f"{HERE}:counted", "with": {"label": "nightly"}},
            },
            "parameters": {**FAN_OUT["parameters"], "LABEL": "{label}"},
            "postprocess": {"call": f"{HERE}:console_results", "with": {"team": "payments-ops"}},
        }
        service = jenkins_service(make_service, jenkins, **options)
        run_id, outcome = dispatch(service, "Payments-Cards", {"regions": ["eu", "us"]})
        assert outcome.refusal is None
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "PASS"), run.latest_dispatch.error
        (trigger,) = jenkins.triggers()
        assert trigger.form["LABEL"] == "nightly:?:eu,us"
        assert trigger.form["INCLUDE"] == "Payments-Cards"  # the plain spelling still works
        logs = [event["message"] for event in events_of(service, run_id, "log")]
        assert [line for line in logs if "belongs to" in line] == [
            "shard eu belongs to payments-ops", "shard us belongs to payments-ops",
        ]

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"field_providers": {"include": {"call": "no-colon"}}}, r"field_providers\.include: a configured callable is an import path"),
            ({"field_providers": {"include": {"call": f"{HERE}:counted", "with": ["label"]}}}, "'with' must be a mapping"),
            ({"field_providers": {"include": {"run": f"{HERE}:counted"}}}, r"field_providers\.include: a configured callable is"),
            ({"field_providers": ["include"]}, "field_providers maps field names"),
            ({"postprocess": {"call": f"{HERE}:console_results", "and": {}}}, "postprocess: a configured callable is"),
            ({"plan_provider": "no-colon"}, "plan_provider is an import path"),
            ({"plan_fields": ["partitions"]}, "plan_fields names fields that plan_provider yields"),
            ({"plan_provider": PLANNED, "plan_fields": ["token"]}, "not one of the built-in fields"),
            ({"plan_provider": PLANNED, "plan_fields": "partitions"}, "plan_fields is a list of field names"),
            ({"plan_provider": PLANNED, "plan_fields": ["label"],
              "field_providers": {"label": f"{HERE}:counted"}}, "named under plan_fields and field_providers"),
            ({"plan_provider": PLANNED, "parameters": {"X": "{partitions}"}}, r"parameters\.X uses \{partitions\}"),
            ({"outage_tolerance_s": -1}, "outage_tolerance_s is a number of seconds"),
            ({"outage_tolerance_s": "long"}, "outage_tolerance_s is a number of seconds"),
        ],
    )
    def test_misconfiguration_is_refused_at_startup(self, make_service, jenkins, options, message):
        with pytest.raises(ConfigError, match=message):
            jenkins_service(make_service, jenkins, **options).validate()

    def test_a_callable_that_cannot_be_imported_refuses_the_dispatch_naming_its_option(self, make_service, jenkins):
        service = jenkins_service(
            make_service, jenkins,
            field_providers={"label": {"call": "tests.no_such_module:counted", "with": {"label": "x"}}},
            parameters={"LABEL": "{label}"},
        )
        run_id, outcome = dispatch(service)
        assert "field_providers.label" in outcome.refusal and "no_such_module" in outcome.refusal
        assert jenkins.triggers() == [] and service.require_run(run_id).state == "COMPOSED"


# -- plan_provider ------------------------------------------------------------------------------


class TestPlanProvider:
    def options(self, **more: Any) -> dict[str, Any]:
        return {
            "plan_provider": PLANNED,
            "plan_fields": ["partitions", "pool", "window"],
            "field_providers": {"label": {"call": f"{HERE}:counted", "with": {"label": "nightly"}}},
            "parameters": {
                "REGIONS": "{shards}", "PARTITIONS": "{partitions}", "POOL": "{pool}",
                "WINDOW": "{window}", "LABEL": "{label}", "DISPATCH": "{dispatch_id}",
                "CALLBACK_URL": "{callback_url}", "TOKEN": "{token}",
            },
            **more,
        }

    def test_its_shards_items_groups_and_fields_are_what_is_declared_and_sent(self, make_service, jenkins):
        ASKED.clear()
        service = jenkins_service(make_service, jenkins, **self.options())
        run_id, outcome = dispatch(service, "Payments-Cards")
        assert outcome.refusal is None
        record = service.require_run(run_id).latest_dispatch
        assert record.state == "AWAITING_RESULTS"
        assert record.shards == ("eu", "us")
        assert {shard: list(items) for shard, items in record.shard_items.items()} == {
            "eu": CARDS[0::2], "us": CARDS[1::2],
        }
        assert dict(record.shard_groups) == {"eu": "cluster-west", "us": "cluster-east"}
        (trigger,) = jenkins.triggers()
        assert trigger.form["REGIONS"] == "eu,us"
        assert (trigger.form["PARTITIONS"], trigger.form["POOL"]) == ("2", "staging")
        assert trigger.form["WINDOW"] == "", "a declared field the provider did not yield is empty"
        assert trigger.form["LABEL"] == "nightly:2:eu,us", "field providers see the provider's fields"
        # It was asked with the built-in fields, {shards} as the option renders it.
        ((asked_for, fields),) = ASKED
        assert asked_for == outcome.dispatch_id and fields["shards"] == "1"
        assert fields["item_ids"].split("\n") == CARDS and "partitions" not in fields

    def test_the_preview_shows_the_provider_s_plan_and_sends_nothing(self, make_service, jenkins):
        ASKED.clear()
        service = jenkins_service(make_service, jenkins, **self.options())
        outcome = service.plan(RUNNER, selection={"tag_filter": "Payments-Cards"})
        preview = outcome.preview
        assert outcome.refusal is None
        assert {shard: list(items) for shard, items in preview.shards.items()} == {
            "eu": CARDS[0::2], "us": CARDS[1::2],
        }
        assert dict(preview.groups) == {"eu": "cluster-west", "us": "cluster-east"}
        assert preview.data["build_parameters"]["PARTITIONS"] == "2"
        assert not any("distributes the items" in note for note in preview.notes)
        assert [asked for asked, _ in ASKED] == ["‹at dispatch›"]
        assert jenkins.triggers() == [] and service.list_runs().total == 0

    def test_it_wins_over_the_shards_option_and_sees_what_that_rendered(self, make_service, jenkins):
        ASKED.clear()
        service = jenkins_service(
            make_service, jenkins, **self.options(**{
                "shards": "{params[regions]}", "run_parameters": FAN_OUT["run_parameters"],
            })
        )
        # Even a shard option that renders to nothing is no refusal when the
        # provider says what the shards are.
        for regions, seen in ((["apac"], "apac"), ([], "")):
            ASKED.clear()
            run_id, outcome = dispatch(service, "Payments-Cards", {"regions": regions})
            assert outcome.refusal is None
            assert service.require_run(run_id).latest_dispatch.shards == ("eu", "us")
            assert ASKED[0][1]["shards"] == seen

    def test_a_provider_that_returns_no_shards_keeps_the_configured_ones(self, make_service, jenkins):
        service = jenkins_service(
            make_service, jenkins, shards=["eu", "us"],
            plan_provider={"call": f"{HERE}:fields_only", "with": {"pool": "night"}}, plan_fields=["pool"],
            parameters={"REGIONS": "{shards}", "POOL": "{pool}", "TOKEN": "{token}", "CALLBACK_URL": "{callback_url}"},
        )
        run_id, outcome = dispatch(service)
        assert outcome.refusal is None
        record = service.require_run(run_id).latest_dispatch
        assert record.shards == ("eu", "us") and dict(record.shard_items) == {}
        assert jenkins.triggers()[0].form["POOL"] == "night-eu+us"
        preview = service.plan(RUNNER, run_id=run_id).preview
        assert dict(preview.shards) == {"eu": [], "us": []}
        assert any("distributes the items" in note for note in preview.notes)

    def test_polled_results_are_told_apart_by_the_provider_s_shards(self, make_service, jenkins):
        """End to end with completion: poll — the job distributes the items
        the way the provider planned them, archives them per region."""
        jenkins.add_job(JOB, regional_job(archive=True))
        options = {
            **POLL,
            "format": "runcomposer-verdicts",
            "artifact_paths": ["results/{shard}/*.json"],
            "plan_provider": PLANNED,
            "parameters": {"SPEC_B64": "{spec_b64}", "REGIONS": "{shards}"},
        }
        service = jenkins_service(make_service, jenkins, **options)
        run_id, outcome = dispatch(service, "Payments-Cards")
        assert outcome.refusal is None
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "PASS"), run.latest_dispatch.error
        by_shard: dict[str, list[str]] = {}
        for verdict in service.store.verdicts_for(run_id):
            by_shard.setdefault(verdict.shard, []).append(verdict.item_id)
        assert by_shard == {"eu": CARDS[0::2], "us": CARDS[1::2]}
        assert dict(run.latest_dispatch.shard_groups) == {"eu": "cluster-west", "us": "cluster-east"}

    @pytest.mark.parametrize(
        ("answer", "complaint"),
        [
            (["eu", "us"], "list, not a mapping"),
            ({"shard": {"eu": []}}, "the key(s) ['shard']"),
            ({"shards": {}}, "not a non-empty mapping"),
            ({"shards": {"eu": ["Shop.Cart.T001"]}}, "item(s) ['Shop.Cart.T001'] for shard 'eu' that this run does not plan"),
            ({"shards": {"eu,us": []}}, "the shard name 'eu,us'"),
            ({"shards": {"eu": CARDS[0]}}, "for shard 'eu', not a list of item ids"),
            ({"groups": {"eu": "cluster-west"}}, "groups without shards"),
            ({"shards": {"eu": []}, "groups": {"us": "cluster-east"}}, "groups for shard(s) ['us'] it did not return"),
            ({"fields": {"token": "forged"}}, "the field 'token'"),
            ({"fields": {"partitions": 3}}, "int for the field 'partitions', not text"),
        ],
    )
    def test_what_it_returns_is_checked_before_the_trigger(self, make_service, jenkins, answer, complaint):
        service = jenkins_service(
            make_service, jenkins, plan_provider={"call": f"{HERE}:verbatim", "with": {"answer": answer}},
        )
        run_id, outcome = dispatch(service, "Payments-Cards")
        assert f"plan_provider {HERE}:verbatim returned" in outcome.refusal
        assert complaint in outcome.refusal
        assert jenkins.triggers() == [] and service.require_run(run_id).state == "COMPOSED"
        planned = service.plan(RUNNER, run_id=run_id)
        assert planned.preview is None and complaint in planned.refusal

    def test_it_may_refuse(self, make_service, jenkins):
        service = jenkins_service(
            make_service, jenkins,
            plan_provider={"call": f"{HERE}:verbatim", "with": {"refuse": "no partition is free tonight"}},
        )
        run_id, outcome = dispatch(service)
        assert outcome.refusal == "no partition is free tonight"
        assert jenkins.triggers() == [] and service.plan(RUNNER, run_id=run_id).refusal == outcome.refusal


# -- the job's declared parameters ----------------------------------------------------------------


class TestDeclaredParameters:
    def test_the_client_reads_what_the_job_declares(self, jenkins):
        jenkins.add_job("shop-nightly", parameters=["SPEC_B64", "REGIONS"])
        client = JenkinsClient(jenkins.url, retry_backoff_s=0.01)
        assert client.job_parameters("shop-nightly") == ["SPEC_B64", "REGIONS"]
        assert "parameterDefinitions" in jenkins.gets("/job/shop-nightly/api/json")[-1].query["tree"]
        assert client.job_parameters(JOB) is None, "not parameterized"
        with pytest.raises(JenkinsError) as missing:
            client.job_parameters("shop-renamed-away")
        assert missing.value.status == 404

    def test_older_servers_keep_the_definitions_in_an_action(self):
        class Older(JenkinsClient):
            def get_json(self, url: str, *, retry: bool = True) -> dict[str, Any]:
                return {"actions": [{}, {"parameterDefinitions": [{"name": "SPEC_B64"}, {"name": "REGIONS"}]}]}

        assert Older("http://ci.shop.example").job_parameters(JOB) == ["SPEC_B64", "REGIONS"]

    def test_the_definitions_carry_kind_choices_and_default(self, jenkins):
        jenkins.add_job("shop-nightly", parameters=[
            "SPEC_B64",
            {"name": "SPEED", "kind": "choice", "choices": ["normal", "fast"], "description": "how fast"},
            {"name": "REGIONS", "kind": "text", "default": "eu"},
            {"name": "INGEST_TOKEN", "kind": "password", "default": "never-shown"},
            {"name": "DRY_RUN", "kind": "boolean", "default": False},
        ])
        client = JenkinsClient(jenkins.url, retry_backoff_s=0.01)
        definitions = client.job_parameter_definitions("shop-nightly")
        assert definitions == [
            JobParameter("SPEC_B64", "string"),
            JobParameter("SPEED", "choice", ("normal", "fast"), "normal", "how fast"),
            JobParameter("REGIONS", "text", default="eu"),
            JobParameter("INGEST_TOKEN", "password"),
            JobParameter("DRY_RUN", "boolean", default=False),
        ]
        speed = definitions[1]
        assert speed.accepts("fast") and not speed.accepts("faster")
        assert definitions[0].accepts("anything at all")
        # The names, as before, from the same answer.
        assert client.job_parameters("shop-nightly") == ["SPEC_B64", "SPEED", "REGIONS", "INGEST_TOKEN", "DRY_RUN"]
        assert client.job_parameter_definitions(JOB) is None, "not parameterized"
        with pytest.raises(JenkinsError) as missing:
            client.job_parameter_definitions("shop-renamed-away")
        assert missing.value.status == 404

    def test_a_value_the_choice_does_not_list_starts_no_build(self, jenkins):
        """What Jenkins 2.541 does (``ci/jenkins/e2e.py``, step 3b): an error
        answer, nothing queued."""
        jenkins.add_job("shop-nightly", parameters=[
            {"name": "PACE", "kind": "choice", "choices": ["normal", "brisk"]}, "REGIONS",
        ])
        client = JenkinsClient(jenkins.url, retry_backoff_s=0.01)
        with pytest.raises(JenkinsError) as refused:
            client.trigger("shop-nightly", {"PACE": "warp", "REGIONS": "eu"})
        assert refused.value.status == 500
        assert jenkins.queue == {} and jenkins.builds == {}
        client.trigger("shop-nightly", {"PACE": "brisk", "REGIONS": "eu"})
        wait_until(lambda: ("shop-nightly", 1) in jenkins.builds, what="the build")
        assert jenkins.builds[("shop-nightly", 1)].params == {"PACE": "brisk", "REGIONS": "eu"}

    def test_definitions_are_read_as_a_real_server_shows_them(self):
        """The fields a job's ``api/json`` carries for a definition — ``type``,
        ``choices``, ``defaultParameterValue`` — read the way
        ``ci/jenkins/e2e.py`` (step 3b) reads them from a real server."""

        class Real(JenkinsClient):
            def get_json(self, url: str, *, retry: bool = True) -> dict[str, Any]:
                return {"actions": [{}], "property": [{
                    "_class": "hudson.model.ParametersDefinitionProperty",
                    "parameterDefinitions": [
                        {"_class": "hudson.model.ChoiceParameterDefinition", "name": "SPEED",
                         "type": "ChoiceParameterDefinition", "description": "",
                         "choices": ["normal", "fast"],
                         "defaultParameterValue": {"_class": "hudson.model.StringParameterValue", "value": "normal"}},
                        {"_class": "hudson.model.PasswordParameterDefinition", "name": "INGEST_TOKEN",
                         "type": "PasswordParameterDefinition", "description": "ingest token of this dispatch",
                         "defaultParameterValue": {"_class": "hudson.model.PasswordParameterValue"}},
                    ]}]}

        assert Real("http://ci.shop.example").job_parameter_definitions(JOB) == [
            JobParameter("SPEED", "choice", ("normal", "fast"), "normal"),
            JobParameter("INGEST_TOKEN", "password", description="ingest token of this dispatch"),
        ]

    def test_a_definition_the_stand_in_cannot_serve_is_refused_where_it_is_given(self, jenkins):
        with pytest.raises(TypeError, match="a choice parameter lists its choices"):
            jenkins.add_job("shop-nightly", parameters=[{"name": "SPEED", "kind": "choice"}])
        with pytest.raises(TypeError, match=r"unknown key\(s\) \['choise'\]"):
            jenkins.add_job("shop-nightly", parameters=[{"name": "SPEED", "choise": ["a"]}])

    def test_the_stand_in_drops_what_the_job_does_not_declare(self, jenkins):
        jenkins.add_job("shop-nightly", parameters=["SPEC_B64", "REGIONS"])
        client = JenkinsClient(jenkins.url, retry_backoff_s=0.01)
        queue_url = client.trigger("shop-nightly", {"SPEC_B64": "H4s", "REGIONS": "eu", "REGOINS": "us"})
        build_url = client.wait_for_build("shop-nightly", queue_url, timeout_s=5, poll_interval_s=0.01)
        assert jenkins.builds[("shop-nightly", 1)].params == {"SPEC_B64": "H4s", "REGIONS": "eu"}
        assert jenkins.ignored_parameters == [("shop-nightly", "REGOINS")]
        assert jenkins.triggers()[0].form["REGOINS"] == "us", "it was sent all the same"
        assert build_url.endswith("/job/shop-nightly/1")
        # A job that is not parameterized takes every field, as before.
        client.trigger(JOB, {"ANYTHING": "goes"})
        wait_until(lambda: (JOB, 1) in jenkins.builds, what="the build")
        assert jenkins.builds[(JOB, 1)].params == {"ANYTHING": "goes"}
        assert jenkins.ignored_parameters == [("shop-nightly", "REGOINS")]

    def check_row(self, health: Any) -> dict[str, Any]:
        (row,) = [check for check in health.data["checks"] if check["check"] == "job parameters"]
        return dict(row)

    def test_a_health_check_with_every_name_declared_is_ok(self, make_service, jenkins):
        jenkins.add_job(JOB, parameters=["SPEC_JSON", "DISPATCH_ID", "CALLBACK_URL", "INGEST_TOKEN", "EXTRA"])
        health = jenkins_service(make_service, jenkins, check_parameters=True).runner_health(RUNNER, refresh=True)
        assert health.status == "ok"
        assert self.check_row(health) == {
            "check": "job parameters", "ok": True, "detail": "the job declares all 4 configured parameter(s)",
        }

    def test_a_name_the_job_does_not_declare_degrades_the_runner(self, make_service, jenkins):
        jenkins.add_job(JOB, parameters=["SPEC_JSON", "DISPATCH_ID"])
        service = jenkins_service(make_service, jenkins, check_parameters=True)
        health = service.runner_health(RUNNER, refresh=True)
        row = self.check_row(health)
        assert health.status == "degraded" and row["ok"] is False
        assert "does not declare CALLBACK_URL, INGEST_TOKEN" in row["detail"]
        assert "ignore them silently" in row["detail"] and row["detail"] in health.detail
        # …which is what a dispatch would have run into, without a word.
        _run_id, outcome = dispatch(service)
        assert outcome.refusal is None
        assert sorted(jenkins.ignored_parameters) == [(JOB, "CALLBACK_URL"), (JOB, "INGEST_TOKEN")]
        assert set(jenkins.builds[(JOB, 1)].params) == {"SPEC_JSON", "DISPATCH_ID"}

    def test_a_job_that_declares_no_parameters_is_not_ok_either(self, make_service, jenkins):
        health = jenkins_service(make_service, jenkins, check_parameters=True).runner_health(RUNNER, refresh=True)
        row = self.check_row(health)
        assert health.status == "degraded" and row["ok"] is False
        assert "the job declares no parameters — every configured one would be ignored" in row["detail"]

    def test_a_job_that_is_not_readable_is_not_checked(self, make_service, jenkins):
        service = jenkins_service(make_service, jenkins, job="shop-renamed-away", check_parameters=True)
        health = service.runner_health(RUNNER, refresh=True)
        assert health.status == "degraded" and "not readable" in health.detail
        assert self.check_row(health) == {
            "check": "job parameters", "ok": False, "detail": "not checked — the job is not readable",
        }


# -- following through an outage --------------------------------------------------------------------


class Follower:
    """``client.follow`` on a thread of its own, so that the test can take the
    server away and bring it back meanwhile."""

    def __init__(self, client: JenkinsClient, queue_url: str, **options: Any):
        self.builds: list[str] = []
        self.on_build = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: BaseException | None = None
        self.ended_at = 0.0

        def started(url: str) -> None:
            self.builds.append(url)
            self.on_build.set()

        def run() -> None:
            try:
                self.result = client.follow(JOB, queue_url, on_build=started, **options)
            except BaseException as exc:
                self.error = exc
            self.ended_at = time.monotonic()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def join(self) -> Follower:
        self.thread.join(30)
        assert not self.thread.is_alive(), "follow did not return"
        return self


def requests_while_down(fake: FakeJenkins, count: int = 3) -> None:
    """Wait until the stand-in has turned away ``count`` more requests."""
    mark = len(fake.seen)
    wait_until(lambda: len(fake.seen) >= mark + count, timeout=10, what=f"{count} requests during the outage")


class TestFollowingThroughAnOutage:
    def test_an_outage_shorter_than_the_tolerance_is_ridden_out_in_either_wait(self, jenkins):
        """Down while the build is queued (connections dropped), down again
        while it runs (503): followed to its end, ``on_build`` told once. The
        client's own retries would wait 30 s and more between attempts — a
        tolerant follow makes one attempt per probe, so it never does."""
        release = threading.Event()
        jenkins.add_job(JOB, lambda build: release.wait(30))
        jenkins.auto_start = False
        client = JenkinsClient(jenkins.url, retries=3, retry_backoff_s=30, request_timeout_s=5)
        follower = Follower(
            client, client.trigger(JOB, {}), timeout_s=30, poll_interval_s=0.02, outage_tolerance_s=10,
        )
        wait_until(lambda: jenkins.gets("/queue/item/1/api/json"), what="the queue item to be polled")
        jenkins.down(status=None)
        requests_while_down(jenkins)
        jenkins.up()
        jenkins.start(1)
        assert follower.on_build.wait(10)
        jenkins.down(status=503)
        requests_while_down(jenkins)
        jenkins.up()
        release.set()
        follower.join()
        assert follower.error is None, follower.error
        assert follower.result["result"] == "SUCCESS"
        assert follower.builds == [f"{jenkins.url}/job/{JOB}/1"]

    def test_on_outage_is_told_when_the_server_goes_away_and_when_it_is_back(self, jenkins):
        """One call per change, across both waits: away while queued, back,
        away while it runs, back — four calls, each with how long nothing
        answered (or since the last answer)."""
        release = threading.Event()
        jenkins.add_job(JOB, lambda build: release.wait(30))
        jenkins.auto_start = False
        told: list[tuple[bool, float]] = []
        client = JenkinsClient(jenkins.url, retries=0, request_timeout_s=5)
        follower = Follower(
            client, client.trigger(JOB, {}), timeout_s=30, poll_interval_s=0.02, outage_tolerance_s=10,
            on_outage=lambda answering, seconds: told.append((answering, seconds)),
        )
        wait_until(lambda: jenkins.gets("/queue/item/1/api/json"), what="the queue item to be polled")
        jenkins.down(status=None)
        requests_while_down(jenkins)
        wait_until(lambda: len(told) == 1, what="the outage to be told")
        assert told[0][0] is False and told[0][1] >= 0
        jenkins.up()
        jenkins.start(1)
        assert follower.on_build.wait(10)
        assert [answering for answering, _s in told] == [False, True]
        jenkins.down(status=503)
        requests_while_down(jenkins)
        jenkins.up()
        release.set()
        follower.join()
        assert follower.error is None, follower.error
        assert [answering for answering, _s in told] == [False, True, False, True]
        assert all(seconds >= 0 for _a, seconds in told)

    def test_without_an_outage_on_outage_is_never_called(self, jenkins):
        jenkins.add_job(JOB)
        told: list[tuple[bool, float]] = []
        client = JenkinsClient(jenkins.url, retries=0)
        Follower(client, client.trigger(JOB, {}), timeout_s=30, poll_interval_s=0.02, outage_tolerance_s=10,
                 on_outage=lambda answering, seconds: told.append((answering, seconds))).join()
        assert told == []

    def test_errors_with_answers_in_between_never_add_up(self, jenkins):
        """Twelve failed probes, each 0.05 s after the last answer — 0.6 s of
        failures in all, three times the tolerance, but never 0.2 s without
        an answer. What counts is the time since the server last answered."""
        release = threading.Event()
        jenkins.add_job(JOB, lambda build: release.wait(30))
        client = JenkinsClient(jenkins.url, retries=0, request_timeout_s=5)
        follower = Follower(
            client, client.trigger(JOB, {}), timeout_s=30, poll_interval_s=0.05, outage_tolerance_s=0.2,
        )
        assert follower.on_build.wait(10)
        probe = f"/job/{JOB}/1/api/json"
        for _ in range(12):
            jenkins.faults.append(("GET", probe, 503))
            wait_until(lambda: not jenkins.faults, timeout=10, what="the fault to be answered")
            answered = len(jenkins.gets(probe))
            wait_until(
                lambda seen=answered: len(jenkins.gets(probe)) > seen, timeout=10, what="an answer after it"
            )
        release.set()
        follower.join()
        assert follower.error is None, follower.error
        assert follower.result["result"] == "SUCCESS"

    @pytest.mark.parametrize("status", [503, None], ids=["503", "dropped"])
    def test_an_outage_longer_than_the_tolerance_raises_and_says_for_how_long(self, jenkins, status):
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        client = JenkinsClient(jenkins.url, retries=3, retry_backoff_s=30, request_timeout_s=5)
        follower = Follower(
            client, client.trigger(JOB, {}), timeout_s=30, poll_interval_s=0.02, outage_tolerance_s=0.3,
        )
        assert follower.on_build.wait(10)
        # The tolerance counts from the server's last answer. That answer is
        # no earlier than this moment: the follower probes one at a time, so
        # once two more probes have arrived, the first of them was answered
        # after it — and only then is the server taken away.
        probe = f"/job/{JOB}/1/api/json"
        answering = time.monotonic()
        seen = len(jenkins.gets(probe))
        wait_until(lambda: len(jenkins.gets(probe)) > seen + 1, timeout=10, what="two more probes")
        jenkins.down(status=status)
        follower.join()
        assert isinstance(follower.error, JenkinsError) and not isinstance(follower.error, JenkinsTimeout)
        assert follower.error.status == status
        assert "has not answered for" in str(follower.error) and "outage_tolerance_s=0.3" in str(follower.error)
        assert follower.ended_at - answering >= 0.3
        assert follower.builds == [f"{jenkins.url}/job/{JOB}/1"]

    def test_without_a_tolerance_an_outage_fails_as_before(self, jenkins):
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        client = JenkinsClient(jenkins.url, retries=1, retry_backoff_s=0.01, request_timeout_s=5)
        follower = Follower(client, client.trigger(JOB, {}), timeout_s=30, poll_interval_s=0.02)
        assert follower.on_build.wait(10)
        jenkins.down(status=503)
        follower.join()
        assert isinstance(follower.error, JenkinsError) and "after 2 attempt(s)" in str(follower.error)

    def test_a_queue_item_cancelled_in_jenkins_is_final_also_with_a_tolerance(self, jenkins):
        jenkins.auto_start = False
        client = JenkinsClient(jenkins.url, retry_backoff_s=0.01)
        queue_url = client.trigger(JOB, {})
        jenkins.queue[1].cancelled = True  # somebody cancelled it in Jenkins
        with pytest.raises(JenkinsQueueCancelled) as cancelled:
            client.follow(JOB, queue_url, timeout_s=30, poll_interval_s=0.02, outage_tolerance_s=30)
        assert cancelled.value.queue_url == queue_url and cancelled.value.status is None
        assert len(jenkins.gets("/queue/item/1/api/json")) == 1, "raised at the first answer"
        assert isinstance(cancelled.value, JenkinsError)

    def test_the_tolerance_does_not_extend_the_timeout(self, jenkins):
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        client = JenkinsClient(jenkins.url, request_timeout_s=5)
        follower = Follower(
            client, client.trigger(JOB, {}), timeout_s=0.5, poll_interval_s=0.02, outage_tolerance_s=30,
        )
        assert follower.on_build.wait(10)
        jenkins.down(status=503)
        follower.join()
        assert isinstance(follower.error, JenkinsTimeout) and follower.error.what == "finish"

    def test_an_outage_does_not_start_the_timeout_again(self, jenkins):
        """Down for most of the 1 s the build has to finish in, then up: the
        timeout still runs out 1 s after the wait began, not 1 s after the
        server came back."""
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        client = JenkinsClient(jenkins.url, request_timeout_s=5)
        follower = Follower(
            client, client.trigger(JOB, {}), timeout_s=1.0, poll_interval_s=0.02, outage_tolerance_s=30,
        )
        assert follower.on_build.wait(10)
        started = time.monotonic()
        jenkins.down(status=503)
        requests_while_down(jenkins)
        wait_until(lambda: time.monotonic() - started >= 0.8, what="most of the timeout to pass")
        jenkins.up()
        follower.join()
        assert isinstance(follower.error, JenkinsTimeout) and follower.error.what == "finish"
        assert follower.ended_at - started < 1.7, "a restarted timeout would have run until 1.8 s"

    def test_a_cancel_during_the_outage_is_noticed(self, jenkins):
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        client = JenkinsClient(jenkins.url, request_timeout_s=5)
        cancelled = threading.Event()

        def check() -> None:
            if cancelled.is_set():
                raise DispatchCancelled("cancelled on request")

        follower = Follower(
            client, client.trigger(JOB, {}), timeout_s=30, poll_interval_s=0.02, outage_tolerance_s=30,
            check=check,
        )
        assert follower.on_build.wait(10)
        jenkins.down(status=None)
        requests_while_down(jenkins)
        cancelled.set()
        follower.join()
        assert isinstance(follower.error, DispatchCancelled)


class TestTheRunnerThroughAnOutage:
    def test_a_polled_build_survives_a_restart_within_outage_tolerance_s(self, make_service, jenkins):
        from .test_jenkins import junit_job

        release = threading.Event()

        def job(build: Build) -> None:
            release.wait(30)
            junit_job()(build)

        jenkins.add_job(JOB, job)
        service = jenkins_service(make_service, jenkins, **POLL, outage_tolerance_s=10, retry_backoff_s=30)
        run_id = compose(service, "Payments-Cards", runner_id=RUNNER)
        outcome = service.dispatch(run_id, RUNNER)
        wait_until(lambda: "build" in service.store.get_dispatch(outcome.dispatch_id).links, what="the build link")
        jenkins.down(status=None)
        requests_while_down(jenkins)
        jenkins.up()
        release.set()
        run = service.wait(run_id, timeout=30)
        assert (run.state, run.completion) == ("COMPLETE", "PASS"), run.latest_dispatch.error
        # The run's log says that Jenkins was away, and when it was back.
        lines = [str(e.data.get("message")) for e in service.store.events(run_id) if e.kind in ("log", "warning")]
        away = [line for line in lines if "the server does not answer" in line]
        back = [line for line in lines if "the server answers again after" in line]
        assert len(away) == 1 and "outage_tolerance_s=10s" in away[0]
        assert len(back) == 1

    def test_a_queue_item_cancelled_in_jenkins_fails_the_dispatch_saying_so(self, make_service, jenkins):
        jenkins.auto_start = False
        service = jenkins_service(make_service, jenkins, **POLL, outage_tolerance_s=30)
        run_id = compose(service, "Payments-Cards", runner_id=RUNNER)
        service.dispatch(run_id, RUNNER)
        wait_until(lambda: jenkins.gets("/queue/item/1/api/json"), what="the queue to be polled")
        jenkins.queue[1].cancelled = True
        run = service.wait(run_id, timeout=30)
        assert run.state == "FAILED"
        assert "the queued Jenkins build was cancelled before it started" in run.latest_dispatch.error


# -- the results push --------------------------------------------------------------------------------


@pytest.fixture()
def awaiting(jenkins, tmp_path):
    """A served deployment with a dispatch awaiting results by callback: the
    ``(harness, build parameters of the trigger, planned item ids)``."""
    runner = {
        "plugin": "jenkins", "base_url": jenkins.url, "job": JOB,
        "callback_base": "${env:RUNCOMPOSER_HARNESS_URL}", "poll_interval_s": 0.01,
    }
    with ConnectorHarness(runners={RUNNER: runner}, serve=True) as harness:
        result = harness.run("Payments-Cards", runner=RUNNER)
        assert result.state == "AWAITING_RESULTS", result.refusal
        form = jenkins.triggers()[0].form
        planned = json.loads(form["SPEC_JSON"])["selection"]["materialized"]["item_ids"]
        yield harness, form, planned


class TestPushResults:
    def test_a_bundle_is_pushed_and_the_run_completes(self, awaiting, tmp_path):
        harness, form, planned = awaiting
        bundle = tmp_path / "results"
        bundle.mkdir()
        (bundle / "results.json").write_text(results_json(planned, failing={planned[0]}), encoding="utf-8")
        answer = push_results(
            form["CALLBACK_URL"], form["INGEST_TOKEN"], bundle, dispatch_id=form["DISPATCH_ID"],
            format="runcomposer-verdicts",
        )
        assert answer["outcome"] == "new" and answer["verdict_count"] == len(planned)
        assert answer["dispatch_id"] == form["DISPATCH_ID"]
        run = harness.wait(json.loads(form["SPEC_JSON"])["run"]["id"], timeout=30)
        assert (run.state, run.completion) == ("COMPLETE", "FAIL")
        # Any URL of the run's resource will do, and a list of files too.
        again = push_results(
            form["CALLBACK_URL"].removesuffix("/results"), form["INGEST_TOKEN"], [bundle / "results.json"],
            shard="1",
        )
        assert again["outcome"] == "duplicate"

    def test_a_refusal_raises_with_the_status_and_the_server_s_answer(self, awaiting, tmp_path):
        harness, form, planned = awaiting
        bundle = tmp_path / "results.json"
        bundle.write_text(results_json(planned), encoding="utf-8")
        with pytest.raises(ResultsPushRefused) as refused:
            push_results(form["CALLBACK_URL"], "rct_wrong", bundle)
        assert refused.value.status == 403 and refused.value.body["code"] == "token-rejected"
        assert "was refused (403)" in str(refused.value)
        with pytest.raises(ResultsPushRefused) as unauthorized:
            push_results(form["CALLBACK_URL"], None, bundle)
        assert unauthorized.value.status == 401
        assert harness.read(json.loads(form["SPEC_JSON"])["run"]["id"]).state == "AWAITING_RESULTS"

    def test_a_server_that_cannot_be_reached_is_an_error_too(self, tmp_path):
        bundle = tmp_path / "results.json"
        bundle.write_text(results_json(CARDS), encoding="utf-8")
        with pytest.raises(ResultsPushRefused) as unreachable:
            push_results(f"http://127.0.0.1:{closed_port()}/api/v1/runs/r/results", "rct_x", bundle, timeout_s=5)
        assert unreachable.value.status is None and unreachable.value.body is None
        assert "could not be pushed" in str(unreachable.value)

    def test_what_cannot_be_a_bundle_is_refused_before_anything_is_sent(self, tmp_path):
        url = f"http://127.0.0.1:{closed_port()}/api/v1/runs/r/results"
        with pytest.raises(FileNotFoundError):
            push_results(url, "rct_x", tmp_path / "missing")
        (tmp_path / "empty").mkdir()
        with pytest.raises(ValueError, match="nothing to push"):
            push_results(url, "rct_x", tmp_path / "empty")
        for region in ("eu", "us"):
            (tmp_path / region).mkdir()
            (tmp_path / region / "results.json").write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError, match=r"results\.json"):
            push_results(url, "rct_x", [tmp_path / "eu", tmp_path / "us"])


# -- a harness an executor in a container can reach ----------------------------------------------------


class TestHarnessAddress:
    def test_the_url_and_the_port_are_set_while_open_and_restored_after(self, monkeypatch):
        monkeypatch.setenv("RUNCOMPOSER_HARNESS_PORT", "left-over")
        monkeypatch.delenv("RUNCOMPOSER_HARNESS_URL", raising=False)
        with ConnectorHarness(serve=True) as harness:
            assert harness.port and harness.url == harness.local_url == f"http://127.0.0.1:{harness.port}"
            assert os.environ["RUNCOMPOSER_HARNESS_URL"] == harness.url
            assert os.environ["RUNCOMPOSER_HARNESS_PORT"] == str(harness.port)
        assert os.environ["RUNCOMPOSER_HARNESS_PORT"] == "left-over"
        assert "RUNCOMPOSER_HARNESS_URL" not in os.environ

    def test_an_entry_puts_a_host_of_its_own_in_front_of_the_port(self, jenkins, monkeypatch):
        monkeypatch.delenv("SHOP_CI_GATEWAY", raising=False)
        runner = {
            "plugin": "jenkins", "base_url": jenkins.url, "job": JOB,
            "callback_base": "http://${env:SHOP_CI_GATEWAY:-127.0.0.1}:${env:RUNCOMPOSER_HARNESS_PORT}",
        }
        with ConnectorHarness(runners={RUNNER: runner}, serve=True) as harness:
            result = harness.run("Payments-Cards", runner=RUNNER)
            assert result.state == "AWAITING_RESULTS", result.refusal
            callback = jenkins.triggers()[0].form["CALLBACK_URL"]
            assert callback == f"http://127.0.0.1:{harness.port}/api/v1/runs/{result.run_id}/results"

    def test_an_advertised_host_is_the_url_and_the_local_url_still_answers(self):
        with ConnectorHarness(serve=True, host="0.0.0.0", advertise_host="host.docker.internal") as harness:
            assert harness.url == f"http://host.docker.internal:{harness.port}"
            assert os.environ["RUNCOMPOSER_HARNESS_URL"] == harness.url
            assert harness.api == f"{harness.url}/api/v1"
            assert harness.local_url == f"http://127.0.0.1:{harness.port}"
            with urllib.request.urlopen(f"{harness.local_url}/api/v1/health", timeout=10) as response:
                assert response.status == 200

    def test_an_advertised_host_needs_a_served_harness(self):
        with pytest.raises(ValueError, match="pass serve=True"):
            ConnectorHarness(advertise_host="host.docker.internal")
