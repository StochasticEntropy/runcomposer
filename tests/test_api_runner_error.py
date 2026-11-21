"""A runner that raises after the hand-off: what an HTTP client reads.

The dispatch ends ``FAILED`` and the exception's text is readable where a
client looks for it — the run detail and the run's row in the list — with
every value the deployment takes from the environment blanked out.
"""

from __future__ import annotations

from typing import Any

from runcomposer.app.redaction import REDACTED
from runcomposer.sdk import DispatchContext, RunnerBase, RunnerInfo

from .conftest import wait_until

API = "/api/v1"
GATEWAY_KEY = "k-shop-gateway-7f3e9a"


class ExplodingRunner(RunnerBase):
    """Declares its shard, then raises with a configured option in the
    message — the way a client library quotes the credential it was given."""

    def __init__(self, gateway_key: str):
        self.gateway_key = gateway_key

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="exploding")

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        raise RuntimeError(f"the staging shop gateway refused key {self.gateway_key}")


def test_the_error_is_in_the_detail_and_in_the_list_row(make_client, monkeypatch):
    monkeypatch.setenv("SHOP_GATEWAY_KEY", GATEWAY_KEY)
    client, _service = make_client(
        runners={
            "exploding": {
                "module": "tests.test_api_runner_error:ExplodingRunner",
                "gateway_key": "${env:SHOP_GATEWAY_KEY}",
            }
        }
    )
    started = client.post(
        f"{API}/runs", json={"selection": {"tag_filter": "Auth-SSO"}, "dispatch": {"runner": "exploding"}}
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["run"]["id"]

    def failed() -> dict[str, Any] | None:
        detail = client.get(f"{API}/runs/{run_id}").json()
        return detail if detail["dispatches"] and detail["dispatches"][-1]["state"] == "FAILED" else None

    detail = wait_until(failed, what="the dispatch to end FAILED")
    expected = f"RuntimeError: the staging shop gateway refused key {REDACTED}"
    assert detail["dispatches"][-1]["error"] == expected

    (row,) = [run for run in client.get(f"{API}/runs").json()["runs"] if run["id"] == run_id]
    assert row["dispatches"][-1]["state"] == "FAILED"
    assert row["dispatches"][-1]["error"] == expected

    for answer in (client.get(f"{API}/runs/{run_id}").text, client.get(f"{API}/runs").text):
        assert GATEWAY_KEY not in answer
