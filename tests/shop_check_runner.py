"""A connector's own runner for checks that are plain Python functions —
no test framework — executed in supervised worker processes. Written
against ``runcomposer.sdk`` alone; the twin of the example in
docs/connectors.md §9.2, run by tests/test_worker_supervisor.py.

Below the runner: the web shop's check functions it is configured with.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

from runcomposer.sdk import (
    DispatchContext,
    PlanContext,
    PlanPreview,
    RunnerBase,
    RunnerInfo,
    Verdict,
    WorkerOutcome,
    WorkerPayload,
    load_import_path,
    plan_chunks,
    supervise_workers,
)


def run_checks(payload: WorkerPayload, stop: Callable[[], bool]) -> list[dict]:
    """In the worker process: each item is one call of the configured check."""
    check = load_import_path(payload.data["check"])
    results = []
    for item_id in payload.item_ids:
        if stop():
            break
        started = time.monotonic()
        try:
            check(item_id)
            status, message = "PASS", ""
        except AssertionError as exc:
            status, message = "FAIL", str(exc)
        results.append({"item_id": item_id, "status": status, "message": message,
                        "duration_ms": int((time.monotonic() - started) * 1000)})
    return results


class CheckRunner(RunnerBase):
    def __init__(self, check: str, shards: int = 2, timeout_s: float | None = None):
        self.check, self.shards, self.timeout_s = check, int(shards), timeout_s

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="checks", capabilities=("cancel", "plan", "shards"),
                          result_format="runcomposer-verdicts")

    def plan(self, ctx: PlanContext) -> PlanPreview:
        chunks = plan_chunks(ctx.item_ids, self.shards, ctx.history.durations()).chunks
        shards = {str(index): list(chunk) for index, chunk in enumerate(chunks, start=1)}
        return PlanPreview(text=f"{len(ctx.item_ids)} check(s) in {len(shards)} shard(s)", shards=shards)

    def dispatch(self, ctx: DispatchContext) -> None:
        preview = self.plan(ctx)
        ctx.declare(preview.shards, plan=preview.text)
        payloads = [WorkerPayload(shard=shard, out_dir=str(ctx.workdir / shard), item_ids=tuple(items),
                                  data={"check": self.check})
                    for shard, items in preview.shards.items()]
        problems: list[str] = []

        def finished(payload: WorkerPayload, outcome: WorkerOutcome) -> None:
            if outcome.problem:
                problems.append(outcome.problem)
            else:
                ctx.deliver(payload.shard, verdicts=[Verdict(**result) for result in outcome.result])

        supervise_workers(payloads, run_checks, max_workers=self.shards, on_done=finished,
                          cancelled=lambda: ctx.cancelled, timeout_s=self.timeout_s, log=ctx.log)
        ctx.raise_if_cancelled()
        if problems:
            raise RuntimeError("; ".join(problems))


# -- the shop's checks ---------------------------------------------------------------------

#: The one card the shop's payment provider declines.
DECLINED = "Shop.Payments.Cards.T002"


def card_check(item_id: str) -> None:
    """A check is a function of the item id: it returns, or fails with an
    ``AssertionError``. The worker runs it in the working directory and
    environment it was given."""
    assert item_id != DECLINED, "the payment provider declined the card"


def card_check_that_hangs(item_id: str) -> None:
    """As :func:`card_check`, but the check of one card never comes back
    (until a minute is over): what a cancellation has to stop. It says so in
    ``$SHOP_CHECKS_DIR`` when it starts hanging."""
    if item_id == "Shop.Payments.Cards.T004":
        Path(os.environ["SHOP_CHECKS_DIR"], "hanging").write_text(str(os.getpid()), encoding="utf-8")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            time.sleep(0.01)
    card_check(item_id)
