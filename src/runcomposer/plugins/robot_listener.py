"""The runcomposer Robot listener (DESIGN.md §6.2a): streams per-item status
while a shard is still executing.

It writes to a ``LiveChannel`` — a picklable handle the dispatch context
gives the runner — so it works in a worker process and with any RunStore.
The parse of the finished output is the authoritative result; live status is
replaced by it when the shard delivers.

Part of the Robot plugin family, which owns the ``id = longname`` id space
(§6.1) — hence the listener reports longnames as item ids.
"""

from __future__ import annotations

from typing import Any

__all__ = ["LiveStatusListener"]

_STATUS_MAP = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP", "NOT RUN": "SKIP"}


class LiveStatusListener:
    ROBOT_LISTENER_API_VERSION = 3

    def __init__(self, channel: Any, shard: str):
        self._channel = channel
        self._shard = shard

    def end_test(self, data: Any, result: Any) -> None:
        try:
            self._channel.live(
                self._shard,
                result.longname,
                _STATUS_MAP.get(result.status, "ERROR"),
                duration_ms=int(result.elapsedtime),
                message=result.message or "",
            )
        except Exception:  # streaming is best-effort; it never breaks the run
            pass
