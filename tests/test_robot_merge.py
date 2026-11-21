"""Merging Robot outputs (``runcomposer.sdk.robot``): rebot options passed
through, the options the toolkit decides refused by name, and the same
merge in a spawned process — so that a server never runs Robot's result
model or its process-global logger itself."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from runcomposer.sdk.robot import (
    MergePayload,
    NameTopSuites,
    merge_outputs,
    run_shard,
    spawn_merge,
)

from .conftest import REPO, needs_robot
from .test_robot_pool import FIVE_TESTS, executed_tests, shop_suite
from .test_robot_toolkit import CART, payload

pytestmark = needs_robot


def two_outputs(tmp_path: Path) -> list[str]:
    """Two disjoint executions of one root, as two chunks leave them."""
    root = shop_suite(tmp_path, FIVE_TESTS, name="cart.robot")
    outputs = []
    for index, ids in enumerate((CART[:2], CART[2:]), start=1):
        out = tmp_path / f"chunk-{index}"
        run_shard(payload(root, out, ids))
        outputs.append(str(out / "output.xml"))
    return outputs


class TestRebotOptions:
    def test_options_reach_rebot(self, tmp_path):
        outputs = two_outputs(tmp_path)
        merged = merge_outputs(
            outputs, tmp_path / "merged",
            rebot_options={"logtitle": "Cart on eu", "reporttitle": "Cart report on eu",
                           "removekeywords": ["passed"]},
        )
        assert len(executed_tests(merged)) == 5
        assert "Cart on eu" in (tmp_path / "merged" / "log.html").read_text(encoding="utf-8")
        assert "Cart report on eu" in (tmp_path / "merged" / "report.html").read_text(encoding="utf-8")
        # removekeywords=passed: the passing tests' keywords are gone from the output.
        assert merged.read_text(encoding="utf-8").count(
            "Content removed using the --remove-keywords option."
        ) == 4

    @pytest.mark.parametrize("name", ["log", "outputdir", "Output-Dir", "merge", "RPA", "stdout"])
    def test_options_the_toolkit_sets_itself_are_refused_by_name(self, tmp_path, name):
        with pytest.raises(ValueError, match=rf"rebot_options may not set '{name}' \(use "):
            merge_outputs(two_outputs(tmp_path), tmp_path / "merged", rebot_options={name: "x"})

    def test_a_modifier_given_there_runs_after_the_toolkit_s_own(self, tmp_path):
        outputs = two_outputs(tmp_path)
        merged = merge_outputs(
            outputs, tmp_path / "side-by-side", merge=False,
            rebot_options={"prerebotmodifier": [NameTopSuites(["eu", "us"])]},
        )
        tests = executed_tests(merged)
        assert sorted(tests) == sorted(
            [f"eu & us.eu.Cart.{name.rsplit('.', 1)[1]}" for name in CART[:2]]
            + [f"eu & us.us.Cart.{name.rsplit('.', 1)[1]}" for name in CART[2:]]
        )
        # …and the outputs it combined are untouched.
        assert set(executed_tests(Path(outputs[0]))) == set(CART[:2])


class TestInASpawnedProcess:
    def test_it_produces_what_the_in_process_merge_produces(self, tmp_path):
        outputs = two_outputs(tmp_path)
        here = merge_outputs(outputs, tmp_path / "here", rebot_options={"logtitle": "Cart"})
        result = spawn_merge(
            MergePayload(outputs=tuple(outputs), out_dir=str(tmp_path / "there"),
                         rebot_options={"logtitle": "Cart"})
        )
        assert result.ok and result.error is None
        assert Path(result.output) == tmp_path / "there" / "output.xml"
        assert executed_tests(Path(result.output)) == executed_tests(here)
        for name in ("log.html", "report.html"):
            assert (tmp_path / "there" / name).is_file() and (tmp_path / "here" / name).is_file()
        # The hand-back is a hidden file: never served under a recorded directory.
        assert json.loads((tmp_path / "there" / ".merge-result.json").read_text())["output"]

    def test_a_merge_that_fails_says_why_and_raises_nothing(self, tmp_path):
        broken = tmp_path / "broken.xml"
        broken.write_text("<robot><suite", encoding="utf-8")
        result = spawn_merge(MergePayload(outputs=(str(broken),), out_dir=str(tmp_path / "out")))
        assert not result.ok and result.output is None
        # Robot's own reason travels back.
        assert result.error.startswith("rebot wrote no output.xml: ") and "broken.xml" in result.error

    def test_refused_options_are_refused_before_anything_starts(self, tmp_path):
        with pytest.raises(ValueError, match="rebot_options may not set 'output'"):
            spawn_merge(MergePayload(outputs=("x.xml",), out_dir=str(tmp_path), rebot_options={"output": "x"}))
        assert not (tmp_path / ".merge-result.json").exists()

    def test_the_calling_process_imports_nothing_of_robot(self, tmp_path):
        """Checked in a fresh interpreter, where no other test's import can
        hide what the call itself loads."""
        outputs = two_outputs(tmp_path)
        script = tmp_path / "merge_here.py"
        script.write_text(
            "import sys\n"
            "from runcomposer.sdk.robot import MergePayload, spawn_merge\n"
            "if __name__ == '__main__':\n"
            f"    result = spawn_merge(MergePayload(outputs=tuple({outputs!r}), out_dir={str(tmp_path / 'fresh')!r}))\n"
            "    assert result.ok, result.error\n"
            "    loaded = sorted(m for m in sys.modules if m == 'robot' or m.startswith('robot.'))\n"
            "    print('robot.result' in sys.modules, len(loaded))\n",
            encoding="utf-8",
        )
        done = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, timeout=120,
            cwd=str(REPO),
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.split() == ["False", "0"]
        assert len(executed_tests(tmp_path / "fresh" / "output.xml")) == 5
