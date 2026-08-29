"""Shared fixtures.

The one thing worth explaining here is why the in-flight tests do not use the
shipped example corpus.
"""

from pathlib import Path

import pytest

_SLOW_LANE_SUITE = """\
*** Settings ***
Documentation     Minimal suite whose only job is to stay in flight long enough
...               for a test to observe a RUNNING run. Not a demo corpus: see
...               the fixture in tests/conftest.py for why this is separate.

*** Test Cases ***
Slow First Shard Finishes
    [Tags]    Checkout    SlowLane
    Sleep    2s
    Log    first shard done

Slow Second Shard Finishes
    [Tags]    Checkout    SlowLane
    Sleep    6s
    Log    second shard done
"""


@pytest.fixture(scope="session")
def slow_lane_suite(tmp_path_factory) -> Path:
    """A tiny Robot suite with exactly two ``SlowLane`` tests, on purpose.

    Two tests need a run that is still executing while they assert against the
    store — one watches for the dispatch record to appear mid-run (§4), the
    other for live verdicts to stream and then reconcile (§6.2a). Both need a
    run slow enough to catch, so both used to select ``SlowLane`` out of
    ``examples/robot-shop``.

    That coupled two unrelated costs. The shipped corpus sleeps 8s and 14s
    because it is *demonstration* material: at a few seconds the whole run
    finished before the runs page could even be opened, so the slow pair did
    not do the job its own ``[Documentation]`` claims. Raising it fixed the
    demo and doubled the test suite, because these two tests then sat waiting
    on 14 seconds of sleeping they never cared about.

    The corpus and the suite want different things, so they get different
    files. The sleeps here are the shortest that still leave a comfortable
    window: with ``max_workers=2`` round-robin puts one test on each shard, so
    the run has ~4 seconds during which exactly one of the two has reported —
    which is what ``0 < live verdicts < planned`` is looking for.

    Session-scoped: it is read-only, so one copy serves every test.
    """
    suite_dir = tmp_path_factory.mktemp("slow-lane-suite") / "tests"
    suite_dir.mkdir()
    (suite_dir / "slow_lane.robot").write_text(_SLOW_LANE_SUITE, encoding="utf-8")
    return suite_dir
