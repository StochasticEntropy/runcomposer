"""The remote-agent adopter kit (examples/remote-agent, DESIGN.md §6.2c): the
agent's Robot adapter, its §3.3 obligations, and the full transport round trip
— compose → carry → execute → carry back → ingest → COMPLETE.

These need Robot Framework (`pip install 'runcomposer[robot]'`) and skip
cleanly without it, like the rest of the robot plugin family.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("robot", reason="requires the runcomposer[robot] extra")

from runcomposer.config import load_config
from runcomposer.inbox import InboxWatcher
from runcomposer.plugins.robot_output_parser import RobotOutputXmlParser
from runcomposer.service import Service

REPO = Path(__file__).parent.parent
KIT = REPO / "examples" / "remote-agent"
CORPUS = REPO / "examples" / "robot-shop" / "tests"

PAYMENTS_IDS = [
    "Tests.Payments.Visa Payment Succeeds",
    "Tests.Payments.Mastercard Payment Succeeds",
    "Tests.Payments.Declined Card Shows Error",
    "Tests.Payments.Refund Restores Balance",
    "Tests.Payments.Expired Card Is Rejected Loudly",
]


def kit_config(work: Path) -> Path:
    """The kit's own config.yaml with the corpus root and the store path made
    absolute — the README's recipe for keeping the state somewhere other than
    next to the shipped example. Everything else is the committed file."""
    data = yaml.safe_load((KIT / "config.yaml").read_text(encoding="utf-8"))
    data["sources"]["robotframework"]["root"] = str(CORPUS)
    data["store"]["sqlite"]["path"] = str(work / "state" / "runcomposer.db")
    path = work / "config.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def runcomposer_shim(work: Path) -> Path:
    """`runcomposer` need not be on PATH under pytest: hand sync.sh a shim
    running the CLI out of the interpreter that runs these tests."""
    path = work / "runcomposer"
    path.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -m runcomposer.cli "$@"\n', encoding="utf-8"
    )
    path.chmod(0o755)
    return path


def run_sync(work: Path, *, suite_root: Path = CORPUS, **env_extra):
    (work / "state").mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "RC_CONFIG": str(kit_config(work)),
        "RUNCOMPOSER": str(runcomposer_shim(work)),
        "RC_PYTHON": sys.executable,
        "RC_SUITE_ROOT": str(suite_root),
        "RC_TITLE": "Kit round trip",
        "RC_ALLOW_DRIFT": "0",
        **env_extra,
    }
    return subprocess.run([str(KIT / "sync.sh")], env=env, capture_output=True, text=True)


def run_adapter(work: Path, item_ids, *, suite_root: Path = CORPUS, **env_extra):
    """Invoke agent/robot_command.py the way runcomposer-exec's --command does."""
    work.mkdir(parents=True, exist_ok=True)
    ids_file = work / "item_ids.txt"
    ids_file.write_text("\n".join(item_ids) + "\n", encoding="utf-8")
    out_dir = work / "results"
    process = subprocess.run(
        [
            sys.executable,
            str(KIT / "agent" / "robot_command.py"),
            str(ids_file),
            str(out_dir),
            str(suite_root),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "RC_ALLOW_DRIFT": "0", **env_extra},
    )
    return process, out_dir


@pytest.fixture()
def drifted_checkout(tmp_path):
    """The executing machine's checkout lost one test after compose time. The
    copy keeps the directory name `tests`: it is part of every longname."""
    checkout = tmp_path / "checkout" / "tests"
    shutil.copytree(CORPUS, checkout)
    payments = checkout / "payments.robot"
    text = payments.read_text(encoding="utf-8")
    start = text.index("Refund Restores Balance")
    end = text.index("Expired Card Is Rejected Loudly")
    payments.write_text(text[:start] + text[end:], encoding="utf-8")
    return checkout


class TestRobotAdapter:
    """agent/robot_command.py — the only file in the kit that knows what a
    test is."""

    def test_runs_exactly_the_requested_ids(self, tmp_path):
        picked = ["Tests.Payments.Visa Payment Succeeds", "Tests.Catalog.Sort By Price Ascending"]
        process, out_dir = run_adapter(tmp_path, picked)
        assert process.returncode == 0, process.stderr
        parsed = {p.native_name: p.status for p in RobotOutputXmlParser().parse(out_dir)}
        assert parsed == {name: "PASS" for name in picked}

    def test_a_failing_test_still_produces_a_valid_bundle(self, tmp_path):
        failing = "Tests.Payments.Expired Card Is Rejected Loudly"
        process, out_dir = run_adapter(tmp_path, [failing])
        assert process.returncode == 0
        parsed = RobotOutputXmlParser().parse(out_dir)
        assert [(p.native_name, p.status) for p in parsed] == [(failing, "FAIL")]

    def test_drift_is_refused_by_default(self, tmp_path, drifted_checkout):
        # §3.3 obligation 2: refuse by default.
        process, out_dir = run_adapter(tmp_path / "w", PAYMENTS_IDS, suite_root=drifted_checkout)
        assert process.returncode == 3
        assert "refusing to execute" in process.stderr
        assert not (out_dir / "output.xml").exists()
        drift = json.loads((out_dir / "drift.json").read_text(encoding="utf-8"))
        assert drift["missing_item_ids"] == ["Tests.Payments.Refund Restores Balance"]

    def test_allow_drift_executes_the_intersection(self, tmp_path, drifted_checkout):
        process, out_dir = run_adapter(
            tmp_path / "w", PAYMENTS_IDS, suite_root=drifted_checkout, RC_ALLOW_DRIFT="1"
        )
        assert process.returncode == 0, process.stderr
        executed = {p.native_name for p in RobotOutputXmlParser().parse(out_dir)}
        assert executed == set(PAYMENTS_IDS) - {"Tests.Payments.Refund Restores Balance"}
        assert (out_dir / "drift.json").is_file()  # the difference rides home named

    def test_a_differently_named_suite_root_drifts_on_every_id(self, tmp_path):
        # The id-space trap the README calls out: a Robot longname starts at
        # the suite root's directory name.
        renamed = tmp_path / "checkout" / "acceptance"
        shutil.copytree(CORPUS, renamed)
        process, _ = run_adapter(tmp_path / "w", PAYMENTS_IDS, suite_root=renamed)
        assert process.returncode == 3
        assert "5 of 5 requested item id(s)" in process.stderr
        assert "top-level suite name" in process.stderr


@pytest.fixture(scope="module")
def round_trip(tmp_path_factory):
    """One real loop over the local transport, shared by the tests below."""
    work = tmp_path_factory.mktemp("remote-agent")
    result = run_sync(work)
    assert result.returncode == 0, result.stdout + result.stderr
    service = Service(load_config(str(work / "config.yaml")))
    runs = service.store.list_runs(limit=2)
    assert len(runs) == 1
    return {"work": work, "service": service, "run": service.store.get_run(runs[0].id)}


class TestRoundTrip:
    def test_reaches_complete_with_real_robot_verdicts(self, round_trip):
        run, service = round_trip["run"], round_trip["service"]
        assert run.state == "COMPLETE"
        assert run.completion == "FAIL"  # the corpus ships one deliberately failing test
        statuses = {v.item_id: v.status for v in service.store.verdicts_for(run.id)}
        assert statuses == {
            "Tests.Payments.Visa Payment Succeeds": "PASS",
            "Tests.Payments.Mastercard Payment Succeeds": "PASS",
            "Tests.Payments.Declined Card Shows Error": "PASS",
            "Tests.Payments.Refund Restores Balance": "PASS",
            "Tests.Payments.Expired Card Is Rejected Loudly": "FAIL",
        }
        assert {d.format for d in run.deliveries} == {"robot-output-xml"}

    def test_the_marker_correlates_the_export_dispatch(self, round_trip):
        run = round_trip["run"]
        bundle = round_trip["work"] / "state" / "inbox" / run.id
        marker = json.loads((bundle / "runcomposer_run.json").read_text(encoding="utf-8"))
        dispatch = run.dispatches[-1]
        assert dispatch.mode == "export"  # no runner drove this execution (§6.2c)
        assert marker["run_id"] == run.id
        assert marker["dispatch_id"] == dispatch.dispatch_id
        assert marker["spec_sha256"] == dispatch.spec_sha256  # §5 marker verification
        assert marker["shard"] == "1"

    def test_the_agent_executed_exactly_the_materialized_ids(self, round_trip):
        run, service = round_trip["run"], round_trip["service"]
        spec = service.store.get_spec_document(run.id)
        ids_file = round_trip["work"] / "state" / "inbox" / run.id / "item_ids.txt"
        carried = ids_file.read_text(encoding="utf-8").split("\n")
        assert [i for i in carried if i] == spec["selection"]["materialized"]["item_ids"]

    def test_the_payload_vendors_the_shipped_single_file_consumer(self, round_trip):
        vendored = round_trip["work"] / "state" / "outbox" / "runcomposer_exec.py"
        assert vendored.read_bytes() == (REPO / "src" / "runcomposer_exec.py").read_bytes()
        payload = {p.name for p in vendored.parent.iterdir()}
        assert payload == {"spec.json", "runcomposer_exec.py", "run_agent.sh", "robot_command.py"}

    def test_the_file_drop_watcher_ingests_the_same_bundle(self, round_trip, tmp_path):
        # The return leg's real transport (§5): the same bytes arriving through
        # the watched inbox correlate to the same run — idempotently.
        run, service = round_trip["run"], round_trip["service"]
        drop = tmp_path / "inbox"
        shutil.copytree(round_trip["work"] / "state" / "inbox" / run.id, drop / run.id)
        reports = InboxWatcher(service, drop).poll_once(min_age_s=0)
        assert [(r.run_id, r.outcome) for r in reports] == [(run.id, "duplicate")]

    def test_drift_stops_the_loop_and_leaves_the_run_awaiting(self, tmp_path, drifted_checkout):
        result = run_sync(tmp_path, suite_root=drifted_checkout)
        assert result.returncode != 0
        assert "refusing to execute" in result.stdout + result.stderr
        assert list((tmp_path / "state" / "inbox").glob("*/output.xml")) == []
        service = Service(load_config(str(tmp_path / "config.yaml")))
        assert [r.state for r in service.store.list_runs(limit=2)] == ["AWAITING_RESULTS"]
