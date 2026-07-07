"""The quarantine inbox (DESIGN.md §4, §5): unsolicited or unverifiable
deliveries never silently become run data — they land here, visibly, for a
human to attach or promote.

Persistence is a bounded directory (§5/§6.4 speak of quarantine *dirs*, and
§6.3 keeps the store schema normative): one subdirectory per entry holding a
copy of the offending bundle plus a ``_quarantine.json`` metadata file. The
bound is enforced by ``runcomposer gc``.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from runcomposer.core.ids import new_ulid

__all__ = ["Quarantine", "QuarantineEntry", "QuarantineError"]

_METADATA = "_quarantine.json"


class QuarantineError(ValueError):
    """Raised for unknown entries or unusable quarantined bundles."""


@dataclass(frozen=True)
class QuarantineEntry:
    entry_id: str
    received_at: str
    reason: str
    transport: str  # "file-drop" | "push" | "cli"
    claimed_run_id: str | None
    content_hash: str
    format: str


class Quarantine:
    def __init__(self, directory: str | Path):
        self._dir = Path(directory)

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
        format: str = "runcomposer-verdicts",
    ) -> QuarantineEntry | None:
        """Copy a refused bundle into quarantine. Returns None when a bundle
        with the same content hash is already quarantined (re-drops and
        transport retries must not multiply entries)."""
        for existing in self.entries():
            if existing.content_hash == content_hash:
                return None
        entry = QuarantineEntry(
            entry_id="q-" + new_ulid(),
            received_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            reason=reason,
            transport=transport,
            claimed_run_id=claimed_run_id,
            content_hash=content_hash,
            format=format,
        )
        entry_dir = self._dir / entry.entry_id
        bundle_target = entry_dir / "bundle"
        if bundle.is_dir():
            shutil.copytree(bundle, bundle_target)
        else:
            bundle_target.mkdir(parents=True)
            shutil.copy2(bundle, bundle_target / bundle.name)
        (entry_dir / _METADATA).write_text(
            json.dumps(asdict(entry), indent=2) + "\n", encoding="utf-8"
        )
        return entry

    def entries(self) -> list[QuarantineEntry]:
        if not self._dir.is_dir():
            return []
        found = []
        for metadata in sorted(self._dir.glob("q-*/" + _METADATA)):
            try:
                found.append(QuarantineEntry(**json.loads(metadata.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, TypeError):
                continue  # half-written entry; gc's age pruning will collect it
        found.sort(key=lambda entry: (entry.received_at, entry.entry_id))
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

    def prune(self, max_entries: int) -> list[str]:
        """Drop the oldest entries beyond the configured bound (§5). Returns
        the removed entry ids."""
        entries = self.entries()
        removed = []
        for entry in entries[: max(0, len(entries) - max_entries)]:
            shutil.rmtree(self._dir / entry.entry_id, ignore_errors=True)
            removed.append(entry.entry_id)
        return removed
