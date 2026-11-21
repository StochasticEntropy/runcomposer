"""Shared fixtures and helpers.

Tests wait on *conditions*, never on the clock: ``wait_until`` polls a
predicate with a deadline and fails with what it was waiting for, and
``Service.wait`` blocks until a run is over. A bare ``time.sleep`` in a test
is a bug waiting for a slow machine.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from runcomposer.sdk import Service, load_config

REPO = Path(__file__).resolve().parent.parent
ROBOT_SHOP = REPO / "examples" / "robot-shop" / "tests"

HAS_ROBOT = importlib.util.find_spec("robot") is not None
#: Mark for tests that execute or parse with real Robot Framework. They skip
#: cleanly where the `robot` extra is not installed.
needs_robot = pytest.mark.skipif(
    not HAS_ROBOT, reason="needs Robot Framework (pip install 'runcomposer[robot]')"
)


#: Mark for tests whose commands are POSIX shell (``echo``, ``sleep``, ``&``,
#: ``$!``), or that rely on POSIX file semantics (unprivileged symlinks).
#: They skip on Windows with that reason; the code they test has a Windows
#: path that this suite does not exercise there.
posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX shell / file semantics — not available on Windows"
)


def process_gone(pid: int) -> bool:
    """Whether ``pid`` no longer runs — through runcomposer's own portable
    probe. (``os.kill(pid, 0)`` would *terminate* the process on Windows.)"""
    from runcomposer.app.liveness import pid_alive

    return not pid_alive(pid)


def wait_until(
    predicate: Callable[[], Any], *, timeout: float = 30.0, interval: float = 0.02, what: str = ""
) -> Any:
    """Poll ``predicate`` until it returns something truthy; return that.
    Fails the test — naming ``what`` — when the deadline passes."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out after {timeout:g}s waiting for {what or predicate}")
        time.sleep(interval)


def write_config(directory: Path, data: dict[str, Any] | None = None, **sections: Any) -> Path:
    """Write ``config.yaml`` into ``directory``. Unless given, the store is a
    sqlite file there, the demo runner is configured, and the inbox watcher
    is off."""
    config: dict[str, Any] = {
        "store": {"sqlite": {"path": "runcomposer.db"}},
        "runners": {"demo": {}},
        "core": {"ingestion": {"inbox": None}},
    }
    for source in (data or {}), sections:
        for key, value in source.items():
            if key == "core" and isinstance(value, dict):
                merged = dict(config.get("core") or {})
                for core_key, core_value in value.items():
                    if isinstance(core_value, dict) and isinstance(merged.get(core_key), dict):
                        merged[core_key] = {**merged[core_key], **core_value}
                    else:
                        merged[core_key] = core_value
                config["core"] = merged
            else:
                config[key] = value
    path = directory / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture()
def make_service(tmp_path: Path):
    """Factory for Services over a config in ``tmp_path`` (the bundled demo
    corpus unless ``sources`` says otherwise). Every service is closed at
    teardown."""
    made: list[Service] = []

    def factory(data: dict[str, Any] | None = None, **sections: Any) -> Service:
        service = Service(load_config(str(write_config(tmp_path, data, **sections))))
        made.append(service)
        return service

    yield factory
    for service in made:
        service.close()


@pytest.fixture()
def service(make_service) -> Service:
    """A Service on the demo corpus with the ``demo`` runner configured."""
    return make_service()


@pytest.fixture()
def make_client(tmp_path: Path):
    """Factory for ``(TestClient, Service)`` pairs — the real app over a
    config in ``tmp_path``. The client runs the app's lifespan."""
    from fastapi.testclient import TestClient

    from runcomposer.http import create_app

    opened: list[Any] = []

    def factory(data: dict[str, Any] | None = None, **sections: Any):
        config = load_config(str(write_config(tmp_path, data, **sections)))
        app = create_app(config)
        client = TestClient(app)
        client.__enter__()
        opened.append(client)
        return client, app.state.service

    yield factory
    for client in opened:
        client.__exit__(None, None, None)


@pytest.fixture()
def client(make_client):
    return make_client()[0]


def compose(service: Service, selection: Any = "Payments", **kwargs: Any) -> str:
    """Compose a stored run; returns its id."""
    if not isinstance(selection, dict):
        selection = {"tag_filter": selection}
    kwargs.setdefault("title", "Test run")
    kwargs.setdefault("origin", "test")
    result = service.compose_run(selection, **kwargs)
    assert result.run is not None
    return result.run.id


def run_to_end(service: Service, run_id: str, runner: str = "demo", parameters: Any = None):
    """Dispatch in the foreground and return the run as it is afterwards."""
    service.dispatch(run_id, runner, parameters, foreground=True)
    return service.require_run(run_id)


_SLOW_LANE_SUITE = """\
*** Settings ***
Documentation     Minimal suite whose only job is to stay in flight long enough
...               for a test to observe a RUNNING run and act on it.

*** Test Cases ***
Slow First Shard Finishes
    [Tags]    Checkout    SlowLane
    Sleep    1s
    Log    first shard done

Slow Second Shard Finishes
    [Tags]    Checkout    SlowLane
    Sleep    4s
    Log    second shard done
"""


@pytest.fixture(scope="session")
def slow_lane_suite(tmp_path_factory) -> Path:
    """A tiny Robot suite with exactly two ``SlowLane`` tests, one much
    slower than the other.

    For tests that need a run still executing while they look at it: with
    ``max_workers=2`` each test gets its own shard, so there is a stretch in
    which exactly one has reported. Tests *wait for that state* (one shard
    delivered, the dispatch still RUNNING) rather than sleeping into it, so
    the durations only need to be long enough to be observable. Kept apart
    from ``examples/robot-shop``, whose slow pair is demonstration material
    with durations chosen for a person watching.
    """
    suite_dir = tmp_path_factory.mktemp("slow-lane-suite") / "tests"
    suite_dir.mkdir()
    (suite_dir / "slow_lane.robot").write_text(_SLOW_LANE_SUITE, encoding="utf-8")
    return suite_dir
