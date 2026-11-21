"""The executor's write path over a real socket: the run and the token are
checked before the body is read, and the upload limit is counted while the
body arrives.

These requests go through a raw socket to a served deployment, with a body
sent in chunks (``Transfer-Encoding: chunked``, so no ``Content-Length``) that
the test produces one chunk at a time. That is what shows *when* the server
answers: an answer that arrives while the body is still being produced was
given without reading the rest of it.
"""

from __future__ import annotations

import http.client
import json
import select
import shutil
import socket
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from runcomposer.testing import ConnectorHarness

from .conftest import wait_until

BOUNDARY = "shop-results-boundary"
SSO = ["Shop.Auth.SSO.T001", "Shop.Auth.SSO.T002", "Shop.Auth.SSO.T003"]
CAP_MB = 1
CAP = CAP_MB * 1024 * 1024
CHUNK = 64 * 1024
# Send what fits and come back, where the platform can say so per call.
_DONTWAIT = getattr(socket, "MSG_DONTWAIT", 0)


def results_json() -> bytes:
    return json.dumps({
        "format": "runcomposer-verdicts",
        "verdicts": [{"name": name, "status": "PASS", "duration_ms": 20} for name in SSO],
    }).encode()


def file_part(name: str, content: bytes = b"") -> bytes:
    return (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + content


def endless_upload(pulled: list[int]) -> Iterator[bytes]:
    """A multipart body that never ends: one file part whose content goes on
    forever. ``pulled[0]`` counts the chunks taken from it."""
    pulled[0] += 1
    yield file_part("output.xml")
    while True:
        pulled[0] += 1
        yield b"x" * CHUNK


class RawRequest:
    """One HTTP/1.1 request on a socket of its own: the head is sent at once,
    the body chunk by chunk when the test says so."""

    def __init__(self, port: int, path: str, headers: dict[str, str], *, chunked: bool = True):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        lines = [f"POST {path} HTTP/1.1", f"Host: 127.0.0.1:{port}"]
        if chunked:
            lines.append("Transfer-Encoding: chunked")
        lines += [f"{name}: {value}" for name, value in headers.items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        self.sent = 0

    def send_chunk(self, data: bytes) -> None:
        self.sock.sendall(b"%x\r\n" % len(data) + data + b"\r\n")
        self.sent += len(data)

    def send_chunk_until(self, data: bytes, stop: threading.Event) -> bool:
        """Send one chunk from a thread of its own, giving up when ``stop``
        is set: it never sits in a send the test can no longer end (closing
        a socket does not reliably wake a thread blocked in sending on it).
        False when it gave up."""
        left = memoryview(b"%x\r\n" % len(data) + data + b"\r\n")
        while left:
            if stop.is_set():
                return False
            _, writable, _ = select.select([], [self.sock], [], 0.05)
            if writable:
                left = left[self.sock.send(left, _DONTWAIT):]
        self.sent += len(data)
        return True

    def end(self) -> None:
        self.sock.sendall(b"0\r\n\r\n")

    def response(self) -> tuple[int, dict[str, str], Any]:
        answer = http.client.HTTPResponse(self.sock)
        answer.begin()
        raw = answer.read()
        return answer.status, {k.lower(): v for k, v in answer.getheaders()}, json.loads(raw)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


@pytest.fixture()
def spool(tmp_path, monkeypatch) -> Path:
    """The process's temporary directory, made observable: everything a
    request writes to a temporary file or directory lands here."""
    directory = tmp_path / "spool"
    directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


@pytest.fixture()
def served(tmp_path, spool):
    """A served deployment with a 1 MB upload limit and a run handed out:
    ``(harness, run_id, token)``."""
    with ConnectorHarness(
        serve=True, directory=tmp_path / "deployment", core={"ingestion": {"max_upload_mb": CAP_MB}}
    ) as harness:
        run_id = harness.compose("Auth-SSO")
        exported = harness.export(run_id)
        yield harness, run_id, exported.token


def files_under(directory: Path) -> list[Path]:
    return [path for path in directory.rglob("*") if path.is_file()] if directory.exists() else []


EXECUTOR_ROUTES = ["results", "live", "log"]


class TestRefusedBeforeTheBodyIsRead:
    @pytest.mark.parametrize("route", EXECUTOR_ROUTES)
    @pytest.mark.parametrize(
        ("case", "status", "code"),
        [
            ("no token", 401, "token-required"),
            ("wrong token", 403, "token-rejected"),
            ("unknown run", 404, "unknown-run"),
        ],
    )
    def test_the_answer_comes_while_the_body_is_still_unsent(self, served, spool, route, case, status, code):
        """One chunk of a body that would never end is sent, then the test
        waits for the answer. A server that read the body before checking
        the request would wait for the rest forever."""
        harness, run_id, token = served
        headers = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}
        target = run_id
        if case == "wrong token":
            headers["Authorization"] = "Bearer rct_guessed"
        if case == "unknown run":
            target = "01NOSUCHRUN000000000000000"
            headers["Authorization"] = f"Bearer {token}"
        pulled = [0]
        body = endless_upload(pulled)
        request = RawRequest(harness.port, f"/api/v1/runs/{target}/{route}", headers)
        try:
            request.send_chunk(next(body))
            request.sock.settimeout(10)
            answer_status, answer_headers, answer = request.response()
        finally:
            request.close()
        assert (answer_status, answer["code"]) == (status, code), answer
        assert isinstance(answer["detail"], str) and answer["detail"]
        assert pulled[0] == 1, "nothing beyond the first chunk was asked for"
        assert answer_headers.get("connection") == "close", "the rest of the body is not read"
        assert files_under(spool) == [], "nothing was written to a temporary file"
        assert files_under(harness.service.quarantine.directory) == []
        assert harness.service.store.verdicts_for(run_id) == []
        assert harness.service.require_run(run_id).state == "AWAITING_RESULTS"

    def test_a_declared_length_beyond_the_limit_is_refused_before_the_body(self, served, spool):
        harness, run_id, token = served
        request = RawRequest(
            harness.port, f"/api/v1/runs/{run_id}/results",
            {
                "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
                "Authorization": f"Bearer {token}",
                "Content-Length": str(50 * CAP),
            },
            chunked=False,
        )
        try:
            request.sock.settimeout(10)
            status, headers, answer = request.response()
        finally:
            request.close()
        assert (status, answer["code"]) == (413, "payload-too-large")
        assert headers.get("connection") == "close"
        assert files_under(spool) == []


class TestTheLimitIsCountedWhileReading:
    @pytest.mark.parametrize("route", EXECUTOR_ROUTES)
    def test_an_endless_body_with_a_valid_token_is_refused_at_the_limit(self, served, spool, monkeypatch, route):
        """No ``Content-Length`` to go by: the server counts what arrives,
        answers 413 once the limit is passed, and ends the connection while
        the client is still sending. No file it wrote grew past the limit."""
        harness, run_id, token = served
        largest: list[int] = []
        removed: list[Path] = []
        rmtree = shutil.rmtree

        def measuring_rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
            path = Path(path)
            if spool in path.parents:
                removed.append(path)
                largest.extend(file.stat().st_size for file in files_under(path))
            rmtree(path, *args, **kwargs)

        monkeypatch.setattr(shutil, "rmtree", measuring_rmtree)
        pulled = [0]
        body = endless_upload(pulled)
        request = RawRequest(
            harness.port, f"/api/v1/runs/{run_id}/{route}",
            {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}", "Authorization": f"Bearer {token}"},
        )
        stop = threading.Event()

        def pump() -> None:
            try:
                for chunk in body:
                    if not request.send_chunk_until(chunk, stop):
                        return
            except (OSError, ValueError):
                return  # the server ended the connection, or the test closed it

        sender = threading.Thread(target=pump, daemon=True)
        sender.start()
        try:
            status, headers, answer = request.response()
        finally:
            stop.set()
            request.close()
            sender.join(30)
        assert not sender.is_alive()
        assert (status, answer["code"]) == (413, "payload-too-large"), answer
        assert "max_upload_mb (1 MB)" in answer["detail"]
        assert headers.get("connection") == "close"
        assert request.sent >= CAP, "the limit was reached, not guessed"
        if route == "results":
            assert removed, "the upload went into a temporary bundle directory"
            assert largest and max(largest) <= CAP, f"a file grew past the limit: {largest}"
        wait_until(lambda: files_under(spool) == [], what="the temporary bundle to be removed")
        assert files_under(harness.service.quarantine.directory) == []


class TestAPushStillWorks:
    def test_a_chunked_upload_without_a_length_delivers(self, served, spool):
        harness, run_id, token = served
        request = RawRequest(
            harness.port, f"/api/v1/runs/{run_id}/results",
            {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}", "X-Runcomposer-Token": token},
        )
        try:
            request.send_chunk(file_part("results.json", results_json()) + b"\r\n")
            request.send_chunk(
                f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="shard"\r\n\r\n1\r\n--{BOUNDARY}--\r\n'.encode()
            )
            request.end()
            status, _headers, answer = request.response()
        finally:
            request.close()
        assert status == 200, answer
        assert (answer["outcome"], answer["run_state"], answer["completion"]) == ("new", "COMPLETE", "PASS")
        assert files_under(spool) == [], "the temporary bundle is removed"

    def test_a_push_with_a_length_as_the_bundled_clients_send_it_delivers(self, served):
        """``runcomposer-exec`` and the SDK send one body with a length."""
        harness, run_id, token = served
        body = (
            file_part("results.json", results_json()) + b"\r\n"
            + f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="dispatch"\r\n\r\n'.encode()
            + harness.service.require_run(run_id).latest_dispatch.dispatch_id.encode()
            + f"\r\n--{BOUNDARY}--\r\n".encode()
        )
        connection = http.client.HTTPConnection("127.0.0.1", harness.port, timeout=30)
        try:
            connection.request(
                "POST", f"/api/v1/runs/{run_id}/results", body=body,
                headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}", "X-Runcomposer-Token": token},
            )
            answer = connection.getresponse()
            status, payload = answer.status, json.loads(answer.read())
        finally:
            connection.close()
        assert status == 200, payload
        assert payload["completion"] == "PASS"

    @pytest.mark.parametrize(
        ("body", "detail"),
        [
            (b"--" + BOUNDARY.encode() + b"\r\nContent-Disposition: form-data; name=\"shard\"\r\n\r\n1\r\n--"
             + BOUNDARY.encode() + b"--\r\n", "at least one file under 'files'"),
            (file_part("results.json", b"{}"), "ended before its closing boundary"),
            (b"this is not multipart at all", "cannot be read"),
        ],
        ids=["no file", "cut short", "garbage"],
    )
    def test_a_body_that_is_not_a_bundle_is_a_400(self, served, spool, body, detail):
        harness, run_id, token = served
        request = RawRequest(
            harness.port, f"/api/v1/runs/{run_id}/results",
            {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}", "X-Runcomposer-Token": token},
        )
        try:
            request.send_chunk(body)
            request.end()
            status, _headers, answer = request.response()
        finally:
            request.close()
        assert (status, answer["code"]) == (400, "invalid-request"), answer
        assert detail in answer["detail"]
        assert files_under(harness.service.quarantine.directory) == []
        wait_until(lambda: files_under(spool) == [], what="the temporary bundle to be removed")

    def test_a_body_that_is_not_multipart_is_a_400(self, served):
        harness, run_id, token = served
        request = RawRequest(
            harness.port, f"/api/v1/runs/{run_id}/results",
            {"Content-Type": "application/json", "X-Runcomposer-Token": token},
        )
        try:
            request.send_chunk(b"{}")
            request.end()
            status, _headers, answer = request.response()
        finally:
            request.close()
        assert (status, answer["code"]) == (400, "invalid-request")
        assert "multipart/form-data" in answer["detail"]


class TestARefusalReachesAClientThatIsStillSending:
    """The server answers a refused push without reading its body and ends
    the connection. A client that is in the middle of sending must still be
    able to read that answer — not find the connection reset under it."""

    @pytest.mark.parametrize("route", EXECUTOR_ROUTES)
    def test_the_client_sends_on_after_the_refusal_and_then_reads_it(self, served, route):
        harness, run_id, _token = served
        request = RawRequest(
            harness.port, f"/api/v1/runs/{run_id}/{route}",
            {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}", "Authorization": "Bearer rct_wrong"},
        )
        try:
            # Four megabytes more, sent without looking for an answer first:
            # the refusal is long out when the last of them leaves.
            try:
                request.send_chunk(file_part("output.xml"))
                for _ in range(4 * 1024 * 1024 // CHUNK):
                    request.send_chunk(b"x" * CHUNK)
            except OSError:
                pass  # the server stopped taking it in; the answer is still wanted
            status, headers, answer = request.response()
        finally:
            request.close()
        assert (status, answer["code"]) == (403, "token-rejected")
        assert headers.get("connection") == "close"
