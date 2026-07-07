"""File-drop inbox + quarantine + gc tests (DESIGN.md §4, §5, §6.4)."""

import json
import os
import time
from pathlib import Path

import pytest

import runcomposer_exec
from runcomposer.cli import main as cli_main
from runcomposer.config import Config, load_config
from runcomposer.inbox import InboxWatcher
from runcomposer.quarantine import Quarantine, QuarantineError
from runcomposer.service import IngestReport, QuarantineReport, Service


@pytest.fixture()
def config(tmp_path):
    return Config(
        data={
            "store": {"sqlite": {"path": str(tmp_path / "inbox.db")}},
            "core": {
                "ingestion": {
                    "inbox": str(tmp_path / "results_inbox"),
                    "quarantine_dir": str(tmp_path / "quarantine"),
                    "quarantine_max": 2,
                    "poll_interval_s": 0.05,
                }
            },
        }
    )


@pytest.fixture()
def service(config):
    return Service(config)


@pytest.fixture()
def watcher(service, config):
    return InboxWatcher(service, config.inbox_dir, poll_interval_s=0.05)


def exported_bundle(service, tmp_path, name="bundle", seed="s1"):
    """Compose + export a spec, execute it, return (run_id, bundle_path)."""
    result = service.compose_run({"tag_filter": "Smoke"}, title="Inbox run", origin="test")
    spec_path = tmp_path / f"{name}-spec.json"
    spec_path.write_text(json.dumps(service.store.get_spec_document(result.run.id)), encoding="utf-8")
    service.export_dispatch(result.run.id, spec_bytes=spec_path.read_bytes())
    bundle = tmp_path / name
    runcomposer_exec.main([str(spec_path), "--out", str(bundle), "--simulate", "--seed", seed])
    return result.run.id, bundle


def drop(inbox: Path, bundle: Path, name: str) -> Path:
    import shutil

    inbox.mkdir(parents=True, exist_ok=True)
    target = inbox / name
    shutil.copytree(bundle, target)
    old = time.time() - 60  # pretend it settled long ago
    os.utime(target, (old, old))
    return target


class TestInboxWatcher:
    def test_dropped_bundle_is_ingested_to_complete(self, service, watcher, config, tmp_path):
        run_id, bundle = exported_bundle(service, tmp_path)
        drop(config.inbox_dir, bundle, "drop-1")
        results = watcher.poll_once()
        assert len(results) == 1 and isinstance(results[0], IngestReport)
        assert results[0].outcome == "new"
        assert service.store.get_run(run_id).state == "COMPLETE"

    def test_processed_bundle_is_archived_not_reprocessed(self, service, watcher, config, tmp_path):
        _run_id, bundle = exported_bundle(service, tmp_path)
        drop(config.inbox_dir, bundle, "drop-1")
        watcher.poll_once()
        assert not (config.inbox_dir / "drop-1").exists()
        archived = list((config.inbox_dir / "processed").iterdir())
        assert len(archived) == 1 and archived[0].name.startswith("drop-1-")
        assert watcher.poll_once() == []  # nothing left to do

    def test_redropped_identical_bundle_is_noop(self, service, watcher, config, tmp_path):
        run_id, bundle = exported_bundle(service, tmp_path)
        drop(config.inbox_dir, bundle, "drop-1")
        watcher.poll_once()
        drop(config.inbox_dir, bundle, "drop-again")
        results = watcher.poll_once()
        assert results[0].outcome == "duplicate"
        assert len(service.store.get_run(run_id).deliveries) == 1

    def test_fresh_entries_wait_one_cycle(self, service, watcher, config, tmp_path):
        _run_id, bundle = exported_bundle(service, tmp_path)
        import shutil

        config.inbox_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bundle, config.inbox_dir / "hot")  # fresh mtime
        assert watcher.poll_once(min_age_s=60) == []
        assert (config.inbox_dir / "hot").exists()

    def test_background_thread_ingests(self, service, config, tmp_path):
        run_id, bundle = exported_bundle(service, tmp_path)
        watcher = InboxWatcher(service, config.inbox_dir, poll_interval_s=0.05)
        watcher.start()
        try:
            drop(config.inbox_dir, bundle, "threaded")
            deadline = time.time() + 5
            while time.time() < deadline:
                if service.store.get_run(run_id).state == "COMPLETE":
                    break
                time.sleep(0.05)
            assert service.store.get_run(run_id).state == "COMPLETE"
        finally:
            watcher.stop()


class TestQuarantineRouting:
    def test_markerless_drop_goes_to_quarantine_not_a_run(self, service, watcher, config, tmp_path):
        loose = tmp_path / "loose"
        loose.mkdir()
        (loose / "results.json").write_text(
            json.dumps({"format": "runcomposer-verdicts",
                        "verdicts": [{"name": "Shop.Payments.Cards.T001", "status": "PASS"}]}),
            encoding="utf-8",
        )
        drop(config.inbox_dir, loose, "unsolicited")
        results = watcher.poll_once()
        assert isinstance(results[0], QuarantineReport)
        assert results[0].reason == "unsolicited"
        assert service.store.list_runs() == []
        assert len(service.quarantine.entries()) == 1

    def test_sha_mismatch_drop_goes_to_quarantine(self, service, watcher, config, tmp_path):
        run_id, bundle = exported_bundle(service, tmp_path)
        marker_path = bundle / "runcomposer_run.json"
        marker = json.loads(marker_path.read_text())
        marker["spec_sha256"] = "0" * 64
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        drop(config.inbox_dir, bundle, "tampered")
        results = watcher.poll_once()
        assert isinstance(results[0], QuarantineReport) and results[0].reason == "sha-mismatch"
        assert service.store.verdicts_for(run_id) == []
        assert service.store.get_run(run_id).state == "AWAITING_RESULTS"

    def test_identical_quarantined_bundle_is_deduplicated(self, service, watcher, config, tmp_path):
        loose = tmp_path / "loose"
        loose.mkdir()
        (loose / "results.json").write_text(
            json.dumps({"format": "runcomposer-verdicts",
                        "verdicts": [{"name": "Shop.Payments.Cards.T001", "status": "PASS"}]}),
            encoding="utf-8",
        )
        drop(config.inbox_dir, loose, "first")
        drop(config.inbox_dir, loose, "second")
        results = watcher.poll_once()
        assert [type(r) for r in results] == [QuarantineReport, QuarantineReport]
        assert results[1].entry_id is None  # deduplicated
        assert len(service.quarantine.entries()) == 1


class TestAttachPromote:
    def _quarantined_entry(self, service, watcher, config, tmp_path, run_id, bundle):
        marker_path = bundle / "runcomposer_run.json"
        marker = json.loads(marker_path.read_text())
        marker["spec_sha256"] = "0" * 64
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        drop(config.inbox_dir, bundle, "for-attach")
        watcher.poll_once()
        return service.quarantine.entries()[0]

    def test_attach_binds_to_existing_run(self, service, watcher, config, tmp_path):
        run_id, bundle = exported_bundle(service, tmp_path)
        entry = self._quarantined_entry(service, watcher, config, tmp_path, run_id, bundle)
        report = service.attach_quarantined(entry.entry_id, run_id)
        assert report.outcome == "new" and report.run_state == "COMPLETE"
        assert service.quarantine.entries() == []

    def test_promote_creates_origin_ingested_run(self, service, watcher, config, tmp_path):
        run_id, bundle = exported_bundle(service, tmp_path)
        entry = self._quarantined_entry(service, watcher, config, tmp_path, run_id, bundle)
        report = service.promote_quarantined(entry.entry_id)
        assert report.run_id != run_id
        promoted = service.store.get_run(report.run_id)
        assert promoted.origin == "ingested"
        assert promoted.state == "COMPLETE"
        assert service.quarantine.entries() == []

    def test_cli_allow_unsolicited_promotes(self, tmp_path, capsys):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            f"store:\n  sqlite: {{ path: {tmp_path / 'cli.db'} }}\n"
            f"core:\n  ingestion: {{ quarantine_dir: {tmp_path / 'q'} }}\n",
            encoding="utf-8",
        )
        loose = tmp_path / "loose"
        loose.mkdir()
        (loose / "results.json").write_text(
            json.dumps({"format": "runcomposer-verdicts",
                        "verdicts": [{"name": "Shop.Auth.Login.T001", "status": "PASS"}]}),
            encoding="utf-8",
        )
        assert cli_main(["ingest", str(loose), "--config", str(config_file)]) == 1
        capsys.readouterr()
        assert cli_main(["ingest", str(loose), "--allow-unsolicited", "--config", str(config_file)]) == 0
        out = capsys.readouterr().out
        assert "promoted" in out and "origin: ingested" in out


class TestGc:
    def test_quarantine_bound_enforced(self, service, config, tmp_path):
        for n in range(3):  # quarantine_max is 2 in this config
            loose = tmp_path / f"loose-{n}"
            loose.mkdir()
            (loose / "results.json").write_text(
                json.dumps({"format": "runcomposer-verdicts",
                            "verdicts": [{"name": f"item-{n}", "status": "PASS"}]}),
                encoding="utf-8",
            )
            service.ingest_or_quarantine(loose, transport="file-drop")
        assert len(service.quarantine.entries()) == 3
        report = service.gc()
        assert len(report["quarantine_removed"]) == 1
        remaining = service.quarantine.entries()
        assert len(remaining) == 2
        # the OLDEST entry was removed
        assert report["quarantine_removed"][0] < remaining[0].entry_id

    def test_gc_prunes_expired_runs_and_artifacts(self, tmp_path):
        config = Config(
            data={
                "store": {"sqlite": {"path": str(tmp_path / "gc.db")}},
                "core": {
                    "artifact_dir": str(tmp_path / "artifacts"),
                    "retention": {"max_age_days": 30},
                    "ingestion": {"quarantine_dir": str(tmp_path / "q"), "inbox": None},
                },
            }
        )
        service = Service(config)
        old = service.compose_run({"tag_filter": "Smoke"}, title="old", origin="test")
        # backdate the run past retention
        import sqlite3

        conn = sqlite3.connect(tmp_path / "gc.db")
        conn.execute("UPDATE runs SET created_at = '2020-01-01T00:00:00Z' WHERE id = ?", (old.run.id,))
        conn.commit()
        conn.close()
        fresh = service.compose_run({"tag_filter": "Smoke"}, title="fresh", origin="test")
        artifact = tmp_path / "artifacts" / "stale.log"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("old artifact", encoding="utf-8")
        ancient = time.time() - 90 * 86400
        os.utime(artifact, (ancient, ancient))

        report = service.gc()
        assert report["runs_removed"] == [old.run.id]
        assert report["artifacts_removed"] == 1
        assert service.store.get_run(old.run.id) is None
        assert service.store.get_spec_document(old.run.id) is None  # cascade
        assert service.store.get_run(fresh.run.id) is not None

    def test_gc_cli_reports(self, tmp_path, capsys):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            f"store:\n  sqlite: {{ path: {tmp_path / 'gc.db'} }}\n"
            f"core:\n  ingestion: {{ quarantine_dir: {tmp_path / 'q'}, inbox: null }}\n",
            encoding="utf-8",
        )
        assert cli_main(["gc", "--config", str(config_file)]) == 0
        out = capsys.readouterr().out
        assert "quarantine:" in out and "store:" in out


class TestQuarantineApi:
    def test_list_attach_promote_endpoints(self, config, service, tmp_path):
        from fastapi.testclient import TestClient

        from runcomposer.api import create_app

        watcher = InboxWatcher(service, config.inbox_dir, poll_interval_s=0.05)
        client = TestClient(create_app(config))
        run_id, bundle = exported_bundle(service, tmp_path)
        marker_path = bundle / "runcomposer_run.json"
        marker = json.loads(marker_path.read_text())
        marker["spec_sha256"] = "0" * 64
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        drop(config.inbox_dir, bundle, "api-entry")
        watcher.poll_once()

        entries = client.get("/api/v1/quarantine").json()["entries"]
        assert len(entries) == 1 and entries[0]["reason"] == "sha-mismatch"
        entry_id = entries[0]["entry_id"]

        attached = client.post(f"/api/v1/quarantine/{entry_id}/attach", json={"run_id": run_id})
        assert attached.status_code == 200
        assert attached.json()["run_state"] == "COMPLETE"
        assert client.get("/api/v1/quarantine").json()["entries"] == []
        assert client.post(f"/api/v1/quarantine/{entry_id}/promote").status_code == 404

    def test_attach_requires_run_id(self, config, service):
        from fastapi.testclient import TestClient

        from runcomposer.api import create_app

        client = TestClient(create_app(config))
        assert client.post("/api/v1/quarantine/q-x/attach", json={}).status_code == 400
