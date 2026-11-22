"""The event stream and the observer port (DESIGN.md §4, §6.5).

Every state change and delivery becomes a ``RunEvent``: appended to the
store (the stream a client reads with a cursor), handed to the configured
observers, and announced to anything in this process waiting for it.

Observers are called on a thread of their own, in order. A slow webhook
delays other notifications, never a dispatch; an observer that raises is
recorded as a warning event and logged — it cannot affect a run.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from runcomposer.core import events as kinds
from runcomposer.core.model import RunEvent
from runcomposer.core.timeutil import utc_now

from .redaction import error_summary, log_failure

__all__ = ["EventBus", "ObserverBinding"]

log = logging.getLogger("runcomposer.events")


@dataclass(frozen=True)
class ObserverBinding:
    key: str
    observer: Any
    #: fnmatch patterns over event kinds; lifecycle events when not configured.
    kinds: tuple[str, ...] = kinds.LIFECYCLE_KINDS

    def wants(self, kind: str) -> bool:
        return any(fnmatchcase(kind, pattern) for pattern in self.kinds)


class EventBus:
    def __init__(
        self,
        store: Any,
        observers: Sequence[ObserverBinding] = (),
        *,
        redact: Callable[[Any], Any] | None = None,
        redact_text: Callable[[Any], Any] | None = None,
    ):
        self._store = store
        #: Blanks configured secrets out of free text: an observer's failure
        #: as it is logged.
        self._redact_text = redact_text or (lambda value: value)
        #: Blanks configured secrets out of whatever a runner put into an
        #: event — a refusal reason, a log line — before it is stored, logged
        #: or handed to an observer.
        self._redact = redact or (lambda value: value)
        self._observers = tuple(observers)
        self._changed = threading.Condition()
        self._queue: queue.Queue[RunEvent | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._pending = 0
        self._idle = threading.Condition()

    # -- emitting -----------------------------------------------------------------

    def emit(
        self,
        run_id: str,
        kind: str,
        *,
        dispatch_id: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> RunEvent:
        event: RunEvent = self._store.append_event(
            run_id,
            kind=kind,
            at=utc_now(),
            dispatch_id=dispatch_id,
            data=self._redact(dict(data or {})),
        )
        log.log(
            logging.WARNING if kind == kinds.WARNING else logging.INFO,
            "%s run=%s dispatch=%s %s",
            kind,
            run_id,
            dispatch_id or "-",
            _brief(event.data),
        )
        if any(binding.wants(kind) for binding in self._observers):
            with self._idle:
                self._pending += 1
            self._ensure_thread()
            self._queue.put(event)
        with self._changed:
            self._changed.notify_all()
        return event

    # -- waiting (in-process) ---------------------------------------------------------

    def wait(self, predicate: Callable[[], bool], timeout: float | None = None) -> bool:
        """Block until ``predicate()`` is true, re-checking after every event
        emitted in this process and at least twice a second — so a change made
        by another process is seen too. Returns the predicate's last value."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while True:
                if predicate():
                    return True
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._changed.wait(0.5 if remaining is None else min(0.5, remaining))

    # -- observers ----------------------------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._deliver, name="runcomposer-observers", daemon=True
            )
            self._thread.start()

    def _deliver(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                return
            try:
                for binding in self._observers:
                    if binding.wants(event.kind):
                        self._notify(binding, event)
            finally:
                with self._idle:
                    self._pending -= 1
                    self._idle.notify_all()

    def _notify(self, binding: ObserverBinding, event: RunEvent) -> None:
        try:
            binding.observer.notify(event)
        except Exception as exc:  # the port's promise: never affects the run
            # An observer may quote what it was configured with — a hook's
            # address, a token: what is logged and what is recorded are
            # blanked like everything else a plugin says.
            log_failure(
                log, f"observer {binding.key!r} failed on {event.kind}", exc, self._redact_text,
                level=logging.WARNING,
            )
            try:
                self._store.append_event(
                    event.run_id,
                    kind=kinds.WARNING,
                    at=utc_now(),
                    dispatch_id=event.dispatch_id,
                    data=self._redact(
                        {
                            "message": f"observer {binding.key!r} failed on {event.kind}: "
                            f"{error_summary(exc)}",
                            "observer": binding.key,
                        }
                    ),
                )
            except Exception as failure:
                log_failure(log, "could not record the observer failure", failure, self._redact_text)

    def flush(self, timeout: float | None = 10.0) -> bool:
        """Wait until every queued notification was delivered."""
        with self._idle:
            return self._idle.wait_for(lambda: self._pending == 0, timeout)

    def close(self) -> None:
        self.flush()
        if self._thread is not None and self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=5)


def _brief(data: Mapping[str, Any]) -> str:
    parts = []
    for key in ("state", "completion", "shard", "outcome", "reason", "message", "error"):
        if data.get(key):
            parts.append(f"{key}={str(data[key])[:200]}")
    return " ".join(parts)
