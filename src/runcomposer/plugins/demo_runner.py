"""`demo` Runner — fakes executions so the compose→dispatch→results loop is
demonstrable without any test framework (DESIGN.md §12).

Verdicts are deterministic in ``(seed, item id)``: re-dispatching with the
same seed reproduces the same delivery byte-for-byte, while a different seed
lets re-runs flip FAIL→PASS the way real re-runs do. Durations are stable per
item (hash-derived), so duration-based features have consistent demo data.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any, Mapping

from runcomposer.core.ids import new_ulid
from runcomposer.core.model import Verdict
from runcomposer.core.ports import DispatchHandle, RunnerInfo

__all__ = ["DemoRunner"]

_FAIL_RATE = 0.15
_SKIP_RATE = 0.03


class DemoRunner:
    runner_id = "demo"

    def __init__(self, seed: str = "demo"):
        self.seed = seed
        # Deliveries a real deployment would receive through ingestion (§5);
        # the demo loop reads them directly since P0 has no store yet.
        self.deliveries: list[dict[str, Any]] = []

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id=self.runner_id, capabilities=("fake-execution",))

    def dispatch(self, spec: Mapping[str, Any]) -> DispatchHandle:
        try:
            run_id = spec["run"]["id"]
            item_ids = spec["selection"]["materialized"]["item_ids"]
        except (KeyError, TypeError):
            raise ValueError(
                "spec is not dispatchable: run.id and selection.materialized.item_ids required "
                "(executor contract, DESIGN.md §3.3)"
            ) from None
        dispatch_id = new_ulid()
        verdicts = [self._fake_verdict(item_id) for item_id in item_ids]
        self.deliveries.append(
            {"run_id": run_id, "dispatch_id": dispatch_id, "shard": "1", "verdicts": verdicts}
        )
        return DispatchHandle(dispatch_id=dispatch_id, shards=1)

    def _fake_verdict(self, item_id: str) -> Verdict:
        roll = random.Random(f"{self.seed}|{item_id}").random()
        digest = hashlib.sha256(item_id.encode("utf-8")).digest()
        duration_ms = 80 + int.from_bytes(digest[:2], "big") % 3920
        if roll < _FAIL_RATE:
            return Verdict(item_id, "FAIL", duration_ms, message="simulated failure (demo runner)")
        if roll < _FAIL_RATE + _SKIP_RATE:
            return Verdict(item_id, "SKIP", 0, message="simulated skip (demo runner)")
        return Verdict(item_id, "PASS", duration_ms)
