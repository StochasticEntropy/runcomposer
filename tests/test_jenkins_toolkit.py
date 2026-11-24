"""``runcomposer.sdk.jenkins`` — the Jenkins toolkit — and the ``jenkins``
runner's switches built on it (DESIGN.md §6.2b), against the protocol
stand-in of ``tests/test_jenkins.py`` (``ci/jenkins/e2e.py`` runs the same
against a real Jenkins).

Three things are shown:

1. the client on its own: trigger → queue item → build, the build found by
   its queue id once the queue item is forgotten, stop and cancel, archived
   artifacts and build-relative URLs, retry, the compressed spec;
2. the runner *configured* for a job that fans out: several shards, build
   parameters worked out from what the run selected, results read from a URL below the build
   that is not an archived artifact, a transformation before parsing;
3. the same *composed* from the client by a small runner that imports
   ``runcomposer.sdk`` and ``runcomposer.sdk.jenkins`` and nothing else.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
import ssl
import threading
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from runcomposer.plugins.jenkins import JenkinsRunner
from runcomposer.sdk import (
    ConfigError,
    DispatchCancelled,
    DispatchContext,
    Parameter,
    PlanContext,
    PlanPreview,
    RunnerBase,
    RunnerInfo,
    sha256_hex,
)
from runcomposer.sdk.jenkins import (
    JenkinsClient,
    JenkinsError,
    JenkinsTimeout,
    decode_spec,
    encode_spec,
    queue_id_of,
)
from runcomposer.testing import ConnectorHarness, FakeDispatchContext, check_runner

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
    junit_for,
)

__all__ = ["jenkins"]  # the fixture


@pytest.fixture()
def jenkins():
    fake = FakeJenkins()
    fake.add_job(JOB)
    yield fake
    fake.close()


def client_for(fake: FakeJenkins, **options: Any) -> JenkinsClient:
    return JenkinsClient(fake.url, retry_backoff_s=0.01, request_timeout_s=5, **options)


CARDS = [f"Shop.Payments.Cards.T{n:03d}" for n in range(1, 7)]


# -- 1. the client -----------------------------------------------------------------------


class TestTheClient:
    def test_trigger_to_queue_item_to_build(self, jenkins):
        jenkins.auto_start = False
        client = client_for(jenkins)
        queue_url = client.trigger(JOB, {"REGION": "eu", "NOTE": "ä b&c"})
        assert queue_url == f"{jenkins.url}/queue/item/1/" and queue_id_of(queue_url) == 1
        assert jenkins.triggers()[0].form == {"REGION": "eu", "NOTE": "ä b&c"}
        assert client.build_of(queue_url) is None, "still queued"
        build = jenkins.start(1)
        assert client.build_of(queue_url) == build.url.rstrip("/")
        assert client.wait_for_build(JOB, queue_url, timeout_s=5, poll_interval_s=0.01) == build.url.rstrip("/")
        info = client.wait_until_finished(build.url, timeout_s=5, poll_interval_s=0.01)
        assert info["result"] == "SUCCESS" and info["building"] is False

    def test_a_build_is_found_by_its_queue_id_after_the_queue_item_expired(self, jenkins):
        """Jenkins remembers an item that has left its queue for some minutes
        only. The build still says which item it came from."""
        client = client_for(jenkins)
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        other = client.trigger(JOB, {"N": "somebody else's"})
        mine = client.trigger(JOB, {"N": "mine"})
        wait_until(lambda: len(jenkins.builds) == 2, what="both builds to start")
        jenkins.expire(queue_id_of(mine))
        with pytest.raises(JenkinsError) as gone:
            client.build_of(mine)
        assert gone.value.status == 404
        assert client.find_build(JOB, queue_id_of(mine)) == f"{jenkins.url}/job/{JOB}/2"
        assert client.locate_build(JOB, mine) == f"{jenkins.url}/job/{JOB}/2"
        assert client.locate_build(JOB, other) == f"{jenkins.url}/job/{JOB}/1"
        assert client.find_build(JOB, 99) is None
        # …and that is how a cancel still reaches it — mine, not the latest.
        assert client.cancel(JOB, mine) == f"stop requested for {jenkins.url}/job/{JOB}/2"
        assert jenkins.builds[(JOB, 2)].stop.is_set() and not jenkins.builds[(JOB, 1)].stop.is_set()
        client.stop_build(f"{jenkins.url}/job/{JOB}/1")

    def test_follow_takes_a_build_from_its_queue_item_to_its_end(self, jenkins):
        jenkins.add_job(JOB, lambda build: build.pages.update(consoleText=b"done"))
        client = client_for(jenkins)
        seen: list[str] = []
        build = client.follow(JOB, client.trigger(JOB, {}), timeout_s=5, poll_interval_s=0.01, on_build=seen.append)
        assert build["url"] == f"{jenkins.url}/job/{JOB}/1" == seen[0]
        assert (build["result"], build["building"]) == ("SUCCESS", False)

    def test_follow_takes_the_build_back_when_the_cancel_was_there_before_it_started(self, jenkins):
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        client = client_for(jenkins)
        queue_url = client.trigger(JOB, {})
        wait_until(lambda: (JOB, 1) in jenkins.builds, what="the build to start")

        def cancelled() -> None:
            raise DispatchCancelled("cancelled while the trigger was under way")

        with pytest.raises(DispatchCancelled):
            client.follow(JOB, queue_url, timeout_s=5, check=cancelled)
        assert jenkins.builds[(JOB, 1)].stop.is_set(), "nobody else could have stopped it"
        assert jenkins.gets(f"/job/{JOB}/1/api/json") == [], "it did not start following"

    def test_cancel_takes_a_queued_item_out_of_the_queue(self, jenkins):
        jenkins.auto_start = False
        client = client_for(jenkins)
        queue_url = client.trigger(JOB, {})
        assert client.cancel(JOB, queue_url) == "queue item 1 cancelled"
        assert jenkins.queue[1].cancelled
        with pytest.raises(JenkinsError, match="the queued Jenkins build was cancelled"):
            client.build_of(queue_url)

    def test_results_are_archived_artifacts_or_any_url_below_the_build(self, jenkins, tmp_path):
        def job(build: Build) -> None:
            build.artifacts["reports/eu/junit.xml"] = junit_for(CARDS[:1])
            build.artifacts["reports/us/junit.xml"] = junit_for(CARDS[1:2])
            build.artifacts["logs/console.txt"] = b"not a result"
            build.pages["robot/report/output.xml"] = b"<robot/>"   # published, not archived
            build.pages["consoleText"] = b"Started by runcomposer\n"

        jenkins.add_job(JOB, job)
        client = client_for(jenkins)
        build_url = client.wait_for_build(JOB, client.trigger(JOB, {}), timeout_s=5, poll_interval_s=0.01)
        client.wait_until_finished(build_url, timeout_s=5, poll_interval_s=0.01)
        assert client.artifacts(build_url) == ["logs/console.txt", "reports/eu/junit.xml", "reports/us/junit.xml"]
        assert client.artifacts(build_url, ["reports/eu/*"]) == ["reports/eu/junit.xml"]
        saved = client.download_artifact(build_url, "reports/eu/junit.xml", tmp_path / "eu" / "junit.xml")
        assert saved.read_bytes() == junit_for(CARDS[:1])
        published = client.download_build_file(build_url, "robot/report/output.xml", tmp_path / "output.xml")
        assert published.read_bytes() == b"<robot/>"
        assert client.download_build_file(build_url, "/consoleText", tmp_path / "console").read_text() == "Started by runcomposer\n"
        with pytest.raises(JenkinsError) as missing:
            client.download_build_file(build_url, "robot/report/log.html", tmp_path / "log.html")
        assert missing.value.status == 404 and not (tmp_path / "log.html").exists()

    def test_transient_errors_are_retried_and_a_wait_times_out_by_name(self, jenkins):
        jenkins.auto_start = False
        client = client_for(jenkins, retries=2)
        queue_url = client.trigger(JOB, {})
        jenkins.faults += [("GET", "/queue/item/1", 503), ("GET", "/queue/item/1", 502)]
        assert client.build_of(queue_url) is None
        assert len(jenkins.gets("/queue/item/1/api/json")) == 3
        with pytest.raises(JenkinsTimeout) as caught:
            client.wait_for_build(JOB, queue_url, timeout_s=0.05, poll_interval_s=0.01)
        assert (caught.value.what, caught.value.timeout_s) == ("start", 0.05)

        def give_up() -> None:
            raise DispatchCancelled("enough")

        with pytest.raises(DispatchCancelled):
            client.wait_for_build(JOB, queue_url, timeout_s=30, check=give_up)

    def test_posts_carry_the_crumb_of_their_session_and_basic_auth(self):
        fake = FakeJenkins(auth=("runcomposer", "t-0123456789"))
        fake.add_job(JOB)
        try:
            client = client_for(fake, user="runcomposer", api_token="t-0123456789")
            assert client.version() == FakeJenkins.VERSION
            assert client.job_exists(JOB) and not client.job_exists("no-such-job")
            assert client.trigger(JOB, {}) is not None
            (post,) = fake.triggers()
            assert post.headers[FakeJenkins.CRUMB_FIELD.lower()] and "jsessionid" in post.headers["cookie"].lower()
            with pytest.raises(JenkinsError) as refused:
                client_for(fake, user="runcomposer", api_token="wrong").version()
            assert refused.value.status == 401
        finally:
            fake.close()

    def test_job_urls_quote_every_segment_and_a_bad_base_url_is_a_value_error(self):
        client = JenkinsClient("https://ci.shop.example/")
        assert client.job_url("shop/nightly checks") == "https://ci.shop.example/job/shop/job/nightly%20checks"
        with pytest.raises(ValueError, match="base_url must start with"):
            JenkinsClient("ci.shop.example")

    def test_the_compressed_spec_restores_the_exact_bytes(self):
        document = (json.dumps({"runspec": "1.0", "title": "Zahlungen prüfen", "ids": CARDS * 200}, indent=2) + "\n").encode()
        text = encode_spec(document)
        assert text == encode_spec(document), "deterministic"
        assert text.isascii() and "\n" not in text and len(text) < len(document) / 10
        assert decode_spec(text) == document and decode_spec(f"  {text}\n") == document
        assert sha256_hex(decode_spec(text)) == sha256_hex(document)


# -- 2. the runner, configured -------------------------------------------------------------


def results_json(item_ids: list[str], failing: set[str] = frozenset()) -> str:
    return json.dumps(
        {
            "format": "runcomposer-verdicts",
            "verdicts": [
                {"name": item, "status": "FAIL" if item in failing else "PASS", "duration_ms": 40}
                for item in item_ids
            ],
        }
    )


def regional_job(failing: set[str] = frozenset(), *, archive: bool = False):
    """A job that fans out over the regions it is given: it takes the spec
    (compressed), distributes the planned tests over its regions and — as
    this shop's nightly job does — prints each region's results into its
    console log instead of archiving them (or archives them per region)."""

    def job(build: Build) -> None:
        spec = json.loads(decode_spec(build.params["SPEC_B64"]))
        item_ids = spec["selection"]["materialized"]["item_ids"]
        regions = [r for r in build.params["REGIONS"].split(",") if r]
        console = [f"Started for run {spec['run']['id']}", f"including {build.params.get('INCLUDE', '')}"]
        for index, region in enumerate(regions):
            share = item_ids[index::len(regions)]
            body = results_json(share, failing)
            if archive:
                build.artifacts[f"results/{region}/results.json"] = body.encode()
            else:
                console += [f"=== results {region} ===", body, "=== end ==="]
        build.pages["consoleText"] = "\n".join(console).encode()
        if failing:
            build.result = "UNSTABLE"

    return job


FAN_OUT = {
    "completion": "poll",
    "format": "runcomposer-verdicts",
    "run_parameters": [
        {"name": "regions", "kind": "multi_choice", "choices": ["eu", "us", "apac"], "default": ["eu", "us"]},
    ],
    "shards": "{params[regions]}",
    "field_providers": {"include": "tests.shop_callables:include_tags", "count": "tests.shop_callables:shard_count"},
    "parameters": {
        "SPEC_B64": "{spec_b64}",
        "REGIONS": "{shards}",
        "REGION_COUNT": "{count}",
        "INCLUDE": "{include}",
        "DISPATCH": "{dispatch_id}",
    },
    "result_urls": ["consoleText"],
    "postprocess": "tests.shop_callables:results_from_console",
}


class TestTheScenarioByConfiguration:
    """A Jenkins-triggered run with several shards, computed build
    parameters, results read from a build-relative URL that is not an
    archived artifact, and a transformation before parsing — configured."""

    def test_the_whole_loop(self, make_service, jenkins):
        jenkins.add_job(JOB, regional_job(failing={CARDS[2]}))
        service = jenkins_service(make_service, jenkins, **FAN_OUT)
        run_id, outcome = dispatch(service, "Payments-Cards", {"regions": ["eu", "us", "apac"]})
        assert outcome.refusal is None
        run = service.require_run(run_id)
        dispatch_record = run.latest_dispatch
        assert (run.state, run.completion) == ("COMPLETE", "FAIL"), dispatch_record.error
        assert dispatch_record.shards == ("eu", "us", "apac")
        assert dict(dispatch_record.summary) == {"PASS": 5, "FAIL": 1}
        by_shard: dict[str, list[str]] = {}
        for verdict in service.store.verdicts_for(run_id):
            by_shard.setdefault(verdict.shard, []).append(verdict.item_id)
        assert by_shard == {"eu": [CARDS[0], CARDS[3]], "us": [CARDS[1], CARDS[4]], "apac": [CARDS[2], CARDS[5]]}

        # Computed build parameters: the shard list, a field computed from
        # it, the tags of the selection as the job's INCLUDE filter.
        (trigger,) = jenkins.triggers()
        assert trigger.form["REGIONS"] == "eu,us,apac" and trigger.form["REGION_COUNT"] == "3"
        assert trigger.form["INCLUDE"] == "Payments-Cards"
        assert trigger.form["DISPATCH"] == dispatch_record.dispatch_id
        # The document arrived as the exact bytes whose hash was recorded.
        handed_out = decode_spec(trigger.form["SPEC_B64"])
        assert sha256_hex(handed_out) == dispatch_record.spec_sha256
        assert handed_out == service.dispatch_spec(run_id, dispatch_record.dispatch_id)[0]
        # Nothing was archived: the results came from below the build's URL…
        assert jenkins.gets("/artifact/") == []
        assert len(jenkins.gets(f"/job/{JOB}/1/consoleText")) == 3
        # …and were transformed before parsing (the hook said so in the log).
        logs = [event["message"] for event in events_of(service, run_id, "log")]
        assert [line for line in logs if "taken from the console log" in line] == [
            f"shard {shard}: results taken from the console log" for shard in ("eu", "us", "apac")
        ]
        # The build's URL is a link of the dispatch from the moment it is known.
        assert dict(dispatch_record.links)["build"] == f"{jenkins.url}/job/{JOB}/1"

    def test_the_plan_preview_shows_what_would_be_sent_and_sends_nothing(self, make_service, jenkins):
        service = jenkins_service(make_service, jenkins, **FAN_OUT)
        outcome = service.plan(RUNNER, selection={"tag_filter": "Payments-Cards"}, parameters={"regions": ["us"]})
        preview = outcome.preview
        assert outcome.refusal is None and list(preview.shards) == ["us"] and preview.shards["us"] == []
        sent = preview.data["build_parameters"]
        assert sent["REGIONS"] == "us" and sent["REGION_COUNT"] == "1" and sent["INCLUDE"] == "Payments-Cards"
        assert sent["DISPATCH"] == "‹at dispatch›"
        assert sent["SPEC_B64"].startswith("‹") and sent["SPEC_B64"].endswith(" bytes›")
        assert preview.data["request_bytes"] > 100 and preview.data["completion"] == "poll"
        assert "would be triggered, 1 shard(s) ['us']" in preview.text
        assert jenkins.triggers() == [] and service.list_runs().total == 0

    def test_a_provider_refuses_before_anything_is_triggered(self, make_service, jenkins):
        service = jenkins_service(make_service, jenkins, **FAN_OUT)
        run_id, outcome = dispatch(service, "Cart")
        assert outcome.refusal.startswith("the nightly job selects by tag, and Shop.Cart.")
        assert jenkins.triggers() == []
        assert service.require_run(run_id).state == "COMPOSED"
        planned = service.plan(RUNNER, run_id=run_id)
        assert planned.preview is None and planned.refusal == outcome.refusal

    def test_a_shard_whose_results_are_missing_fails_the_dispatch_and_keeps_the_rest(self, make_service, jenkins):
        def job(build: Build) -> None:
            regional_job()(build)
            text = build.pages["consoleText"].decode()
            build.pages["consoleText"] = text.replace("=== results us ===", "=== lost ===").encode()

        jenkins.add_job(JOB, job)
        service = jenkins_service(make_service, jenkins, **FAN_OUT)
        run_id, _ = dispatch(service, "Payments-Cards")
        run = service.require_run(run_id)
        assert run.state == "FAILED" and "no results block for shard 'us'" in run.latest_dispatch.error
        assert {v.shard for v in service.store.verdicts_for(run_id)} == {"eu"}


class TestShards:
    def test_static_shards_with_callback_wait_for_one_delivery_each(self, make_service, jenkins, tmp_path):
        service = jenkins_service(
            make_service, jenkins, shards=["eu", "us"],
            parameters={"REGIONS": "{shards}", "CALLBACK_URL": "{callback_url}", "TOKEN": "{token}"},
        )
        run_id, _ = dispatch(service)
        run = service.require_run(run_id)
        assert (run.state, run.latest_dispatch.shards) == ("AWAITING_RESULTS", ("eu", "us"))
        assert jenkins.triggers()[0].form["REGIONS"] == "eu,us"
        for index, shard in enumerate(("eu", "us")):
            bundle = tmp_path / shard
            bundle.mkdir()
            (bundle / "results.json").write_text(results_json(CARDS[index::2]), encoding="utf-8")
            report = service.ingest(bundle, run_id=run_id, shard=shard)
            assert report.run_state == ("AWAITING_RESULTS" if shard == "eu" else "COMPLETE")
        assert service.require_run(run_id).completion == "PASS"

    def test_archived_results_are_told_apart_by_the_shard_in_the_path(self, make_service, jenkins):
        jenkins.add_job(JOB, regional_job(archive=True))
        options = {**FAN_OUT, "artifact_paths": ["results/{shard}/*.json"]}
        del options["result_urls"], options["postprocess"]
        service = jenkins_service(make_service, jenkins, **options)
        run_id, _ = dispatch(service)
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "PASS"), run.latest_dispatch.error
        assert {a["name"]: a["shard"] for a in service.artifacts.list(run_id) if a["kind"] == "local"} == {
            "results.json (eu)": "eu", "results.json (us)": "us",
        }
        assert sorted(s.path for s in jenkins.gets("/artifact/")) == [
            f"/job/{JOB}/1/artifact/results/eu/results.json", f"/job/{JOB}/1/artifact/results/us/results.json",
        ]

    def test_a_shard_a_job_pushed_itself_is_not_downloaded_again(self, make_service, jenkins, tmp_path):
        """Poll and push together: the runner finds part of its dispatch
        delivered — by somebody else — and only fetches what is outstanding."""
        services: list[Any] = []

        def job(build: Build) -> None:
            regional_job(archive=True)(build)
            bundle = tmp_path / "pushed"
            bundle.mkdir()
            (bundle / "results.json").write_bytes(build.artifacts["results/eu/results.json"])
            services[0].ingest(bundle, run_id=json.loads(decode_spec(build.params["SPEC_B64"]))["run"]["id"], shard="eu")

        jenkins.add_job(JOB, job)
        options = {**FAN_OUT, "artifact_paths": ["results/{shard}/*.json"]}
        del options["result_urls"], options["postprocess"]
        services.append(jenkins_service(make_service, jenkins, **options))
        run_id, _ = dispatch(services[0])
        assert services[0].require_run(run_id).state == "COMPLETE"
        assert [s.path for s in jenkins.gets("/artifact/")] == [f"/job/{JOB}/1/artifact/results/us/results.json"]

    def test_several_shards_that_cannot_be_told_apart_are_refused_before_the_trigger(self, make_service, jenkins):
        service = jenkins_service(make_service, jenkins, **{**POLL, "shards": ["eu", "us"]})
        run_id, outcome = dispatch(service)
        assert "2 shards with completion: poll need a way to tell their results apart" in outcome.refusal
        assert jenkins.triggers() == [] and service.require_run(run_id).state == "COMPOSED"

    def test_a_shard_template_that_renders_to_nothing_is_refused(self, make_service, jenkins):
        service = jenkins_service(make_service, jenkins, **FAN_OUT)
        _run_id, outcome = dispatch(service, "Payments-Cards", {"regions": []})
        assert "rendered to nothing for this run" in outcome.refusal and jenkins.triggers() == []

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"shards": []}, "shards is a non-empty list of names"),
            ({"shards": ["eu", "eu"]}, "shards must be distinct"),
            ({"shards": "{shards}"}, "shards cannot be computed from \\{shards\\}"),
            ({"shards": "{nonsense}"}, "shards uses \\{nonsense\\}"),
            ({"field_providers": {"token": "tests.shop_callables:include_tags"}}, "not one of the built-in fields"),
            ({"field_providers": {"include": "no-colon"}}, "field_providers.include is an import path"),
            ({"parameters": {"X": "{include}"}}, "parameters.X uses \\{include\\}"),
            ({"postprocess": "no-colon"}, "postprocess is an import path"),
            ({"result_urls": ["https://elsewhere.example/x"]}, "result_urls entries are paths below the build's URL"),
            ({"max_parameter_bytes": 0}, "max_parameter_bytes must be a positive integer or null"),
        ],
    )
    def test_misconfiguration_is_refused_at_startup(self, make_service, jenkins, options, message):
        with pytest.raises(ConfigError, match=message):
            jenkins_service(make_service, jenkins, **options).validate()

    def test_a_provider_field_is_known_to_the_templates(self, make_service, jenkins):
        jenkins_service(
            make_service, jenkins,
            field_providers={"include": "tests.shop_callables:include_tags"}, parameters={"X": "{include}"},
        ).validate()


BIG = {"parameters": {"SPEC_JSON": "{spec_json}"}, "max_parameter_bytes": 900}


class TestTheDocumentAsABuildParameter:

    def test_a_parameter_over_the_limit_is_refused_before_the_trigger_with_the_way_out(self, make_service, jenkins):
        service = jenkins_service(make_service, jenkins, **BIG)
        run_id, outcome = dispatch(service, "Payments")
        assert "build parameter SPEC_JSON would be" in outcome.refusal
        assert "max_parameter_bytes (900)" in outcome.refusal and "{spec_b64}" in outcome.refusal
        assert jenkins.triggers() == [] and service.require_run(run_id).state == "COMPOSED"

    def test_the_same_run_fits_compressed(self, make_service, jenkins):
        service = jenkins_service(
            make_service, jenkins, parameters={"SPEC_B64": "{spec_b64}"}, max_parameter_bytes=900,
        )
        run_id, outcome = dispatch(service, "Payments")
        assert outcome.refusal is None
        (trigger,) = jenkins.triggers()
        assert len(trigger.form["SPEC_B64"]) < 900
        assert json.loads(decode_spec(trigger.form["SPEC_B64"]))["run"]["id"] == run_id

    def test_the_whole_request_has_a_limit_too_and_null_lifts_one(self, make_service, jenkins):
        service = jenkins_service(
            make_service, jenkins, parameters={"A": "{item_ids}", "B": "{item_ids}"},
            max_parameter_bytes=None, max_request_bytes=300,
        )
        _run_id, outcome = dispatch(service, "Payments")
        assert "the trigger request would be" in outcome.refusal and "max_request_bytes (300)" in outcome.refusal

    def test_spec_url_is_where_the_job_downloads_the_exact_bytes(self, make_client, jenkins):
        client, service = make_client(
            runners={RUNNER: {"base_url": jenkins.url, "job": JOB, "callback_base": "http://runcomposer.shop.example:8100",
                              "parameters": {"SPEC_URL": "{spec_url}", "DISPATCH": "{dispatch_id}"}}}
        )
        run_id = compose(service, "Payments-Cards", runner_id=RUNNER)
        service.dispatch(run_id, RUNNER, foreground=True)
        (trigger,) = jenkins.triggers()
        record = service.require_run(run_id).latest_dispatch
        assert trigger.form["SPEC_URL"] == (
            f"http://runcomposer.shop.example:8100/api/v1/runs/{run_id}/dispatches/{record.dispatch_id}/spec"
        )
        # What the job downloads there (asked of the app directly, by path).
        downloaded = client.get(trigger.form["SPEC_URL"].removeprefix("http://runcomposer.shop.example:8100"))
        assert downloaded.status_code == 200 and sha256_hex(downloaded.content) == record.spec_sha256

    def test_spec_url_needs_a_callback_base(self, make_service, jenkins):
        service = jenkins_service(
            make_service, jenkins, completion="poll", callback_base=None, parameters={"SPEC_URL": "{spec_url}"},
        )
        _run_id, outcome = dispatch(service)
        assert "{spec_url}, which needs the callback_base option" in outcome.refusal

    def test_a_mapping_parameter_renders_as_name_value_lines(self, make_service, jenkins):
        service = jenkins_service(
            make_service, jenkins,
            run_parameters=[{"name": "variables", "kind": "mapping"}, {"name": "notes", "kind": "text"}],
            parameters={"VARIABLES": "{params[variables]}", "NOTES": "{params[notes]}"},
        )
        dispatch(service, "Payments-Cards", {"variables": {"BROWSER": "firefox", "LOCALE": "de"}, "notes": "a\nb"})
        assert jenkins.triggers()[0].form == {"VARIABLES": "BROWSER=firefox\nLOCALE=de", "NOTES": "a\nb"}


class TestCancelBeyondTheQueueItem:
    def test_a_dispatch_awaiting_results_is_stopped_after_its_queue_item_expired(self, make_service, jenkins):
        """Callback mode, minutes later: the queue item is gone. The build is
        found by the queue id it records, and stopped."""
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        service = jenkins_service(make_service, jenkins)
        run_id, _ = dispatch(service)
        wait_until(lambda: (JOB, 1) in jenkins.builds, what="the build to start")
        jenkins.expire(1)
        cancelled = service.cancel(run_id)
        assert cancelled.state == "CANCELLED"
        assert [s.path for s in jenkins.posts("/stop")] == [f"/job/{JOB}/1/stop"]
        assert jenkins.posts("/queue/cancelItem") == [], "nothing was 'cancelled' in a queue that forgot it"
        assert any("stop requested" in e["message"] for e in events_of(service, run_id, "log"))

    def test_a_polled_build_is_stopped_through_the_link_recorded_for_it(self, make_service, jenkins):
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        service = jenkins_service(make_service, jenkins, **POLL)
        run_id = compose(service, "Payments-Cards", runner_id=RUNNER)
        outcome = service.dispatch(run_id, RUNNER)
        wait_until(
            lambda: "build" in service.store.get_dispatch(outcome.dispatch_id).links,
            what="the build link to be recorded",
        )
        jenkins.expire(1)
        queue_reads = len(jenkins.gets("/queue/item/"))
        service.cancel(run_id)
        assert service.wait(run_id, timeout=30).state == "CANCELLED"
        assert jenkins.builds[(JOB, 1)].stop.is_set()
        assert len(jenkins.gets("/queue/item/")) == queue_reads, "the link was enough"

    def test_a_cancel_that_arrives_during_the_trigger_takes_the_build_back(self, make_service, jenkins):
        """The trigger comes before the hand-off, necessarily. A cancel in
        that window finds nothing to stop yet — so the runner, back from the
        trigger, stops the build it has just started."""
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        stalled = threading.Event()
        jenkins.stalls["/buildWithParameters"] = stalled
        service = jenkins_service(make_service, jenkins, **POLL)
        run_id = compose(service, "Payments-Cards", runner_id=RUNNER)
        outcome = service.dispatch(run_id, RUNNER)
        wait_until(lambda: jenkins.triggers(), what="the trigger request to be in flight")
        service.cancel(run_id)
        stalled.set()
        assert outcome.done.wait(30)
        run = service.require_run(run_id)
        assert run.state == "CANCELLED" and run.latest_dispatch.shards == ()
        wait_until(lambda: (JOB, 1) in jenkins.builds, what="the build")
        assert jenkins.builds[(JOB, 1)].stop.is_set() or jenkins.queue[1].cancelled
        assert len(jenkins.posts("/stop")) <= 1, "one stop, whoever reached the build"
        assert any("cancelled during the trigger" in e["message"] for e in events_of(service, run_id, "log"))


#: What the runner below needs to cancel its own dispatch at the chosen moment.
LATE_CANCEL: dict[str, Any] = {}


class LateCancelRunner(JenkinsRunner):
    """The `jenkins` runner, with a cancel landing in the one window where
    two parts of it could each stop the build: after the hand-off recorded
    the queue link (so the runner's ``cancel`` stops the build by it), and
    before ``follow``'s first check (which takes a cancelled build back)."""

    def _poll_and_deliver(self, ctx: DispatchContext, queue_url: str | None, shards: list[str]) -> None:
        fake: FakeJenkins = LATE_CANCEL["jenkins"]
        wait_until(lambda: (JOB, 1) in fake.builds, what="the build to run")
        LATE_CANCEL["service"].cancel(ctx.run_id)  # the runner's cancel runs here
        super()._poll_and_deliver(ctx, queue_url, shards)


#: No answer once a request was on its way: it may have been received.
MAYBE_RECEIVED = [
    urllib.error.URLError(TimeoutError("timed out")),
    urllib.error.URLError(ConnectionResetError(54, "Connection reset by peer")),
    TimeoutError("timed out"),
    ConnectionResetError(54, "Connection reset by peer"),
    http.client.RemoteDisconnected("Remote end closed connection without response"),
]
MAYBE_RECEIVED_IDS = [
    "timeout-on-the-way-out", "reset-on-the-way-out", "timeout-waiting", "reset-waiting", "closed-unanswered",
]


class TestOneStopPerBuild:
    """A second stop ends what a job does after the first: its own clean-up
    (a pipeline's ``post`` steps). So a build is sent one stop — never a
    second, neither by the client's retry nor by a second way to the same
    build."""

    def running_build(self, jenkins: FakeJenkins, client: JenkinsClient) -> tuple[str, str]:
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        queue_url = client.trigger(JOB, {})
        assert queue_url is not None
        return queue_url, client.wait_for_build(JOB, queue_url, timeout_s=5, poll_interval_s=0.01)

    def test_a_stop_that_timed_out_after_it_arrived_is_not_sent_again(self, jenkins):
        client = JenkinsClient(jenkins.url, retries=3, retry_backoff_s=0.01, request_timeout_s=0.3)
        _, build_url = self.running_build(jenkins, client)
        stalled = jenkins.stalls["/stop"] = threading.Event()
        with pytest.raises(JenkinsError, match="timed out"):
            client.stop_build(build_url)
        stalled.set()
        wait_until(lambda: jenkins.builds[(JOB, 1)].stop.is_set(), what="the stop to take effect")
        assert len(jenkins.posts("/stop")) == 1
        assert client.stop_build(build_url) is False, "nor by a later call"
        assert len(jenkins.posts("/stop")) == 1

    def test_a_refused_connection_is_still_retried(self, jenkins, monkeypatch):
        client = client_for(jenkins, retries=2)
        _, build_url = self.running_build(jenkins, client)
        send = client._opener.open
        refused: list[str] = []

        def refuse_the_first_stop(request: Any, timeout: float | None = None) -> Any:
            if request.full_url.endswith("/stop") and not refused:
                refused.append(request.full_url)
                raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
            return send(request, timeout=timeout)

        monkeypatch.setattr(client._opener, "open", refuse_the_first_stop)
        assert client.stop_build(build_url) is True
        assert refused == [f"{build_url}/stop"] and len(jenkins.posts("/stop")) == 1
        assert jenkins.builds[(JOB, 1)].stop.is_set()

    def test_a_server_that_refuses_every_connection_is_tried_as_often_as_configured(self):
        client = JenkinsClient(f"http://127.0.0.1:{closed_port()}", retries=2, retry_backoff_s=0.01)
        with pytest.raises(JenkinsError, match=r"after 3 attempt\(s\)"):
            client.stop_build(f"{client.base_url}/job/{JOB}/1")

    def test_a_stop_that_never_arrived_is_sent_by_a_later_call(self, jenkins, monkeypatch):
        """Every attempt refused: the build was not stopped, so it is not
        remembered as stopped — the next call sends the stop."""
        client = client_for(jenkins, retries=1)
        _, build_url = self.running_build(jenkins, client)
        send = client._opener.open

        def refuse_every_stop(request: Any, timeout: float | None = None) -> Any:
            if request.full_url.endswith("/stop"):
                raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
            return send(request, timeout=timeout)

        with monkeypatch.context() as patched:
            patched.setattr(client._opener, "open", refuse_every_stop)
            with pytest.raises(JenkinsError, match=r"after 2 attempt\(s\)"):
                client.stop_build(build_url)
        assert jenkins.posts("/stop") == []
        assert client.stop_build(build_url) is True
        assert len(jenkins.posts("/stop")) == 1 and jenkins.builds[(JOB, 1)].stop.is_set()

    @pytest.mark.parametrize("status", [502, 504])
    def test_a_trigger_a_gateway_answered_says_the_build_may_be_queued(self, jenkins, status):
        jenkins.add_job(JOB, lambda build: None)
        client = client_for(jenkins, retries=2)
        jenkins.faults.append(("POST", "/buildWithParameters", status))
        with pytest.raises(JenkinsError, match="may have been queued all the same") as caught:
            client.trigger(JOB, {})
        assert caught.value.status == status and len(jenkins.triggers()) == 1

    def test_a_trigger_that_timed_out_says_the_build_may_be_queued(self, jenkins):
        jenkins.add_job(JOB, lambda build: None)
        client = JenkinsClient(jenkins.url, retries=2, retry_backoff_s=0.01, request_timeout_s=0.3)
        stalled = jenkins.stalls["/buildWithParameters"] = threading.Event()
        try:
            with pytest.raises(JenkinsError, match="may have been queued all the same"):
                client.trigger(JOB, {})
        finally:
            stalled.set()
        assert len(jenkins.triggers()) == 1

    @pytest.mark.parametrize("failure", MAYBE_RECEIVED, ids=MAYBE_RECEIVED_IDS)
    def test_a_trigger_without_an_answer_says_the_build_may_be_queued(self, jenkins, monkeypatch, failure):
        """No answer once the request was on its way — a timeout counts
        whenever it came, while connecting too: it cannot be told apart."""
        client = client_for(jenkins, retries=2)
        attempts: list[str] = []

        send = client._opener.open

        def unanswered(request: Any, timeout: float | None = None) -> Any:
            if not request.full_url.endswith("/buildWithParameters"):
                return send(request, timeout=timeout)
            attempts.append(request.full_url)
            raise failure

        monkeypatch.setattr(client._opener, "open", unanswered)
        with pytest.raises(JenkinsError, match="may have been queued all the same"):
            client.trigger(JOB, {})
        assert len(attempts) == 1, "and it is not sent again"

    @pytest.mark.parametrize("failure", MAYBE_RECEIVED, ids=MAYBE_RECEIVED_IDS)
    def test_a_stop_without_an_answer_counts_as_sent(self, jenkins, monkeypatch, failure):
        client = client_for(jenkins, retries=2)
        _, build_url = self.running_build(jenkins, client)
        send = client._opener.open

        def unanswered(request: Any, timeout: float | None = None) -> Any:
            if request.full_url.endswith("/stop"):
                raise failure
            return send(request, timeout=timeout)

        with monkeypatch.context() as patched:
            patched.setattr(client._opener, "open", unanswered)
            with pytest.raises(JenkinsError):
                client.stop_build(build_url)
        assert client.stop_build(build_url) is False and jenkins.posts("/stop") == []

    @pytest.mark.parametrize("status", [400, 403, 404, 500, 429, 503])
    def test_a_trigger_the_server_answered_itself_does_not_say_so(self, jenkins, status):
        """The server's own no, its own error, or "try later" until the
        retries were used up: nothing says that a build was queued."""
        jenkins.add_job(JOB, lambda build: None)
        client = client_for(jenkins, retries=1)
        jenkins.faults.extend([("POST", "/buildWithParameters", status)] * 3)
        with pytest.raises(JenkinsError) as caught:
            client.trigger(JOB, {})
        assert caught.value.status == status and "may have been queued" not in str(caught.value)

    @pytest.mark.parametrize(
        "reason",
        [
            ConnectionRefusedError(61, "Connection refused"),
            socket.gaierror(8, "nodename nor servname provided, or not known"),
            ssl.SSLCertVerificationError(1, "certificate verify failed"),
            ssl.SSLError(1, "wrong version number"),
        ],
        ids=["refused", "name-not-found", "certificate-not-trusted", "no-tls-session"],
    )
    def test_a_trigger_that_never_reached_the_server_does_not_say_so(self, jenkins, monkeypatch, reason):
        client = client_for(jenkins, retries=1)

        def unreachable(request: Any, timeout: float | None = None) -> Any:
            raise urllib.error.URLError(reason)

        monkeypatch.setattr(client._opener, "open", unreachable)
        with pytest.raises(JenkinsError) as caught:
            client.trigger(JOB, {})
        assert "may have been queued" not in str(caught.value)
        assert jenkins.triggers() == []

    @pytest.mark.parametrize(
        "reason",
        [socket.gaierror(8, "not known"), ssl.SSLCertVerificationError(1, "certificate verify failed")],
        ids=["name-not-found", "certificate-not-trusted"],
    )
    def test_a_stop_to_a_server_never_reached_is_sent_by_a_later_call(self, jenkins, monkeypatch, reason):
        client = client_for(jenkins, retries=1)
        _, build_url = self.running_build(jenkins, client)
        send = client._opener.open

        def unreachable(request: Any, timeout: float | None = None) -> Any:
            if request.full_url.endswith("/stop"):
                raise urllib.error.URLError(reason)
            return send(request, timeout=timeout)

        with monkeypatch.context() as patched:
            patched.setattr(client._opener, "open", unreachable)
            with pytest.raises(JenkinsError):
                client.stop_build(build_url)
        assert client.stop_build(build_url) is True and len(jenkins.posts("/stop")) == 1

    def test_a_stop_is_remembered_for_60_minutes_not_for_ever(self, jenkins, monkeypatch):
        """A build URL names another build once its job was deleted and
        created again: the new build 1 must get its stop."""
        from runcomposer.sdk import jenkins as toolkit

        client = client_for(jenkins)
        _, build_url = self.running_build(jenkins, client)
        assert client.stop_build(build_url) is True and client.stop_build(build_url) is False
        clock = toolkit.time.monotonic()
        monkeypatch.setattr(toolkit.time, "monotonic", lambda: clock + 3599)
        assert client.stop_build(build_url) is False, "within the 60 minutes: still the same build"
        monkeypatch.setattr(toolkit.time, "monotonic", lambda: clock + 3601)
        assert client.stop_build(build_url) is True, "after it: a build of that URL is stopped again"
        assert len(jenkins.posts("/stop")) == 2

    def test_no_more_than_4096_stops_are_remembered(self, jenkins, monkeypatch):
        client = client_for(jenkins)
        monkeypatch.setattr(client, "request", lambda *args, **options: contextlib.nullcontext())
        for number in range(1, 4098):
            assert client.stop_build(f"{jenkins.url}/job/{JOB}/{number}") is True
        assert len(client._stopped) == 4096
        assert client.stop_build(f"{jenkins.url}/job/{JOB}/4097") is False
        assert client.stop_build(f"{jenkins.url}/job/{JOB}/1") is True, "the oldest was forgotten"

    def test_a_stop_the_server_said_no_to_is_sent_by_a_later_call(self, jenkins):
        """A 4xx is the server's own no — the build was not stopped, so it
        is not remembered as stopped."""
        client = client_for(jenkins, retries=3)
        _, build_url = self.running_build(jenkins, client)
        jenkins.faults.append(("POST", "/stop", 404))
        with pytest.raises(JenkinsError) as caught:
            client.stop_build(build_url)
        assert caught.value.status == 404 and len(jenkins.posts("/stop")) == 1
        assert not jenkins.builds[(JOB, 1)].stop.is_set()
        assert client.stop_build(build_url) is True
        assert len(jenkins.posts("/stop")) == 2 and jenkins.builds[(JOB, 1)].stop.is_set()

    @pytest.mark.parametrize(("status", "sent"), [(503, 2), (429, 2), (502, 1), (504, 1)])
    def test_only_an_answer_that_says_it_was_not_acted_on_is_retried(self, jenkins, status, sent):
        """429 and 503 say the server did not act on the request; a gateway's
        502 or 504 does not — the server behind it may have stopped the build."""
        client = client_for(jenkins, retries=3)
        _, build_url = self.running_build(jenkins, client)
        jenkins.faults.append(("POST", "/stop", status))
        if sent == 1:
            with pytest.raises(JenkinsError) as caught:
                client.stop_build(build_url)
            assert caught.value.status == status
        else:
            assert client.stop_build(build_url) is True
        assert len(jenkins.posts("/stop")) == sent

    def test_follow_does_not_stop_a_build_its_runner_s_cancel_has_stopped(self, jenkins):
        client = client_for(jenkins)
        queue_url, build_url = self.running_build(jenkins, client)
        assert client.cancel(JOB, queue_url) == f"stop requested for {build_url}"  # a runner's cancel

        def cancelled() -> None:
            raise DispatchCancelled("cancelled before the first check")

        with pytest.raises(DispatchCancelled):
            client.follow(JOB, queue_url, timeout_s=5, check=cancelled)  # its take-back
        assert len(jenkins.posts("/stop")) == 1
        assert client.cancel(JOB, queue_url) == f"stop already requested for {build_url} — not sent again"
        assert len(jenkins.posts("/stop")) == 1

    def test_a_cancel_between_the_hand_off_and_follow_sends_one_stop(self, make_service, jenkins):
        """The runner's ``cancel`` stops the running build by the queue link,
        and ``follow``'s first check then sees the cancelled dispatch and
        takes the build back: two ways to one build, one stop."""
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        service = jenkins_service(
            make_service, jenkins, module="tests.test_jenkins_toolkit:LateCancelRunner", **POLL
        )
        LATE_CANCEL.update(jenkins=jenkins, service=service)
        try:
            run_id = compose(service, "Payments-Cards", runner_id=RUNNER)
            service.dispatch(run_id, RUNNER)
            assert service.wait(run_id, timeout=30).state == "CANCELLED"
        finally:
            LATE_CANCEL.clear()
        assert [s.path for s in jenkins.posts("/stop")] == [f"/job/{JOB}/1/stop"]
        logs = [e["message"] for e in events_of(service, run_id, "log")]
        assert f"jenkins: stop requested for {jenkins.url}/job/{JOB}/1" in logs
        assert jenkins.gets(f"/job/{JOB}/1/api/json") == [], "follow took it back at its first check"

    def test_two_cancels_at_once_send_one_stop(self, make_service, jenkins):
        """Two cancel requests for a dispatch that awaits results — a double
        click — both reach the runner's ``cancel`` before either has closed
        the dispatch."""
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        service = jenkins_service(make_service, jenkins)
        run_id, _ = dispatch(service)
        wait_until(lambda: (JOB, 1) in jenkins.builds, what="the build to start")
        stalled = jenkins.stalls["/stop"] = threading.Event()
        answers: list[Any] = []

        def first_cancel() -> None:
            try:
                answers.append(service.cancel(run_id))
            except Exception as exc:
                answers.append(exc)

        first = threading.Thread(target=first_cancel)
        first.start()
        try:
            wait_until(lambda: jenkins.posts("/stop"), what="the first stop to be in flight")
            assert service.cancel(run_id).state == "CANCELLED"
        finally:
            stalled.set()
            first.join(30)
        assert len(jenkins.posts("/stop")) == 1
        assert service.require_run(run_id).latest_dispatch.state == "CANCELLED"
        assert any("not sent again" in e["message"] for e in events_of(service, run_id, "log"))


# -- 3. the same, composed from the client ----------------------------------------------


class NightlyRunner(RunnerBase):
    """A connector's own Jenkins runner for the shop's nightly job: one build
    per run, one shard per region, results taken from the build's console
    log. Written against ``runcomposer.sdk`` and ``runcomposer.sdk.jenkins``
    only — the HTTP is the toolkit's."""

    def __init__(self, base_url: str, job: str, regions: list[str], timeout_s: float = 600.0,
                 poll_interval_s: float = 5.0):
        self.client = JenkinsClient(base_url, retry_backoff_s=0.01)
        self.job, self.regions = job, list(regions)
        self.timeout_s, self.poll_interval_s = float(timeout_s), float(poll_interval_s)

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="nightly",
            capabilities=("cancel", "plan"),
            parameters=(
                Parameter("regions", kind="multi_choice", choices=tuple(self.regions), default=list(self.regions)),
            ),
            result_format="runcomposer-verdicts",
        )

    def _regions(self, ctx: PlanContext) -> list[str]:
        regions = list(ctx.parameters.get("regions") or self.regions)
        if len(ctx.item_ids) < len(regions):
            ctx.refuse(f"{len(ctx.item_ids)} test(s) cannot be spread over {len(regions)} regions")
        return regions

    def plan(self, ctx: PlanContext) -> PlanPreview:
        regions = self._regions(ctx)
        return PlanPreview(text=f"nightly job, regions {regions}", shards={region: [] for region in regions})

    def dispatch(self, ctx: DispatchContext) -> None:
        regions = self._regions(ctx)
        queue_url = self.client.trigger(
            self.job, {"SPEC_B64": encode_spec(ctx.export_spec()), "REGIONS": ",".join(regions)}
        )
        # The trigger was accepted: from here on a build exists, so this is
        # the hand-off — whatever goes wrong now is a failed dispatch that
        # names the job, never a refusal that forgets it.
        ctx.declare(regions, plan=self.plan(ctx).text, links={"queue": queue_url or "", "job": self.client.job_url(self.job)})
        if queue_url is None:
            raise RuntimeError("the server did not say where it queued the build")
        # Queue item → build → its end; a cancel that came while the trigger
        # was under way is seen by the first check, and the build taken back.
        build = self.client.follow(
            self.job, queue_url, timeout_s=self.timeout_s, poll_interval_s=self.poll_interval_s,
            check=ctx.raise_if_cancelled, on_build=lambda url: ctx.set_link("build", url))
        if build["result"] not in ("SUCCESS", "UNSTABLE"):
            raise RuntimeError(f"the build ended {build['result']}: {build['url']}")
        build_url = build["url"]
        console = self.client.download_build_file(build_url, "consoleText", ctx.private_dir / "console.txt")
        for region in ctx.outstanding_shards():
            _, found, rest = console.read_text(encoding="utf-8").partition(f"=== results {region} ===\n")
            if not found:
                raise RuntimeError(f"the console log has no results for {region}")
            results = ctx.workdir / region / "results.json"
            results.parent.mkdir(parents=True, exist_ok=True)
            results.write_text(rest.partition("\n=== end ===")[0], encoding="utf-8")
            ctx.add_artifact(f"results ({region})", results, shard=region)
            ctx.deliver(region, bundle=results.parent)

    def cancel(self, ctx: DispatchContext) -> None:
        if "build" in ctx.links:
            self.client.stop_build(ctx.links["build"])
        elif "queue" in ctx.links:
            ctx.log(self.client.cancel(self.job, ctx.links["queue"]))


NIGHTLY = "tests.test_jenkins_toolkit:NightlyRunner"


class TestAComposedRunner:
    def options(self, jenkins: FakeJenkins) -> dict[str, Any]:
        return {"base_url": jenkins.url, "job": JOB, "regions": ["eu", "us"], "poll_interval_s": 0.01}

    def test_it_passes_the_conformance_check(self, jenkins):
        jenkins.add_job(JOB, regional_job())
        ctx = check_runner(NightlyRunner(**self.options(jenkins)), FakeDispatchContext(CARDS), expect="complete")
        assert ctx.declared == ("eu", "us") and ctx.summary() == {"PASS": 6}
        assert dict(ctx.links) == {
            "job": f"{jenkins.url}/job/{JOB}",
            "queue": f"{jenkins.url}/queue/item/1/",
            "build": f"{jenkins.url}/job/{JOB}/1",
        }
        assert (ctx.private_dir / "console.txt").is_file()
        assert [a.name for a in ctx.artifacts] == ["results (eu)", "results (us)"]

    def test_it_runs_in_a_real_deployment_and_its_private_file_is_not_served(self, jenkins):
        jenkins.add_job(JOB, regional_job(failing={CARDS[0]}))
        with ConnectorHarness(runners={"nightly": {"module": NIGHTLY, **self.options(jenkins)}}, serve=True) as harness:
            import urllib.error
            import urllib.request

            result = harness.run("Payments-Cards", runner="nightly", parameters={"regions": ["us", "eu"]})
            assert (result.state, result.completion) == ("COMPLETE", "FAIL")
            assert result.dispatch.shards == ("us", "eu") and result.summary == {"PASS": 5, "FAIL": 1}
            assert result.links["build"] == f"{jenkins.url}/job/{JOB}/1"
            base = f"{harness.url}/artifacts/{result.run_id}/{result.dispatch.dispatch_id}"
            with urllib.request.urlopen(f"{base}/eu/results.json", timeout=10) as response:
                assert response.status == 200
            for unrecorded in ("console.txt", "../../private/console.txt"):
                with pytest.raises(urllib.error.HTTPError) as refused:
                    urllib.request.urlopen(f"{base}/{unrecorded}", timeout=10)
                assert refused.value.code == 404

    def test_it_is_cancelled_while_its_build_runs(self, jenkins):
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        with ConnectorHarness(runners={"nightly": {"module": NIGHTLY, **self.options(jenkins)}}) as harness:
            handle = harness.start("Payments-Cards", runner="nightly")
            handle.wait_until(lambda run: "build" in run.links, timeout=30)
            handle.cancel()
            assert handle.wait(timeout=30).state == "CANCELLED"
            assert jenkins.builds[(JOB, 1)].stop.is_set()

    def test_a_cancel_that_arrives_during_its_trigger_takes_the_build_back(self, jenkins):
        """Before the hand-off there is no link for ``cancel`` to find the
        build by. ``follow`` sees the cancellation at its first check and
        stops what the trigger started."""
        jenkins.add_job(JOB, lambda build: build.stop.wait(30))
        stalled = threading.Event()
        jenkins.stalls["/buildWithParameters"] = stalled
        with ConnectorHarness(runners={"nightly": {"module": NIGHTLY, **self.options(jenkins)}}) as harness:
            handle = harness.start("Payments-Cards", runner="nightly")
            wait_until(lambda: jenkins.triggers(), what="the trigger request to be in flight")
            handle.cancel()
            stalled.set()
            assert handle.wait(timeout=30).state == "CANCELLED"
            wait_until(lambda: (JOB, 1) in jenkins.builds, what="the build")
            assert jenkins.builds[(JOB, 1)].stop.is_set() or jenkins.queue[1].cancelled

    def test_it_uses_only_the_sdk_and_the_jenkins_toolkit(self):
        import ast
        import inspect
        import textwrap

        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        allowed, other = set(), set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                names = {alias.asname or alias.name for alias in node.names}
                if node.module in ("runcomposer.sdk", "runcomposer.sdk.jenkins"):
                    allowed |= names
                elif node.level or (node.module or "").startswith("runcomposer"):
                    other |= names
        source = ast.parse(textwrap.dedent(inspect.getsource(NightlyRunner)))
        used = {node.id for node in ast.walk(source) if isinstance(node, ast.Name)}
        assert not used & other and {"JenkinsClient", "encode_spec", "RunnerBase"} <= used & allowed
        assert not [node for node in ast.walk(source) if isinstance(node, (ast.Import, ast.ImportFrom))]
