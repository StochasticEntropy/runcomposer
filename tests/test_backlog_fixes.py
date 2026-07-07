"""Regression tests for the post-P3 backlog: CI-path marker verification,
empty-history messaging, gc directory cleanup, push format check."""

import hashlib
import io
import json
import urllib.parse

import pytest

from runcomposer.config import Config
from runcomposer.core.model import Verdict
from runcomposer.core.spec import build_spec
from runcomposer.plugins.ci_trigger import CiTriggerRunner
from runcomposer.service import Service, ServiceError


@pytest.fixture()
def service(tmp_path):
    return Service(Config(data={
        "store": {"sqlite": {"path": str(tmp_path / "b.db")}},
        "core": {
            "artifact_dir": str(tmp_path / "artifacts"),
            "ingestion": {"inbox": None, "quarantine_dir": str(tmp_path / "q")},
        },
        "runners": {"ci-trigger": {"base_url": "http://ci.local", "job": "consumer",
                                   "callback_base": "http://cb.local"}},
    }))


def test_ci_trigger_dispatch_records_spec_sha256(service, monkeypatch):
    """§5: the marker hash of the spec the consumer receives must be
    verifiable — the dispatch now records the SPEC_JSON hash."""
    captured = {}

    def fake_open(self, request, timeout=30):
        url = request.full_url
        if "/crumbIssuer/" in url:
            raise urllib.error.HTTPError(url, 404, "none", {}, None)
        captured.update(
            dict(pair.split("=", 1) for pair in request.data.decode().split("&"))
        )
        response = io.BytesIO(b"")
        response.headers = {"Location": None}
        response.__enter__ = lambda *a: response
        response.__exit__ = lambda *a: False
        return response

    import urllib.error

    monkeypatch.setattr(CiTriggerRunner, "_open", fake_open)
    result = service.compose_run({"tag_filter": "Smoke"}, title="sha", origin="test")
    dispatch = service.dispatch_runner(result.run.id, "ci-trigger")
    spec_json = urllib.parse.unquote_plus(captured["SPEC_JSON"])
    assert dispatch.spec_sha256 == hashlib.sha256(spec_json.encode()).hexdigest()
    stored = service.store.get_run(result.run.id).dispatches[-1]
    assert stored.spec_sha256 == dispatch.spec_sha256


def test_history_with_zero_matches_says_nothing_to_rerun(service):
    spec = build_spec(title="all green", tag_filter="Smoke",
                      materialized_ids=["Shop.Payments.Cards.T001"],
                      source_provider="manifest", snapshot="sha256:" + "ab" * 32)
    run = service.store.create_run(spec, origin="test")
    service.store.add_dispatch(run.id, dispatch_id="D1", mode="export", declared_shards=1)
    service.store.record_delivery(run.id, dispatch_id="D1", shard="1",
                                  content_hash="sha256:x", format="runcomposer-verdicts",
                                  verdicts=[Verdict("Shop.Payments.Cards.T001", "PASS")])
    service.store.set_run_state(run.id, "COMPLETE", completion="PASS",
                                completed_at="2026-07-07T10:00:00Z")
    with pytest.raises(ServiceError, match="nothing to rerun"):
        service.compose_run({"history": "failed@latest"}, title="rerun", origin="test")


def test_gc_removes_emptied_artifact_directories(service, tmp_path):
    import os
    import time

    nested = tmp_path / "artifacts" / "run-x" / "dispatch-y" / "shard-1"
    nested.mkdir(parents=True)
    stale = nested / "output.xml"
    stale.write_text("old", encoding="utf-8")
    ancient = time.time() - 365 * 86400
    os.utime(stale, (ancient, ancient))
    report = service.gc()
    assert report["artifacts_removed"] == 1
    assert not (tmp_path / "artifacts" / "run-x").exists()  # no empty husks left


def test_push_declared_format_mismatch_is_400(service, tmp_path):
    from fastapi.testclient import TestClient

    from runcomposer.api import create_app

    client = TestClient(create_app(service.config))
    result = service.compose_run({"tag_filter": "Smoke"}, title="fmt", origin="test")
    token = result.spec["results"]["token"]
    response = client.post(
        f"/api/v1/runs/{result.run.id}/results",
        files=[("files", ("results.json", io.BytesIO(b"{}"), "application/json"))],
        data={"format": "robot-output-xml"},  # spec expects runcomposer-verdicts
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400
    assert "does not match the run's expected results format" in response.json()["detail"]
    assert service.store.verdicts_for(result.run.id) == []
