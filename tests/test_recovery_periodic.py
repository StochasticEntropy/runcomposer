"""Recovery while the server runs (DESIGN.md §4).

Another process on the same store — a foreground ``runcomposer run`` — owns
the dispatch it executes. When that process dies while a server is up, the
server marks the dispatch ``STALE`` within ``core.dispatch.recovery_interval_s``
without being restarted. A dispatch of a process that is alive, and one this
server executes itself, are left alone.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from runcomposer.app.config import ConfigError
from runcomposer.app.liveness import parse_owner
from runcomposer.app.recovery import PeriodicRecovery
from runcomposer.http import create_app
from runcomposer.sdk import Service, load_config

from .conftest import compose, wait_until, write_config

API = "/api/v1"

#: A foreground dispatch in a process of its own, as `runcomposer run` makes.
_CHILD = """
import sys
from runcomposer.sdk import Service, load_config

service = Service(load_config(sys.argv[1]))
service.dispatch(sys.argv[2], "demo", foreground=True)
"""


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    # 46 items x 0.5 s: no dispatch here finishes on its own during a test.
    return write_config(
        tmp_path,
        runners={"demo": {"delay_s": 0.5}},
        core={"dispatch": {"recovery_interval_s": 0.2}},
    )


@pytest.fixture()
def children() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for child in started:
        child.kill()
        child.wait(timeout=60)


def run_detail(client: TestClient, run_id: str) -> dict[str, Any]:
    response = client.get(f"{API}/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def executing_in(client: TestClient, run_id: str, pid: int) -> bool:
    detail = run_detail(client, run_id)
    dispatches = detail["dispatches"]
    if detail["state"] != "RUNNING" or not dispatches:
        return False
    owner = client.app.state.service.store.get_dispatch(dispatches[-1]["dispatch_id"]).owner
    return bool(owner) and parse_owner(owner)[1] == pid


def test_a_dispatch_whose_owner_dies_under_a_running_server_becomes_stale(
    config_path, children
):
    with Service(load_config(str(config_path))) as setup:
        doomed, alive, served = (compose(setup, "Regression") for _ in range(3))

    with TestClient(create_app(load_config(str(config_path)))) as client:
        for run_id in (doomed, alive):
            children.append(
                subprocess.Popen([sys.executable, "-c", _CHILD, str(config_path), run_id])
            )
        # And one this server executes itself.
        response = client.post(f"{API}/runs/{served}/dispatch", json={"runner": "demo"})
        assert response.status_code in (200, 201, 202), response.text

        doomed_child, alive_child = children
        wait_until(lambda: executing_in(client, doomed, doomed_child.pid), timeout=60,
                   what="the first process to be executing its run")
        wait_until(lambda: executing_in(client, alive, alive_child.pid), timeout=60,
                   what="the second process to be executing its run")
        wait_until(lambda: run_detail(client, served)["state"] == "RUNNING", timeout=60,
                   what="the server's own dispatch to be running")

        doomed_child.kill()
        doomed_child.wait(timeout=60)

        detail = wait_until(
            lambda: (lambda d: d if d["state"] == "STALE" else None)(run_detail(client, doomed)),
            timeout=30, what="the run of the killed process to become STALE",
        )
        assert str(doomed_child.pid) in detail["dispatches"][-1]["error"]
        assert detail["events"][-1]["kind"] == "run.stale"
        assert detail["dispatches"][-1]["executing"] is False

        # The pass that marked it went over every owned dispatch: the live
        # process's and the server's own are still executing.
        assert executing_in(client, alive, alive_child.pid)
        assert run_detail(client, served)["state"] == "RUNNING"
        assert "run.stale" not in [e["kind"] for e in run_detail(client, alive)["events"]]
        assert "run.stale" not in [e["kind"] for e in run_detail(client, served)["events"]]


def test_the_interval_has_a_default_and_null_turns_the_loop_off(tmp_path):
    default = load_config(str(write_config(tmp_path)))
    assert default.dispatch["recovery_interval_s"] == 15.0
    off = load_config(str(write_config(tmp_path, core={"dispatch": {"recovery_interval_s": None}})))
    with Service(off) as service:
        loop = PeriodicRecovery(service)
        loop.start()
        assert loop._thread is None
        loop.stop()


@pytest.mark.parametrize("value", [0, -1, "often", True])
def test_a_bad_interval_is_refused_at_startup(tmp_path, value):
    with pytest.raises(ConfigError, match=r"core\.dispatch\.recovery_interval_s"):
        load_config(str(write_config(tmp_path, core={"dispatch": {"recovery_interval_s": value}})))
