"""The file-drop inbox (DESIGN.md §5, transport 2): a watched directory for
git-transported or air-gapped result bundles.

Each immediate subdirectory of the inbox is one bundle. A processed bundle is
moved into ``processed/`` (suffixed with a ULID) so nothing is re-processed
forever; refusals are quarantined by the service and the original is moved
the same way. Entries newer than one poll interval are left alone so a bundle
still being copied in is not ingested half-written.

The watcher is a polling loop inside `runcomposer serve` — deliberately not a
scheduler or daemon (§13 scope guard).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from runcomposer.core.ids import new_ulid
from runcomposer.service import IngestReport, QuarantineReport, Service

__all__ = ["InboxWatcher"]

PROCESSED_DIR = "processed"


class InboxWatcher:
    def __init__(self, service: Service, inbox: Path, *, poll_interval_s: float = 2.0):
        self.service = service
        self.inbox = Path(inbox)
        self.poll_interval_s = poll_interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self.inbox.mkdir(parents=True, exist_ok=True)
        (self.inbox / PROCESSED_DIR).mkdir(exist_ok=True)
        self._thread = threading.Thread(target=self._loop, name="runcomposer-inbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval_s * 2 + 1)

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_interval_s):
            try:
                self.poll_once()
            except Exception:  # a broken bundle must not kill the watcher
                continue

    # -- one deterministic pass (also the unit-test surface) -------------------

    def poll_once(self, *, min_age_s: float | None = None) -> list[IngestReport | QuarantineReport]:
        """Process every settled bundle currently in the inbox."""
        if not self.inbox.is_dir():
            return []
        quiet = self.poll_interval_s if min_age_s is None else min_age_s
        now = time.time()
        results: list[IngestReport | QuarantineReport] = []
        for entry in sorted(self.inbox.iterdir()):
            if not entry.is_dir() or entry.name == PROCESSED_DIR:
                continue
            if now - entry.stat().st_mtime < quiet:
                continue  # possibly still being copied in
            results.append(self.service.ingest_or_quarantine(entry, transport="file-drop"))
            self._archive(entry)
        return results

    def _archive(self, entry: Path) -> None:
        target_root = self.inbox / PROCESSED_DIR
        target_root.mkdir(exist_ok=True)
        entry.rename(target_root / f"{entry.name}-{new_ulid()}")
