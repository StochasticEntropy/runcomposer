"""Two processes on one sqlite store (DESIGN.md §6.3): a server and a CLI, or
two servers, deliver shards of one dispatch at the same time.

A dispatch's completion is decided from the shards delivered and their
verdicts. That reading and the writing of the outcome are one decision in the
store's ``atomic()`` unit, which for sqlite is one ``BEGIN IMMEDIATE``
transaction: no other process writes in between. Without it, a process that
read "shard 2 not delivered yet" could write its summary after the other
process had completed the dispatch — a ``COMPLETE (FAIL)`` dispatch whose
summary counts one shard.

The processes here are real OS processes. The deterministic test pauses one
of them between its reading and its writing (a store that waits for a file)
and asks the database, from outside, whether its write lock is held then.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from runcomposer.core import lifecycle
from runcomposer.plugins.sqlite_store import SqliteRunStore
from runcomposer.sdk import IngestError, Service, Verdict, load_config

from .conftest import compose, wait_until, write_config

ROOT = Path(__file__).resolve().parents[1]


class PausingStore(SqliteRunStore):
    """The sqlite store, except that while the file ``armed`` exists in
    ``pause_dir`` the first summary written for a dispatch waits — after
    creating ``paused`` — until ``release`` appears. That is the moment
    between a recompute's reading and its writing."""

    def __init__(self, path: str = "runcomposer.db", pause_dir: str = ""):
        super().__init__(path)
        self._pause_dir = Path(pause_dir) if pause_dir else None

    def update_dispatch(self, dispatch_id: str, **changes: Any) -> Any:
        armed = self._pause_dir / "armed" if self._pause_dir else None
        if armed is not None and "summary" in changes and armed.exists():
            assert self._pause_dir is not None
            armed.unlink()
            (self._pause_dir / "paused").touch()
            deadline = time.monotonic() + 120
            while not (self._pause_dir / "release").exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("never released")
                time.sleep(0.01)
        return super().update_dispatch(dispatch_id, **changes)


#: One process delivering one shard: config, run id, dispatch id, shard.
_DELIVER = """
import sys
from runcomposer.sdk import Service, Verdict, load_config
config, run_id, dispatch_id, shard = sys.argv[1:5]
with Service(load_config(config)) as service:
    items = service.ledger.planned_ids(run_id)
    half = len(items) // 2
    mine = items[:half] if shard == "1" else items[half:]
    verdicts = [Verdict(item, "FAIL" if shard == "2" and n == 0 else "PASS", 10)
                for n, item in enumerate(mine)]
    dispatch = service.store.get_dispatch(dispatch_id)
    service.ingestion.record(dispatch, shard, verdicts, format_id="runcomposer-verdicts")
"""

#: A process delivering one shard per line read: "<run id> <dispatch id>".
_WORKER = """
import sys
from runcomposer.sdk import Service, Verdict, load_config
config, shard = sys.argv[1:3]
with Service(load_config(config)) as service:
    print("ready", flush=True)
    for line in sys.stdin:
        run_id, dispatch_id = line.split()
        items = service.ledger.planned_ids(run_id)
        half = len(items) // 2
        mine = items[:half] if shard == "1" else items[half:]
        verdicts = [Verdict(item, "FAIL" if shard == "2" and n == 0 else "PASS", 10)
                    for n, item in enumerate(mine)]
        dispatch = service.store.get_dispatch(dispatch_id)
        service.ingestion.record(dispatch, shard, verdicts, format_id="runcomposer-verdicts")
        print("done", flush=True)
"""


def store_config(directory: Path, store: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.yaml"
    path.write_text(
        yaml.safe_dump({"store": store, "runners": {"demo": {}}, "core": {"ingestion": {"inbox": None}}}),
        encoding="utf-8",
    )
    return path


def deliver(config: Path, run_id: str, dispatch_id: str, shard: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", _DELIVER, str(config), run_id, dispatch_id, shard], cwd=ROOT
    )


def write_lock_held(database: Path) -> bool:
    """Asked from outside every runcomposer process: could a writer begin now?"""
    connection = sqlite3.connect(str(database), timeout=0, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc):
            return True
        raise
    else:
        connection.execute("ROLLBACK")
        return False
    finally:
        connection.close()


def two_shard_dispatch(service: Service) -> tuple[str, str]:
    run_id = compose(service, "Regression")
    dispatch = service.ledger.open(run_id, "export", state="AWAITING_RESULTS", shards=["1", "2"])
    return run_id, dispatch.dispatch_id


def contradictions(service: Service, run_id: str, dispatch_id: str) -> list[str]:
    """Where the recorded dispatch and run disagree with the verdicts."""
    dispatch = service.store.get_dispatch(dispatch_id)
    verdicts = service.store.verdicts_for(run_id, dispatch_id)
    scored = verdicts + lifecycle.missing_verdicts(
        service.ledger.planned_ids(run_id), verdicts, dispatch.shard_items
    )
    want = ("COMPLETE", lifecycle.completion_of(scored), lifecycle.summarize(scored))
    got = (dispatch.state, dispatch.completion, dict(dispatch.summary))
    run = service.require_run(run_id)
    problems = []
    if got != want:
        problems.append(f"dispatch recorded {got}, its verdicts say {want}")
    if (run.state, run.completion) != want[:2]:
        problems.append(f"run headline {(run.state, run.completion)}, verdicts say {want[:2]}")
    return problems


class TestDeliveriesFromTwoProcesses:
    def test_no_process_writes_between_a_recompute_reading_and_writing(self, tmp_path):
        database = tmp_path / "runcomposer.db"
        pause = tmp_path / "pause"
        pause.mkdir()
        with Service(load_config(str(write_config(tmp_path)))) as service:
            run_id, dispatch_id = two_shard_dispatch(service)
            pausing = store_config(tmp_path / "a", {"pausing": {
                "module": "tests.test_two_processes:PausingStore",
                "path": str(database), "pause_dir": str(pause)}})
            plain = store_config(tmp_path / "b", {"sqlite": {"path": str(database)}})
            (pause / "armed").touch()
            first = deliver(pausing, run_id, dispatch_id, "1")
            second = None
            try:
                wait_until(lambda: (pause / "paused").exists() or first.poll() is not None,
                           what="process A between reading and writing", timeout=60)
                assert (pause / "paused").exists(), f"process A ended early ({first.returncode})"
                locked = write_lock_held(database)
                second = deliver(plain, run_id, dispatch_id, "2")
                if not locked:
                    # Nothing keeps B out: its delivery and recompute land in
                    # A's window, and A then writes what it had read before.
                    assert second.wait(60) == 0
                (pause / "release").touch()
                assert first.wait(60) == 0
                assert second.wait(60) == 0
            finally:
                (pause / "release").touch()
                for process in (first, second):
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait(60)
            assert contradictions(service, run_id, dispatch_id) == []
            assert locked, "between a recompute's reading and its writing, the write lock is held"

    def test_many_rounds_of_concurrent_deliveries_never_contradict_the_verdicts(self, tmp_path):
        """Not deterministic by construction — the stress variant; a few
        dozen rounds keep it fast."""
        rounds = 30
        config = write_config(tmp_path)
        workers = [
            subprocess.Popen([sys.executable, "-c", _WORKER, str(config), shard], cwd=ROOT,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            for shard in ("1", "2")
        ]
        try:
            for worker in workers:
                assert worker.stdout is not None and worker.stdout.readline().strip() == "ready"
            with Service(load_config(str(config))) as service:
                problems: list[str] = []
                for _ in range(rounds):
                    run_id, dispatch_id = two_shard_dispatch(service)
                    for worker in workers:
                        assert worker.stdin is not None
                        worker.stdin.write(f"{run_id} {dispatch_id}\n")
                        worker.stdin.flush()
                    for worker in workers:
                        assert worker.stdout is not None and worker.stdout.readline().strip() == "done"
                    problems += contradictions(service, run_id, dispatch_id)
                assert problems == []
        finally:
            for worker in workers:
                if worker.stdin is not None:
                    worker.stdin.close()
                try:
                    worker.wait(60)
                except subprocess.TimeoutExpired:
                    worker.kill()


class TestACancelRecordedElsewhere:
    def test_a_delivery_read_against_an_older_record_is_refused_once_cancelled(self, service):
        """The delivery path read the dispatch before another process
        cancelled it: the state is read again in the unit that writes, so
        a cancelled dispatch takes no results — not even in that race."""
        run_id = compose(service, "Auth-SSO")
        dispatch = service.ledger.open(run_id, "export", state="AWAITING_RESULTS", shards=["1"])
        read_before = service.store.get_dispatch(dispatch.dispatch_id)
        assert service.cancel(run_id).state == "CANCELLED"
        verdicts = [Verdict(item, "PASS", 5) for item in service.ledger.planned_ids(run_id)]
        with pytest.raises(IngestError) as refused:
            service.ingestion.record(read_before, "1", verdicts, format_id="runcomposer-verdicts")
        assert refused.value.reason == "dispatch-cancelled"
        assert service.store.delivered_shards(run_id, dispatch.dispatch_id) == set()
        assert service.store.get_dispatch(dispatch.dispatch_id).state == "CANCELLED"
