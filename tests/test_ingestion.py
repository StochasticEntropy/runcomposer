"""Result ingestion (DESIGN.md §5): the one pipeline every delivery goes
through — correlation by marker, verification against the handed-out spec,
idempotency per (run, dispatch, shard), computed completion — plus the ingest
token and the quarantine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from runcomposer.app.ingestion import IngestReport, QuarantineReport
from runcomposer.sdk import (
    AuthRequired,
    Forbidden,
    IngestError,
    ParsedVerdict,
    ParserBase,
    Service,
    SpecConflict,
    UnknownRun,
    sha256_hex,
)

from .conftest import compose
from .exec_loader import runcomposer_exec

SSO = ["Shop.Auth.SSO.T001", "Shop.Auth.SSO.T002", "Shop.Auth.SSO.T003"]


def bundle(
    directory: Path,
    verdicts: dict[str, str] | list[str],
    *,
    marker: dict[str, Any] | None = None,
    message: str = "",
) -> Path:
    """A results bundle in the reference format; ``verdicts`` maps native
    names to statuses (a list means all PASS)."""
    if isinstance(verdicts, list):
        verdicts = dict.fromkeys(verdicts, "PASS")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "results.json").write_text(
        json.dumps(
            {
                "format": "runcomposer-verdicts",
                "verdicts": [
                    {"name": name, "status": status, "duration_ms": 30, "message": message}
                    for name, status in verdicts.items()
                ],
            }
        ),
        encoding="utf-8",
    )
    if marker is not None:
        (directory / "runcomposer_run.json").write_text(json.dumps(marker), encoding="utf-8")
    return directory


def exported(service: Service, tmp_path: Path, selection: str = "Auth-SSO", **kwargs: Any):
    """Compose, export, and write the handed-out bytes to a file — the way
    the document reaches an executor."""
    run_id = compose(service, selection, **kwargs)
    export = service.export(run_id)
    spec_file = tmp_path / f"spec-{run_id}.json"
    spec_file.write_bytes(export.content)
    return run_id, export, spec_file


def execute(spec_file: Path, out: Path, *extra: str, seed: str = "a") -> Path:
    assert runcomposer_exec.main([str(spec_file), "--out", str(out), "--simulate", "--seed", seed, *extra]) == 0
    return out


class ExplodingParser(ParserBase):
    """A parser with a bug: it raises something that is not a ParseError."""

    format_id = "shop-exploding"

    def parse(self, path: Any) -> list[ParsedVerdict]:
        raise ZeroDivisionError("the parser divided the till by zero")


class QuotingParser(ParserBase):
    """A parser that fails quoting one of its options, or — told to pass —
    reports a result whose message quotes it."""

    format_id = "shop-quoting"

    def __init__(self, token: str = "", fail: bool = True):
        self.token, self.fail = token, fail

    def parse(self, path: Any) -> list[ParsedVerdict]:
        if self.fail:
            raise RuntimeError(f"the results service refused the token {self.token}")
        return [ParsedVerdict(native_name=SSO[0], status="FAIL", message=f"refused with token {self.token}")]


class OverclaimingParser(ParserBase):
    """A parser that claims a status no executor can report."""

    format_id = "shop-overclaiming"

    def parse(self, path: Any) -> list[ParsedVerdict]:
        return [ParsedVerdict(native_name=SSO[0], status="MISSING")]


# -- the round trip ---------------------------------------------------------------


class TestRoundTrip:
    def test_export_execute_ingest_reaches_complete(self, service, tmp_path):
        run_id, export, spec_file = exported(service, tmp_path)
        assert service.require_run(run_id).state == "AWAITING_RESULTS"
        assert export.dispatch.mode == "export" and export.dispatch.shards == ("1",)
        assert export.dispatch.spec_sha256 == sha256_hex(spec_file.read_bytes())

        out = execute(spec_file, tmp_path / "bundle")
        marker = json.loads((out / "runcomposer_run.json").read_text())
        assert marker == {"run_id": run_id, "spec_sha256": export.dispatch.spec_sha256}

        # No run id given: the marker carries the correlation home.
        report = service.ingest(out)
        assert isinstance(report, IngestReport)
        assert (report.run_id, report.dispatch_id) == (run_id, export.dispatch.dispatch_id)
        assert (report.outcome, report.shard, report.verdict_count) == ("new", "1", 3)
        assert (report.run_state, report.completion) == ("COMPLETE", "PASS")
        assert report.warnings == []
        run = service.require_run(run_id)
        assert run.completed_at is not None
        assert [d.format for d in run.deliveries] == ["runcomposer-verdicts"]
        assert [e.kind for e in service.store.events(run_id)][-2:] == [
            "delivery.recorded",
            "run.completed",
        ]

    def test_the_marker_names_dispatch_and_shard(self, service, tmp_path):
        _run_id, export, spec_file = exported(service, tmp_path)
        out = execute(
            spec_file, tmp_path / "bundle", "--dispatch", export.dispatch.dispatch_id, "--shard", "1"
        )
        report = service.ingest(out)
        assert (report.dispatch_id, report.shard) == (export.dispatch.dispatch_id, "1")
        assert report.completion == "PASS"

    def test_a_failing_simulation_completes_as_fail(self, service, tmp_path):
        run_id, _export, spec_file = exported(service, tmp_path)
        report = service.ingest(execute(spec_file, tmp_path / "bundle", seed="b"))
        assert (report.run_state, report.completion) == ("COMPLETE", "FAIL")
        assert dict(service.require_run(run_id).latest_dispatch.summary) == {"FAIL": 2, "PASS": 1}

    def test_a_marker_from_another_document_is_refused(self, service, tmp_path):
        run_id, _export, spec_file = exported(service, tmp_path)
        # The same document re-serialized is NOT the bytes that were handed out.
        tampered = tmp_path / "tampered.json"
        tampered.write_text(json.dumps(json.loads(spec_file.read_text())), encoding="utf-8")
        out = execute(tampered, tmp_path / "bundle")
        with pytest.raises(IngestError, match="does not match the spec bytes") as refused:
            service.ingest(out)
        assert refused.value.reason == "sha-mismatch" and refused.value.code == "sha-mismatch"
        run = service.require_run(run_id)
        assert run.state == "AWAITING_RESULTS" and run.deliveries == ()

    def test_a_dispatch_without_a_recorded_hash_skips_verification_with_a_warning(
        self, service, tmp_path
    ):
        run_id = compose(service, "Auth-SSO")
        service.ingest(bundle(tmp_path / "first", {SSO[0]: "PASS"}), run_id=run_id)
        report = service.ingest(
            bundle(tmp_path / "second", SSO, marker={"run_id": run_id, "spec_sha256": "f" * 64})
        )
        assert any("verification skipped" in warning for warning in report.warnings)

    @pytest.mark.parametrize(
        ("marker", "reason"),
        [
            (None, "unsolicited"),
            ({"run_id": "01NOSUCHRUN000000000000000"}, "unknown-run"),
            ("{not json", "bad-bundle"),
            ({"dispatch_id": "x"}, "bad-bundle"),
            ({"run_id": "RUN", "shard": 7}, "bad-bundle"),
        ],
        ids=["no-marker", "unknown-run", "marker-not-json", "marker-without-run", "marker-bad-type"],
    )
    def test_bundles_that_cannot_be_correlated_are_refused(self, service, tmp_path, marker, reason):
        run_id = compose(service, "Auth-SSO")
        path = bundle(tmp_path / "bundle", SSO)
        if isinstance(marker, str):
            (path / "runcomposer_run.json").write_text(marker, encoding="utf-8")
        elif marker is not None:
            if marker.get("run_id") == "RUN":
                marker = {**marker, "run_id": run_id}
            (path / "runcomposer_run.json").write_text(json.dumps(marker), encoding="utf-8")
        with pytest.raises(IngestError) as refused:
            service.ingest(path)
        assert refused.value.reason == reason
        assert service.require_run(run_id).dispatches == ()

    def test_a_given_run_or_dispatch_that_contradicts_the_marker_is_a_conflict(
        self, service, tmp_path
    ):
        run_id, export, _spec = exported(service, tmp_path)
        other = compose(service, "Auth-SSO")
        marked = bundle(
            tmp_path / "bundle",
            SSO,
            marker={"run_id": run_id, "dispatch_id": export.dispatch.dispatch_id},
        )
        with pytest.raises(IngestError, match="contradicts the bundle marker's run_id") as refused:
            service.ingest(marked, run_id=other)
        assert refused.value.reason == "marker-conflict"
        with pytest.raises(IngestError, match="contradicts the bundle marker's dispatch_id") as refused:
            service.ingest(marked, run_id=run_id, dispatch_id="01ANOTHERDISPATCH000000000")
        assert refused.value.reason == "marker-conflict"
        with pytest.raises(IngestError) as refused:
            service.ingest(bundle(tmp_path / "b2", SSO), run_id=run_id, dispatch_id="01NOSUCH")
        assert refused.value.reason == "unknown-dispatch"

    def test_a_missing_bundle_and_an_empty_one_are_bad_bundles(self, service, tmp_path):
        run_id = compose(service, "Auth-SSO")
        with pytest.raises(IngestError, match="bundle not found"):
            service.ingest(tmp_path / "nowhere", run_id=run_id)
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(IngestError, match="contains no 'runcomposer-verdicts' results"):
            service.ingest(empty, run_id=run_id)
        assert service.require_run(run_id).dispatches == (), "nothing was written for it"

    def test_a_single_results_file_is_a_bundle_of_one(self, service, tmp_path):
        run_id = compose(service, "Auth-SSO")
        file = bundle(tmp_path / "bundle", SSO) / "results.json"
        report = service.ingest(file, run_id=run_id)
        assert (report.run_state, report.completion, report.verdict_count) == ("COMPLETE", "PASS", 3)


# -- idempotency and replacement ------------------------------------------------------


class TestIdempotency:
    def test_the_same_bundle_again_is_a_no_op(self, service, tmp_path):
        run_id, _export, spec_file = exported(service, tmp_path)
        out = execute(spec_file, tmp_path / "bundle")
        service.ingest(out)
        events_before = len(service.store.events(run_id))
        again = service.ingest(out)
        assert again.outcome == "duplicate"
        assert (again.run_state, again.completion) == ("COMPLETE", "PASS")
        assert len(service.require_run(run_id).deliveries) == 1
        assert len(service.store.verdicts_for(run_id)) == 3
        assert len(service.store.events(run_id)) == events_before, "a no-op emits nothing"

    def test_a_corrected_bundle_replaces_the_shard_and_flips_the_completion(
        self, service, tmp_path
    ):
        run_id, _export, _spec = exported(service, tmp_path)
        first = service.ingest(
            bundle(tmp_path / "red", {SSO[0]: "FAIL", SSO[1]: "PASS", SSO[2]: "PASS"}),
            run_id=run_id,
        )
        assert (first.outcome, first.completion) == ("new", "FAIL")
        completed_at = service.require_run(run_id).completed_at

        second = service.ingest(bundle(tmp_path / "green", SSO), run_id=run_id)
        assert (second.outcome, second.run_state, second.completion) == ("replaced", "COMPLETE", "PASS")
        run = service.require_run(run_id)
        assert len(run.deliveries) == 1 and run.completed_at == completed_at
        assert {v.status for v in service.store.verdicts_for(run_id)} == {"PASS"}
        completions = [
            e.data["completion"] for e in service.store.events(run_id) if e.kind == "run.completed"
        ]
        assert completions == ["FAIL", "PASS"]

    def test_only_the_message_changed_is_a_different_delivery(self, service, tmp_path):
        run_id, _export, _spec = exported(service, tmp_path)
        service.ingest(bundle(tmp_path / "one", {SSO[0]: "FAIL"}, message="timeout"), run_id=run_id)
        report = service.ingest(
            bundle(tmp_path / "two", {SSO[0]: "FAIL"}, message="card declined"), run_id=run_id
        )
        assert report.outcome == "replaced"
        assert [v.message for v in service.store.verdicts_for(run_id)] == ["card declined"]

    def test_a_message_with_a_lone_surrogate_is_stored_as_text(self, service, tmp_path):
        """``"\\ud800"`` in a result file is valid JSON and no text: the
        result is kept, the stray character replaced — not a store error."""
        run_id, _export, _spec = exported(service, tmp_path)
        report = service.ingest(
            bundle(tmp_path / "one", {SSO[0]: "FAIL"}, message="card \ud800 declined"), run_id=run_id
        )
        assert report.outcome == "new"
        assert [v.message for v in service.store.verdicts_for(run_id)] == ["card \ufffd declined"]

    def test_a_marker_field_with_a_lone_surrogate_is_a_refused_bundle(self, service, tmp_path):
        run_id, _export, _spec = exported(service, tmp_path)
        marked = bundle(tmp_path / "one", {SSO[0]: "PASS"}, marker={"run_id": run_id, "shard": "s\ud800"})
        with pytest.raises(IngestError) as refused:
            service.ingest(marked)
        assert refused.value.reason == "bad-bundle" and "'shard'" in str(refused.value)

    def test_shards_accumulate_until_the_declared_set_is_in(self, service, tmp_path):
        """A spec that says `results.shards: 2` exports with shards 1 and 2."""
        composed = service.build_spec_document({"tag_filter": "Auth-SSO"}, title="Two shards")
        composed.spec["results"]["shards"] = 2
        run_id = service.import_spec(composed.spec, origin="test")
        export = service.export(run_id)
        assert export.dispatch.shards == ("1", "2")

        one = service.ingest(bundle(tmp_path / "one", SSO[:2]), run_id=run_id, shard="1")
        assert (one.run_state, one.completion) == ("AWAITING_RESULTS", None)
        two = service.ingest(bundle(tmp_path / "two", SSO[2:]), run_id=run_id, shard="2")
        assert (two.run_state, two.completion) == ("COMPLETE", "PASS")

    def test_a_shard_outside_the_declared_set_is_recorded_but_completes_nothing(
        self, service, tmp_path
    ):
        run_id, _export, _spec = exported(service, tmp_path)
        report = service.ingest(bundle(tmp_path / "x", SSO), run_id=run_id, shard="retry-of-1")
        assert report.outcome == "new" and report.run_state == "AWAITING_RESULTS"
        assert any("was not declared" in warning for warning in report.warnings)
        assert len(service.store.verdicts_for(run_id)) == 3


# -- headline and dispatch attribution -------------------------------------


class TestDispatchAttribution:
    def test_a_late_bundle_for_an_old_dispatch_does_not_change_the_headline(
        self, service, tmp_path
    ):
        run_id, export, _spec = exported(service, tmp_path)
        old = export.dispatch.dispatch_id
        service.finalize(run_id)  # nobody answered the export
        service.dispatch(run_id, "demo", {"seed": "a"}, foreground=True)
        headline = service.require_run(run_id)
        assert (headline.state, headline.completion) == ("COMPLETE", "PASS")
        assert [d.completion for d in headline.dispatches] == ["INCOMPLETE", "PASS"]

        late = bundle(
            tmp_path / "late", dict.fromkeys(SSO, "FAIL"), marker={"run_id": run_id, "dispatch_id": old}
        )
        report = service.ingest(late)
        assert report.dispatch_id == old
        # The report speaks of the run: its headline is still the latest dispatch's.
        assert (report.run_state, report.completion) == ("COMPLETE", "PASS")
        after = service.require_run(run_id)
        assert (after.state, after.completion, after.completed_at) == (
            "COMPLETE",
            "PASS",
            headline.completed_at,
        )
        assert [d.completion for d in after.dispatches] == ["FAIL", "PASS"]
        event = [e for e in service.store.events(run_id) if e.kind == "run.completed"][-1]
        assert event.dispatch_id == old and event.data["latest"] is False

    def test_an_unaddressed_bundle_goes_to_the_dispatch_still_waiting(self, service, tmp_path):
        run_id = compose(service, "Auth-SSO")
        service.dispatch(run_id, "demo", {"seed": "b"}, foreground=True)  # COMPLETE (FAIL)
        export = service.export(run_id)  # a second dispatch, awaiting
        report = service.ingest(bundle(tmp_path / "b", SSO), run_id=run_id)
        assert report.dispatch_id == export.dispatch.dispatch_id
        assert [d.completion for d in service.require_run(run_id).dispatches] == ["FAIL", "PASS"]

    def test_results_for_a_never_dispatched_run_open_an_ingest_dispatch(self, service, tmp_path):
        run_id = compose(service, "Auth-SSO")
        report = service.ingest(bundle(tmp_path / "b", SSO), run_id=run_id)
        assert any("'ingest' dispatch was opened" in warning for warning in report.warnings)
        run = service.require_run(run_id)
        assert [(d.mode, d.state, d.shards) for d in run.dispatches] == [("ingest", "COMPLETE", ("1",))]
        assert run.deliveries[0].dispatch_id == run.dispatches[0].dispatch_id
        assert run.completion == "PASS"


# -- what counts as reported -----------------------------------------------


class TestCompletionAgainstThePlan:
    def test_unknown_native_names_warn_and_are_never_invented(self, service, tmp_path):
        run_id, _export, _spec = exported(service, tmp_path)
        report = service.ingest(
            bundle(tmp_path / "b", {SSO[0]: "PASS", "Shop.Ghost.T404": "PASS", SSO[1]: "PASS"}),
            run_id=run_id,
        )
        assert report.verdict_count == 2
        assert report.warnings == ["unknown native name 'Shop.Ghost.T404' — verdict skipped"]
        assert [v.item_id for v in service.store.verdicts_for(run_id)] == SSO[:2]
        # The third planned item never reported: the run is not green.
        assert (report.run_state, report.completion) == ("COMPLETE", "INCOMPLETE")
        assert dict(service.require_run(run_id).latest_dispatch.summary) == {"PASS": 2, "MISSING": 1}

    def test_a_delivery_whose_only_verdict_was_unresolvable_is_not_a_pass(self, service, tmp_path):
        run_id, _export, _spec = exported(service, tmp_path)
        report = service.ingest(bundle(tmp_path / "b", ["Shop.Ghost.T404"]), run_id=run_id)
        assert report.verdict_count == 0
        assert (report.run_state, report.completion) == ("COMPLETE", "INCOMPLETE")
        assert dict(service.require_run(run_id).latest_dispatch.summary) == {"MISSING": 3}

    def test_one_of_many_delivered_is_incomplete(self, service, tmp_path):
        run_id, _export, _spec = exported(service, tmp_path, selection="Payments")
        first = service.require_spec(run_id)["selection"]["materialized"]["item_ids"][0]
        report = service.ingest(bundle(tmp_path / "b", [first]), run_id=run_id)
        assert (report.run_state, report.completion) == ("COMPLETE", "INCOMPLETE")
        assert dict(service.require_run(run_id).latest_dispatch.summary) == {"PASS": 1, "MISSING": 16}

    def test_skips_alone_complete_as_pass(self, service, tmp_path):
        run_id, _export, _spec = exported(service, tmp_path)
        report = service.ingest(bundle(tmp_path / "b", dict.fromkeys(SSO, "SKIP")), run_id=run_id)
        assert report.completion == "PASS"

    def test_an_error_outranks_a_failure(self, service, tmp_path):
        run_id, _export, _spec = exported(service, tmp_path)
        report = service.ingest(
            bundle(tmp_path / "b", {SSO[0]: "FAIL", SSO[1]: "ERROR", SSO[2]: "PASS"}), run_id=run_id
        )
        assert report.completion == "ERROR"

    def test_a_pushed_bundle_cannot_claim_missing(self, service, tmp_path):
        run_id, _export, _spec = exported(service, tmp_path)
        with pytest.raises(IngestError, match="'status' must be one of") as refused:
            service.ingest(bundle(tmp_path / "b", {SSO[0]: "MISSING"}), run_id=run_id)
        assert refused.value.reason == "bad-bundle"


# -- parsers and formats ---------------------------------------------


class TestParsers:
    @pytest.mark.parametrize(
        "format_id", ["tests.test_ingestion:ExplodingParser", "os:system", "../verdicts", ""]
    )
    def test_a_format_id_is_never_read_as_an_import_path(self, service, tmp_path, format_id):
        run_id = compose(service, "Auth-SSO")
        with pytest.raises(IngestError, match="is not a result format id") as refused:
            service.ingest(bundle(tmp_path / "b", SSO), run_id=run_id, format=format_id or " : ")
        assert refused.value.reason == "bad-bundle"

    def test_a_document_naming_an_import_path_as_its_format_executes_nothing(
        self, service, tmp_path
    ):
        """The format travels in the document (`results.expect`). A document
        must not be able to make this process import a module of its choice."""
        composed = service.build_spec_document(
            {"tag_filter": "Auth-SSO"},
            title="Hostile",
            expect_format="tests.test_ingestion:ExplodingParser",
        )
        run_id = service.import_spec(composed.spec, origin="test")
        with pytest.raises(IngestError, match="never given as import paths"):
            service.ingest(bundle(tmp_path / "b", SSO), run_id=run_id)

    def test_an_unknown_format_is_a_refused_bundle(self, service, tmp_path):
        run_id = compose(service, "Auth-SSO")
        with pytest.raises(IngestError, match="no ResultParser for format 'shop-nonexistent'"):
            service.ingest(bundle(tmp_path / "b", SSO), run_id=run_id, format="shop-nonexistent")

    def test_a_parser_that_crashes_is_a_refused_bundle_not_a_crash(self, make_service, tmp_path):
        service = make_service(
            parsers={"shop-exploding": {"module": "tests.test_ingestion:ExplodingParser"}}
        )
        run_id = compose(service, "Auth-SSO", expect_format="shop-exploding")
        export = service.export(run_id)
        with pytest.raises(IngestError, match="parser failed: ZeroDivisionError") as refused:
            service.ingest(bundle(tmp_path / "b", SSO), run_id=run_id)
        assert refused.value.reason == "bad-bundle"

        # Through a transport it is quarantined — never raised, never lost.
        report = service.ingest_or_quarantine(
            bundle(tmp_path / "b", SSO, marker={"run_id": run_id}), transport="file-drop"
        )
        assert isinstance(report, QuarantineReport)
        assert report.reason == "bad-bundle" and "ZeroDivisionError" in report.detail
        entry = service.quarantine.get(report.entry_id)
        assert (entry.claimed_run_id, entry.transport, entry.format) == (
            run_id,
            "file-drop",
            "shop-exploding",
        )
        assert service.store.get_dispatch(export.dispatch.dispatch_id).state == "AWAITING_RESULTS"

    def test_a_parser_that_fails_quoting_a_secret_is_refused_and_quarantined_without_it(
        self, make_service, tmp_path, monkeypatch, caplog
    ):
        """What a parser says when it fails is answered, printed, logged and
        kept in the quarantine: a configured secret in it is blanked in all."""
        monkeypatch.setenv("SHOP_RESULTS_TOKEN", "results-token-example-9")
        service = make_service(
            parsers={"shop-quoting": {"module": "tests.test_ingestion:QuotingParser", "token": "${env:SHOP_RESULTS_TOKEN}"}}
        )
        run_id = compose(service, "Auth-SSO", expect_format="shop-quoting")
        service.export(run_id)
        with caplog.at_level("DEBUG", logger="runcomposer"):
            with pytest.raises(IngestError, match="the results service refused the token") as refused:
                service.ingest(bundle(tmp_path / "b", SSO), run_id=run_id)
            report = service.ingest_or_quarantine(
                bundle(tmp_path / "b", SSO, marker={"run_id": run_id}), transport="file-drop"
            )
        assert "results-token-example-9" not in str(refused.value) and refused.value.__cause__ is None
        assert isinstance(report, QuarantineReport) and "refused the token" in report.detail
        kept = "".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in service.config.quarantine_dir.rglob("_quarantine.json")
        )
        listed = repr([vars(entry) for entry in service.quarantine.entries()])
        logged = "\n".join(record.getMessage() for record in caplog.records)
        for text in (report.detail, kept, listed, logged):
            assert "results-token-example-9" not in text
        assert kept and "refused the token" in logged

    def test_a_result_message_that_quotes_a_secret_is_stored_without_it(
        self, make_service, tmp_path, monkeypatch
    ):
        """A result's message is free text like any other: it is stored, and
        so served and exported, with configured secrets blanked out."""
        monkeypatch.setenv("SHOP_RESULTS_TOKEN", "results-token-example-9")
        service = make_service(
            parsers={
                "shop-quoting": {
                    "module": "tests.test_ingestion:QuotingParser",
                    "token": "${env:SHOP_RESULTS_TOKEN}",
                    "fail": False,
                }
            }
        )
        run_id = compose(service, "Auth-SSO", expect_format="shop-quoting")
        service.export(run_id)
        service.ingest(bundle(tmp_path / "b", SSO), run_id=run_id)
        messages = [verdict.message for verdict in service.store.verdicts_for(run_id)]
        assert len(messages) == 1 and messages[0].startswith("refused with token ")
        assert "results-token-example-9" not in messages[0]

    def test_an_argument_that_is_not_text_is_a_refused_bundle(self, service, tmp_path):
        run_id, _export, _spec = exported(service, tmp_path)
        with pytest.raises(IngestError, match="the shard given for b holds a lone UTF-16 surrogate") as refused:
            service.ingest(bundle(tmp_path / "b", SSO), run_id=run_id, shard="s\udcff")
        assert refused.value.reason == "bad-bundle"

    def test_a_parser_cannot_report_a_status_no_executor_reports(self, make_service, tmp_path):
        service = make_service(
            parsers={"shop-overclaiming": {"module": "tests.test_ingestion:OverclaimingParser"}}
        )
        run_id = compose(service, "Auth-SSO", expect_format="shop-overclaiming")
        with pytest.raises(IngestError, match="carries status 'MISSING'"):
            service.ingest(bundle(tmp_path / "b", SSO), run_id=run_id)

    def test_a_malformed_results_document_is_a_refused_bundle(self, service, tmp_path):
        run_id = compose(service, "Auth-SSO")
        broken = tmp_path / "b"
        broken.mkdir()
        (broken / "results.json").write_text(
            json.dumps({"format": "runcomposer-verdicts", "verdicts": [{"status": "PASS"}]}),
            encoding="utf-8",
        )
        with pytest.raises(IngestError, match="'name' is required") as refused:
            service.ingest(broken, run_id=run_id)
        assert refused.value.reason == "bad-bundle"


# -- the ingest token ------------------------------------------------------


class TestIngestToken:
    def test_the_token_is_returned_once_and_stored_only_as_a_hash(self, service, tmp_path):
        run_id = compose(service, "Auth-SSO")
        assert service.store.ingest_tokens(run_id) == {}, "composing mints nothing"
        export = service.export(run_id)
        token = export.token
        assert token and token.startswith("rct_")
        assert service.store.ingest_tokens(run_id) == {
            sha256_hex(token.encode()): export.dispatch.dispatch_id
        }
        # Neither the stored document, nor the handed-out bytes, nor the
        # re-download carry it.
        assert "token" not in service.require_spec(run_id)["results"]
        assert token.encode() not in export.content
        assert token.encode() not in service.dispatch_spec(run_id, export.dispatch.dispatch_id)[0]
        assert token not in json.dumps(service.store.get_spec_document(run_id))

    def test_authorize_push_states(self, service):
        run_id = compose(service, "Auth-SSO")
        with pytest.raises(AuthRequired):
            service.ingestion.authorize_push(run_id, None)
        # A run nothing was handed out for has no token that could match.
        with pytest.raises(Forbidden):
            service.ingestion.authorize_push(run_id, "rct_made-up")
        export = service.export(run_id)
        with pytest.raises(AuthRequired):
            service.ingestion.authorize_push(run_id, "")
        with pytest.raises(Forbidden, match="does not match this run"):
            service.ingestion.authorize_push(run_id, "rct_wrong")
        assert service.ingestion.authorize_push(run_id, export.token) == export.dispatch.dispatch_id

    def test_a_token_opens_its_own_run_only(self, service):
        first, second = compose(service, "Auth-SSO"), compose(service, "Auth-SSO")
        token = service.export(first).token
        service.export(second)
        with pytest.raises(Forbidden):
            service.ingestion.authorize_push(second, token)

    def test_rotating_invalidates_the_previous_token(self, service):
        run_id = compose(service, "Auth-SSO")
        export = service.export(run_id)
        fresh = service.rotate_token(run_id, export.dispatch.dispatch_id)
        assert fresh and fresh != export.token
        with pytest.raises(Forbidden):
            service.ingestion.authorize_push(run_id, export.token)
        assert service.ingestion.authorize_push(run_id, fresh) == export.dispatch.dispatch_id
        assert len(service.store.ingest_tokens(run_id)) == 1

    def test_disabled_tokens_open_the_write_path_and_mint_nothing(self, make_service):
        service = make_service(core={"ingestion": {"tokens": "disabled"}})
        run_id = compose(service, "Auth-SSO")
        export = service.export(run_id)
        assert export.token is None and service.store.ingest_tokens(run_id) == {}
        assert service.ingestion.authorize_push(run_id, None) is None
        assert service.rotate_token(run_id, export.dispatch.dispatch_id) is None


class TestImportSpec:
    def _document(self, service: Service, **kwargs: Any) -> dict[str, Any]:
        return service.build_spec_document(
            {"tag_filter": "Auth-SSO"}, title="Composed elsewhere", **kwargs
        ).spec

    def test_a_0_1_style_document_keeps_the_token_hash_and_drops_the_plaintext(self, service):
        document = self._document(service)
        document["results"]["token"] = "rct_minted-by-an-older-release"
        run_id = service.import_spec(document, origin="cli")
        assert run_id == document["run"]["id"]
        assert "token" not in service.require_spec(run_id)["results"]
        assert service.store.ingest_tokens(run_id) == {
            sha256_hex(b"rct_minted-by-an-older-release"): None
        }
        # A run-level token: valid, bound to no particular dispatch.
        assert service.ingestion.authorize_push(run_id, "rct_minted-by-an-older-release") is None
        assert document["results"]["token"], "the caller's document is not modified"

    def test_importing_the_same_document_again_is_a_no_op(self, service):
        document = self._document(service)
        run_id = service.import_spec(document, origin="cli")
        assert service.import_spec(document, origin="cli") == run_id
        assert service.list_runs().total == 1
        assert [e.kind for e in service.store.events(run_id)] == ["run.composed"]

    def test_an_edited_copy_of_a_stored_run_is_refused(self, service):
        """Regression: this used to be silently ignored — the edited file was
        dispatched as if it were the stored run."""
        document = self._document(service)
        run_id = service.import_spec(document, origin="cli")
        edited = json.loads(json.dumps(document))
        edited["selection"]["materialized"]["item_ids"] = SSO[:1]
        edited["selection"]["materialized"]["count"] = 1
        with pytest.raises(SpecConflict, match="already stored and the given document differs"):
            service.import_spec(edited, origin="cli")
        assert service.require_spec(run_id)["selection"]["materialized"]["count"] == 3

    def test_runner_values_and_the_token_do_not_count_as_an_edit(self, service):
        document = self._document(service)
        run_id = service.import_spec(document, origin="cli")
        service.dispatch(run_id, "demo", {"seed": "a"}, foreground=True)  # writes runner.demo
        again = json.loads(json.dumps(document))
        again["results"]["token"] = "rct_whatever"
        assert service.import_spec(again, origin="cli") == run_id

    def test_an_undispatchable_document_is_refused(self, service):
        document = self._document(service)
        del document["selection"]["materialized"]
        with pytest.raises(Exception, match="not dispatchable") as refused:
            service.import_spec(document, origin="cli")
        assert getattr(refused.value, "code", "") == "invalid-request"


# -- quarantine ----------------------------------------------------------------------------


class TestQuarantine:
    def test_a_refused_transport_delivery_lands_in_quarantine(self, service, tmp_path):
        run_id, _export, spec_file = exported(service, tmp_path)
        tampered = tmp_path / "tampered.json"
        tampered.write_text(json.dumps(json.loads(spec_file.read_text())), encoding="utf-8")
        out = execute(tampered, tmp_path / "bundle")
        report = service.ingest_or_quarantine(out, transport="file-drop")
        assert isinstance(report, QuarantineReport) and report.reason == "sha-mismatch"
        entry = service.quarantine.get(report.entry_id)
        assert entry.claimed_run_id == run_id and entry.format == "runcomposer-verdicts"
        assert entry.reason == "sha-mismatch" and "does not match" in entry.detail
        assert (service.quarantine.bundle_path(entry.entry_id) / "results.json").is_file()
        assert service.require_run(run_id).deliveries == ()

    def test_an_identical_quarantined_bundle_is_not_stored_twice(self, service, tmp_path):
        stray = bundle(tmp_path / "stray", SSO)
        first = service.ingest_or_quarantine(stray, transport="file-drop")
        second = service.ingest_or_quarantine(stray, transport="file-drop")
        assert first.entry_id and second.entry_id is None
        assert second.reason == "unsolicited"
        assert len(service.quarantine.entries()) == 1

    def test_attach_is_an_explicit_recorded_action_with_normal_delivery_semantics(
        self, service, tmp_path
    ):
        run_id, export, spec_file = exported(service, tmp_path)
        tampered = tmp_path / "tampered.json"
        tampered.write_text(json.dumps(json.loads(spec_file.read_text())), encoding="utf-8")
        report = service.ingest_or_quarantine(execute(tampered, tmp_path / "b"), transport="push")
        entry_id = report.entry_id

        attached = service.ingestion.attach_quarantined(entry_id, run_id)
        assert (attached.run_state, attached.completion) == ("COMPLETE", "PASS")
        assert attached.dispatch_id == export.dispatch.dispatch_id
        assert service.quarantine.entries() == []
        event = next(e for e in service.store.events(run_id) if e.kind == "delivery.attached")
        assert event.data["quarantine_entry"] == entry_id
        assert event.data["reason"] == "sha-mismatch" and event.data["transport"] == "push"
        assert event.dispatch_id == export.dispatch.dispatch_id

    def test_attach_may_bind_to_a_run_the_marker_did_not_name(self, service, tmp_path):
        target = compose(service, "Auth-SSO")
        stray = bundle(tmp_path / "stray", SSO, marker={"run_id": "01SOMEWHEREELSE00000000000"})
        report = service.ingest_or_quarantine(stray, transport="file-drop")
        assert report.reason == "unknown-run"
        attached = service.ingestion.attach_quarantined(report.entry_id, target)
        assert (attached.run_id, attached.completion) == (target, "PASS")
        event = next(e for e in service.store.events(target) if e.kind == "delivery.attached")
        assert event.data["claimed_run_id"] == "01SOMEWHEREELSE00000000000"

    def test_attach_to_an_unknown_run_or_entry_is_not_found(self, service, tmp_path):
        report = service.ingest_or_quarantine(bundle(tmp_path / "s", SSO), transport="file-drop")
        with pytest.raises(UnknownRun):
            service.ingestion.attach_quarantined(report.entry_id, "01NOSUCHRUN000000000000000")
        assert len(service.quarantine.entries()) == 1, "a failed attach keeps the entry"
        with pytest.raises(LookupError, match="unknown quarantine entry"):
            service.ingestion.attach_quarantined("q-nope", compose(service, "Auth-SSO"))

    def test_an_unattachable_bundle_stays_in_quarantine(self, service, tmp_path):
        run_id = compose(service, "Auth-SSO")
        junk = tmp_path / "junk"
        junk.mkdir()
        (junk / "notes.txt").write_text("not results", encoding="utf-8")
        report = service.ingest_or_quarantine(junk, transport="file-drop")
        with pytest.raises(IngestError):
            service.ingestion.attach_quarantined(report.entry_id, run_id)
        assert [entry.entry_id for entry in service.quarantine.entries()] == [report.entry_id]

    def test_promote_makes_an_ingested_run_of_what_the_bundle_reported(self, service, tmp_path):
        stray = bundle(tmp_path / "stray", {SSO[0]: "PASS", SSO[1]: "FAIL"})
        report = service.ingest_or_quarantine(stray, transport="file-drop")
        entry = service.quarantine.get(report.entry_id)
        assert entry.format is None, "nothing said which format a stray file drop is in"

        with pytest.raises(IngestError, match="does not say which result format"):
            service.ingestion.promote_quarantined(report.entry_id)
        promoted = service.ingestion.promote_quarantined(report.entry_id, format="runcomposer-verdicts")

        run = service.require_run(promoted.run_id)
        assert run.origin == "ingested" and dict(run.labels) == {"transport": "promoted"}
        assert (run.state, run.completion, run.planned_count) == ("COMPLETE", "FAIL", 2)
        assert [d.mode for d in run.dispatches] == ["ingest"]
        assert service.require_spec(run.id)["selection"]["materialized"]["item_ids"] == SSO[:2]
        assert service.quarantine.entries() == []

    def test_promote_records_the_run_the_bundle_claimed(self, service, tmp_path):
        stray = bundle(tmp_path / "stray", SSO, marker={"run_id": "01SOMEWHEREELSE00000000000"})
        report = service.ingest_or_quarantine(stray, transport="push", format="runcomposer-verdicts")
        assert service.quarantine.get(report.entry_id).format == "runcomposer-verdicts"
        promoted = service.ingestion.promote_quarantined(report.entry_id)
        assert dict(service.require_run(promoted.run_id).labels) == {
            "transport": "promoted",
            "claimed_run_id": "01SOMEWHEREELSE00000000000",
        }

    def test_a_bundle_with_nothing_resolvable_cannot_be_promoted(self, service, tmp_path):
        with pytest.raises(IngestError, match="no verdicts resolvable against the catalog"):
            service.ingestion.promote_bundle(bundle(tmp_path / "s", ["Shop.Ghost.T404"]))
        assert service.list_runs().total == 0

    def test_the_quarantine_is_bounded_on_write(self, make_service, tmp_path):
        """Regression: the bound used to be applied only by a manual `gc`."""
        service = make_service(core={"ingestion": {"quarantine_max": 2}})
        ids = []
        for index in range(4):
            stray = bundle(tmp_path / f"stray-{index}", [SSO[index % 3]], message=f"n{index}")
            ids.append(service.ingest_or_quarantine(stray, transport="file-drop").entry_id)
        kept = [entry.entry_id for entry in service.quarantine.entries()]
        assert len(kept) == 2 and set(kept) <= set(ids)
        assert ids[-1] in kept, "the newest entry is never the one evicted"
        assert len(list(service.quarantine.directory.glob("q-*"))) == 2


# -- another framework entirely: the agnosticism proof ----------------------------------

PYTEST_SHOP = Path(__file__).resolve().parent.parent / "examples" / "pytest-shop"


class TestPytestLoop:
    """Compose over a pytest catalog → export → `runcomposer-exec` running
    REAL pytest → JUnit XML → ingest. Nothing in the core knows pytest: the
    manifest owns the id space (node ids) and declares, as aliases, how the
    JUnit report spells each test."""

    @pytest.fixture()
    def pytest_service(self, make_service) -> Service:
        return make_service(sources={"manifest": {"path": str(PYTEST_SHOP / "manifest.json")}})

    def test_compose_export_execute_ingest_with_real_pytest(self, pytest_service, tmp_path):
        import sys

        service = pytest_service
        run_id = compose(service, "Cart", expect_format="junit-xml")
        export = service.export(run_id)
        spec_file = tmp_path / "spec.json"
        spec_file.write_bytes(export.content)
        out = tmp_path / "bundle"
        command = f'"{sys.executable}" "{PYTEST_SHOP / "run_pytest.py"}" {{ids_file}} {{out_dir}}'
        assert runcomposer_exec.main([str(spec_file), "--out", str(out), "--command", command]) == 0

        report = service.ingest(out)
        assert report.verdict_count == 5 and report.warnings == []
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "FAIL")
        assert run.deliveries[0].format == "junit-xml"
        statuses = {v.item_id: v.status for v in service.store.verdicts_for(run_id)}
        assert statuses["test_cart.py::test_total_never_negative"] == "FAIL"
        assert statuses["test_cart.py::test_checkout[visa]"] == "PASS"
        assert set(statuses) == set(service.require_spec(run_id)["selection"]["materialized"]["item_ids"])

    def test_every_name_the_report_spells_resolves_to_exactly_one_id(self, pytest_service, tmp_path):
        """§2, the id-space invariant, on a real report of the whole corpus."""
        import subprocess
        import sys

        service = pytest_service
        ids_file = tmp_path / "ids.txt"
        ids_file.write_text("\n".join(item.id for item in service.catalog.items()) + "\n", encoding="utf-8")
        out = tmp_path / "junit-out"
        subprocess.run(
            [sys.executable, str(PYTEST_SHOP / "run_pytest.py"), str(ids_file), str(out)],
            check=False,
            timeout=300,
        )
        verdicts, warnings = service.ingestion.parse(out, "junit-xml")
        assert warnings == [] and len(verdicts) == 7
        assert len({verdict.item_id for verdict in verdicts}) == 7
        assert {verdict.item_id for verdict in verdicts} == set(service.catalog.item_ids())
