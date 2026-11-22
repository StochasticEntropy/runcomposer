"""Hostile names on the two routes that turn a name from a request into a
path on this machine: the artifact route (a URL path) and the results push
(an upload's file name).

Every name in the table answers a 4xx with the error document — never a 5xx —
writes nothing outside the directory it belongs in, and the artifact route
never serves a file outside ``core.artifact_dir``. A refused push names its
bundle by the upload's names, never by where the server kept it.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from .conftest import compose

API = "/api/v1"
BOUNDARY = "shop-results-boundary"
SECRET = "do not serve me: k-5e6f7a8b"

HOSTILE_NAMES = [
    "\x00",
    "results\x00.json",
    "..",
    ".",
    "",
    "/",
    "dir/",
    "../x",
    "../../escaped.json",
    "..\\x",
    "..\\..\\escaped.json",
    "/etc/passwd",
    "C:\\x",
    "C:x",
    "\\\\server\\share\\x",
    "x" * 300,
    "x" * 5000,
    "\x01\x1f",
    "a\nb",
    "a\r\nX-Injected: yes",
    "tab\tname",
    "\x7f",
    "\u2215etc\u2215passwd",  # DIVISION SLASH
    "\uff0f",  # FULLWIDTH SOLIDUS
    "\uff0e\uff0e",  # FULLWIDTH FULL STOP, twice
    "\u2024\u2024",  # ONE DOT LEADER, twice
    "%2e%2e",
    "%2e%2e%2fescaped.json",
    "..%2fescaped.json",
    "..%252fescaped.json",
    "%252e%252e%252fescaped.json",
    "x. ",
    "x...",
    " ",
    "...",
    "name\nwith\nnewlines",
]
IDS = [repr(name)[:40] for name in HOSTILE_NAMES]


def assert_error_document(response: Any) -> dict[str, Any]:
    assert 400 <= response.status_code < 500, (response.status_code, response.text)
    body = response.json()
    assert isinstance(body["detail"], str) and body["detail"], body
    assert isinstance(body["code"], str) and body["code"], body
    return body


def files_outside(root: Path, allowed: list[Path]) -> set[Path]:
    """Every file under ``root`` that is not under one of ``allowed`` — and
    not the store, which every request may write."""
    found = set()
    for path in root.rglob("*"):
        if not path.is_file() or path.name.startswith("runcomposer.db"):
            continue
        if any(directory == path or directory in path.parents for directory in allowed):
            continue
        found.add(path)
    return found


def multipart(filename: str, content: bytes) -> bytes:
    """One file part under ``files``, the name exactly as given — no client
    library in between to escape or re-encode it."""
    return (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="files"; filename="'.encode()
        + filename.encode("utf-8", "surrogatepass")
        + b'"\r\nContent-Type: application/octet-stream\r\n\r\n'
        + content
        + f"\r\n--{BOUNDARY}--\r\n".encode()
    )


@pytest.fixture()
def spool(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "spool"
    directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


@pytest.fixture()
def deployment(make_client, tmp_path, spool):
    """``(client, service, run_id, token, tmp_path)``: a run handed out, a
    recorded artifact, and a secret beside the artifact directory."""
    client, service = make_client()
    run_id = compose(service, "Auth-SSO")
    exported = service.export(run_id)
    artifact = tmp_path / "artifacts" / run_id / "dispatch-1" / "env1" / "output.xml"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<robot/>", encoding="utf-8")
    service.store.add_artifact_ref(
        run_id, None, name="output.xml", media_type="application/xml", url_or_path=str(artifact)
    )
    service.store.add_artifact_ref(
        run_id, None, name="env1", media_type="inode/directory", url_or_path=str(artifact.parent)
    )
    (tmp_path / "secret.txt").write_text(SECRET, encoding="utf-8")
    (tmp_path / "artifacts" / run_id / "secret.txt").write_text(SECRET, encoding="utf-8")
    return client, service, run_id, exported.token, tmp_path


class TestTheArtifactRoute:
    @pytest.mark.parametrize("name", HOSTILE_NAMES, ids=IDS)
    def test_a_hostile_name_is_a_404_and_serves_nothing(self, deployment, name):
        client, _service, run_id, _token, tmp_path = deployment
        before = files_outside(tmp_path, [tmp_path / "spool"])
        urls = [
            # the name as one path segment, delivered exactly
            f"/artifacts/{run_id}/dispatch-1/env1/{quote(name, safe='')}",
            # …with its dots encoded too, so no client resolves `..` before sending
            f"/artifacts/{run_id}/dispatch-1/env1/{quote(name, safe='').replace('.', '%2E')}",
            # the name with its slashes and percent signs left to the server
            f"/artifacts/{run_id}/dispatch-1/env1/{quote(name, safe='/%')}",
            f"/artifacts/{quote(name, safe='/%')}",
        ]
        for url in urls:
            if not httpx.URL("http://testserver" + url).path.startswith("/artifacts/"):
                continue  # the client resolved the dot segments itself: not a request to this route
            response = client.get(url)
            body = assert_error_document(response)
            assert response.status_code == 404 and body["code"] in ("unknown-artifact", "not-found"), (url, body)
            assert SECRET not in response.text
        assert files_outside(tmp_path, [tmp_path / "spool"]) == before, "a GET writes nothing"

    def test_a_nul_byte_in_the_path_is_a_404(self, deployment):
        """A path with a NUL byte cannot name a file; resolving it raised
        instead of answering."""
        client, _service, run_id, _token, _tmp = deployment
        response = client.get(f"/artifacts/{run_id}/dispatch-1/env1/output%00.xml")
        assert response.status_code == 404
        assert response.json()["code"] == "unknown-artifact"

    @pytest.mark.parametrize(
        "url",
        [
            "/artifacts//shop.example/share/output.xml",
            "/artifacts/%5C%5Cshop.example%5Cshare%5Coutput.xml",
            "/artifacts/%2F%2Fshop.example%2Fshare%2Foutput.xml",
            "/artifacts/%5C%5C%3F%5CUNC%5Cshop.example%5Cshare%5Coutput.xml",
            "/artifacts/C:%5Coutput.xml",
            "/artifacts/C:output.xml",
            "/artifacts/%5Coutput.xml",
        ],
    )
    def test_a_share_a_drive_or_a_root_is_refused_before_it_is_resolved(self, deployment, monkeypatch, url):
        """Resolving ``\\\\host\\share\\…`` on Windows connects to that host
        (and offers it the server's credentials). Such a path, read the
        POSIX or the Windows way, is a miss without ever being resolved."""
        client, _service, _run_id, _token, _tmp = deployment
        resolved: list[str] = []
        real_resolve = Path.resolve

        def watching(self: Path, strict: bool = False) -> Path:
            resolved.append(str(self))
            return real_resolve(self, strict=strict)

        monkeypatch.setattr(Path, "resolve", watching)
        response = client.get(url)
        body = assert_error_document(response)
        assert response.status_code == 404 and body["code"] == "unknown-artifact", body
        assert not [path for path in resolved if "shop.example" in path or "output.xml" in path], resolved

    def test_the_recorded_artifact_is_still_served(self, deployment):
        client, _service, run_id, _token, _tmp = deployment
        response = client.get(f"/artifacts/{run_id}/dispatch-1/env1/output.xml")
        assert response.status_code == 200 and response.text == "<robot/>"


class TestThePushRoute:
    @pytest.mark.parametrize("name", HOSTILE_NAMES, ids=IDS)
    def test_a_hostile_upload_name_is_a_4xx_and_lands_nowhere_else(self, deployment, spool, name):
        """Each name answers a 4xx — a 400 when it is not a file name at all,
        else the content (not results) is refused and quarantined. Either
        way nothing is written beside the quarantine and the temporary
        bundle, and the temporary bundle is gone afterwards."""
        client, service, run_id, token, tmp_path = deployment
        quarantine = service.quarantine.directory
        before = files_outside(tmp_path, [quarantine, spool])
        response = client.post(
            f"{API}/runs/{run_id}/results",
            content=multipart(name, b"these are not results"),
            headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}", "X-Runcomposer-Token": token},
        )
        body = assert_error_document(response)
        assert response.status_code in (400, 422), body
        assert files_outside(tmp_path, [quarantine, spool]) == before
        assert [path for path in spool.rglob("*") if path.is_file()] == []
        for kept in quarantine.rglob("*") if quarantine.exists() else []:
            assert quarantine.resolve() in kept.resolve().parents
        assert not [path for path in tmp_path.parent.rglob("escaped.json") if "quarantine" not in path.parts]

    @pytest.mark.parametrize("name", ["..", ".", "", "/", "dir/", "results\x00.json", "a\nb", "x" * 300, "\uff0e\uff0e"])
    def test_a_name_that_is_not_a_file_name_is_a_400(self, deployment, name):
        client, service, run_id, token, _tmp = deployment
        response = client.post(
            f"{API}/runs/{run_id}/results",
            content=multipart(name, b"{}"),
            headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}", "X-Runcomposer-Token": token},
        )
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "invalid-request"
        assert service.quarantine.entries() == []

    def test_a_directory_in_the_name_is_dropped(self, deployment):
        """A bundle is flat: a client's directories are not kept."""
        client, _service, run_id, token, _tmp = deployment
        payload = json.dumps({
            "format": "runcomposer-verdicts",
            "verdicts": [
                {"name": name, "status": "PASS"}
                for name in ("Shop.Auth.SSO.T001", "Shop.Auth.SSO.T002", "Shop.Auth.SSO.T003")
            ],
        }).encode()
        response = client.post(
            f"{API}/runs/{run_id}/results",
            content=multipart("reports\\nightly/results.json", payload),
            headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}", "X-Runcomposer-Token": token},
        )
        assert response.status_code == 200, response.text
        assert response.json()["completion"] == "PASS"


ON_WINDOWS = os.name == "nt"

#: (name, refused on a POSIX server?, refused on a Windows server?). A name
#: that is not one file name anywhere is a 400 on every server, as it was;
#: what only Windows cannot keep — a character it refuses (``:`` opens
#: another stream of a file there), a device name with or without an
#: extension, a trailing dot or space it drops — is a 400 where the server
#: runs Windows, and kept as it always was on Linux and macOS.
SERVER_NAMES = [
    ("a:b.txt", True, True),  # read the Windows way, a drive: refused everywhere, as before
    ("C:x", True, True),
    ("reports\\..", True, True),  # its base name is ".."
    ("CON", False, True),
    ("con.json", False, True),
    ("nul", False, True),
    ("NUL.txt", False, True),
    ("CON .txt", False, True),
    ("aux.tar.gz", False, True),
    ("prn", False, True),
    ("COM0", False, True),
    ("COM1", False, True),
    ("com9.json", False, True),
    ("LPT0", False, True),
    ("lpt\u00b9.txt", False, True),
    ("conin$", False, True),
    ("results.json:stream", False, True),
    ("output.xml:Zone.Identifier", False, True),
    ("results.json::$DATA", False, True),
    ("a<b", False, True),
    ("a>b", False, True),
    ('a"b', False, True),
    ("a|b", False, True),
    ("a?b", False, True),
    ("a*b", False, True),
    ("results.json.", False, True),
    ("results.json ", False, True),
    ("x.", False, True),
    (" ", False, True),
    ("...", False, True),
    ("COM10", False, False),
    ("PROGRA~1", False, False),
]


def push(client, run_id, token, names, content=b"{}"):
    return client.post(
        f"{API}/runs/{run_id}/results",
        files=[("files", (name, content, "application/octet-stream")) for name in names],
        headers={"X-Runcomposer-Token": token},
    )


def one_file_here(directory: Path, first: str, second: str) -> bool:
    """Whether this file system keeps ``first`` and ``second`` as one file —
    asked of the file system itself, as the server asks it."""
    probe = directory / "probe"
    probe.mkdir()
    (probe / first).write_bytes(b"")
    try:
        (probe / second).open("xb").close()
    except FileExistsError:
        return True
    return False


class TestServerPushNames:
    """The push refuses what is wrong everywhere, and what the server's own
    file system cannot keep — decided by that file system, not by a list of
    what some other machine refuses."""

    @pytest.mark.parametrize(("name", "on_posix", "on_windows"), SERVER_NAMES, ids=[repr(n[0]) for n in SERVER_NAMES])
    def test_a_name_is_refused_where_the_server_cannot_keep_it(self, deployment, spool, name, on_posix, on_windows):
        client, service, run_id, token, _tmp = deployment
        response = client.post(
            f"{API}/runs/{run_id}/results",
            content=multipart(name, b"{}"),
            headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}", "X-Runcomposer-Token": token},
        )
        body = assert_error_document(response)  # "{}" holds no results: a 422 when the name is kept
        if on_windows if ON_WINDOWS else on_posix:
            assert response.status_code == 400 and body["code"] == "invalid-request", body
            assert service.quarantine.entries() == []
        else:
            assert response.status_code == 422, body
        assert [path for path in spool.rglob("*") if path.is_file()] == []

    def test_a_linux_or_macos_server_keeps_a_time_stamp_in_a_name_again(self, deployment):
        """``:`` in ``results-2026-01-01T10:00:00.json`` is a file name on
        Linux and macOS, and was taken there before 0.2."""
        client, _service, run_id, token, _tmp = deployment
        payload = json.dumps({
            "format": "runcomposer-verdicts",
            "verdicts": [{"name": f"Shop.Auth.SSO.T00{n}", "status": "PASS"} for n in (1, 2, 3)],
        }).encode()
        response = push(client, run_id, token, ["results-2026-01-01T10:00:00.json"], payload)
        if ON_WINDOWS:
            assert response.status_code == 400 and "this server runs on Windows" in response.json()["detail"]
        else:
            assert response.status_code == 200, response.text

    @pytest.mark.parametrize(
        "names",
        [("Results.json", "results.json"), ("caf\u00e9.json", "cafe\u0301.json"), ("results.json", "results.json")],
        ids=["case", "normalization", "twice"],
    )
    def test_two_names_that_are_one_file_here_are_a_400(self, deployment, spool, tmp_path, names):
        """One file on Windows and on a default macOS volume, two on Linux:
        the server asks its file system, and refuses only what it would keep
        as one file. The same name twice is one file everywhere."""
        client, service, run_id, token, _tmp = deployment
        expected_one = one_file_here(tmp_path, *names)
        response = push(client, run_id, token, list(names))
        body = assert_error_document(response)
        if expected_one:
            assert response.status_code == 400 and "already holds" in body["detail"], body
            assert service.quarantine.entries() == []
        else:
            assert response.status_code == 422, body
        assert [path for path in spool.rglob("*") if path.is_file()] == []

    def test_a_name_with_dots_and_spaces_inside_is_kept(self, deployment):
        client, _service, run_id, token, _tmp = deployment
        payload = json.dumps({
            "format": "runcomposer-verdicts",
            "verdicts": [{"name": f"Shop.Auth.SSO.T00{n}", "status": "PASS"} for n in (1, 2, 3)],
        }).encode()
        response = client.post(
            f"{API}/runs/{run_id}/results",
            files=[
                ("files", ("results.json", payload, "application/json")),
                ("files", ("console.log .txt", b"connected", "text/plain")),
                ("files", ("conference.notes", b"kept", "text/plain")),
            ],
            headers={"X-Runcomposer-Token": token},
        )
        assert response.status_code == 200, response.text


class TestARefusedPushNamesItsFilesOnly:
    @pytest.mark.parametrize(
        ("content", "detail"),
        [
            (b"these are not results", "bundle contains no 'runcomposer-verdicts' results: results.json"),
            (b'{"format": "runcomposer-verdicts", "verdicts": {}}', "results.json: 'verdicts' must be a list"),
        ],
        ids=["no results", "malformed"],
    )
    def test_the_answer_and_the_quarantine_entry_carry_no_server_path(self, deployment, spool, content, detail):
        client, service, run_id, token, tmp_path = deployment
        response = client.post(
            f"{API}/runs/{run_id}/results",
            files=[("files", ("results.json", content, "application/json"))],
            headers={"X-Runcomposer-Token": token},
        )
        assert response.status_code == 422, response.text
        answered = response.json()
        (listed,) = client.get(f"{API}/quarantine").json()["entries"]
        (kept,) = service.quarantine.entries()  # what `runcomposer quarantine list` prints
        for text in (answered["detail"], listed["detail"], kept.detail):
            assert detail in text, text
            assert str(spool) not in text and os.path.realpath(spool) not in text
            assert tempfile.gettempprefix() not in text and "runcomposer-push-" not in text
            assert str(tmp_path) not in text and os.path.realpath(tmp_path) not in text
        assert answered["quarantine_entry"] == listed["entry_id"] == kept.entry_id

    def test_several_files_are_named_together(self, deployment):
        client, _service, run_id, token, _tmp = deployment
        response = client.post(
            f"{API}/runs/{run_id}/results",
            files=[
                ("files", ("notes.txt", b"no results", "text/plain")),
                ("files", ("summary.txt", b"none here either", "text/plain")),
            ],
            headers={"X-Runcomposer-Token": token},
        )
        assert response.status_code == 422
        assert response.json()["detail"].endswith("results: notes.txt, summary.txt")

    def test_a_file_drop_is_named_by_its_directory_not_its_location(self, deployment, tmp_path):
        """The inbox lies on this machine too, and the quarantine is served
        over HTTP: a dropped bundle is named by its directory's name."""
        client, service, run_id, _token, _tmp = deployment
        dropped = tmp_path / "inbox" / "nightly-eu"
        dropped.mkdir(parents=True)
        (dropped / "results.json").write_bytes(b'{"format": "runcomposer-verdicts", "verdicts": {}}')
        (dropped / "runcomposer_run.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
        report = service.ingest_or_quarantine(dropped, transport="file-drop")
        assert report.detail == "results.json: 'verdicts' must be a list"
        (listed,) = client.get(f"{API}/quarantine").json()["entries"]
        assert str(tmp_path) not in listed["detail"]
