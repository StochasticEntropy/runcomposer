"""Housekeeping (DESIGN.md §5, §6.4): retention applied by ``gc``, artifacts
that live and die with their run, and a quarantine that is bounded when it is
written to — not only when somebody remembers to collect garbage."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from runcomposer.app.housekeeping import remove_run_artifacts
from runcomposer.app.quarantine import Quarantine, QuarantineError
from runcomposer.cli import main as cli_main
from runcomposer.core.spec import build_spec
from runcomposer.sdk import NotFound, RunActive, Service, bundle_hash, new_ulid

from .conftest import compose, run_to_end, wait_until

OLD = "2020-01-01T00:00:00Z"


def stored_run(service: Service, *, created_at: str | None = None, title: str = "Housekeeping") -> str:
    """A run with a chosen creation time — retention is judged by it."""
    spec = build_spec(
        title=title,
        tag_filter="Smoke",
        materialized_ids=["Shop.Payments.Cards.T001"],
        source_provider="manifest",
        snapshot=service.catalog.snapshot(),
        results={"expect": [{"format": "runcomposer-verdicts"}], "shards": 1},
        created_at=created_at,
    )
    return service.store.create_run(spec, origin="test").id


def artifact(service: Service, run_id: str, name: str = "log.html", *, age_days: float = 0) -> Path:
    path = service.config.artifact_dir / run_id / new_ulid() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<html>shard log</html>", encoding="utf-8")
    if age_days:
        ancient = time.time() - age_days * 86400
        os.utime(path, (ancient, ancient))
        os.utime(path.parent, (ancient, ancient))
    return path


def loose_bundle(directory: Path, name: str, item: str = "Shop.Payments.Cards.T001") -> Path:
    """A results bundle with no marker — unsolicited, so it is quarantined."""
    bundle = directory / name
    bundle.mkdir(parents=True)
    (bundle / "results.json").write_text(
        json.dumps({"format": "runcomposer-verdicts",
                    "verdicts": [{"name": item, "status": "PASS", "message": name}]}),
        encoding="utf-8",
    )
    return bundle


def next_millisecond() -> None:
    """Quarantine entries are ordered by when they were received, to the
    millisecond. Wait for the clock to move on so "oldest" is well defined."""
    start = time.time_ns() // 1_000_000
    wait_until(lambda: time.time_ns() // 1_000_000 > start, timeout=5, interval=0.0005,
               what="the clock to reach the next millisecond")


# -- retention ---------------------------------------------------------------------


class TestRetention:
    def test_a_run_past_retention_is_pruned_with_everything_recorded_for_it(self, make_service):
        service = make_service(core={"retention": {"max_age_days": 30}})
        old = stored_run(service, created_at=OLD, title="old")
        run_to_end(service, old)
        fresh = compose(service, "Smoke", title="fresh")

        report = service.gc()
        assert report["runs_removed"] == [old]
        assert service.store.get_run(old) is None
        assert service.store.get_spec_document(old) is None  # cascade
        assert service.store.verdicts_for(old) == []
        assert service.store.events(old) == []
        assert service.store.get_run(fresh) is not None

    def test_max_age_days_null_does_not_raise_and_expires_nothing_by_age(self, make_service, tmp_path):
        """Regression: ``max_age_days: null`` was a TypeError in gc."""
        service = make_service(
            core={"retention": {"max_age_days": None}, "ingestion": {"inbox": "inbox"}}
        )
        ancient = stored_run(service, created_at=OLD)
        kept_artifact = artifact(service, ancient, age_days=3650)
        processed = tmp_path / "inbox" / "processed" / "bundle-from-2020"
        processed.mkdir(parents=True)
        long_ago = time.time() - 3650 * 86400
        os.utime(processed, (long_ago, long_ago))

        report = service.gc()
        assert report["runs_removed"] == []
        assert report["inbox_processed_removed"] == 0
        assert report["artifact_dirs_removed"] == 0
        assert service.store.get_run(ancient) is not None
        assert kept_artifact.exists() and processed.exists()

    def test_max_runs_still_prunes_without_an_age_limit(self, make_service):
        service = make_service(core={"retention": {"max_age_days": None, "max_runs": 2}})
        runs = [
            stored_run(service, created_at=f"2026-07-0{day}T10:00:00Z", title=f"day {day}")
            for day in (1, 2, 3, 4)
        ]
        report = service.gc()
        assert sorted(report["runs_removed"]) == sorted(runs[:2])  # the two oldest
        assert [run.id for run in service.list_runs().runs] == [runs[3], runs[2]]

    def test_the_default_keeps_ninety_days(self, service):
        recent = compose(service)
        old = stored_run(service, created_at=OLD)
        assert service.gc()["runs_removed"] == [old]
        assert service.store.get_run(recent) is not None

    def test_gc_on_an_empty_deployment_is_a_quiet_no_op(self, service):
        assert service.gc() == {
            "quarantine_removed": [],
            "inbox_processed_removed": 0,
            "artifact_dirs_removed": 0,
            "expired_dispatches": [],
            "runs_removed": [],
        }
        assert not service.config.artifact_dir.exists()  # gc creates nothing


# -- artifacts live and die with their run --------------------------------------------


class TestArtifacts:
    def test_a_pruned_runs_artifact_directory_goes_with_it(self, make_service):
        service = make_service(core={"retention": {"max_age_days": 30}})
        old = stored_run(service, created_at=OLD)
        fresh = compose(service)
        doomed, kept = artifact(service, old), artifact(service, fresh)

        report = service.gc()
        assert report["runs_removed"] == [old] and report["artifact_dirs_removed"] == 1
        assert not doomed.exists()
        assert not (service.config.artifact_dir / old).exists()  # no empty husk left
        assert kept.exists()

    def test_an_old_file_of_a_kept_run_is_not_expired(self, make_service):
        """Regression: artifacts used to expire by file mtime, so a run that
        was still listed lost its log. They now go when the run goes."""
        service = make_service(core={"retention": {"max_age_days": 30}})
        kept_run = compose(service)
        old_file = artifact(service, kept_run, "output.xml", age_days=365)
        report = service.gc()
        assert report["artifact_dirs_removed"] == 0
        assert old_file.exists()

    def test_a_directory_that_belongs_to_no_run_is_swept(self, service):
        orphan = new_ulid()  # shaped like a run id, but no such run
        leftover = artifact(service, orphan)
        kept = artifact(service, compose(service))
        report = service.gc()
        assert report["artifact_dirs_removed"] == 1
        assert not leftover.exists() and not (service.config.artifact_dir / orphan).exists()
        assert kept.exists()

    def test_what_is_not_shaped_like_a_run_id_is_left_alone(self, service):
        """The artifact directory may hold things gc knows nothing about: it
        only ever sweeps directories named like a run id."""
        root = service.config.artifact_dir
        lower_case = new_ulid().lower()  # run ids are upper case; this is somebody else's
        left_alone = ["shared-screenshots", "run-x", "01SHORT", lower_case]
        for name in left_alone:
            (root / name).mkdir(parents=True)
            (root / name / "keep.txt").write_text("not ours", encoding="utf-8")
        (root / "README.txt").write_text("a file, not a directory", encoding="utf-8")
        file_named_like_a_run = root / new_ulid()
        file_named_like_a_run.write_text("a file", encoding="utf-8")

        assert service.gc()["artifact_dirs_removed"] == 0
        assert sorted(p.name for p in root.iterdir()) == sorted(
            [*left_alone, "README.txt", file_named_like_a_run.name]
        )
        assert (root / "shared-screenshots" / "keep.txt").exists()

    def test_delete_run_removes_its_artifacts(self, service):
        doomed, kept_run = compose(service), compose(service)
        gone, kept = artifact(service, doomed), artifact(service, kept_run)
        service.delete_run(doomed)
        assert service.store.get_run(doomed) is None
        assert not gone.exists() and not (service.config.artifact_dir / doomed).exists()
        assert kept.exists()

    def test_delete_run_is_refused_while_something_is_executing_it(self, service):
        run_id = compose(service)
        service.store.add_dispatch(
            run_id, dispatch_id=new_ulid(), mode="demo", state="RUNNING",
            owner=service.dispatcher.owner,
        )
        log = artifact(service, run_id)
        with pytest.raises(RunActive, match="cancel it before deleting"):
            service.delete_run(run_id)
        assert service.store.get_run(run_id) is not None and log.exists()

    def test_remove_run_artifacts_only_touches_a_run_directory(self, tmp_path):
        root = tmp_path / "artifacts"
        run_id = new_ulid()
        (root / run_id / "d").mkdir(parents=True)
        (root / "elsewhere").mkdir()
        assert remove_run_artifacts(root, run_id) is True
        assert not (root / run_id).exists()
        assert remove_run_artifacts(root, run_id) is False  # already gone
        assert remove_run_artifacts(root, "elsewhere") is False  # not a run id
        assert remove_run_artifacts(root, "../" + root.name) is False  # nor a way out
        assert (root / "elsewhere").exists() and root.exists()


# -- the inbox's processed bundles ------------------------------------------------------


class TestProcessedInboxBundles:
    def test_processed_bundles_expire_with_the_retention_age(self, make_service, tmp_path):
        service = make_service(
            core={"retention": {"max_age_days": 30}, "ingestion": {"inbox": "inbox"}}
        )
        processed = tmp_path / "inbox" / "processed"
        old, recent = processed / "nightly-old", processed / "nightly-recent"
        for directory in (old, recent):
            directory.mkdir(parents=True)
            (directory / "results.json").write_text("{}", encoding="utf-8")
        ancient = time.time() - 90 * 86400
        os.utime(old, (ancient, ancient))
        waiting = tmp_path / "inbox" / "still-arriving"  # not processed yet: never gc's business
        waiting.mkdir()
        os.utime(waiting, (ancient, ancient))

        report = service.gc()
        assert report["inbox_processed_removed"] == 1
        assert not old.exists() and recent.exists() and waiting.exists()

    def test_no_inbox_configured_is_fine(self, make_service):
        service = make_service(core={"ingestion": {"inbox": None}})
        assert service.gc()["inbox_processed_removed"] == 0


# -- the quarantine is bounded on write ---------------------------------------------------


class TestQuarantineBound:
    """Regression: ``quarantine_max`` was enforced only by a manual gc, so
    anything that could reach the quarantine could fill the disk."""

    def test_adding_beyond_the_bound_evicts_the_oldest(self, make_service, tmp_path):
        service = make_service(core={"ingestion": {"quarantine_max": 2}})
        added = []
        for n in range(4):
            report = service.ingest_or_quarantine(
                loose_bundle(tmp_path / "drops", f"loose-{n}"), transport="file-drop"
            )
            assert report.reason == "unsolicited" and report.entry_id
            added.append(report.entry_id)
            assert len(service.quarantine.entries()) == min(n + 1, 2)  # never above the bound
            next_millisecond()
        assert [entry.entry_id for entry in service.quarantine.entries()] == added[2:]
        on_disk = sorted(p.name for p in service.config.quarantine_dir.iterdir())
        assert on_disk == sorted(added[2:])  # the evicted bundles are gone from disk too

    def test_gc_has_nothing_left_to_remove(self, make_service, tmp_path):
        service = make_service(core={"ingestion": {"quarantine_max": 2}})
        for n in range(3):
            service.ingest_or_quarantine(
                loose_bundle(tmp_path / "drops", f"loose-{n}"), transport="file-drop"
            )
            next_millisecond()
        assert service.gc()["quarantine_removed"] == []
        assert len(service.quarantine.entries()) == 2

    def test_the_same_bundle_again_does_not_multiply_entries(self, service, tmp_path):
        """Re-drops and transport retries redeliver in practice."""
        bundle = loose_bundle(tmp_path / "drops", "loose")
        first = service.ingest_or_quarantine(bundle, transport="file-drop")
        again = service.ingest_or_quarantine(bundle, transport="push")
        assert first.entry_id is not None
        assert again.entry_id is None and again.reason == "unsolicited"
        assert [entry.entry_id for entry in service.quarantine.entries()] == [first.entry_id]

    def test_the_default_bound_is_one_hundred(self, service):
        assert service.config.ingestion["quarantine_max"] == 100
        assert service.quarantine._max == 100


class TestQuarantine:
    """The directory itself, without a service."""

    def add(self, quarantine: Quarantine, bundle: Path, **fields: Any):
        fields.setdefault("reason", "unsolicited")
        fields.setdefault("transport", "file-drop")
        return quarantine.add(bundle, content_hash=bundle_hash(bundle), **fields)

    def test_an_entry_keeps_a_copy_of_the_bundle_and_says_why(self, tmp_path):
        quarantine = Quarantine(tmp_path / "q")
        bundle = loose_bundle(tmp_path, "drop")
        entry = self.add(
            quarantine, bundle, reason="sha-mismatch", transport="push",
            claimed_run_id="01CLAIMEDRUN00000000000000", format="runcomposer-verdicts",
            detail="marker spec_sha256 does not match",
        )
        assert entry is not None and entry.entry_id.startswith("q-")
        assert quarantine.get(entry.entry_id) == entry
        assert (entry.reason, entry.transport) == ("sha-mismatch", "push")
        assert entry.claimed_run_id == "01CLAIMEDRUN00000000000000"
        assert entry.format == "runcomposer-verdicts"
        assert entry.detail == "marker spec_sha256 does not match"
        copy = quarantine.bundle_path(entry.entry_id)
        assert bundle_hash(copy) == entry.content_hash == bundle_hash(bundle)
        assert quarantine.directory == tmp_path / "q"

    def test_a_single_file_is_quarantined_as_a_bundle_of_one(self, tmp_path):
        quarantine = Quarantine(tmp_path / "q")
        file = tmp_path / "junit.xml"
        file.write_text("<testsuite/>", encoding="utf-8")
        entry = self.add(quarantine, file)
        assert [p.name for p in quarantine.bundle_path(entry.entry_id).iterdir()] == ["junit.xml"]

    def test_the_format_is_unknown_unless_something_said(self, tmp_path):
        """A file drop for an unknown run does not know its format; the entry
        says so instead of claiming the reference format (promoting another
        format used to fail for exactly that reason)."""
        quarantine = Quarantine(tmp_path / "q")
        assert self.add(quarantine, loose_bundle(tmp_path, "drop")).format is None

    def test_a_long_detail_is_bounded(self, tmp_path):
        quarantine = Quarantine(tmp_path / "q")
        entry = self.add(quarantine, loose_bundle(tmp_path, "drop"), detail="x" * 5000)
        assert len(entry.detail) == 500

    def test_the_bound_holds_on_every_write(self, tmp_path):
        quarantine = Quarantine(tmp_path / "q", max_entries=3)
        ids = []
        for n in range(6):
            ids.append(self.add(quarantine, loose_bundle(tmp_path / "drops", f"b{n}")).entry_id)
            assert len(quarantine.entries()) <= 3
            next_millisecond()
        assert [entry.entry_id for entry in quarantine.entries()] == ids[3:]

    def test_a_bound_below_one_still_keeps_the_newest(self, tmp_path):
        quarantine = Quarantine(tmp_path / "q", max_entries=0)
        first = self.add(quarantine, loose_bundle(tmp_path / "drops", "b0"))
        next_millisecond()
        second = self.add(quarantine, loose_bundle(tmp_path / "drops", "b1"))
        assert [entry.entry_id for entry in quarantine.entries()] == [second.entry_id]
        assert first.entry_id != second.entry_id

    def test_dedup_is_by_content_not_by_name(self, tmp_path):
        quarantine = Quarantine(tmp_path / "q")
        first = self.add(quarantine, loose_bundle(tmp_path / "a", "drop"))
        assert self.add(quarantine, loose_bundle(tmp_path / "b", "drop")) is None  # same bytes
        other = self.add(quarantine, loose_bundle(tmp_path / "c", "another-drop"))
        assert other is not None and other.entry_id != first.entry_id
        assert len(quarantine.entries()) == 2

    def test_unknown_entries_are_not_found(self, tmp_path):
        quarantine = Quarantine(tmp_path / "q")
        assert issubclass(QuarantineError, NotFound)
        assert QuarantineError.code == "unknown-quarantine-entry"
        for action in (quarantine.get, quarantine.bundle_path, quarantine.remove):
            with pytest.raises(QuarantineError, match="unknown quarantine entry 'q-nope'"):
                action("q-nope")
        assert quarantine.entries() == []  # and nothing was created by asking

    def test_remove_discards_the_entry_and_its_bundle(self, tmp_path):
        quarantine = Quarantine(tmp_path / "q")
        entry = self.add(quarantine, loose_bundle(tmp_path, "drop"))
        quarantine.remove(entry.entry_id)
        assert quarantine.entries() == [] and not (tmp_path / "q" / entry.entry_id).exists()

    def test_prune_enforces_a_tighter_bound_and_collects_half_written_entries(self, tmp_path):
        quarantine = Quarantine(tmp_path / "q", max_entries=10)
        ids = []
        for n in range(4):
            ids.append(self.add(quarantine, loose_bundle(tmp_path / "drops", f"b{n}")).entry_id)
            next_millisecond()
        husk = tmp_path / "q" / "q-HALFWRITTEN"  # a copy that never got its metadata
        (husk / "bundle").mkdir(parents=True)
        assert len(quarantine.entries()) == 4  # the husk is not an entry…

        assert quarantine.prune() == []  # within the bound: nothing evicted…
        assert not husk.exists()  # …but the husk is collected
        assert quarantine.prune(2) == ids[:2]
        assert [entry.entry_id for entry in quarantine.entries()] == ids[2:]

    def test_entries_survive_a_new_quarantine_object_on_the_same_directory(self, tmp_path):
        entry = self.add(Quarantine(tmp_path / "q"), loose_bundle(tmp_path, "drop"))
        assert Quarantine(tmp_path / "q").entries() == [entry]


# -- the command ---------------------------------------------------------------------------


class TestGcCommand:
    def test_it_reports_what_it_did(self, make_service, tmp_path, capsys):
        service = make_service(core={"retention": {"max_age_days": 30}})
        old = stored_run(service, created_at=OLD)
        artifact(service, old)
        config = str(tmp_path / "config.yaml")
        assert cli_main(["gc", "--config", config]) == 0
        out = capsys.readouterr().out
        assert "quarantine: removed 0" in out
        assert "store: pruned 1 run(s) past retention" in out
        assert "artifacts: removed 1 run director" in out
        assert service.store.get_run(old) is None

    def test_json_output_is_the_report(self, make_service, tmp_path, capsys):
        service = make_service(core={"retention": {"max_age_days": None}})
        stored_run(service, created_at=OLD)
        assert cli_main(["gc", "--json", "--config", str(tmp_path / "config.yaml")]) == 0
        assert json.loads(capsys.readouterr().out) == {
            "quarantine_removed": [],
            "inbox_processed_removed": 0,
            "artifact_dirs_removed": 0,
            "expired_dispatches": [],
            "runs_removed": [],
        }
