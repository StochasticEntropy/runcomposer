"""The dispatch context used from several threads at once (DESIGN.md §6.2).

A runner's workers deliver their shards, log, record artifacts, stream live
status and set links from threads of their own. Every one of those calls is
safe to make concurrently, on the real context and on the kit's fake: nothing
is lost and nothing is recorded twice. (What stays the runner's to order: one
shard is delivered by one thread, and ``declare`` comes first.)
"""

from __future__ import annotations

import threading
from collections import Counter
from typing import Any

from runcomposer.sdk import DispatchContext, RunnerBase, RunnerInfo, Verdict
from runcomposer.testing import FakeDispatchContext

from .conftest import compose

HERE = "tests.test_context_threads"
THREADS = 12
LOGS_PER_THREAD = 20
WORKER_LINES = 3000


class ThreadedRunner(RunnerBase):
    """One shard per thread; every thread starts at the same instant."""

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="threaded")

    def dispatch(self, ctx: DispatchContext) -> None:
        items = list(ctx.item_ids)
        shards = {f"lane-{n:02d}": items[n::THREADS] for n in range(THREADS)}
        ctx.declare(shards)
        # What a worker process would have written: log lines in the live
        # files, which every delivery drains before it records.
        channel = ctx.live_channel()
        for line in range(WORKER_LINES):
            channel.log(f"worker line {line}")
        barrier = threading.Barrier(THREADS)
        errors: list[BaseException] = []

        def lane(n: int) -> None:
            shard = f"lane-{n:02d}"
            try:
                barrier.wait(30)
                ctx.set_link(f"build-{n:02d}", f"https://ci.shop.example/job/shop/{n}")
                for item in shards[shard]:
                    ctx.live(shard, item, "RUNNING")
                for line in range(LOGS_PER_THREAD):
                    ctx.log(f"{shard} line {line}")
                (ctx.workdir / f"{shard}.log").write_text(shard, encoding="utf-8")
                ctx.add_artifact(f"log ({shard})", ctx.workdir / f"{shard}.log", shard=shard)
                ctx.add_artifact(f"report ({shard})", f"https://ci.shop.example/{shard}", shard=shard)
                ctx.deliver(shard, verdicts=[Verdict(item, "PASS", 3) for item in shards[shard]])
            except BaseException as exc:  # surfaced below, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=lane, args=(n,)) for n in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        if errors:
            raise errors[0]


def messages(texts: list[str]) -> Counter[str]:
    return Counter(texts)


def all_events(service: Any, run_id: str) -> list[Any]:
    """The whole event stream, page by page."""
    events: list[Any] = []
    while True:
        page = service.store.events(run_id, after=events[-1].seq if events else 0, limit=500)
        if not page:
            return events
        events.extend(page)


def expected_messages() -> Counter[str]:
    wanted = [f"lane-{n:02d} line {line}" for n in range(THREADS) for line in range(LOGS_PER_THREAD)]
    wanted += [f"worker line {line}" for line in range(WORKER_LINES)]
    return Counter(wanted)


def test_a_real_dispatch_loses_and_duplicates_nothing(make_service):
    service = make_service(runners={"threaded": {"module": f"{HERE}:ThreadedRunner"}})
    run_id = compose(service, "Regression")
    planned = service.ledger.planned_ids(run_id)
    assert len(planned) >= THREADS, "every lane has items"
    service.dispatch(run_id, "threaded", foreground=True)

    run = service.require_run(run_id)
    (dispatch,) = run.dispatches
    assert dispatch.state == "COMPLETE" and dict(dispatch.summary) == {"PASS": len(planned)}
    assert sorted(service.store.delivered_shards(run_id, dispatch.dispatch_id)) == [
        f"lane-{n:02d}" for n in range(THREADS)
    ]
    verdicts = service.store.verdicts_for(run_id, dispatch.dispatch_id)
    assert sorted(v.item_id for v in verdicts) == sorted(planned), "each item once"
    assert dict(dispatch.links) == {
        f"build-{n:02d}": f"https://ci.shop.example/job/shop/{n}" for n in range(THREADS)
    }
    artifacts = service.store.artifact_refs(run_id)
    assert len(artifacts) == 2 * THREADS and len({a.name for a in artifacts}) == 2 * THREADS
    logged = [
        str(event.data["message"]) for event in all_events(service, run_id)
        if event.kind == "log" and not str(event.data["message"]).startswith("link ")
    ]
    assert messages(logged) == expected_messages()


def test_the_fake_context_loses_and_duplicates_nothing():
    items = [f"Shop.Checkout.Lane.T{n:03d}" for n in range(THREADS * 3)]
    with FakeDispatchContext(items) as ctx:
        assert ctx.run(ThreadedRunner()) == "complete"
        assert sorted(ctx.delivered_shards()) == [f"lane-{n:02d}" for n in range(THREADS)]
        assert sorted(v.item_id for v in ctx.verdicts) == sorted(items)
        assert {d.outcome for d in ctx.deliveries} == {"new"} and len(ctx.deliveries) == THREADS
        assert len(ctx.artifacts) == 2 * THREADS
        assert dict(ctx.links) == {
            f"build-{n:02d}": f"https://ci.shop.example/job/shop/{n}" for n in range(THREADS)
        }
        running: Any = Counter((s.shard, s.item_id) for s in ctx.live_updates if s.status == "RUNNING")
        assert len(running) == len(items) and set(running.values()) == {1}
        assert messages([message for _level, message in ctx.logs]) == expected_messages()
