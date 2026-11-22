"""``callback_urls``: the URLs an executor that runs elsewhere reaches a run
by, made by the SDK instead of formatted by every runner (DESIGN.md §6.6).

The reference ``jenkins`` runner hands its job the ``results`` URL as
``{callback_url}`` and the ``spec`` URL as ``{spec_url}``; an outside runner
gets the same URLs from the same helper, and they are the endpoints a
served deployment really answers on.
"""

from __future__ import annotations

import json

import pytest

from runcomposer.sdk import (
    CallbackUrls,
    DispatchContext,
    RemoteLiveChannel,
    RunnerBase,
    RunnerInfo,
    callback_urls,
    push_results,
)
from runcomposer.testing import ConnectorHarness

HERE = "tests.test_callback_urls"
#: What the executor elsewhere is given, by run id: the ingest token of its
#: dispatch and the items it is to execute.
HANDED_OUT: dict[str, tuple[str, tuple[str, ...]]] = {}


class TestTheUrls:
    def test_every_url_of_a_run_under_the_base_address(self):
        urls = callback_urls("https://runcomposer.shop.example/", "01RUN", "01DISPATCH")
        assert urls == CallbackUrls(
            run="https://runcomposer.shop.example/api/v1/runs/01RUN",
            results="https://runcomposer.shop.example/api/v1/runs/01RUN/results",
            live="https://runcomposer.shop.example/api/v1/runs/01RUN/live",
            log="https://runcomposer.shop.example/api/v1/runs/01RUN/log",
            spec="https://runcomposer.shop.example/api/v1/runs/01RUN/dispatches/01DISPATCH/spec",
        )

    def test_a_base_that_names_the_api_already_is_taken_as_it_is(self):
        assert callback_urls("https://shop.example/rc/api/v1", "01RUN").results == (
            "https://shop.example/rc/api/v1/runs/01RUN/results"
        )

    def test_without_a_dispatch_there_is_no_document_url(self):
        assert callback_urls("http://localhost:8100", "01RUN").spec is None

    @pytest.mark.parametrize(("base", "run_id"), [("", "01RUN"), ("  ", "01RUN"), ("http://x", "")])
    def test_an_empty_base_or_run_id_is_refused(self, base, run_id):
        with pytest.raises(ValueError, match="callback_urls needs"):
            callback_urls(base, run_id)

    def test_the_jenkins_runner_hands_out_the_same_urls(self):
        from runcomposer.plugins.jenkins import JenkinsRunner
        from runcomposer.testing import FakeDispatchContext

        runner = JenkinsRunner(
            base_url="https://ci.shop.example", job="shop-regression", completion="callback",
            callback_base="https://runcomposer.shop.example",
            parameters={"CALLBACK_URL": "{callback_url}", "SPEC_URL": "{spec_url}"},
        )
        ctx = FakeDispatchContext(["Shop.Cart.Core.T001"])
        preview = ctx.preview(runner)
        assert preview is not None
        urls = callback_urls("https://runcomposer.shop.example", ctx.run_id)
        assert urls.results in preview.text


class Elsewhere(RunnerBase):
    """Hands the work to an executor elsewhere and returns: the executor
    reports through the run's callback URLs."""

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="elsewhere", capabilities=("external_delivery",))

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["agent-1"])
        HANDED_OUT[ctx.run_id] = (ctx.ingest_token() or "", tuple(ctx.item_ids))


class TestTheyAreTheEndpointsADeploymentAnswersOn:
    def test_live_status_a_log_line_and_the_results_arrive_through_them(self, tmp_path):
        with ConnectorHarness(
            runners={"elsewhere": {"module": f"{HERE}:Elsewhere"}}, serve=True
        ) as harness:
            handle = harness.start("Checkout", runner="elsewhere")
            run = handle.wait()
            assert run.state == "AWAITING_RESULTS"
            dispatch = run.dispatch
            assert dispatch is not None
            urls = callback_urls(harness.url, run.run_id, dispatch.dispatch_id)
            token, item_ids = HANDED_OUT[run.run_id]
            first = item_ids[0]

            channel = RemoteLiveChannel(urls.live, token, dispatch_id=dispatch.dispatch_id)
            channel.live("agent-1", first, "RUNNING")
            channel.log("agent-1 started", "info")
            # Nothing to close and nothing in flight: each call is one request,
            # answered before the call returns.
            seen = handle.read()
            assert [s.status for s in seen.live] == ["RUNNING"] and "agent-1 started" in seen.logs()
            assert channel.failures == 0 and not hasattr(channel, "close")

            results = tmp_path / "results.json"
            results.write_text(json.dumps({
                "format": "runcomposer-verdicts",
                "verdicts": [{"name": item_id, "status": "PASS"} for item_id in item_ids],
            }), encoding="utf-8")
            push_results(urls.results, token, results, shard="agent-1", dispatch_id=dispatch.dispatch_id)
            done = harness.wait(run.run_id, timeout=30)
            assert (done.state, done.completion) == ("COMPLETE", "PASS")
