"""`demo` Runner — **simulated** execution, so the compose → dispatch →
results loop can be shown without any test framework (DESIGN.md §12).

No test is run. Verdicts are deterministic in ``(seed, item id)``:
re-dispatching with the same seed reproduces the same delivery, a different
seed lets re-runs flip FAIL→PASS the way real re-runs do. Durations are
stable per item (hash-derived), so duration-based features have consistent
demo data. It says so itself: the ``simulated`` capability, and every failure
message.

``seed`` is a per-run parameter — `runcomposer demo` seeds several different
completed runs through one configured runner with it. ``delay_s`` (configured
only) makes each item take that long, streaming live status meanwhile, so
progress and cancellation can be watched.
"""

from __future__ import annotations

import hashlib
import random
import time

from runcomposer.sdk import DispatchContext, Health, Parameter, RunnerBase, RunnerInfo, Verdict

__all__ = ["DemoRunner"]

_FAIL_RATE = 0.15
_SKIP_RATE = 0.03


class DemoRunner(RunnerBase):
    def __init__(self, seed: str = "demo", delay_s: float = 0.0):
        self.seed = seed
        self.delay_s = float(delay_s)

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="demo",
            title="Demo (simulated)",
            description="Simulates an execution: no test is run, verdicts are derived from a seed.",
            capabilities=("simulated", "live_status", "cancel"),
            parameters=(
                Parameter(
                    "seed",
                    kind="string",
                    label="Seed",
                    help="Same seed, same simulated verdicts; change it to see results flip.",
                    default=self.seed,
                ),
            ),
            result_format="runcomposer-verdicts",
        )

    def health(self) -> Health:
        return Health(status="ok", detail="simulated — always available")

    def dispatch(self, ctx: DispatchContext) -> None:
        seed = str(ctx.options.get("seed") or self.seed)
        ctx.declare(
            {"1": list(ctx.item_ids)},
            plan=f"demo runner: simulating {len(ctx.item_ids)} item(s) with seed {seed!r}",
        )
        verdicts = []
        for item_id in ctx.item_ids:
            if self.delay_s:
                deadline = time.monotonic() + self.delay_s
                while time.monotonic() < deadline:
                    ctx.raise_if_cancelled()
                    time.sleep(min(0.05, self.delay_s))
            ctx.raise_if_cancelled()
            verdict = fake_verdict(item_id, seed)
            verdicts.append(verdict)
            if self.delay_s:
                ctx.live("1", item_id, verdict.status, duration_ms=verdict.duration_ms)
        ctx.deliver("1", verdicts=verdicts, format="runcomposer-verdicts")


def fake_verdict(item_id: str, seed: str) -> Verdict:
    roll = random.Random(f"{seed}|{item_id}").random()
    digest = hashlib.sha256(item_id.encode("utf-8")).digest()
    duration_ms = 80 + int.from_bytes(digest[:2], "big") % 3920
    if roll < _FAIL_RATE:
        return Verdict(item_id, "FAIL", duration_ms, message="simulated failure (demo runner)")
    if roll < _FAIL_RATE + _SKIP_RATE:
        return Verdict(item_id, "SKIP", 0, message="simulated skip (demo runner)")
    return Verdict(item_id, "PASS", duration_ms)
