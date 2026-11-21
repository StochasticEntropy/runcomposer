"""What a worker reported before its shard is delivered is in the run's event
stream before that delivery (DESIGN.md §6.2).

A worker's lines travel through the live channel's files, which two readers
drain: a follower thread, every moment, and each delivery, just before it
records. A delivery therefore waits for a line the follower has already taken
out of the file but not yet put into the stream — otherwise the line would
land after the delivery it preceded.
"""

from __future__ import annotations

import threading
from typing import Any

from runcomposer.sdk import DispatchContext, RunnerBase, RunnerInfo, Verdict

from .conftest import compose

HERE = "tests.test_live_lines_before_delivery"
LINE = "shard checkout: finished"

# Set by the test around one dispatch: the follower is about to put the line
# into the stream / the runner's delivery has returned.
taken = threading.Event()
delivered = threading.Event()


class OneShardRunner(RunnerBase):
    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="one-shard")

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare({"checkout": list(ctx.item_ids)})
        ctx.live_channel().log(LINE)
        # Deliver only once the follower thread holds the line.
        assert taken.wait(30), "the follower never picked the line up"
        ctx.deliver("checkout", verdicts=[Verdict(item, "PASS", 3) for item in ctx.item_ids])
        delivered.set()


def test_a_line_the_follower_holds_is_in_the_stream_before_the_delivery(make_service, monkeypatch):
    service = make_service(runners={"one-shard": {"module": f"{HERE}:OneShardRunner"}})
    run_id = compose(service, "Auth-SSO")
    taken.clear()
    delivered.clear()
    emit = service.bus.emit

    def holding_emit(run: str, kind: str, *args: Any, **kwargs: Any) -> Any:
        if (kwargs.get("data") or {}).get("message") == LINE:
            taken.set()
            # Held back for as long as the delivery could use to overtake it:
            # a delivery that waits for the line is still waiting when this
            # gives up; one that does not has returned long before.
            delivered.wait(1.0)
        return emit(run, kind, *args, **kwargs)

    monkeypatch.setattr(service.bus, "emit", holding_emit)
    service.dispatch(run_id, "one-shard", foreground=True)

    kinds = [
        "line" if event.data.get("message") == LINE else event.kind
        for event in service.store.events(run_id)
        if event.kind == "delivery.recorded" or event.data.get("message") == LINE
    ]
    assert kinds == ["line", "delivery.recorded"]
    assert service.require_run(run_id).state == "COMPLETE"
