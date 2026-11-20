"""Four ways to execute — one runner contract (docs/connectors.md).

Each structurally different executor has a runnable example under
``examples/``; each test here runs *that example's config file*, copied with
its neighbours into a temporary directory (its relative paths resolve
against the file, DESIGN.md §8), through the real CLI:

1. in-process pool  — ``examples/robot-pool/config.yaml``   (robot-pool)
2. remote CI trigger — ``examples/jenkins/config.yaml``     (jenkins, poll + callback)
3. local command    — ``examples/pytest-shop/config.yaml``  (command → junit-xml)
4. export / remote agent — ``examples/remote-agent/``       (tests/test_remote_agent.py
   runs that kit's whole round trip; it is not repeated here)

The Jenkins test uses the protocol-faithful stand-in of tests/test_jenkins.py;
ci/jenkins/e2e.py drives the same config against a real Jenkins.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from runcomposer.cli import main

from .conftest import REPO, needs_robot
from .test_jenkins import FakeJenkins

EXAMPLES = REPO / "examples"


def copy_examples(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "examples"
    for name in names:
        shutil.copytree(EXAMPLES / name, root / name, ignore=shutil.ignore_patterns("state", "__pycache__"))
    return root


def run_cli(capsys, *args: str) -> tuple[int, dict[str, Any]]:
    code = main([*args, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out)


@needs_robot
def test_1_in_process_pool_robot(tmp_path, capsys):
    root = copy_examples(tmp_path, "robot-pool", "robot-shop")
    config = str(root / "robot-pool" / "config.yaml")
    code, run = run_cli(capsys, "run", "Cart-Basics", "--runner", "robot-pool", "--config", config)
    assert code == 0, run
    assert (run["state"], run["completion"]) == ("COMPLETE", "PASS")
    assert sorted(s["shard"] for s in run["shards"]) == ["eu-1", "eu-2", "us-1", "us-2"]
    assert run["verdict_summary"] == {"PASS": 8}  # 4 tests × 2 partitions
    assert {a["name"].split(" ")[0] for a in run["artifacts"]} >= {"output.xml", "log.html", "report.html"}
    # The state stayed next to the config file, wherever the command ran.
    assert (root / "robot-pool" / "state" / "runcomposer.db").is_file()

    code, run = run_cli(capsys, "run", "Cart-Basics", "--runner", "robot-pool",
                        "--param", "partitions=[eu]", "--config", config)
    # One partition chosen per run: its shards only (chunked over all four workers now).
    assert code == 0 and {s["shard"].split("-")[0] for s in run["shards"]} == {"eu"}
    assert run["verdict_summary"] == {"PASS": 4}


def test_3_local_command_pytest(tmp_path, capsys, monkeypatch):
    root = copy_examples(tmp_path, "pytest-shop")
    config = str(root / "pytest-shop" / "config.yaml")
    # The example names `python` on PATH: make it this interpreter.
    monkeypatch.setenv("PATH", os.pathsep.join([str(Path(sys.executable).parent), os.environ["PATH"]]))
    code, run = run_cli(capsys, "run", "Cart", "--runner", "pytest", "--config", config)
    assert code == 1, "the example suite ships one failing test, and a FAIL run exits 1"
    assert (run["state"], run["completion"]) == ("COMPLETE", "FAIL")
    failed = [v["item_id"] for v in run["verdicts"] if v["status"] == "FAIL"]
    assert failed == ["test_cart.py::test_total_never_negative"]
    assert sorted(s["shard"] for s in run["shards"]) == ["1", "2"]
    assert {d["format"] for d in run["deliveries"]} == {"junit-xml"}


def consumer_job(build: Any) -> None:
    """What the ci/jenkins job's build step produces (runcomposer-exec
    --simulate): the reference verdicts JSON and the marker, archived under
    results/. Here every planned item passes."""
    spec = json.loads(build.params["SPEC_JSON"])
    ids = spec["selection"]["materialized"]["item_ids"]
    build.artifacts["results/results.json"] = json.dumps({
        "format": "runcomposer-verdicts",
        "verdicts": [{"name": item_id, "status": "PASS", "duration_ms": 5} for item_id in ids],
    }).encode()
    build.artifacts["results/runcomposer_run.json"] = json.dumps(
        {"run_id": spec["run"]["id"], "dispatch_id": build.params["DISPATCH_ID"]}
    ).encode()


@pytest.fixture()
def jenkins_job():
    fake = FakeJenkins()
    fake.add_job("runcomposer-consumer", consumer_job)
    yield fake
    fake.close()


def test_2_remote_ci_trigger_jenkins_poll_and_callback(tmp_path, capsys, monkeypatch, jenkins_job):
    root = copy_examples(tmp_path, "jenkins")
    config = str(root / "jenkins" / "config.yaml")
    monkeypatch.setenv("JENKINS_URL", jenkins_job.url)
    monkeypatch.setenv("RUNCOMPOSER_CALLBACK_BASE", "http://runcomposer.shop.example:8100")

    # poll: runcomposer follows the queue item to the build and delivers.
    code, run = run_cli(capsys, "run", "Payments-Cards", "--runner", "jenkins-poll", "--config", config)
    assert code == 0, run
    assert (run["state"], run["completion"]) == ("COMPLETE", "PASS")
    assert any(a["name"] == "Jenkins build" and a["kind"] == "url" for a in run["artifacts"])

    # callback: the trigger hands over the spec, a callback URL and a token;
    # the run waits for the job's POST (not sent by the stand-in).
    code, run = run_cli(capsys, "run", "Payments-Cards", "--runner", "jenkins", "--no-wait", "--config", config)
    assert run["state"] == "AWAITING_RESULTS", run
    sent = jenkins_job.triggers()[-1].form
    assert sent["CALLBACK_URL"] == f"http://runcomposer.shop.example:8100/api/v1/runs/{run['id']}/results"
    assert sent["INGEST_TOKEN"].startswith("rct_")
    assert json.loads(sent["SPEC_JSON"])["run"]["id"] == run["id"]


def test_4_export_remote_agent_is_covered_by_its_own_round_trip_test():
    """The fourth way has its own end-to-end test; this pins that it exists
    and drives the documented kit."""
    text = (REPO / "tests" / "test_remote_agent.py").read_text(encoding="utf-8")
    assert "examples" in text and "sync.sh" in text and "test_reaches_complete_with_real_robot_verdicts" in text
