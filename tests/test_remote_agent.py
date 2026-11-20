"""The remote-agent adopter kit (examples/remote-agent, DESIGN.md §6.2c): the
agent's Robot adapter, its §3.3 obligations, and the full transport round trip
— compose → carry → execute → carry back → ingest → COMPLETE.

These need Robot Framework (`pip install '.[robot]'`) and skip
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

from runcomposer.app.inbox import InboxWatcher
from runcomposer.sdk import Service, load_config

from .conftest import REPO, ROBOT_SHOP, needs_robot

pytestmark = [
    needs_robot,
    pytest.mark.skipif(os.name == "nt", reason="the kit's transport script is POSIX shell"),
]

KIT = REPO / "examples" / "remote-agent"
CORPUS = ROBOT_SHOP


def parse_output(path):
    """What a Robot output says, through the stock parser."""
    from runcomposer.plugins.robot_output_parser import RobotOutputXmlParser

    return RobotOutputXmlParser().parse(path)

# A hand-picked subset the adapter tests request explicitly.
PAYMENTS_IDS = [
    "Tests.Payments.Visa Payment Succeeds",
    "Tests.Payments.Mastercard Payment Succeeds",
    "Tests.Payments.Declined Card Shows Error",
    "Tests.Payments.Refund Restores Balance",
    "Tests.Payments.Expired Card Is Rejected Loudly",
]

# Everything the default `RC_FILTER=Payments` compose selects — the whole
# Payments area of the shipped corpus, which the round trip executes for real.
PAYMENTS_TAG_IDS = [
    "Tests.Payments.Visa Payment Succeeds",
    "Tests.Payments.Mastercard Payment Succeeds",
    "Tests.Payments.Declined Card Shows Error",
    "Tests.Payments.Three D Secure Challenge Completes",
    "Tests.Payments.Card Number Is Masked In Receipts",
    "Tests.Payments.Expired Card Is Rejected Loudly",
    "Tests.Payments.Wallet Payment Succeeds",
    "Tests.Payments.Wallet Top Up Is Booked",
    "Tests.Payments.Wallet Currency Conversion Rounds Half Up",
    "Tests.Payments.Wallet Timeout Falls Back To Card",
    "Tests.Payments.Refund Restores Balance",
    "Tests.Payments.Partial Refund Keeps Remainder",
    "Tests.Payments.Refund Of A Refund Is Refused",
]
FAILING_ID = "Tests.Payments.Expired Card Is Rejected Loudly"


def kit_config(work: Path) -> Path:
    """The kit's own config.yaml, copied to a scratch directory with only the
    corpus root repointed at this repository's example suite (the executing
    machine's checkout is a separate thing). The store path stays exactly as
    shipped — relative — so the round trip below exercises the §8 rule that
    it lands next to *this* config file rather than next to whatever
    directory sync.sh happened to be invoked from."""
    data = yaml.safe_load((KIT / "config.yaml").read_text(encoding="utf-8"))
    data["sources"]["robotframework"]["root"] = str(CORPUS)
    assert data["store"]["sqlite"]["path"] == "state/runcomposer.db"  # as shipped
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
    # Deliberately NOT the config's directory: sync.sh used to have to `cd`
    # there, and the loop must now work from anywhere (DESIGN.md §8).
    elsewhere = work.parent
    return subprocess.run(
        [str(KIT / "sync.sh")], env=env, cwd=str(elsewhere), capture_output=True, text=True
    )


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


def drop_test_case(path: Path, name: str) -> None:
    """Delete one test case from a .robot file: its name line plus the indented
    body, up to the next unindented line (the next test case, or EOF)."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.rstrip("\n") == name)
    after = range(start + 1, len(lines))
    end = next((i for i in after if lines[i].strip() and not lines[i][:1].isspace()), len(lines))
    path.write_text("".join(lines[:start] + lines[end:]), encoding="utf-8")


@pytest.fixture()
def drifted_checkout(tmp_path):
    """The executing machine's checkout lost one test after compose time. The
    copy keeps the directory name `tests`: it is part of every longname."""
    checkout = tmp_path / "checkout" / "tests"
    shutil.copytree(CORPUS, checkout)
    drop_test_case(checkout / "payments.robot", "Refund Restores Balance")
    return checkout


class TestRobotAdapter:
    """agent/robot_command.py — the only file in the kit that knows what a
    test is."""

    def test_runs_exactly_the_requested_ids(self, tmp_path):
        picked = ["Tests.Payments.Visa Payment Succeeds", "Tests.Catalog.Sort By Price Ascending"]
        process, out_dir = run_adapter(tmp_path, picked)
        assert process.returncode == 0, process.stderr
        parsed = {p.native_name: p.status for p in parse_output(out_dir)}
        assert parsed == dict.fromkeys(picked, "PASS")

    def test_a_failing_test_still_produces_a_valid_bundle(self, tmp_path):
        failing = "Tests.Payments.Expired Card Is Rejected Loudly"
        process, out_dir = run_adapter(tmp_path, [failing])
        assert process.returncode == 0
        parsed = parse_output(out_dir)
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
        executed = {p.native_name for p in parse_output(out_dir)}
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


class TestShippedConfigIsDirectoryIndependent:
    """§8: the kit ships a config of relative paths. Every one of them — the
    core's inbox/quarantine/artifacts *and* the plugin sections — resolves
    against the config file's directory, which is why `sync.sh` no longer has
    to `cd` there first."""

    def test_relative_paths_all_land_in_the_kit_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # anywhere but the config's directory
        config = load_config(str(KIT / "config.yaml"))

        assert config.inbox_dir == KIT / "state" / "inbox"
        assert config.quarantine_dir == KIT / "state" / "quarantine"
        assert config.artifact_dir == KIT / "state" / "artifacts"

        # The store path as the store will be constructed with it — read off
        # the resolved plugin spec, without building the store (which would
        # drop runtime state into the repository).
        assert Path(config.store_spec().options["path"]) == KIT / "state" / "runcomposer.db"

        # The source root: it used to raise "robotframework source root not
        # found: ../robot-shop/tests" from any directory but the kit's.
        with Service(config) as service:
            items = {item.id for item in service.catalog.items()}
        assert "Tests.Payments.Visa Payment Succeeds" in items
        assert not (KIT / "state").exists() or not (KIT / "state" / "runcomposer.db").exists()

    def test_the_kit_configures_no_runner_and_validates(self, tmp_path):
        """The pattern exports and ingests; nothing is dispatched in-process —
        so the shipped config leaves `runners:` out, and that is a valid
        deployment (checked on a copy, so nothing is created in the repo)."""
        with Service(load_config(str(kit_config(tmp_path)))) as service:
            service.validate()
            assert service.runner_infos() == []
            assert service.config.tokens_required is False

    def test_sync_sh_does_not_cd_into_the_config_directory(self):
        """The workaround is gone, and stays gone."""
        script = (KIT / "sync.sh").read_text(encoding="utf-8")
        assert 'cd "$config_dir"' not in script


@pytest.fixture(scope="module")
def round_trip(tmp_path_factory):
    """One real loop over the local transport, shared by the tests below."""
    work = tmp_path_factory.mktemp("remote-agent")
    result = run_sync(work)
    assert result.returncode == 0, result.stdout + result.stderr
    service = Service(load_config(str(work / "config.yaml")))
    runs = service.list_runs(limit=2).runs
    assert len(runs) == 1
    yield {"work": work, "service": service, "run": service.require_run(runs[0].id), "out": result}
    service.close()


class TestRoundTrip:
    def test_reaches_complete_with_real_robot_verdicts(self, round_trip):
        run, service = round_trip["run"], round_trip["service"]
        assert run.state == "COMPLETE"
        assert run.completion == "FAIL"  # the corpus ships one deliberately failing test
        statuses = {v.item_id: v.status for v in service.store.verdicts_for(run.id)}
        assert statuses == {
            item_id: "FAIL" if item_id == FAILING_ID else "PASS" for item_id in PAYMENTS_TAG_IDS
        }
        assert {d.format for d in run.deliveries} == {"robot-output-xml"}
        assert dict(run.latest_dispatch.summary) == {"PASS": 12, "FAIL": 1}  # nothing MISSING

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
        assert dispatch.shards == ("1",) and dispatch.state == "COMPLETE"

    def test_the_spec_that_travelled_is_the_bytes_the_export_recorded(self, round_trip):
        """What was carried out is byte for byte what the export dispatch's
        hash was computed over — and it can be downloaded again."""
        import hashlib

        run, service = round_trip["run"], round_trip["service"]
        carried = (round_trip["work"] / "state" / "outbox" / "spec.json").read_bytes()
        dispatch = run.dispatches[-1]
        assert hashlib.sha256(carried).hexdigest() == dispatch.spec_sha256
        content, media_type, _ = service.dispatch_spec(run.id, dispatch.dispatch_id)
        assert content == carried and media_type == "application/json"
        # Tokens are disabled in this kit (results return by file drop): none
        # was minted, and the document carries none either way.
        assert "token" not in json.loads(carried)["results"]
        assert service.store.ingest_tokens(run.id) == {}
        assert "ingest token" not in round_trip["out"].stdout + round_trip["out"].stderr

    def test_the_agent_executed_exactly_the_materialized_ids(self, round_trip):
        run, service = round_trip["run"], round_trip["service"]
        spec = service.require_spec(run.id)
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
        # The marker — written last by runcomposer-exec — is the bundle's
        # completion signal, so the watcher takes it at once.
        reports = InboxWatcher(service, drop).poll_once()
        assert [(r.run_id, r.outcome) for r in reports] == [(run.id, "duplicate")]
        assert not (drop / run.id).exists() and len(list((drop / "processed").iterdir())) == 1

    def test_drift_stops_the_loop_and_leaves_the_run_awaiting(self, tmp_path, drifted_checkout):
        result = run_sync(tmp_path, suite_root=drifted_checkout)
        assert result.returncode != 0
        assert "refusing to execute" in result.stdout + result.stderr
        assert list((tmp_path / "state" / "inbox").glob("*/output.xml")) == []
        with Service(load_config(str(tmp_path / "config.yaml"))) as service:
            assert [r.state for r in service.list_runs(limit=2).runs] == ["AWAITING_RESULTS"]

    def test_a_refused_execution_leaves_no_marker_behind(self, tmp_path, drifted_checkout):
        """The marker says "this bundle is complete". runcomposer-exec used to
        write it even after the command had failed — so the refused run's
        half-bundle looked finished to whatever carried it home. Now the
        directory the agent left has the drift report and NO marker, and a
        watcher that is handed it anyway takes nothing in."""
        result = run_sync(tmp_path, suite_root=drifted_checkout)
        assert result.returncode != 0
        assert "no marker written" in result.stdout + result.stderr
        (left_behind,) = (tmp_path / "state" / "remote").glob("*/results")
        assert (left_behind / "drift.json").is_file()
        assert list((tmp_path / "state").rglob("runcomposer_run.json")) == []

        with Service(load_config(str(tmp_path / "config.yaml"))) as service:
            (run,) = service.list_runs(limit=2).runs
            drop = tmp_path / "state" / "inbox"
            shutil.copytree(left_behind, drop / run.id)
            watcher = InboxWatcher(service, drop, poll_interval_s=1, settle_s=5)
            # Without the marker a bundle counts as complete only once it has
            # held still for the settle window — and then it is quarantined
            # (it names no run), never attached.
            assert watcher.poll_once(now=100.0) == []
            assert watcher.poll_once(now=102.0) == []
            (report,) = watcher.poll_once(now=106.0)
            assert report.reason == "unsolicited" and report.entry_id
            assert service.require_run(run.id).state == "AWAITING_RESULTS"
            assert service.store.verdicts_for(run.id) == []
