"""Several processes opening a sqlite store that does not exist yet, at the
same moment — two servers started together, a server and a CLI command, or
CLI commands in parallel. Each one opens it without an error, and the file
ends with one schema, at the current version."""

from __future__ import annotations

import multiprocessing
import sqlite3

PROCESSES = 6
TRIALS = 12


def _open_at_once(path: str, barrier, answers) -> None:  # runs in a spawned process
    from runcomposer.plugins.sqlite_store import SqliteRunStore

    barrier.wait()
    try:
        store = SqliteRunStore(path)
        store.list_runs(limit=1)
        store.close()
        answers.put("ok")
    except Exception as exc:  # reported, not raised: the parent counts
        answers.put(f"{type(exc).__name__}: {exc}")


def test_a_fresh_store_opened_by_several_processes_at_once(tmp_path):
    from runcomposer.plugins.sqlite_store import SCHEMA_VERSION

    context = multiprocessing.get_context("spawn")
    problems: list[str] = []
    for trial in range(TRIALS):
        path = tmp_path / f"trial-{trial}" / "runcomposer.db"
        barrier = context.Barrier(PROCESSES)
        answers = context.Queue()
        workers = [
            context.Process(target=_open_at_once, args=(str(path), barrier, answers)) for _ in range(PROCESSES)
        ]
        for worker in workers:
            worker.start()
        results = [answers.get(timeout=120) for _ in workers]
        for worker in workers:
            worker.join(60)
        problems += [f"trial {trial}: {result}" for result in results if result != "ok"]
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'runs'")]
            assert tables == ["runs"]
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        connection.close()
    assert problems == []
