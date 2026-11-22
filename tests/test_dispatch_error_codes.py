"""Why a dispatch ended with an error, as a field of its own (DESIGN.md §4,
§9): ``error_code`` beside the free-text ``error`` on every dispatch of
``GET /api/v1/runs/{id}``, one of a closed list — ``runner-raised``,
``interrupted``, ``expired``, ``cancelled`` — and ``""`` for a dispatch
without an error. A client tells "the runner reported an error" from "the
process executing it is gone" by the code, not by reading the text.
"""

from __future__ import annotations

from typing import Any

from runcomposer.sdk import DISPATCH_ERROR_CODES, DispatchContext, RunnerBase, RunnerInfo

from .conftest import wait_until

API = "/api/v1"
HERE = "tests.test_dispatch_error_codes"


class Raising(RunnerBase):
    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="raising")

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        raise RuntimeError("the staging shop answered 502")


class Elsewhere(RunnerBase):
    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="elsewhere", capabilities=("external_delivery",))

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])


def latest_dispatch(client: Any, run_id: str) -> dict[str, Any]:
    dispatch: dict[str, Any] = client.get(f"{API}/runs/{run_id}").json()["dispatches"][-1]
    return dispatch


def start(client: Any, runner: str) -> str:
    created = client.post(f"{API}/runs", json={"selection": {"tag_filter": "Auth-SSO"}, "dispatch": {"runner": runner}})
    assert created.status_code == 201, created.text
    run_id: str = created.json()["run"]["id"]
    return run_id


def test_the_codes_are_a_closed_list():
    assert DISPATCH_ERROR_CODES == ("runner-raised", "interrupted", "expired", "cancelled")


def test_a_runner_that_raises_after_the_hand_off(make_client):
    client, _service = make_client(runners={"raising": {"module": f"{HERE}:Raising"}})
    run_id = start(client, "raising")
    wait_until(lambda: latest_dispatch(client, run_id)["state"] == "FAILED", what="the dispatch to fail")
    dispatch = latest_dispatch(client, run_id)
    assert (dispatch["error_code"], dispatch["error_owner"]) == ("runner-raised", None)
    assert dispatch["error"] == "RuntimeError: the staging shop answered 502"


def test_a_cancelled_dispatch(make_client):
    client, _service = make_client(runners={"elsewhere": {"module": f"{HERE}:Elsewhere"}})
    run_id = start(client, "elsewhere")
    wait_until(lambda: latest_dispatch(client, run_id)["state"] == "AWAITING_RESULTS", what="the hand-off")
    assert latest_dispatch(client, run_id)["error_code"] == ""
    assert client.post(f"{API}/runs/{run_id}/cancel").status_code in (200, 202)
    wait_until(lambda: latest_dispatch(client, run_id)["state"] == "CANCELLED", what="the cancel")
    dispatch = latest_dispatch(client, run_id)
    assert (dispatch["error_code"], dispatch["error_owner"]) == ("cancelled", None)


def test_an_expired_dispatch(make_client):
    from runcomposer.app.expiry import expire_awaiting
    from runcomposer.core.timeutil import epoch_ms

    client, service = make_client(
        runners={"elsewhere": {"module": f"{HERE}:Elsewhere"}},
        core={"dispatch": {"awaiting_timeout_s": 1}},
    )
    run_id = start(client, "elsewhere")
    wait_until(lambda: latest_dispatch(client, run_id)["state"] == "AWAITING_RESULTS", what="the hand-off")
    dispatch_id = latest_dispatch(client, run_id)["dispatch_id"]
    later = service.store.events(run_id)[-1].at
    expired = expire_awaiting(service, now_ms=(epoch_ms(later) or 0) + 5_000)
    assert [d.dispatch_id for d in expired] == [dispatch_id]
    dispatch = latest_dispatch(client, run_id)
    assert (dispatch["state"], dispatch["error_code"]) == ("FAILED", "expired")
    assert dispatch["error"].startswith("expired: no results for")


class EmptyBundle(RunnerBase):
    """Delivers a shard directory that holds no JUnit file: the parser's
    error names the directory, as a path of the work directory."""

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="empty", result_format="junit-xml")

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["shard-1"])
        bundle = ctx.workdir / "shard-1"
        bundle.mkdir(parents=True, exist_ok=True)
        ctx.log(f"delivering {bundle}")
        ctx.deliver("shard-1", bundle=bundle, format="junit-xml")


def test_an_error_names_no_directory_of_the_server(make_client):
    """What is recorded for a dispatch — its error and its log lines — says
    where in the artifact directory, never where that directory is."""
    client, service = make_client(runners={"empty": {"module": f"{HERE}:EmptyBundle"}})
    run_id = start(client, "empty")
    wait_until(lambda: latest_dispatch(client, run_id)["state"] == "FAILED", what="the dispatch to fail")
    dispatch = latest_dispatch(client, run_id)
    roots = {str(service.config.artifact_dir), str(service.config.artifact_dir.resolve())}
    assert "bundle contains no 'junit-xml' results" in dispatch["error"]
    assert not any(root in dispatch["error"] for root in roots), dispatch["error"]
    assert dispatch["error"].rstrip().endswith("shard-1"), dispatch["error"]
    events = client.get(f"{API}/runs/{run_id}/events").json()
    text = " ".join(str(event["data"]) for event in (events["events"] if isinstance(events, dict) else events))
    assert "delivering" in text and not any(root in text for root in roots)
