"""The HTTP API (DESIGN.md §9), through the real app: ``create_app`` over a
config in a temp directory, with its lifespan running.

Nothing here waits on the clock. A run in flight is followed the way a client
follows one — the events long-poll (``wait=``) — and runners that must be
caught mid-run are gated on ``threading.Event``.
"""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from runcomposer import __version__
from runcomposer.core.taxonomy import TaxonomyError
from runcomposer.http import create_app
from runcomposer.sdk import (
    ConfigError,
    DispatchContext,
    Health,
    RunnerBase,
    RunnerInfo,
    Service,
    Verdict,
    load_config,
)

from .conftest import compose, posix_only, wait_until, write_config
from .exec_loader import runcomposer_exec

SSO = ["Shop.Auth.SSO.T001", "Shop.Auth.SSO.T002", "Shop.Auth.SSO.T003"]
API = "/api/v1"

# -- runners for this module, configured by import path --------------------------------

GATES: dict[str, threading.Event] = {}
HEALTH_PROBES: list[str] = []


class RefusingRunner(RunnerBase):
    """Refuses every dispatch, right away — the drift check's shape."""

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="refusing")

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.refuse("the corpus changed since this run was composed")


class GatedRunner(RunnerBase):
    """Declares, then stays in flight until the test opens its gate or it is
    cancelled."""

    def __init__(self, gate: str):
        self.gate = gate

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="gated", capabilities=("cancel",))

    def health(self) -> Health:
        HEALTH_PROBES.append(self.gate)
        return Health(status="ok", detail="gate is in place", data={"probes": len(HEALTH_PROBES)})

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        GATES[self.gate + ":started"].set()
        wait_until(lambda: GATES[self.gate].is_set() or ctx.cancelled, what="the gate or a cancel")
        ctx.raise_if_cancelled()
        ctx.deliver("1", verdicts=[Verdict(i, "PASS", 5) for i in ctx.item_ids])


class SlowRefusingRunner(RunnerBase):
    """Prepares until the test opens its gate, then refuses — a refusal that
    arrives after the request's hand-off wait is over."""

    def __init__(self, gate: str):
        self.gate = gate

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="slow-refusing")

    def dispatch(self, ctx: DispatchContext) -> None:
        wait_until(lambda: GATES[self.gate].is_set(), what="the gate")
        ctx.refuse("the staging shop went down while preparing")


@pytest.fixture()
def gated(make_client, request):
    """``(client, service, started, release)`` with a runner ``gated``."""
    name = request.node.name
    GATES[name] = threading.Event()
    GATES[name + ":started"] = threading.Event()
    client, service = make_client(
        runners={"gated": {"module": "tests.test_api:GatedRunner", "gate": name}, "demo": {}}
    )
    yield client, service, GATES[name + ":started"], GATES[name]
    GATES[name].set()
    service.dispatcher.join(30)
    del GATES[name], GATES[name + ":started"]


# -- helpers -----------------------------------------------------------------------------


def follow(client: Any, run_id: str, *, until: str, after: int = 0, timeout: float = 60) -> list[dict]:
    """Follow a run through the events long-poll until an event of kind
    ``until`` arrived; returns every event seen."""
    import time

    seen: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"{API}/runs/{run_id}/events", params={"after": after, "wait": 5}).json()
        seen.extend(body["events"])
        after = body["last_seq"]
        if any(event["kind"] == until for event in seen):
            return seen
    pytest.fail(f"no {until} event for run {run_id} within {timeout}s: {[e['kind'] for e in seen]}")


def assert_error(response: Any, status: int, code: str, *, detail: str = "") -> dict:
    """Every error answer has the same body: a string ``detail`` and a
    stable ``code``."""
    assert response.status_code == status, response.text
    body = response.json()
    assert isinstance(body["detail"], str) and body["detail"]
    assert body["code"] == code, body
    if detail:
        assert detail in body["detail"], body["detail"]
    return body


def finished_run(service: Service, selection: str = "Auth-SSO", seed: str = "a", **kwargs: Any) -> str:
    run_id = compose(service, selection, **kwargs)
    service.dispatch(run_id, "demo", {"seed": seed}, foreground=True)
    return run_id


def results_file(names: dict[str, str] | list[str]) -> tuple[str, tuple[str, bytes, str]]:
    if isinstance(names, list):
        names = dict.fromkeys(names, "PASS")
    payload = {
        "format": "runcomposer-verdicts",
        "verdicts": [{"name": n, "status": s, "duration_ms": 20} for n, s in names.items()],
    }
    return ("files", ("results.json", json.dumps(payload).encode(), "application/json"))


def marker_file(**marker: Any) -> tuple[str, tuple[str, bytes, str]]:
    return ("files", ("runcomposer_run.json", json.dumps(marker).encode(), "application/json"))


def export(client: Any, run_id: str, **body: Any) -> dict:
    response = client.post(f"{API}/runs/{run_id}/dispatch", json={"mode": "export", **body})
    assert response.status_code == 202, response.text
    return response.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# -- errors: one body, the right status -------------------------------------


class TestErrorBody:
    def test_a_domain_error_a_missing_route_and_a_wrong_method_look_the_same(self, client):
        assert_error(client.get(f"{API}/runs/01NOSUCHRUN000000000000000"), 404, "unknown-run")
        assert_error(client.get(f"{API}/no/such/route"), 404, "not-found")
        assert_error(client.put(f"{API}/health"), 405, "method-not-allowed")

    @pytest.mark.parametrize(
        ("body", "field"),
        [
            ({"selection": {"tag_filter": "Auth"}, "title": 5}, "title"),
            ({"selection": {"tag_filter": "Auth"}, "labels": {"a": 1}}, "labels.a"),
            ({"selection": {"tag_filter": "Auth"}, "surprise": True}, "surprise"),
            ({"title": "no selection"}, "selection"),
            ({"selection": "Auth"}, "selection"),
            ({"selection": {"tag_filter": "Auth"}, "keep_run": "yes"}, "keep_run"),
            (
                {"selection": {"tag_filter": "Auth"}, "dispatch": {"runner": "demo", "mode": "export"}},
                "dispatch",
            ),
            ({"selection": {"tag_filter": "Auth"}, "dispatch": {}}, "dispatch"),
            (
                {"selection": {"tag_filter": "Auth"}, "dispatch": {"mode": "export", "parameters": {}}},
                "dispatch",
            ),
            ({"selection": {"tag_filter": "Auth"}, "dispatch": {"mode": "import"}}, "dispatch.mode"),
        ],
    )
    def test_request_validation_is_a_400_with_fields_and_stores_nothing(self, client, body, field):
        """`title: 5` and `labels: {a: 1}` used to be 500s."""
        error = assert_error(client.post(f"{API}/runs", json=body), 400, "invalid-request")
        assert error["detail"].startswith("invalid request — ")
        assert [entry["field"] for entry in error["fields"]] == [field]
        assert all(isinstance(entry["message"], str) for entry in error["fields"])
        assert client.get(f"{API}/runs").json()["total"] == 0

    def test_a_body_that_is_not_json_is_a_400(self, client):
        response = client.post(
            f"{API}/runs", content=b"{not json", headers={"Content-Type": "application/json"}
        )
        assert_error(response, 400, "invalid-request")

    def test_selection_errors_are_400s_with_their_own_codes(self, client):
        bad_filter = client.post(f"{API}/selection/compile", json={"tag_filter": {"op": "XOR", "items": []}})
        assert_error(bad_filter, 400, "invalid-filter", detail="unknown operator")
        unknown_id = client.post(f"{API}/selection/compile", json={"item_ids": ["Shop.Ghost.T404"]})
        assert_error(unknown_id, 400, "invalid-selection", detail="unknown item ids")
        empty = client.post(f"{API}/runs", json={"selection": {}})
        assert_error(empty, 400, "invalid-selection", detail="selection is empty")

    def test_an_unknown_runner_is_a_400_and_leaves_no_run(self, client):
        response = client.post(
            f"{API}/runs", json={"selection": {"tag_filter": "Auth"}, "dispatch": {"runner": "ghost"}}
        )
        assert_error(response, 400, "unknown-runner", detail="no runner is configured under the id 'ghost'")
        assert client.get(f"{API}/runs").json()["total"] == 0

    def test_a_bad_parameter_is_a_400_naming_it_and_leaves_no_run(self, client):
        response = client.post(
            f"{API}/runs",
            json={
                "selection": {"tag_filter": "Auth"},
                "dispatch": {"runner": "demo", "parameters": {"sed": "a"}},
            },
        )
        assert_error(response, 400, "invalid-parameter", detail="unknown parameter 'sed'")
        ill_typed = client.post(
            f"{API}/runs",
            json={"selection": {"tag_filter": "Auth"}, "dispatch": {"runner": "demo", "parameters": {"seed": 7}}},
        )
        assert_error(ill_typed, 400, "invalid-parameter", detail="parameter 'seed' must be a string")
        assert client.get(f"{API}/runs").json()["total"] == 0

    def test_three_failed_dispatch_requests_leave_no_orphans(self, client):
        for body in (
            {"selection": {"tag_filter": "Auth"}, "dispatch": {"runner": "ghost"}},
            {"selection": {"tag_filter": "Auth"}, "dispatch": {"runner": "demo", "parameters": {"x": 1}}},
            {"selection": {"tag_filter": "regex:^NoSuchTagAnywhere$"}, "dispatch": {"runner": "demo"}},
        ):
            assert client.post(f"{API}/runs", json=body).status_code == 400
        assert client.get(f"{API}/runs").json() == {"runs": [], "total": 0, "limit": 50, "offset": 0}

    def test_an_empty_selection_cannot_be_dispatched(self, client):
        response = client.post(
            f"{API}/runs",
            json={"selection": {"tag_filter": "regex:^NoSuchTagAnywhere$"}, "dispatch": {"runner": "demo"}},
        )
        body = assert_error(response, 400, "invalid-request", detail="nothing to execute")
        assert body["run_kept"] is False and body["run_id"] is None


class TestRefusedDispatch:
    @pytest.fixture()
    def refusing(self, make_client):
        return make_client(runners={"closed": {"module": "tests.test_api:RefusingRunner"}})

    def test_the_composed_run_is_removed_and_the_answer_says_so(self, refusing):
        client, _service = refusing
        response = client.post(
            f"{API}/runs", json={"selection": {"tag_filter": "Auth"}, "dispatch": {"runner": "closed"}}
        )
        body = assert_error(response, 409, "dispatch-refused", detail="the corpus changed")
        assert body["run_kept"] is False and body["run_id"] is None
        assert client.get(f"{API}/runs").json()["total"] == 0

    def test_keep_run_keeps_it_composed_with_the_refusal_on_record(self, refusing):
        client, _service = refusing
        response = client.post(
            f"{API}/runs",
            json={"selection": {"tag_filter": "Auth"}, "dispatch": {"runner": "closed"}, "keep_run": True},
        )
        body = assert_error(response, 409, "dispatch-refused")
        assert body["run_kept"] is True
        detail = client.get(f"{API}/runs/{body['run_id']}").json()
        assert detail["state"] == "COMPOSED" and detail["dispatches"] == []
        refused = [e for e in detail["events"] if e["kind"] == "dispatch.refused"]
        assert refused[0]["data"]["reason"] == "the corpus changed since this run was composed"

    def test_a_refusal_after_the_answer_keeps_the_run_the_answer_named(self, make_client, request):
        """The rule is one rule: a refusal that is the request's answer
        removes the run it composed (unless ``keep_run``); a run an answer
        has named is never removed implicitly. ``handoff: pending`` in the
        answer says which case a later refusal will be."""
        name = request.node.name
        GATES[name] = threading.Event()
        try:
            client, service = make_client(
                runners={"slow": {"module": "tests.test_api:SlowRefusingRunner", "gate": name}},
                core={"dispatch": {"handoff_wait_s": 0.05}},
            )
            response = client.post(
                f"{API}/runs", json={"selection": {"tag_filter": "Auth"}, "dispatch": {"runner": "slow"}}
            )
            assert response.status_code == 201, response.text
            body = response.json()
            assert body["handoff"] == "pending" and body["dispatch"]["state"] == "DISPATCHED"
            run_id = body["run"]["id"]
            GATES[name].set()
            follow(client, run_id, until="dispatch.refused")
            detail = client.get(f"{API}/runs/{run_id}").json()
            assert detail["state"] == "COMPOSED" and detail["dispatches"] == []
            refused = [e for e in detail["events"] if e["kind"] == "dispatch.refused"]
            assert refused[0]["data"]["reason"] == "the staging shop went down while preparing"
        finally:
            GATES[name].set()
            service.dispatcher.join(30)
            del GATES[name]

    def test_an_answer_after_the_hand_off_says_done(self, gated):
        client, _service, _started, release = gated
        response = client.post(
            f"{API}/runs", json={"selection": {"tag_filter": "Auth-SSO"}, "dispatch": {"runner": "gated"}}
        )
        assert response.status_code == 201 and response.json()["handoff"] == "done"
        release.set()

    def test_dispatching_a_stored_run_keeps_the_run(self, refusing):
        client, service = refusing
        run_id = compose(service, "Auth-SSO")
        response = client.post(f"{API}/runs/{run_id}/dispatch", json={"runner": "closed"})
        assert_error(response, 409, "dispatch-refused")
        assert client.get(f"{API}/runs/{run_id}").json()["state"] == "COMPOSED"


# -- compose and dispatch ------------------------------------------------------------------


class TestComposeAndDispatch:
    def test_compose_only(self, client):
        response = client.post(
            f"{API}/runs",
            json={"selection": {"tag_filter": "Auth-SSO"}, "title": "Anmeldung prüfen", "labels": {"suite": "adhoc"}},
        )
        assert response.status_code == 201
        body = response.json()
        assert "dispatch" not in body and body["warnings"] == []
        run = body["run"]
        assert (run["state"], run["title"], run["origin"]) == ("COMPOSED", "Anmeldung prüfen", "api")
        assert run["labels"] == {"suite": "adhoc"} and run["planned_count"] == 3
        assert run["mode"] is None and run["duration_s"] is None and run["verdict_summary"] == {}
        assert client.get(f"{API}/runs/{run['id']}/items").json() == {"item_ids": SSO, "count": 3}

    def test_a_dispatched_run_comes_back_before_it_finishes(self, gated):
        """The request is answered while the run executes — here the runner
        cannot finish until the test opens its gate, so the answer provably
        came first. The client then follows the run through its events."""
        client, _service, started, release = gated
        response = client.post(
            f"{API}/runs", json={"selection": {"tag_filter": "Auth-SSO"}, "dispatch": {"runner": "gated"}}
        )
        assert response.status_code == 201
        body = response.json()
        run_id = body["run"]["id"]
        assert body["run"]["state"] == "RUNNING", "the hand-off happened; the execution has not ended"
        assert body["run"]["completion"] is None and body["run"]["verdict_summary"] == {}
        assert body["dispatch"]["mode"] == "gated" and body["dispatch"]["executing"] is True
        assert started.is_set() and not release.is_set()
        in_flight = client.get(f"{API}/runs/{run_id}").json()
        assert in_flight["state"] == "RUNNING" and in_flight["verdicts"] == []
        assert in_flight["shards"] == [
            {"shard": "1", "declared": True, "delivered": False, "count": 0, "summary": {}, "completion": None}
        ]

        release.set()
        events = follow(client, run_id, until="run.completed")
        assert [e["kind"] for e in events if e["kind"].startswith("run.")] == [
            "run.composed", "run.dispatched", "run.running", "run.completed",
        ]
        final = client.get(f"{API}/runs/{run_id}").json()
        assert (final["state"], final["completion"]) == ("COMPLETE", "PASS")
        assert final["verdict_summary"] == {"PASS": 3} and final["dispatches"][0]["executing"] is False

    def test_compose_and_dispatch_with_per_run_parameters(self, make_client):
        client, _service = make_client(runners={"demo": {"delay_s": 0.02}})
        response = client.post(
            f"{API}/runs",
            json={
                "selection": {"tag_filter": "Auth-SSO"},
                "labels": {"suite": "nightly"},
                "dispatch": {"runner": "demo", "parameters": {"seed": "a"}},
            },
        )
        assert response.status_code == 201
        body = response.json()
        run_id = body["run"]["id"]
        assert body["dispatch"]["mode"] == "demo" and body["dispatch"]["parameters"] == {"seed": "a"}
        assert body["run"]["labels"] == {"suite": "nightly"}
        # The accepted values are part of the document (§3: runner.*).
        assert client.get(f"{API}/runs/{run_id}/spec").json()["runner"] == {"demo": {"seed": "a"}}
        follow(client, run_id, until="run.completed")
        final = client.get(f"{API}/runs/{run_id}").json()
        assert (final["state"], final["completion"], final["verdict_summary"]) == ("COMPLETE", "PASS", {"PASS": 3})

    def test_live_status_is_visible_while_the_run_is_in_flight(self, make_client):
        # 46 items at 0.2 s each: the run is cancelled below, long before its end.
        client, service = make_client(runners={"demo": {"delay_s": 0.2}})
        response = client.post(
            f"{API}/runs",
            json={"selection": {"tag_filter": "Regression"}, "dispatch": {"runner": "demo", "parameters": {"seed": "a"}}},
        )
        run_id = response.json()["run"]["id"]

        def live_rows() -> list[dict]:
            detail = client.get(f"{API}/runs/{run_id}").json()
            return [row for row in detail["verdicts"] if row["live"]] if detail["state"] == "RUNNING" else []

        rows = wait_until(live_rows, what="live rows of a run in flight")
        assert set(rows[0]) == {"item_id", "status", "duration_ms", "message", "shard", "attempt", "live"}
        assert rows[0]["shard"] == "1"
        client.post(f"{API}/runs/{run_id}/cancel")
        follow(client, run_id, until="run.cancelled")
        service.dispatcher.join(30)

    def test_dispatching_a_stored_run_and_dispatching_it_again(self, client, make_client):
        run_id = client.post(f"{API}/runs", json={"selection": {"tag_filter": "Auth-SSO"}}).json()["run"]["id"]
        first = client.post(f"{API}/runs/{run_id}/dispatch", json={"runner": "demo", "parameters": {"seed": "b"}})
        assert first.status_code == 202
        follow(client, run_id, until="run.completed")
        assert client.get(f"{API}/runs/{run_id}").json()["completion"] == "FAIL"

        cursor = client.get(f"{API}/runs/{run_id}/events").json()["last_seq"]
        second = client.post(f"{API}/runs/{run_id}/dispatch", json={"runner": "demo", "parameters": {"seed": "a"}})
        assert second.status_code == 202
        events = follow(client, run_id, until="run.completed", after=cursor)
        assert [e["data"]["completion"] for e in events if e["kind"] == "run.completed"] == ["PASS"]
        detail = client.get(f"{API}/runs/{run_id}").json()
        assert [d["completion"] for d in detail["dispatches"]] == ["FAIL", "PASS"]
        assert detail["dispatch_count"] == 2
        # ?dispatch= shows an earlier dispatch's verdicts.
        earlier = client.get(f"{API}/runs/{run_id}", params={"dispatch": detail["dispatches"][0]["dispatch_id"]}).json()
        assert sorted(row["status"] for row in earlier["verdicts"]) == ["FAIL", "FAIL", "PASS"]
        assert_error(client.get(f"{API}/runs/{run_id}", params={"dispatch": "01NOPE"}), 404, "not-found")

    def test_a_second_dispatch_while_one_is_in_flight_is_a_409(self, gated):
        client, service, started, _release = gated
        run_id = compose(service, "Auth-SSO")
        assert client.post(f"{API}/runs/{run_id}/dispatch", json={"runner": "gated"}).status_code == 202
        assert started.wait(30)
        again = client.post(f"{API}/runs/{run_id}/dispatch", json={"runner": "demo"})
        assert_error(again, 409, "run-active", detail="already has a dispatch in flight")
        assert_error(client.post(f"{API}/runs/{run_id}/finalize"), 409, "run-active")
        assert_error(client.delete(f"{API}/runs/{run_id}"), 409, "run-active", detail="cancel it before deleting")
        assert_error(client.post(f"{API}/runs/{run_id}/dispatch", json={"mode": "export"}), 409, "run-active")

    def test_dispatch_of_an_unknown_run_is_a_404(self, client):
        assert_error(client.post(f"{API}/runs/01NOPE/dispatch", json={"runner": "demo"}), 404, "unknown-run")
        assert_error(client.post(f"{API}/runs/01NOPE/cancel"), 404, "unknown-run")
        assert_error(client.post(f"{API}/runs/01NOPE/finalize"), 404, "unknown-run")
        assert_error(client.post(f"{API}/runs/01NOPE/rerun", json={}), 404, "unknown-run")
        assert_error(client.delete(f"{API}/runs/01NOPE"), 404, "unknown-run")
        assert_error(client.get(f"{API}/runs/01NOPE/spec"), 404, "unknown-run")
        assert_error(client.get(f"{API}/runs/01NOPE/items"), 404, "unknown-run")
        assert_error(client.get(f"{API}/runs/01NOPE/events"), 404, "unknown-run")
        assert_error(client.get(f"{API}/runs/01NOPE/verdicts"), 404, "unknown-run")


class TestCancelRerunFinalizeDelete:
    def test_cancelling_a_run_in_flight(self, gated):
        client, service, started, _release = gated
        run_id = compose(service, "Auth-SSO")
        client.post(f"{API}/runs/{run_id}/dispatch", json={"runner": "gated"})
        assert started.wait(30)

        response = client.post(f"{API}/runs/{run_id}/cancel")
        assert response.status_code == 202
        assert response.json()["dispatch"]["cancel_requested"] is True
        events = follow(client, run_id, until="run.cancelled")
        kinds = [event["kind"] for event in events]
        assert kinds.index("run.cancel_requested") < kinds.index("run.cancelled")
        detail = client.get(f"{API}/runs/{run_id}").json()
        assert detail["state"] == "CANCELLED" and detail["completion"] is None
        # Nothing left to cancel.
        assert_error(client.post(f"{API}/runs/{run_id}/cancel"), 409, "conflict", detail="nothing in flight")

    def test_cancelling_an_export_that_nobody_answered(self, client, make_client):
        run_id = client.post(f"{API}/runs", json={"selection": {"tag_filter": "Auth-SSO"}}).json()["run"]["id"]
        export(client, run_id)
        response = client.post(f"{API}/runs/{run_id}/cancel")
        assert response.status_code == 202 and response.json()["run"]["state"] == "CANCELLED"

    def test_rerun_all_dispatches_the_same_run_again(self, make_client):
        client, service = make_client()
        run_id = finished_run(service, seed="b")
        response = client.post(f"{API}/runs/{run_id}/rerun", json={"scope": "all"})
        assert response.status_code == 201
        body = response.json()
        assert body["source_run_id"] == run_id and body["run"]["id"] == run_id
        assert body["dispatch"]["parameters"] == {"seed": "b"}, "same runner, same parameters"
        service.dispatcher.join(30)
        assert client.get(f"{API}/runs/{run_id}").json()["dispatch_count"] == 2

    def test_rerun_failed_composes_a_new_run_from_what_did_not_pass(self, make_client):
        client, service = make_client()
        run_id = finished_run(service, seed="b", labels={"suite": "nightly"})  # 2 FAIL, 1 PASS
        response = client.post(
            f"{API}/runs/{run_id}/rerun", json={"scope": "failed", "parameters": {"seed": "a"}}
        )
        assert response.status_code == 201
        body = response.json()
        new_id = body["run"]["id"]
        assert new_id != run_id and body["source_run_id"] == run_id
        assert body["run"]["planned_count"] == 2 and body["run"]["labels"] == {"suite": "nightly"}
        follow(client, new_id, until="run.completed")
        detail = client.get(f"{API}/runs/{new_id}").json()
        assert (detail["completion"], detail["origin"]) == ("PASS", "rerun")
        provenance = detail["selection"]["derived_from"][0]
        assert provenance["resolved_run_id"] == run_id
        assert provenance["query"]["verdicts"] == ["FAIL", "ERROR", "MISSING"]

    def test_rerun_failed_of_a_green_run_is_a_400_and_composes_nothing(self, make_client):
        client, service = make_client()
        run_id = finished_run(service, seed="a")
        assert_error(client.post(f"{API}/runs/{run_id}/rerun", json={"scope": "failed"}), 400, "invalid-request", detail="nothing to rerun")
        assert client.get(f"{API}/runs").json()["total"] == 1
        assert_error(client.post(f"{API}/runs/{run_id}/rerun", json={"scope": "some"}), 400, "invalid-request")

    def test_finalize_closes_an_awaiting_run_as_it_stands(self, client):
        run_id = client.post(f"{API}/runs", json={"selection": {"tag_filter": "Auth-SSO"}}).json()["run"]["id"]
        assert_error(client.post(f"{API}/runs/{run_id}/finalize"), 409, "conflict", detail="nothing to finalize")
        export(client, run_id)
        body = client.post(f"{API}/runs/{run_id}/finalize").json()
        assert (body["state"], body["completion"]) == ("COMPLETE", "INCOMPLETE")
        assert body["verdict_summary"] == {"MISSING": 3}

    def test_delete_removes_the_run_and_its_artifacts(self, make_client, tmp_path):
        client, service = make_client()
        run_id = finished_run(service)
        artifact_dir = tmp_path / "artifacts" / run_id
        (artifact_dir / "d").mkdir(parents=True)
        (artifact_dir / "d" / "log.html").write_text("<html/>", encoding="utf-8")
        response = client.delete(f"{API}/runs/{run_id}")
        assert response.status_code == 204 and response.content == b""
        assert_error(client.get(f"{API}/runs/{run_id}"), 404, "unknown-run")
        assert not artifact_dir.exists()
        assert service.store.events(run_id) == []


# -- listing ---------------------------------------------------------------------------------


class TestRunList:
    @pytest.fixture()
    def listing(self, make_client):
        client, service = make_client()
        ids = {
            "nightly": finished_run(service, "Auth-SSO", "b", title="Nightly regression", labels={"suite": "nightly", "env": "staging"}),
            "payments": finished_run(service, "Payments-Refunds", "demo", title="Zahlungen prüfen", labels={"suite": "nightly"}),
            "composed": compose(service, "Checkout", title="Checkout draft", labels={"suite": "adhoc"}),
        }
        exported = compose(service, "Cart", title="Cart export")
        service.export(exported)
        ids["exported"] = exported
        return client, ids

    def test_rows_are_complete_without_a_second_request(self, listing):
        client, ids = listing
        body = client.get(f"{API}/runs").json()
        assert (body["total"], body["limit"], body["offset"]) == (4, 50, 0)
        rows = {row["id"]: row for row in body["runs"]}
        assert set(rows) == set(ids.values())
        nightly = rows[ids["nightly"]]
        assert set(nightly) >= {
            "id", "title", "created_at", "origin", "labels", "state", "completion", "completed_at",
            "planned_count", "verdict_summary", "mode", "duration_s", "dispatch_count", "dispatches",
        }
        assert (nightly["state"], nightly["completion"], nightly["mode"]) == ("COMPLETE", "FAIL", "demo")
        assert nightly["verdict_summary"] == {"FAIL": 2, "PASS": 1} and nightly["planned_count"] == 3
        assert nightly["labels"] == {"suite": "nightly", "env": "staging"} and nightly["origin"] == "test"
        assert isinstance(nightly["duration_s"], int) and nightly["duration_s"] >= 0
        assert nightly["dispatches"][0]["mode"] == "demo"
        composed = rows[ids["composed"]]
        assert (composed["state"], composed["mode"], composed["duration_s"]) == ("COMPOSED", None, None)
        assert composed["planned_count"] == 13 and composed["dispatches"] == []
        assert rows[ids["exported"]]["mode"] == "export"
        assert rows[ids["exported"]]["state"] == "AWAITING_RESULTS"

    def test_paging_keeps_the_total(self, listing):
        client, _ids = listing
        everything = [row["id"] for row in client.get(f"{API}/runs").json()["runs"]]
        first = client.get(f"{API}/runs", params={"limit": 3}).json()
        rest = client.get(f"{API}/runs", params={"limit": 3, "offset": 3}).json()
        assert first["total"] == rest["total"] == 4
        assert [r["id"] for r in first["runs"]] + [r["id"] for r in rest["runs"]] == everything
        assert client.get(f"{API}/runs", params={"limit": 0}).json() == {"runs": [], "total": 4, "limit": 0, "offset": 0}
        assert client.get(f"{API}/runs", params={"offset": 99}).json()["runs"] == []

    @pytest.mark.parametrize("params", [{"limit": -1}, {"limit": 501}, {"offset": -1}, {"limit": "many"}])
    def test_paging_out_of_range_is_a_400(self, listing, params):
        client, _ids = listing
        assert_error(client.get(f"{API}/runs", params=params), 400, "invalid-request")

    def test_filters(self, listing):
        client, ids = listing

        def found(**params: Any) -> set[str]:
            body = client.get(f"{API}/runs", params=params).json()
            assert body["total"] == len(body["runs"])
            return {row["id"] for row in body["runs"]}

        assert found(state="COMPLETE") == {ids["nightly"], ids["payments"]}
        assert found(state="COMPOSED") == {ids["composed"]}
        assert found(label="suite=nightly") == {ids["nightly"], ids["payments"]}
        assert found(label=["suite=nightly", "env=staging"]) == {ids["nightly"]}
        assert found(label="suite=weekly") == set()
        assert found(q="CHECKOUT") == {ids["composed"]}
        assert found(q="prüf") == {ids["payments"]}, "title search folds case beyond ASCII"
        assert found(q="PRÜFEN") == {ids["payments"]}
        assert found(q="e", state="COMPLETE", label="suite=nightly") == {ids["nightly"], ids["payments"]}

    def test_time_window_accepts_offsets_and_refuses_what_is_not_a_time(self, listing):
        client, ids = listing
        everything = set(ids.values())

        def found(**params: Any) -> set[str]:
            return {row["id"] for row in client.get(f"{API}/runs", params=params).json()["runs"]}

        assert found(since="2000-01-01T00:00:00Z") == everything
        assert found(since="2000-01-01T02:00:00+02:00") == everything
        assert found(until="2000-01-01T00:00:00Z") == set()
        assert found(since="2999-01-01T00:00:00-05:00") == set()
        assert found(since="2000-01-01", until="2999-12-31T23:59:59Z") == everything
        assert_error(client.get(f"{API}/runs", params={"since": "yesterday"}), 400, "invalid-request", detail="not an ISO-8601 time")
        assert_error(client.get(f"{API}/runs", params={"until": "12/31/2026"}), 400, "invalid-request")

    def test_an_unknown_state_or_a_malformed_label_is_a_400(self, listing):
        client, _ids = listing
        assert_error(client.get(f"{API}/runs", params={"state": "DONE"}), 400, "invalid-request", detail="unknown run state")
        assert_error(client.get(f"{API}/runs", params={"label": "suite"}), 400, "invalid-request", detail="key=value")


# -- one run in full ---------------------------------------------------------------------------


class TestRunDetail:
    def test_everything_a_reader_needs_is_in_the_payload(self, make_client, tmp_path):
        client, service = make_client()
        run_id = compose(
            service, {"tag_filter": {"op": "AND", "items": ["Auth", {"not": "prefix:Quarantine-"}]}, "item_ids": SSO},
            title="Anmeldung prüfen", labels={"suite": "nightly"},
        )
        outcome = service.dispatch(run_id, "demo", {"seed": "b"}, foreground=True)
        log = tmp_path / "artifacts" / run_id / outcome.dispatch_id / "1" / "log.html"
        log.parent.mkdir(parents=True)
        log.write_text("<html>log</html>", encoding="utf-8")
        service.store.add_artifact_ref(
            run_id, outcome.dispatch_id, name="log.html (1)", media_type="text/html", url_or_path=str(log), shard="1"
        )
        service.store.add_artifact_ref(
            run_id, outcome.dispatch_id, name="Build", media_type="text/html", url_or_path="https://ci.shop.example/job/7/"
        )

        body = client.get(f"{API}/runs/{run_id}").json()
        json.dumps(body)
        assert (body["state"], body["completion"], body["dispatch_id"]) == ("COMPLETE", "FAIL", outcome.dispatch_id)
        assert len(body["verdicts"]) == 3
        assert set(body["verdicts"][0]) == {"item_id", "status", "duration_ms", "message", "shard", "attempt", "live"}
        assert (body["shard_groups"], body["groups"], body["live_summary"], body["rerun_of"]) == ({}, [], {}, None)
        failing = [row for row in body["verdicts"] if row["status"] == "FAIL"]
        assert len(failing) == 2 and failing[0]["duration_ms"] > 0 and failing[0]["message"]
        assert {row["shard"] for row in body["verdicts"]} == {"1"}
        assert body["shards"] == [
            {"shard": "1", "declared": True, "delivered": True, "count": 3, "summary": {"FAIL": 2, "PASS": 1}, "completion": "FAIL"}
        ]
        assert body["artifacts"] == [
            {
                "name": "log.html (1)", "media_type": "text/html", "dispatch_id": outcome.dispatch_id,
                "shard": "1", "group": None, "url_or_path": str(log), "kind": "local",
                "href": f"/artifacts/{run_id}/{outcome.dispatch_id}/1/log.html",
            },
            {
                "name": "Build", "media_type": "text/html", "dispatch_id": outcome.dispatch_id,
                "shard": None, "group": None, "url_or_path": "https://ci.shop.example/job/7/", "kind": "url",
                "href": "https://ci.shop.example/job/7/",
            },
        ]
        assert [e["kind"] for e in body["events"]] == [
            "run.composed", "run.dispatched", "run.running", "delivery.recorded", "run.completed",
        ]
        assert body["last_event_seq"] == body["events"][-1]["seq"]
        # The selection, so a client can load it back into a builder.
        assert body["selection"] == {
            "tag_filter": {"op": "AND", "items": ["Auth", {"not": "prefix:Quarantine-"}]},
            "item_ids": SSO,
            "derived_from": [],
        }
        assert body["expect_format"] == "runcomposer-verdicts"
        assert body["deliveries"][0]["shard"] == "1" and body["deliveries"][0]["format"] == "runcomposer-verdicts"
        dispatch = body["dispatches"][0]
        assert set(dispatch) >= {
            "dispatch_id", "mode", "state", "created_at", "shards", "declared_shards", "completion",
            "completed_at", "summary", "error", "plan", "links", "parameters", "spec_sha256",
            "cancel_requested", "executing",
        }
        assert dispatch["shards"] == ["1"] and "simulating 3 item(s)" in dispatch["plan"]

    def test_fan_out_reads_green_here_red_there(self, make_client):
        client, service = make_client()
        run_id = compose(service, "Auth-SSO")
        dispatch = service.ledger.open(run_id, "ingest", state="AWAITING_RESULTS", shards=["env1", "env2"])
        service.ingestion.record(
            dispatch, "env1", [Verdict(i, "PASS", 10) for i in SSO], format_id="runcomposer-verdicts"
        )
        service.ingestion.record(
            service.store.get_dispatch(dispatch.dispatch_id), "env2",
            [Verdict(SSO[0], "PASS", 10), Verdict(SSO[1], "FAIL", 10, "declined"), Verdict(SSO[2], "PASS", 10)],
            format_id="runcomposer-verdicts",
        )
        body = client.get(f"{API}/runs/{run_id}").json()
        assert body["completion"] == "FAIL" and len(body["verdicts"]) == 6
        by_shard = {entry["shard"]: entry for entry in body["shards"]}
        assert (by_shard["env1"]["completion"], by_shard["env2"]["completion"]) == ("PASS", "FAIL")
        assert by_shard["env2"]["summary"] == {"PASS": 2, "FAIL": 1}

        only = client.get(f"{API}/runs/{run_id}/verdicts", params={"shard": "env2", "status": "FAIL"}).json()
        assert only["total"] == 1 and only["verdicts"][0]["item_id"] == SSO[1]
        assert only["verdicts"][0]["message"] == "declined"

    def test_verdicts_are_paged_and_filtered(self, make_client):
        client, service = make_client()
        run_id = finished_run(service, "Regression", "demo")
        everything = client.get(f"{API}/runs/{run_id}/verdicts", params={"limit": 5000}).json()
        assert everything["total"] == 46 == len(everything["verdicts"])
        page = client.get(f"{API}/runs/{run_id}/verdicts", params={"limit": 10, "offset": 40}).json()
        assert (page["total"], len(page["verdicts"]), page["limit"], page["offset"]) == (46, 6, 10, 40)
        assert page["verdicts"] == everything["verdicts"][40:]
        failed = client.get(f"{API}/runs/{run_id}/verdicts", params={"status": "FAIL"}).json()
        assert failed["total"] >= 1 and {row["status"] for row in failed["verdicts"]} == {"FAIL"}
        both = client.get(f"{API}/runs/{run_id}/verdicts", params={"status": ["FAIL", "SKIP"]}).json()
        assert {row["status"] for row in both["verdicts"]} <= {"FAIL", "SKIP"} and both["total"] >= failed["total"]
        assert both["dispatch_id"] == everything["dispatch_id"]
        assert_error(client.get(f"{API}/runs/{run_id}/verdicts", params={"status": "RED"}), 400, "invalid-request", detail="unknown verdict status")
        assert_error(client.get(f"{API}/runs/{run_id}/verdicts", params={"dispatch": "01NOPE"}), 404, "not-found")

    def test_missing_items_are_rows_once_the_dispatch_is_over(self, make_client):
        client, service = make_client()
        run_id = compose(service, "Auth-SSO")
        service.export(run_id)
        assert client.get(f"{API}/runs/{run_id}/verdicts").json()["total"] == 0
        service.finalize(run_id)
        missing = client.get(f"{API}/runs/{run_id}/verdicts", params={"status": "MISSING"}).json()
        assert [row["item_id"] for row in missing["verdicts"]] == SSO
        assert {row["message"] for row in missing["verdicts"]} == {"not-reported"}

    def test_the_event_stream_has_a_cursor(self, make_client):
        client, service = make_client()
        run_id = finished_run(service)
        everything = client.get(f"{API}/runs/{run_id}/events").json()
        assert len(everything["events"]) == 5 and everything["last_seq"] == everything["events"][-1]["seq"]
        assert set(everything["events"][0]) == {"seq", "run_id", "at", "kind", "dispatch_id", "data"}
        third = everything["events"][2]["seq"]
        after = client.get(f"{API}/runs/{run_id}/events", params={"after": third}).json()
        assert [e["seq"] for e in after["events"]] == [e["seq"] for e in everything["events"][3:]]
        limited = client.get(f"{API}/runs/{run_id}/events", params={"limit": 2}).json()
        assert len(limited["events"]) == 2 and limited["last_seq"] == limited["events"][-1]["seq"]
        # Nothing new: the cursor comes back unchanged, at once and after a wait.
        last = everything["last_seq"]
        assert client.get(f"{API}/runs/{run_id}/events", params={"after": last}).json() == {"events": [], "last_seq": last}
        assert client.get(f"{API}/runs/{run_id}/events", params={"after": last, "wait": 0.05}).json()["events"] == []

    def test_the_long_poll_returns_when_an_event_arrives(self, gated):
        client, service, started, release = gated
        run_id = compose(service, "Auth-SSO")
        client.post(f"{API}/runs/{run_id}/dispatch", json={"runner": "gated"})
        assert started.wait(30)
        cursor = client.get(f"{API}/runs/{run_id}/events").json()["last_seq"]
        with ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(
                lambda: client.get(f"{API}/runs/{run_id}/events", params={"after": cursor, "wait": 25}).json()
            )
            release.set()
            body = waiting.result(timeout=30)
        assert body["events"] and body["events"][0]["kind"] == "delivery.recorded"

    def test_the_stored_spec_in_both_formats(self, make_client):
        import yaml

        client, service = make_client()
        run_id = compose(service, "Auth-SSO", title="Anmeldung prüfen")
        as_json = client.get(f"{API}/runs/{run_id}/spec").json()
        assert as_json["run"]["id"] == run_id and as_json["runspec"] == "1.0"
        as_yaml = client.get(f"{API}/runs/{run_id}/spec", params={"format": "yaml"})
        assert as_yaml.headers["content-type"].startswith("application/yaml")
        assert yaml.safe_load(as_yaml.text) == as_json and "prüfen" in as_yaml.text
        assert_error(client.get(f"{API}/runs/{run_id}/spec", params={"format": "toml"}), 400, "invalid-request")


# -- what a client asks first ------------------------------------------------------------------


class TestMeta:
    def test_health(self, client):
        assert client.get(f"{API}/health").json() == {"status": "ok", "version": __version__}

    def test_meta_is_everything_a_client_needs_at_boot(self, make_client):
        client, _service = make_client(
            core={"locale_default": "de"}, ui={"quick_filters": ["Smoke", "Regression"]}
        )
        body = client.get(f"{API}/meta").json()
        assert body["version"] == __version__ and body["locale_default"] == "de"
        assert (body["catalog"]["items"], body["catalog"]["tags"]) == (60, 47)
        assert body["catalog"]["snapshot"].startswith("sha256:") and body["catalog"]["sources"] == ["demo-corpus"]
        assert body["taxonomy"] == {"configured": False}
        assert body["ingestion"] == {"tokens": "required", "inbox": False}
        assert body["ui"] == {"quick_filters": [{"label": "Smoke", "filter": "Smoke", "tag_filter": "Smoke", "text": "Smoke", "kind": "pattern"}, {"label": "Regression", "filter": "Regression", "tag_filter": "Regression", "text": "Regression", "kind": "pattern"}]}
        assert "COMPLETE" in body["states"] and "INCOMPLETE" in body["completions"]
        (runner,) = body["runners"]
        assert (runner["id"], runner["plugin"]) == ("demo", "demo")
        assert "simulated" in runner["capabilities"]
        assert runner["parameters"] == [
            {
                "name": "seed", "kind": "string", "label": "Seed", "default": "demo", "choices": [],
                "required": False, "pattern": None,
                "help": "Same seed, same simulated verdicts; change it to see results flip.",
            }
        ]
        assert "health" not in runner, "meta does not probe"
        plugins = {(p["group"], p["name"]): p for p in body["plugins"]}
        robot_pool = plugins[("runcomposer.runners", "robot-pool")]
        assert (robot_pool["distribution"], robot_pool["version"]) == ("runcomposer", __version__)
        assert ("runcomposer.observers", "webhook") in plugins and ("runcomposer.parsers", "junit-xml") in plugins

    def test_meta_says_when_a_taxonomy_is_configured(self, make_client, tmp_path):
        (tmp_path / "tree.yaml").write_text("taxonomy:\n  - {label: Payments, filter: Payments}\n", encoding="utf-8")
        client, _service = make_client(core={"taxonomy_file": "tree.yaml"})
        assert client.get(f"{API}/meta").json()["taxonomy"] == {"configured": True}
        assert client.get(f"{API}/taxonomy").json() == {"taxonomy": [{"label": "Payments", "filter": "Payments"}]}

    def test_ui_config(self, make_client):
        client, _service = make_client(ui={"quick_filters": ["Smoke"]})
        assert client.get(f"{API}/ui-config").json() == {
            "locale_default": "en", "quick_filters": [{"label": "Smoke", "filter": "Smoke", "tag_filter": "Smoke", "text": "Smoke", "kind": "pattern"}],
        }

    def test_runners_are_listed_under_their_config_key_with_cached_health(self, gated):
        """The id a runner is dispatched by is the key it is configured
        under — not whatever its plugin calls itself."""
        client, _service, _started, _release = gated
        HEALTH_PROBES.clear()
        first = client.get(f"{API}/runners").json()
        assert [(r["id"], r["plugin"]) for r in first] == [("gated", "gated"), ("demo", "demo")]
        assert first[0]["health"]["status"] == "ok" and first[0]["health"]["detail"] == "gate is in place"
        assert first[0]["capabilities"] == ["cancel"] and first[0]["parameters"] == []
        assert first[1]["health"] == {"status": "ok", "detail": "simulated — always available", "data": {}}
        assert len(HEALTH_PROBES) == 1
        client.get(f"{API}/runners")
        assert len(HEALTH_PROBES) == 1, "health is cached between requests"
        refreshed = client.get(f"{API}/runners", params={"refresh": "true"}).json()
        assert len(HEALTH_PROBES) == 2 and refreshed[0]["health"]["data"] == {"probes": 2}

    def test_catalog_tags(self, client):
        body = client.get(f"{API}/catalog/tags").json()
        assert body["items"] == 60 and len(body["tags"]) == 47
        counts = {entry["tag"]: entry["count"] for entry in body["tags"]}
        assert (counts["Payments"], counts["Regression"], counts["Auth-SSO"]) == (17, 46, 3)
        assert [entry["tag"] for entry in body["tags"]] == sorted(counts)

    def test_catalog_reload_picks_up_an_edited_manifest(self, make_client, tmp_path):
        """Regression: the catalog used to be frozen at process start."""
        manifest = tmp_path / "corpus.json"
        items = [{"id": f"Shop.Gift.T00{n}", "tags": ["Checkout-Gift"]} for n in (1, 2)]
        manifest.write_text(json.dumps({"items": items}), encoding="utf-8")
        client, _service = make_client(sources={"manifest": {"path": "corpus.json"}})
        before = client.post(f"{API}/selection/compile", json={"tag_filter": "Checkout-Gift"}).json()
        assert (before["count"], before["total"]) == (2, 2)

        items.append({"id": "Shop.Gift.T003", "tags": ["Checkout-Gift", "Smoke"]})
        manifest.write_text(json.dumps({"items": items}), encoding="utf-8")
        reloaded = client.post(f"{API}/catalog/reload").json()
        assert reloaded["reloaded"] is True and reloaded["items"] == 3
        assert reloaded["snapshot"] != before["snapshot"]
        after = client.post(f"{API}/selection/compile", json={"tag_filter": "Checkout-Gift"}).json()
        assert (after["count"], after["total"]) == (3, 3)
        assert client.post(f"{API}/catalog/reload").json()["reloaded"] is False

    def test_taxonomy_plain_and_resolved(self, client):
        plain = client.get(f"{API}/taxonomy").json()
        assert [node["label"] for node in plain["taxonomy"]][:2] == ["Areas", "Suites"]
        assert "resolved" not in plain
        resolved = client.get(f"{API}/taxonomy", params={"resolve": "true"}).json()
        assert resolved["resolved"]["tags_total"] == 47 and resolved["taxonomy"]

    def test_a_taxonomy_broken_under_a_running_server_is_a_500_that_explains(self, make_client, tmp_path):
        tree = tmp_path / "tree.yaml"
        tree.write_text("taxonomy:\n  - {label: Payments, filter: Payments}\n", encoding="utf-8")
        client, _service = make_client(core={"taxonomy_file": "tree.yaml"})
        assert client.get(f"{API}/taxonomy").status_code == 200
        tree.write_text("groups: []\n", encoding="utf-8")
        assert_error(client.get(f"{API}/taxonomy"), 500, "taxonomy-invalid", detail="no top-level 'taxonomy' key")


class TestStartup:
    """§8: a deployment that is not startable refuses to start."""

    def _app(self, tmp_path: Path, **sections: Any) -> Any:
        return create_app(load_config(str(write_config(tmp_path, **sections))))

    def test_an_unknown_plugin_refuses_the_start(self, tmp_path):
        with pytest.raises(ConfigError, match=r"runners\.nonexistent"):
            self._app(tmp_path, runners={"nonexistent": {}})
        with pytest.raises(ConfigError, match=r"store\.oracle"):
            self._app(tmp_path, store={"oracle": {}})

    def test_a_mistyped_option_refuses_the_start_naming_the_accepted_ones(self, tmp_path):
        """Regression: this used to boot and then answer 500 at dispatch."""
        with pytest.raises(ConfigError) as refused:
            self._app(tmp_path, runners={"demo": {"sead": "x"}})
        assert "runners.demo: unknown option(s) ['sead']" in str(refused.value)
        assert "accepts: ['delay_s', 'seed']" in str(refused.value)

    def test_a_broken_taxonomy_refuses_the_start(self, tmp_path):
        (tmp_path / "tree.yaml").write_text("nodes:\n  - {label: A}\n", encoding="utf-8")
        with pytest.raises(TaxonomyError, match="no top-level 'taxonomy' key"):
            self._app(tmp_path, core={"taxonomy_file": "tree.yaml"})


# -- selection preview ---------------------------------------------------------------------------


class TestSelection:
    def test_compile_previews_with_the_catalog_size(self, client):
        body = client.post(f"{API}/selection/compile", json={"tag_filter": "Auth-SSO"}).json()
        assert (body["count"], body["total"], body["warnings"], body["derived_from"]) == (3, 60, [], [])
        assert body["snapshot"].startswith("sha256:")
        assert body["items"][0] == {
            "id": "Shop.Auth.SSO.T001", "name": body["items"][0]["name"],
            "tags": ["Auth", "Auth-SSO", "Smoke", "Sprint-12", "SHOP-1257"],
            "hierarchy": ["Shop", "Auth", "SSO"],
        }
        limited = client.post(f"{API}/selection/compile", json={"tag_filter": "Regression", "limit": 5}).json()
        assert (limited["count"], len(limited["items"])) == (46, 5)
        nothing = client.post(f"{API}/selection/compile", json={"tag_filter": "regex:^NoSuchTagAnywhere$"}).json()
        assert nothing["count"] == 0 and nothing["warnings"] == ["selection matched 0 items"]

    def test_picks_intersect_with_the_filter(self, client):
        body = client.post(
            f"{API}/selection/compile", json={"tag_filter": "Auth", "item_ids": [SSO[0], "Shop.Payments.Cards.T001"]}
        ).json()
        assert [item["id"] for item in body["items"]] == [SSO[0]]

    def test_an_unknown_field_is_refused(self, client):
        assert_error(client.post(f"{API}/selection/compile", json={"tag_filtr": "Auth"}), 400, "invalid-request")

    def test_history_selection_reports_which_run_it_resolved_to(self, make_client):
        """The scope is not decoration: unscoped, "latest" is a
        one-off run of something else."""
        client, service = make_client()
        nightly = finished_run(service, "Auth-SSO", "b", title="Nightly regression", labels={"suite": "nightly"})
        adhoc = finished_run(service, "Payments-Refunds", "a", title="A one-off catalog check", labels={"suite": "adhoc"})

        unscoped = client.post(f"{API}/selection/compile", json={"history": "failed@latest"}).json()
        assert unscoped["derived_from"][0]["resolved_run_id"] == adhoc and unscoped["count"] == 1

        inline = client.post(f"{API}/selection/compile", json={"history": "failed@latest?suite=nightly"}).json()
        structured = client.post(
            f"{API}/selection/compile", json={"history": {"query": "failed@latest", "labels": {"suite": "nightly"}}}
        ).json()
        for body in (inline, structured):
            (provenance,) = body["derived_from"]
            assert provenance["resolved_run_id"] == nightly
            assert provenance["resolved_run_title"] == "Nightly regression"
            assert provenance["resolved_run_labels"] == {"suite": "nightly"}
            assert provenance["query"] == {"run": "LATEST", "verdicts": ["FAIL"], "labels": {"suite": "nightly"}}
            assert provenance["resolved_run_completed_at"].endswith("Z")
            assert (body["count"], body["total"]) == (2, 60)
        assert inline["items"] == structured["items"]

        by_run = client.post(f"{API}/selection/compile", json={"history": f"passed@run:{nightly}"}).json()
        assert by_run["count"] == 1 and by_run["derived_from"][0]["query"]["run"] == nightly

    def test_history_errors_are_400s(self, make_client):
        client, service = make_client()
        dark = client.post(f"{API}/selection/compile", json={"history": "failed@latest"})
        assert_error(dark, 400, "invalid-request", detail="history features are dark on a fresh store")
        run_id = finished_run(service, "Auth-SSO", "a")
        for query, detail in [
            ("failed", "must be '<verdict>@<selector>'"),
            ("exploded@latest", "must be '<verdict>@<selector>'"),
            ("failed@whenever", "unknown history selector"),
            (f"failed@run:{run_id}?suite=nightly", "already names one run"),
            ("failed@latest?suite", "key=value"),
            ("failed@latest?suite=weekly", "matched no completed run scoped to labels"),
            ("failed@before:lunchtime", "not an ISO-8601 time"),
            ("failed@latest", "nothing to rerun"),
        ]:
            assert_error(client.post(f"{API}/selection/compile", json={"history": query}), 400, "invalid-request", detail=detail)
        conflicting = {"history": {"query": "failed@latest?suite=a", "labels": {"suite": "b"}}}
        assert_error(client.post(f"{API}/selection/compile", json=conflicting), 400, "invalid-request", detail="refusing to guess")

    def test_a_composed_history_run_carries_the_provenance(self, make_client):
        client, service = make_client()
        nightly = finished_run(service, "Auth-SSO", "b", labels={"suite": "nightly"})
        response = client.post(
            f"{API}/runs",
            json={"selection": {"history": "failed@latest?suite=nightly"}, "title": "Rerun", "labels": {"suite": "nightly"}},
        )
        assert response.status_code == 201
        spec = client.get(f"{API}/runs/{response.json()['run']['id']}/spec").json()
        assert spec["selection"]["materialized"]["count"] == 2
        assert spec["selection"]["derived_from"][0]["resolved_run_id"] == nightly
        assert spec["selection"]["derived_from"][0]["query"]["labels"] == {"suite": "nightly"}

    def test_preview_is_safe_to_call_concurrently(self, make_client):
        client, service = make_client()
        finished_run(service, "Auth-SSO", "b", labels={"suite": "nightly"})
        bodies = [
            {"tag_filter": "Regression"},
            {"tag_filter": {"op": "OR", "items": ["Payments", "prefix:Checkout-"]}},
            {"history": "failed@latest?suite=nightly"},
            {"item_ids": SSO},
        ]

        def call(index: int) -> tuple[int, int]:
            response = client.post(f"{API}/selection/compile", json=bodies[index % len(bodies)])
            return response.status_code, response.json()["count"]

        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(call, range(64)))
        assert {status for status, _count in results} == {200}
        assert [count for _status, count in results[:4]] == [46, 30, 2, 3]
        assert results == results[:4] * 16, "the same question always gets the same answer"
        assert client.get(f"{API}/runs").json()["total"] == 1, "previewing stores nothing"

    def test_spec_preview_renders_and_stores_nothing(self, client):
        response = client.post(
            f"{API}/selection/spec-preview",
            json={"selection": {"tag_filter": "Auth-SSO"}, "title": "Preview", "labels": {"suite": "adhoc"}, "expect_format": "junit-xml"},
        )
        assert response.status_code == 200
        body = response.json()
        spec = body["spec"]
        assert spec["run"]["title"] == "Preview" and spec["run"]["labels"] == {"suite": "adhoc"}
        assert spec["selection"]["materialized"]["item_ids"] == SSO
        assert spec["results"]["expect"] == [{"format": "junit-xml"}] and "token" not in spec["results"]
        assert body["derived_from"] == [] and body["warnings"] == []
        assert client.get(f"{API}/runs").json()["total"] == 0
        assert_error(client.get(f"{API}/runs/{spec['run']['id']}"), 404, "unknown-run")


# -- export: the exact bytes, and the token ----------------------------------------------------


class TestExport:
    def test_the_whole_round_trip_with_a_non_ascii_title(self, client, tmp_path):
        """Export → download the exact bytes → runcomposer-exec → push with
        the token → COMPLETE. A title outside ASCII is where a client that
        re-serialises the document breaks the marker hash; downloading the
        bytes cannot."""
        composed = client.post(
            f"{API}/runs", json={"selection": {"tag_filter": "Auth-SSO"}, "title": "Zahlungen prüfen"}
        ).json()
        run_id = composed["run"]["id"]
        exported = export(client, run_id)
        assert exported["run"]["state"] == "AWAITING_RESULTS"
        assert exported["dispatch"]["mode"] == "export" and exported["dispatch"]["shards"] == ["1"]
        assert exported["spec_media_type"] == "application/json"
        token = exported["ingest_token"]
        assert token.startswith("rct_") and "spec_document" not in exported

        download = client.get(exported["spec_url"])
        assert download.status_code == 200
        raw = download.content
        assert hashlib.sha256(raw).hexdigest() == exported["spec_sha256"] == exported["dispatch"]["spec_sha256"]
        assert download.headers["x-runcomposer-spec-sha256"] == exported["spec_sha256"]
        assert download.headers["etag"] == f'"{exported["spec_sha256"]}"'
        assert download.headers["content-type"].startswith("application/json")
        assert f"runspec-{run_id}.json" in download.headers["content-disposition"]
        assert "Zahlungen prüfen".encode() in raw
        # What a naive client would do — parse and re-serialise — is a different document.
        assert hashlib.sha256(json.dumps(json.loads(raw)).encode()).hexdigest() != exported["spec_sha256"]

        spec_file = tmp_path / "spec.json"
        spec_file.write_bytes(raw)
        out = tmp_path / "bundle"
        assert runcomposer_exec.main(
            [str(spec_file), "--out", str(out), "--simulate", "--seed", "a", "--dispatch", exported["dispatch"]["dispatch_id"]]
        ) == 0
        files = [
            ("files", (path.name, path.read_bytes(), "application/json"))
            for path in sorted(out.iterdir())
        ]
        pushed = client.post(f"{API}/runs/{run_id}/results", files=files, headers=bearer(token))
        assert pushed.status_code == 200, pushed.text
        assert pushed.json() == {
            "run_id": run_id, "dispatch_id": exported["dispatch"]["dispatch_id"], "shard": "1",
            "outcome": "new", "verdict_count": 3, "run_state": "COMPLETE", "completion": "PASS", "warnings": [],
        }
        assert client.get(f"{API}/runs/{run_id}").json()["completion"] == "PASS"

    def test_the_bytes_are_re_downloadable_and_never_carry_the_token(self, client):
        run_id = client.post(f"{API}/runs", json={"selection": {"tag_filter": "Auth-SSO"}}).json()["run"]["id"]
        exported = export(client, run_id)
        token = exported["ingest_token"].encode()
        first = client.get(exported["spec_url"]).content
        again = client.get(exported["spec_url"]).content
        assert first == again and token not in first
        # Nothing the read API serves contains it.
        for url in (
            f"{API}/runs/{run_id}", f"{API}/runs/{run_id}/spec", f"{API}/runs/{run_id}/spec?format=yaml",
            f"{API}/runs/{run_id}/events", f"{API}/runs", f"{API}/meta", exported["spec_url"],
        ):
            assert token not in client.get(url).content, url
        assert "token" not in client.get(f"{API}/runs/{run_id}/spec").json()["results"]

    def test_yaml_export_and_two_exports_of_one_run(self, client):
        import yaml

        run_id = client.post(f"{API}/runs", json={"selection": {"tag_filter": "Auth-SSO"}}).json()["run"]["id"]
        as_yaml = export(client, run_id, format="yaml")
        download = client.get(as_yaml["spec_url"])
        assert download.headers["content-type"].startswith("application/yaml")
        assert f"runspec-{run_id}.yaml" in download.headers["content-disposition"]
        assert yaml.safe_load(download.content)["run"]["id"] == run_id
        assert hashlib.sha256(download.content).hexdigest() == as_yaml["spec_sha256"]

        as_json = export(client, run_id)
        assert as_json["dispatch"]["dispatch_id"] != as_yaml["dispatch"]["dispatch_id"]
        assert as_json["ingest_token"] != as_yaml["ingest_token"]
        assert client.get(as_yaml["spec_url"]).content == download.content, "an earlier hand-out stays as it was"
        # Exporting is an action on ONE run: it never composes another.
        assert client.get(f"{API}/runs").json()["total"] == 1
        assert client.get(f"{API}/runs/{run_id}").json()["dispatch_count"] == 2

    def test_compose_and_export_in_one_request(self, client):
        response = client.post(
            f"{API}/runs", json={"selection": {"tag_filter": "Auth-SSO"}, "dispatch": {"mode": "export"}}
        )
        assert response.status_code == 201
        body = response.json()
        assert body["run"]["state"] == "AWAITING_RESULTS" and body["ingest_token"].startswith("rct_")
        assert hashlib.sha256(client.get(body["spec_url"]).content).hexdigest() == body["spec_sha256"]

    def test_a_runner_dispatch_has_no_document_to_download(self, make_client):
        client, service = make_client()
        run_id = finished_run(service)
        dispatch_id = client.get(f"{API}/runs/{run_id}").json()["dispatch_id"]
        assert_error(client.get(f"{API}/runs/{run_id}/dispatches/{dispatch_id}/spec"), 404, "not-found", detail="no handed-out document")
        assert_error(client.get(f"{API}/runs/{run_id}/dispatches/01NOPE/spec"), 404, "not-found")

    def test_rotating_the_token(self, client):
        run_id = client.post(f"{API}/runs", json={"selection": {"tag_filter": "Auth-SSO"}}).json()["run"]["id"]
        exported = export(client, run_id)
        dispatch_id = exported["dispatch"]["dispatch_id"]
        rotated = client.post(f"{API}/runs/{run_id}/dispatches/{dispatch_id}/token").json()
        assert rotated["dispatch_id"] == dispatch_id and rotated["ingest_token"] != exported["ingest_token"]
        old = client.post(f"{API}/runs/{run_id}/results", files=[results_file(SSO)], headers=bearer(exported["ingest_token"]))
        assert_error(old, 403, "token-rejected")
        new = client.post(f"{API}/runs/{run_id}/results", files=[results_file(SSO)], headers=bearer(rotated["ingest_token"]))
        assert new.status_code == 200 and new.json()["completion"] == "PASS"
        assert_error(client.post(f"{API}/runs/{run_id}/dispatches/01NOPE/token"), 404, "not-found")


# -- push -----------------------------------------------------------------------------


class TestPush:
    @pytest.fixture()
    def awaiting(self, make_client):
        client, service = make_client()
        run_id = compose(service, "Auth-SSO")
        exported = service.export(run_id)
        return client, service, run_id, exported

    def test_an_unknown_run_is_a_plain_404_and_nothing_is_written(self, awaiting):
        """This used to quarantine the upload before any check — an
        unauthenticated way to write to the server's disk."""
        client, service, _run_id, exported = awaiting
        response = client.post(
            f"{API}/runs/01NOSUCHRUN000000000000000/results", files=[results_file(SSO)], headers=bearer(exported.token)
        )
        body = assert_error(response, 404, "unknown-run")
        assert "quarantine_entry" not in body
        assert client.get(f"{API}/quarantine").json() == {"entries": []}
        assert not service.quarantine.directory.exists() or not any(service.quarantine.directory.iterdir())

    def test_no_token_is_a_401_and_nothing_lands(self, awaiting):
        client, service, run_id, _exported = awaiting
        response = client.post(f"{API}/runs/{run_id}/results", files=[results_file(SSO)])
        assert_error(response, 401, "token-required")
        assert service.store.verdicts_for(run_id) == [] and service.quarantine.entries() == []
        assert service.require_run(run_id).state == "AWAITING_RESULTS"

    def test_a_wrong_token_is_a_403_and_nothing_lands(self, awaiting):
        client, service, run_id, _exported = awaiting
        response = client.post(f"{API}/runs/{run_id}/results", files=[results_file(SSO)], headers=bearer("rct_guessed"))
        assert_error(response, 403, "token-rejected")
        assert service.store.verdicts_for(run_id) == [] and service.quarantine.entries() == []

    def test_a_run_nothing_was_handed_out_for_cannot_be_pushed_to(self, make_client):
        client, service = make_client()
        run_id = compose(service, "Auth-SSO")
        assert_error(client.post(f"{API}/runs/{run_id}/results", files=[results_file(SSO)]), 401, "token-required")
        assert_error(
            client.post(f"{API}/runs/{run_id}/results", files=[results_file(SSO)], headers=bearer("rct_anything")),
            403, "token-rejected",
        )
        assert service.require_run(run_id).dispatches == ()

    def test_the_right_token_delivers_and_completes(self, awaiting):
        client, _service, run_id, exported = awaiting
        response = client.post(
            f"{API}/runs/{run_id}/results", files=[results_file(SSO)], data={"shard": "1"}, headers=bearer(exported.token)
        )
        assert response.status_code == 200
        body = response.json()
        assert (body["outcome"], body["run_state"], body["completion"]) == ("new", "COMPLETE", "PASS")
        assert body["dispatch_id"] == exported.dispatch.dispatch_id

    def test_the_x_header_is_accepted_too(self, awaiting):
        client, _service, run_id, exported = awaiting
        response = client.post(
            f"{API}/runs/{run_id}/results", files=[results_file(SSO)], headers={"X-Runcomposer-Token": exported.token}
        )
        assert response.status_code == 200 and response.json()["completion"] == "PASS"

    def test_a_dispatchs_token_cannot_deliver_into_another_dispatch(self, awaiting):
        client, service, run_id, first = awaiting
        service.finalize(run_id)
        second = service.export(run_id)
        response = client.post(
            f"{API}/runs/{run_id}/results", files=[results_file(SSO)],
            data={"dispatch": second.dispatch.dispatch_id}, headers=bearer(first.token),
        )
        assert_error(response, 403, "token-rejected", detail="delivers to that dispatch only")
        assert service.store.verdicts_for(run_id) == []
        # Without naming a dispatch, the token's own dispatch is the target —
        # not "the latest".
        delivered = client.post(f"{API}/runs/{run_id}/results", files=[results_file(SSO)], headers=bearer(first.token))
        assert delivered.status_code == 200 and delivered.json()["dispatch_id"] == first.dispatch.dispatch_id
        assert service.store.get_dispatch(second.dispatch.dispatch_id).state == "AWAITING_RESULTS"

    def test_the_same_upload_again_is_a_no_op_and_a_new_one_replaces(self, awaiting):
        client, service, run_id, exported = awaiting
        url, headers = f"{API}/runs/{run_id}/results", bearer(exported.token)
        red = {SSO[0]: "FAIL", SSO[1]: "PASS", SSO[2]: "PASS"}
        assert client.post(url, files=[results_file(red)], headers=headers).json()["completion"] == "FAIL"
        assert client.post(url, files=[results_file(red)], headers=headers).json()["outcome"] == "duplicate"
        green = client.post(url, files=[results_file(SSO)], headers=headers).json()
        assert (green["outcome"], green["completion"]) == ("replaced", "PASS")
        assert len(service.store.verdicts_for(run_id)) == 3

    def test_a_malformed_bundle_is_a_422_and_is_quarantined(self, awaiting):
        """Authenticated, but unreadable: kept for a person to look at."""
        client, service, run_id, exported = awaiting
        broken = ("files", ("results.json", b'{"format": "runcomposer-verdicts", "verdicts": [{"status": "PASS"}]}', "application/json"))
        response = client.post(f"{API}/runs/{run_id}/results", files=[broken], headers=bearer(exported.token))
        body = assert_error(response, 422, "bad-bundle", detail="'name' is required")
        (entry,) = client.get(f"{API}/quarantine").json()["entries"]
        assert body["quarantine_entry"] == entry["entry_id"]
        assert (entry["reason"], entry["transport"], entry["claimed_run_id"]) == ("bad-bundle", "push", run_id)
        assert service.require_run(run_id).state == "AWAITING_RESULTS"

        nothing = ("files", ("notes.txt", b"no results in here", "text/plain"))
        empty = client.post(f"{API}/runs/{run_id}/results", files=[nothing], headers=bearer(exported.token))
        assert_error(empty, 422, "bad-bundle", detail="contains no 'runcomposer-verdicts' results")

    def test_a_marker_from_another_document_is_a_409_and_is_quarantined(self, awaiting):
        client, service, run_id, exported = awaiting
        files = [results_file(SSO), marker_file(run_id=run_id, spec_sha256="0" * 64)]
        response = client.post(f"{API}/runs/{run_id}/results", files=files, headers=bearer(exported.token))
        body = assert_error(response, 409, "sha-mismatch", detail="does not match the spec bytes")
        assert service.quarantine.get(body["quarantine_entry"]).reason == "sha-mismatch"
        assert service.store.verdicts_for(run_id) == []

    def test_a_marker_naming_another_run_is_a_409(self, awaiting):
        client, _service, run_id, exported = awaiting
        files = [results_file(SSO), marker_file(run_id="01SOMEBODYELSESRUN00000000")]
        response = client.post(f"{API}/runs/{run_id}/results", files=files, headers=bearer(exported.token))
        assert_error(response, 409, "marker-conflict")

    def test_a_declared_format_that_is_not_the_runs_is_a_400(self, awaiting):
        client, service, run_id, exported = awaiting
        response = client.post(
            f"{API}/runs/{run_id}/results", files=[results_file(SSO)], data={"format": "junit-xml"}, headers=bearer(exported.token)
        )
        assert_error(response, 400, "invalid-request", detail="does not match the run's expected results format")
        assert service.quarantine.entries() == []
        same = client.post(
            f"{API}/runs/{run_id}/results", files=[results_file(SSO)],
            data={"format": "runcomposer-verdicts"}, headers=bearer(exported.token),
        )
        assert same.status_code == 200

    def test_an_oversized_upload_is_a_413(self, make_client):
        client, service = make_client(core={"ingestion": {"max_upload_mb": 1}})
        run_id = compose(service, "Auth-SSO")
        exported = service.export(run_id)
        big = ("files", ("results.json", b"x" * (1024 * 1024 + 4096), "application/json"))
        response = client.post(f"{API}/runs/{run_id}/results", files=[big], headers=bearer(exported.token))
        assert_error(response, 413, "payload-too-large", detail="max_upload_mb (1 MB)")
        assert service.quarantine.entries() == [] and service.store.verdicts_for(run_id) == []

    def test_an_upload_filename_cannot_leave_the_bundle(self, awaiting, tmp_path):
        client, _service, run_id, exported = awaiting
        payload = json.loads(results_file(SSO)[1][1])
        sneaky = ("files", ("../../escaped.json", json.dumps(payload).encode(), "application/json"))
        response = client.post(f"{API}/runs/{run_id}/results", files=[sneaky], headers=bearer(exported.token))
        assert response.status_code == 200
        assert not list(tmp_path.rglob("escaped.json"))

    def test_disabled_tokens_open_the_endpoint(self, make_client):
        client, service = make_client(core={"ingestion": {"tokens": "disabled"}})
        run_id = compose(service, "Auth-SSO")
        exported = export(client, run_id)
        assert exported["ingest_token"] is None
        response = client.post(f"{API}/runs/{run_id}/results", files=[results_file(SSO)])
        assert response.status_code == 200 and response.json()["completion"] == "PASS"


# -- quarantine ----------------------------------------------------------------------------------


class TestQuarantineApi:
    @pytest.fixture()
    def quarantined(self, make_client, tmp_path):
        """Two stray file drops in quarantine: one for an unknown run, one
        without a marker."""
        client, service = make_client()

        def stray(name: str, marker: dict | None) -> str:
            directory = tmp_path / name
            directory.mkdir()
            (directory / "results.json").write_bytes(results_file(SSO)[1][1])
            if marker:
                (directory / "runcomposer_run.json").write_text(json.dumps(marker), encoding="utf-8")
            report = service.ingest_or_quarantine(directory, transport="file-drop")
            return str(report.entry_id)

        return client, service, stray("claimed", {"run_id": "01SOMEBODYELSESRUN00000000"}), stray("bare", None)

    def test_list(self, quarantined):
        client, _service, claimed, bare = quarantined
        entries = {entry["entry_id"]: entry for entry in client.get(f"{API}/quarantine").json()["entries"]}
        assert set(entries) == {claimed, bare}
        assert set(entries[claimed]) == {
            "entry_id", "received_at", "reason", "transport", "claimed_run_id", "content_hash", "format", "detail",
            "sequence",
        }
        assert (entries[claimed]["reason"], entries[claimed]["claimed_run_id"]) == ("unknown-run", "01SOMEBODYELSESRUN00000000")
        assert (entries[bare]["reason"], entries[bare]["claimed_run_id"], entries[bare]["format"]) == ("unsolicited", None, None)

    def test_attach_binds_an_entry_to_a_run_and_records_it(self, quarantined):
        client, service, claimed, bare = quarantined
        run_id = compose(service, "Auth-SSO")
        response = client.post(f"{API}/quarantine/{claimed}/attach", json={"run_id": run_id})
        assert response.status_code == 200
        assert (response.json()["run_id"], response.json()["completion"]) == (run_id, "PASS")
        assert [e["entry_id"] for e in client.get(f"{API}/quarantine").json()["entries"]] == [bare]
        events = client.get(f"{API}/runs/{run_id}/events").json()["events"]
        attached = next(e for e in events if e["kind"] == "delivery.attached")
        assert attached["data"]["quarantine_entry"] == claimed

    def test_attach_needs_a_known_run_and_a_known_entry(self, quarantined):
        client, service, claimed, _bare = quarantined
        assert_error(client.post(f"{API}/quarantine/{claimed}/attach", json={}), 400, "invalid-request")
        assert_error(client.post(f"{API}/quarantine/{claimed}/attach", json={"run_id": "01NOPE"}), 404, "unknown-run")
        run_id = compose(service, "Auth-SSO")
        assert_error(client.post(f"{API}/quarantine/q-nope/attach", json={"run_id": run_id}), 404, "unknown-quarantine-entry")
        assert len(client.get(f"{API}/quarantine").json()["entries"]) == 2

    def test_promote_needs_a_format_when_the_entry_has_none(self, quarantined):
        client, _service, _claimed, bare = quarantined
        assert_error(client.post(f"{API}/quarantine/{bare}/promote"), 422, "bad-bundle", detail="does not say which result format")
        assert_error(client.post(f"{API}/quarantine/{bare}/promote", json={"format": "junit-xml"}), 422, "bad-bundle")
        response = client.post(f"{API}/quarantine/{bare}/promote", json={"format": "runcomposer-verdicts"})
        assert response.status_code == 200
        run = client.get(f"{API}/runs/{response.json()['run_id']}").json()
        assert (run["origin"], run["state"], run["completion"], run["mode"]) == ("ingested", "COMPLETE", "PASS", "ingest")
        assert_error(client.post(f"{API}/quarantine/q-nope/promote"), 404, "unknown-quarantine-entry")

    def test_discard(self, quarantined):
        client, _service, claimed, bare = quarantined
        response = client.delete(f"{API}/quarantine/{claimed}")
        assert response.status_code == 204 and response.content == b""
        assert [e["entry_id"] for e in client.get(f"{API}/quarantine").json()["entries"]] == [bare]
        assert_error(client.delete(f"{API}/quarantine/{claimed}"), 404, "unknown-quarantine-entry")


# -- files: artifacts and the UI ------------------------------------------------------------------


class TestArtifactRoute:
    @pytest.fixture()
    def with_artifact(self, make_client, tmp_path):
        client, service = make_client()
        run_id = compose(service, "Auth-SSO")
        artifact = tmp_path / "artifacts" / run_id / "dispatch-1" / "env1" / "output.xml"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("<robot><suite/></robot>", encoding="utf-8")
        service.store.add_artifact_ref(
            run_id, None, name="output.xml (env1)", media_type="application/xml", url_or_path=str(artifact)
        )
        (tmp_path / "secret.txt").write_text("do not serve me", encoding="utf-8")
        return client, service, run_id, tmp_path

    def test_the_file_is_served_sandboxed(self, with_artifact):
        client, _service, run_id, _tmp = with_artifact
        href = client.get(f"{API}/runs/{run_id}").json()["artifacts"][0]["href"]
        assert href == f"/artifacts/{run_id}/dispatch-1/env1/output.xml"
        response = client.get(href)
        assert response.status_code == 200 and response.text == "<robot><suite/></robot>"
        assert response.headers["content-type"].startswith(("application/xml", "text/xml"))
        # Untrusted bytes on the API's own origin: no scripting, no sniffing.
        policy = response.headers["content-security-policy"]
        assert "sandbox" in policy and "default-src 'none'" in policy
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"

    def test_only_recorded_artifacts_are_served(self, with_artifact):
        """A file is downloadable because a runner recorded it — not because
        it lies in the artifact directory. What a runner merely wrote into
        its work directory (a scratch file, a backup, a credential a tool
        left behind) answers exactly like a file that does not exist."""
        client, service, run_id, tmp_path = with_artifact
        workdir = tmp_path / "artifacts" / run_id / "dispatch-1"
        (workdir / "env1" / "environment.bak").write_text("SHOP_API_KEY=k-123456", encoding="utf-8")
        (workdir / ".live").mkdir()
        (workdir / ".live" / "live-1.jsonl").write_text("{}", encoding="utf-8")
        for unrecorded in ("env1/environment.bak", ".live/live-1.jsonl"):
            response = client.get(f"/artifacts/{run_id}/dispatch-1/{unrecorded}")
            assert_error(response, 404, "unknown-artifact")
            assert "k-123456" not in response.text
        assert client.get(f"/artifacts/{run_id}/dispatch-1/env1/output.xml").status_code == 200
        # Recording it is what makes it servable.
        service.store.add_artifact_ref(
            run_id, None, name="environment", media_type="text/plain",
            url_or_path=str(workdir / "env1" / "environment.bak"),
        )
        assert client.get(f"/artifacts/{run_id}/dispatch-1/env1/environment.bak").status_code == 200

    def test_a_recorded_directory_makes_its_tree_servable_and_nothing_beside_it(self, with_artifact):
        """A report refers to files next to it (screenshots): a runner
        records the directory, and what lies under it is served."""
        client, service, run_id, tmp_path = with_artifact
        workdir = tmp_path / "artifacts" / run_id / "dispatch-1"
        shots = workdir / "env2" / "screenshots"
        shots.mkdir(parents=True)
        (shots / "checkout.png").write_bytes(b"\x89PNG")
        (workdir / "notes.txt").write_text("beside, not inside", encoding="utf-8")
        assert client.get(f"/artifacts/{run_id}/dispatch-1/env2/screenshots/checkout.png").status_code == 404
        service.store.add_artifact_ref(
            run_id, None, name="files (env2)", media_type="inode/directory", url_or_path=str(workdir / "env2")
        )
        served = client.get(f"/artifacts/{run_id}/dispatch-1/env2/screenshots/checkout.png")
        assert served.status_code == 200 and served.content == b"\x89PNG"
        assert client.get(f"/artifacts/{run_id}/dispatch-1/notes.txt").status_code == 404
        # Hidden files in a recorded directory are a tool's own bookkeeping:
        # not served with the directory — only when recorded by themselves.
        (workdir / "env2" / ".worker-state.json").write_text("{}", encoding="utf-8")
        (workdir / "env2" / ".cache").mkdir()
        (workdir / "env2" / ".cache" / "session.txt").write_text("k-123456", encoding="utf-8")
        for hidden in (".worker-state.json", ".cache/session.txt"):
            assert client.get(f"/artifacts/{run_id}/dispatch-1/env2/{hidden}").status_code == 404
        service.store.add_artifact_ref(
            run_id, None, name="worker state", media_type="application/json",
            url_or_path=str(workdir / "env2" / ".worker-state.json"),
        )
        assert client.get(f"/artifacts/{run_id}/dispatch-1/env2/.worker-state.json").status_code == 200
        listed = {a["name"]: a for a in client.get(f"{API}/runs/{run_id}").json()["artifacts"]}
        assert (listed["files (env2)"]["kind"], listed["files (env2)"]["href"]) == ("directory", None)

    def test_a_file_recorded_by_another_run_is_not_served_from_this_runs_directory(self, with_artifact):
        client, service, run_id, tmp_path = with_artifact
        other = compose(service, "Auth-SSO")
        stray = tmp_path / "artifacts" / run_id / "dispatch-1" / "stray.txt"
        stray.write_text("x", encoding="utf-8")
        service.store.add_artifact_ref(other, None, name="stray", media_type="text/plain", url_or_path=str(stray))
        assert client.get(f"/artifacts/{run_id}/dispatch-1/stray.txt").status_code == 404
        (entry,) = client.get(f"{API}/runs/{other}").json()["artifacts"]
        assert (entry["kind"], entry["href"]) == ("external-path", None)

    @pytest.mark.parametrize(
        "attack",
        ["../../../../etc/passwd", "..%2f..%2f..%2fetc%2fpasswd", "run/dispatch/../../../secret.txt", "%2e%2e/secret.txt"],
    )
    def test_traversal_out_of_the_artifact_dir_is_a_404(self, with_artifact, attack):
        client, _service, _run_id, _tmp = with_artifact
        response = client.get(f"/artifacts/{attack}")
        assert response.status_code == 404 and "do not serve me" not in response.text

    def test_an_absolute_path_is_a_404(self, with_artifact):
        client, _service, _run_id, tmp_path = with_artifact
        response = client.get(f"/artifacts//{tmp_path / 'secret.txt'}")
        assert response.status_code == 404 and "do not serve me" not in response.text

    @posix_only
    def test_a_symlink_out_of_the_artifact_dir_is_a_404(self, with_artifact):
        client, _service, _run_id, tmp_path = with_artifact
        (tmp_path / "artifacts" / "escape.txt").symlink_to(tmp_path / "secret.txt")
        response = client.get("/artifacts/escape.txt")
        assert_error(response, 404, "unknown-artifact")
        assert "do not serve me" not in response.text

    @posix_only
    def test_a_miss_and_a_refusal_are_indistinguishable_in_status_and_code(self, with_artifact):
        client, service, run_id, _tmp = with_artifact
        gone = _tmp / "artifacts" / run_id / "dispatch-1" / "away" / "output.xml"
        service.store.add_artifact_ref(
            run_id, None, name="output.xml", media_type="application/xml", url_or_path=str(gone)
        )
        miss = assert_error(
            client.get(f"/artifacts/{run_id}/dispatch-1/away/output.xml"), 404, "unknown-artifact"
        )
        assert "retention" in miss["detail"], "a recorded file that is gone names the likely cause"
        (tmp_path := _tmp / "artifacts" / "link").symlink_to(_tmp / "secret.txt")
        refusal = assert_error(client.get("/artifacts/link"), 404, "unknown-artifact")
        assert (refusal["code"], tmp_path.exists()) == (miss["code"], True)

    def test_a_path_outside_the_artifact_dir_is_listed_without_a_link(self, with_artifact):
        client, service, run_id, tmp_path = with_artifact
        service.store.add_artifact_ref(
            run_id, None, name="stray", media_type="text/plain", url_or_path=str(tmp_path / "secret.txt")
        )
        service.store.add_artifact_ref(
            run_id, None, name="not a link", media_type="text/html", url_or_path="javascript:alert(1)"
        )
        by_name = {a["name"]: a for a in client.get(f"{API}/runs/{run_id}").json()["artifacts"]}
        assert (by_name["stray"]["kind"], by_name["stray"]["href"]) == ("external-path", None)
        assert by_name["stray"]["url_or_path"] == str(tmp_path / "secret.txt")
        assert (by_name["not a link"]["kind"], by_name["not a link"]["href"]) == ("external-path", None)


class TestUiFiles:
    UI = resources.files("runcomposer") / "ui_dist"

    def test_the_index_is_revalidated_and_hashed_assets_are_immutable(self, client):
        if not self.UI.is_dir():
            body = client.get("/").json()
            assert body["runcomposer"] == __version__ and "/api/v1" in body["detail"]
            return
        index = client.get("/")
        assert index.status_code == 200 and '<div id="root">' in index.text
        assert index.headers["cache-control"] == "no-cache"
        locale = client.get("/locales/en.json")
        assert locale.status_code == 200 and locale.headers["cache-control"] == "no-cache"
        assert isinstance(locale.json(), dict)
        asset = next(p.name for p in (self.UI / "assets").iterdir() if p.name.endswith(".js"))
        served = client.get(f"/assets/{asset}")
        assert served.status_code == 200
        assert served.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert_error(client.get("/assets/no-such-file.js"), 404, "not-found")

    def test_an_unknown_locale_is_a_404(self, client):
        if not self.UI.is_dir():
            pytest.skip("this build carries no UI bundle")
        assert_error(client.get("/locales/tlh.json"), 404, "not-found")
        assert client.get("/locales/..%2f..%2findex.json").status_code == 404
