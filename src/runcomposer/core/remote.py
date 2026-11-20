"""Pushing progress to a runcomposer API from wherever the tests execute
(DESIGN.md §5, §6.2): the client side of ``POST /runs/{id}/live`` and
``POST /runs/{id}/log``.

An external executor — a CI job, a remote agent — holds the ingest token of
its dispatch. With it, and the run's callback URL, it can report which item
is executing now and append log lines while its work is still under way; the
results themselves come home as a bundle, as before.

Stdlib only, so that a worker or an agent can use it with nothing installed
but this package. Best-effort by design: progress that cannot be pushed is
dropped, never raised into the execution it reports about.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

__all__ = ["RemoteLiveChannel", "progress_urls"]


def progress_urls(callback_url: str) -> tuple[str, str]:
    """``(live URL, log URL)`` for a run, from any URL of the run's API
    resource: ``…/api/v1/runs/{id}`` itself or its ``…/results`` endpoint
    (the ``callback_url`` a runner hands out)."""
    base = callback_url.rstrip("/")
    for suffix in ("/results", "/live", "/log"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}/live", f"{base}/log"


class RemoteLiveChannel:
    """A ``LiveChannel`` over HTTP: ``live`` and ``log`` are POSTed to the
    run's API with the dispatch's ingest token. Picklable (its state is four
    strings and a number). ``failures`` counts what could not be pushed."""

    def __init__(
        self,
        callback_url: str,
        token: str | None = None,
        *,
        dispatch_id: str | None = None,
        timeout_s: float = 5.0,
    ):
        self.live_url, self.log_url = progress_urls(callback_url)
        self.token = token
        self.dispatch_id = dispatch_id
        self.timeout_s = float(timeout_s)
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
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                response.read()
        except (urllib.error.URLError, OSError, ValueError):
            self.failures += 1
