"""Pushing home over HTTPS: the results push, the live channel and
``runcomposer-exec`` against a server whose certificate the system does not
trust — a private certificate authority, or a lab server's self-signed
certificate.

The server is a real HTTPS server on 127.0.0.1, with the throwaway
certificate of ``tests/fixtures/tls`` (self-signed for ``localhost`` and
``127.0.0.1``; its key is committed, so it protects nothing). Default
verification refuses it; ``ca_bundle`` naming that certificate, or
``verify_tls=False``, lets the push through.
"""

from __future__ import annotations

import base64
import json
import multiprocessing
import pickle
import ssl
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from runcomposer.core.remote import RemoteLiveChannel, ResultsPushRefused, push_results

from .exec_loader import EXEC_FILE, runcomposer_exec

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tls"
TOKEN = "rct_shop_tls_token"
RUN_ID = "01HTLSTESTRUN0000000000000"
IDS = ["Shop.Checkout.Cards.Pay By Card", "Shop.Checkout.Cards.Declined Card"]


@dataclass
class Received:
    path: str
    token: str | None
    body: bytes


@dataclass
class TlsServer:
    base: str
    cert: Path
    received: list[Received] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def callback(self) -> str:
        return f"{self.base}/api/v1/runs/{RUN_ID}/results"

    def paths(self) -> list[str]:
        with self.lock:
            return [entry.path for entry in self.received]


@pytest.fixture()
def tls_files(tmp_path: Path) -> tuple[Path, Path]:
    """The fixture certificate and key as the text files ``ssl`` loads. They
    are committed in binary form (``.der``), and turned into text here."""
    cert = tmp_path / "localhost-cert.pem"
    cert.write_text(ssl.DER_cert_to_PEM_cert((FIXTURES / "localhost-cert.der").read_bytes()))
    text = base64.b64encode((FIXTURES / "localhost-key.der").read_bytes()).decode("ascii")
    lines = [text[start:start + 64] for start in range(0, len(text), 64)]
    key = tmp_path / "localhost-key.pem"
    key.write_text("\n".join(["-----BEGIN PRIVATE KEY-----", *lines, "-----END PRIVATE KEY-----"]) + "\n")
    return cert, key


@pytest.fixture()
def tls_server(tls_files: tuple[Path, Path]) -> Iterator[TlsServer]:
    cert, key = tls_files
    server_state: dict[str, TlsServer] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            state = server_state["server"]
            with state.lock:
                state.received.append(Received(self.path, self.headers.get("X-Runcomposer-Token"), body))
            if self.path.endswith("/results"):
                answer: dict[str, Any] = {"outcome": "recorded", "verdict_count": len(IDS)}
            else:
                answer = {"dispatch_id": None, "accepted": 1}
            raw = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args: Any) -> None:
            return None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    state = TlsServer(f"https://127.0.0.1:{httpd.server_address[1]}", cert)
    server_state["server"] = state
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    directory = tmp_path / "bundle"
    directory.mkdir()
    (directory / "results.json").write_text(json.dumps({
        "format": "runcomposer-verdicts", "run_id": RUN_ID,
        "verdicts": [{"name": item, "status": "PASS"} for item in IDS]}))
    return directory


# -- the results push ----------------------------------------------------------------


class TestPushResults:
    def test_default_verification_refuses_a_certificate_the_system_does_not_trust(self, tls_server, bundle):
        with pytest.raises(ResultsPushRefused) as refused:
            push_results(tls_server.callback, TOKEN, bundle, timeout_s=10)
        message = str(refused.value)
        assert refused.value.status is None
        assert "certificate verify failed" in message.lower()
        assert "ca_bundle=" in message and "verify_tls=False" in message
        assert tls_server.paths() == []

    def test_the_ca_bundle_makes_the_certificate_trusted(self, tls_server, bundle):
        answer = push_results(tls_server.callback, TOKEN, bundle, ca_bundle=tls_server.cert, timeout_s=10)
        assert answer == {"outcome": "recorded", "verdict_count": 2}
        (received,) = tls_server.received
        assert received.path == f"/api/v1/runs/{RUN_ID}/results" and received.token == TOKEN
        assert b'filename="results.json"' in received.body

    def test_the_ca_bundle_may_be_given_as_a_string(self, tls_server, bundle):
        push_results(tls_server.callback, TOKEN, bundle, ca_bundle=str(tls_server.cert), timeout_s=10)
        assert tls_server.paths() == [f"/api/v1/runs/{RUN_ID}/results"]

    def test_verify_tls_false_skips_the_check(self, tls_server, bundle):
        answer = push_results(tls_server.callback, TOKEN, bundle, verify_tls=False, timeout_s=10)
        assert answer["outcome"] == "recorded"
        assert tls_server.paths() == [f"/api/v1/runs/{RUN_ID}/results"]

    def test_a_ca_bundle_that_does_not_exist_is_refused_before_anything_is_sent(
        self, tls_server, bundle, tmp_path
    ):
        missing = tmp_path / "certs" / "shop-ca.pem"
        with pytest.raises(FileNotFoundError, match=r"no such CA bundle: .*shop-ca\.pem"):
            push_results(tls_server.callback, TOKEN, bundle, ca_bundle=missing, timeout_s=10)
        assert tls_server.paths() == []

    def test_a_ca_bundle_without_a_certificate_is_refused(self, tls_server, bundle, tmp_path):
        not_a_bundle = tmp_path / "shop-ca.pem"
        not_a_bundle.write_text("this is no certificate\n")
        with pytest.raises(ValueError, match="holds no certificate"):
            push_results(tls_server.callback, TOKEN, bundle, ca_bundle=not_a_bundle, timeout_s=10)
        assert tls_server.paths() == []


# -- the live channel ----------------------------------------------------------------


def push_from_a_spawned_process(channel: RemoteLiveChannel, answers: Any) -> None:
    """Runs in a spawned process: the channel arrives pickled, and pushes from
    there (the TLS context is built in this process)."""
    channel.live("eu", IDS[0], "RUNNING")
    channel.log("pushed from a spawned worker")
    answers.put(channel.failures)


class TestRemoteLiveChannel:
    def test_default_verification_counts_a_failure_and_raises_nothing(self, tls_server):
        channel = RemoteLiveChannel(tls_server.callback, TOKEN, timeout_s=10)
        channel.live("eu", IDS[0], "RUNNING")
        channel.log("not trusted")
        assert channel.failures == 2
        assert tls_server.paths() == []

    def test_the_ca_bundle_makes_the_certificate_trusted(self, tls_server):
        channel = RemoteLiveChannel(tls_server.callback, TOKEN, dispatch_id="d-1", ca_bundle=tls_server.cert)
        channel.live("eu", IDS[0], "PASS", duration_ms=12)
        channel.log("trusted through the CA bundle")
        assert channel.failures == 0
        live, log = tls_server.received
        assert live.path == f"/api/v1/runs/{RUN_ID}/live" and live.token == TOKEN
        assert json.loads(live.body)["updates"][0]["item_id"] == IDS[0]
        assert json.loads(live.body)["dispatch"] == "d-1"
        assert log.path == f"/api/v1/runs/{RUN_ID}/log"
        assert json.loads(log.body)["lines"] == [{"message": "trusted through the CA bundle", "level": "info"}]

    def test_verify_tls_false_skips_the_check(self, tls_server):
        channel = RemoteLiveChannel(tls_server.callback, TOKEN, verify_tls=False)
        channel.log("a lab server")
        assert channel.failures == 0
        assert tls_server.paths() == [f"/api/v1/runs/{RUN_ID}/log"]

    def test_a_ca_bundle_that_does_not_exist_is_refused_where_it_is_given(self, tmp_path):
        with pytest.raises(FileNotFoundError, match=r"no such CA bundle: .*shop-ca\.pem"):
            RemoteLiveChannel("https://ci.shop.example/api/v1/runs/r/results", TOKEN,
                              ca_bundle=tmp_path / "shop-ca.pem")

    def test_it_survives_a_pickle_round_trip_and_still_pushes(self, tls_server):
        channel = RemoteLiveChannel(tls_server.callback, TOKEN, ca_bundle=tls_server.cert)
        channel.log("before the round trip")  # a context exists in this process now
        assert not any(isinstance(value, ssl.SSLContext) for value in vars(channel).values())
        copy = pickle.loads(pickle.dumps(channel))
        assert (copy.ca_bundle, copy.verify_tls, copy.token) == (str(tls_server.cert), True, TOKEN)
        copy.log("after the round trip")
        assert copy.failures == 0
        assert len(tls_server.paths()) == 2

    def test_a_spawned_process_pushes_with_the_ca_bundle(self, tls_server):
        spawn = multiprocessing.get_context("spawn")
        answers = spawn.Queue()
        channel = RemoteLiveChannel(tls_server.callback, TOKEN, ca_bundle=tls_server.cert, timeout_s=10)
        child = spawn.Process(target=push_from_a_spawned_process, args=(channel, answers))
        child.start()
        try:
            failures = answers.get(timeout=60)
        finally:
            child.join(timeout=60)
        assert child.exitcode == 0
        assert failures == 0
        assert tls_server.paths() == [f"/api/v1/runs/{RUN_ID}/live", f"/api/v1/runs/{RUN_ID}/log"]

    def test_a_spawned_process_with_default_verification_counts_failures(self, tls_server):
        spawn = multiprocessing.get_context("spawn")
        answers = spawn.Queue()
        channel = RemoteLiveChannel(tls_server.callback, TOKEN, timeout_s=10)
        child = spawn.Process(target=push_from_a_spawned_process, args=(channel, answers))
        child.start()
        try:
            failures = answers.get(timeout=60)
        finally:
            child.join(timeout=60)
        assert child.exitcode == 0 and failures == 2
        assert tls_server.paths() == []


# -- runcomposer-exec ----------------------------------------------------------------


@pytest.fixture()
def spec_file(tmp_path: Path) -> Path:
    document = {
        "runspec": "1.0",
        "run": {"id": RUN_ID, "title": "Cards over HTTPS", "created_at": "2026-07-06T09:14:03Z"},
        "selection": {"tag_filter": "Payments-Cards",
                      "materialized": {"item_ids": IDS, "at": "2026-07-06T09:14:02Z", "count": 2}},
        "source": {"provider": "manifest", "snapshot": "sha256:0"},
        "results": {"expect": [{"format": "runcomposer-verdicts"}], "shards": 1, "deliver": "none"},
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def clean_env(monkeypatch):
    for name in ("RUNCOMPOSER_CA_BUNDLE", "RUNCOMPOSER_VERIFY_TLS", "RUNCOMPOSER_INGEST_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def exec_args(spec_file: Path, out: Path, server: TlsServer, *extra: str) -> list[str]:
    return [str(spec_file), "--out", str(out), "--simulate", "--shard", "eu", "--dispatch", "d-1",
            "--callback", server.callback, "--token", TOKEN, "--push", *extra]


def everything_arrived(server: TlsServer) -> None:
    paths = server.paths()
    base = f"/api/v1/runs/{RUN_ID}"
    assert paths.count(f"{base}/live") == 2 * len(IDS)  # RUNNING, then how it ended
    assert paths.count(f"{base}/log") == 1
    assert paths[-1] == f"{base}/results"
    assert all(entry.token == TOKEN for entry in server.received)


class TestTheConsumerOverHttps:
    def test_default_verification_refuses_the_push_and_says_what_to_do(
        self, spec_file, tmp_path, tls_server, clean_env, capsys
    ):
        out = tmp_path / "out"
        assert runcomposer_exec.main(exec_args(spec_file, out, tls_server)) == 4
        said = capsys.readouterr().err
        assert "certificate verify failed" in said.lower()
        assert "--ca-bundle" in said and "--no-verify-tls" in said
        # Progress was dropped (said once), the push failed, the bundle stayed on disk.
        assert said.count("progress could not be reported") == 1
        assert (out / runcomposer_exec.MARKER_FILENAME).is_file()
        assert tls_server.paths() == []

    def test_ca_bundle_option(self, spec_file, tmp_path, tls_server, clean_env):
        out = tmp_path / "out"
        assert runcomposer_exec.main(exec_args(spec_file, out, tls_server, "--ca-bundle", str(tls_server.cert))) == 0
        everything_arrived(tls_server)

    def test_ca_bundle_from_the_environment(self, spec_file, tmp_path, tls_server, clean_env):
        clean_env.setenv("RUNCOMPOSER_CA_BUNDLE", str(tls_server.cert))
        assert runcomposer_exec.main(exec_args(spec_file, tmp_path / "out", tls_server)) == 0
        everything_arrived(tls_server)

    def test_no_verify_tls_option(self, spec_file, tmp_path, tls_server, clean_env):
        assert runcomposer_exec.main(exec_args(spec_file, tmp_path / "out", tls_server, "--no-verify-tls")) == 0
        everything_arrived(tls_server)

    def test_verify_tls_false_from_the_environment(self, spec_file, tmp_path, tls_server, clean_env):
        clean_env.setenv("RUNCOMPOSER_VERIFY_TLS", "false")
        assert runcomposer_exec.main(exec_args(spec_file, tmp_path / "out", tls_server)) == 0
        everything_arrived(tls_server)

    def test_the_option_wins_over_the_environment(self, spec_file, tmp_path, tls_server, clean_env, capsys):
        clean_env.setenv("RUNCOMPOSER_VERIFY_TLS", "false")
        assert runcomposer_exec.main(exec_args(spec_file, tmp_path / "out", tls_server, "--verify-tls")) == 4
        assert "certificate verify failed" in capsys.readouterr().err.lower()

    def test_a_verify_tls_value_that_is_not_a_boolean_is_refused(self, spec_file, tmp_path, tls_server, clean_env):
        clean_env.setenv("RUNCOMPOSER_VERIFY_TLS", "sometimes")
        with pytest.raises(SystemExit, match="RUNCOMPOSER_VERIFY_TLS: not true or false"):
            runcomposer_exec.main(exec_args(spec_file, tmp_path / "out", tls_server))

    def test_a_ca_bundle_that_does_not_exist_stops_it_before_anything_executes(
        self, spec_file, tmp_path, tls_server, clean_env
    ):
        out = tmp_path / "out"
        missing = tmp_path / "shop-ca.pem"
        with pytest.raises(SystemExit, match=r"--ca-bundle: no such CA bundle: .*shop-ca\.pem"):
            runcomposer_exec.main(exec_args(spec_file, out, tls_server, "--ca-bundle", str(missing)))
        assert not out.exists() and tls_server.paths() == []

    def test_a_ca_bundle_without_a_certificate_stops_it_before_anything_executes(
        self, spec_file, tmp_path, tls_server, clean_env
    ):
        out = tmp_path / "out"
        not_a_bundle = tmp_path / "shop-ca.pem"
        not_a_bundle.write_text("this is no certificate\n")
        with pytest.raises(SystemExit, match="holds no certificate"):
            runcomposer_exec.main(exec_args(spec_file, out, tls_server, "--ca-bundle", str(not_a_bundle)))
        assert not out.exists() and tls_server.paths() == []

    def test_as_a_script_with_nothing_but_the_interpreter(self, spec_file, tmp_path, tls_server):
        """The vendored file, run by a bare interpreter in isolated mode (no
        site packages, no environment), pushes over HTTPS with --ca-bundle."""
        done = subprocess.run(
            [sys.executable, "-I", str(EXEC_FILE), *exec_args(spec_file, tmp_path / "out", tls_server,
                                                          "--ca-bundle", str(tls_server.cert))],
            capture_output=True, text=True, timeout=120, env={},
        )
        assert done.returncode == 0, done.stderr
        assert "bundle pushed" in done.stderr
        everything_arrived(tls_server)
