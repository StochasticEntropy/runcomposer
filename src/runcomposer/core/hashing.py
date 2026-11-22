"""Content hashes — the idempotency keys of DESIGN.md §5, in one place.

Two things get hashed: a bundle on disk (file names + bytes) and a list of
verdicts handed over in memory. Both produce the same ``sha256:<hex>``
spelling, and nothing else in the package rolls its own.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .model import Verdict

__all__ = ["bundle_hash", "canonical_json", "sha256_hex", "verdicts_hash"]


def sha256_hex(data: bytes) -> str:
    """The SHA-256 of ``data`` as lower-case hex."""
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> bytes:
    """Deterministic JSON bytes: sorted keys, no insignificant whitespace,
    non-ASCII kept as UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def bundle_hash(path: str | Path) -> str:
    """Hash of a whole bundle: relative file names and bytes, in name order.
    A single file hashes as a bundle of one."""
    root = Path(path)
    digest = hashlib.sha256()
    if root.is_file():
        files, base = [root], root.parent
    else:
        files, base = sorted(p for p in root.rglob("*") if p.is_file()), root
    for file in files:
        digest.update(file.relative_to(base).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file.read_bytes())
    return "sha256:" + digest.hexdigest()


def verdicts_hash(verdicts: Iterable[Verdict]) -> str:
    """Hash of verdicts delivered in memory. Every reported field takes part
    — a corrected message is a different delivery."""
    payload = [
        [v.item_id, v.status, v.duration_ms, v.message, v.attempt] for v in verdicts
    ]
    return "sha256:" + sha256_hex(canonical_json(payload))
