"""The merge process (``spawn_merge``) is the SDK's worker supervisor's —
one implementation of starting, stopping and reaping — and still stops on
a timeout and on a cancellation, at once; and links in a merged log reach
the files the shards wrote, also when served as artifacts (DESIGN.md §6.4,
§6.6). With real Robot Framework.
"""

from __future__ import annotations

import ast
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

from runcomposer.sdk.robot import MergePayload, spawn_merge

from .conftest import REPO, needs_robot, process_gone, wait_until
from .test_robot_merge import two_outputs
from .test_robot_pool import RUNNER, shop_suite

pytestmark = needs_robot


class BlocksUntilReleased:
    """A pre-rebot modifier that notes its process id, then waits for a
    file that never comes (or 60 s): a merge that does not end by itself."""

    def __init__(self, marker: str):
        self.marker = marker

    def visit_suite(self, suite) -> None:
        # Written whole or not at all: a stop that lands while the note is
        # being made must not leave an empty file for the test to read.
        noted = Path(self.marker + ".writing")
        noted.write_text(str(os.getpid()), encoding="utf-8")
        os.replace(noted, self.marker)
        deadline = time.monotonic() + 60
        while not Path(self.marker + ".release").exists() and time.monotonic() < deadline:
            time.sleep(0.05)


class TestOneSupervisor:
    def test_spawn_merge_starts_no_process_of_its_own(self):
        tree = ast.parse((REPO / "src" / "runcomposer" / "sdk" / "robot.py").read_text(encoding="utf-8"))
        imported = {
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        assert "multiprocessing" not in imported and "subprocess" not in imported
        spawn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "spawn_merge")
        called = {n.func.id for n in ast.walk(spawn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "supervise_workers" in called

    def test_a_merge_over_its_timeout_is_stopped_at_once(self, tmp_path):
        marker = tmp_path / "merging"
        started = time.monotonic()
        result = spawn_merge(
            MergePayload(outputs=tuple(two_outputs(tmp_path)), out_dir=str(tmp_path / "merged"),
                         rebot_options={"prerebotmodifier": [BlocksUntilReleased(str(marker))]}),
            timeout_s=3.0,  # long enough for the merge to have started on a busy machine
        )
        took = time.monotonic() - started
        assert result.error == "the merge exceeded its timeout of 3s" and result.output is None
        pid = int(marker.read_text(encoding="utf-8"))
        assert process_gone(pid)
        # The first stop request ends a merge: no grace period is waited out.
        assert took < 3.0 + 4.0, took

    def test_a_cancelled_merge_is_stopped_at_once(self, tmp_path):
        marker = tmp_path / "merging"
        outputs = tuple(two_outputs(tmp_path))
        stop_asked: list[float] = []

        def cancelled() -> bool:
            if marker.exists() and not stop_asked:
                stop_asked.append(time.monotonic())
            return bool(stop_asked)

        result = spawn_merge(
            MergePayload(outputs=outputs, out_dir=str(tmp_path / "merged"),
                         rebot_options={"prerebotmodifier": [BlocksUntilReleased(str(marker))]}),
            cancelled=cancelled,
        )
        took = time.monotonic() - stop_asked[0]
        assert result.error == "the merge was cancelled"
        assert process_gone(int(marker.read_text(encoding="utf-8")))
        assert took < 4.0, took


# -- links in merged logs ------------------------------------------------------------------

RECEIPTS = """\
*** Settings ***
Library    OperatingSystem

*** Test Cases ***
Receipt For One Item
    [Tags]    Checkout
    Write Receipt    one

Receipt For Two Items
    [Tags]    Checkout
    Write Receipt    two

Receipt For A Coupon
    [Tags]    Checkout
    Write Receipt    coupon

Receipt For A Refund
    [Tags]    Checkout
    Write Receipt    refund

*** Keywords ***
Write Receipt
    [Arguments]    ${name}
    Create File    ${OUTPUT DIR}/receipt-${name}.txt    receipt ${name}
    Log    <a href="receipt-${name}.txt">receipt ${name}</a>    html=True
"""
_HREF = re.compile(r'href="([^"]+)"')


def linked_files(output: Path) -> list[str]:
    """Every relative link of an HTML message in an output (Robot's own
    ``file://`` links to what ``Create File`` wrote are absolute)."""
    from robot.api import ExecutionResult, ResultVisitor

    links: list[str] = []

    class Collect(ResultVisitor):
        def visit_message(self, message):
            if message.html:
                links.extend(link for link in _HREF.findall(message.message) if ":" not in link)

    ExecutionResult(str(output)).visit(Collect())
    return links


class TestLinksInMergedLogs:
    def test_every_link_of_a_merged_log_reaches_its_file_where_it_is_served(self, make_client, tmp_path):
        root = shop_suite(tmp_path, RECEIPTS, name="receipts.robot")
        client, service = make_client(
            sources={"robotframework": {"root": str(root)}},
            runners={RUNNER: {
                "suite_root": str(root), "partitions": ["staging", "preview"], "max_workers": 4,
                "max_tests_per_chunk": 1, "merge_groups": True,
            }},
        )
        response = client.post(
            "/api/v1/runs",
            json={"selection": {"tag_filter": "Checkout"}, "title": "Receipts", "dispatch": {"runner": RUNNER}},
        )
        assert response.status_code == 201, response.text
        run_id = response.json()["run"]["id"]
        detail = wait_until(
            lambda: (d := client.get(f"/api/v1/runs/{run_id}").json())["state"] == "COMPLETE" and d,
            what="the run to complete",
        )
        assert detail["completion"] == "PASS"
        by_path = {a["url_or_path"]: a for a in service.artifacts.list(run_id, server_paths=True)}
        logs = {a["name"]: a for a in by_path.values() if a["name"].startswith("log.html")}
        # Shards of two executions each, a group of two shards, the run: all merged here.
        assert {"log.html (staging-1)", "log.html (group staging)", "log.html (run)"} <= set(logs)
        checked = 0
        for name, log in logs.items():
            output = Path(log["url_or_path"]).with_name("output.xml")
            links = linked_files(output)
            assert len(links) == (8 if name == "log.html (run)" else 4 if "group" in name else 2), (name, links)
            for link in links:
                assert (output.parent / link).resolve().is_file(), (name, link)
                served = client.get(urljoin(log["href"], link))
                assert served.status_code == 200, (name, link, served.status_code)
                assert served.text.startswith("receipt "), (name, link)
                checked += 1
        assert checked >= 2 * 2 + 2 * 4 + 8
        # Nothing of the rewriting is left behind or changed in what the shards wrote.
        work = service.config.artifact_dir / run_id / detail["dispatch_id"]
        assert not list(work.rglob(".relinked-*"))
        for execution in work.glob("*/execution-*/output.xml"):
            assert all(not link.startswith("..") and "/" not in link for link in linked_files(execution))
