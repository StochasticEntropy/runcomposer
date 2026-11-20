"""``RunStore.atomic()`` and the ledger decisions taken inside it (DESIGN.md
§4, §6.3).

The ledger reads a dispatch's facts, asks the state machine what they mean
and writes the answer — one decision, one atomic unit of the store. These
tests pin the unit itself (for the sqlite store, a ``BEGIN IMMEDIATE``
transaction that nested store calls join) and the decisions that depend on
it; the two-process behaviour is in ``test_two_processes.py``.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from runcomposer.plugins.sqlite_store import SqliteRunStore
from runcomposer.sdk import StoreBase, Verdict
from runcomposer.testing import check_store

from .conftest import compose
from .test_store import make_spec


@pytest.fixture(params=["file", "memory"])
def store(request, tmp_path):
    made = SqliteRunStore(str(tmp_path / "runcomposer.db") if request.param == "file" else ":memory:")
    yield made
    made.close()


def a_dispatch(store: SqliteRunStore) -> str:
    run = store.create_run(make_spec(), origin="test")
    store.add_dispatch(run.id, dispatch_id="01DISPATCH0000000000000001", mode="demo", state="RUNNING")
    return "01DISPATCH0000000000000001"


class TestTheSqliteUnit:
    def test_writes_inside_are_read_back_and_committed_together(self, store):
        dispatch_id = a_dispatch(store)
        with store.atomic():
            store.update_dispatch(dispatch_id, error="first")
            assert store.get_dispatch(dispatch_id).error == "first"
            store.update_dispatch(dispatch_id, plan="second")
        after = store.get_dispatch(dispatch_id)
        assert (after.error, after.plan) == ("first", "second")

    def test_an_exception_undoes_the_whole_block_nested_blocks_included(self, store):
        dispatch_id = a_dispatch(store)
        with pytest.raises(RuntimeError), store.atomic():
            store.update_dispatch(dispatch_id, error="gone")
            with store.atomic():
                store.update_dispatch(dispatch_id, plan="gone too")
            raise RuntimeError("undo")
        after = store.get_dispatch(dispatch_id)
        assert (after.error, after.plan) == ("", "")

    def test_a_call_that_fails_inside_undoes_only_itself(self, store):
        """Each store call inside a block is still all-or-nothing on its own
        (a savepoint): a refused call leaves the block's other writes."""
        from runcomposer.sdk import StoreError

        dispatch_id = a_dispatch(store)
        with store.atomic():
            store.update_dispatch(dispatch_id, error="kept")
            with pytest.raises(StoreError):
                store.create_run(make_spec(), origin="test")  # the id exists
        assert store.get_dispatch(dispatch_id).error == "kept"

    def test_another_thread_writes_after_the_block_not_inside_it(self, store):
        dispatch_id = a_dispatch(store)
        entered, finished = threading.Event(), threading.Event()

        def other() -> None:
            entered.wait(30)
            store.update_dispatch(dispatch_id, error="other")
            finished.set()

        thread = threading.Thread(target=other)
        thread.start()
        with store.atomic():
            seen = store.get_dispatch(dispatch_id).error
            entered.set()
            assert not finished.wait(0.3), "the other writer must wait for the block"
            store.update_dispatch(dispatch_id, error=seen + "mine")
        thread.join(30)
        assert finished.is_set() and store.get_dispatch(dispatch_id).error == "other"


class StoreWithoutAUnit(StoreBase):
    """The sqlite store, but ``atomic()`` is the base class's — no unit."""

    def __init__(self) -> None:
        self._inner = SqliteRunStore(":memory:")
        self.store_id = "unitless"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _forward(cls: type) -> None:
    from runcomposer.sdk import RunStore, contract_members

    for name in contract_members(RunStore):
        if name in ("store_id", "atomic") or name in vars(cls):
            continue

        def forwarded(self: Any, *args: Any, _name: str = name, **kwargs: Any) -> Any:
            return getattr(self._inner, _name)(*args, **kwargs)

        setattr(cls, name, forwarded)


_forward(StoreWithoutAUnit)


def test_the_kit_tells_a_store_without_a_unit():
    with pytest.raises(AssertionError, match=r"\[store\.atomic\]"):
        check_store(StoreWithoutAUnit)


class TestLedgerDecisions:
    def test_two_recoveries_of_one_dispatch_mark_it_stale_once(self, service):
        run_id = compose(service, "Auth-SSO")
        owner = "elsewhere.shop.example:4242:01TOKENOFTHATPROCESS000000"
        dispatch = service.ledger.open(run_id, "demo", owner=owner)
        first = service.ledger.mark_stale(dispatch.dispatch_id, owner=owner, error="gone")
        second = service.ledger.mark_stale(dispatch.dispatch_id, owner=owner, error="gone")
        assert first is not None and first.state == "STALE" and second is None
        assert [e.kind for e in service.store.events(run_id)].count("run.stale") == 1

    def test_a_dispatch_owned_by_someone_else_now_is_not_marked(self, service):
        run_id = compose(service, "Auth-SSO")
        dispatch = service.ledger.open(run_id, "demo", owner="a.shop.example:1:01TOKENA0000000000000000000")
        assert service.ledger.mark_stale(
            dispatch.dispatch_id, owner="b.shop.example:2:01TOKENB0000000000000000000", error="gone"
        ) is None
        assert service.store.get_dispatch(dispatch.dispatch_id).state == "DISPATCHED"

    def test_release_with_an_owner_releases_only_that_owner(self, service):
        run_id = compose(service, "Auth-SSO")
        dispatch = service.ledger.open(run_id, "demo", owner="a.shop.example:1:01TOKENA0000000000000000000")
        service.ledger.release(dispatch.dispatch_id, owner="b.shop.example:2:01TOKENB0000000000000000000")
        assert service.store.get_dispatch(dispatch.dispatch_id).owner is not None
        service.ledger.release(dispatch.dispatch_id, owner="a.shop.example:1:01TOKENA0000000000000000000")
        assert service.store.get_dispatch(dispatch.dispatch_id).owner is None

    @pytest.mark.parametrize("over", ["FAILED", "STALE"])
    def test_a_partial_late_delivery_keeps_the_summary_of_a_dispatch_that_is_over(self, service, over):
        """A FAILED or STALE dispatch counts what never reported as MISSING;
        a late delivery for one of its shards does not make those items
        vanish from its summary."""
        run_id = compose(service, "Auth-SSO")
        dispatch = service.ledger.open(run_id, "export", state="AWAITING_RESULTS", shards=["1", "2"])
        service.ledger.move(dispatch.dispatch_id, over)
        planned = service.ledger.planned_ids(run_id)
        service.ingestion.record(
            service.store.get_dispatch(dispatch.dispatch_id), "1",
            [Verdict(planned[0], "PASS", 5)], format_id="runcomposer-verdicts",
        )
        after = service.store.get_dispatch(dispatch.dispatch_id)
        assert after.state == over
        assert dict(after.summary) == {"PASS": 1, "MISSING": len(planned) - 1}
        from runcomposer.core import lifecycle

        assert dict(after.summary) == lifecycle.summarize(service.ledger.scored_verdicts(after))

    def test_events_are_emitted_once_the_decision_is_committed(self, make_service):
        """A reader woken by a state event finds the state it announces —
        the event is emitted after the unit is committed, not inside it."""
        service = make_service()
        run_id = compose(service, "Auth-SSO")
        seen: list[tuple[str, str]] = []
        original = service.bus.emit

        def emit(run: str, kind: str, **kwargs: Any) -> Any:
            if kind == "run.cancelled":
                # Asked from another thread, i.e. another connection: only
                # committed state is visible there.
                box: list[str] = []
                reader = threading.Thread(target=lambda: box.append(service.store.get_run(run).state))
                reader.start()
                reader.join(30)
                seen.append((kind, box[0]))
            return original(run, kind, **kwargs)

        service.bus.emit = emit  # type: ignore[method-assign]
        dispatch = service.ledger.open(run_id, "export", state="AWAITING_RESULTS", shards=["1"])
        service.ledger.move(dispatch.dispatch_id, "CANCELLED")
        assert seen == [("run.cancelled", "CANCELLED")]
