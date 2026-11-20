"""Edges of the contracts: what holds when a runner declares less than it was
given, when two requests race, when a process comes back under its old id,
when a secret would be quoted in an error — the cases a happy path never
visits.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import ssl
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from runcomposer import __version__
from runcomposer.cli import main as cli_main
from runcomposer.sdk import (
    ConfigError,
    DispatchContext,
    InvalidRequest,
    Parameter,
    RunActive,
    RunnerBase,
    RunnerInfo,
    Service,
    SpecConflict,
    Verdict,
    load_config,
    missing_verdicts,
    verdicts_hash,
)

from .conftest import REPO, compose, wait_until, write_config
from .exec_loader import runcomposer_exec

API = "/api/v1"
SSO = ["Shop.Auth.SSO.T001", "Shop.Auth.SSO.T002", "Shop.Auth.SSO.T003"]

#: What the scripted runner does with its context — set per test.
SCRIPT: dict[str, Any] = {}


class ScriptedRunner(RunnerBase):
    """Runs whatever the test put in ``SCRIPT["dispatch"]``."""

    def __init__(self, endpoint: str = "", token: str = ""):
        self.endpoint, self.token = endpoint, token

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="scripted", capabilities=("cancel",))

    def dispatch(self, ctx: DispatchContext) -> None:
        SCRIPT["dispatch"](ctx)


class NamedRunner(ScriptedRunner):
    """The scripted runner with one per-run parameter."""

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="named", parameters=(Parameter("who", kind="string"),))


class LeakyRunner(RunnerBase):
    """Quotes its configured token wherever a careless plugin might: in its
    refusal, in an error after the hand-off, in its health."""

    def __init__(self, token: str, fail: str = "refuse"):
        self.token, self.fail = token, fail

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="leaky")

    def health(self) -> Any:
        from runcomposer.sdk import Health

        return Health(status="degraded", detail=f"probe with {self.token} was rejected",
                      data={"sent": f"Bearer {self.token}"})

    def dispatch(self, ctx: DispatchContext) -> None:
        if self.fail == "refuse":
            ctx.refuse(f"stage rejected 'Authorization: Bearer {self.token}'")
        ctx.log(f"calling the stage with {self.token}")
        ctx.declare(["1"], plan=f"one shard, token {self.token}", links={"stage": f"https://stage.shop.example/?t={self.token}"})
        raise RuntimeError(f"stage answered 403 to token {self.token!r}")


class PickyRunner(RunnerBase):
    """Refuses its ``endpoint`` option, quoting the value it was given."""

    def __init__(self, endpoint: str):
        raise ValueError(f"cannot use endpoint {endpoint!r}")


SCRIPTED = {"scripted": {"module": "tests.test_hardening:ScriptedRunner"}}


@pytest.fixture(autouse=True)
def _fresh_script():
    SCRIPT.clear()
    yield
    SCRIPT.clear()


# -- completion against the planned set --------------------------------------------------


class TestAPlanSmallerThanTheRun:
    def test_items_no_shard_plan_mentions_are_missing(self):
        """A per-shard plan does not replace the planned set: an item that no
        shard was declared to carry, and nobody reported, is owed all the
        same."""
        planned = ["a", "b", "c"]
        reported = [Verdict("a", "PASS", shard="s1")]
        missing = missing_verdicts(planned, reported, {"s1": ["a"]})
        assert sorted((v.item_id, v.shard, v.status) for v in missing) == [
            ("b", "", "MISSING"),
            ("c", "", "MISSING"),
        ]
        # …while an item one shard reported although another was planned to
        # is not counted twice.
        assert missing_verdicts(planned, reported, {"s1": ["a", "b", "c"]}) != missing

    def test_a_runner_cannot_make_a_run_green_by_declaring_one_item(self, make_service):
        def one_of_three(ctx: DispatchContext) -> None:
            first = ctx.item_ids[0]
            ctx.declare({"only": [first]})
            ctx.deliver("only", verdicts=[Verdict(first, "PASS")])

        SCRIPT["dispatch"] = one_of_three
        service = make_service(runners=SCRIPTED)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "scripted", foreground=True)
        run = service.require_run(run_id)
        assert run.planned_count == 3
        assert (run.state, run.completion) == ("COMPLETE", "INCOMPLETE")
        assert dict(run.latest_dispatch.summary) == {"PASS": 1, "MISSING": 2}


class TestDeliveryHash:
    def test_every_reported_field_takes_part_in_the_hash(self):
        base = [Verdict("Shop.Auth.SSO.T001", "FAIL", 120, message="timeout after 30s")]
        assert verdicts_hash(base) == verdicts_hash(list(base))
        for changed in (
            Verdict("Shop.Auth.SSO.T001", "FAIL", 120, message="timeout after 60s"),
            Verdict("Shop.Auth.SSO.T001", "FAIL", 121, message="timeout after 30s"),
            Verdict("Shop.Auth.SSO.T001", "ERROR", 120, message="timeout after 30s"),
            Verdict("Shop.Auth.SSO.T001", "FAIL", 120, message="timeout after 30s", attempt=2),
        ):
            assert verdicts_hash([changed]) != verdicts_hash(base)

    def test_a_runner_delivery_that_only_corrects_a_message_replaces_the_shard(self, make_service):
        """Delivered in memory, where the hash is over the verdicts: the same
        results with another message are a correction, not a duplicate."""
        outcomes: list[str] = []

        def deliver_twice(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            for message in ("first wording", "first wording", "corrected wording"):
                verdicts = [Verdict(i, "FAIL", 10, message=message) for i in ctx.item_ids]
                outcomes.append(ctx.deliver("1", verdicts=verdicts))

        SCRIPT["dispatch"] = deliver_twice
        service = make_service(runners=SCRIPTED)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "scripted", foreground=True)
        assert outcomes == ["new", "duplicate", "replaced"]
        assert {v.message for v in service.store.verdicts_for(run_id)} == {"corrected wording"}


# -- the hand-off --------------------------------------------------------------------------


class TestDeclare:
    def test_a_runner_can_record_the_hash_of_bytes_it_handed_out_itself(self, make_service):
        def hand_out(ctx: DispatchContext) -> None:
            ctx.declare(["1"], spec_sha256="ab" * 32)

        SCRIPT["dispatch"] = hand_out
        service = make_service(runners=SCRIPTED)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "scripted", foreground=True)
        dispatch = service.require_run(run_id).latest_dispatch
        assert dispatch.state == "AWAITING_RESULTS" and dispatch.spec_sha256 == "ab" * 32


class TestTwoRequestsForOneRun:
    def test_concurrent_dispatches_leave_exactly_one_in_flight(self, make_service):
        """Checking "is something in flight" and recording the dispatch is
        one step: of eight simultaneous requests one wins, seven are told the
        run is active, and the runner is entered once."""
        entered, release = [], threading.Event()

        def gated(ctx: DispatchContext) -> None:
            entered.append(ctx.dispatch_id)
            ctx.declare(["1"])
            release.wait(30)
            ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        SCRIPT["dispatch"] = gated
        service = make_service(runners=SCRIPTED, core={"dispatch": {"max_concurrent": 8}})
        run_id = compose(service, "Auth-SSO")
        start = threading.Barrier(8)

        def attempt(_n: int) -> str:
            start.wait(30)
            try:
                service.dispatch(run_id, "scripted")
                return "accepted"
            except RunActive:
                return "active"

        try:
            with ThreadPoolExecutor(8) as pool:
                results = list(pool.map(attempt, range(8)))
            assert sorted(results) == ["accepted"] + ["active"] * 7
            wait_until(lambda: entered, what="the one dispatch to reach the runner")
            assert len(service.require_run(run_id).dispatches) == 1
        finally:
            release.set()
        assert service.wait(run_id, timeout=30).state == "COMPLETE"
        assert len(entered) == 1


    def test_a_refused_request_does_not_leave_its_parameters_in_the_stored_spec(
        self, make_service
    ):
        release = threading.Event()

        def gated(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            release.wait(30)
            ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        SCRIPT["dispatch"] = gated
        service = make_service(
            runners={"named": {"module": "tests.test_hardening:NamedRunner"}},
            core={"dispatch": {"max_concurrent": 8}},
        )
        try:
            for _round in range(10):
                release.clear()
                run_id = compose(service, "Auth-SSO")
                start = threading.Barrier(8)

                def attempt(n: int, run_id: str = run_id, start: threading.Barrier = start) -> None:
                    start.wait(30)
                    try:
                        service.dispatch(run_id, "named", {"who": f"request-{n}"})
                    except RunActive:
                        pass

                with ThreadPoolExecutor(8) as pool:
                    list(pool.map(attempt, range(8)))
                (dispatch,) = service.require_run(run_id).dispatches
                stored = service.require_spec(run_id)["runner"]["named"]
                assert stored == dict(dispatch.parameters), "the spec says what was dispatched"
                release.set()
                assert service.wait(run_id, timeout=30).state == "COMPLETE"
        finally:
            release.set()

    def test_an_export_and_a_runner_dispatch_cannot_both_find_the_run_idle(self, make_service):
        release = threading.Event()

        def gated(ctx: DispatchContext) -> None:
            ctx.declare(["1"])
            release.wait(30)
            ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        SCRIPT["dispatch"] = gated
        service = make_service(runners=SCRIPTED, core={"dispatch": {"max_concurrent": 8}})
        try:
            for _round in range(15):
                release.clear()
                run_id = compose(service, "Auth-SSO")
                start = threading.Barrier(6)

                def attempt(n: int, run_id: str = run_id, start: threading.Barrier = start) -> str:
                    start.wait(30)
                    try:
                        if n % 2:
                            service.export(run_id)
                            return "export"
                        service.dispatch(run_id, "scripted")
                        return "runner"
                    except RunActive:
                        return "refused"

                with ThreadPoolExecutor(6) as pool:
                    results = list(pool.map(attempt, range(6)))
                modes = [d.mode for d in service.require_run(run_id).dispatches]
                # Either the runner got the run (and every export was refused),
                # or exports did (and no runner dispatch was accepted).
                assert modes == ["scripted"] or set(modes) == {"export"}, (results, modes)
                release.set()
                service.dispatcher.join(30)
        finally:
            release.set()


# -- a refusal that comes after the answer ----------------------------------------------------


class TestLateRefusal:
    def test_a_refusal_after_the_http_answer_leaves_the_run_composed_with_the_reason(
        self, make_client
    ):
        """The request does not wait for the runner (here: not at all). When
        the runner then refuses, the run the caller was already given stays —
        COMPOSED, with the refusal in its events."""
        go = threading.Event()

        def refuse_later(ctx: DispatchContext) -> None:
            go.wait(30)
            ctx.refuse("the readiness check failed: checkout stage is down")

        SCRIPT["dispatch"] = refuse_later
        client, service = make_client(runners=SCRIPTED, core={"dispatch": {"handoff_wait_s": 0}})
        try:
            answer = client.post(
                f"{API}/runs",
                json={"selection": {"tag_filter": "Auth-SSO"}, "dispatch": {"runner": "scripted"}},
            )
            assert answer.status_code == 201 and answer.json()["run"]["state"] == "DISPATCHED"
            run_id = answer.json()["run"]["id"]
        finally:
            go.set()
        assert service.wait(run_id, timeout=30).state == "COMPOSED"
        detail = client.get(f"{API}/runs/{run_id}").json()
        assert detail["dispatches"] == []
        refusals = [e for e in detail["events"] if e["kind"] == "dispatch.refused"]
        assert len(refusals) == 1 and "checkout stage is down" in refusals[0]["data"]["reason"]

    def test_the_compose_answer_says_which_run_a_history_selection_resolved_to(self, make_client):
        client, service = make_client()
        first = compose(service, "Auth-SSO", labels={"suite": "nightly"})
        service.dispatch(first, "demo", {"seed": "b"}, foreground=True)
        assert service.require_run(first).completion == "FAIL"
        answer = client.post(
            f"{API}/runs",
            json={"selection": {"history": "failed@latest?suite=nightly"}, "title": "Rerun"},
        )
        assert answer.status_code == 201
        (provenance,) = answer.json()["derived_from"]
        assert provenance["resolved_run_id"] == first
        assert provenance["query"]["labels"] == {"suite": "nightly"}


# -- cancel --force over the CLI and the API ---------------------------------------------------


class TestForcedCancel:
    def test_over_http_and_from_the_command_line(self, make_client, tmp_path, capsys):
        import socket
        import sys

        client, service = make_client()
        impostor = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
        )
        try:
            owner = f"{socket.gethostname()}:{impostor.pid}:01SOMEOTHERPROCESS00000000"
            runs = []
            for _ in range(2):
                run_id = compose(service, "Auth-SSO")
                dispatch = service.ledger.open(run_id, "demo", owner=owner)
                service.ledger.declare(
                    dispatch.dispatch_id, shards=["1"], shard_items={}, plan="", links={}
                )
                runs.append(run_id)
            plain = client.post(f"{API}/runs/{runs[0]}/cancel")
            assert plain.status_code == 202 and plain.json()["run"]["state"] == "RUNNING"
            forced = client.post(f"{API}/runs/{runs[0]}/cancel", params={"force": "true"})
            assert forced.status_code == 202 and forced.json()["run"]["state"] == "CANCELLED"
            code = cli_main(["cancel", runs[1], "--force", "--config", str(tmp_path / "config.yaml")])
            assert code == 0 and "CANCELLED" in capsys.readouterr().out
        finally:
            impostor.communicate(b"")
        assert service.require_run(runs[1]).state == "CANCELLED"


# -- a spec file handed in again ----------------------------------------------------------------


class TestASpecFileHandedInAgain:
    def test_its_runner_section_is_this_dispatch_s_input_and_its_core_is_not_editable(
        self, service
    ):
        """The open `runner` section of a spec file states the per-run
        parameters for the dispatch it is handed in for — editing it takes
        effect. Editing anything else is refused."""
        run_id = compose(service, "Auth-SSO", runner_id="demo", parameters={"seed": "b"})
        document = service.require_spec(run_id)
        assert service.import_spec(document, origin="cli") == run_id  # unchanged: a no-op
        service.dispatch(run_id, "demo", foreground=True)
        first = service.require_run(run_id).latest_dispatch

        edited = json.loads(json.dumps(document))
        edited["runner"]["demo"]["seed"] = "a"
        assert service.import_spec(edited, origin="cli") == run_id
        assert service.require_spec(run_id) == document, "handing it in writes nothing"
        service.dispatch(run_id, "demo", foreground=True, document=edited)
        second = service.require_run(run_id).latest_dispatch
        assert (first.parameters, second.parameters) == ({"seed": "b"}, {"seed": "a"})
        assert first.summary != second.summary, "the two seeds give different results"
        assert service.require_spec(run_id)["runner"] == {"demo": {"seed": "a"}}

        tampered = json.loads(json.dumps(document))
        tampered["selection"]["materialized"]["item_ids"].pop()
        tampered["selection"]["materialized"]["count"] -= 1
        with pytest.raises(SpecConflict):
            service.import_spec(tampered, origin="cli")

    def test_a_refused_hand_in_changes_nothing_that_is_stored(self, tmp_path, capsys):
        """A file whose runner section does not validate is refused — and the
        stored run is exactly what it was, still dispatchable."""
        config = str(write_config(tmp_path))
        with Service(load_config(config)) as service:
            run_id = compose(service, "Auth-SSO", runner_id="demo", parameters={"seed": "a"})
            document = service.require_spec(run_id)
        bad = json.loads(json.dumps(document))
        bad["runner"]["demo"]["seed"] = ["not", "a", "string"]
        bad_file = tmp_path / "bad.json"
        bad_file.write_text(json.dumps(bad), encoding="utf-8")
        assert cli_main(["dispatch", str(bad_file), "--runner", "demo", "--config", config]) == 1
        assert "parameter 'seed' must be a string" in capsys.readouterr().err
        with Service(load_config(config)) as service:
            assert service.require_spec(run_id) == document
            assert service.require_run(run_id).dispatches == ()
        assert cli_main(["dispatch", "--run", run_id, "--runner", "demo", "--config", config]) == 0
        capsys.readouterr()

    def test_the_command_says_which_keys_of_the_runner_section_it_ignored(self, tmp_path, capsys):
        config = str(write_config(tmp_path))
        with Service(load_config(config)) as service:
            run_id = compose(service, "Auth-SSO", runner_id="demo", parameters={"seed": "a"})
            document = service.require_spec(run_id)
        document["runner"]["demo"]["delay_s"] = 99  # a configured option, not a parameter
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(document), encoding="utf-8")
        assert cli_main(["dispatch", str(spec_file), "--runner", "demo", "--config", config]) == 0
        err = capsys.readouterr().err
        assert "warning: runner.demo in the spec carries ['delay_s']" in err and "ignored" in err

    def test_a_document_listing_an_id_twice_is_not_a_spec(self, service):
        document = service.build_spec_document({"tag_filter": "Auth-SSO"}, title="x").spec
        ids = document["selection"]["materialized"]["item_ids"]
        ids.append(ids[0])
        document["selection"]["materialized"]["count"] += 1
        with pytest.raises(InvalidRequest, match="more than once"):
            service.import_spec(document, origin="cli")


# -- times are compared as times ------------------------------------------------------------------


class TestStoredTimes:
    def test_an_imported_document_with_an_offset_is_filtered_by_its_instant(self, make_client):
        client, service = make_client()
        run_id = compose(service, "Auth-SSO")
        document = service.require_spec(run_id)
        service.delete_run(run_id)
        document["run"]["created_at"] = "2026-07-06T11:14:03+02:00"  # 09:14:03Z
        document["selection"]["materialized"]["at"] = "2026-07-06T11:14:03+02:00"
        service.import_spec(document, origin="cli")
        assert service.require_run(run_id).created_at == "2026-07-06T09:14:03Z"
        assert service.require_spec(run_id)["run"]["created_at"] == "2026-07-06T11:14:03+02:00"

        def listed(**window: str) -> bool:
            rows = client.get(f"{API}/runs", params=window).json()["runs"]
            return run_id in [row["id"] for row in rows]

        assert not listed(since="2026-07-06T10:00:00Z")
        assert listed(until="2026-07-06T10:00:00Z")
        assert listed(since="2026-07-06T11:00:00+02:00", until="2026-07-06T11:30:00+02:00")


    def test_a_0_1_database_with_an_offset_time_is_normalized_on_migration(self, tmp_path):
        import sqlite3

        from runcomposer.plugins.sqlite_store import SqliteRunStore

        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.executescript((REPO / "tests" / "fixtures" / "store-0.1.7.sql").read_text("utf-8"))
        conn.execute(
            "UPDATE runs SET created_at = '2026-07-06T11:14:03+02:00'"
            " WHERE title = 'Auth smoke (composed only)'"
        )
        conn.commit()
        conn.close()
        store = SqliteRunStore(str(path))
        run = next(r for r in store.list_runs(limit=100).runs if r.title.startswith("Auth smoke"))
        assert run.created_at == "2026-07-06T09:14:03Z"
        early = store.list_runs(until="2026-07-06T10:00:00Z", limit=100).runs
        assert [r.id for r in early] == [run.id]
        assert run.id not in [r.id for r in store.list_runs(since="2026-07-06T10:00:00Z", limit=100).runs]


# -- an id that two items carry ---------------------------------------------------------------------


class TestADuplicatedId:
    def test_it_is_planned_once(self, make_service, tmp_path):
        for name in ("one", "two"):
            (tmp_path / f"{name}.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {"id": "Shop.Cart.Core.T001", "tags": ["Cart"]},
                            {"id": f"Shop.Cart.Core.{name}", "tags": ["Cart"]},
                        ]
                    }
                ),
                encoding="utf-8",
            )
        service = make_service(
            sources={
                "first": {"plugin": "manifest", "path": "one.json"},
                "second": {"plugin": "manifest", "path": "two.json"},
            }
        )
        assert service.catalog.duplicate_ids == ["Shop.Cart.Core.T001"]
        result = service.compose_run({"tag_filter": "Cart"}, title="Cart", origin="test")
        planned = result.spec["selection"]["materialized"]["item_ids"]
        assert sorted(planned) == ["Shop.Cart.Core.T001", "Shop.Cart.Core.one", "Shop.Cart.Core.two"]
        assert result.run.planned_count == 3
        assert any("share an id" in warning for warning in result.warnings)
        preview = service.preview({"tag_filter": "Cart"})
        assert len(preview.items) == 3, "the preview shows what would be planned"


# -- what a deployment loads -------------------------------------------------------------------------


class TestConfiguredPlugins:
    def test_meta_and_doctor_list_what_is_loaded_including_import_path_plugins(
        self, make_client, tmp_path, capsys
    ):
        client, _service = make_client(runners={**SCRIPTED, "demo": {}})
        configured = {
            (entry["section"], entry["id"]): entry
            for entry in client.get(f"{API}/meta").json()["configured"]
        }
        scripted = configured[("runners", "scripted")]
        assert scripted["plugin"] == "tests.test_hardening:ScriptedRunner"
        assert scripted["distribution"] == ""  # not from an installed distribution
        demo = configured[("runners", "demo")]
        assert demo["plugin"].endswith(":DemoRunner")
        assert (demo["distribution"], demo["version"]) == ("runcomposer", __version__)
        assert ("store", "sqlite") in configured and ("sources", "demo-corpus") in configured

        assert cli_main(["doctor", "--config", str(tmp_path / "config.yaml")]) == 0
        out = capsys.readouterr().out
        assert "configured (what this deployment loads):" in out
        assert "tests.test_hardening:ScriptedRunner" in out


# -- secrets ---------------------------------------------------------------------------------------------


class TestSecretsStaySecret:
    def test_a_plugin_that_quotes_a_rejected_value_does_not_leak_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHOP_ENDPOINT", "ftp://deploy:hunter2-very-secret@stage.shop.example")
        config = load_config(
            str(
                write_config(
                    tmp_path,
                    runners={
                        "picky": {
                            "module": "tests.test_hardening:PickyRunner",
                            "endpoint": "${env:SHOP_ENDPOINT}",
                        }
                    },
                )
            )
        )
        with Service(config) as service, pytest.raises(ConfigError) as caught:
            service.validate()
        message = str(caught.value)
        assert message.startswith("runners.picky: ValueError: cannot use endpoint")
        assert "hunter2" not in message and "‹from the environment›" in message
        assert caught.value.__cause__ is None, "the original error would carry the value"

    def test_the_shipped_plugins_do_not_quote_the_value_at_all(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHOP_HOOK", "ftp://deploy:hunter2-very-secret@chat.shop.example/hook")
        config = load_config(
            str(
                write_config(
                    tmp_path, observers={"chat": {"plugin": "webhook", "url": "${env:SHOP_HOOK}"}}
                )
            )
        )
        with Service(config) as service, pytest.raises(ConfigError) as caught:
            service.validate()
        assert "hunter2" not in str(caught.value) and "must start with http" in str(caught.value)

    def test_a_resolved_secret_is_in_no_log_line_of_a_whole_run(
        self, make_service, monkeypatch, caplog
    ):
        monkeypatch.setenv("SHOP_RUNNER_TOKEN", "hunter2-very-secret")
        seen: dict[str, Any] = {}

        def run(ctx: DispatchContext) -> None:
            seen["token"] = ctx.options["token"]
            ctx.log("starting on the staging stage")
            ctx.declare(["1"])
            ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        SCRIPT["dispatch"] = run
        with caplog.at_level(logging.DEBUG, logger="runcomposer"):
            service = make_service(
                runners={
                    "scripted": {
                        "module": "tests.test_hardening:ScriptedRunner",
                        "endpoint": "https://stage.shop.example",
                        "token": "${env:SHOP_RUNNER_TOKEN}",
                    }
                }
            )
            service.validate()
            run_id = compose(service, "Auth-SSO")
            service.dispatch(run_id, "scripted", foreground=True)
            service.runner_infos(refresh=True)
        assert seen["token"] == "hunter2-very-secret", "the runner itself does get it"
        assert caplog.records, "the run was logged"
        assert "hunter2" not in caplog.text
        assert "hunter2" not in json.dumps(service.require_spec(run_id))
        assert "hunter2" not in json.dumps([dict(e.data) for e in service.store.events(run_id)])


    @pytest.mark.parametrize("fail", ["refuse", "raise"])
    def test_a_runner_that_quotes_its_token_at_runtime_does_not_leak_it(
        self, make_client, monkeypatch, caplog, fail
    ):
        """A refusal reason, an error after the hand-off, a log line, the
        health detail: wherever a runner puts a configured secret, what is
        answered, recorded and logged has it blanked out."""
        monkeypatch.setenv("SHOP_STAGE_TOKEN", "hunter2-very-secret")
        with caplog.at_level(logging.DEBUG, logger="runcomposer"):
            client, service = make_client(
                runners={
                    "leaky": {
                        "module": "tests.test_hardening:LeakyRunner",
                        "token": "${env:SHOP_STAGE_TOKEN}",
                        "fail": fail,
                    }
                }
            )
            answer = client.post(
                f"{API}/runs",
                json={"selection": {"tag_filter": "Auth-SSO"}, "dispatch": {"runner": "leaky"},
                      "keep_run": True},
            )
            run_id = answer.json().get("run_id") or answer.json()["run"]["id"]
            service.dispatcher.join(30)
            detail = client.get(f"{API}/runs/{run_id}").json()
            runners = client.get(f"{API}/runners", params={"refresh": "true"}).json()
            events = client.get(f"{API}/runs/{run_id}/events").json()
        if fail == "refuse":
            assert answer.status_code == 409 and "Authorization: Bearer ‹from the environment›" in answer.json()["detail"]
        else:
            assert detail["state"] == "FAILED"
            assert "‹from the environment›" in detail["dispatches"][-1]["error"]
            assert detail["dispatches"][-1]["plan"] == "one shard, token ‹from the environment›"
        for answered in (answer.json(), detail, runners, events):
            assert "hunter2" not in json.dumps(answered, ensure_ascii=False)
        assert runners[0]["health"]["detail"] == "probe with ‹from the environment› was rejected"
        assert "hunter2" not in caplog.text
        assert "hunter2" not in json.dumps(
            [dict(e.data) for e in service.store.events(run_id)], ensure_ascii=False
        )


# -- the marker -------------------------------------------------------------------------------------------


class TestTheMarkerIsWrittenWhole:
    def test_no_temporary_file_is_left_and_the_marker_is_complete_json(self, service, tmp_path):
        exported = service.export(compose(service, "Auth-SSO"))
        spec_file = tmp_path / "spec.json"
        spec_file.write_bytes(exported.content)
        out = tmp_path / "bundle"
        assert runcomposer_exec.main([str(spec_file), "--out", str(out), "--simulate"]) == 0
        assert sorted(path.name for path in out.iterdir()) == ["results.json", "runcomposer_run.json"]
        marker = json.loads((out / "runcomposer_run.json").read_text(encoding="utf-8"))
        assert marker["spec_sha256"] == exported.dispatch.spec_sha256


# -- the version has one source -------------------------------------------------------------------------


def test_the_readme_and_the_changelog_speak_of_this_version():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"Version {__version__}" in readme or f"**{__version__}**" in readme, (
        "README.md names another version than runcomposer.__version__"
    )
    first_entry = next(line for line in changelog.splitlines() if line.startswith("## "))
    assert first_entry.startswith(f"## {__version__} "), first_entry


# -- TLS to the CI server -----------------------------------------------------------------------------------


@pytest.fixture()
def tls_jenkins(tmp_path: Path):
    """The Jenkins stand-in behind TLS, with a certificate made for the test
    (needs the `openssl` command; skipped where there is none)."""
    from .test_jenkins import JOB, FakeJenkins

    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("needs the openssl command to make a certificate")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    made = subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        capture_output=True,
        text=True,
    )
    if made.returncode != 0:
        pytest.skip(f"this openssl cannot make the certificate: {made.stderr.strip()[:200]}")
    fake = FakeJenkins()
    fake.add_job(JOB)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    fake.server.socket = context.wrap_socket(fake.server.socket, server_side=True)
    fake.url = f"https://localhost:{fake.server.server_address[1]}"
    yield fake, cert
    fake.close()


class TestJenkinsOverTls:
    def test_the_certificate_is_verified_against_the_ca_bundle(self, make_service, tls_jenkins):
        from .test_jenkins import RUNNER, FakeJenkins, jenkins_service

        fake, cert = tls_jenkins
        service = jenkins_service(make_service, fake, ca_bundle=str(cert))
        health = service.runner_health(RUNNER, refresh=True)
        assert health.status == "ok" and FakeJenkins.VERSION in health.detail

    def test_an_unknown_certificate_is_refused_by_default(self, make_service, tls_jenkins):
        from .test_jenkins import RUNNER, jenkins_service

        fake, _cert = tls_jenkins
        health = jenkins_service(make_service, fake).runner_health(RUNNER, refresh=True)
        assert health.status == "down" and "certificate" in health.detail.lower()
        assert not any(seen.path.startswith("/api/json") for seen in fake.seen)

    def test_verify_tls_false_is_the_explicit_opt_out(self, make_service, tls_jenkins):
        from .test_jenkins import RUNNER, jenkins_service

        fake, _cert = tls_jenkins
        service = jenkins_service(make_service, fake, verify_tls=False)
        assert service.runner_health(RUNNER, refresh=True).status == "ok"


def test_environment_is_not_consulted_for_which_file_to_load(tmp_path, monkeypatch):
    """Secrets come from the environment; the choice of config file and of
    plugins never does."""
    monkeypatch.setenv("RUNCOMPOSER_CONFIG", str(tmp_path / "elsewhere.yaml"))
    monkeypatch.chdir(tmp_path)
    assert load_config().path is None
    assert os.environ["RUNCOMPOSER_CONFIG"]


def test_the_workflows_only_name_test_files_that_exist():
    """A CI step that runs a test file which was renamed away passes nothing
    and proves nothing."""
    import re

    named = set()
    for workflow in (REPO / ".github" / "workflows").glob("*.yml"):
        named |= set(re.findall(r"\btests/[\w/.-]+\.py\b", workflow.read_text(encoding="utf-8")))
    assert named, "the robot job names its test files"
    missing = sorted(path for path in named if not (REPO / path).is_file())
    assert not missing, f"workflows name test files that do not exist: {missing}"


class TestWhatShowSaysAboutLiveStatus:
    def test_a_cancelled_run_is_not_described_as_still_executing(self, tmp_path, capsys):
        release = threading.Event()

        def stream_then_wait(ctx: DispatchContext) -> None:
            ctx.declare({"1": list(ctx.item_ids)})
            ctx.live("1", ctx.item_ids[0], "PASS", duration_ms=5)
            SCRIPT["streamed"] = True
            while not ctx.cancelled:
                release.wait(0.01)
            ctx.raise_if_cancelled()

        SCRIPT["dispatch"] = stream_then_wait
        config = str(write_config(tmp_path, runners=SCRIPTED))
        with Service(load_config(config)) as service:
            run_id = compose(service, "Auth-SSO")
            service.dispatch(run_id, "scripted")
            wait_until(lambda: SCRIPT.get("streamed"), what="the runner to stream one item")
            assert cli_main(["show", run_id, "--config", config]) == 0
            assert "live: 1 item(s) reported so far by shards still executing" in capsys.readouterr().out
            service.cancel(run_id)
            assert service.wait(run_id, timeout=30).state == "CANCELLED"
        assert cli_main(["show", run_id, "--config", config]) == 0
        out = capsys.readouterr().out
        assert "still executing" not in out
        assert "last live status of shards that never delivered: 1 item(s)" in out


class TestAShardNobodyDeclared:
    def test_a_delivery_under_an_undeclared_label_is_recorded_with_a_warning(self, make_service):
        def undeclared(ctx: DispatchContext) -> None:
            ctx.declare([])
            ctx.deliver("surprise", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])

        SCRIPT["dispatch"] = undeclared
        service = make_service(runners=SCRIPTED)
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "scripted", foreground=True)
        warnings = [e.data["message"] for e in service.store.events(run_id) if e.kind == "warning"]
        assert any("'surprise' was not declared" in message for message in warnings)
