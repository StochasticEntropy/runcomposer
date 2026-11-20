"""The file-drop inbox (DESIGN.md §5, transport 2).

The drop protocol under test: a bundle is taken in when it is *complete* —
the sender said so (the marker, or a ``.done`` file), or every file in it
held still for ``settle_s``. Whatever happens to a bundle, it is archived, so
one bad drop never blocks the ones sorted after it.

The settle logic is driven with an explicit clock (``poll_once(now=…)``);
the one test that runs the real watcher thread waits on the store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from runcomposer.app.inbox import DONE_FILENAME, InboxWatcher
from runcomposer.app.ingestion import IngestReport, QuarantineReport
from runcomposer.sdk import Service

from .conftest import compose, wait_until

SSO = ["Shop.Auth.SSO.T001", "Shop.Auth.SSO.T002", "Shop.Auth.SSO.T003"]


def results(names: list[str], status: str = "PASS") -> str:
    return json.dumps(
        {
            "format": "runcomposer-verdicts",
            "verdicts": [{"name": name, "status": status, "duration_ms": 25} for name in names],
        }
    )


def drop(
    inbox: Path,
    name: str,
    *,
    run_id: str | None = None,
    names: list[str] | None = None,
    done: bool = False,
) -> Path:
    """Drop a bundle into the inbox: results, plus the marker when ``run_id``
    is given, plus a ``.done`` file when asked."""
    target = inbox / name
    target.mkdir(parents=True)
    (target / "results.json").write_text(results(names or SSO), encoding="utf-8")
    if run_id is not None:
        (target / "runcomposer_run.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    if done:
        (target / DONE_FILENAME).write_text("", encoding="utf-8")
    return target


@pytest.fixture()
def inbox(tmp_path: Path) -> Path:
    path = tmp_path / "inbox"
    path.mkdir()
    return path


@pytest.fixture()
def watcher(service: Service, inbox: Path) -> InboxWatcher:
    return InboxWatcher(service, inbox, poll_interval_s=2.0, settle_s=10.0)


def awaiting(service: Service, selection: str = "Auth-SSO") -> str:
    run_id = compose(service, selection)
    service.export(run_id)
    return run_id


# -- complete because the sender said so ------------------------------------------


class TestCompletionSignal:
    def test_a_bundle_with_the_marker_is_taken_at_once(self, service, watcher, inbox):
        run_id = awaiting(service)
        drop(inbox, "bundle-1", run_id=run_id)
        reports = watcher.poll_once(now=0.0)
        assert len(reports) == 1 and isinstance(reports[0], IngestReport)
        assert (reports[0].run_id, reports[0].outcome) == (run_id, "new")
        run = service.require_run(run_id)
        assert (run.state, run.completion) == ("COMPLETE", "PASS")

    def test_a_done_file_is_a_completion_signal_too(self, service, watcher, inbox):
        """A sender that cannot write the marker last drops `.done` instead.
        The bundle is then complete — and, lacking a marker, unsolicited."""
        drop(inbox, "bundle-1", done=True)
        reports = watcher.poll_once(now=0.0)
        assert len(reports) == 1 and isinstance(reports[0], QuarantineReport)
        assert reports[0].reason == "unsolicited"

    def test_a_processed_bundle_is_archived_and_never_seen_again(self, service, watcher, inbox):
        run_id = awaiting(service)
        drop(inbox, "bundle-1", run_id=run_id)
        assert len(watcher.poll_once(now=0.0)) == 1
        assert not (inbox / "bundle-1").exists()
        archived = list((inbox / "processed").iterdir())
        assert len(archived) == 1 and archived[0].name.startswith("bundle-1-")
        assert (archived[0] / "results.json").is_file()
        # processed/ is not a bundle.
        assert watcher.poll_once(now=1.0) == [] and watcher.poll_once(now=100.0) == []
        assert len(service.require_run(run_id).deliveries) == 1

    def test_the_same_bundle_dropped_again_is_a_no_op(self, service, watcher, inbox):
        run_id = awaiting(service)
        drop(inbox, "bundle-1", run_id=run_id)
        watcher.poll_once(now=0.0)
        drop(inbox, "bundle-1", run_id=run_id)  # a git re-pull, a retried copy
        (report,) = watcher.poll_once(now=5.0)
        assert isinstance(report, IngestReport) and report.outcome == "duplicate"
        assert len(service.require_run(run_id).deliveries) == 1
        assert len(list((inbox / "processed").iterdir())) == 2


# -- complete because nothing moves any more ----------------------------


class TestSettling:
    def test_a_bundle_without_a_signal_waits_until_it_held_still(self, service, watcher, inbox):
        drop(inbox, "slow-copy")
        assert watcher.poll_once(now=0.0) == [], "first sight: the clock starts"
        assert watcher.poll_once(now=5.0) == [], "unchanged, but not for settle_s yet"
        assert watcher.poll_once(now=9.9) == []
        assert (inbox / "slow-copy").is_dir()
        (report,) = watcher.poll_once(now=10.0)
        assert isinstance(report, QuarantineReport)  # complete now; markerless → quarantine
        assert not (inbox / "slow-copy").exists()

    def test_a_file_that_is_still_growing_restarts_the_clock(self, service, watcher, inbox):
        """The old check looked at the directory's mtime only, which does not
        change while a file inside it is being written: a slow copy was
        ingested half-written."""
        target = drop(inbox, "slow-copy")
        payload = target / "results.json"
        assert watcher.poll_once(now=0.0) == []
        payload.write_text(payload.read_text() + " ", encoding="utf-8")  # still being written
        assert watcher.poll_once(now=9.0) == [], "it changed: stillness starts over"
        assert watcher.poll_once(now=18.9) == [], "9.9 s still — not enough"
        assert len(watcher.poll_once(now=19.0)) == 1

    def test_a_file_added_later_restarts_the_clock(self, service, watcher, inbox):
        target = drop(inbox, "slow-copy")
        assert watcher.poll_once(now=0.0) == []
        (target / "second.json").write_text(results(SSO[:1]), encoding="utf-8")
        assert watcher.poll_once(now=10.0) == []
        assert len(watcher.poll_once(now=20.0)) == 1

    def test_the_marker_arriving_ends_the_wait(self, service, watcher, inbox):
        run_id = awaiting(service)
        target = drop(inbox, "bundle-1")
        assert watcher.poll_once(now=0.0) == []
        (target / "runcomposer_run.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
        (report,) = watcher.poll_once(now=0.1)
        assert isinstance(report, IngestReport) and report.completion == "PASS"

    def test_a_half_written_bundle_is_not_ingested(self, service, watcher, inbox):
        """The scenario the protocol exists for, end to end: results arrive
        in two writes, the marker last. Nothing is taken in between."""
        run_id = awaiting(service)
        target = inbox / "bundle-1"
        target.mkdir()
        payload = target / "results.json"
        payload.write_text(results(SSO)[:40], encoding="utf-8")  # truncated JSON
        assert watcher.poll_once(now=0.0) == []
        assert watcher.poll_once(now=3.0) == []
        payload.write_text(results(SSO), encoding="utf-8")
        assert watcher.poll_once(now=6.0) == []
        (target / "runcomposer_run.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
        (report,) = watcher.poll_once(now=6.5)
        assert isinstance(report, IngestReport) and report.verdict_count == 3
        assert service.quarantine.entries() == []

    def test_settle_defaults_to_two_poll_intervals(self, service, inbox):
        assert InboxWatcher(service, inbox, poll_interval_s=3.0).settle_s == 6.0
        assert InboxWatcher(service, inbox, poll_interval_s=3.0, settle_s=0).settle_s == 0.0

    def test_loose_files_and_a_missing_inbox_are_ignored(self, service, watcher, inbox, tmp_path):
        (inbox / "stray.txt").write_text("not a bundle", encoding="utf-8")
        assert watcher.poll_once(now=0.0) == [] and watcher.poll_once(now=99.0) == []
        assert (inbox / "stray.txt").is_file()
        assert InboxWatcher(service, tmp_path / "no-such-inbox").poll_once(now=0.0) == []


# -- a bad bundle blocks nothing ------------------------------------------


class TestBadBundles:
    def test_a_bad_bundle_is_quarantined_archived_and_does_not_block_the_next(
        self, service, watcher, inbox
    ):
        run_id = awaiting(service)
        broken = inbox / "a-broken"  # sorts first
        broken.mkdir()
        (broken / "results.json").write_text(
            json.dumps({"format": "runcomposer-verdicts", "verdicts": [{"status": "PASS"}]}),
            encoding="utf-8",
        )
        (broken / "runcomposer_run.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
        drop(inbox, "b-good", run_id=run_id)

        reports = watcher.poll_once(now=0.0)

        assert [type(report) for report in reports] == [QuarantineReport, IngestReport]
        assert reports[0].reason == "bad-bundle" and "'name' is required" in reports[0].detail
        assert service.require_run(run_id).state == "COMPLETE"
        assert sorted(p.name.rsplit("-", 1)[0] for p in (inbox / "processed").iterdir()) == [
            "a-broken",
            "b-good",
        ]
        (entry,) = service.quarantine.entries()
        assert (entry.reason, entry.transport, entry.claimed_run_id) == ("bad-bundle", "file-drop", run_id)
        # …and it is not processed again on the next pass.
        assert watcher.poll_once(now=1.0) == []

    @pytest.mark.parametrize("marker", ["{broken json", '["not", "an", "object"]'])
    def test_an_unreadable_marker_is_quarantined_and_archived(self, service, watcher, inbox, marker):
        target = drop(inbox, "bundle-1")
        (target / "runcomposer_run.json").write_text(marker, encoding="utf-8")
        (report,) = watcher.poll_once(now=0.0)
        assert isinstance(report, QuarantineReport) and report.reason == "bad-bundle"
        assert not target.exists() and len(service.quarantine.entries()) == 1

    def test_a_bundle_for_an_unknown_run_is_quarantined_never_a_run(self, service, watcher, inbox):
        drop(inbox, "bundle-1", run_id="01SOMEBODYELSESRUN00000000")
        (report,) = watcher.poll_once(now=0.0)
        assert isinstance(report, QuarantineReport) and report.reason == "unknown-run"
        assert service.list_runs().total == 0
        (entry,) = service.quarantine.entries()
        assert entry.claimed_run_id == "01SOMEBODYELSESRUN00000000" and entry.format is None

    def test_an_exception_inside_ingestion_still_archives(self, service, watcher, inbox, monkeypatch):
        """Even a failure the pipeline did not foresee must not leave the
        bundle in place to be retried forever in front of everything else."""
        run_id = awaiting(service)
        drop(inbox, "a-poison", run_id=run_id)
        drop(inbox, "b-good", run_id=run_id)
        real = service.ingestion.ingest_or_quarantine

        def explode_on_poison(bundle: Any, **kwargs: Any) -> Any:
            if Path(bundle).name == "a-poison":
                raise OSError("disk went away")
            return real(bundle, **kwargs)

        monkeypatch.setattr(service.ingestion, "ingest_or_quarantine", explode_on_poison)
        reports = watcher.poll_once(now=0.0)
        assert len(reports) == 1 and isinstance(reports[0], IngestReport)
        assert not (inbox / "a-poison").exists() and not (inbox / "b-good").exists()
        assert len(list((inbox / "processed").iterdir())) == 2
        assert service.require_run(run_id).state == "COMPLETE"


# -- the real watcher ---------------------------------------------------------------------


def test_the_watcher_thread_ingests_a_dropped_bundle(service, inbox):
    run_id = awaiting(service)
    watcher = InboxWatcher(service, inbox, poll_interval_s=0.05)
    watcher.start()
    try:
        assert (inbox / "processed").is_dir()
        drop(inbox, "bundle-1", run_id=run_id)
        wait_until(
            lambda: service.require_run(run_id).state == "COMPLETE",
            what="the watcher to ingest the drop",
        )
    finally:
        watcher.stop()
    assert service.require_run(run_id).completion == "PASS"
    assert not (inbox / "bundle-1").exists()


def test_serve_runs_the_watcher_on_the_configured_inbox(make_client, tmp_path):
    client, service = make_client(
        core={"ingestion": {"inbox": "drops", "poll_interval_s": 0.05, "settle_s": 0.1}}
    )
    inbox = tmp_path / "drops"
    wait_until(lambda: (inbox / "processed").is_dir(), what="the watcher to create its inbox")
    run_id = awaiting(service)
    drop(inbox, "bundle-1", run_id=run_id)
    wait_until(
        lambda: client.get(f"/api/v1/runs/{run_id}").json()["state"] == "COMPLETE",
        what="the served watcher to ingest the drop",
    )
    # A markerless drop settles (0.1 s of stillness) and is quarantined.
    drop(inbox, "bundle-2")
    wait_until(
        lambda: client.get("/api/v1/quarantine").json()["entries"],
        what="the markerless drop to be quarantined",
    )
