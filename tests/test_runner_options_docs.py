"""Every option of the configurable reference runners is in the guide's
option tables (docs/connectors.md §9.1), with its default as the constructor
has it — a new option cannot be added without its row, and a row cannot
outlive its option."""

from __future__ import annotations

import inspect
import re

import pytest

from runcomposer.plugins.command_runner import CommandRunner
from runcomposer.plugins.jenkins import JenkinsRunner
from runcomposer.plugins.robot_pool import RobotPoolRunner

from .conftest import REPO

GUIDE = (REPO / "docs" / "connectors.md").read_text(encoding="utf-8")


def table_after(marker: str) -> dict[str, str]:
    """The option index table that follows ``marker``: option → default cell."""
    rest = GUIDE.split(marker, 1)[1]
    table = rest.split("| Option | Default | Meaning |", 1)[1].split("\n\n", 1)[0]
    rows = re.findall(r"^\| `(\w+)` \| ([^|]+) \| ([^|]+) \|$", table, re.MULTILINE)
    assert all(meaning.strip() for _name, _default, meaning in rows)
    return {name: default.strip() for name, default, _meaning in rows}


def default_text(parameter: inspect.Parameter) -> str:
    if parameter.default is inspect.Parameter.empty:
        return "required"
    value = parameter.default
    if value is None:
        return "—"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, float) and value.is_integer():
        return f"`{int(value)}`"
    return f"`{value}`"


@pytest.mark.parametrize(
    ("runner", "marker"),
    [
        (RobotPoolRunner, "Every option of `robot-pool`"),
        (JenkinsRunner, "Every option of `jenkins`"),
        (CommandRunner, "**`command`**"),
    ],
)
def test_the_table_lists_every_constructor_option_with_its_default(runner, marker):
    options = {
        name: default_text(parameter)
        for name, parameter in inspect.signature(runner.__init__).parameters.items()
        if name != "self"
    }
    assert table_after(marker) == options


def test_the_worker_payload_table_lists_every_field_with_its_default():
    """docs/connectors.md §9.2: one row per ``WorkerPayload`` field, in
    order, with the dataclass's default (``—`` for a required field)."""
    import dataclasses

    from runcomposer.sdk import WorkerPayload

    table = GUIDE.split("| Field | Default | What it is |", 1)[1].split("\n\n", 1)[0]
    rows = re.findall(r"^\| `(\w+)` \| ([^|]+) \| ([^|]+) \|$", table, re.MULTILINE)

    def default(field: dataclasses.Field) -> str:
        if field.default is not dataclasses.MISSING:
            return f"`{field.default!r}`"
        if field.default_factory is not dataclasses.MISSING:
            return f"`{field.default_factory()!r}`"
        return "—"

    expected = [(field.name, default(field)) for field in dataclasses.fields(WorkerPayload)]
    assert [(name, cell.strip()) for name, cell, _what in rows] == expected
    assert all(what.strip() for _name, _cell, what in rows)
