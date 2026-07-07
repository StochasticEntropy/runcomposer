"""ci-trigger runner tests (DESIGN.md §6.2b) — the HTTP boundary is mocked;
the live Jenkins-in-docker loop is proven separately (ci/jenkins/)."""

import io
import json
import urllib.error
import urllib.request

import pytest

from runcomposer.config import Config
from runcomposer.core.ports import DispatchRefused
from runcomposer.plugins.ci_trigger import CiTriggerRunner
from runcomposer.service import Service


class FakeJenkins:
    """Answers the exact URL surface the runner touches."""

    def __init__(self, *, crumb=False, verdicts=None):
        self.crumb = crumb
        self.verdicts = verdicts or []
        self.trigger_calls: list[dict] = []
        self.build_polls = 0

    def urlopen(self, request, timeout=None):
        url = request.full_url if isinstance(request, urllib.request.Request) else request

        def respond(body: bytes, headers: dict | None = None):
            response = io.BytesIO(body)
            response.headers = headers or {}
            response.__enter__ = lambda *a: response
            response.__exit__ = lambda *a: False
            response.read = response.getvalue  # type: ignore[assignment]
            return response

        if "/crumbIssuer/" in url:
            if not self.crumb:
                raise urllib.error.HTTPError(url, 404, "no crumb", {}, None)
            return respond(json.dumps({"crumbRequestField": "Jenkins-Crumb", "crumb": "c0ffee"}).encode())
        if url.endswith("/buildWithParameters"):
            params = dict(
                pair.split("=", 1)
                for pair in request.data.decode().split("&")
            )
            self.trigger_calls.append({"params": params, "headers": dict(request.header_items())})
            return respond(b"", {"Location": "http://ci.local/queue/item/42/"})
        if "/queue/item/42/api/json" in url:
            return respond(json.dumps({"executable": {"url": "http://ci.local/job/consumer/7/"}}).encode())
        if url.endswith("/job/consumer/7/api/json"):
            self.build_polls += 1
            result = "SUCCESS" if self.build_polls >= 2 else None
            return respond(json.dumps({"result": result}).encode())
        if "/artifact/results/results.json" in url:
            return respond(json.dumps({"format": "runcomposer-verdicts", "verdicts": self.verdicts}).encode())
        if "/artifact/results/runcomposer_run.json" in url:
            return respond(json.dumps({"run_id": "whatever"}).encode())
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture()
def service(tmp_path):
    return Service(Config(data={
        "store": {"sqlite": {"path": str(tmp_path / "ci.db")}},
        "core": {"ingestion": {"inbox": None, "quarantine_dir": str(tmp_path / "q")}},
    }))


def composed(service):
    result = service.compose_run({"tag_filter": "Smoke"}, title="CI run", origin="test")
    return result.run.id, service.store.get_spec_document(result.run.id)


class TestCallbackMode:
    def test_trigger_passes_spec_dispatch_callback_and_token(self, service, monkeypatch):
        fake = FakeJenkins()
        monkeypatch.setattr(CiTriggerRunner, "_open", lambda self, req, timeout=30: fake.urlopen(req))
        run_id, spec = composed(service)
        runner = CiTriggerRunner(
            base_url="http://ci.local", job="consumer",
            callback_base="http://host.docker.internal:8100",
        )
        handle = runner.dispatch(spec)
        assert handle.shards == 1
        params = fake.trigger_calls[0]["params"]
        assert json.loads(_unquote(params["SPEC_JSON"]))["run"]["id"] == run_id
        assert _unquote(params["CALLBACK_URL"]) == f"http://host.docker.internal:8100/api/v1/runs/{run_id}/results"
        assert _unquote(params["INGEST_TOKEN"]).startswith("rct_")
        assert params["DISPATCH_ID"] == handle.dispatch_id
        assert "webhook-out" in runner.last_plan

    def test_crumb_header_used_when_issued(self, service, monkeypatch):
        fake = FakeJenkins(crumb=True)
        monkeypatch.setattr(CiTriggerRunner, "_open", lambda self, req, timeout=30: fake.urlopen(req))
        _run_id, spec = composed(service)
        CiTriggerRunner(base_url="http://ci.local", job="consumer",
                        callback_base="http://x").dispatch(spec)
        headers = {k.lower(): v for k, v in fake.trigger_calls[0]["headers"].items()}
        assert headers.get("jenkins-crumb") == "c0ffee"

    def test_callback_mode_without_callback_base_refused(self, service, monkeypatch):
        monkeypatch.setattr(CiTriggerRunner, "_open", lambda self, req, timeout=30: FakeJenkins().urlopen(req))
        _run_id, spec = composed(service)
        with pytest.raises(DispatchRefused, match="callback_base"):
            CiTriggerRunner(base_url="http://ci.local", job="consumer").dispatch(spec)

    def test_unreachable_ci_is_a_clean_refusal(self, service, monkeypatch):
        def down(*_a, **_k):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(CiTriggerRunner, "_open", down)
        _run_id, spec = composed(service)
        with pytest.raises(DispatchRefused, match="unreachable"):
            CiTriggerRunner(base_url="http://ci.local", job="consumer",
                            callback_base="http://x").dispatch(spec)


class TestPollMode:
    def test_polls_build_downloads_artifacts_and_records_delivery(self, service, monkeypatch):
        run_id, spec = composed(service)
        item = spec["selection"]["materialized"]["item_ids"][0]
        fake = FakeJenkins(verdicts=[{"name": item, "status": "PASS", "duration_ms": 5}])
        monkeypatch.setattr(CiTriggerRunner, "_open", lambda self, req, timeout=30: fake.urlopen(req))
        runner = CiTriggerRunner(base_url="http://ci.local", job="consumer",
                                 completion="poll", poll_interval_s=0.01)
        runner.bind(store=service.store, source=service.source)
        runner.dispatch(spec)
        verdicts = service.store.verdicts_for(run_id)
        assert [v.status for v in verdicts] == ["PASS"]
        assert fake.build_polls >= 2  # actually polled until result appeared


def _unquote(value: str) -> str:
    import urllib.parse

    return urllib.parse.unquote_plus(value)
