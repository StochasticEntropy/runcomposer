"""Logging for the command line: one handler on the ``runcomposer`` logger,
writing to stderr.

Every module logs under ``runcomposer.*`` through the standard ``logging``
module; this is only the default *output* the CLI gives those loggers. An
application embedding runcomposer configures logging itself and never calls
this.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

__all__ = ["configure_logging"]


class _CurrentStderr(logging.StreamHandler):  # type: ignore[type-arg, unused-ignore]
    """Writes to whatever ``sys.stderr`` is when the record is emitted — not
    to the stream that happened to be there when logging was set up. `main`
    may be called more than once in a process (tests, an embedding script),
    each time with a different stderr."""

    def __init__(self) -> None:
        logging.Handler.__init__(self)
        self.quiet_events = True

    def filter(self, record: logging.LogRecord) -> bool:
        return _not_a_repeated_event(record, self.quiet_events) and bool(super().filter(record))

    @property
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, _value: Any) -> None:
        return None


def configure_logging(*, level: int, server: bool = False) -> None:
    """Idempotent: one handler, (re)configured on every call.

    For a command: ``LEVEL logger: message``, warnings by default and what
    runcomposer does with ``--verbose``. For ``serve``: the same with a UTC
    timestamp in front, at ``--log-level``.
    """
    logger = logging.getLogger("runcomposer")
    handler = next((h for h in logger.handlers if isinstance(h, _CurrentStderr)), None)
    if handler is None:
        handler = _CurrentStderr()
        logger.addHandler(handler)
    if server:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%SZ"
        )
        formatter.converter = time.gmtime  # UTC, like every time runcomposer stores
    else:
        formatter = logging.Formatter("%(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.setLevel(level)
    # Warnings recorded on a run are printed by the command that caused them;
    # unless asked for more, the event log would only repeat them. Done on
    # this handler, not on the logger, so nobody else's logging is changed.
    handler.quiet_events = not (server or level <= logging.INFO)


def _not_a_repeated_event(record: logging.LogRecord, quiet: bool) -> bool:
    return not (quiet and record.name == "runcomposer.events" and record.levelno < logging.ERROR)
