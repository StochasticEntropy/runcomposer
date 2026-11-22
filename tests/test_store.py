"""SqliteRunStore against the whole RunStore port (DESIGN.md §6.3; the §5
idempotency contract; safe sharing between threads and processes)."""

from __future__ import annotations

import multiprocessing
import sqlite3
import threading
from pathlib import Path

import pytest

from runcomposer.core.spec import build_spec
from runcomposer.plugins.sqlite_store import SCHEMA_VERSION, SqliteRunStore
from runcomposer.sdk import RunPage, StoreError, Verdict, utc_now

T1, T2 = "Shop.Payments.Cards.T001", "Shop.Payments.Cards.T002"


def make_spec(
    run_id: str = "01TESTRUN0000000000000000A",
    *,
    title: str = "Store test run",
    labels: dict[str, str] | None = None,
    created_at: str = "2026-07-07T10:00:00Z",
    item_ids: tuple[str, ...] = (T1, T2),
):
    return build_spec(
        title=title,
        tag_filter="Payments",
        materialized_ids=list(item_ids),
        source_provider="manifest",
        snapshot="sha256:" + "ab" * 32,
        results={"expect": [{"format": "runcomposer-verdicts"}], "shards": 1},
        labels=labels,
        run_id=run_id,
        created_at=created_at,
    )


def run_ids(page: RunPage) -> list[str]:
    return [run.id for run in page.runs]


def titles(page: RunPage) -> list[str]:
    return [run.title for run in page.runs]


@pytest.fixture()
def store(tmp_path):
    store = SqliteRunStore(path=str(tmp_path / "test.db"))
    yield store
    store.close()


@pytest.fixture()
def run_id(store):
    """A stored run with one dispatch, ``D1``."""
    record = store.create_run(make_spec(), origin="test")
    store.add_dispatch(record.id, dispatch_id="D1", mode="export", state="AWAITING_RESULTS")
    return record.id


def deliver(store, run_id, *, dispatch_id="D1", shard="1", content_hash="sha256:aaaa", status="PASS"):
    return store.record_delivery(
        run_id,
        dispatch_id=dispatch_id,
        shard=shard,
        content_hash=content_hash,
        format="runcomposer-verdicts",
        verdicts=[Verdict(T1, status, 120), Verdict(T2, "PASS", 80)],
    )


# -- runs & specs ----------------------------------------------------------------


class TestRunsAndSpecs:
    def test_create_and_get_run(self, store):
        record = store.create_run(make_spec(), origin="test")
        assert record.state == "COMPOSED"
        assert record.title == "Store test run"
        assert record.planned_count == 2
        assert (record.completion, record.completed_at) == (None, None)
        assert store.get_run(record.id) == record
        assert store.get_spec_document(record.id)["selection"]["materialized"]["count"] == 2

    def test_a_run_id_is_created_once(self, store):
        store.create_run(make_spec(), origin="test")
        with pytest.raises(StoreError, match="already exists"):
            store.create_run(make_spec(), origin="test")

    def test_persistence_across_store_instances(self, tmp_path):
        """Two store objects on the same file = two independent processes."""
        path = str(tmp_path / "shared.db")
        SqliteRunStore(path=path).create_run(make_spec(), origin="proc-1")
        other = SqliteRunStore(path=path)
        assert [run.origin for run in other.list_runs().runs] == ["proc-1"]

    def test_unknown_run_operations(self, store):
        assert store.get_run("nope") is None
        assert store.get_spec_document("nope") is None
        assert store.delete_run("nope") is False
        with pytest.raises(StoreError, match="unknown run"):
            store.set_run_headline("nope", "COMPLETE", completion="PASS", completed_at=utc_now())
        with pytest.raises(StoreError, match="unknown run"):
            store.add_dispatch("nope", dispatch_id="D", mode="demo", state="DISPATCHED")
        with pytest.raises(StoreError, match="unknown run"):
            deliver(store, "nope")
        with pytest.raises(StoreError, match="unknown run"):
            store.set_spec_document("nope", make_spec())

    def test_set_spec_document_replaces_the_document(self, store):
        record = store.create_run(make_spec(), origin="test")
        spec = store.get_spec_document(record.id)
        spec["runner"] = {"demo": {"seed": "nightly-2"}}
        store.set_spec_document(record.id, spec)
        assert store.get_spec_document(record.id)["runner"] == {"demo": {"seed": "nightly-2"}}

    def test_non_ascii_survives_the_round_trip(self, store):
        record = store.create_run(
            make_spec(title="Zahlungen prüfen", labels={"team": "Kasse – Süd"}), origin="test"
        )
        fetched = store.get_run(record.id)
        assert fetched.title == "Zahlungen prüfen"
        assert fetched.labels == {"team": "Kasse – Süd"}
        assert store.get_spec_document(record.id)["run"]["title"] == "Zahlungen prüfen"

    def test_set_run_headline_writes_state_completion_and_time(self, store):
        record = store.create_run(make_spec(), origin="test")
        store.set_run_headline(
            record.id, "COMPLETE", completion="FAIL", completed_at="2026-07-07T10:05:00Z"
        )
        run = store.get_run(record.id)
        assert (run.state, run.completion, run.completed_at) == (
            "COMPLETE", "FAIL", "2026-07-07T10:05:00Z",
        )
        store.set_run_headline(record.id, "RUNNING", completion=None, completed_at=None)
        run = store.get_run(record.id)
        assert (run.state, run.completion, run.completed_at) == ("RUNNING", None, None)


class TestListRuns:
    @pytest.fixture()
    def seeded(self, store):
        rows = [
            ("Checkout smoke", {"origin": "ui"}),
            ("Nightly regression", {"origin": "cli"}),
            ("Zahlungen PRÜFEN", {"origin": "ui", "env": "staging"}),
        ]
        for n, (title, labels) in enumerate(rows):
            store.create_run(
                make_spec(
                    run_id=f"01LISTRUN000000000000000{n}A",
                    title=title,
                    labels=labels,
                    created_at=f"2026-07-07T10:00:0{n}Z",
                ),
                origin="test",
            )
        return store

    def test_newest_first_with_a_total(self, seeded):
        page = seeded.list_runs()
        assert isinstance(page, RunPage)
        assert titles(page) == ["Zahlungen PRÜFEN", "Nightly regression", "Checkout smoke"]
        assert page.total == 3

    def test_state_filter(self, seeded):
        newest = seeded.list_runs().runs[0].id
        seeded.set_run_headline(newest, "COMPLETE", completion="PASS", completed_at=utc_now())
        page = seeded.list_runs(state="COMPLETE")
        assert run_ids(page) == [newest] and page.total == 1
        assert seeded.list_runs(state="COMPOSED").total == 2

    def test_label_filter_requires_all_pairs(self, seeded):
        assert titles(seeded.list_runs(labels={"origin": "ui"})) == [
            "Zahlungen PRÜFEN", "Checkout smoke",
        ]
        both = seeded.list_runs(labels={"origin": "ui", "env": "staging"})
        assert titles(both) == ["Zahlungen PRÜFEN"] and both.total == 1
        assert seeded.list_runs(labels={"origin": "ui", "env": "prod"}).total == 0
        assert seeded.list_runs(labels={"nosuch": "x"}).total == 0

    def test_label_keys_and_values_are_matched_literally(self, store):
        odd = {'we"ird.key': "a'b", "plain": "100%"}
        store.create_run(make_spec(labels=odd), origin="test")
        assert store.list_runs(labels={'we"ird.key': "a'b"}).total == 1
        assert store.list_runs(labels={"plain": "100%"}).total == 1
        assert store.list_runs(labels={"plain": "100"}).total == 0

    def test_time_window_filter(self, seeded):
        assert titles(seeded.list_runs(since="2026-07-07T10:00:01Z")) == [
            "Zahlungen PRÜFEN", "Nightly regression",
        ]
        assert titles(seeded.list_runs(until="2026-07-07T10:00:00Z")) == ["Checkout smoke"]
        window = seeded.list_runs(since="2026-07-07T10:00:01Z", until="2026-07-07T10:00:01Z")
        assert titles(window) == ["Nightly regression"]

    def test_title_search_is_a_case_insensitive_substring(self, seeded):
        assert titles(seeded.list_runs(q="REGRESS")) == ["Nightly regression"]
        assert titles(seeded.list_runs(q="smoke")) == ["Checkout smoke"]
        assert seeded.list_runs(q="warehouse").total == 0

    def test_title_search_folds_non_ascii_case_too(self, seeded):
        assert titles(seeded.list_runs(q="prüfen")) == ["Zahlungen PRÜFEN"]
        assert titles(seeded.list_runs(q="ZAHLUNGEN prü")) == ["Zahlungen PRÜFEN"]

    def test_title_search_treats_like_wildcards_as_text(self, seeded):
        assert seeded.list_runs(q="%").total == 0
        assert seeded.list_runs(q="_").total == 0

    def test_limit_and_offset_page_through_the_filtered_set(self, seeded):
        first = seeded.list_runs(limit=2)
        assert titles(first) == ["Zahlungen PRÜFEN", "Nightly regression"] and first.total == 3
        second = seeded.list_runs(limit=2, offset=2)
        assert titles(second) == ["Checkout smoke"] and second.total == 3
        beyond = seeded.list_runs(limit=2, offset=10)
        assert beyond.runs == () and beyond.total == 3

    def test_limit_zero_returns_no_rows_but_the_total(self, seeded):
        page = seeded.list_runs(limit=0)
        assert page.runs == () and page.total == 3

    def test_a_negative_limit_does_not_drop_a_row(self, seeded):
        """0.1 sliced ``[:limit]``, so -1 silently lost the last row."""
        page = seeded.list_runs(limit=-1)
        assert page.runs == () and page.total == 3

    def test_filters_combine_and_total_counts_the_filtered_set(self, seeded):
        page = seeded.list_runs(labels={"origin": "ui"}, since="2026-07-07T10:00:01Z", limit=1)
        assert titles(page) == ["Zahlungen PRÜFEN"] and page.total == 1

    def test_rows_carry_their_dispatches_but_not_their_deliveries(self, seeded):
        target = seeded.list_runs(q="nightly").runs[0].id
        seeded.add_dispatch(target, dispatch_id="D1", mode="demo", state="DISPATCHED")
        seeded.add_dispatch(target, dispatch_id="D2", mode="export", state="AWAITING_RESULTS")
        deliver(seeded, target, dispatch_id="D2")
        row = next(run for run in seeded.list_runs().runs if run.id == target)
        assert [d.dispatch_id for d in row.dispatches] == ["D1", "D2"]
        assert row.latest_dispatch.mode == "export"
        assert row.deliveries == ()
        other = next(run for run in seeded.list_runs().runs if run.id != target)
        assert other.dispatches == ()


class TestDeleteAndPrune:
    def _populated(self, store, run_id="01TESTRUN0000000000000000A", **spec):
        record = store.create_run(make_spec(run_id, **spec), origin="test")
        dispatch_id = f"D-{run_id}"
        store.add_dispatch(record.id, dispatch_id=dispatch_id, mode="export", state="AWAITING_RESULTS")
        deliver(store, record.id, dispatch_id=dispatch_id)
        store.record_live_verdict(record.id, dispatch_id, "2", Verdict(T1, "PASS"))
        store.add_artifact_ref(
            record.id, dispatch_id, name="log.html", media_type="text/html", url_or_path="x/log.html"
        )
        store.append_event(record.id, kind="run.composed", at=utc_now())
        store.set_dispatch_spec(dispatch_id, b"{}", "application/json")
        store.set_ingest_token(record.id, dispatch_id, "a" * 64)
        return record.id, dispatch_id

    def test_delete_run_removes_everything_recorded_for_it(self, store, tmp_path):
        doomed, doomed_dispatch = self._populated(store)
        kept, kept_dispatch = self._populated(store, "01TESTRUN0000000000000000B")
        assert store.delete_run(doomed) is True
        assert store.get_run(doomed) is None
        assert store.get_spec_document(doomed) is None
        assert store.get_dispatch(doomed_dispatch) is None
        assert store.get_dispatch_spec(doomed_dispatch) is None
        assert store.verdicts_for(doomed) == []
        assert store.live_verdicts(doomed, doomed_dispatch) == []
        assert store.artifact_refs(doomed) == []
        assert store.events(doomed) == []
        assert store.ingest_tokens(doomed) == {}
        store.close()
        conn = sqlite3.connect(tmp_path / "test.db")
        # What is left in every table is the other run's, and only that.
        for table in ("runs", "specs", "dispatches", "deliveries", "live_verdicts",
                      "artifact_refs", "events", "ingest_tokens", "dispatch_specs"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1, table
        assert conn.execute("SELECT DISTINCT run_id FROM verdicts").fetchall() == [(kept,)]
        conn.close()
        # …and the other run is untouched.
        again = SqliteRunStore(path=str(tmp_path / "test.db"))
        assert len(again.verdicts_for(kept)) == 2
        assert again.get_dispatch_spec(kept_dispatch) == (b"{}", "application/json")

    def test_prune_by_age(self, store):
        old, _ = self._populated(store, "01TESTRUN0000000000000000A", created_at="2020-01-01T00:00:00Z")
        fresh, _ = self._populated(store, "01TESTRUN0000000000000000B", created_at="2026-07-07T10:00:00Z")
        assert store.prune_runs(before="2026-01-01T00:00:00Z") == [old]
        assert store.get_run(old) is None and store.get_spec_document(old) is None
        assert store.verdicts_for(old) == []
        assert store.get_run(fresh) is not None

    def test_prune_beyond_the_newest_n(self, store):
        for n in range(4):
            store.create_run(
                make_spec(f"01TESTRUN000000000000000{n}A", created_at=f"2026-07-07T10:00:0{n}Z"),
                origin="test",
            )
        removed = store.prune_runs(max_runs=2)
        assert sorted(removed) == ["01TESTRUN0000000000000000A", "01TESTRUN0000000000000001A"]
        assert store.list_runs().total == 2

    def test_prune_with_no_bound_removes_nothing(self, store):
        self._populated(store)
        assert store.prune_runs() == []
        assert store.list_runs().total == 1


class TestLatestCompletedRun:
    def _completed(self, store, n, labels, completed_at):
        record = store.create_run(
            make_spec(f"01TESTRUN000000000000000{n}A", labels=labels), origin="test"
        )
        store.set_run_headline(record.id, "COMPLETE", completion="FAIL", completed_at=completed_at)
        return record.id

    def test_latest_means_latest_completed(self, store):
        assert store.latest_completed_run() is None
        first = self._completed(store, 0, {}, "2026-07-07T10:00:00Z")
        second = self._completed(store, 1, {}, "2026-07-07T11:00:00Z")
        store.create_run(make_spec("01TESTRUN0000000000000009A"), origin="test")  # only composed
        assert store.latest_completed_run().id == second
        assert store.latest_completed_run(completed_before="2026-07-07T10:30:00Z").id == first
        assert store.latest_completed_run(completed_before="2026-07-07T09:00:00Z") is None

    def test_a_label_scope_keeps_somebody_elses_run_out(self, store):
        nightly = self._completed(store, 0, {"suite": "nightly", "env": "staging"}, "2026-07-07T10:00:00Z")
        adhoc = self._completed(store, 1, {"suite": "adhoc"}, "2026-07-07T11:00:00Z")
        assert store.latest_completed_run().id == adhoc
        assert store.latest_completed_run(labels={"suite": "nightly"}).id == nightly
        assert store.latest_completed_run(labels={"suite": "nightly", "env": "staging"}).id == nightly
        assert store.latest_completed_run(labels={"suite": "nightly", "env": "prod"}) is None

    def test_it_comes_with_its_dispatches(self, store):
        run = self._completed(store, 0, {}, "2026-07-07T10:00:00Z")
        store.add_dispatch(run, dispatch_id="D1", mode="demo", state="COMPLETE")
        assert [d.dispatch_id for d in store.latest_completed_run().dispatches] == ["D1"]


# -- dispatches -------------------------------------------------------------------


class TestDispatches:
    def test_add_get_and_defaults(self, store, run_id):
        dispatch = store.add_dispatch(
            run_id, dispatch_id="D2", mode="demo", state="DISPATCHED", owner="host:1:tok",
            parameters={"seed": "nightly-2", "partitions": ["env1"]},
        )
        assert dispatch == store.get_dispatch("D2")
        assert (dispatch.run_id, dispatch.mode, dispatch.state) == (run_id, "demo", "DISPATCHED")
        assert dispatch.owner == "host:1:tok"
        assert dispatch.parameters == {"seed": "nightly-2", "partitions": ["env1"]}
        assert dispatch.shards == () and dispatch.declared_shards == 0
        assert dispatch.shard_items == {} and dispatch.links == {} and dispatch.summary == {}
        assert (dispatch.error, dispatch.plan) == ("", "")
        assert (dispatch.completion, dispatch.completed_at, dispatch.spec_sha256) == (None, None, None)
        assert dispatch.cancel_requested is False
        assert store.get_dispatch("nope") is None

    def test_a_dispatch_id_is_added_once(self, store, run_id):
        with pytest.raises(StoreError, match="already exists"):
            store.add_dispatch(run_id, dispatch_id="D1", mode="demo", state="DISPATCHED")

    def test_dispatches_come_back_in_the_order_they_were_added(self, store, run_id):
        for name in ("D9", "D2", "D5"):  # not sorted, on purpose
            store.add_dispatch(run_id, dispatch_id=name, mode="demo", state="DISPATCHED")
        run = store.get_run(run_id)
        assert [d.dispatch_id for d in run.dispatches] == ["D1", "D9", "D2", "D5"]
        assert run.latest_dispatch.dispatch_id == "D5"

    def test_update_each_field(self, store, run_id):
        updated = store.update_dispatch(
            "D1",
            state="COMPLETE",
            shards=("env1-1", "env2-1"),
            shard_items={"env1-1": (T1, T2), "env2-1": [T1]},
            spec_sha256="c" * 64,
            error="the runner said so",
            plan="2 chunk(s)",
            links={"job": "https://ci.shop.example/job/regression"},
            completion="FAIL",
            completed_at="2026-07-07T10:05:00Z",
            summary={"PASS": 1, "FAIL": 1},
            owner="host:1:tok",
            cancel_requested=True,
            parameters={"seed": "x"},
        )
        assert updated == store.get_dispatch("D1")
        assert updated.state == "COMPLETE"
        assert updated.shards == ("env1-1", "env2-1") and updated.declared_shards == 2
        assert updated.shard_items == {"env1-1": (T1, T2), "env2-1": (T1,)}
        assert updated.spec_sha256 == "c" * 64
        assert (updated.error, updated.plan) == ("the runner said so", "2 chunk(s)")
        assert updated.links == {"job": "https://ci.shop.example/job/regression"}
        assert (updated.completion, updated.completed_at) == ("FAIL", "2026-07-07T10:05:00Z")
        assert updated.summary == {"PASS": 1, "FAIL": 1}
        assert updated.owner == "host:1:tok" and updated.cancel_requested is True
        assert updated.parameters == {"seed": "x"}

    def test_an_update_touches_only_what_it_names(self, store, run_id):
        store.update_dispatch("D1", plan="the plan", shards=["1", "2"])
        after = store.update_dispatch("D1", state="RUNNING")
        assert (after.state, after.plan, after.shards) == ("RUNNING", "the plan", ("1", "2"))
        cleared = store.update_dispatch("D1", owner=None, cancel_requested=False)
        assert cleared.owner is None and cleared.cancel_requested is False

    def test_an_unknown_column_is_refused(self, store, run_id):
        with pytest.raises(StoreError, match="cannot set 'run_id'"):
            store.update_dispatch("D1", run_id="other")
        with pytest.raises(StoreError, match="cannot set 'pool_state'"):
            store.update_dispatch("D1", pool_state="warm")

    def test_updating_an_unknown_dispatch_is_refused(self, store, run_id):
        with pytest.raises(StoreError, match="unknown dispatch"):
            store.update_dispatch("nope", state="RUNNING")

    def test_delete_dispatch_leaves_no_trace_of_it(self, store, run_id):
        store.add_dispatch(run_id, dispatch_id="D2", mode="demo", state="DISPATCHED")
        deliver(store, run_id, dispatch_id="D2")
        store.record_live_verdict(run_id, "D2", "2", Verdict(T1, "PASS"))
        store.set_dispatch_spec("D2", b"{}", "application/json")
        store.set_ingest_token(run_id, "D2", "b" * 64)
        store.add_artifact_ref(run_id, "D2", name="log.html", media_type="text/html", url_or_path="x")
        deliver(store, run_id, dispatch_id="D1", content_hash="sha256:keep")

        store.delete_dispatch("D2")
        run = store.get_run(run_id)
        assert [d.dispatch_id for d in run.dispatches] == ["D1"]
        assert [d.dispatch_id for d in run.deliveries] == ["D1"]
        assert store.verdicts_for(run_id, "D2") == [] and len(store.verdicts_for(run_id)) == 2
        assert store.live_verdicts(run_id, "D2") == []
        assert store.get_dispatch_spec("D2") is None
        assert store.ingest_tokens(run_id) == {}
        assert store.artifact_refs(run_id) == []

    def test_owned_dispatches_is_the_recovery_question(self, store, run_id):
        assert store.owned_dispatches() == []
        store.add_dispatch(run_id, dispatch_id="D2", mode="demo", state="RUNNING", owner="host:7:a")
        store.add_dispatch(run_id, dispatch_id="D3", mode="demo", state="DISPATCHED", owner="host:8:b")
        assert [d.dispatch_id for d in store.owned_dispatches()] == ["D2", "D3"]
        store.update_dispatch("D2", owner=None)
        assert [d.dispatch_id for d in store.owned_dispatches()] == ["D3"]


class TestDispatchSpecBytes:
    def test_exact_bytes_round_trip(self, store, run_id):
        content = '{"run": {"title": "Zahlungen prüfen"}}\n'.encode() + b"\x00\xff trailing"
        assert store.get_dispatch_spec("D1") is None
        store.set_dispatch_spec("D1", content, "application/json")
        assert store.get_dispatch_spec("D1") == (content, "application/json")

    def test_setting_again_replaces(self, store, run_id):
        store.set_dispatch_spec("D1", b"first", "application/json")
        store.set_dispatch_spec("D1", b"second: 1\n", "application/yaml")
        assert store.get_dispatch_spec("D1") == (b"second: 1\n", "application/yaml")


class TestIngestTokens:
    def test_only_hashes_are_kept_per_dispatch(self, store, run_id):
        assert store.ingest_tokens(run_id) == {}
        store.set_ingest_token(run_id, "D1", "a" * 64)
        assert store.ingest_tokens(run_id) == {"a" * 64: "D1"}

    def test_a_dispatchs_token_is_replaced_not_added(self, store, run_id):
        store.set_ingest_token(run_id, "D1", "a" * 64)
        store.set_ingest_token(run_id, "D1", "b" * 64)  # rotation
        assert store.ingest_tokens(run_id) == {"b" * 64: "D1"}

    def test_each_dispatch_has_its_own_and_the_run_may_have_one(self, store, run_id):
        store.add_dispatch(run_id, dispatch_id="D2", mode="export", state="AWAITING_RESULTS")
        store.set_ingest_token(run_id, "D1", "a" * 64)
        store.set_ingest_token(run_id, "D2", "b" * 64)
        store.set_ingest_token(run_id, None, "c" * 64)  # run-level (an imported document's)
        assert store.ingest_tokens(run_id) == {"a" * 64: "D1", "b" * 64: "D2", "c" * 64: None}

    def test_tokens_belong_to_their_run(self, store, run_id):
        other = store.create_run(make_spec("01TESTRUN0000000000000000B"), origin="test")
        store.set_ingest_token(run_id, "D1", "a" * 64)
        assert store.ingest_tokens(other.id) == {}
        with pytest.raises(StoreError, match="unknown run"):
            store.set_ingest_token("nope", None, "d" * 64)


# -- deliveries & verdicts (§5 idempotency) ------------------------------------------


class TestDeliveryIdempotency:
    """§5 normative, keyed on (run, dispatch, shard): the same bundle again
    is a no-op, new content for a shard replaces it."""

    def test_new_then_duplicate_is_noop(self, store, run_id):
        assert deliver(store, run_id, content_hash="sha256:aaaa", status="FAIL") == "new"
        assert deliver(store, run_id, content_hash="sha256:aaaa", status="PASS") == "duplicate"
        # the duplicate did NOT overwrite: FAIL from the first delivery remains
        assert [v.status for v in store.verdicts_for(run_id)] == ["FAIL", "PASS"]
        assert len(store.get_run(run_id).deliveries) == 1

    def test_new_content_for_same_shard_replaces(self, store, run_id):
        deliver(store, run_id, content_hash="sha256:aaaa", status="FAIL")
        assert deliver(store, run_id, content_hash="sha256:bbbb", status="PASS") == "replaced"
        # last-writer-wins: FAIL→PASS flip is possible, no monotonic merge
        assert [v.status for v in store.verdicts_for(run_id)] == ["PASS", "PASS"]
        run = store.get_run(run_id)
        assert len(run.deliveries) == 1 and run.deliveries[0].content_hash == "sha256:bbbb"

    def test_other_shard_is_not_replaced(self, store, run_id):
        deliver(store, run_id, shard="1", content_hash="sha256:aaaa")
        assert deliver(store, run_id, shard="2", content_hash="sha256:bbbb") == "new"
        assert store.delivered_shards(run_id, "D1") == {"1", "2"}
        assert len(store.verdicts_for(run_id)) == 4

    def test_the_same_content_under_another_dispatch_is_a_new_delivery(self, store, run_id):
        """Regression: the key used to be (run, content hash), so re-running
        a spec that produced identical results was swallowed as a duplicate
        and the second dispatch never saw a delivery."""
        store.add_dispatch(run_id, dispatch_id="D2", mode="export", state="AWAITING_RESULTS")
        assert deliver(store, run_id, dispatch_id="D1", content_hash="sha256:same") == "new"
        assert deliver(store, run_id, dispatch_id="D2", content_hash="sha256:same") == "new"
        assert store.delivered_shards(run_id, "D1") == {"1"}
        assert store.delivered_shards(run_id, "D2") == {"1"}
        assert len(store.verdicts_for(run_id, "D1")) == 2
        assert len(store.verdicts_for(run_id, "D2")) == 2
        assert len(store.get_run(run_id).deliveries) == 2

    def test_the_same_content_under_another_shard_is_a_new_delivery(self, store, run_id):
        assert deliver(store, run_id, shard="env1", content_hash="sha256:same") == "new"
        assert deliver(store, run_id, shard="env2", content_hash="sha256:same") == "new"
        assert store.delivered_shards(run_id, "D1") == {"env1", "env2"}

    def test_a_delivery_record_says_what_arrived(self, store, run_id):
        deliver(store, run_id, shard="env1", content_hash="sha256:aaaa")
        (delivery,) = store.get_run(run_id).deliveries
        assert (delivery.run_id, delivery.dispatch_id, delivery.shard) == (run_id, "D1", "env1")
        assert (delivery.content_hash, delivery.format) == ("sha256:aaaa", "runcomposer-verdicts")
        assert delivery.delivery_id and delivery.created_at


class TestVerdicts:
    def test_every_verdict_read_back_carries_shard_and_attempt(self, store, run_id):
        store.record_delivery(
            run_id, dispatch_id="D1", shard="env1", content_hash="sha256:a", format="x",
            verdicts=[
                Verdict(T1, "FAIL", 120, message="card declined", attempt=1),
                Verdict(T1, "PASS", 110, attempt=2),
            ],
        )
        store.record_delivery(
            run_id, dispatch_id="D1", shard="env2", content_hash="sha256:b", format="x",
            verdicts=[Verdict(T1, "PASS", 90)],
        )
        verdicts = store.verdicts_for(run_id)
        assert [(v.shard, v.item_id, v.status, v.attempt) for v in verdicts] == [
            ("env1", T1, "FAIL", 1), ("env1", T1, "PASS", 2), ("env2", T1, "PASS", 1),
        ]
        assert verdicts[0].message == "card declined" and verdicts[0].duration_ms == 120

    def test_narrowing_by_dispatch_and_shard(self, store, run_id):
        store.add_dispatch(run_id, dispatch_id="D2", mode="demo", state="RUNNING")
        deliver(store, run_id, dispatch_id="D1", shard="env1", content_hash="sha256:a")
        deliver(store, run_id, dispatch_id="D1", shard="env2", content_hash="sha256:b")
        deliver(store, run_id, dispatch_id="D2", shard="env1", content_hash="sha256:c")
        assert len(store.verdicts_for(run_id)) == 6
        assert len(store.verdicts_for(run_id, "D1")) == 4
        assert {v.shard for v in store.verdicts_for(run_id, "D1", shard="env2")} == {"env2"}
        assert len(store.verdicts_for(run_id, shard="env1")) == 4
        assert store.verdicts_for(run_id, "D2", shard="env2") == []
        assert store.delivered_shards(run_id, "D2") == {"env1"}
        assert store.delivered_shards(run_id, "nope") == set()


class TestConcurrentDelivery:
    """Regression: the check-then-write of ``record_delivery`` is one write
    transaction, so concurrent deliveries to one shard leave exactly one."""

    def assert_exactly_one_delivery(self, store, run_id, outcomes, writers):
        assert sorted(outcomes) == ["new"] + ["replaced"] * (writers - 1)
        run = store.get_run(run_id)
        assert len(run.deliveries) == 1
        verdicts = store.verdicts_for(run_id, "D1")
        assert len(verdicts) == 3, "one delivery's verdicts, not a mixture"
        # All three verdicts are the surviving delivery's own.
        winner = {v.message for v in verdicts}
        assert len(winner) == 1
        assert run.deliveries[0].content_hash == f"sha256:{winner.pop()}"

    def test_eight_threads_to_one_shard_leave_one_delivery(self, store, run_id):
        writers = 8
        barrier = threading.Barrier(writers)
        outcomes: list[str] = []
        errors: list[BaseException] = []

        def write(n: int) -> None:
            try:
                barrier.wait(timeout=10)
                outcomes.append(_deliver_distinct(store, run_id, n))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not errors, errors
        self.assert_exactly_one_delivery(store, run_id, outcomes, writers)

    def test_several_processes_on_one_file_leave_one_delivery(self, store, run_id, tmp_path):
        writers = 4
        mp = multiprocessing.get_context("spawn")
        barrier = mp.Barrier(writers)
        results = mp.Queue()
        processes = [
            mp.Process(
                target=_deliver_from_process,
                args=(str(tmp_path / "test.db"), run_id, n, barrier, results),
            )
            for n in range(writers)
        ]
        for process in processes:
            process.start()
        outcomes = [results.get(timeout=60) for _ in processes]
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
        assert all(not outcome.startswith("error") for outcome in outcomes), outcomes
        self.assert_exactly_one_delivery(store, run_id, outcomes, writers)

    def test_threads_writing_different_shards_all_land(self, store, run_id):
        writers = 8
        barrier = threading.Barrier(writers)

        def write(n: int) -> None:
            barrier.wait(timeout=10)
            deliver(store, run_id, shard=f"shard-{n}", content_hash=f"sha256:{n}")

        threads = [threading.Thread(target=write, args=(n,)) for n in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert store.delivered_shards(run_id, "D1") == {f"shard-{n}" for n in range(writers)}
        assert len(store.verdicts_for(run_id)) == 2 * writers


class TestExclusiveDispatch:
    """Regression: one dispatch in flight per run, held by the store itself —
    so two processes (each with its own dispatch manager and its own lock)
    cannot both open a dispatch for one idle run."""

    def test_an_exclusive_dispatch_is_refused_while_one_is_in_flight(self, store):
        from runcomposer.sdk import RunActive

        run = store.create_run(make_spec(), origin="test")
        store.add_dispatch(run.id, dispatch_id="D1", mode="demo", state="RUNNING", exclusive=True)
        with pytest.raises(RunActive, match="D1, RUNNING"):
            store.add_dispatch(run.id, dispatch_id="D2", mode="demo", state="DISPATCHED", exclusive=True)
        assert [d.dispatch_id for d in store.get_run(run.id).dispatches] == ["D1"]
        # Without `exclusive` the store records what it is told (ingest, export).
        store.add_dispatch(run.id, dispatch_id="D3", mode="ingest", state="AWAITING_RESULTS")

    def test_a_finished_dispatch_does_not_block_the_next(self, store):
        run = store.create_run(make_spec(), origin="test")
        store.add_dispatch(run.id, dispatch_id="D1", mode="demo", state="RUNNING", exclusive=True)
        store.update_dispatch("D1", state="COMPLETE")
        store.add_dispatch(run.id, dispatch_id="D2", mode="demo", state="DISPATCHED", exclusive=True)

    def test_several_processes_racing_for_one_run_open_exactly_one_dispatch(self, store, tmp_path):
        run = store.create_run(make_spec(), origin="test")
        racers = 4
        mp = multiprocessing.get_context("spawn")
        barrier = mp.Barrier(racers)
        results = mp.Queue()
        processes = [
            mp.Process(
                target=_claim_from_process,
                args=(str(tmp_path / "test.db"), run.id, n, barrier, results),
            )
            for n in range(racers)
        ]
        for process in processes:
            process.start()
        outcomes = sorted(results.get(timeout=60) for _ in processes)
        for process in processes:
            process.join(timeout=30)
        assert outcomes == ["claimed"] + ["refused"] * (racers - 1), outcomes
        assert len(store.get_run(run.id).dispatches) == 1


def _claim_from_process(path: str, run_id: str, n: int, barrier, results) -> None:
    from runcomposer.sdk import RunActive

    try:
        store = SqliteRunStore(path=path)
        barrier.wait(timeout=30)
        try:
            store.add_dispatch(
                run_id, dispatch_id=f"D{n}", mode="demo", state="DISPATCHED", exclusive=True
            )
            results.put("claimed")
        except RunActive:
            results.put("refused")
    except BaseException as exc:
        results.put(f"error: {type(exc).__name__}: {exc}")
        raise


def _deliver_distinct(store, run_id: str, n: int) -> str:
    """Writer ``n``'s own content for shard "1": three verdicts tagged with
    its hash, so a mixture of two writers would be visible."""
    marker = f"writer-{n}"
    return store.record_delivery(
        run_id,
        dispatch_id="D1",
        shard="1",
        content_hash=f"sha256:{marker}",
        format="runcomposer-verdicts",
        verdicts=[Verdict(f"Shop.Cart.Core.T00{i}", "PASS", message=marker) for i in (1, 2, 3)],
    )


def _deliver_from_process(path: str, run_id: str, n: int, barrier, results) -> None:
    try:
        store = SqliteRunStore(path=path)
        barrier.wait(timeout=30)
        results.put(_deliver_distinct(store, run_id, n))
    except BaseException as exc:
        results.put(f"error: {type(exc).__name__}: {exc}")
        raise


class TestLiveVerdicts:
    """Status streamed while a shard executes is kept apart from results."""

    def test_live_status_is_not_a_delivered_verdict(self, store, run_id):
        store.record_live_verdict(run_id, "D1", "env1", Verdict(T1, "PASS", 90))
        assert store.verdicts_for(run_id) == []
        assert store.delivered_shards(run_id, "D1") == set()
        (live,) = store.live_verdicts(run_id, "D1")
        assert (live.item_id, live.status, live.shard, live.duration_ms) == (T1, "PASS", "env1", 90)

    def test_one_row_per_shard_and_item_the_latest_wins(self, store, run_id):
        store.record_live_verdict(run_id, "D1", "env1", Verdict(T1, "FAIL", 90, message="first"))
        store.record_live_verdict(run_id, "D1", "env1", Verdict(T1, "PASS", 70, attempt=2))
        store.record_live_verdict(run_id, "D1", "env2", Verdict(T1, "PASS", 60))
        live = store.live_verdicts(run_id, "D1")
        assert [(v.shard, v.status, v.attempt, v.message) for v in live] == [
            ("env1", "PASS", 2, ""), ("env2", "PASS", 1, ""),
        ]

    def test_a_shards_delivery_clears_that_shards_live_status_only(self, store, run_id):
        store.record_live_verdict(run_id, "D1", "env1", Verdict(T1, "PASS"))
        store.record_live_verdict(run_id, "D1", "env2", Verdict(T1, "PASS"))
        deliver(store, run_id, shard="env1")
        assert [v.shard for v in store.live_verdicts(run_id, "D1")] == ["env2"]
        assert {v.shard for v in store.verdicts_for(run_id)} == {"env1"}

    def test_live_status_is_per_dispatch(self, store, run_id):
        store.add_dispatch(run_id, dispatch_id="D2", mode="demo", state="RUNNING")
        store.record_live_verdict(run_id, "D2", "1", Verdict(T1, "PASS"))
        assert store.live_verdicts(run_id, "D1") == []
        assert len(store.live_verdicts(run_id, "D2")) == 1


class TestDurationAggregates:
    def _seed(self, store, n, durations, labels=None):
        run = store.create_run(
            make_spec(f"01HISTORY000000000000000{n}A", labels=labels, item_ids=tuple(durations)),
            origin="test",
        )
        store.add_dispatch(run.id, dispatch_id=f"D-{n}", mode="robot-pool", state="COMPLETE")
        store.record_delivery(
            run.id, dispatch_id=f"D-{n}", shard="1", content_hash=f"sha256:{n}", format="x",
            verdicts=[Verdict(item, "PASS", duration_ms=ms) for item, ms in durations.items()],
        )
        return run.id

    def test_a_fresh_store_has_no_history(self, store):
        assert store.duration_aggregates() == {}

    def test_mean_over_recent_dispatches(self, store):
        self._seed(store, 0, {T1: 100, T2: 300})
        self._seed(store, 1, {T1: 200, T2: 500})
        assert store.duration_aggregates(last_n=5) == {T1: 150.0, T2: 400.0}

    def test_only_the_last_n_dispatches_count(self, store):
        self._seed(store, 0, {T1: 100})
        self._seed(store, 1, {T1: 900})
        assert store.duration_aggregates(last_n=1) == {T1: 900.0}

    def test_label_selector_filters_history(self, store):
        self._seed(store, 0, {T1: 100}, labels={"suite": "nightly"})
        self._seed(store, 1, {T1: 900}, labels={"suite": "adhoc"})
        assert store.duration_aggregates(labels={"suite": "nightly"}, last_n=5) == {T1: 100.0}
        assert store.duration_aggregates(labels={"suite": "weekly"}) == {}

    def test_zero_durations_and_undelivered_dispatches_are_not_history(self, store):
        run = self._seed(store, 0, {T1: 100, T2: 0})
        store.add_dispatch(run, dispatch_id="D-empty", mode="demo", state="RUNNING")
        store.record_live_verdict(run, "D-empty", "1", Verdict(T1, "PASS", 99_999))
        assert store.duration_aggregates() == {T1: 100.0}

    def test_a_runs_current_state_is_irrelevant(self, store):
        """A re-dispatch must still see the run's own earlier history."""
        run = self._seed(store, 0, {T1: 100})
        store.set_run_headline(run, "RUNNING", completion=None, completed_at=None)
        assert store.duration_aggregates() == {T1: 100.0}


# -- artifacts & events ----------------------------------------------------------------


class TestArtifactRefs:
    def test_reads_back_what_it_wrote_with_the_shard(self, store, run_id):
        store.add_artifact_ref(
            run_id, "D1", name="log.html (env1)", media_type="text/html",
            url_or_path="/srv/shop/artifacts/env1/log.html", shard="env1",
        )
        store.add_artifact_ref(
            run_id, None, name="build", media_type="text/html",
            url_or_path="https://ci.shop.example/job/regression/7/",
        )
        refs = store.artifact_refs(run_id)
        assert [(r.name, r.dispatch_id, r.shard) for r in refs] == [
            ("log.html (env1)", "D1", "env1"), ("build", None, None),
        ]
        assert refs[0].media_type == "text/html"
        assert refs[0].url_or_path == "/srv/shop/artifacts/env1/log.html"
        assert refs[1].url_or_path == "https://ci.shop.example/job/regression/7/"

    def test_narrowing_by_dispatch(self, store, run_id):
        store.add_dispatch(run_id, dispatch_id="D2", mode="demo", state="RUNNING")
        store.add_artifact_ref(run_id, "D1", name="a", media_type="text/plain", url_or_path="a")
        store.add_artifact_ref(run_id, "D2", name="b", media_type="text/plain", url_or_path="b")
        assert [r.name for r in store.artifact_refs(run_id, "D2")] == ["b"]
        assert store.artifact_refs(run_id, "nope") == []
        assert store.artifact_refs("nope") == []

    def test_an_artifact_needs_its_run(self, store):
        with pytest.raises(StoreError, match="unknown run"):
            store.add_artifact_ref("nope", None, name="a", media_type="", url_or_path="a")


class TestEvents:
    def test_append_returns_the_event_with_its_cursor(self, store, run_id):
        event = store.append_event(
            run_id, kind="run.running", at="2026-07-07T10:00:01Z", dispatch_id="D1",
            data={"state": "RUNNING", "title": "Zahlungen prüfen", "shards": ["env1", "env2"]},
        )
        assert event.seq >= 1
        assert (event.run_id, event.kind, event.dispatch_id) == (run_id, "run.running", "D1")
        assert event.data == {"state": "RUNNING", "title": "Zahlungen prüfen", "shards": ["env1", "env2"]}
        assert store.events(run_id) == [event]

    def test_the_cursor_is_strictly_increasing_across_runs(self, store, run_id):
        other = store.create_run(make_spec("01TESTRUN0000000000000000B"), origin="test")
        seqs = [
            store.append_event(target, kind="log", at=utc_now()).seq
            for target in (run_id, other.id, run_id, other.id)
        ]
        assert seqs == sorted(seqs) and len(set(seqs)) == 4
        assert [e.seq for e in store.events(run_id)] == [seqs[0], seqs[2]]

    def test_read_after_a_cursor(self, store, run_id):
        events = [store.append_event(run_id, kind=f"log.{n}", at=utc_now()) for n in range(5)]
        assert store.events(run_id, after=0) == events
        assert store.events(run_id, after=events[1].seq) == events[2:]
        assert store.events(run_id, after=events[-1].seq) == []

    def test_limit(self, store, run_id):
        events = [store.append_event(run_id, kind="log", at=utc_now()) for _ in range(5)]
        assert store.events(run_id, limit=2) == events[:2]
        assert store.events(run_id, after=events[1].seq, limit=2) == events[2:4]

    def test_no_data_is_an_empty_mapping(self, store, run_id):
        assert store.append_event(run_id, kind="log", at=utc_now()).data == {}
        assert store.events(run_id)[0].data == {}

    def test_latest_events_is_a_bounded_read_of_the_tail(self, store, run_id):
        events = [store.append_event(run_id, kind=f"log.{n}", at=utc_now()) for n in range(7)]
        assert store.latest_events(run_id, limit=3) == events[-3:]  # oldest first
        assert store.latest_events(run_id, limit=100) == events
        assert store.latest_events(run_id, limit=0) == []
        assert store.latest_events("01NOSUCHRUN000000000000000", limit=5) == []

    def test_events_survive_a_reopen(self, tmp_path):
        path = str(tmp_path / "events.db")
        first = SqliteRunStore(path=path)
        run = first.create_run(make_spec(), origin="test")
        written = first.append_event(run.id, kind="run.composed", at=utc_now(), data={"planned": 2})
        first.close()
        assert SqliteRunStore(path=path).events(run.id) == [written]


# -- the database itself ---------------------------------------------------------------


class TestDatabase:
    def test_a_fresh_database_is_stamped_with_the_schema_version(self, tmp_path):
        path = tmp_path / "fresh.db"
        SqliteRunStore(path=str(path)).close()
        conn = sqlite3.connect(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        conn.close()

    def test_a_file_store_runs_in_wal_mode(self, tmp_path):
        path = tmp_path / "wal.db"
        store = SqliteRunStore(path=str(path))
        store.create_run(make_spec(), origin="test")
        conn = sqlite3.connect(path)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        conn.close()
        store.close()

    def test_every_thread_gets_its_own_connection(self, store, run_id):
        seen: list[int] = []

        def remember() -> None:
            seen.append(id(store._connection()))
            store.get_run(run_id)

        threads = [threading.Thread(target=remember) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert len(set(seen)) == 3 and id(store._connection()) not in seen

    def test_the_schema_has_what_the_port_needs_and_no_runner_state(self, tmp_path):
        """§6.3: runner health and pool state are runner memory, never store
        schema."""
        path = tmp_path / "schema.db"
        SqliteRunStore(path=str(path)).close()
        conn = sqlite3.connect(path)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not row[0].startswith("sqlite_")
        }
        assert tables == {
            "runs", "specs", "dispatches", "dispatch_specs", "ingest_tokens", "deliveries",
            "verdicts", "live_verdicts", "artifact_refs", "events", "duration_seeds", "item_names",
        }
        columns = {
            row[1] for table in tables for row in conn.execute(f"PRAGMA table_info({table})")
        }
        conn.close()
        for forbidden in ("health", "pool", "workers", "heartbeat", "runner_state"):
            assert not any(forbidden in column for column in columns), columns

    def test_deliveries_are_unique_per_run_dispatch_and_shard(self, tmp_path):
        path = tmp_path / "unique.db"
        store = SqliteRunStore(path=str(path))
        run = store.create_run(make_spec(), origin="test")
        store.add_dispatch(run.id, dispatch_id="D1", mode="export", state="AWAITING_RESULTS")
        deliver(store, run.id)
        store.close()
        conn = sqlite3.connect(path)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO deliveries VALUES ('other', ?, 'D1', '1', 'sha256:x', 'x', ?)",
                (run.id, utc_now()),
            )
        conn.close()


class TestInMemory:
    """Regression: ``:memory:`` was documented and unusable (one connection
    per operation — "no such table")."""

    def test_it_works(self):
        store = SqliteRunStore(path=":memory:")
        run = store.create_run(make_spec(), origin="test")
        store.add_dispatch(run.id, dispatch_id="D1", mode="demo", state="RUNNING")
        assert deliver(store, run.id) == "new"
        assert len(store.verdicts_for(run.id)) == 2
        assert store.list_runs().total == 1
        store.close()

    def test_it_is_one_database_across_threads(self):
        store = SqliteRunStore(path=":memory:")
        run = store.create_run(make_spec(), origin="test")
        store.add_dispatch(run.id, dispatch_id="D1", mode="demo", state="RUNNING")
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def work(n: int) -> None:
            try:
                barrier.wait(timeout=10)
                assert store.get_run(run.id) is not None  # another thread's write is visible
                deliver(store, run.id, shard=f"shard-{n}", content_hash=f"sha256:{n}")
                store.append_event(run.id, kind="log", at=utc_now(), data={"n": n})
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not errors, errors
        assert store.delivered_shards(run.id, "D1") == {f"shard-{n}" for n in range(8)}
        assert len(store.events(run.id)) == 8
        store.close()

    def test_two_in_memory_stores_are_two_databases(self):
        first, second = SqliteRunStore(path=":memory:"), SqliteRunStore(path=":memory:")
        first.create_run(make_spec(), origin="test")
        assert second.list_runs().total == 0

    def test_a_closed_in_memory_store_says_so(self):
        store = SqliteRunStore(path=":memory:")
        store.close()
        with pytest.raises(StoreError, match="closed"):
            store.list_runs()

    def test_no_file_named_memory_appears(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        SqliteRunStore(path=":memory:").close()
        assert list(tmp_path.iterdir()) == []

    def test_the_file_uri_form_is_refused_with_a_reason(self):
        with pytest.raises(StoreError, match="'file:' URI form is not supported"):
            SqliteRunStore(path="file:shop?mode=memory&cache=shared")

    def test_through_a_config_it_is_refused_too_and_no_file_appears(self, tmp_path):
        """The check runs before the path is anchored to the config file —
        anchored, 'file:old.db' would be a file of that name beside it."""
        from runcomposer.sdk import ConfigError, Service, load_config

        from .conftest import write_config

        config = load_config(str(write_config(tmp_path, store={"sqlite": {"path": "file:old.db"}})))
        with pytest.raises(ConfigError, match="'file:' URI form is not supported"):
            Service(config).validate()
        assert not list(tmp_path.glob("file:*"))

    def test_a_missing_directory_is_created(self, tmp_path):
        """Like every other directory a config names (artifacts, inbox,
        quarantine), the one the store lives in is created on first use."""
        path = tmp_path / "state" / "nested" / "x.db"
        SqliteRunStore(path=str(path))
        assert path.is_file()

    def test_an_unopenable_path_is_a_store_error(self, tmp_path):
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("a file where a directory is needed", encoding="utf-8")
        with pytest.raises(StoreError, match="sqlite store"):
            SqliteRunStore(path=str(blocker / "x.db"))
        directory = tmp_path / "a-directory.db"
        directory.mkdir()
        with pytest.raises(StoreError, match="cannot open the sqlite store"):
            SqliteRunStore(path=str(directory))


def test_the_store_is_closed_per_thread_and_reopens_on_use(tmp_path):
    store = SqliteRunStore(path=str(Path(tmp_path) / "reopen.db"))
    run = store.create_run(make_spec(), origin="test")
    store.close()
    assert store.get_run(run.id) is not None  # a file store reconnects
