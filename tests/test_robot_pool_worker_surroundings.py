"""`robot-pool` tells its workers where to stand (DESIGN.md §6.2a):
``worker_cwd`` (a path option, anchored to the config file), ``worker_env``
(text, ``${env:…}`` allowed), and what a ``pre_dispatch`` step computes once
per dispatch — ``{variables, env, worker_data}`` merged into every shard,
``worker_data`` reaching ``worker_setup`` as ``info["data"]``. With real
spawned workers and a real suite.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from runcomposer.sdk import ConfigError

from .conftest import REPO, needs_robot
from .test_robot_pool import execute, pool_service, shop_suite, workdir

pytestmark = needs_robot

HERE = "tests.test_robot_pool_worker_surroundings"

SUITE = (
    "*** Settings ***\nLibrary    OperatingSystem\n\n"
    "*** Test Cases ***\n"
    "Coupon File Is Read From The Checkout\n"
    "    [Tags]    Checkout\n"
    "    ${coupons}=    Get File    data/coupons.txt\n"
    "    Should Be Equal    ${coupons.strip()}    spring10\n\n"
    "Region Comes From The Worker Environment\n"
    "    [Tags]    Checkout\n"
    "    Should Be Equal    %{SHOP_REGION}    eu\n\n"
    "Token Comes From The Dispatch\n"
    "    [Tags]    Checkout\n"
    "    Should Start With    %{SHOP_TOKEN}    tok-\n"
    "    Should Be Equal    ${HOLD_MINUTES}    30\n"
)


def reserve(ctx: Any, info: dict) -> dict:
    """pre_dispatch: computed once in the dispatching process."""
    return {
        "env": {"SHOP_TOKEN": f"tok-{len(info['shards'])}"},
        "worker_data": {"booking": "staging-eu-7", "shards": sorted(info["shards"])},
        "variables": {"HOLD_MINUTES": 30},
    }


def note_data(info: dict) -> None:
    """worker_setup: notes the data it got, where it ran."""
    Path(info["out_dir"], "setup.json").write_text(
        json.dumps({"data": info["data"], "cwd": os.getcwd(), "pid": os.getpid()}), encoding="utf-8"
    )


def checkout(tmp_path: Path) -> Path:
    """The files the suite reads relative to its working directory, in a
    directory next to the config file (``tmp_path``)."""
    place = tmp_path / "shop checkout"
    (place / "data").mkdir(parents=True)
    (place / "data" / "coupons.txt").write_text("spring10\n", encoding="utf-8")
    return place


class TestWhereTheWorkersStand:
    def test_cwd_env_and_what_pre_dispatch_computed_reach_every_worker(
        self, make_service, tmp_path, monkeypatch
    ):
        root = shop_suite(tmp_path / "corpus", SUITE)
        place = checkout(tmp_path)
        monkeypatch.setenv("SHOP_REGION_FOR_TESTS", "eu")
        monkeypatch.delenv("SHOP_TOKEN", raising=False)
        service = pool_service(
            make_service, root, max_workers=2,
            worker_cwd="shop checkout",  # relative: anchored to the config file's directory
            worker_env={"SHOP_REGION": "${env-public:SHOP_REGION_FOR_TESTS}"},
            pre_dispatch=[f"{HERE}:reserve"], worker_setup=[f"{HERE}:note_data"], pythonpath=[str(REPO)],
        )
        run = execute(service, "Checkout")
        verdicts = {v.item_id: (v.status, v.message) for v in service.store.verdicts_for(run.id)}
        assert run.completion == "PASS", verdicts
        assert len(verdicts) == 3
        shards = run.latest_dispatch.shards
        assert len(shards) == 2
        for shard in shards:
            setup = json.loads((workdir(service, run) / shard / "setup.json").read_text(encoding="utf-8"))
            assert setup["data"] == {"booking": "staging-eu-7", "shards": sorted(shards)}
            assert Path(setup["cwd"]).resolve() == place.resolve() and setup["pid"] != os.getpid()
        assert "SHOP_TOKEN" not in os.environ  # the dispatching process is untouched

    def test_without_worker_cwd_the_relative_file_is_not_found(self, make_service, tmp_path):
        """The other side: it is worker_cwd that makes the file readable."""
        root = shop_suite(tmp_path / "corpus", SUITE)
        checkout(tmp_path)
        service = pool_service(make_service, root, max_workers=1)
        run = execute(service, "Checkout")
        (coupon,) = [v for v in service.store.verdicts_for(run.id) if "Coupon" in v.item_id]
        assert coupon.status == "FAIL" and "coupons.txt" in coupon.message

    def test_a_worker_cwd_that_is_not_there_degrades_health_and_refuses_the_dispatch(
        self, make_service, tmp_path
    ):
        root = shop_suite(tmp_path / "corpus", SUITE)
        service = pool_service(make_service, root, worker_cwd="no such checkout")
        health = service.runner_health("robot-pool", refresh=True)
        assert health.status == "degraded"
        assert health.detail == f"worker_cwd is not a directory: {tmp_path / 'no such checkout'}"
        from .conftest import compose

        run_id = compose(service, "Checkout", runner_id="robot-pool")
        outcome = service.dispatch(run_id, "robot-pool", foreground=True)
        assert outcome.refusal == f"worker_cwd {str(tmp_path / 'no such checkout')!r} is not a directory"

    @pytest.mark.parametrize(
        ("env", "message"),
        [
            ({"SHOP_REGION": {"eu": 1}}, r"worker_env\.SHOP_REGION: an environment variable's value is text"),
            (["SHOP_REGION=eu"], "worker_env must be a mapping"),
        ],
    )
    def test_worker_env_is_a_mapping_of_text(self, make_service, tmp_path, env, message):
        with pytest.raises(ConfigError, match=message):
            pool_service(make_service, shop_suite(tmp_path, SUITE), worker_env=env).validate()


class TestWhatAPreDispatchAnswerMeans:
    """A mapping of Robot variables, as it always was — or, when its keys
    are only ``variables`` / ``env`` / ``worker_data``, each a mapping,
    those three. ``None`` or any other answer adds nothing."""

    def additions(self, *answers: Any):
        from runcomposer.plugins.robot_pool import _Additions

        added = _Additions()
        for answer in answers:
            added.take(answer, "shop.hooks:reserve")
        return added

    def test_a_plain_mapping_is_robot_variables(self):
        added = self.additions({"HOLD_MINUTES": 30, "env": "staging"})
        assert (added.variables, added.env, added.worker_data) == (
            {"HOLD_MINUTES": "30", "env": "staging"}, {}, {},
        )

    def test_the_structured_answer_and_later_steps_winning(self):
        added = self.additions(
            {"env": {"SHOP_TOKEN": "a"}, "worker_data": {"booking": "staging-eu-7"}},
            {"variables": {"HOLD_MINUTES": "30"}, "env": {"SHOP_TOKEN": "b", "SHOP_REGION": "eu"}},
        )
        assert added.variables == {"HOLD_MINUTES": "30"}
        assert added.env == {"SHOP_TOKEN": "b", "SHOP_REGION": "eu"}
        assert added.worker_data == {"booking": "staging-eu-7"}

    @pytest.mark.parametrize("answer", [None, "ready", 3, ["HOLD_MINUTES"], {}])
    def test_none_and_other_answers_add_nothing(self, answer):
        added = self.additions(answer)
        assert (added.variables, added.env, added.worker_data) == ({}, {}, {})
