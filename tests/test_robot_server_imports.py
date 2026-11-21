"""What a server process has loaded of Robot Framework after it answered
``health`` for a `robot-pool` runner, previewed a run and ran a dispatch
with ``merge_groups`` to completion on real workers (DESIGN.md §6.2a, §6.6).

Measured in a fresh interpreter, where no other test's import can hide what
the server's own calls load:

- with a catalog that is not read through Robot (a manifest) the server
  process loads **nothing** of Robot — no ``robot`` module at all, so its
  result model and its process-global logger are never there: Robot runs in
  the workers and merge processes only;
- with the ``robotframework`` catalog source in the same process, Robot is
  loaded when the catalog is built (the source walks the suites with
  ``robot.api``; importing any part of the ``robot`` package loads all of
  it). The runner adds nothing to that: the same modules before and after,
  and Robot's process-global logger exactly as the source left it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .conftest import REPO, needs_robot, write_config
from .test_robot_pool import FIVE_TESTS, shop_suite
from .test_robot_toolkit import CART

pytestmark = needs_robot

SCRIPT = """\
import json, sys

from runcomposer.sdk import Service, load_config


def robot_modules():
    return sorted(m for m in sys.modules if m == "robot" or m.startswith("robot."))


def logger_state():
    if "robot.output" not in sys.modules:
        return None
    from robot.output import LOGGER
    return {name: repr(value) for name, value in vars(LOGGER).items()}


if __name__ == "__main__":
    service = Service(load_config(sys.argv[1]))
    run_id = service.compose_run({"tag_filter": "Cart"}, title="Cart", origin="test",
                                 runner_id="robot-pool").run.id
    seen = {"catalog": robot_modules()}
    logger = logger_state()
    health = service.runner_health("robot-pool", refresh=True)
    seen["health"] = robot_modules()
    preview = service.plan("robot-pool", selection={"tag_filter": "Cart"})
    seen["plan"] = robot_modules()
    service.dispatch(run_id, "robot-pool", foreground=True)
    seen["dispatch"] = robot_modules()
    run = service.require_run(run_id)
    print(json.dumps({
        "seen": seen,
        "logger_untouched": logger_state() == logger,
        "health": health.status,
        "previewed": sorted(preview.preview.shards),
        "state": [run.state, run.completion],
        "verdicts": len(service.store.verdicts_for(run_id)),
        "merged": sorted(a["name"] for a in service.artifacts.list(run_id) if "(run)" in a["name"]),
    }))
    service.close()
"""


def measure(tmp_path: Path, source: dict) -> dict:
    root = shop_suite(tmp_path, FIVE_TESTS, name="cart.robot")
    config = write_config(
        tmp_path,
        sources=source if source else {"robotframework": {"root": str(root)}},
        runners={"robot-pool": {
            "suite_root": str(root), "partitions": ["staging", "preview"], "partition_mode": "split",
            "max_tests_per_chunk": 1, "chunk_shards": True, "merge_groups": True, "max_workers": 2,
        }},
    )
    script = tmp_path / "serve.py"
    script.write_text(SCRIPT, encoding="utf-8")
    done = subprocess.run(
        [sys.executable, str(script), str(config)], capture_output=True, text=True, timeout=300,
        cwd=str(REPO),
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip().splitlines()[-1])
    # The dispatch really ran on workers and really merged.
    assert result["health"] == "ok" and result["state"] == ["COMPLETE", "FAIL"]
    assert result["verdicts"] == 5 and len(result["previewed"]) == 5
    assert result["merged"] == ["log.html (run)", "output.xml (run)", "report.html (run)"]
    return result


def test_with_a_manifest_catalog_the_server_loads_nothing_of_robot(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"items": [{"id": item, "tags": ["Cart"]} for item in CART]}),
                        encoding="utf-8")
    result = measure(tmp_path, {"manifest": {"path": str(manifest)}})
    assert result["seen"] == {"catalog": [], "health": [], "plan": [], "dispatch": []}


def test_with_the_robot_catalog_source_the_runner_adds_nothing_to_what_the_source_loaded(tmp_path):
    result = measure(tmp_path, {})
    seen = result["seen"]
    assert "robot.result" in seen["catalog"]  # the source's doing: robot.api loads all of robot
    assert seen["health"] == seen["plan"] == seen["dispatch"] == seen["catalog"]
    assert result["logger_untouched"] is True
