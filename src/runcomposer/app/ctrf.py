"""The results of a run as a CTRF document (``runcomposer export --format
ctrf``, ``GET /api/v1/runs/{id}/export?format=ctrf``; DESIGN.md §9)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from runcomposer.core import lifecycle
from runcomposer.core.timeutil import epoch_ms

if TYPE_CHECKING:
    from .service import Service

__all__ = ["export_ctrf"]


def export_ctrf(service: Service, run_id: str) -> dict[str, Any]:
    """A normalized cross-tool result document (CTRF) of the run's latest
    dispatch — effective verdicts, ``MISSING`` reported as ``other``."""
    run = service.require_run(run_id)
    dispatch = run.latest_dispatch
    verdicts = (
        lifecycle.effective_verdicts(service.ledger.scored_verdicts(dispatch)) if dispatch else []
    )
    status_map = {
        "PASS": "passed", "FAIL": "failed", "SKIP": "skipped", "ERROR": "other",
        "MISSING": "other",
    }
    tests = []
    for verdict in verdicts:
        test: dict[str, Any] = {
            "name": verdict.item_id,
            "status": status_map[verdict.status],
            "duration": verdict.duration_ms,
        }
        if verdict.status in ("ERROR", "MISSING"):
            test["rawStatus"] = verdict.status
        if verdict.message:
            test["message"] = verdict.message
        if verdict.shard:
            test["extra"] = {"shard": verdict.shard, "attempt": verdict.attempt}
        tests.append(test)
    counts = dict.fromkeys(("passed", "failed", "skipped", "pending", "other"), 0)
    for test in tests:
        counts[test["status"]] += 1
    return {
        "results": {
            "tool": {"name": "runcomposer"},
            "summary": {
                "tests": len(tests),
                **counts,
                "start": epoch_ms(run.created_at),
                "stop": epoch_ms(run.completed_at),
            },
            "tests": tests,
            "extra": {"run_id": run.id, "state": run.state, "completion": run.completion},
        }
    }
