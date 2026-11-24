"""Pushing to a runcomposer API from wherever the tests execute (DESIGN.md
§5, §6.2): the client side of ``POST /runs/{id}/live``, ``POST
/runs/{id}/log``, ``POST /runs/{id}/results`` and ``GET
/runs/{id}/dispatches/{dispatch_id}/state``.

An external executor — a CI job, a remote agent — holds the ingest token of
its dispatch. With it, and the run's callback URL, it can report which item
is executing now and append log lines while its work is still under way,
ask whether its dispatch was cancelled (:class:`RemoteLiveChannel`), and
push the results home as a bundle when it is done (:func:`push_results`).

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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RunComposerError

__all__ = [
    "CallbackUrls",
    "RemoteLiveChannel",
    "ResultsPushRefused",
    "callback_urls",
    "progress_urls",
    "push_results",
    "results_url",
]

#: The API's path below a deployment's base address.
_API_PATH = "/api/v1"


@dataclass(frozen=True)
class CallbackUrls:
    """The URLs an executor that runs elsewhere reaches one run by (see
    :func:`callback_urls`): ``run`` — the run's API resource; ``results`` —
    where a bundle is pushed (``POST``, what a runner hands out as its
    callback URL); ``live`` and ``log`` — where live status and log lines
    are pushed; ``spec`` — the document handed out for the dispatch
    (``GET``; ``None`` without a dispatch id); ``state`` — whether the
    dispatch was cancelled (``GET`` with the token; ``None`` without a
    dispatch id)."""

    run: str
    results: str
    live: str
    log: str
    spec: str | None = None
    state: str | None = None


def callback_urls(base: str, run_id: str, dispatch_id: str | None = None) -> CallbackUrls:
    """The callback URLs of a run under a deployment's base address — the
    address an executor elsewhere reaches this runcomposer by, such as a
    runner's ``callback_base`` option (``https://runcomposer.shop.example``;
    a base that already ends in ``/api/v1`` is taken as it is). With
    ``dispatch_id``, ``spec`` is the document handed out for that dispatch
    and ``state`` where that dispatch's state is read. Raises
    ``ValueError`` for an empty base or run id."""
    root = str(base or "").strip().rstrip("/")
    if not root:
        raise ValueError("callback_urls needs the deployment's base address (callback_base)")
    if not str(run_id or "").strip():
        raise ValueError("callback_urls needs a run id")
    if not root.endswith(_API_PATH):
        root += _API_PATH
    run = f"{root}/runs/{run_id}"
    return CallbackUrls(
        run=run,
        results=f"{run}/results",
        live=f"{run}/live",
        log=f"{run}/log",
        spec=f"{run}/dispatches/{dispatch_id}/spec" if dispatch_id else None,
        state=_state_url(run, dispatch_id) if dispatch_id else None,
    )


def _state_url(run_url: str, dispatch_id: str) -> str:
    return f"{run_url}/dispatches/{urllib.parse.quote(str(dispatch_id), safe='')}/state"


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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are not followed: the request carries the ingest token, and
    a redirect would hand it to whatever address the answer names. A
    redirect answer is an error answer (``HTTPError`` with its 3xx status)."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise urllib.error.HTTPError(
            req.full_url, code, f"redirect to {newurl} not followed (the request carries a token)",
            headers, fp,
        )


def _open(
    request: urllib.request.Request, timeout_s: float, ca_bundle: str | None, verify_tls: bool
) -> Any:
    """Send ``request`` — over HTTPS (the scheme compared without regard to
    case) with the TLS settings given — following no redirect."""
    secure = urllib.parse.urlsplit(request.full_url).scheme.lower() == "https"
    handlers: list[Any] = [_NoRedirect()]
    if secure:
        handlers.append(urllib.request.HTTPSHandler(context=_tls_context(ca_bundle, verify_tls)))
    return urllib.request.build_opener(*handlers).open(request, timeout=timeout_s)


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
    # The body is streamed, file by file in pieces: a bundle is never held
    # in memory as a whole. Its length is known beforehand.
    pieces: list[bytes | Path] = []
    for path in chosen:
        filename = path.name.replace('"', "%22")
        pieces.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
        )
        pieces.append(path)
        pieces.append(b"\r\n")
    for name, value in (("shard", shard), ("dispatch", dispatch_id), ("format", format)):
        if value is not None:
            pieces.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode()
            )
    pieces.append(f"--{boundary}--\r\n".encode())
    length = sum(piece.stat().st_size if isinstance(piece, Path) else len(piece) for piece in pieces)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(length),
    }
    if token:
        headers["X-Runcomposer-Token"] = token
    url = results_url(callback_url)
    request = urllib.request.Request(url, data=_streamed(pieces), headers=headers, method="POST")
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


#: The size of the pieces a pushed file is read and sent in.
_PIECE_BYTES = 1 << 16


def _streamed(pieces: Iterable[bytes | Path]) -> Iterable[bytes]:
    """The multipart body piece by piece: literal parts as they are, a file
    read in pieces of :data:`_PIECE_BYTES`."""
    for piece in pieces:
        if isinstance(piece, Path):
            with open(piece, "rb") as handle:
                while chunk := handle.read(_PIECE_BYTES):
                    yield chunk
        else:
            yield piece


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
    included; nothing is raised into the execution. With ``dispatch_id``,
    :meth:`cancel_requested` asks whether the dispatch was cancelled, at
    most once per ``cancel_check_interval_s`` seconds.

    Over HTTPS, ``ca_bundle`` is a file of the CA certificates to trust
    instead of the system's, and ``verify_tls=False`` skips the
    certificate check. A ``ca_bundle`` that does not exist, or holds no
    certificate, is refused here (``FileNotFoundError``, ``ValueError``).

    Picklable — it travels into spawned workers: its state is strings,
    numbers and flags, and the TLS context is built in the process that
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
        cancel_check_interval_s: float = 5.0,
    ):
        self.live_url, self.log_url = progress_urls(callback_url)
        self.token = token
        self.dispatch_id = dispatch_id
        self.timeout_s = float(timeout_s)
        self.ca_bundle = _checked_ca_bundle(ca_bundle)
        self.verify_tls = bool(verify_tls)
        self.failures = 0
        interval = float(cancel_check_interval_s)
        if not interval >= 0:
            raise ValueError(
                f"cancel_check_interval_s is a number of seconds, 0 or more, got {cancel_check_interval_s!r}"
            )
        self.cancel_check_interval_s = interval
        #: Where the dispatch's state is read; ``None`` without a dispatch id.
        self.state_url = _state_url(_run_url(callback_url), dispatch_id) if dispatch_id else None
        self._cancelled = False
        self._asked_at: float | None = None
        self._asking = threading.Lock()

    def __getstate__(self) -> dict[str, Any]:
        state = dict(self.__dict__)
        del state["_asking"]
        # A monotonic time of another process means nothing in this one.
        state["_asked_at"] = None
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._asking = threading.Lock()

    def cancel_requested(self) -> bool:
        """Whether runcomposer says this dispatch should stop: a cancel was
        requested for it, or it was cancelled (``GET
        /runs/{id}/dispatches/{dispatch_id}/state``, its ``stop``). The way
        an execution runcomposer did not start hears of a cancel whose stop
        never reached it — a CI server can lose one.

        Made to be called often — between two tests, or handed on as
        ``supervise(…, cancelled=channel.cancel_requested)``: the server is
        asked at most once per ``cancel_check_interval_s`` seconds (5 by
        default; 0 asks at every call), and a call in between answers
        ``False`` without a request. Once ``True`` it stays ``True``, and
        nothing more is asked.

        Never raises into the execution, as a push that cannot be made is
        dropped: a server that cannot be reached, a refusal (a wrong token,
        an unknown run or dispatch) or an answer that cannot be read is
        "not known" — ``False``. Without a ``dispatch_id`` there is nothing
        to ask about: always ``False``. A request takes at most
        ``timeout_s``."""
        if self._cancelled:
            return True
        if self.state_url is None:
            return False
        with self._asking:
            now = time.monotonic()
            if self._asked_at is not None and now - self._asked_at < self.cancel_check_interval_s:
                return False
            self._asked_at = now
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Runcomposer-Token"] = self.token
        request = urllib.request.Request(self.state_url, headers=headers, method="GET")
        try:
            with _open(request, self.timeout_s, self.ca_bundle, self.verify_tls) as response:
                answer = _answer(response.read())
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
            return False
        if isinstance(answer, dict) and answer.get("stop") is True:
            self._cancelled = True
        return self._cancelled

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
