"""``python -m runcomposer`` is the command line — the same entry point as
the ``runcomposer`` script, exit codes included (DESIGN.md §9)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from runcomposer import __version__


def python_m(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "runcomposer", *args],
        cwd=cwd, capture_output=True, text=True, timeout=120,
    )


def test_version(tmp_path):
    completed = python_m("--version", cwd=tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == f"runcomposer {__version__}"


def test_a_command_answers_as_the_script_does(tmp_path):
    # No config file: the built-in demo deployment.
    completed = python_m("catalog", "--json", cwd=tmp_path)
    assert completed.returncode == 0, completed.stderr
    stats = json.loads(completed.stdout)
    assert stats["items"] == 60 and stats["duplicate_ids"] == []


def test_a_usage_error_exits_2_and_names_the_program(tmp_path):
    completed = python_m("no-such-command", cwd=tmp_path)
    assert completed.returncode == 2
    assert completed.stderr.startswith("usage: runcomposer ")


def test_a_configuration_error_exits_2(tmp_path):
    completed = python_m("doctor", "--config", "missing.yaml", cwd=tmp_path)
    assert completed.returncode == 2
    assert "config file not found" in completed.stderr


def test_an_answer_that_is_no_exits_1(tmp_path):
    completed = python_m("show", "no-such-run", cwd=tmp_path)
    assert completed.returncode == 1, completed.stdout + completed.stderr
