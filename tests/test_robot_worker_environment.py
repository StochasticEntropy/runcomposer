"""A Robot worker's working directory, environment and data
(``ShardPayload.cwd`` / ``env`` / ``worker_data``), and the lines the
supervisor writes when a shard starts and ends — with real spawned workers
and a real suite (DESIGN.md §6.6).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from runcomposer.sdk.robot import Execution, ShardPayload, read_shard_result, run_shard, supervise

from .conftest import needs_robot
from .test_robot_pool import executed_tests, shop_suite

pytestmark = needs_robot

COUPONS = (
    "*** Settings ***\nLibrary    OperatingSystem\n\n"
    "*** Test Cases ***\n"
    "Coupon File Is Read From The Checkout\n"
    "    [Tags]    Checkout\n"
    "    ${coupons}=    Get File    data/coupons.txt\n"
    "    Should Be Equal    ${coupons.strip()}    spring10\n\n"
    "Region Comes From The Environment\n"
    "    [Tags]    Checkout\n"
    "    Should Be Equal    %{SHOP_REGION}    eu\n"
    "    Variable Should Not Exist    \\${SHOP_REGION}\n"
)
IDS = ("Shop.Checkout.Coupon File Is Read From The Checkout", "Shop.Checkout.Region Comes From The Environment")


def note_setup(info: dict[str, Any]) -> None:
    """A ``worker_setup`` callable: notes what it was handed, where it ran."""
    Path(info["out_dir"], "setup.json").write_text(
        json.dumps({"data": info["data"], "cwd": os.getcwd(), "region": os.environ.get("SHOP_REGION")}),
        encoding="utf-8",
    )


def checkout(tmp_path: Path) -> tuple[Path, Path]:
    """The suite, and a checkout directory holding the files it reads
    relative to the working directory."""
    root = shop_suite(tmp_path / "corpus", COUPONS)
    place = tmp_path / "shop checkout"
    (place / "data").mkdir(parents=True)
    (place / "data" / "coupons.txt").write_text("spring10\n", encoding="utf-8")
    return root, place


class TestTheWorkersSurroundings:
    def test_a_spawned_worker_runs_in_cwd_with_env_and_its_setup_gets_the_data(self, tmp_path):
        from runcomposer.app.livechannel import FileLiveChannel, LiveTail

        root, place = checkout(tmp_path)
        live = tmp_path / "live"
        live.mkdir()
        shard = ShardPayload(
            shard="eu-01", out_dir=str(tmp_path / "out"), executions=(Execution(str(root), IDS),),
            lane="eu", channel=FileLiveChannel(str(live)),
            worker_setup=("tests.test_robot_worker_environment:note_setup",),
            cwd=str(place), env={"SHOP_REGION": "eu"},
            worker_data={"booking": "staging-eu-7", "carts": [1, 2]},
        )
        (outcome,) = supervise([shard], max_workers=1)

        assert outcome.problem is None, outcome.problem
        assert {name: status for name, (status, _t, _m) in executed_tests(tmp_path / "out" / "output.xml").items()} == {
            IDS[0]: "PASS", IDS[1]: "PASS",
        }
        setup = json.loads((tmp_path / "out" / "setup.json").read_text(encoding="utf-8"))
        assert setup["data"] == {"booking": "staging-eu-7", "carts": [1, 2]}
        assert Path(setup["cwd"]).resolve() == place.resolve() and setup["region"] == "eu"
        assert "SHOP_REGION" not in os.environ, "the dispatching process is untouched"

        logs = [entry["log"] for entry in LiveTail(live).read() if "log" in entry]
        assert logs[0] == "shard eu-01 (lane eu): started"
        assert re.fullmatch(r"shard eu-01 \(lane eu\): finished after \d+\.\ds", logs[-1])

    def test_without_cwd_the_relative_file_is_not_found(self, tmp_path):
        """The other side of the first test: it is the working directory
        that makes the file readable."""
        root, _place = checkout(tmp_path)
        shard = ShardPayload(
            shard="eu-01", out_dir=str(tmp_path / "out"), executions=(Execution(str(root), IDS[:1]),),
        )
        (outcome,) = supervise([shard], max_workers=1)
        (status, _tags, message) = executed_tests(tmp_path / "out" / "output.xml")[IDS[0]]
        assert status == "FAIL" and "coupons.txt" in message and outcome.problem is None

    def test_in_process_cwd_and_env_apply_for_the_call_only(self, tmp_path, monkeypatch):
        root, place = checkout(tmp_path)
        monkeypatch.delenv("SHOP_REGION", raising=False)
        monkeypatch.chdir(tmp_path)
        here = os.getcwd()
        run_shard(
            ShardPayload(
                shard="eu-01", out_dir="out", executions=(Execution(str(root), IDS),),
                cwd=str(place), env={"SHOP_REGION": "eu"},
            )
        )
        assert os.getcwd() == here and "SHOP_REGION" not in os.environ
        # A relative out_dir is where the caller was, not inside cwd.
        result = read_shard_result(tmp_path / "out")
        assert result is not None and result.error is None
        (output,) = result.outputs
        assert {s for s, _t, _m in executed_tests(Path(output.path)).values()} == {"PASS"}
