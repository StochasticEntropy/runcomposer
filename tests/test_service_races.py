"""Check-then-act in the service, against another process sharing the store
(DESIGN.md §4, §6.3).

The "other process" here is a second store or Service on the same sqlite
file — separate connections, as two processes have. A store hook lets it
commit at the moment between the service's check and its write: right then
when the check is not inside a unit of the store; when it is, the other
writer has to wait for the unit (it is started on a thread and lands after
it) — which is the point.

- Deleting a run checks "nothing is executing it" and deletes in one unit:
  a dispatch started elsewhere meanwhile is refused, not deleted under its
  runner.
- A cancel that loses to a completion committed elsewhere says the run
  completed (409, no traceback); the expiry of a dispatch awaiting results
  that completed meanwhile is skipped quietly.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from runcomposer.plugins.sqlite_store import SqliteRunStore
from runcomposer.sdk import DispatchContext, RunnerBase, RunnerInfo, Service, Verdict, load_config, new_ulid

from .conftest import compose, write_config

API = "/api/v1"
HERE = "tests.test_service_races"


class HookedStore(SqliteRunStore):
    """The sqlite store with one-shot hooks run *after* a read returned —
    the moment between a check and the write that follows it."""

    def __init__(self, path: str = "runcomposer.db"):
        super().__init__(path)
        self.after: dict[str, Callable[[], None]] = {}

    def _fire(self, name: str) -> None:
        hook = self.after.pop(name, None)
        if hook is not None:
            hook()

    def get_run(self, run_id: str) -> Any:
        run = super().get_run(run_id)
        self._fire("get_run")
        return run

    def delivered_shards(self, run_id: str, dispatch_id: str) -> Any:
        delivered = super().delivered_shards(run_id, dispatch_id)
        self._fire("delivered_shards")
        return delivered


def write_lock_held(database: Path) -> bool:
    """Asked over a connection of its own: could a writer begin now?"""
    connection = sqlite3.connect(str(database), timeout=0, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc):
            return True
        raise
    connection.execute("ROLLBACK")
    connection.close()
    return False


class Elsewhere:
    """Runs ``action`` as another process would at that moment: now, if
    nothing holds the store; otherwise on a thread that waits its turn."""

    def __init__(self, database: Path, action: Callable[[], Any]):
        self.database, self.action = database, action
        self.result: Any = None
        self.error: BaseException | None = None
        self.thread: threading.Thread | None = None

    def _run(self) -> None:
        try:
            self.result = self.action()
        except BaseException as exc:
            self.error = exc

    def __call__(self) -> None:
        if write_lock_held(self.database):
            self.thread = threading.Thread(target=self._run)
            self.thread.start()
        else:
            self._run()

    def settle(self) -> None:
        if self.thread is not None:
            self.thread.join(60)
            assert not self.thread.is_alive()


class HandsOffRunner(RunnerBase):
    """Declares one shard and returns: the results come from elsewhere.
    Its cancel is where, in the test, the other process completes the run."""

    on_cancel: Callable[[], None] | None = None

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="hands-off", capabilities=("cancel",))

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])

    def cancel(self, ctx: DispatchContext) -> None:
        if HandsOffRunner.on_cancel is not None:
            HandsOffRunner.on_cancel()


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    return write_config(
        tmp_path,
        store={"hooked": {"module": f"{HERE}:HookedStore", "path": "runcomposer.db"}},
        runners={"hands-off": {"module": f"{HERE}:HandsOffRunner"}},
        core={"dispatch": {"awaiting_timeout_s": 60}},
    )


def awaiting_run(service: Service) -> tuple[str, str]:
    run_id = compose(service, "Checkout")
    service.dispatch(run_id, "hands-off", foreground=True)
    run = service.require_run(run_id)
    assert run.state == "AWAITING_RESULTS"
    return run_id, run.dispatches[-1].dispatch_id


def complete_from(config: Path, run_id: str, dispatch_id: str) -> Callable[[], None]:
    """The other process delivering the one outstanding shard."""

    def deliver() -> None:
        with Service(load_config(str(config))) as other:
            items = other.ledger.planned_ids(run_id)
            dispatch = other.store.get_dispatch(dispatch_id)
            other.ingestion.record(dispatch, "1", [Verdict(i, "PASS", 4) for i in items],
                                   format_id="runcomposer-verdicts")

    return deliver


def test_a_dispatch_started_elsewhere_between_the_check_and_the_delete(config_path, tmp_path):
    database = tmp_path / "runcomposer.db"
    with Service(load_config(str(config_path))) as service:
        run_id = compose(service, "Checkout")
        other = SqliteRunStore(str(database))
        elsewhere = Elsewhere(database, lambda: other.add_dispatch(
            run_id, dispatch_id=new_ulid(), mode="hands-off", state="DISPATCHED",
            owner="build-agent:4242:01JSHOPOWNER", exclusive=True))
        service.store.after["get_run"] = elsewhere
        service.delete_run(run_id)
        elsewhere.settle()
        other.close()
        assert service.store.get_run(run_id) is None
        assert elsewhere.result is None and elsewhere.error is not None, (
            "the other process recorded a dispatch the delete then removed under its runner"
        )


def test_a_cancel_that_loses_to_a_completion_says_the_run_completed(config_path, caplog):
    from fastapi.testclient import TestClient

    from runcomposer.http import create_app

    app = create_app(load_config(str(config_path)))
    with TestClient(app) as client, caplog.at_level(logging.WARNING, logger="runcomposer"):
        service = app.state.service
        run_id, dispatch_id = awaiting_run(service)
        HandsOffRunner.on_cancel = complete_from(config_path, run_id, dispatch_id)
        try:
            answer = client.post(f"{API}/runs/{run_id}/cancel")
        finally:
            HandsOffRunner.on_cancel = None
        assert answer.status_code == 409, answer.text
        assert answer.json()["detail"] == (
            f"run {run_id} has nothing in flight to cancel any more: it became COMPLETE "
            "before the cancel took effect"
        )
        assert service.require_run(run_id).state == "COMPLETE"
    assert not [r for r in caplog.records if r.exc_info], "no traceback in the server log"


def test_an_expiry_that_loses_to_a_completion_is_skipped_quietly(config_path, caplog):
    with Service(load_config(str(config_path))) as service:
        run_id, dispatch_id = awaiting_run(service)
        service.store.after["delivered_shards"] = complete_from(config_path, run_id, dispatch_id)
        with caplog.at_level(logging.DEBUG, logger="runcomposer"):
            expired = service.expire_awaiting(now_ms=10**15)
        assert expired == []
        assert service.require_run(run_id).state == "COMPLETE"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("was not expired: it is COMPLETE by now" in r.getMessage() for r in caplog.records)


def test_an_expiry_without_a_race_still_expires(config_path):
    with Service(load_config(str(config_path))) as service:
        run_id, _dispatch_id = awaiting_run(service)
        (expired,) = service.expire_awaiting(now_ms=10**15)
        assert expired.state == "FAILED" and service.require_run(run_id).state == "FAILED"
