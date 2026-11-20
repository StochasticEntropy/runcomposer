"""Housekeeping (DESIGN.md §6.4): retention, applied by ``runcomposer gc``.

Artifacts live and die with their run: when a run is pruned (or deleted) its
directory under ``core.artifact_dir`` goes with it, and a directory that
belongs to no stored run is swept. ``max_age_days: null`` means no age limit
— nothing expires by age, only ``max_runs`` (if set) prunes.
"""

from __future__ import annotations

import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .service import Service

__all__ = ["collect_garbage", "remove_run_artifacts"]

_RUN_DIR = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")  # a ULID — the only names gc sweeps


def remove_run_artifacts(artifact_dir: Path, run_id: str, private_dir: Path | None = None) -> bool:
    """Remove a run's directory under the artifact root — and, with
    ``private_dir``, what its dispatches kept in their private directories."""
    removed = False
    for base in (artifact_dir, private_dir):
        target = base / run_id if base is not None else None
        if target is not None and _RUN_DIR.fullmatch(run_id) and target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed = removed or base is artifact_dir
    return removed


def collect_garbage(service: Service) -> dict[str, Any]:
    config = service.config
    retention = config.retention
    max_age_days = retention.get("max_age_days")
    cutoff_epoch = None if max_age_days is None else time.time() - float(max_age_days) * 86400
    cutoff_iso = (
        None
        if cutoff_epoch is None
        else datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    report: dict[str, Any] = {
        "quarantine_removed": service.quarantine.prune(),
        "inbox_processed_removed": 0,
        "artifact_dirs_removed": 0,
    }
    inbox = config.inbox_dir
    processed = inbox / "processed" if inbox else None
    if cutoff_epoch is not None and processed and processed.is_dir():
        for child in processed.iterdir():
            if child.stat().st_mtime < cutoff_epoch:
                shutil.rmtree(child, ignore_errors=True)
                report["inbox_processed_removed"] += 1

    removed = service.store.prune_runs(before=cutoff_iso, max_runs=retention.get("max_runs"))
    report["runs_removed"] = removed
    artifact_dir = config.artifact_dir
    private_dir = config.private_dir
    for run_id in removed:
        report["artifact_dirs_removed"] += int(
            remove_run_artifacts(artifact_dir, run_id, private_dir)
        )
    for base in (artifact_dir, private_dir):
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if (
                child.is_dir()
                and _RUN_DIR.fullmatch(child.name)
                and service.store.get_run(child.name) is None
            ):
                shutil.rmtree(child, ignore_errors=True)
                report["artifact_dirs_removed"] += int(base is artifact_dir)
    report["expired_dispatches"] = [d.dispatch_id for d in service.expire_awaiting()]
    return report
