"""What a browser may do with this server: the headers artifacts are served
with (and that SECURITY.md quotes them exactly), the API documentation
routes, and the refusal of state-changing requests a browser sends for
another site's page."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Route

from runcomposer.app.config import ConfigError
from runcomposer.sdk import load_config

from .conftest import REPO, compose, write_config

API = "/api/v1"
SSO = ["Shop.Auth.SSO.T001", "Shop.Auth.SSO.T002", "Shop.Auth.SSO.T003"]
#: The methods that change nothing — every other one changes state.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
ARTIFACT_HEADERS = ("content-security-policy", "x-content-type-options", "referrer-policy")


# -- artifact headers, as SECURITY.md quotes them --------------------------------------------


def quoted_header_blocks() -> list[dict[str, str]]:
    """The fenced header blocks of SECURITY.md's artifact section, in order:
    every artifact, then a ``text/html`` one."""
    text = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    section = text[text.index("**2. Artifact serving.**"):text.index("**3. ")]
    blocks = re.findall(r"^```\n(.*?)^```$", section, flags=re.MULTILINE | re.DOTALL)
    parsed = []
    for block in blocks:
        headers = {}
        for line in block.strip().splitlines():
            name, _, value = line.partition(": ")
            headers[name.strip().lower()] = value.strip()
        parsed.append(headers)
    return parsed


class TestArtifactHeadersAreTheOnesDocumented:
    @pytest.fixture()
    def served(self, make_client, tmp_path):
        client, service = make_client()
        run_id = compose(service, "Auth-SSO")
        workdir = tmp_path / "artifacts" / run_id / "dispatch-1"
        workdir.mkdir(parents=True)
        (workdir / "output.xml").write_text("<robot/>", encoding="utf-8")
        (workdir / "log.html").write_text("<html><script>1</script></html>", encoding="utf-8")
        for name, media_type in (("output.xml", "application/xml"), ("log.html", "text/html")):
            service.store.add_artifact_ref(
                run_id, None, name=name, media_type=media_type, url_or_path=str(workdir / name)
            )
        return client, run_id

    def test_security_md_quotes_two_complete_header_blocks(self):
        blocks = quoted_header_blocks()
        assert len(blocks) == 2, blocks
        for block in blocks:
            assert set(block) == set(ARTIFACT_HEADERS), block

    @pytest.mark.parametrize(("index", "file"), [(0, "output.xml"), (1, "log.html")], ids=["any artifact", "text/html"])
    def test_the_server_sends_exactly_what_security_md_quotes(self, served, index, file):
        client, run_id = served
        response = client.get(f"/artifacts/{run_id}/dispatch-1/{file}")
        assert response.status_code == 200
        documented = quoted_header_blocks()[index]
        assert {name: response.headers[name] for name in ARTIFACT_HEADERS} == documented


# -- API documentation routes ---------------------------------------------------------------


class TestApiDocumentation:
    def test_by_default_only_the_schema_is_served(self, make_client):
        client, _service = make_client()
        schema = client.get("/openapi.json")
        assert schema.status_code == 200 and schema.headers["content-type"].startswith("application/json")
        assert schema.json()["info"]["title"] == "runcomposer"
        assert "cdn." not in schema.text
        for viewer in ("/docs", "/redoc", "/docs/oauth2-redirect"):
            response = client.get(viewer)
            assert response.status_code == 404, viewer
            assert response.json()["code"] == "not-found"
        assert client.get(f"{API}/meta").json()["api"] == {"docs": False}

    def test_core_api_docs_turns_the_viewers_on(self, make_client):
        client, _service = make_client(core={"api": {"docs": True}})
        for viewer in ("/docs", "/redoc"):
            response = client.get(viewer)
            assert response.status_code == 200 and "/openapi.json" in response.text, viewer
        assert client.get("/openapi.json").status_code == 200
        assert client.get(f"{API}/meta").json()["api"] == {"docs": True}

    @pytest.mark.parametrize("value", ["yes", 1, None])
    def test_core_api_docs_must_be_a_boolean(self, tmp_path, value):
        with pytest.raises(ConfigError, match=r"core\.api\.docs must be true or false"):
            load_config(str(write_config(tmp_path, core={"api": {"docs": value}})))

    def test_the_schema_still_describes_the_executor_routes(self, make_client):
        """The push and progress routes check their token before FastAPI
        sees the body; their request bodies and token headers are still in
        the schema."""
        client, _service = make_client()
        paths = client.get("/openapi.json").json()["paths"]
        push = paths["/api/v1/runs/{run_id}/results"]["post"]
        form = push["requestBody"]["content"]["multipart/form-data"]["schema"]
        assert form["required"] == ["files"]
        assert set(form["properties"]) == {"files", "shard", "dispatch", "format"}
        for route in ("results", "live", "log"):
            operation = paths[f"/api/v1/runs/{{run_id}}/{route}"]["post"]
            headers = {p["name"] for p in operation["parameters"] if p["in"] == "header"}
            assert headers == {"authorization", "x-runcomposer-token"}, route
        live = paths["/api/v1/runs/{run_id}/live"]["post"]["requestBody"]["content"]["application/json"]
        assert live["schema"]["$ref"].endswith("/LivePush")


# -- cross-site requests ----------------------------------------------------------------------


EVIL = "https://shop-deals.example"


def compile_request(client: Any, **headers: str) -> Any:
    return client.post(f"{API}/selection/compile", json={"tag_filter": "Auth-SSO"}, headers=headers)


def assert_cross_site_refusal(response: Any) -> None:
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "cross-site-request" and isinstance(body["detail"], str) and body["detail"]


class TestCrossSiteRequests:
    def test_a_foreign_origin_is_refused(self, client):
        assert_cross_site_refusal(compile_request(client, Origin=EVIL))

    def test_a_cross_site_fetch_without_an_origin_is_refused(self, client):
        assert_cross_site_refusal(compile_request(client, **{"Sec-Fetch-Site": "cross-site"}))

    @pytest.mark.parametrize("origin", ["null", "file://", "chrome-extension://abc", "not a url"])
    def test_an_origin_that_is_no_http_origin_is_refused(self, client, origin):
        assert_cross_site_refusal(compile_request(client, Origin=origin))

    def test_a_cross_site_body_less_post_changes_nothing(self, make_client):
        client, service = make_client()
        run_id = compose(service, "Auth-SSO")
        service.export(run_id)
        for action in ("finalize", "cancel", "rerun"):
            assert_cross_site_refusal(client.post(f"{API}/runs/{run_id}/{action}", headers={"Origin": EVIL}))
        assert_cross_site_refusal(client.delete(f"{API}/runs/{run_id}", headers={"Origin": EVIL}))
        run = service.require_run(run_id)
        assert (run.state, len(run.dispatches)) == ("AWAITING_RESULTS", 1)

    def test_the_servers_own_origin_passes(self, client):
        """The test client sends ``Host: testserver``."""
        assert compile_request(client, Origin="http://testserver", **{"Sec-Fetch-Site": "same-origin"}).status_code == 200
        assert compile_request(client, Origin="http://testserver:80").status_code == 200
        assert compile_request(client, Origin="HTTP://TestServer").status_code == 200

    def test_another_port_on_the_same_host_is_another_origin(self, client):
        assert_cross_site_refusal(compile_request(client, Origin="http://testserver:8100"))

    def test_behind_a_tls_proxy_that_passes_the_host_the_ui_passes(self, client):
        """The scheme is not compared: the proxy speaks https to the browser
        and http to runcomposer."""
        headers = {"Host": "runcomposer.shop.example", "Origin": "https://runcomposer.shop.example"}
        assert compile_request(client, **headers).status_code == 200

    def test_a_proxy_that_rewrites_the_host_needs_the_public_origin_listed(self, make_client):
        headers = {"Host": "127.0.0.1:8100", "Origin": "https://runcomposer.shop.example"}
        client, _service = make_client()
        assert_cross_site_refusal(compile_request(client, **headers))
        client, _service = make_client(core={"api": {"cors": ["https://runcomposer.shop.example"]}})
        assert compile_request(client, **headers).status_code == 200

    @pytest.mark.parametrize("value", ["same-origin", "same-site", "none"])
    def test_a_same_site_or_user_initiated_request_without_an_origin_passes(self, client, value):
        assert compile_request(client, **{"Sec-Fetch-Site": value}).status_code == 200

    def test_a_sibling_host_of_the_same_site_is_another_origin(self, client):
        headers = {"Host": "runcomposer.shop.example", "Origin": "https://wiki.shop.example", "Sec-Fetch-Site": "same-site"}
        assert_cross_site_refusal(compile_request(client, **headers))

    def test_an_allow_listed_origin_passes(self, make_client):
        client, _service = make_client(core={"api": {"cors": ["https://dashboard.shop.example"]}})
        response = compile_request(client, Origin="https://dashboard.shop.example", **{"Sec-Fetch-Site": "cross-site"})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://dashboard.shop.example"
        assert_cross_site_refusal(compile_request(client, Origin=EVIL))

    def test_an_allow_list_of_any_origin_lets_every_origin_in(self, make_client):
        client, _service = make_client(core={"api": {"cors": ["*"]}})
        assert compile_request(client, Origin=EVIL).status_code == 200

    def test_a_client_that_is_no_browser_is_unaffected(self, client):
        assert compile_request(client).status_code == 200

    def test_reading_is_unaffected(self, client):
        for headers in ({"Origin": EVIL}, {"Sec-Fetch-Site": "cross-site"}):
            assert client.get(f"{API}/health", headers=headers).status_code == 200
            assert client.head(f"{API}/health", headers=headers).status_code in (200, 405)
            assert client.options(f"{API}/health", headers=headers).status_code in (200, 405)

    def test_a_token_push_from_a_foreign_origin_is_refused_and_without_one_delivers(self, make_client):
        client, service = make_client()
        run_id = compose(service, "Auth-SSO")
        token = service.export(run_id).token
        payload = (
            '{"format": "runcomposer-verdicts", "verdicts": ['
            + ", ".join(f'{{"name": "{name}", "status": "PASS"}}' for name in SSO)
            + "]}"
        ).encode()
        files = [("files", ("results.json", payload, "application/json"))]
        refused = client.post(
            f"{API}/runs/{run_id}/results", files=files, headers={"X-Runcomposer-Token": token, "Origin": EVIL}
        )
        assert_cross_site_refusal(refused)
        assert service.store.verdicts_for(run_id) == []
        delivered = client.post(f"{API}/runs/{run_id}/results", files=files, headers={"X-Runcomposer-Token": token})
        assert delivered.status_code == 200 and delivered.json()["completion"] == "PASS"


def state_changing_routes(app: Any) -> list[tuple[str, str]]:
    """Every (method, path) of the app that is not GET/HEAD/OPTIONS: the
    API's own description lists every API route, whatever router it was
    included through; the routes outside it are the app's top-level ones."""
    found = set()
    for path, operations in app.openapi()["paths"].items():
        found.update((method.upper(), path) for method in operations if method.upper() not in SAFE_METHODS)
    for route in app.routes:
        if isinstance(route, (APIRoute, Route)):
            found.update((method, route.path) for method in (route.methods or set()) - SAFE_METHODS)
    return sorted(found)


def concrete(path: str, run_id: str) -> str:
    return re.sub(r"\{[^}]+\}", lambda m: run_id if m.group(0) == "{run_id}" else "x", path)


class TestNoStateChangingRouteIsExempt:
    def test_every_state_changing_route_refuses_a_cross_site_request(self, make_client):
        """Every route of the app that is not GET/HEAD/OPTIONS, enumerated
        from the app itself — a route added later is covered without being
        listed here."""
        client, service = make_client()
        run_id = compose(service, "Auth-SSO")
        token = service.export(run_id).token
        routes = state_changing_routes(client.app)
        assert ("POST", "/api/v1/runs/{run_id}/results") in routes
        assert ("POST", "/api/v1/runs/{run_id}/finalize") in routes
        assert ("DELETE", "/api/v1/quarantine/{entry_id}") in routes
        assert len(routes) >= 18, routes
        for method, path in routes:
            url = concrete(path, run_id)
            for headers in ({"Origin": EVIL}, {"Sec-Fetch-Site": "cross-site"}):
                response = client.request(
                    method, url, headers={**headers, "X-Runcomposer-Token": token}, json={}
                )
                assert response.status_code == 403, (method, path, headers, response.text)
                assert response.json()["code"] == "cross-site-request", (method, path)
        run = service.require_run(run_id)
        assert (run.state, len(run.dispatches)) == ("AWAITING_RESULTS", 1), "nothing was changed"
        assert service.store.verdicts_for(run_id) == []

    def test_the_rule_is_not_attached_to_routes(self, client):
        """One rule in front of the app, not a check per route: a path no
        route serves is refused the same way."""
        assert_cross_site_refusal(client.post(f"{API}/no-such-route", headers={"Origin": EVIL}))
        assert client.post(f"{API}/no-such-route").status_code == 404

