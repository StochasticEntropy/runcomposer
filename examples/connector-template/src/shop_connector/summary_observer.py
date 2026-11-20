"""The ``run-summary`` observer: one line per finished run, appended to a
text file — the smallest useful completion hook.

``runcomposer.sdk.RunObserver`` is one method, ``notify(event)``. It is
called on a thread of its own, in event order, for the event kinds the
config entry asks for (by default the lifecycle events). Whatever it raises
is recorded on the run as a warning and changes nothing else.
"""

from __future__ import annotations

import threading
from pathlib import Path

from runcomposer.sdk import ObserverBase, RunEvent

__all__ = ["RunSummaryObserver"]

#: The events that end a dispatch.
_FINAL = {"run.completed", "run.failed", "run.cancelled", "run.stale", "dispatch.refused"}


class RunSummaryObserver(ObserverBase):
    PATH_OPTIONS = ("path",)

    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = threading.Lock()

    def notify(self, event: RunEvent) -> None:
        if event.kind not in _FINAL:
            return
        data = event.data
        outcome = data.get("completion") or data.get("state") or event.kind
        detail = data.get("error") or data.get("reason") or ""
        line = f"{event.at}  {event.run_id}  {event.kind:<16} {outcome}"
        if detail:
            line += f"  — {detail}"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
