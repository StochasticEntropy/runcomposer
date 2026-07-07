"""Ingestion push API tests (DESIGN.md §5 transport 1, §9): token floor,
size limit, §5 delivery semantics over HTTP, quarantine routing."""

import io
import json

import pytest
from fastapi.testclient import TestClient

from runcomposer.api import create_app
from runcomposer.config import Config
from runcomposer.service import Service


def make_config(tmp_path, **ingestion):
    return Config(
        data={
            "store": {"sqlite": {"path": str(tmp_path / "push.db")}},
            "core": {
                "ingestion": {
                    "inbox": None,  # no watcher in these tests
                    "quarantine_dir": str(tmp_path / "quarantine"),
                    **ingestion,
                }
            },
        }
    )


@pytest.fixture()
def config(tmp_path):
    return make_config(tmp_path)


@pytest.fixture()
def service(config):
    return Service(config)


@pytest.fixture()
def client(config, service):
    app = create_app(config)
    app.state._service_for_tests = service  # noqa: SLF001 - test-only handle
    return TestClient(app)


def compose(service, tag_filter="Smoke"):
    result = service.compose_run({"tag_filter": tag_filter}, title="Push target", origin="test")
    return result.run.id, result.spec["results"].get("token"), [i.id for i in result.items]


def verdicts_file(item_ids, status="PASS", name="results.json"):
    payload = {
        "format": "runcomposer-verdicts",
        "verdicts": [{"name": item_id, "status": status} for item_id in item_ids],
    }
    return ("files", (name, io.BytesIO(json.dumps(payload).encode()), "application/json"))


class TestTokenFloor:
    def test_missing_token_is_401_and_nothing_lands(self, client, service):
        run_id, _token, items = compose(service)
        response = client.post(f"/api/v1/runs/{run_id}/results", files=[verdicts_file(items)])
        assert response.status_code == 401
        assert service.store.verdicts_for(run_id) == []
        assert service.store.get_run(run_id).state == "COMPOSED"

    def test_wrong_token_is_403_and_nothing_lands(self, client, service):
        run_id, _token, items = compose(service)
        response = client.post(
            f"/api/v1/runs/{run_id}/results",
            files=[verdicts_file(items)],
            headers={"Authorization": "Bearer rct_wrong"},
        )
        assert response.status_code == 403
        assert service.store.verdicts_for(run_id) == []

    def test_correct_token_ingests_and_completes(self, client, service):
        run_id, token, items = compose(service)
        response = client.post(
            f"/api/v1/runs/{run_id}/results",
            files=[verdicts_file(items)],
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["outcome"] == "new" and body["run_state"] == "COMPLETE"
        assert service.store.get_run(run_id).completion == "PASS"

    def test_x_header_token_also_accepted(self, client, service):
        run_id, token, items = compose(service)
        response = client.post(
            f"/api/v1/runs/{run_id}/results",
            files=[verdicts_file(items)],
            headers={"X-Runcomposer-Token": token},
        )
        assert response.status_code == 200

    def test_disabled_tokens_skip_the_check(self, tmp_path):
        config = make_config(tmp_path, tokens="disabled")
        service = Service(config)
        client = TestClient(create_app(config))
        run_id, token, items = compose(service)
        assert token is None or "token" not in service.store.get_spec_document(run_id)["results"]
        response = client.post(f"/api/v1/runs/{run_id}/results", files=[verdicts_file(items)])
        assert response.status_code == 200


class TestUploadLimits:
    def test_oversized_upload_is_rejected_with_clear_error(self, tmp_path):
        config = make_config(tmp_path, max_upload_mb=1)
        service = Service(config)
        client = TestClient(create_app(config))
        run_id, token, _items = compose(service)
        blob = io.BytesIO(b"x" * (1024 * 1024 + 1))
        response = client.post(
            f"/api/v1/runs/{run_id}/results",
            files=[("files", ("huge.json", blob, "application/json"))],
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 413
        assert "max_upload_mb" in response.json()["detail"]
        assert service.store.verdicts_for(run_id) == []


class TestPushDeliverySemantics:
    """§5: same semantics as CLI ingest, over HTTP."""

    def test_byte_identical_reupload_is_noop(self, client, service):
        run_id, token, items = compose(service)
        headers = {"Authorization": f"Bearer {token}"}
        first = client.post(f"/api/v1/runs/{run_id}/results", files=[verdicts_file(items)], headers=headers)
        again = client.post(f"/api/v1/runs/{run_id}/results", files=[verdicts_file(items)], headers=headers)
        assert first.json()["outcome"] == "new"
        assert again.json()["outcome"] == "duplicate"
        assert len(service.store.get_run(run_id).deliveries) == 1

    def test_new_bundle_for_same_shard_replaces_fail_flips_to_pass(self, client, service):
        run_id, token, items = compose(service)
        headers = {"Authorization": f"Bearer {token}"}
        client.post(f"/api/v1/runs/{run_id}/results", files=[verdicts_file(items, "FAIL")], headers=headers)
        assert service.store.get_run(run_id).completion == "FAIL"
        replaced = client.post(
            f"/api/v1/runs/{run_id}/results", files=[verdicts_file(items, "PASS")], headers=headers
        )
        assert replaced.json()["outcome"] == "replaced"
        run = service.store.get_run(run_id)
        assert run.completion == "PASS"  # no monotonic merge
        assert len(run.deliveries) == 1


class TestPushQuarantineRouting:
    def test_unknown_run_id_is_quarantined_not_created(self, client, service):
        response = client.post(
            "/api/v1/runs/01NOSUCHRUN00000000000000A/results",
            files=[verdicts_file(["Shop.Payments.Cards.T001"])],
        )
        assert response.status_code == 404
        entry_id = response.json()["quarantine_entry"]
        assert entry_id and entry_id.startswith("q-")
        entries = service.quarantine.entries()
        assert [e.entry_id for e in entries] == [entry_id]
        assert entries[0].reason == "unknown-run"
        assert service.store.list_runs() == []  # nothing created

    def test_marker_conflict_is_quarantined_with_409(self, client, service):
        run_id, token, items = compose(service)
        marker = json.dumps({"run_id": "01SOMEOTHERRUN00000000000A", "spec_sha256": "0" * 64})
        response = client.post(
            f"/api/v1/runs/{run_id}/results",
            files=[
                verdicts_file(items),
                ("files", ("runcomposer_run.json", io.BytesIO(marker.encode()), "application/json")),
            ],
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 409
        assert response.json()["reason"] == "marker-conflict"
        assert service.store.verdicts_for(run_id) == []
        assert len(service.quarantine.entries()) == 1
