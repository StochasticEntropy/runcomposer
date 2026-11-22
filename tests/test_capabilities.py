"""The capability vocabulary of docs/connectors.md §3.1 is what the shipped
runners declare: every capability any of them can declare, in any
configuration, has a row with its meaning, and no row names a capability
none of them declares."""

from __future__ import annotations

import re

from runcomposer.plugins.command_runner import CommandRunner
from runcomposer.plugins.demo_runner import DemoRunner
from runcomposer.plugins.jenkins import JenkinsRunner
from runcomposer.plugins.robot_pool import RobotPoolRunner

from .conftest import REPO


def declared() -> set[str]:
    runners = [
        DemoRunner(),
        CommandRunner(command="true"),
        CommandRunner(command="true", shards=3),
        RobotPoolRunner(suite_root=str(REPO / "examples")),
        JenkinsRunner(base_url="https://ci.shop.example", job="shop", completion="poll"),
        JenkinsRunner(base_url="https://ci.shop.example", job="shop", completion="callback",
                      callback_base="https://runcomposer.shop.example"),
    ]
    return {capability for runner in runners for capability in runner.describe().capabilities}


def documented() -> set[str]:
    guide = (REPO / "docs" / "connectors.md").read_text(encoding="utf-8")
    table = guide.split("| Capability | Meaning | Declared by |", 1)[1].split("\n\n", 1)[0]
    return set(re.findall(r"^\| `([a-z_]+)` \|", table, re.MULTILINE))


def test_every_declared_capability_is_documented_and_nothing_else():
    assert declared() == documented()


def test_the_table_has_a_meaning_for_each():
    guide = (REPO / "docs" / "connectors.md").read_text(encoding="utf-8")
    table = guide.split("| Capability | Meaning | Declared by |", 1)[1].split("\n\n", 1)[0]
    for row in table.splitlines()[1:]:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert len(cells) == 3 and all(cells), row
