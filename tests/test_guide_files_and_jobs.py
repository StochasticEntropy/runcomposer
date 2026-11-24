"""The two patterns of docs/connectors.md §9.2, "A file named by someone
else" and "A process you start yourself", run as the guide writes them."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from runcomposer.sdk import (
    ProcessJob,
    child_path,
    local_name_problem,
    name_collision_key,
    portable_name_problem,
)

from .conftest import process_gone, wait_until


def keep(directory: Path, name: str, content: bytes) -> Path:
    """One downloaded file, kept under the name it came with."""
    target = child_path(directory, name)   # ValueError: not one file name here
    with open(target, "xb") as file:        # FileExistsError: that file is taken
        file.write(content)
    return target


def start_tool(args: list[str]) -> tuple[subprocess.Popen, object]:
    """Start a helper process; the second value stops it and what it started."""
    if os.name == "nt":
        process, job = ProcessJob.start(args, kill_on_close=True)
        return process, job.terminate
    process = subprocess.Popen(args, start_new_session=True)
    return process, lambda: os.killpg(process.pid, signal.SIGTERM)


class TestFilesNamedFromOutside:
    def test_a_name_is_kept_as_one_file_of_its_directory(self, tmp_path):
        kept = keep(tmp_path, "junit-eu.xml", b"<testsuites/>")
        assert kept == tmp_path / "junit-eu.xml" and kept.read_bytes() == b"<testsuites/>"

    @pytest.mark.parametrize("name", ["../escape.xml", "eu/junit.xml", "eu\\junit.xml", "..", "", "C:junit.xml"])
    def test_a_name_that_is_no_single_file_name_is_refused_before_anything_is_written(self, tmp_path, name):
        with pytest.raises(ValueError):
            keep(tmp_path, name, b"x")
        assert list(tmp_path.iterdir()) == []

    def test_a_second_file_the_file_system_keeps_as_the_first_is_refused_not_written_over(self, tmp_path):
        keep(tmp_path, "results.json", b"first")
        with pytest.raises(FileExistsError):
            keep(tmp_path, "results.json", b"second")
        assert (tmp_path / "results.json").read_bytes() == b"first"
        # Whether a name differing in case is the same file only the file
        # system says; the collision key says beforehand that it may be.
        assert name_collision_key("Results.json") == name_collision_key("results.json")
        try:
            keep(tmp_path, "Results.json", b"third")
        except FileExistsError:
            assert (tmp_path / "results.json").read_bytes() == b"first"

    def test_which_check_suits_whose_file_system(self):
        # Linux keeps a colon; Windows does not — so only the portable check
        # refuses it everywhere, and the local one only on Windows.
        assert portable_name_problem("results-10:00.json") is not None
        assert (local_name_problem("results-10:00.json") is not None) == (os.name == "nt")
        assert portable_name_problem("re​sults.json") is not None, "an invisible character"
        assert local_name_problem("results.json") is None and portable_name_problem("results.json") is None


class TestAPluginsOwnProcess:
    def test_the_helper_and_what_it_started_are_stopped_together(self, tmp_path):
        marker = tmp_path / "grandchild.pid"
        # The helper starts a process of its own, then both wait.
        script = (
            "import subprocess, sys, time, pathlib\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))\n"
            "time.sleep(120)\n"
        )
        process, stop = start_tool([sys.executable, "-c", script])
        try:
            grandchild = int(wait_until(lambda: marker.exists() and marker.read_text(), what="the grandchild"))
            stop()  # type: ignore[operator]
            process.wait(timeout=30)
            wait_until(lambda: process_gone(grandchild), what="what the helper started to be stopped with it")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    @pytest.mark.skipif(os.name == "nt", reason="off Windows only")
    def test_off_windows_a_job_object_is_an_os_error_that_says_what_to_use(self):
        with pytest.raises(OSError, match=r"start_new_session=True .* os\.killpg"):
            ProcessJob(kill_on_close=True)
        with pytest.raises(OSError, match="Windows only"):
            ProcessJob.start([sys.executable, "-c", "pass"])

