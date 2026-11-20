"""The quarantine (DESIGN.md §4, §5): unsolicited or unverifiable deliveries
never silently become run data — they land here, visibly, for a person to
attach or promote.

A bounded directory: one subdirectory per entry holding a copy of the bundle
plus a ``_quarantine.json`` metadata file. The bound is enforced **on write**
— adding an entry beyond ``max_entries`` evicts the oldest — so nothing that
writes here can fill the disk by repetition.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from runcomposer.core.errors import NotFound
from runcomposer.core.ids import new_ulid
from runcomposer.core.timeutil import utc_now

__all__ = ["Quarantine", "QuarantineEntry", "QuarantineError"]

log = logging.getLogger("runcomposer.quarantine")

_METADATA = "_quarantine.json"


class QuarantineError(NotFound):
    """An unknown entry, or one whose bundle is gone."""

    code = "unknown-quarantine-entry"


@dataclass(frozen=True)
class QuarantineEntry:
    entry_id: str
    received_at: str
    reason: str
    transport: str  # "file-drop" | "push" | "cli"
    claimed_run_id: str | None
    content_hash: str
    #: The result format the bundle is believed to be in — ``None`` when
    #: nothing said (a file drop for an unknown run); promoting then needs one.
    format: str | None = None
    detail: str = ""
    #: Arrival order (nanoseconds). ``received_at`` has second resolution;
    #: "the oldest entry" must not be a coin toss within one second.
    sequence: int = 0


class Quarantine:
    def __init__(self, directory: str | Path, *, max_entries: int = 100):
        self._dir = Path(directory)
        self._max = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._last_sequence = 0

    @property
    def directory(self) -> Path:
        return self._dir

    def add(
        self,
        bundle: Path,
        *,
        reason: str,
        transport: str,
        content_hash: str,
        claimed_run_id: str | None = None,
        format: str | None = None,
        detail: str = "",
    ) -> QuarantineEntry | None:
        """Copy a refused bundle into quarantine. Returns ``None`` when a
        bundle with the same content hash is already there (re-drops and
        transport retries must not multiply entries)."""
        with self._lock:
            existing = self.entries()
            if any(entry.content_hash == content_hash for entry in existing):
                return None
            entry = QuarantineEntry(
                entry_id="q-" + new_ulid(),
                received_at=utc_now(),
                reason=reason,
                transport=transport,
                claimed_run_id=claimed_run_id,
                content_hash=content_hash,
                format=format,
                detail=detail[:500],
                sequence=max(time.time_ns(), self._last_sequence + 1),
            )
            self._last_sequence = entry.sequence
            entry_dir = self._dir / entry.entry_id
            target = entry_dir / "bundle"
            if bundle.is_dir():
                shutil.copytree(bundle, target)
            else:
                target.mkdir(parents=True)
                shutil.copy2(bundle, target / bundle.name)
            (entry_dir / _METADATA).write_text(
                json.dumps(asdict(entry), indent=2) + "\n", encoding="utf-8"
            )
            for evicted in self._evict(len(existing) + 1 - self._max, existing):
                log.warning(
                    "quarantine is full (max %d): evicted the oldest entry %s", self._max, evicted
                )
            return entry

    def _evict(self, count: int, oldest_first: list[QuarantineEntry]) -> list[str]:
        removed = []
        for entry in oldest_first[: max(0, count)]:
            shutil.rmtree(self._dir / entry.entry_id, ignore_errors=True)
            removed.append(entry.entry_id)
        return removed

    def entries(self) -> list[QuarantineEntry]:
        if not self._dir.is_dir():
            return []
        found = []
        for metadata in self._dir.glob("q-*/" + _METADATA):
            try:
                raw = json.loads(metadata.read_text(encoding="utf-8"))
                found.append(
                    QuarantineEntry(
                        **{key: raw.get(key) for key in QuarantineEntry.__dataclass_fields__
                           if key in raw}
                    )
                )
            except (json.JSONDecodeError, TypeError, OSError):
                continue  # half-written entry; pruning collects it
        found.sort(key=lambda entry: (entry.received_at, entry.sequence, entry.entry_id))
        return found

    def get(self, entry_id: str) -> QuarantineEntry:
        for entry in self.entries():
            if entry.entry_id == entry_id:
                return entry
        raise QuarantineError(f"unknown quarantine entry {entry_id!r}")

    def bundle_path(self, entry_id: str) -> Path:
        self.get(entry_id)
        path = self._dir / entry_id / "bundle"
        if not path.exists():
            raise QuarantineError(f"quarantine entry {entry_id!r} has no bundle payload")
        return path

    def remove(self, entry_id: str) -> None:
        self.get(entry_id)
        shutil.rmtree(self._dir / entry_id)

    def prune(self, max_entries: int | None = None) -> list[str]:
        """Drop the oldest entries beyond the bound, plus directories that
        never became an entry. Returns the removed entry ids."""
        with self._lock:
            entries = self.entries()
            bound = self._max if max_entries is None else max(0, int(max_entries))
            removed = self._evict(len(entries) - bound, entries)
            if self._dir.is_dir():
                known = {entry.entry_id for entry in entries}
                for child in self._dir.glob("q-*"):
                    if child.is_dir() and child.name not in known:
                        shutil.rmtree(child, ignore_errors=True)
            return removed
