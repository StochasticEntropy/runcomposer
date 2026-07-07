"""SqliteRunStore tests (DESIGN.md §6.3 schema, §5 idempotency contract)."""

import sqlite3

import pytest

from runcomposer.core.model import Verdict
from runcomposer.core.spec import build_spec
from runcomposer.plugins.sqlite_store import SqliteRunStore, StoreError


def make_spec(run_id="01TESTRUN0000000000000000A"):
    return build_spec(
        title="Store test run",
        tag_filter="Payments",
        materialized_ids=["A.T001", "A.T002"],
        source_provider="manifest",
        snapshot="sha256:" + "ab" * 32,
        results={"expect": [{"format": "runcomposer-verdicts"}], "shards": 1},
        run_id=run_id,
        created_at="2026-07-07T10:00:00Z",
    )


@pytest.fixture()
def store(tmp_path):
    return SqliteRunStore(path=tmp_path / "test.db")


class TestRunsAndSpecs:
    def test_create_and_get_run(self, store):
        record = store.create_run(make_spec(), origin="test")
        assert record.state == "COMPOSED"
        assert record.title == "Store test run"
        fetched = store.get_run(record.id)
        assert fetched == record
        assert store.get_spec_document(record.id)["selection"]["materialized"]["count"] == 2

    def test_persistence_across_store_instances(self, tmp_path):
        """Two store objects on the same file = two independent processes."""
        path = tmp_path / "shared.db"
        SqliteRunStore(path=path).create_run(make_spec(), origin="proc-1")
        other = SqliteRunStore(path=path)
        runs = other.list_runs()
        assert [run.origin for run in runs] == ["proc-1"]

    def test_list_runs_filters_and_orders(self, store):
        for n in range(3):
            spec = make_spec(run_id=f"01TESTRUN000000000000000{n}A"[:26])
            spec["run"]["created_at"] = f"2026-07-07T10:00:0{n}Z"
            store.create_run(spec, origin="test")
        store.set_run_state(store.list_runs()[0].id, "COMPLETE", completion="PASS")
        assert len(store.list_runs()) == 3
        assert store.list_runs()[0].created_at == "2026-07-07T10:00:02Z"  # newest first
        assert len(store.list_runs(state="COMPLETE")) == 1
        assert len(store.list_runs(limit=2)) == 2

    def test_unknown_run_operations_raise(self, store):
        assert store.get_run("nope") is None
        with pytest.raises(StoreError, match="unknown run"):
            store.set_run_state("nope", "COMPLETE")


class TestDeliveryIdempotency:
    """§5 normative: duplicate = no-op, same shard new content = replace."""

    @pytest.fixture()
    def run_id(self, store):
        record = store.create_run(make_spec(), origin="test")
        store.add_dispatch(record.id, dispatch_id="D1", mode="export", declared_shards=1)
        return record.id

    def _verdicts(self, status="PASS"):
        return [Verdict("A.T001", status), Verdict("A.T002", "PASS")]

    def test_new_then_duplicate_is_noop(self, store, run_id):
        first = store.record_delivery(
            run_id, dispatch_id="D1", shard="1", content_hash="sha256:aaaa",
            format="runcomposer-verdicts", verdicts=self._verdicts("FAIL"),
        )
        assert first == "new"
        again = store.record_delivery(
            run_id, dispatch_id="D1", shard="1", content_hash="sha256:aaaa",
            format="runcomposer-verdicts", verdicts=self._verdicts("PASS"),
        )
        assert again == "duplicate"
        # the duplicate did NOT overwrite: FAIL from the first delivery remains
        assert [v.status for v in store.verdicts_for(run_id)] == ["FAIL", "PASS"]

    def test_new_content_for_same_shard_replaces(self, store, run_id):
        store.record_delivery(
            run_id, dispatch_id="D1", shard="1", content_hash="sha256:aaaa",
            format="runcomposer-verdicts", verdicts=self._verdicts("FAIL"),
        )
        outcome = store.record_delivery(
            run_id, dispatch_id="D1", shard="1", content_hash="sha256:bbbb",
            format="runcomposer-verdicts", verdicts=self._verdicts("PASS"),
        )
        assert outcome == "replaced"
        # last-writer-wins: FAIL→PASS flip is possible, no monotonic merge
        assert [v.status for v in store.verdicts_for(run_id)] == ["PASS", "PASS"]
        run = store.get_run(run_id)
        assert len(run.deliveries) == 1

    def test_other_shard_is_not_replaced(self, store, run_id):
        store.record_delivery(
            run_id, dispatch_id="D1", shard="1", content_hash="sha256:aaaa",
            format="runcomposer-verdicts", verdicts=self._verdicts(),
        )
        outcome = store.record_delivery(
            run_id, dispatch_id="D1", shard="2", content_hash="sha256:bbbb",
            format="runcomposer-verdicts", verdicts=self._verdicts(),
        )
        assert outcome == "new"
        assert store.delivered_shards(run_id, "D1") == {"1", "2"}
        assert len(store.verdicts_for(run_id)) == 4


def test_no_runner_lifecycle_fields_in_schema(tmp_path):
    """§6.3: runner health/pool state never becomes store schema."""
    path = tmp_path / "schema.db"
    SqliteRunStore(path=path)
    conn = sqlite3.connect(path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"runs", "specs", "dispatches", "deliveries", "verdicts", "artifact_refs"}
    columns = {
        row[1]
        for table in tables
        for row in conn.execute(f"PRAGMA table_info({table})")
    }
    conn.close()
    for forbidden in ("health", "pool", "workers", "heartbeat", "runner_state"):
        assert not any(forbidden in column for column in columns), columns
