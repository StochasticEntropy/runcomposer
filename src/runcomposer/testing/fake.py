"""An in-memory ``DispatchContext`` for unit-testing a runner, and the small
pieces it is made of.

``FakeDispatchContext`` implements every member of the protocol and records
what the runner did with each one. The order of the calls — *refuse only
before declare, declare exactly once, deliver only after declare* — and the
shape of what is handed over are enforced by the same code the real context
uses (``runcomposer.core.handoff``), and the document expects the runner's
own ``result_format`` as a run composed for it would — so what a runner
meets here is what it meets in runcomposer.

```python
from runcomposer.testing import FakeDispatchContext

ctx = FakeDispatchContext(["Shop.Cart.Core.T001", "Shop.Cart.Core.T002"],
                          options={"base_url": "https://shop.example"})
assert ctx.run(MyRunner(...)) == "complete"
assert ctx.summary() == {"PASS": 2}
```
"""

from __future__ import annotations

import dataclasses
import json
import mimetypes
import shutil
import tempfile
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn

from runcomposer.app.livechannel import FileLiveChannel, LiveTail
from runcomposer.core.errors import DispatchCancelled, DispatchRefused
from runcomposer.core.filter import parse_filter
from runcomposer.core.handoff import (
    HandoffOrder,
    check_delivery_args,
    check_reportable,
    correlate,
    normalize_groups,
    normalize_shards,
)
from runcomposer.core.hashing import bundle_hash, canonical_json, sha256_hex, verdicts_hash
from runcomposer.core.ids import new_ulid
from runcomposer.core.lifecycle import effective_verdicts, summarize
from runcomposer.core.marker import read_marker
from runcomposer.core.model import LIVE_STATUSES, ArtifactRef, Item, LiveStatus, Verdict
from runcomposer.core.parameters import validate_parameters
from runcomposer.core.ports import PlanPreview, SourceBase
from runcomposer.core.registry import PARSER_GROUP, load_entry_point
from runcomposer.core.spec import build_spec

__all__ = ["FakeDispatchContext", "FakeHistory", "ListSource"]


class ListSource(SourceBase):
    """A TestSource over a fixed list of items — the catalog of a fake
    dispatch. Native names resolve by identity, plus an optional alias map."""

    provider_id = "list"

    def __init__(self, items: Sequence[Item], *, aliases: Mapping[str, str] | None = None):
        self._items = list(items)
        self._ids = {item.id for item in self._items}
        self._aliases = dict(aliases or {})
        self._snapshot = "sha256:" + sha256_hex(
            canonical_json([{"id": item.id, "tags": list(item.tags)} for item in self._items])
        )

    def items(self) -> list[Item]:
        return list(self._items)

    def snapshot(self) -> str:
        return self._snapshot

    def resolve(self, native_name: str) -> str | None:
        if native_name in self._ids:
            return native_name
        return self._aliases.get(native_name)

    def fingerprint(self) -> str:
        return self._snapshot


class FakeHistory:
    """``History`` over a fixed mapping of item id → mean duration (ms). The
    calls made to it are recorded in ``calls``."""

    def __init__(self, durations: Mapping[str, float] | None = None):
        self._durations = dict(durations or {})
        self.calls: list[dict[str, Any]] = []

    def durations(
        self,
        *,
        labels: Mapping[str, str] | None = None,
        last_n: int = 5,
        statuses: Sequence[str] | None = None,
    ) -> dict[str, float]:
        self.calls.append(
            {
                "labels": dict(labels or {}),
                "last_n": last_n,
                "statuses": None if statuses is None else tuple(statuses),
            }
        )
        return dict(self._durations)


class _PlanView:
    """What ``Runner.plan`` receives from the fake: the members of
    ``PlanContext`` and nothing else — a runner that reaches for ``declare``
    or ``deliver`` in its plan fails here as it would in runcomposer."""

    def __init__(self, ctx: FakeDispatchContext):
        self._ctx = ctx
        self.run_id = ctx.run_id
        self.runner_id = ctx.runner_id
        self.spec = ctx.spec
        self.item_ids = ctx.item_ids
        self.options = ctx.options
        self.parameters = ctx.parameters
        self.source = ctx.source
        self.history = ctx.history

    def connector_settings(self, name: str) -> Mapping[str, Any]:
        return self._ctx.connector_settings(name)

    def refuse(self, reason: str) -> NoReturn:
        raise DispatchRefused(reason)

    def export_spec(self) -> bytes:
        return self._ctx.spec_bytes()


@dataclasses.dataclass(frozen=True)
class Delivery:
    """One ``deliver`` call as the fake recorded it."""

    shard: str
    outcome: str  # new | replaced | duplicate
    content_hash: str
    format: str
    verdicts: tuple[Verdict, ...]


class FakeDispatchContext:
    """A recording, in-memory :class:`runcomposer.sdk.DispatchContext`.

    Inputs (all optional except the items):

    - ``item_ids`` or ``items`` — the planned set (``items`` also gives the
      catalog its tags); or a whole ``spec`` document, whose materialized ids
      are then the planned set.
    - ``options`` / ``parameters`` — what a runner reads: its configured
      options, and the per-run parameters, each on its own as in runcomposer
      (a parameter never shows up in ``options``).
    - ``source`` — a TestSource for drift checks and name resolution
      (default: a :class:`ListSource` over the items).
    - ``durations`` — what ``history.durations`` answers.
    - ``parsers`` — ``{format id: parser}`` for ``parse`` / ``deliver(bundle=)``;
      a format not given is loaded from the ``runcomposer.parsers`` entry
      points, as runcomposer does.
    - ``expect_format`` — the result format the document expects (what
      ``deliver(bundle=…)`` parses without a ``format=``). Default: the
      runner's ``RunnerInfo.result_format`` once :meth:`run` or
      ``check_runner`` sees the runner, else ``runcomposer-verdicts`` — as a
      run composed for that runner in runcomposer. Ignored with ``spec``.
    - ``workdir`` — default: a temporary directory removed by :meth:`close`.
    - ``tokens`` — ``False`` for a deployment with ingest tokens disabled.
    - ``connectors`` — ``{name: settings}``, what ``ctx.connector_settings(name)``
      answers (the deployment's ``connectors:`` section).

    What the runner did is in ``declared``, ``shard_items``, ``shard_groups``,
    ``plan``, ``links``, ``spec_sha256``, ``deliveries``, ``live_updates``
    (everything it streamed, in order) and ``live_now`` (what a store would
    hold: the latest status per item of shards that have not delivered),
    ``artifacts``, ``logs``, ``refusal``, ``exported`` and ``tokens``.

    A runner that looks at the catalog itself — where an item comes from,
    what its ``Item.meta`` holds — needs real items: build the fake from the source,
    :meth:`from_source`.
    """

    __test__ = False

    def __init__(
        self,
        item_ids: Sequence[str] | None = None,
        *,
        items: Sequence[Item] | None = None,
        spec: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
        parameters: Mapping[str, Any] | None = None,
        runner_id: str = "runner",
        run_id: str | None = None,
        dispatch_id: str | None = None,
        workdir: str | Path | None = None,
        source: Any = None,
        durations: Mapping[str, float] | None = None,
        parsers: Mapping[str, Any] | None = None,
        expect_format: str | None = None,
        tokens: bool = True,
        connectors: Mapping[str, Mapping[str, Any]] | None = None,
    ):
        #: Whether the expected format may still follow the runner's own.
        self._format_adoptable = spec is None and expect_format is None
        expect_format = expect_format or "runcomposer-verdicts"
        if items is None:
            if item_ids is None:
                planned = (spec or {}).get("selection", {}).get("materialized", {}).get("item_ids")
                item_ids = list(planned or [])
            items = [Item(id=str(item_id)) for item_id in item_ids]
        self.source: Any = source if source is not None else ListSource(items)
        self._parameter_values: dict[str, Any] = dict(parameters or {})
        self.parameters: Mapping[str, Any] = MappingProxyType(self._parameter_values)
        self.options: Mapping[str, Any] = MappingProxyType(dict(options or {}))
        self.runner_id = runner_id
        if spec is None:
            spec = build_spec(
                title="Fake dispatch",
                materialized_ids=[item.id for item in items],
                source_provider=str(self.source.provider_id),
                snapshot=str(self.source.snapshot()),
                results={"expect": [{"format": expect_format}], "shards": 1},
                runner={runner_id: dict(parameters)} if parameters else None,
                run_id=run_id,
            )
        self._document = json.loads(json.dumps(dict(spec)))
        self.spec: Mapping[str, Any] = MappingProxyType(self._document)
        self.run_id: str = str(self._document.get("run", {}).get("id") or run_id or new_ulid())
        self.dispatch_id: str = dispatch_id or new_ulid()
        materialized = (self._document.get("selection") or {}).get("materialized") or {}
        self.item_ids: tuple[str, ...] = tuple(materialized.get("item_ids") or ())
        self.history = FakeHistory(durations)
        self._parsers = dict(parsers or {})
        self._expect_format = _expected_format(self._document) or expect_format
        self._tokens_enabled = tokens
        self._connectors = {str(name): dict(value) for name, value in (connectors or {}).items()}
        self._own_workdir = workdir is None
        self._workdir: Path | None = Path(workdir).resolve() if workdir is not None else None
        self._order = HandoffOrder(self.dispatch_id)
        self._cancel = threading.Event()
        self._lock = threading.RLock()
        self._tail: LiveTail | None = None
        # -- what the runner did ------------------------------------------------
        self.declared: tuple[str, ...] | None = None
        self.shard_items: dict[str, list[str]] = {}
        self.shard_groups: dict[str, str] = {}
        self.plan: str = ""
        self._links: dict[str, str] = {}
        self.links: Mapping[str, str] = MappingProxyType(self._links)
        self.spec_sha256: str | None = None
        self.deliveries: list[Delivery] = []
        self._live: list[LiveStatus] = []
        self._private_dir: Path | None = None
        self.artifacts: list[ArtifactRef] = []
        self.logs: list[tuple[str, str]] = []
        self.refusal: str | None = None
        self.exported: bytes | None = None
        self.tokens: list[str] = []
        self.cancel_calls = 0

    # -- input ---------------------------------------------------------------------

    @property
    def workdir(self) -> Path:
        with self._lock:
            if self._workdir is None:
                self._workdir = Path(tempfile.mkdtemp(prefix="runcomposer-fake-")).resolve()
            self._workdir.mkdir(parents=True, exist_ok=True)
            return self._workdir

    @property
    def private_dir(self) -> Path:
        """A directory of its own beside the work directory, as in
        runcomposer: nothing in it is ever an artifact."""
        with self._lock:
            if self._private_dir is None:
                self._private_dir = Path(tempfile.mkdtemp(prefix="runcomposer-fake-private-")).resolve()
            self._private_dir.mkdir(parents=True, exist_ok=True)
            return self._private_dir

    @classmethod
    def from_source(
        cls, source: Any, selection: Any = None, **kwargs: Any
    ) -> FakeDispatchContext:
        """A fake over a real catalog: the planned items are the source's own
        ``Item`` objects (tags, hierarchy, ``meta``) and ``ctx.source`` is the
        source. ``selection`` picks them — ``None`` for all, a tag-filter
        expression (``"Checkout"``, ``{"op": "AND", …}``) or a list of item
        ids. Everything else is passed on to the constructor."""
        items = list(source.items())
        if isinstance(selection, (list, tuple)):
            wanted = [str(item_id) for item_id in selection]
            by_id = {item.id: item for item in items}
            unknown = [item_id for item_id in wanted if item_id not in by_id]
            if unknown:
                raise ValueError(f"the source has no item(s) {unknown}")
            items = [by_id[item_id] for item_id in wanted]
        elif selection is not None:
            node = parse_filter(selection)
            items = [item for item in items if node.matches(item.tags)]
        return cls(items=items, source=source, **kwargs)

    def connector_settings(self, name: str) -> Mapping[str, Any]:
        """``connectors=`` of the constructor, by name — what a deployment's
        ``connectors:`` section holds in runcomposer."""
        return MappingProxyType(dict(self._connectors.get(str(name), {})))

    # -- the hand-off ----------------------------------------------------------------

    def refuse(self, reason: str) -> NoReturn:
        if not self._order.declared:
            self.refusal = str(reason)
        self._order.refuse(reason)

    def declare(
        self,
        shards: Sequence[str] | Mapping[str, Sequence[str]],
        *,
        plan: str = "",
        links: Mapping[str, str] | None = None,
        spec_sha256: str | None = None,
        groups: Mapping[str, str] | None = None,
    ) -> None:
        with self._lock:
            self._order.before_declare()
            names, shard_items = normalize_shards(shards)
            self.shard_groups = normalize_groups(groups, names)
            self.declared = tuple(names)
            self.shard_items = shard_items
            self.plan = str(plan)
            self._links.update({str(k): str(v) for k, v in (links or {}).items()})
            if spec_sha256:
                self.spec_sha256 = spec_sha256
            self._order.declared = True

    def set_link(self, name: str, url: str) -> None:
        with self._lock:
            self._links[str(name)] = str(url)

    @property
    def is_declared(self) -> bool:
        return self._order.declared

    # -- results -------------------------------------------------------------------------

    def live(
        self, shard: str, item_id: str, status: str, *, duration_ms: int = 0, message: str = ""
    ) -> None:
        # What worker processes wrote before this call comes first: the
        # stream reads in the order things happened.
        self._drain_live()
        self._record_live(shard, item_id, status, duration_ms, message)

    def _record_live(
        self, shard: str, item_id: str, status: str, duration_ms: int = 0, message: str = ""
    ) -> None:
        if status not in LIVE_STATUSES or item_id not in self.item_ids:
            return  # best-effort, as in runcomposer: live status never invents an item
        with self._lock:
            self._live.append(
                LiveStatus(item_id, status, int(duration_ms), str(message), shard=str(shard))
            )

    def live_channel(self) -> FileLiveChannel:
        """A picklable channel for worker processes — the same file
        transport runcomposer uses; what workers write shows up in
        :attr:`live_updates` and (their log lines) in :attr:`logs`."""
        with self._lock:
            directory = self.workdir / ".live"
            directory.mkdir(exist_ok=True)
            if self._tail is None:
                self._tail = LiveTail(directory)
            return FileLiveChannel(str(directory))

    @property
    def live_updates(self) -> list[LiveStatus]:
        """The live stream as the runner produced it, oldest first — ``RUNNING``
        included, and kept after the shard delivered: the whole stream, for
        asserting what a runner reported while it ran."""
        self._drain_live()
        with self._lock:
            return list(self._live)

    @property
    def live_now(self) -> list[LiveStatus]:
        """The live status as a store holds it at this moment: the latest
        entry per (shard, item), and nothing for shards that have delivered
        (a delivery replaces a shard's live status)."""
        self._drain_live()
        with self._lock:
            delivered = {delivery.shard for delivery in self.deliveries}
            latest: dict[tuple[str, str], LiveStatus] = {}
            for status in self._live:
                if status.shard not in delivered:
                    latest[(status.shard, status.item_id)] = status
            return list(latest.values())

    def delivered_shards(self) -> set[str]:
        with self._lock:
            return {delivery.shard for delivery in self.deliveries}

    def outstanding_shards(self) -> list[str]:
        """Declared shards nothing was delivered for, in declaration order."""
        delivered = self.delivered_shards()
        return [shard for shard in (self.declared or ()) if shard not in delivered]

    def _drain_live(self) -> None:
        if self._tail is None:
            return
        for entry in self._tail.read():
            if "log" in entry:
                self.log(str(entry["log"]), level=str(entry.get("level") or "info"))
                continue
            try:
                self._record_live(
                    str(entry["shard"]), str(entry["item_id"]), str(entry["status"]),
                    int(entry.get("duration_ms") or 0), str(entry.get("message") or ""),
                )
            except (KeyError, TypeError, ValueError):
                continue

    def parse(self, bundle: str | Path, *, format: str | None = None) -> list[Verdict]:
        format_id = format or self._expect_format
        parser = self._parsers.get(format_id)
        if parser is None:
            parser = self._parsers[format_id] = load_entry_point(PARSER_GROUP, format_id)()
        verdicts, warnings = correlate(parser, Path(bundle), format_id, self.source.resolve)
        for warning in warnings:
            self.log(warning, level="warning")
        return verdicts

    def deliver(
        self,
        shard: str,
        *,
        verdicts: Sequence[Verdict] | None = None,
        bundle: str | Path | None = None,
        format: str | None = None,
    ) -> str:
        self._order.before_deliver()
        check_delivery_args(verdicts, bundle)
        format_id = format or self._expect_format
        if bundle is not None:
            path = Path(bundle)
            self._check_marker(path)
            delivered = self.parse(path, format=format_id)
            content_hash = bundle_hash(path)
        else:
            assert verdicts is not None
            delivered = check_reportable(verdicts)
            content_hash = verdicts_hash(delivered)
        shard = str(shard)
        self._drain_live()
        with self._lock:
            if self.declared is not None and shard not in self.declared:
                self.log(
                    f"shard {shard!r} was not declared by dispatch {self.dispatch_id} "
                    f"(declared: {list(self.declared)}) — recorded, but it completes nothing",
                    level="warning",
                )
            unplanned = sorted({v.item_id for v in delivered if v.item_id not in self.item_ids})
            if unplanned:
                self.log(
                    f"{len(unplanned)} reported item(s) were not planned for this run "
                    f"(first: {unplanned[0]!r})",
                    level="warning",
                )
            previous = next((d for d in reversed(self.deliveries) if d.shard == shard), None)
            if previous is None:
                outcome = "new"
            elif previous.content_hash == content_hash:
                outcome = "duplicate"
            else:
                outcome = "replaced"
            labelled = tuple(dataclasses.replace(v, shard=shard) for v in delivered)
            self.deliveries.append(Delivery(shard, outcome, content_hash, format_id, labelled))
            return outcome

    def _check_marker(self, bundle: Path) -> None:
        marker = read_marker(bundle)
        if not marker:
            return
        if marker["run_id"] != self.run_id:
            raise RuntimeError(
                f"{bundle}: marker names run {marker['run_id']!r}, not {self.run_id} — "
                "these are another run's results"
            )
        if marker.get("dispatch_id") and marker["dispatch_id"] != self.dispatch_id:
            raise RuntimeError(
                f"{bundle}: marker names dispatch {marker['dispatch_id']!r}, not {self.dispatch_id}"
            )
        handed_out = sha256_hex(self.exported) if self.exported is not None else None
        if marker.get("spec_sha256") and handed_out and marker["spec_sha256"] != handed_out:
            raise RuntimeError(
                f"{bundle}: marker spec_sha256 does not match the spec handed out for this dispatch"
            )

    def add_artifact(
        self,
        name: str,
        path_or_url: str | Path,
        *,
        media_type: str | None = None,
        shard: str | None = None,
        group: str | None = None,
    ) -> None:
        target = str(path_or_url)
        guessed = media_type or mimetypes.guess_type(target)[0] or "application/octet-stream"
        with self._lock:
            self.artifacts.append(
                ArtifactRef(self.run_id, self.dispatch_id, name, guessed, target, shard, group)
            )

    def log(self, message: str, level: str = "info") -> None:
        with self._lock:
            self.logs.append((str(level), str(message)))

    # -- for external executors -------------------------------------------------------------

    def spec_bytes(self) -> bytes:
        """The document as the bytes ``export_spec`` hands out — without
        recording that they were."""
        return (json.dumps(self._document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    def export_spec(self) -> bytes:
        with self._lock:
            if self.exported is None:
                self.exported = self.spec_bytes()
                self.spec_sha256 = sha256_hex(self.exported)
            return self.exported

    def ingest_token(self) -> str | None:
        if not self._tokens_enabled:
            return None
        with self._lock:
            if not self.tokens:
                self.tokens.append(f"rct_fake_{self.dispatch_id.lower()}")
            return self.tokens[0]

    # -- cancellation --------------------------------------------------------------------------

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise DispatchCancelled(f"dispatch {self.dispatch_id} was cancelled")

    def request_cancel(self) -> None:
        """What runcomposer does when someone cancels: set the flag."""
        self._cancel.set()

    # -- reading what happened -------------------------------------------------------------------

    @property
    def refused(self) -> bool:
        return self.refusal is not None and not self._order.declared

    @property
    def outstanding(self) -> list[str]:
        """Declared shards nothing was delivered for (``outstanding_shards()``
        as an attribute, for assertions)."""
        return self.outstanding_shards()

    @property
    def verdicts(self) -> list[Verdict]:
        """Every delivered verdict, the latest delivery per shard counting
        (``replaced`` overrides, ``duplicate`` changes nothing)."""
        latest: dict[str, Delivery] = {}
        for delivery in self.deliveries:
            if delivery.outcome != "duplicate":
                latest[delivery.shard] = delivery
        return [v for delivery in latest.values() for v in delivery.verdicts]

    def summary(self) -> dict[str, int]:
        """Status counts over the effective verdicts (highest attempt)."""
        return dict(summarize(effective_verdicts(self.verdicts)))

    def warnings(self) -> list[str]:
        return [message for level, message in self.logs if level in ("warning", "error")]

    def adopt_result_format(self, format_id: str) -> None:
        """Expect ``format_id`` back, when neither a spec nor an explicit
        ``expect_format`` said otherwise — what composing a run for a runner
        with a fixed ``result_format`` does in runcomposer."""
        if not self._format_adoptable or not format_id:
            return
        self._expect_format = format_id
        results = self._document.setdefault("results", {})
        results["expect"] = [{"format": format_id}]

    def adopt(self, info: Any) -> None:
        """Become the context of a dispatch *to this runner* (its
        ``RunnerInfo``), as composing and dispatching a run for it does in
        runcomposer: the document expects the runner's ``result_format``
        (:meth:`adopt_result_format`), and the per-run parameters are
        validated against its declaration with the declared defaults applied
        — so ``ctx.parameters`` holds here what it holds there. A value the
        runner does not accept raises ``InvalidParameter``, as the request
        would be refused. :meth:`run`, :meth:`preview` and ``check_runner``
        call this."""
        if info.result_format:
            self.adopt_result_format(info.result_format)
        accepted = validate_parameters(info.parameters, self._parameter_values)
        self._parameter_values.clear()
        self._parameter_values.update(accepted)
        if accepted and self._format_adoptable:
            self._document.setdefault("runner", {})[self.runner_id] = dict(accepted)

    def preview(self, runner: Any) -> PlanPreview | None:
        """Call ``runner.plan`` the way runcomposer does — with a context
        that has the members of ``PlanContext`` only — and return its
        preview; ``None`` when the plan refuses (the reason is then in
        ``refusal``). Nothing of the fake is touched by a preview."""
        self.adopt(runner.describe())
        try:
            answer = runner.plan(_PlanView(self))
        except DispatchRefused as exc:
            self.refusal = str(exc)
            return None
        if not isinstance(answer, PlanPreview):
            raise TypeError(f"plan() must return a PlanPreview, got {type(answer).__name__}")
        return answer

    def run(self, runner: Any) -> str:
        """Call ``runner.dispatch(self)`` and say how it ended, the way the
        dispatch manager would: ``refused`` (``ctx.refuse`` before declaring),
        ``undeclared`` (returned without declaring — runcomposer records that
        as a refusal), ``cancelled``, ``complete`` (every declared shard
        delivered) or ``awaiting`` (shards outstanding: results come by
        ingestion). Any other exception propagates, so a test sees it: in
        runcomposer one raised before the hand-off is recorded as a refusal
        (and logged as a crash), one raised after it is a FAILED dispatch."""
        self.adopt(runner.describe())
        try:
            runner.dispatch(self)
        except DispatchRefused:
            return "refused"
        except DispatchCancelled:
            return "cancelled"
        if self.cancelled:
            return "cancelled"
        if not self._order.declared:
            return "undeclared"
        return "awaiting" if self.outstanding else "complete"

    def close(self) -> None:
        """Remove the temporary work directory (if this fake made it)."""
        if self._own_workdir and self._workdir is not None:
            shutil.rmtree(self._workdir, ignore_errors=True)
        if self._private_dir is not None:
            shutil.rmtree(self._private_dir, ignore_errors=True)

    def __enter__(self) -> FakeDispatchContext:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _expected_format(document: Mapping[str, Any]) -> str | None:
    expect = (document.get("results") or {}).get("expect") or []
    if expect and isinstance(expect[0], Mapping) and expect[0].get("format"):
        return str(expect[0]["format"])
    return None
