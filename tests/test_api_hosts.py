"""The host names the server answers to (``core.api.hosts``).

A request for any other name — what a page on a rebound DNS name sends — is
refused with ``400`` (``unknown-host``) and the project's error document,
for every method and every route, health and meta included. Unset, a
server bound to a loopback address answers to the loopback names only; any
other server, and an app built without knowing its address, answers to
every name.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import subprocess
import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient

from runcomposer.app.config import ConfigError
from runcomposer.http import create_app
from runcomposer.sdk import load_config

from .conftest import wait_until, write_config

API = "/api/v1"
FOREIGN = "pages.invalid"


@pytest.fixture()
def make_app(tmp_path):
    opened: list[TestClient] = []

    def factory(hosts: Any = None, bind_host: str | None = None) -> TestClient:
        core = {"api": {"hosts": hosts}} if hosts is not None else {}
        config = load_config(str(write_config(tmp_path, core=core)))
        app = create_app(config) if bind_host is None else create_app(config, bind_host=bind_host)
        client = TestClient(app)
        client.__enter__()
        opened.append(client)
        return client

    yield factory
    for client in opened:
        client.__exit__(None, None, None)


def get(client: TestClient, path: str, host: str) -> Any:
    return client.get(path, headers={"host": host})


def post_compile(client: TestClient, host: str) -> Any:
    return client.post(
        f"{API}/selection/compile", json={"tag_filter": "Payments-Cards"}, headers={"host": host}
    )


def assert_refused(response: Any, host: str) -> None:
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "unknown-host"
    assert set(body) == {"detail", "code"}
    assert repr(host) in body["detail"] and "core.api.hosts" in body["detail"]


class TestConfiguredHosts:
    HOSTS = ("runcomposer.shop.example", "*.preview.shop.example", "localhost:8100", "[::1]:8100")

    @pytest.mark.parametrize("path", [f"{API}/health", f"{API}/meta", f"{API}/runs", "/"])
    def test_a_foreign_host_is_refused_for_every_read(self, make_app, path):
        client = make_app(list(self.HOSTS))
        assert_refused(get(client, path, FOREIGN), FOREIGN)
        assert_refused(get(client, path, f"{FOREIGN}:8100"), f"{FOREIGN}:8100")

    def test_a_foreign_host_is_refused_for_a_state_changing_request(self, make_app):
        client = make_app(list(self.HOSTS))
        assert_refused(post_compile(client, FOREIGN), FOREIGN)
        response = client.post(
            f"{API}/runs", json={"selection": {"tag_filter": "Cart"}, "title": "Cart"},
            headers={"host": FOREIGN},
        )
        assert_refused(response, FOREIGN)
        assert client.get(f"{API}/runs", headers={"host": "runcomposer.shop.example"}).json()["total"] == 0

    def test_a_listed_name_passes_on_any_port_when_the_entry_names_none(self, make_app):
        client = make_app(list(self.HOSTS))
        for host in ("runcomposer.shop.example", "runcomposer.shop.example:8443", "RunComposer.Shop.Example"):
            assert get(client, f"{API}/health", host).status_code == 200, host
        assert post_compile(client, "runcomposer.shop.example").status_code == 200

    def test_a_wildcard_matches_the_names_below_it_not_the_name_itself(self, make_app):
        client = make_app(list(self.HOSTS))
        assert get(client, f"{API}/health", "eu.preview.shop.example").status_code == 200
        assert get(client, f"{API}/health", "a.b.preview.shop.example:9000").status_code == 200
        assert_refused(get(client, f"{API}/health", "preview.shop.example"), "preview.shop.example")
        assert_refused(get(client, f"{API}/health", "evilpreview.shop.example"), "evilpreview.shop.example")

    def test_an_entry_with_a_port_matches_that_port_only(self, make_app):
        client = make_app(list(self.HOSTS))
        assert get(client, f"{API}/health", "localhost:8100").status_code == 200
        assert get(client, f"{API}/health", "[::1]:8100").status_code == 200
        for host in ("localhost:9000", "localhost", "[::1]:9000"):
            assert_refused(get(client, f"{API}/health", host), host)

    def test_a_listed_host_still_meets_the_cross_site_rule(self, make_app):
        client = make_app(list(self.HOSTS))
        response = client.post(
            f"{API}/selection/compile", json={"tag_filter": "Cart"},
            headers={"host": "runcomposer.shop.example", "origin": f"http://{FOREIGN}"},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "cross-site-request"

    def test_a_listed_star_answers_to_every_name(self, make_app):
        client = make_app(["*"], bind_host="127.0.0.1")
        assert get(client, f"{API}/health", FOREIGN).status_code == 200

    def test_the_configured_list_wins_over_the_loopback_default(self, make_app):
        client = make_app(["runcomposer.shop.example"], bind_host="127.0.0.1")
        assert get(client, f"{API}/health", "runcomposer.shop.example").status_code == 200
        assert_refused(get(client, f"{API}/health", "127.0.0.1:8100"), "127.0.0.1:8100")


class TestTheDefault:
    def test_an_app_built_without_its_address_answers_to_every_name(self, make_app):
        client = make_app()
        assert get(client, f"{API}/health", FOREIGN).status_code == 200
        assert client.get(f"{API}/health").status_code == 200  # the test client's own name

    @pytest.mark.parametrize("bind_host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
    def test_a_loopback_bind_answers_to_the_loopback_names_only(self, make_app, bind_host):
        client = make_app(bind_host=bind_host)
        for host in ("localhost", "localhost:8100", "127.0.0.1:8100", "127.0.0.1:51234", "[::1]:8100"):
            assert get(client, f"{API}/health", host).status_code == 200, host
        for host in (FOREIGN, f"{FOREIGN}:8100", "testserver"):
            assert_refused(get(client, f"{API}/health", host), host)
            assert_refused(get(client, f"{API}/meta", host), host)
            assert_refused(post_compile(client, host), host)

    def test_the_bound_loopback_address_itself_is_answered(self, make_app):
        client = make_app(bind_host="127.0.0.2")
        assert get(client, f"{API}/health", "127.0.0.2:8100").status_code == 200

    @pytest.mark.parametrize("bind_host", ["0.0.0.0", "::", "192.0.2.10", "runcomposer.shop.example"])
    def test_any_other_bind_answers_to_every_name(self, make_app, bind_host):
        client = make_app(bind_host=bind_host)
        assert get(client, f"{API}/health", FOREIGN).status_code == 200
        assert post_compile(client, FOREIGN).status_code == 200

    def test_a_request_without_a_host_is_refused_where_names_are_checked(self, make_app):
        client = make_app(["runcomposer.shop.example"])
        stack = client.app.middleware_stack  # the app as a server calls it
        refusals: list[Any] = []

        async def receive() -> dict:
            return {"type": "http.request", "body": b""}

        async def send(message: dict) -> None:
            refusals.append(message)

        scope = {"type": "http", "method": "GET", "path": f"{API}/health", "raw_path": b"",
                 "query_string": b"", "headers": [], "scheme": "http", "server": None,
                 "client": None, "root_path": "", "http_version": "1.1", "app": client.app}
        asyncio.run(stack(scope, receive, send))
        assert refusals[0]["status"] == 400
        assert json.loads(refusals[1]["body"])["code"] == "unknown-host"


class TestTheSettingIsChecked:
    @pytest.mark.parametrize(
        "hosts",
        [[], "runcomposer.shop.example", [""], ["a*.shop.example"], ["*shop.example"], ["::1"],
         ["localhost:0"], ["localhost:70000"], ["localhost:port"], [8100], ["[::1"],
         ["https://runcomposer.shop.example"]],
    )
    def test_a_bad_entry_is_refused_at_startup(self, tmp_path, hosts):
        with pytest.raises(ConfigError, match=r"core\.api\.hosts"):
            load_config(str(write_config(tmp_path, core={"api": {"hosts": hosts}})))

    def test_null_is_the_default(self, tmp_path):
        config = load_config(str(write_config(tmp_path, core={"api": {"hosts": None}})))
        assert config.api["hosts"] is None


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def status_for(port: int, host: str, path: str = f"{API}/health") -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.putrequest("GET", path, skip_host=True)
        connection.putheader("Host", host)
        connection.endheaders()
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


def test_serve_on_the_default_loopback_address_refuses_a_foreign_host(tmp_path):
    """`runcomposer serve` hands the address it binds to the app: on the
    default 127.0.0.1 a rebound name is refused, the loopback names are
    answered."""
    port = free_port()
    config = write_config(tmp_path)
    server = subprocess.Popen(
        [sys.executable, "-m", "runcomposer.cli", "serve", "--port", str(port),
         "--config", str(config), "--log-level", "warning"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        def answering() -> bool:
            if server.poll() is not None:
                pytest.fail(f"serve exited: {server.stdout.read().decode() if server.stdout else ''}")
            try:
                return status_for(port, f"127.0.0.1:{port}")[0] == 200
            except OSError:
                return False

        wait_until(answering, timeout=60, what="the server to answer")
        assert status_for(port, f"localhost:{port}")[0] == 200
        status, body = status_for(port, f"{FOREIGN}:{port}")
        assert status == 400 and body["code"] == "unknown-host"
        status, body = status_for(port, f"{FOREIGN}:{port}", f"{API}/meta")
        assert status == 400 and body["code"] == "unknown-host"
    finally:
        server.terminate()
        server.wait(timeout=30)


def test_an_empty_list_is_refused_saying_what_null_means(tmp_path):
    """``null`` is not "every name": on a loopback bind it is the loopback
    names only — the refusal says so."""
    with pytest.raises(ConfigError, match=r"or null for the default — the loopback names on a loopback bind, every name on any other"):
        load_config(str(write_config(tmp_path, core={"api": {"hosts": []}})))
