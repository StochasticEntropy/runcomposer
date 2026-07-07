"""Service + HTTP API tests (DESIGN.md §4 lifecycle, §9 surface)."""

import pytest
from fastapi.testclient import TestClient

from runcomposer.api import create_app
from runcomposer.config import Config
from runcomposer.service import Service

FILTER = {"op": "AND", "items": ["Payments", {"not": "prefix:Quarantine-"}]}


@pytest.fixture()
def config(tmp_path):
    return Config(data={"store": {"sqlite": {"path": str(tmp_path / "svc.db")}}})


@pytest.fixture()
def service(config):
    return Service(config)


@pytest.fixture()
def client(config):
    return TestClient(create_app(config))


class TestServiceLifecycle:
    def test_compose_run_stores_composed_state_and_token(self, service):
        result = service.compose_run({"tag_filter": FILTER}, title="T", origin="test")
        assert result.run.state == "COMPOSED"
        assert result.spec["results"]["token"].startswith("rct_")
        stored = service.store.get_spec_document(result.run.id)
        assert stored["selection"]["materialized"]["item_ids"] == [i.id for i in result.items]

    def test_export_dispatch_moves_to_awaiting_results(self, service):
        result = service.compose_run({"tag_filter": FILTER}, title="T", origin="test")
        dispatch = service.export_dispatch(result.run.id, spec_bytes=b"exported-bytes")
        assert dispatch.mode == "export"
        assert dispatch.declared_shards == 1
        assert len(dispatch.spec_sha256) == 64
        assert service.store.get_run(result.run.id).state == "AWAITING_RESULTS"

    def test_demo_runner_dispatch_completes_the_run(self, service):
        result = service.compose_run({"tag_filter": FILTER}, title="T", origin="test")
        dispatch = service.dispatch_runner(result.run.id, "demo")
        run = service.store.get_run(result.run.id)
        assert run.state == "COMPLETE"
        assert run.completion in ("PASS", "FAIL", "ERROR")
        assert run.completed_at is not None
        assert len(service.store.verdicts_for(run.id, dispatch.dispatch_id)) == len(result.items)

    def test_finalize_completes_an_awaiting_run(self, service):
        result = service.compose_run({"tag_filter": FILTER}, title="T", origin="test")
        service.export_dispatch(result.run.id, spec_bytes=b"x")
        run = service.finalize(result.run.id)
        assert run.state == "COMPLETE"
        assert run.completion == "PASS"  # §4: explicit finalize; no verdicts = nothing failed


class TestApiSurface:
    def test_health(self, client):
        body = client.get("/api/v1/health").json()
        assert body["status"] == "ok"

    def test_taxonomy_is_served(self, client):
        body = client.get("/api/v1/taxonomy").json()
        assert body["taxonomy"][0]["label"] == "Areas"

    def test_ui_config_carries_locale_default(self, client):
        assert client.get("/api/v1/ui-config").json()["locale_default"] == "en"

    def test_runners_registry(self, client):
        runners = client.get("/api/v1/runners").json()
        assert {"id": "demo", "capabilities": ["fake-execution"]} in runners

    def test_selection_compile_previews(self, client):
        response = client.post("/api/v1/selection/compile", json={"tag_filter": FILTER})
        body = response.json()
        assert response.status_code == 200
        assert body["count"] == len(body["items"]) > 0
        assert body["warnings"] == []

    def test_selection_compile_rejects_bad_filter(self, client):
        response = client.post(
            "/api/v1/selection/compile", json={"tag_filter": {"op": "XOR", "items": ["x"]}}
        )
        assert response.status_code == 400
        assert "XOR" in response.json()["detail"]

    def test_spec_preview_renders_but_does_not_store(self, client):
        response = client.post(
            "/api/v1/selection/spec-preview",
            json={"selection": {"tag_filter": "Smoke"}, "title": "Preview"},
        )
        assert response.status_code == 200
        spec = response.json()["spec"]
        assert spec["runspec"] == "1.0"
        assert spec["selection"]["materialized"]["count"] > 0
        assert client.get("/api/v1/runs").json()["runs"] == []

    def test_create_run_list_and_detail(self, client):
        created = client.post(
            "/api/v1/runs",
            json={"selection": {"tag_filter": FILTER}, "title": "Via API"},
        )
        assert created.status_code == 201
        run_id = created.json()["run"]["id"]
        assert created.json()["run"]["state"] == "COMPOSED"

        listed = client.get("/api/v1/runs").json()["runs"]
        assert [run["id"] for run in listed] == [run_id]

        detail = client.get(f"/api/v1/runs/{run_id}").json()
        assert detail["dispatches"] == [] and detail["deliveries"] == []

        items = client.get(f"/api/v1/runs/{run_id}/items").json()
        assert items["count"] == len(items["item_ids"]) > 0

        spec = client.get(f"/api/v1/runs/{run_id}/spec").json()
        assert spec["run"]["id"] == run_id
        yaml_spec = client.get(f"/api/v1/runs/{run_id}/spec?format=yaml")
        assert yaml_spec.headers["content-type"].startswith("application/yaml")

    def test_create_run_with_demo_dispatch_shows_lifecycle(self, client):
        created = client.post(
            "/api/v1/runs",
            json={
                "selection": {"tag_filter": "Smoke"},
                "title": "Dispatched",
                "dispatch": {"runner": "demo"},
            },
        ).json()
        run = created["run"]
        assert created["dispatch"]["mode"] == "demo"
        assert run["state"] == "COMPLETE"
        assert len(run["dispatches"]) == 1 and len(run["deliveries"]) == 1
        assert sum(run["verdict_summary"].values()) > 0

    def test_create_run_with_export_dispatch(self, client):
        created = client.post(
            "/api/v1/runs",
            json={
                "selection": {"tag_filter": "Smoke"},
                "title": "Exported",
                "dispatch": {"mode": "export"},
            },
        ).json()
        assert created["run"]["state"] == "AWAITING_RESULTS"
        assert created["dispatch"]["mode"] == "export"
        assert created["spec_document"]["runspec"] == "1.0"

    def test_finalize_endpoint(self, client):
        created = client.post(
            "/api/v1/runs",
            json={
                "selection": {"tag_filter": "Smoke"},
                "title": "Finalize me",
                "dispatch": {"mode": "export"},
            },
        ).json()
        finalized = client.post(f"/api/v1/runs/{created['run']['id']}/finalize").json()
        assert finalized["state"] == "COMPLETE"

    def test_unknown_run_is_404(self, client):
        assert client.get("/api/v1/runs/nope").status_code == 404
        assert client.get("/api/v1/runs/nope/items").status_code == 404
        assert client.get("/api/v1/runs/nope/spec").status_code == 404

    def test_root_stays_informative_without_ui_bundle_or_serves_it(self, client):
        response = client.get("/")
        assert response.status_code == 200
