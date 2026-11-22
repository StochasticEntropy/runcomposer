"""The file-drop inbox (DESIGN.md §5, transport 2): a watched directory for
bundles that travel by git, by copy, or across an air gap.

**The drop protocol.** Each immediate subdirectory of the inbox is one
bundle. A bundle is taken in when it is *complete*, which the watcher knows
in one of two ways:

1. it contains the ``runcomposer_run.json`` marker (``runcomposer-exec``
   writes it last) or an empty ``.done`` file — the sender's own signal; or
2. nothing in it changed — no file added, removed, resized or touched — for
   ``core.ingestion.settle_s`` (default: two poll intervals). This is the
   fallback for senders that cannot write a signal, and it is why a slow
   copy is not ingested half-written: every file's size and modification
   time is part of what must hold still, not the directory's alone.

A processed bundle is moved into ``processed/`` whatever happened to it —
ingested or quarantined — so one bad bundle can never block the ones sorted
after it.

The watcher is a polling loop inside ``runcomposer serve`` — deliberately not
a scheduler or daemon (§13 scope guard).
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from runcomposer.core.ids import new_ulid
from runcomposer.core.marker import MARKER_FILENAME

from .ingestion import IngestReport, QuarantineReport
from .redaction import log_failure

if TYPE_CHECKING:
    from .service import Service

__all__ = ["DONE_FILENAME", "InboxWatcher"]

log = logging.getLogger("runcomposer.inbox")

PROCESSED_DIR = "processed"
DONE_FILENAME = ".done"

_Snapshot = tuple[tuple[str, int, int], ...]


def _snapshot(bundle: Path) -> _Snapshot:
    entries = []
    for file in sorted(bundle.rglob("*")):
        try:
            stat = file.stat()
        except OSError:
            continue
        entries.append((file.relative_to(bundle).as_posix(), stat.st_size, stat.st_mtime_ns))
    return tuple(entries)


class InboxWatcher:
    def __init__(
        self,
        service: Service,
        inbox: Path,
        *,
        poll_interval_s: float = 2.0,
        settle_s: float | None = None,
    ):
        self.service = service
        self.inbox = Path(inbox)
        self.poll_interval_s = poll_interval_s
        self.settle_s = poll_interval_s * 2 if settle_s is None else float(settle_s)
        self._seen: dict[str, tuple[_Snapshot, float]] = {}
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
            except Exception as exc:  # the watcher outlives anything a bundle can do
                log_failure(log, "inbox poll failed", exc, self.service.redact)

    # -- one deterministic pass (also the unit-test surface) -------------------

    def is_complete(self, bundle: Path, now: float) -> bool:
        if (bundle / MARKER_FILENAME).is_file() or (bundle / DONE_FILENAME).is_file():
            return True
        snapshot = _snapshot(bundle)
        seen = self._seen.get(bundle.name)
        if seen is None or seen[0] != snapshot:
            self._seen[bundle.name] = (snapshot, now)  # changed: the clock restarts
            return False
        return now - seen[1] >= self.settle_s

    def poll_once(self, *, now: float | None = None) -> list[IngestReport | QuarantineReport]:
        """Process every complete bundle currently in the inbox. ``now`` is
        the clock reading to judge stillness by (tests pass their own)."""
        if not self.inbox.is_dir():
            return []
        clock = time.monotonic() if now is None else now
        results: list[IngestReport | QuarantineReport] = []
        present = set()
        for entry in sorted(self.inbox.iterdir()):
            if not entry.is_dir() or entry.name == PROCESSED_DIR:
                continue
            present.add(entry.name)
            if not self.is_complete(entry, clock):
                continue
            try:
                result = self.service.ingestion.ingest_or_quarantine(entry, transport="file-drop")
                results.append(result)
                if isinstance(result, QuarantineReport):
                    log.warning(
                        "inbox bundle %s quarantined (%s): %s",
                        entry.name, result.reason, result.detail,
                    )
                else:
                    log.info(
                        "inbox bundle %s ingested into run %s (%s)",
                        entry.name, result.run_id, result.outcome,
                    )
            except Exception as exc:
                log_failure(log, f"inbox bundle {entry.name} could not be processed", exc, self.service.redact)
            finally:
                self._archive(entry)
                self._seen.pop(entry.name, None)
        for name in set(self._seen) - present:
            del self._seen[name]
        return results

    def _archive(self, entry: Path) -> None:
        target_root = self.inbox / PROCESSED_DIR
        target_root.mkdir(exist_ok=True)
        try:
            entry.rename(target_root / f"{entry.name}-{new_ulid()}")
        except OSError as exc:
            # Cannot move it: remove it, so it is not processed forever.
            log_failure(
                log, f"could not archive inbox bundle {entry.name} — removing it", exc, self.service.redact
            )
            shutil.rmtree(entry, ignore_errors=True)
