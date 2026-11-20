"""An in-memory ``DispatchContext`` for unit-testing a runner, and the small
pieces it is made of.

``FakeDispatchContext`` implements every member of the protocol and records
what the runner did with each one. The order of the calls — *refuse only
before declare, declare exactly once, deliver only after declare* — and the
shape of what is handed over are enforced by the same code the real context
uses (``runcomposer.core.handoff``), so a runner that is correct against the
fake is correct against runcomposer.

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
from runcomposer.core.handoff import (
    HandoffOrder,
    check_delivery_args,
    check_reportable,
    correlate,
    normalize_shards,
)
from runcomposer.core.hashing import bundle_hash, canonical_json, sha256_hex, verdicts_hash
from runcomposer.core.ids import new_ulid
from runcomposer.core.lifecycle import effective_verdicts, summarize
from runcomposer.core.marker import read_marker
from runcomposer.core.model import REPORTABLE_STATUSES, ArtifactRef, Item, Verdict
from runcomposer.core.ports import SourceBase
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
        self, *, labels: Mapping[str, str] | None = None, last_n: int = 5
    ) -> dict[str, float]:
        self.calls.append({"labels": dict(labels or {}), "last_n": last_n})
        return dict(self._durations)


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
    - ``options`` / ``parameters`` — what a runner reads; ``options`` is the
      configured options overlaid by ``parameters``, as in runcomposer.
    - ``source`` — a TestSource for drift checks and name resolution
      (default: a :class:`ListSource` over the items).
    - ``durations`` — what ``history.durations`` answers.
    - ``parsers`` — ``{format id: parser}`` for ``parse`` / ``deliver(bundle=)``;
      a format not given is loaded from the ``runcomposer.parsers`` entry
      points, as runcomposer does.
    - ``workdir`` — default: a temporary directory removed by :meth:`close`.
    - ``tokens`` — ``False`` for a deployment with ingest tokens disabled.

    What the runner did is in ``declared``, ``shard_items``, ``plan``,
    ``links``, ``spec_sha256``, ``deliveries``, ``live_updates``,
    ``artifacts``, ``logs``, ``refusal``, ``exported`` and ``tokens``.
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
        expect_format: str = "runcomposer-verdicts",
        tokens: bool = True,
    ):
        if items is None:
            if item_ids is None:
                planned = (spec or {}).get("selection", {}).get("materialized", {}).get("item_ids")
                item_ids = list(planned or [])
            items = [Item(id=str(item_id)) for item_id in item_ids]
        self.source: Any = source if source is not None else ListSource(items)
        self.parameters: Mapping[str, Any] = MappingProxyType(dict(parameters or {}))
        self.options: Mapping[str, Any] = MappingProxyType(
            {**dict(options or {}), **dict(parameters or {})}
        )
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
        self._own_workdir = workdir is None
        self._workdir: Path | None = Path(workdir).resolve() if workdir is not None else None
        self._order = HandoffOrder(self.dispatch_id)
        self._cancel = threading.Event()
        self._lock = threading.RLock()
        self._tail: LiveTail | None = None
        # -- what the runner did ------------------------------------------------
        self.declared: tuple[str, ...] | None = None
        self.shard_items: dict[str, list[str]] = {}
        self.plan: str = ""
        self.links: Mapping[str, str] = {}
        self.spec_sha256: str | None = None
        self.deliveries: list[Delivery] = []
        self._live: list[Verdict] = []
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
    ) -> None:
        with self._lock:
            self._order.before_declare()
            names, shard_items = normalize_shards(shards)
            self.declared = tuple(names)
            self.shard_items = shard_items
            self.plan = str(plan)
            self.links = dict(links or {})
            if spec_sha256:
                self.spec_sha256 = spec_sha256
            self._order.declared = True

    @property
    def is_declared(self) -> bool:
        return self._order.declared

    # -- results -------------------------------------------------------------------------

    def live(
        self, shard: str, item_id: str, status: str, *, duration_ms: int = 0, message: str = ""
    ) -> None:
        if status not in REPORTABLE_STATUSES or item_id not in self.item_ids:
            return  # best-effort, as in runcomposer: live status never invents an item
        with self._lock:
            self._live.append(
                Verdict(item_id, status, int(duration_ms), str(message), shard=str(shard))
            )

    def live_channel(self) -> FileLiveChannel:
        """A picklable channel for worker processes — the same file
        transport runcomposer uses; what workers write shows up in
        :attr:`live_updates`."""
        with self._lock:
            directory = self.workdir / ".live"
            directory.mkdir(exist_ok=True)
            if self._tail is None:
                self._tail = LiveTail(directory)
            return FileLiveChannel(str(directory))

    @property
    def live_updates(self) -> list[Verdict]:
        """Every live status the runner streamed, in order (shard set)."""
        self._drain_live()
        with self._lock:
            return list(self._live)

    def _drain_live(self) -> None:
        if self._tail is None:
            return
        for entry in self._tail.read():
            try:
                self.live(
                    str(entry["shard"]), str(entry["item_id"]), str(entry["status"]),
                    duration_ms=int(entry.get("duration_ms") or 0),
                    message=str(entry.get("message") or ""),
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
            # A delivery replaces the shard's live status, as in runcomposer.
            self._live = [v for v in self._live if v.shard != shard]
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
    ) -> None:
        target = str(path_or_url)
        guessed = media_type or mimetypes.guess_type(target)[0] or "application/octet-stream"
        with self._lock:
            self.artifacts.append(
                ArtifactRef(self.run_id, self.dispatch_id, name, guessed, target, shard)
            )

    def log(self, message: str, level: str = "info") -> None:
        with self._lock:
            self.logs.append((str(level), str(message)))

    # -- for external executors -------------------------------------------------------------

    def export_spec(self) -> bytes:
        with self._lock:
            if self.exported is None:
                self.exported = (
                    json.dumps(self._document, indent=2, ensure_ascii=False) + "\n"
                ).encode("utf-8")
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
    def delivered_shards(self) -> set[str]:
        return {d.shard for d in self.deliveries}

    @property
    def outstanding(self) -> list[str]:
        """Declared shards nothing was delivered for."""
        return [shard for shard in (self.declared or ()) if shard not in self.delivered_shards]

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

    def run(self, runner: Any) -> str:
        """Call ``runner.dispatch(self)`` and say how it ended, the way the
        dispatch manager would: ``refused`` (it refused, or raised, before
        declaring), ``undeclared`` (returned without declaring — runcomposer
        records that as a refusal), ``cancelled``, ``complete`` (every declared
        shard delivered) or ``awaiting`` (shards outstanding: results come by
        ingestion). An exception raised after the hand-off propagates — in
        runcomposer it is a FAILED dispatch."""
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

    def __enter__(self) -> FakeDispatchContext:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _expected_format(document: Mapping[str, Any]) -> str | None:
    expect = (document.get("results") or {}).get("expect") or []
    if expect and isinstance(expect[0], Mapping) and expect[0].get("format"):
        return str(expect[0]["format"])
    return None
