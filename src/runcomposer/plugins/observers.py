"""Reference observers (DESIGN.md §6.5): three ways to be told that a run
changed state, none of which needs code.

- ``webhook`` — POST the event as JSON, optionally signed;
- ``jsonl`` — append the event as one JSON line to a file;
- ``command`` — run a command with the event in its environment.

Which events an observer is told about is configured on its entry with
``events:`` (a list of kinds; ``*`` wildcards allowed) and defaults to the
lifecycle events. An observer that raises is recorded as a warning on the
run and affects nothing else.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import ssl
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runcomposer.sdk import ConfigError, RunEvent

__all__ = ["CommandObserver", "JsonlObserver", "WebhookObserver", "event_payload"]

SIGNATURE_HEADER = "X-Runcomposer-Signature"


def event_payload(event: RunEvent) -> dict[str, Any]:
    """The JSON shape every reference observer emits."""
    return {
        "seq": event.seq,
        "run_id": event.run_id,
        "at": event.at,
        "kind": event.kind,
        "dispatch_id": event.dispatch_id,
        "data": dict(event.data),
    }


class WebhookObserver:
    """POST each event as JSON to ``url``.

    With a ``secret`` the body is signed: the request carries
    ``X-Runcomposer-Signature: sha256=<hex HMAC-SHA256 of the body>`` — verify
    it over the raw bytes you received. Give the secret as ``${env:NAME}`` so
    it is not in the config file. A non-2xx answer or a network error raises,
    which runcomposer records as a warning on the run.
    """

    def __init__(
        self,
        url: str,
        secret: str | None = None,
        timeout_s: float = 10.0,
        headers: Mapping[str, str] | None = None,
        verify_tls: bool = True,
        ca_bundle: str | None = None,
    ):
        if not str(url).startswith(("http://", "https://")):
            # The value is not echoed: a webhook URL is often itself the secret.
            raise ConfigError("webhook observer: url must start with http:// or https://")
        self.url = str(url)
        self._secret = secret.encode("utf-8") if secret else None
        self.timeout_s = float(timeout_s)
        self.headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self._context: ssl.SSLContext | None = None
        if self.url.startswith("https://"):
            self._context = ssl.create_default_context(cafile=ca_bundle)
            if not verify_tls:
                self._context.check_hostname = False
                self._context.verify_mode = ssl.CERT_NONE

    PATH_OPTIONS = ("ca_bundle",)

    def notify(self, event: RunEvent) -> None:
        body = json.dumps(event_payload(event), ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "runcomposer-webhook",
            "X-Runcomposer-Event": event.kind,
            **self.headers,
        }
        if self._secret is not None:
            digest = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
            headers[SIGNATURE_HEADER] = f"sha256={digest}"
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s, context=self._context):
                pass
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"webhook answered {exc.code} {exc.reason}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"webhook unreachable: {exc.reason}") from None


class JsonlObserver:
    """Append each event as one JSON line to ``path`` (created on first
    use; relative to the config file)."""

    PATH_OPTIONS = ("path",)

    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = threading.Lock()

    def notify(self, event: RunEvent) -> None:
        line = json.dumps(event_payload(event), ensure_ascii=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)


class CommandObserver:
    """Run ``command`` for each event, with the event in the environment:

    ``RUNCOMPOSER_EVENT`` (the kind), ``RUNCOMPOSER_RUN_ID``,
    ``RUNCOMPOSER_DISPATCH_ID``, ``RUNCOMPOSER_AT``, ``RUNCOMPOSER_STATE``,
    ``RUNCOMPOSER_COMPLETION``, ``RUNCOMPOSER_TITLE``, and
    ``RUNCOMPOSER_EVENT_JSON`` (the whole event). The same JSON is on stdin.

    ``command`` is an argv list (run directly) or a string (run by the
    shell). The event never becomes part of the command line, so nothing in a
    run title can be read as shell syntax. A non-zero exit or a timeout
    raises.
    """

    def __init__(
        self,
        command: str | list[str],
        timeout_s: float = 30.0,
        cwd: str | None = None,
    ):
        if isinstance(command, (list, tuple)):
            if not command or not all(isinstance(part, str) for part in command):
                raise ConfigError("command observer: an argv list must be non-empty strings")
            self.command: str | list[str] = list(command)
        elif isinstance(command, str) and command.strip():
            self.command = command
        else:
            raise ConfigError("command observer: 'command' must be a string or an argv list")
        self.timeout_s = float(timeout_s)
        self.cwd = cwd

    PATH_OPTIONS = ("cwd",)

    def notify(self, event: RunEvent) -> None:
        payload = json.dumps(event_payload(event), ensure_ascii=False)
        data = event.data
        env = {
            **os.environ,
            "RUNCOMPOSER_EVENT": event.kind,
            "RUNCOMPOSER_RUN_ID": event.run_id,
            "RUNCOMPOSER_DISPATCH_ID": event.dispatch_id or "",
            "RUNCOMPOSER_AT": event.at,
            "RUNCOMPOSER_STATE": str(data.get("state") or ""),
            "RUNCOMPOSER_COMPLETION": str(data.get("completion") or ""),
            "RUNCOMPOSER_TITLE": str(data.get("title") or ""),
            "RUNCOMPOSER_EVENT_JSON": payload,
        }
        try:
            completed = subprocess.run(
                self.command,
                shell=isinstance(self.command, str),
                input=payload,
                text=True,
                capture_output=True,
                env=env,
                cwd=self.cwd,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"observer command did not finish within {self.timeout_s:g}s") from None
        if completed.returncode != 0:
            raise RuntimeError(
                f"observer command exited {completed.returncode}: "
                f"{(completed.stderr or completed.stdout).strip()[:300]}"
            )
