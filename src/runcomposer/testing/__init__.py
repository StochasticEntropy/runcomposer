"""runcomposer.testing — the conformance kit for connector authors.

Run the contract against your own plugins, in your own test suite, with any
test runner (nothing here imports pytest)::

    from runcomposer.testing import (
        ConnectorHarness, FakeDispatchContext,
        check_observer, check_parser, check_runner, check_source, check_store,
    )

- ``check_source(source)``, ``check_parser(parser, bundle)``,
  ``check_runner(runner, ctx)``, ``check_store(make_store)``,
  ``check_observer(observer)`` — each runs the invariants of one port and
  raises ``AssertionError`` whose message begins with the violated rule in
  brackets: ``[runner.delivers-declared-shards] delivered shard 'eu', which
  was not declared (['us'])``.
- ``FakeDispatchContext`` — a recording, in-memory ``DispatchContext`` for
  unit tests of a runner; the order of calls is enforced by the same code as
  in runcomposer.
- ``ConnectorHarness`` — a real ``Service`` in a temporary directory that
  drives a runner through compose → dispatch → settled, synchronously, with
  a timeout; read back runs, dispatches, verdicts, events and artifacts.

Every reference plugin of runcomposer is tested with this kit — that is the
evidence that it is enough to test a connector with.

Stability: the names in ``__all__`` keep their meaning within a minor line,
like ``runcomposer.sdk``; rules may be added to the checks in a minor
release (a connector that broke the contract may then start failing them).
This surface is separate from ``runcomposer.sdk`` on purpose: plugins import
the SDK at run time, tests import the kit.
"""

from __future__ import annotations

from .checks import (
    DEFAULT_ITEM_IDS,
    check_observer,
    check_parser,
    check_runner,
    check_source,
    check_store,
    sample_events,
    xml_with_dtd,
)
from .fake import Delivery, FakeDispatchContext, FakeHistory, ListSource
from .harness import ConnectorHarness, DispatchHandle, HarnessRun, plugin

__all__ = [
    "DEFAULT_ITEM_IDS",
    "ConnectorHarness",
    "Delivery",
    "DispatchHandle",
    "FakeDispatchContext",
    "FakeHistory",
    "HarnessRun",
    "ListSource",
    "check_observer",
    "check_parser",
    "check_runner",
    "check_source",
    "check_store",
    "plugin",
    "sample_events",
    "xml_with_dtd",
]
