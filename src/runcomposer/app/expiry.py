"""Expiry of dispatches that wait for results too long (DESIGN.md §4).

A dispatch handed to an external executor waits in ``AWAITING_RESULTS`` until
its results arrive. If the executor died, they never do. With
``core.dispatch.awaiting_timeout_s`` set, a served deployment closes such a
dispatch itself (``Service.expire_awaiting``): this is the loop that asks.
``runcomposer gc`` asks once, for deployments without a server.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import Service

__all__ = ["AwaitingExpiry"]

log = logging.getLogger("runcomposer.expiry")


class AwaitingExpiry:
    """A background loop inside ``serve``. Does nothing — starts no thread —
    while the option is unset."""

    def __init__(self, service: Service):
        self._service = service
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def interval_s(self) -> float | None:
        timeout = self._service.config.dispatch.get("awaiting_timeout_s")
        # Often enough that a dispatch is closed within a quarter of the
        # timeout after it ran out; never busier than once a second.
        return None if not timeout else max(1.0, min(60.0, float(timeout) / 4))

    def start(self) -> None:
        if self.interval_s() is None or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="runcomposer-expiry", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        interval = self.interval_s() or 60.0
        while not self._stop.wait(interval):
            try:
                self._service.expire_awaiting()
            except Exception:
                log.exception("expiring dispatches that await results failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
