"""Worker targets for ``supervise_workers`` (tests/test_worker_supervisor.py).

Each is a module-level function, so that it can be sent to a spawned
process, and imports nothing but the standard library — a worker starts
quickly. Each writes what the test needs to see into its shard's directory
or a directory named in ``payload.data``; none sleeps to synchronise:
waiting is for a condition, with a deadline.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEADLINE_S = 60.0


def _until(condition: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + DEADLINE_S
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def _running(payload: Any) -> None:
    Path(payload.out_dir, "running").write_text(str(os.getpid()), encoding="utf-8")


def _pid_running(pid: int) -> bool:
    """Through runcomposer's own probe: ``os.kill(pid, 0)`` would end the
    process on Windows."""
    from runcomposer.app.liveness import pid_alive

    return pid_alive(pid)


def hand_back(payload: Any, stop: Callable[[], bool]) -> dict[str, Any]:
    """Returns what it was told, and which process it ran in."""
    return {
        "shard": payload.shard,
        "items": list(payload.item_ids),
        "data": dict(payload.data),
        "pid": os.getpid(),
    }


def lane_step(payload: Any, stop: Callable[[], bool]) -> dict[str, Any]:
    """One step of a lane test. ``data["meet"]``: shards that must be running
    at the same time as this one (each waits until the others have
    started); ``data["after"]``: the shard before this one in its lane,
    whose process must be gone when this one starts."""
    meeting = Path(payload.data["meeting"])
    seen: dict[str, Any] = {"pid": os.getpid()}
    after = payload.data.get("after")
    if after is not None:
        before = int((meeting / f"pid-{after}").read_text(encoding="utf-8"))
        seen["previous_still_running"] = _pid_running(before)
        seen["previous_ended"] = (meeting / f"ended-{after}").exists()
    (meeting / f"pid-{payload.shard}").write_text(str(os.getpid()), encoding="utf-8")
    (meeting / f"started-{payload.shard}").write_text("", encoding="utf-8")
    meet = list(payload.data.get("meet") or ())
    if meet:
        seen["met"] = _until(lambda: all((meeting / f"started-{other}").exists() for other in meet))
    (meeting / f"ended-{payload.shard}").write_text("", encoding="utf-8")
    return seen


def raises(payload: Any, stop: Callable[[], bool]) -> None:
    raise ValueError(f"no such product: {payload.item_ids[0]}")


def dies(payload: Any, stop: Callable[[], bool]) -> None:
    os._exit(7)


def stops_when_asked(payload: Any, stop: Callable[[], bool]) -> dict[str, Any]:
    """Runs until ``stop()`` says so, then notes what it saw and returns."""
    _running(payload)
    stopped = _until(stop)
    marker = Path(payload.out_dir, ".stop-requested").exists()
    Path(payload.out_dir, "saw-stop").write_text(f"{stopped} {marker}", encoding="utf-8")
    return {"stopped": stopped, "marker": marker}


def quick_or_blocking(payload: Any, stop: Callable[[], bool]) -> Any:
    """``quick`` returns once ``blocked`` is running; ``blocked`` waits until
    it is stopped — so it is running when ``quick``'s ``on_done`` raises."""
    if payload.shard == "quick":
        _until(lambda: Path(payload.out_dir).parent.joinpath("blocked", "running").exists())
        return None
    return stops_when_asked(payload, stop)


def ignores_the_request(payload: Any, stop: Callable[[], bool]) -> None:
    """Never looks at ``stop()``: it is ended by the next step of the stop
    protocol, and notes how."""
    _running(payload)
    try:
        while True:
            time.sleep(0.01)
    except BaseException as exc:
        Path(payload.out_dir, "ended-by").write_text(type(exc).__name__, encoding="utf-8")
        raise


def ignores_every_signal(payload: Any, stop: Callable[[], bool]) -> None:
    """Ignores the termination signal altogether: only a kill ends it."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _running(payload)
    while True:
        time.sleep(0.01)


CHILD = (
    "import os, signal, sys, time\n"
    "if sys.argv[2] == 'stubborn':\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "time.sleep(600)\n"
)


def starts_children(payload: Any, stop: Callable[[], bool]) -> None:
    """Starts two processes of its own — one that ends on the termination
    signal, one that ignores it — and then ignores the stop request."""
    out = Path(payload.out_dir)
    children = []
    for kind in ("plain", "stubborn"):
        pid_file = out / f"child-{kind}.pid"
        children.append(subprocess.Popen([sys.executable, "-c", CHILD, str(pid_file), kind]))
        _until(lambda pid_file=pid_file: pid_file.exists() and bool(pid_file.read_text(encoding="utf-8")))
    _running(payload)
    while True:
        time.sleep(0.01)


def where_am_i(payload: Any, stop: Callable[[], bool]) -> dict[str, Any]:
    """Reports its working directory and environment — and those of a
    process it starts."""
    child = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getcwd()); print(os.environ['SHOP_REGION'])"],
        capture_output=True, text=True, check=True,
    )
    return {
        "cwd": os.getcwd(),
        "region": os.environ.get("SHOP_REGION"),
        "coupons": Path("data", "coupons.txt").read_text(encoding="utf-8").strip(),
        "child": child.stdout.splitlines(),
    }
