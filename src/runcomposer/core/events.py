"""Event kinds of a run's event stream (DESIGN.md §4).

Every state change and every delivery is an event; observers are told about
the lifecycle ones. The kinds are plain strings so a store, a webhook
receiver and a shell script all read them the same way.
"""

from __future__ import annotations

__all__ = [
    "ALL_KINDS",
    "DELIVERY_ATTACHED",
    "DELIVERY_RECORDED",
    "DISPATCH_REFUSED",
    "LIFECYCLE_KINDS",
    "LOG",
    "RUN_AWAITING",
    "RUN_CANCELLED",
    "RUN_CANCEL_REQUESTED",
    "RUN_COMPLETED",
    "RUN_COMPOSED",
    "RUN_DISPATCHED",
    "RUN_FAILED",
    "RUN_RUNNING",
    "RUN_STALE",
    "STATE_EVENTS",
    "WARNING",
]

RUN_COMPOSED = "run.composed"
RUN_DISPATCHED = "run.dispatched"
RUN_RUNNING = "run.running"
RUN_AWAITING = "run.awaiting_results"
RUN_COMPLETED = "run.completed"
RUN_FAILED = "run.failed"
RUN_CANCELLED = "run.cancelled"
RUN_CANCEL_REQUESTED = "run.cancel_requested"
RUN_STALE = "run.stale"
DISPATCH_REFUSED = "dispatch.refused"
DELIVERY_RECORDED = "delivery.recorded"
DELIVERY_ATTACHED = "delivery.attached"
LOG = "log"
WARNING = "warning"

#: Every kind the event stream holds — what an observer may be told about
#: when it asks for more than the lifecycle (``events: ["*"]``). The one list
#: of kinds; ``runcomposer.testing.sample_events`` is made from it.
ALL_KINDS = (
    RUN_COMPOSED,
    RUN_DISPATCHED,
    RUN_RUNNING,
    RUN_AWAITING,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_CANCELLED,
    RUN_CANCEL_REQUESTED,
    RUN_STALE,
    DISPATCH_REFUSED,
    DELIVERY_RECORDED,
    DELIVERY_ATTACHED,
    LOG,
    WARNING,
)

#: The event a dispatch entering each state emits.
STATE_EVENTS = {
    "DISPATCHED": RUN_DISPATCHED,
    "RUNNING": RUN_RUNNING,
    "AWAITING_RESULTS": RUN_AWAITING,
    "COMPLETE": RUN_COMPLETED,
    "FAILED": RUN_FAILED,
    "CANCELLED": RUN_CANCELLED,
    "STALE": RUN_STALE,
}

#: What an observer is told about unless it asks for something else.
LIFECYCLE_KINDS = (
    RUN_COMPOSED,
    RUN_DISPATCHED,
    RUN_RUNNING,
    RUN_AWAITING,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_CANCELLED,
    RUN_STALE,
    DISPATCH_REFUSED,
    DELIVERY_RECORDED,
)
