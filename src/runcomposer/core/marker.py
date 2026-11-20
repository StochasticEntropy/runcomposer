"""The bundle marker (DESIGN.md §5): the small file an executor writes next
to its results so that any transport carries correlation home.

``{run_id, dispatch_id?, shard?, spec_sha256?}`` — written last, which also
makes it the signal that a bundle dropped into a directory is complete.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import IngestError

__all__ = ["MARKER_FILENAME", "read_marker"]

MARKER_FILENAME = "runcomposer_run.json"


def read_marker(bundle: str | Path) -> dict[str, Any] | None:
    """The marker of a bundle directory, ``None`` when there is none (or the
    bundle is a single file). A marker that is present but unusable is a
    refused bundle, never a silently ignored one."""
    root = Path(bundle)
    marker_path = root / MARKER_FILENAME if root.is_dir() else None
    if marker_path is None or not marker_path.is_file():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise IngestError(
            f"{marker_path}: marker is not valid JSON: {exc}", reason="bad-bundle"
        ) from None
    if not isinstance(marker, dict) or not isinstance(marker.get("run_id"), str) or not marker["run_id"]:
        raise IngestError(
            f"{marker_path}: marker must be an object with a run_id", reason="bad-bundle"
        )
    for key in ("dispatch_id", "shard", "spec_sha256"):
        if key in marker and marker[key] is not None and not isinstance(marker[key], str):
            raise IngestError(f"{marker_path}: marker field {key!r} must be a string", reason="bad-bundle")
    return marker
