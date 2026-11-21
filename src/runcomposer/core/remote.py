"""Pushing to a runcomposer API from wherever the tests execute (DESIGN.md
§5, §6.2): the client side of ``POST /runs/{id}/live``, ``POST
/runs/{id}/log`` and ``POST /runs/{id}/results``.

An external executor — a CI job, a remote agent — holds the ingest token of
its dispatch. With it, and the run's callback URL, it can report which item
is executing now and append log lines while its work is still under way
(:class:`RemoteLiveChannel`), and push the results home as a bundle when it
is done (:func:`push_results`).

Stdlib only, so that a worker or an agent can use it with nothing installed
but this package. Progress is best-effort by design: what cannot be pushed is
dropped, never raised into the execution it reports about. Results are not:
a bundle that cannot be pushed is an error the caller has to see.

Over HTTPS the server's certificate is verified against the system's trust
store, or against ``ca_bundle`` (a file of the CA certificates to trust,
as text — a private CA); ``verify_tls=False`` skips the verification, for a
lab server whose certificate nobody signed. A ``ca_bundle`` that does not
exist, or holds no certificate, is refused where it is given — never
discovered as a string of dropped pushes.
"""

from __future__ import annotations

import functools
import http.client
import json
import ssl
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .errors import RunComposerError

__all__ = ["RemoteLiveChannel", "ResultsPushRefused", "progress_urls", "push_results", "results_url"]


def _run_url(callback_url: str) -> str:
    base = callback_url.rstrip("/")
    for suffix in ("/results", "/live", "/log"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def progress_urls(callback_url: str) -> tuple[str, str]:
    """``(live URL, log URL)`` for a run, from any URL of the run's API
    resource: ``…/api/v1/runs/{id}`` itself or its ``…/results`` endpoint
    (the ``callback_url`` a runner hands out)."""
    base = _run_url(callback_url)
    return f"{base}/live", f"{base}/log"


def results_url(callback_url: str) -> str:
    """The run's results endpoint, from any URL of the run's API resource
    (as :func:`progress_urls`)."""
    return f"{_run_url(callback_url)}/results"


def _checked_ca_bundle(ca_bundle: str | Path | None) -> str | None:
    """The CA bundle as a path string, after making sure ``ssl`` can load it:
    a file that does not exist is a ``FileNotFoundError``, one that holds no
    certificate a ``ValueError`` — both naming the file."""
    if ca_bundle is None:
        return None
    path = str(ca_bundle)
    if not Path(path).is_file():
        raise FileNotFoundError(f"no such CA bundle: {path}")
    try:
        _tls_context(path, True)
    except ssl.SSLError as exc:
        raise ValueError(f"the CA bundle {path} holds no certificate that can be loaded: {exc}") from None
    return path


@functools.lru_cache(maxsize=8)
def _tls_context(ca_bundle: str | None, verify_tls: bool) -> ssl.SSLContext:
    """One TLS context per process and setting — built where it is used, so
    that nothing unpicklable ever has to travel with a channel."""
    context = ssl.create_default_context(cafile=ca_bundle)
    if not verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _open(
    request: urllib.request.Request, timeout_s: float, ca_bundle: str | None, verify_tls: bool
) -> Any:
    context = _tls_context(ca_bundle, verify_tls) if request.full_url.startswith("https:") else None
    return urllib.request.urlopen(request, timeout=timeout_s, context=context)


def _unreachable_reason(exc: BaseException) -> str:
    """What to say about a push that did not reach the server; for a
    certificate that could not be verified, also what to do about it."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    text = str(reason)
    if isinstance(reason, ssl.SSLCertVerificationError):
        text += (
            " — the server's certificate is not trusted: give ca_bundle= (a file holding the "
            "certificate of the CA that signed it), or verify_tls=False for a server whose "
            "certificate nobody signed"
        )
    return text


class ResultsPushRefused(RunComposerError, RuntimeError):
    """A results push that did not go through. ``status`` is the HTTP
    status of the refusal (404 an unknown run, 401 no token, 403 a wrong
    token or another dispatch's, 409 or 422 a bundle refused for what it
    holds — ``body`` says why, and names the quarantine entry where it was
    kept), or ``None`` when the server could not
    be reached. ``body`` is the server's answer: its JSON error document
    (``code``, ``detail`` …) when there is one, else the text."""

    code = "results-push-refused"

    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def push_results(
    callback_url: str,
    token: str | None,
    files: str | Path | Iterable[str | Path],
    *,
    shard: str | None = None,
    dispatch_id: str | None = None,
    format: str | None = None,
    timeout_s: float = 120.0,
    ca_bundle: str | Path | None = None,
    verify_tls: bool = True,
) -> dict[str, Any]:
    """Push a results bundle to the run's results endpoint — what a job's
    post step does with ``curl -F files=@…`` — and return the server's
    answer (the ingest report: ``outcome``, ``verdict_count``, ``warnings``
    …).

    ``callback_url`` is any URL of the run's API resource (the
    ``{callback_url}`` a runner hands out), ``token`` the ingest token of the
    dispatch. ``files`` is a path or several: a file is sent as it is, a
    directory sends every file directly in it. ``shard`` names the shard the
    bundle delivers (needed when the dispatch declared more than one),
    ``dispatch_id`` the dispatch it belongs to, ``format`` the results format
    when it should be declared. One multipart ``POST``, never repeated — a
    second delivery of the same bundle is harmless (a duplicate), but a
    retry policy is the caller's to choose. Over HTTPS, ``ca_bundle`` is a
    file of the CA certificates to trust instead of the system's, and
    ``verify_tls=False`` skips the certificate check.

    Not best-effort: a refusal raises :class:`ResultsPushRefused` with the
    status and the server's answer, and so does a server that cannot be
    reached (``status`` ``None``) — a certificate that cannot be verified
    among them, its reason in the message. A path that does not exist, or a
    ``ca_bundle`` that does not, is a ``FileNotFoundError``; nothing to send,
    two files of the same name, or a ``ca_bundle`` without a certificate a
    ``ValueError`` — all before anything is sent."""
    bundle = _checked_ca_bundle(ca_bundle)
    paths = [files] if isinstance(files, (str, Path)) else list(files)
    chosen: list[Path] = []
    for entry in paths:
        path = Path(entry)
        if path.is_dir():
            chosen.extend(sorted(child for child in path.iterdir() if child.is_file()))
        elif path.is_file():
            chosen.append(path)
        else:
            raise FileNotFoundError(f"no such results file or directory: {path}")
    if not chosen:
        raise ValueError(f"nothing to push: no files in {[str(p) for p in paths]}")
    names = [path.name for path in chosen]
    twice = sorted({name for name in names if names.count(name) > 1})
    if twice:
        raise ValueError(f"two results files would be sent as {twice} — a bundle is flat")

    boundary = "runcomposer-" + uuid.uuid4().hex
    parts: list[bytes] = []
    for path in chosen:
        filename = path.name.replace('"', "%22")
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
            + path.read_bytes() + b"\r\n"
        )
    for name, value in (("shard", shard), ("dispatch", dispatch_id), ("format", format)):
        if value is not None:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode()
            )
    parts.append(f"--{boundary}--\r\n".encode())
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if token:
        headers["X-Runcomposer-Token"] = token
    url = results_url(callback_url)
    request = urllib.request.Request(url, data=b"".join(parts), headers=headers, method="POST")
    try:
        with _open(request, timeout_s, bundle, bool(verify_tls)) as response:
            status, raw = int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        body = _answer(exc.read())
        detail = (body.get("detail") or body.get("code")) if isinstance(body, dict) else body
        raise ResultsPushRefused(
            f"the results push to {url} was refused ({exc.code}): {detail}",
            status=exc.code, body=body,
        ) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise ResultsPushRefused(
            f"the results could not be pushed to {url}: {_unreachable_reason(exc)}"
        ) from None
    answer = _answer(raw)
    if not isinstance(answer, dict):
        raise ResultsPushRefused(
            f"the results push to {url} was answered ({status}) with something that is not a "
            f"JSON object: {str(answer)[:200]}",
            status=status, body=answer,
        )
    return answer


def _answer(raw: bytes) -> Any:
    """The server's JSON answer, or its text when it is not JSON."""
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


class RemoteLiveChannel:
    """A ``LiveChannel`` over HTTP: ``live`` and ``log`` are POSTed to the
    run's API, authorized by the ingest token of the dispatch. ``failures``
    counts what could not be pushed — a certificate that cannot be verified
    included; nothing is raised into the execution.

    Over HTTPS, ``ca_bundle`` is a file of the CA certificates to trust
    instead of the system's, and ``verify_tls=False`` skips the
    certificate check. A ``ca_bundle`` that does not exist, or holds no
    certificate, is refused here (``FileNotFoundError``, ``ValueError``).

    Picklable — it travels into spawned workers: its state is strings,
    numbers and a flag, and the TLS context is built in the process that
    pushes."""

    def __init__(
        self,
        callback_url: str,
        token: str | None = None,
        *,
        dispatch_id: str | None = None,
        timeout_s: float = 5.0,
        ca_bundle: str | Path | None = None,
        verify_tls: bool = True,
    ):
        self.live_url, self.log_url = progress_urls(callback_url)
        self.token = token
        self.dispatch_id = dispatch_id
        self.timeout_s = float(timeout_s)
        self.ca_bundle = _checked_ca_bundle(ca_bundle)
        self.verify_tls = bool(verify_tls)
        self.failures = 0

    def live(
        self, shard: str, item_id: str, status: str, *, duration_ms: int = 0, message: str = ""
    ) -> None:
        self._post(
            self.live_url,
            {
                "updates": [
                    {
                        "shard": str(shard),
                        "item_id": str(item_id),
                        "status": str(status),
                        "duration_ms": int(duration_ms),
                        "message": str(message),
                    }
                ]
            },
        )

    def log(self, message: str, level: str = "info") -> None:
        self._post(self.log_url, {"lines": [{"message": str(message), "level": str(level)}]})

    def _post(self, url: str, body: dict[str, Any]) -> None:
        if self.dispatch_id:
            body["dispatch"] = self.dispatch_id
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Runcomposer-Token"] = self.token
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with _open(request, self.timeout_s, self.ca_bundle, self.verify_tls) as response:
                response.read()
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
            self.failures += 1
