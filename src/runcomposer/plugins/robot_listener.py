"""The runcomposer Robot listener (DESIGN.md §6.2a): streams per-item verdicts
to the RunStore *during* execution, giving the UI live status. The terminal
output.xml parse remains the authoritative reconciliation — live rows are
cleared when the shard's real delivery lands.

Part of the robot plugin family, which owns the ``id = longname`` id space
(§6.1) — hence the listener may write longnames as item ids directly.
"""

from __future__ import annotations

__all__ = ["LiveStatusListener"]

_STATUS_MAP = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP", "NOT RUN": "SKIP"}


class LiveStatusListener:
    ROBOT_LISTENER_API_VERSION = 3

    def __init__(self, store, run_id: str, dispatch_id: str, shard: str):
        self._store = store
        self._run_id = run_id
        self._dispatch_id = dispatch_id
        self._shard = shard

    def end_test(self, data, result):  # noqa: ANN001 - robot API signature
        from runcomposer.core.model import Verdict

        try:
            self._store.record_live_verdict(
                self._run_id,
                self._dispatch_id,
                self._shard,
                Verdict(
                    item_id=result.longname,
                    status=_STATUS_MAP.get(result.status, "ERROR"),
                    duration_ms=int(result.elapsedtime),
                    message=result.message or "",
                ),
            )
        except Exception:  # streaming is best-effort; never break the run
            pass
