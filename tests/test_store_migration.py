"""A database written by runcomposer 0.1.7 opens under 0.2 (DESIGN.md §6.3).

``fixtures/store-0.1.7.sql`` is the dump of a database created by the real
0.1.7 code (``fixtures/make_store_0_1_7.py`` says how, and what is in it).
Each test loads it into a fresh file and opens that with the current store —
the migration runs in place, exactly as it does for an adopter upgrading.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from runcomposer.plugins.sqlite_store import SCHEMA_VERSION, SqliteRunStore
from runcomposer.sdk import Service, StoreError, load_config, sha256_hex

from .conftest import write_config

FIXTURE = Path(__file__).parent / "fixtures" / "store-0.1.7.sql"


@pytest.fixture()
def legacy_db(tmp_path: Path) -> Path:
    path = tmp_path / "runcomposer.db"
    conn = sqlite3.connect(path)
    conn.executescript(FIXTURE.read_text(encoding="utf-8"))
    conn.close()
    return path


def by_title(store: SqliteRunStore) -> dict[str, object]:
    return {run.title: store.get_run(run.id) for run in store.list_runs(limit=100).runs}


def test_the_fixture_really_is_a_0_1_database(legacy_db):
    conn = sqlite3.connect(legacy_db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    columns = {row[1] for row in conn.execute("PRAGMA table_info(dispatches)")}
    assert "state" not in columns and "declared_shards" in columns
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 7


def test_it_opens_and_is_stamped_with_the_current_schema_version(legacy_db):
    store = SqliteRunStore(str(legacy_db))
    assert len(store.list_runs(limit=100).runs) == 7
    store.close()
    conn = sqlite3.connect(legacy_db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # Opening it again is a no-op, not a second migration.
    again = SqliteRunStore(str(legacy_db))
    assert again.list_runs(limit=100).total == 7


def test_a_completed_run_keeps_its_results_and_gains_a_dispatch_state(legacy_db):
    runs = by_title(SqliteRunStore(str(legacy_db)))
    nightly = runs["Nightly regression"]
    assert (nightly.state, nightly.completion) == ("COMPLETE", "FAIL")
    assert nightly.planned_count == 46
    dispatch = nightly.latest_dispatch
    assert dispatch.state == "COMPLETE" and dispatch.mode == "demo"
    assert dispatch.shards == ("1",)
    assert sum(dispatch.summary.values()) == 46 and dispatch.summary["FAIL"] >= 1


def test_states_move_onto_the_dispatch(legacy_db):
    runs = by_title(SqliteRunStore(str(legacy_db)))
    assert runs["Checkout export"].state == "AWAITING_RESULTS"
    assert runs["Checkout export"].latest_dispatch.mode == "export"
    assert runs["Auth smoke (composed only)"].state == "COMPOSED"
    assert runs["Auth smoke (composed only)"].dispatches == ()


def test_a_green_run_with_missing_items_is_no_longer_green(legacy_db):
    """0.1.7 recorded COMPLETE/PASS for a run where one of 17 planned items
    reported. Completion is recomputed on migration: it is INCOMPLETE."""
    store = SqliteRunStore(str(legacy_db))
    partial = by_title(store)["Payments partial"]
    assert partial.planned_count == 17
    assert (partial.state, partial.completion) == ("COMPLETE", "INCOMPLETE")
    assert dict(partial.latest_dispatch.summary) == {"PASS": 1, "MISSING": 16}


def test_a_delivery_without_a_dispatch_gets_an_ingest_dispatch(legacy_db):
    store = SqliteRunStore(str(legacy_db))
    direct = by_title(store)["Catalog direct ingest"]
    assert [d.mode for d in direct.dispatches] == ["ingest"]
    assert all(delivery.dispatch_id == direct.dispatches[0].dispatch_id for delivery in direct.deliveries)
    assert (direct.state, direct.completion) == ("COMPLETE", "PASS")
    assert len(store.verdicts_for(direct.id, direct.dispatches[0].dispatch_id)) == direct.planned_count


def test_a_run_that_was_executing_is_stale_and_its_live_rows_are_gone(legacy_db):
    store = SqliteRunStore(str(legacy_db))
    cart = by_title(store)["Cart interrupted"]
    assert cart.state == "STALE" and cart.completion is None
    assert "interrupted" in cart.latest_dispatch.error
    assert store.verdicts_for(cart.id) == []
    assert store.live_verdicts(cart.id, cart.latest_dispatch.dispatch_id) == []


def test_two_deliveries_for_one_shard_collapse_to_the_newest(legacy_db):
    store = SqliteRunStore(str(legacy_db))
    double = by_title(store)["Payments double delivery"]
    assert len(double.deliveries) == 1
    assert double.deliveries[0].content_hash == "sha256:second"
    verdicts = store.verdicts_for(double.id)
    assert len(verdicts) == 17 and {v.status for v in verdicts} == {"PASS"}
    assert double.completion == "PASS"
    # …and the new idempotency key holds from here on.
    outcome = store.record_delivery(
        double.id, dispatch_id=double.latest_dispatch.dispatch_id, shard="1",
        content_hash="sha256:second", format="runcomposer-verdicts", verdicts=[],
    )
    assert outcome == "duplicate"


def test_plaintext_tokens_leave_the_stored_documents_but_stay_valid(legacy_db):
    conn = sqlite3.connect(legacy_db)
    before = dict(conn.execute("SELECT run_id, document FROM specs").fetchall())
    conn.close()
    assert all('"token": "rct_' in document for document in before.values())

    store = SqliteRunStore(str(legacy_db))
    for run in store.list_runs(limit=100).runs:
        spec = store.get_spec_document(run.id)
        assert "token" not in spec["results"]
        token = __import__("json").loads(before[run.id])["results"]["token"]
        assert store.ingest_tokens(run.id) == {sha256_hex(token.encode()): None}


def test_the_migrated_store_serves_the_application(legacy_db, tmp_path):
    """End to end on the migrated file: list, history selection, a new
    dispatch of an old run."""
    with Service(load_config(str(write_config(tmp_path)))) as service:
        assert service.list_runs().total == 7
        ids, provenance = service.resolve_history("failed@latest?suite=nightly")
        assert ids and provenance["resolved_run_labels"] == {"suite": "nightly"}
        checkout = next(r for r in service.list_runs().runs if r.title == "Checkout export")
        service.cancel(checkout.id)  # stop waiting for the export…
        service.dispatch(checkout.id, "demo", foreground=True)  # …and run it here
        after = service.require_run(checkout.id)
        assert after.state == "COMPLETE" and [d.mode for d in after.dispatches] == ["export", "demo"]


def test_a_database_from_the_future_is_refused(tmp_path):
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(StoreError, match="newer than this runcomposer"):
        SqliteRunStore(str(path))
