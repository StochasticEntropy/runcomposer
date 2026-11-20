"""History-based selection (DESIGN.md §7): "rerun what failed", resolved at
compose time with provenance.

The runs here are seeded with explicit completion times, because the
questions are about *which* run "latest" and "before" pick.
"""

from __future__ import annotations

from typing import Any

import pytest

from runcomposer.core.spec import build_spec
from runcomposer.sdk import InvalidRequest, Service, Verdict

T1, T2, T3, T4 = (f"Shop.Payments.Cards.T00{n}" for n in (1, 2, 3, 4))


def seed(
    service: Service,
    statuses: dict[str, str],
    completed_at: str,
    *,
    labels: dict[str, str] | None = None,
    complete: bool = True,
    title: str = "Seeded run",
) -> Any:
    """A run as history holds it: planned for exactly ``statuses``, delivered
    (unless ``complete`` is false), completed at ``completed_at``."""
    spec = build_spec(
        title=title,
        tag_filter="Payments",
        materialized_ids=list(statuses),
        source_provider="manifest",
        snapshot=service.catalog.snapshot(),
        results={"expect": [{"format": "runcomposer-verdicts"}], "shards": 1, "deliver": "none"},
        labels=labels,
        created_at=completed_at,
    )
    run = service.store.create_run(spec, origin="test")
    dispatch = service.ledger.open(run.id, "export", state="AWAITING_RESULTS", shards=["1"])
    if complete:
        service.ingestion.record(
            dispatch,
            "1",
            [Verdict(item_id, status, 50) for item_id, status in statuses.items()],
            format_id="runcomposer-verdicts",
        )
        finished = service.store.update_dispatch(dispatch.dispatch_id, completed_at=completed_at)
        service.store.set_run_headline(
            run.id, "COMPLETE", completion=finished.completion, completed_at=completed_at
        )
    return service.require_run(run.id)


class TestWhichRun:
    def test_latest_means_latest_completed(self, service):
        seed(service, {T1: "FAIL"}, "2026-07-01T00:00:00Z")
        newest = seed(service, {T2: "FAIL", T3: "PASS"}, "2026-07-02T00:00:00Z")
        # An even newer run that is still waiting for results is not history yet.
        seed(service, {T4: "FAIL"}, "2026-07-03T00:00:00Z", complete=False)

        ids, provenance = service.resolve_history("failed@latest")
        assert ids == [T2]
        assert provenance == {
            "provider": "history",
            "query": {"run": "LATEST", "verdicts": ["FAIL"]},
            "resolved_run_id": newest.id,
            "resolved_run_completed_at": "2026-07-02T00:00:00Z",
            "resolved_run_title": "Seeded run",
        }

    def test_before_and_run_selectors(self, service):
        early = seed(service, {T1: "FAIL"}, "2026-07-01T00:00:00Z")
        seed(service, {T2: "FAIL"}, "2026-07-05T00:00:00Z")
        ids, provenance = service.resolve_history("failed@before:2026-07-02T00:00:00Z")
        assert ids == [T1] and provenance["query"]["run"] == {"before": "2026-07-02T00:00:00Z"}
        # Any ISO-8601 spelling of the same instant is the same cutoff.
        ids, provenance = service.resolve_history("failed@before:2026-07-02T02:00:00+02:00")
        assert ids == [T1] and provenance["query"]["run"] == {"before": "2026-07-02T00:00:00Z"}
        ids, provenance = service.resolve_history(f"passed@run:{early.id}")
        assert ids == [] and provenance["resolved_run_id"] == early.id
        with pytest.raises(InvalidRequest, match="matched no completed run"):
            service.resolve_history("failed@before:2026-06-01T00:00:00Z")

    def test_an_empty_store_goes_dark_not_wrong(self, service):
        with pytest.raises(InvalidRequest, match="dark on a fresh store"):
            service.resolve_history("failed@latest")

    @pytest.mark.parametrize(
        ("query", "message"),
        [
            ("exploded@latest", "verdict in"),
            ("failed", "'<verdict>@<selector>'"),
            ("@latest", "'<verdict>@<selector>'"),
            ("failed@yesterday", "unknown history selector"),
            ("failed@latest?suite", "key=value"),
            ("failed@latest?", "empty label scope"),
            ("failed@before:teatime", "not an ISO-8601 time"),
        ],
    )
    def test_malformed_queries_are_refused(self, service, query, message):
        with pytest.raises(InvalidRequest, match=message):
            service.resolve_history(query)

    def test_a_query_must_be_a_string(self, service):
        with pytest.raises(InvalidRequest, match="must be a string"):
            service.resolve_history(["failed@latest"])  # type: ignore[arg-type]


class TestWhichItems:
    def test_several_verdicts_can_be_asked_for_at_once(self, service):
        seed(service, {T1: "FAIL", T2: "ERROR", T3: "PASS", T4: "SKIP"}, "2026-07-01T00:00:00Z")
        assert service.resolve_history("failed@latest")[0] == [T1]
        assert service.resolve_history("error@latest")[0] == [T2]
        assert service.resolve_history("skipped@latest")[0] == [T4]
        ids, provenance = service.resolve_history("failed+error@latest")
        assert ids == [T1, T2] and provenance["query"]["verdicts"] == ["FAIL", "ERROR"]

    def test_items_that_never_reported_are_selectable_as_missing(self, service, tmp_path):
        run = seed(service, {T1: "PASS", T2: "PASS", T3: "PASS"}, "2026-07-01T00:00:00Z", complete=False)
        dispatch = run.latest_dispatch
        service.ingestion.record(dispatch, "1", [Verdict(T1, "PASS")], format_id="runcomposer-verdicts")
        ids, provenance = service.resolve_history(f"missing@run:{run.id}")
        assert ids == [T2, T3] and provenance["query"]["verdicts"] == ["MISSING"]
        assert service.resolve_history(f"failed+missing@run:{run.id}")[0] == [T2, T3]

    def test_the_last_attempt_decides_what_failed(self, service):
        run = seed(service, {T1: "PASS", T2: "PASS"}, "2026-07-01T00:00:00Z", complete=False)
        service.ingestion.record(
            run.latest_dispatch,
            "1",
            [
                Verdict(T1, "FAIL", attempt=1),
                Verdict(T2, "FAIL", attempt=1),
                Verdict(T1, "PASS", attempt=2),
            ],
            format_id="runcomposer-verdicts",
        )
        assert service.resolve_history(f"failed@run:{run.id}")[0] == [T2]

    def test_only_the_latest_dispatch_of_the_run_counts(self, service):
        run = seed(service, {T1: "FAIL", T2: "PASS"}, "2026-07-01T00:00:00Z")
        again = service.ledger.open(run.id, "export", state="AWAITING_RESULTS", shards=["1"])
        service.ingestion.record(
            again, "1", [Verdict(T1, "PASS"), Verdict(T2, "FAIL")], format_id="runcomposer-verdicts"
        )
        assert service.resolve_history(f"failed@run:{run.id}")[0] == [T2]


class TestScope:
    """Unscoped, "latest" ranges over every completed run in the store; a
    label scope narrows it to one stream of runs."""

    def test_unscoped_latest_picks_the_foreign_run_scoped_does_not(self, service):
        nightly = seed(service, {T1: "FAIL", T2: "FAIL"}, "2026-07-01T00:00:00Z", labels={"suite": "nightly"})
        adhoc = seed(service, {T1: "FAIL"}, "2026-07-02T00:00:00Z", labels={"suite": "adhoc"})
        unscoped_ids, unscoped = service.resolve_history("failed@latest")
        assert unscoped["resolved_run_id"] == adhoc.id and unscoped_ids == [T1]
        scoped_ids, scoped = service.resolve_history("failed@latest?suite=nightly")
        assert scoped["resolved_run_id"] == nightly.id and scoped_ids == [T1, T2]
        by_argument, _ = service.resolve_history("failed@latest", labels={"suite": "nightly"})
        assert by_argument == scoped_ids

    def test_provenance_says_which_run_and_why(self, service):
        nightly = seed(
            service, {T1: "FAIL"}, "2026-07-01T00:00:00Z",
            labels={"suite": "nightly", "env": "staging"}, title="Nightly regression",
        )
        _ids, provenance = service.resolve_history("failed@latest?suite=nightly")
        assert provenance == {
            "provider": "history",
            "query": {"run": "LATEST", "verdicts": ["FAIL"], "labels": {"suite": "nightly"}},
            "resolved_run_id": nightly.id,
            "resolved_run_completed_at": "2026-07-01T00:00:00Z",
            "resolved_run_labels": {"suite": "nightly", "env": "staging"},
            "resolved_run_title": "Nightly regression",
        }

    def test_several_labels_and_percent_escapes(self, service):
        seed(service, {T1: "FAIL"}, "2026-07-01T00:00:00Z", labels={"suite": "nightly", "env": "staging"})
        seed(service, {T2: "FAIL"}, "2026-07-02T00:00:00Z", labels={"suite": "nightly", "env": "a&b=c"})
        assert service.resolve_history("failed@latest?suite=nightly&env=staging")[0] == [T1]
        ids, provenance = service.resolve_history("failed@latest?suite=nightly&env=a%26b%3Dc")
        assert ids == [T2] and provenance["query"]["labels"] == {"suite": "nightly", "env": "a&b=c"}

    def test_before_takes_a_scope_too(self, service):
        seed(service, {T1: "FAIL"}, "2026-07-01T00:00:00Z", labels={"suite": "nightly"})
        seed(service, {T2: "FAIL"}, "2026-07-02T00:00:00Z", labels={"suite": "adhoc"})
        ids, provenance = service.resolve_history("failed@before:2026-07-03T00:00:00Z?suite=nightly")
        assert ids == [T1]
        assert provenance["query"] == {
            "run": {"before": "2026-07-03T00:00:00Z"},
            "verdicts": ["FAIL"],
            "labels": {"suite": "nightly"},
        }

    def test_a_scope_that_matches_nothing_says_so(self, service):
        seed(service, {T1: "FAIL"}, "2026-07-01T00:00:00Z", labels={"suite": "adhoc"})
        with pytest.raises(InvalidRequest, match="scoped to labels"):
            service.resolve_history("failed@latest?suite=nightly")

    def test_a_scope_on_an_explicit_run_is_refused_not_ignored(self, service):
        run = seed(service, {T1: "FAIL"}, "2026-07-01T00:00:00Z", labels={"suite": "nightly"})
        with pytest.raises(InvalidRequest, match="already names one run"):
            service.resolve_history(f"failed@run:{run.id}?suite=nightly")
        with pytest.raises(InvalidRequest, match="already names one run"):
            service.resolve_history(f"failed@run:{run.id}", labels={"suite": "nightly"})

    def test_conflicting_scopes_are_refused(self, service):
        with pytest.raises(InvalidRequest, match="refusing to guess"):
            service.resolve_history("failed@latest?suite=nightly", labels={"suite": "smoke"})


class TestComposeFromHistory:
    def test_the_spec_materializes_only_the_failures_and_records_provenance(self, service):
        seed(service, {T1: "FAIL", T2: "PASS", "Shop.Retired.T999": "FAIL"}, "2026-07-01T00:00:00Z")
        result = service.compose_run({"history": "failed@latest"}, title="Rerun", origin="test")
        spec = result.spec
        assert spec["selection"]["materialized"]["item_ids"] == [T1]
        assert spec["selection"]["item_ids"] == [T1]
        assert spec["selection"]["derived_from"][0]["provider"] == "history"
        # An item that left the catalog is dropped with a warning, not invented.
        assert result.warnings == ["history item 'Shop.Retired.T999' is no longer in the catalog — dropped"]

    def test_the_scope_survives_into_the_composed_spec(self, service):
        seed(service, {T1: "FAIL"}, "2026-07-01T00:00:00Z", labels={"suite": "nightly"})
        seed(service, {T2: "FAIL"}, "2026-07-02T00:00:00Z", labels={"suite": "adhoc"})
        result = service.compose_run(
            {"history": "failed@latest?suite=nightly"}, title="Nightly rerun", origin="test",
            labels={"suite": "nightly"},
        )
        assert result.spec["selection"]["materialized"]["item_ids"] == [T1]
        assert result.spec["selection"]["derived_from"][0]["query"]["labels"] == {"suite": "nightly"}

    def test_history_narrows_and_is_narrowed_by_the_rest_of_the_selection(self, service):
        seed(service, {T1: "FAIL", T2: "FAIL", "Shop.Auth.Login.T001": "FAIL"}, "2026-07-01T00:00:00Z")
        narrowed = service.preview({"history": "failed@latest", "tag_filter": "Payments"})
        assert [item.id for item in narrowed.items] == [T1, T2]
        picked = service.preview({"history": "failed@latest", "item_ids": [T2, T3]})
        assert [item.id for item in picked.items] == [T2]
        # Picks that share nothing with the history leave nothing — never
        # "everything the filter matches".
        nothing = service.preview({"history": "failed@latest", "item_ids": [T3], "tag_filter": "Payments"})
        assert nothing.items == [] and "selection matched 0 items" in nothing.warnings

    def test_no_failures_means_nothing_to_rerun(self, service):
        run = seed(service, {T1: "PASS", T2: "PASS"}, "2026-07-01T00:00:00Z")
        with pytest.raises(InvalidRequest, match=f"matched no items in run {run.id} — nothing to rerun"):
            service.compose_run({"history": "failed@latest"}, title="Rerun", origin="test")
        assert service.list_runs().total == 1

    def test_the_structured_form_carries_its_scope(self, service):
        nightly = seed(service, {T1: "FAIL"}, "2026-07-01T00:00:00Z", labels={"suite": "nightly"})
        seed(service, {T2: "FAIL"}, "2026-07-02T00:00:00Z", labels={"suite": "adhoc"})
        preview = service.preview({"history": {"query": "failed@latest", "labels": {"suite": "nightly"}}})
        assert [item.id for item in preview.items] == [T1]
        assert preview.derived_from[0]["resolved_run_id"] == nightly.id
        assert preview.catalog_size == 60
        with pytest.raises(InvalidRequest, match="labels must be a mapping"):
            service.preview({"history": {"query": "failed@latest", "labels": "suite=nightly"}})
