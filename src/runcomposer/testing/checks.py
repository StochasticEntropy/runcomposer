"""Contract checks: run the invariants of one port against an instance.

Each ``check_*`` raises ``AssertionError`` whose message starts with the rule
it found broken, in brackets — ``[source.resolve-own-ids] …`` — and returns
quietly (or with what it recorded) when every rule holds. They import
nothing from pytest; call them from any test runner.

The rules are the contract of DESIGN.md §5–§6 written as code. The presence
of every member a protocol names is the first rule of every check: it is the
same list (``runcomposer.sdk.contract_members``) that startup checks a
configured plugin against.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from runcomposer.core.errors import (
    DispatchCancelled,
    DispatchRefused,
    InvalidParameter,
    ParseError,
    RunActive,
)
from runcomposer.core.events import LIFECYCLE_KINDS, LOG, WARNING
from runcomposer.core.lifecycle import ACTIVE_STATES
from runcomposer.core.model import REPORTABLE_STATUSES, Item, LiveStatus, RunEvent, Verdict
from runcomposer.core.parameters import PARAMETER_KINDS, Parameter, validate_parameters
from runcomposer.core.ports import (
    Configurable,
    Health,
    ParsedVerdict,
    PlanPreview,
    ResultParser,
    Runner,
    RunnerInfo,
    RunObserver,
    RunStore,
    TestSource,
    missing_members,
)
from runcomposer.core.spec import build_spec
from runcomposer.core.timeutil import utc_now

from .fake import FakeDispatchContext

__all__ = [
    "DEFAULT_ITEM_IDS",
    "check_observer",
    "check_parser",
    "check_runner",
    "check_source",
    "check_store",
    "sample_events",
    "xml_with_dtd",
]

#: The planned set ``check_runner`` uses when no context is given — items of
#: the bundled web-shop demo corpus.
DEFAULT_ITEM_IDS = ("Shop.Cart.Core.T001", "Shop.Cart.Core.T002", "Shop.Cart.Core.T003")

HEALTH_STATUSES = ("ok", "degraded", "down", "unknown")


def _fail(rule: str, detail: str) -> AssertionError:
    return AssertionError(f"[{rule}] {detail}")


def _require(condition: object, rule: str, detail: str) -> None:
    if not condition:
        raise _fail(rule, detail)


def _members(plugin: Any, protocol: type, rule: str) -> None:
    missing = missing_members(plugin, protocol)
    _require(
        not missing,
        rule,
        f"{type(plugin).__name__} does not provide {missing} of the {protocol.__name__} "
        "contract — derive it from the runcomposer.sdk base class for the role, or define them",
    )
    owner = plugin if isinstance(plugin, type) else type(plugin)
    missing = missing_members(owner, Configurable)
    _require(
        not missing,
        rule,
        f"{owner.__name__} does not provide {missing} of the Configurable contract "
        "(OPTIONS, resolve_config_paths) — derive it from runcomposer.sdk.PluginBase",
    )


# -- TestSource -----------------------------------------------------------------------


def check_source(
    source: Any,
    *,
    native_names: Mapping[str, str] | None = None,
    rebuild: Callable[[], Any] | None = None,
) -> list[Item]:
    """Check a :class:`runcomposer.sdk.TestSource`.

    - ``native_names`` — ``{native name: item id}`` pairs a result artifact of
      this framework spells; each must resolve to that id.
    - ``rebuild`` — builds a second instance over the same corpus; its items
      and snapshot must equal the first one's (a snapshot is a function of
      the corpus, not of the process).

    Returns the items, for further checks of your own.
    """
    _members(source, TestSource, "source.contract")
    _require(
        isinstance(source.provider_id, str) and source.provider_id,
        "source.provider-id", f"provider_id must be a non-empty string, got {source.provider_id!r}",
    )
    items = source.items()
    _require(isinstance(items, list), "source.items-type", f"items() must return a list, got {type(items).__name__}")
    for item in items:
        _require(isinstance(item, Item), "source.items-type", f"items() returned {item!r}, not an Item")
        _require(isinstance(item.id, str) and item.id, "source.item-id", f"an item id must be a non-empty string: {item!r}")
        _require(
            isinstance(item.tags, tuple) and all(isinstance(t, str) and t for t in item.tags),
            "source.item-tags", f"{item.id}: tags must be a tuple of non-empty strings, got {item.tags!r}",
        )
    _require(source.items() == items, "source.items-stable", "two calls of items() disagree")
    counts = Counter(item.id for item in items)
    duplicated = {item_id for item_id, n in counts.items() if n > 1}
    listed = set(source.duplicate_ids or ())
    _require(
        listed == duplicated,
        "source.duplicate-ids",
        f"duplicate_ids must list exactly the ids several items carry: listed {sorted(listed)}, "
        f"carried twice or more {sorted(duplicated)}",
    )
    snapshot = source.snapshot()
    _require(isinstance(snapshot, str) and snapshot, "source.snapshot", f"snapshot() must be a non-empty string, got {snapshot!r}")
    _require(source.snapshot() == snapshot, "source.snapshot-stable", "two calls of snapshot() disagree")
    fingerprint = source.fingerprint()
    _require(
        fingerprint is None or isinstance(fingerprint, str),
        "source.fingerprint", f"fingerprint() returns a string or None, got {fingerprint!r}",
    )
    _require(
        source.fingerprint() == fingerprint,
        "source.fingerprint-stable", "fingerprint() changed although the corpus did not",
    )
    # §2: items' ids and resolve() define one id space. The reference result
    # format (runcomposer-verdicts) names items by id, so every id resolves to
    # itself.
    for item in items:
        resolved = source.resolve(item.id)
        _require(
            resolved == item.id,
            "source.resolve-own-ids", f"resolve({item.id!r}) returned {resolved!r}, not the id itself",
        )
    unknown = "runcomposer-kit::no-such-test::∃"
    _require(
        source.resolve(unknown) is None,
        "source.resolve-unknown", f"resolve() of a name nothing carries must be None, got {source.resolve(unknown)!r}",
    )
    ids = set(counts)
    for native, expected in (native_names or {}).items():
        resolved = source.resolve(native)
        _require(
            resolved == expected,
            "source.resolve-native", f"resolve({native!r}) returned {resolved!r}, expected {expected!r}",
        )
        _require(expected in ids, "source.resolve-native", f"{expected!r} is not an id items() lists")
    if rebuild is not None:
        again = rebuild()
        _require(again.items() == items, "source.rebuild-stable", "a second instance over the same corpus lists other items")
        _require(
            again.snapshot() == snapshot,
            "source.rebuild-stable", "a second instance over the same corpus has another snapshot",
        )
    return items


# -- ResultParser ---------------------------------------------------------------------


def check_parser(
    parser: Any,
    bundle: str | Path,
    *,
    expected: Mapping[str, str] | None = None,
    bad_bundles: Iterable[str | Path] = (),
) -> list[ParsedVerdict]:
    """Check a :class:`runcomposer.sdk.ResultParser` against a good
    ``bundle`` (a file or a directory, whatever your executor writes).

    - ``expected`` — ``{native name: status}`` the bundle must yield (the
      effective — highest — attempt per name).
    - ``bad_bundles`` — inputs that must be refused with ``ParseError`` and
      nothing else: truncated files, foreign formats, an XML document with a
      DTD (:func:`xml_with_dtd` writes one).

    Returns the parsed verdicts.
    """
    _members(parser, ResultParser, "parser.contract")
    format_id = parser.format_id
    _require(
        isinstance(format_id, str) and format_id and ":" not in format_id and "/" not in format_id,
        "parser.format-id",
        f"format_id must be a non-empty name without ':' or '/' (a spec names a format, never "
        f"an import path), got {format_id!r}",
    )
    parsed = parser.parse(Path(bundle))
    _require(isinstance(parsed, list), "parser.parse-type", f"parse() must return a list, got {type(parsed).__name__}")
    _require(parsed, "parser.non-empty", f"{bundle} yielded no verdicts — the pipeline refuses an empty bundle")
    for verdict in parsed:
        _require(isinstance(verdict, ParsedVerdict), "parser.parse-type", f"parse() returned {verdict!r}, not a ParsedVerdict")
        _require(
            isinstance(verdict.native_name, str) and verdict.native_name,
            "parser.native-name", f"a native name must be a non-empty string: {verdict!r}",
        )
        _require(
            verdict.status in REPORTABLE_STATUSES,
            "parser.status", f"{verdict.native_name}: status {verdict.status!r} is not one of {REPORTABLE_STATUSES}",
        )
        _require(
            isinstance(verdict.duration_ms, int) and verdict.duration_ms >= 0,
            "parser.duration", f"{verdict.native_name}: duration_ms must be an int ≥ 0, got {verdict.duration_ms!r}",
        )
        _require(
            isinstance(verdict.attempt, int) and verdict.attempt >= 1,
            "parser.attempt", f"{verdict.native_name}: attempt must be an int ≥ 1, got {verdict.attempt!r}",
        )
        _require(isinstance(verdict.message, str), "parser.message", f"{verdict.native_name}: message must be a string")
    _require(parser.parse(Path(bundle)) == parsed, "parser.deterministic", "two parses of one bundle disagree")
    if expected is not None:
        effective: dict[str, ParsedVerdict] = {}
        for verdict in parsed:
            current = effective.get(verdict.native_name)
            if current is None or verdict.attempt >= current.attempt:
                effective[verdict.native_name] = verdict
        got = {name: verdict.status for name, verdict in effective.items()}
        _require(got == dict(expected), "parser.expected", f"parsed {got}, expected {dict(expected)}")
    for bad in bad_bundles:
        try:
            parser.parse(Path(bad))
        except ParseError:
            continue
        except Exception as exc:
            raise _fail(
                "parser.bad-input-is-parse-error",
                f"{bad}: raised {type(exc).__name__} ({exc}) — bad input is a ParseError and nothing else",
            ) from exc
        raise _fail("parser.bad-input-is-parse-error", f"{bad}: was parsed without complaint")
    return parsed


def xml_with_dtd(path: str | Path, *, root: str = "testsuite") -> Path:
    """Write an XML document that carries a DTD (an entity-expansion bomb) to
    ``path`` — for ``check_parser(bad_bundles=…)``: every XML-reading parser
    must refuse it (read XML through ``runcomposer.sdk.parse_xml``)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        '<?xml version="1.0"?>\n'
        f'<!DOCTYPE {root} [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>\n'
        f'<{root} name="&b;"><testcase name="Cart Total Matches"/></{root}>\n',
        encoding="utf-8",
    )
    return target


# -- Runner ---------------------------------------------------------------------------


def check_runner(
    runner: Any,
    ctx: FakeDispatchContext | None = None,
    *,
    expect: str | None = None,
    cancel_after_declare: bool = False,
    cancel_within: float = 10.0,
    allow_missing: bool = False,
    timeout: float = 60.0,
) -> FakeDispatchContext:
    """Check a :class:`runcomposer.sdk.Runner`: its description, its health,
    and one dispatch against ``ctx`` (default: a
    :class:`FakeDispatchContext` over :data:`DEFAULT_ITEM_IDS`).

    - ``expect`` — how the dispatch must end: ``complete`` (every declared
      shard delivered), ``awaiting`` (shards outstanding, results come by
      ingestion — the runner must declare ``external_delivery``),
      ``refused``, ``cancelled`` or ``failed`` (raised after the hand-off).
      ``None`` accepts ``complete``, ``refused`` and — with
      ``external_delivery`` — ``awaiting``.
    - ``cancel_after_declare`` — once the runner has declared, cancel the
      dispatch the way runcomposer does (flag, then ``runner.cancel(ctx)``)
      and require ``dispatch`` to return within ``cancel_within`` seconds of
      that, without delivering every shard afterwards (which would mean it
      did not stop). Meant for a runner held in execution — a command that
      blocks, a build that holds — so that the cancel meets it working.
    - ``allow_missing`` — accept a ``complete`` dispatch that left planned
      items unreported or delivered native names the catalog does not know
      (both are ``INCOMPLETE`` runs in runcomposer); off by default.
    - ``timeout`` — how long ``dispatch`` may take at all.

    When the runner's ``RunnerInfo.result_format`` is set and the context
    built its own document, the document expects that format — as a run
    composed for the runner does in runcomposer.

    Returns the context, with everything the runner did recorded on it.
    """
    _members(runner, Runner, "runner.contract")
    info = runner.describe()
    _check_info(info)
    health = runner.health()
    _require(isinstance(health, Health), "runner.health", f"health() must return a Health, got {health!r}")
    _require(
        health.status in HEALTH_STATUSES,
        "runner.health", f"health().status must be one of {HEALTH_STATUSES}, got {health.status!r}",
    )
    _require(isinstance(health.detail, str), "runner.health", "health().detail must be a string")

    ctx = ctx if ctx is not None else FakeDispatchContext(DEFAULT_ITEM_IDS)
    ctx.adopt(info)
    preview = _check_plan(runner, ctx) if "plan" in info.capabilities else None
    outcome: dict[str, Any] = {}
    delivered_before_cancel = 0

    def target() -> None:
        try:
            runner.dispatch(ctx)
            outcome["returned"] = True
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=target, name="runcomposer-check-runner", daemon=True)
    thread.start()
    if cancel_after_declare:
        deadline = time.monotonic() + timeout
        while not ctx.is_declared and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        _require(ctx.is_declared, "runner.cancel", "the runner never declared, so there was nothing to cancel")
        delivered_before_cancel = len(ctx.deliveries)
        ctx.request_cancel()
        ctx.cancel_calls += 1
        runner.cancel(ctx)
        thread.join(cancel_within)
        if thread.is_alive():
            raise _fail(
                "runner.cancel",
                f"dispatch() did not return within {cancel_within:g}s of being cancelled — check "
                "ctx.cancelled between units of work, and stop what is running in cancel(ctx)",
            )
    thread.join(timeout)
    if thread.is_alive():
        if not ctx.cancelled:
            ctx.request_cancel()
            runner.cancel(ctx)
        raise _fail(
            "runner.cancel" if cancel_after_declare else "runner.returns",
            f"dispatch() did not return within {timeout:g}s"
            + (" of being cancelled" if cancel_after_declare else ""),
        )
    error = outcome.get("error")
    if isinstance(error, RuntimeError) and _is_order_error(error):
        raise _fail("runner.order", str(error)) from error

    if isinstance(error, DispatchRefused):
        ended = "refused"
    elif isinstance(error, DispatchCancelled) or (error is None and ctx.cancelled):
        ended = "cancelled"
    elif error is not None:
        if not ctx.is_declared:
            ended = "refused"  # runcomposer records a raise before declare as a refusal
        else:
            ended = "failed"
    elif not ctx.is_declared:
        raise _fail(
            "runner.declares",
            "dispatch() returned without calling ctx.declare() or ctx.refuse() — nothing was "
            "handed off (runcomposer records that as a refusal)",
        )
    else:
        ended = "awaiting" if ctx.outstanding else "complete"

    if (
        cancel_after_declare
        and error is None
        and not ctx.outstanding
        and len(ctx.deliveries) > delivered_before_cancel
    ):
        raise _fail(
            "runner.cancel",
            "every declared shard was delivered after the cancellation — the runner did not stop "
            "(raise DispatchCancelled via ctx.raise_if_cancelled(), or return with shards outstanding)",
        )
    if ctx.is_declared:
        _check_declared(ctx)
        if preview is not None:
            _require(
                set(preview.shards) == set(ctx.declared or ()),
                "runner.plan-matches-dispatch",
                f"plan() previewed the shards {sorted(preview.shards)}, the dispatch declared "
                f"{sorted(ctx.declared or ())} — a preview shows what the dispatch does",
            )
        if ended == "complete" and not allow_missing:
            _check_reported(ctx)
    if expect is not None:
        _require(
            ended == expect,
            "runner.expected-outcome",
            f"the dispatch ended {ended!r}, expected {expect!r}"
            + (f" — {type(error).__name__}: {error}" if error is not None else "")
            + (f"; outstanding shards {ctx.outstanding}" if ended == "awaiting" else ""),
        )
    else:
        if ended == "failed":
            raise _fail("runner.raised", f"dispatch() raised after the hand-off: {type(error).__name__}: {error}") from error
    if ended == "awaiting":
        _require(
            "external_delivery" in info.capabilities,
            "runner.external-delivery",
            f"the runner returned with shards {ctx.outstanding} outstanding but does not declare "
            "the external_delivery capability — say so, or deliver every declared shard",
        )
    if cancel_after_declare:
        _require(
            ended in ("cancelled", "failed") or not ctx.outstanding,
            "runner.cancel", f"after a cancellation the dispatch ended {ended!r}",
        )
    return ctx


def _check_reported(ctx: FakeDispatchContext) -> None:
    """A complete dispatch reports every planned item, under names the
    catalog knows — otherwise runcomposer scores the run INCOMPLETE."""
    unknown = [message for message in ctx.warnings() if message.startswith("unknown native name")]
    _require(
        not unknown,
        "runner.correlates",
        f"delivered native names the catalog does not resolve: {unknown[:3]} — the source's "
        "resolve() must map every name the results spell (or pass allow_missing=True)",
    )
    reported = {v.item_id for v in ctx.verdicts}
    unreported = [item_id for item_id in ctx.item_ids if item_id not in reported]
    _require(
        not unreported,
        "runner.reports-every-item",
        f"{len(unreported)} planned item(s) were never reported (first: {unreported[:3]}) — "
        "runcomposer counts them MISSING and the run INCOMPLETE (or pass allow_missing=True)",
    )


def _is_order_error(error: RuntimeError) -> bool:
    text = str(error)
    return (
        "already declared" in text
        or "deliver() before declare()" in text
        or "refused after the hand-off" in text
    )


def _check_plan(runner: Any, ctx: FakeDispatchContext) -> PlanPreview | None:
    """A runner that declares the ``plan`` capability: its preview is a
    well-formed ``PlanPreview`` and leaves no trace — nothing declared,
    delivered, recorded, exported or logged. ``None`` when the plan refuses
    (a dispatch of the same context is then expected to be refused too)."""
    before = (len(ctx.deliveries), len(ctx.artifacts), len(ctx.logs), len(ctx.tokens), ctx.exported)
    refusal = ctx.refusal
    try:
        preview = ctx.preview(runner)
    except AttributeError as exc:
        raise _fail(
            "runner.plan-is-read-only",
            f"plan() reached for something a preview does not have: {exc} — it gets a "
            "PlanContext (no declare, deliver, artifacts, token or work directory)",
        ) from exc
    except Exception as exc:
        raise _fail("runner.plan", f"plan() raised {type(exc).__name__}: {exc}") from exc
    after = (len(ctx.deliveries), len(ctx.artifacts), len(ctx.logs), len(ctx.tokens), ctx.exported)
    _require(
        not ctx.is_declared and after == before,
        "runner.plan-is-read-only", "plan() declared, delivered or recorded something",
    )
    if preview is None:
        ctx.refusal = refusal  # the dispatch below says for itself why it refuses
        return None
    _require(isinstance(preview.text, str), "runner.plan", "PlanPreview.text must be a string")
    planned = set(ctx.item_ids)
    for shard, items in preview.shards.items():
        _require(isinstance(shard, str) and bool(shard), "runner.plan",
                 f"a previewed shard name must be a non-empty string, got {shard!r}")
        stray = [item for item in items if item not in planned]
        _require(not stray, "runner.plan",
                 f"previewed shard {shard!r} lists item(s) that were not planned: {stray[:3]}")
    stray_groups = sorted(set(preview.groups) - set(preview.shards))
    _require(not stray_groups, "runner.plan",
             f"PlanPreview.groups labels shard(s) the preview does not list: {stray_groups}")
    return preview


def _check_info(info: Any) -> None:
    _require(isinstance(info, RunnerInfo), "runner.describe", f"describe() must return a RunnerInfo, got {info!r}")
    _require(isinstance(info.id, str) and info.id, "runner.describe", "RunnerInfo.id must be a non-empty string")
    _require(
        isinstance(info.capabilities, tuple) and all(isinstance(c, str) for c in info.capabilities),
        "runner.capabilities", f"capabilities must be a tuple of strings, got {info.capabilities!r}",
    )
    _require(
        isinstance(info.parameters, tuple) and all(isinstance(p, Parameter) for p in info.parameters),
        "runner.parameters", f"parameters must be a tuple of Parameter, got {info.parameters!r}",
    )
    names = [p.name for p in info.parameters]
    _require(len(set(names)) == len(names), "runner.parameters", f"parameter names must be distinct: {names}")
    for parameter in info.parameters:
        _require(parameter.kind in PARAMETER_KINDS, "runner.parameters", f"{parameter.name}: unknown kind {parameter.kind!r}")
    if not any(p.required for p in info.parameters):
        try:
            validate_parameters(info.parameters, {})
        except InvalidParameter as exc:
            raise _fail("runner.parameter-defaults", f"the declared defaults do not validate: {exc}") from exc
    _require(
        info.result_format is None or (isinstance(info.result_format, str) and info.result_format),
        "runner.describe", f"result_format must be None or a format id, got {info.result_format!r}",
    )


def _check_declared(ctx: FakeDispatchContext) -> None:
    declared = ctx.declared or ()
    _require(declared, "runner.shards", "declare() was called with no shards — nothing can ever complete")
    planned = set(ctx.item_ids)
    for shard, items in ctx.shard_items.items():
        stray = sorted(set(items) - planned)
        _require(not stray, "runner.shard-items", f"shard {shard!r} carries items that were not planned: {stray[:5]}")
    for delivery in ctx.deliveries:
        _require(
            delivery.shard in declared,
            "runner.delivers-declared-shards",
            f"delivered shard {delivery.shard!r}, which was not declared ({list(declared)})",
        )
        stray = sorted({v.item_id for v in delivery.verdicts} - planned)
        _require(
            not stray,
            "runner.reports-planned-items",
            f"shard {delivery.shard!r} reported items that were not planned: {stray[:5]}",
        )


# -- RunObserver -----------------------------------------------------------------------


def sample_events(run_id: str = "01KITRUN000000000000000000") -> list[RunEvent]:
    """One event of every kind an observer may be told about."""
    kinds = (*LIFECYCLE_KINDS, LOG, WARNING)
    return [
        RunEvent(
            seq=index + 1,
            run_id=run_id,
            at=utc_now(),
            kind=kind,
            dispatch_id="01KITDISPATCH0000000000000",
            data={"state": "RUNNING", "title": "Checkout before the release", "message": "ok"},
        )
        for index, kind in enumerate(kinds)
    ]


def check_observer(
    observer: Any,
    *,
    seen: Callable[[], Sequence[Any]] | None = None,
    kinds: Sequence[str] | None = None,
    threads: int = 4,
) -> None:
    """Check a :class:`runcomposer.sdk.RunObserver`: it accepts every event
    kind, returns ``None``, and copes with being called from several threads
    at once. With ``seen`` — a callable returning what your observer's
    destination received so far (requests, lines, files) — every
    notification must have arrived exactly once.

    ``kinds`` is for an observer that passes on only some event kinds by its
    own decision (a summary of finished runs): the kinds it does pass on, as
    names or ``fnmatch`` patterns (``"run.*"``). ``seen`` is then expected to
    grow by exactly the notifications of those kinds — the others must be
    accepted and must leave no trace."""
    _members(observer, RunObserver, "observer.contract")
    before = len(seen()) if seen is not None else 0
    events = sample_events()
    for event in events:
        try:
            result = observer.notify(event)
        except Exception as exc:
            raise _fail("observer.accepts-every-kind", f"notify() raised on {event.kind!r}: {type(exc).__name__}: {exc}") from exc
        _require(result is None, "observer.returns-none", f"notify() returned {result!r}")
    errors: list[BaseException] = []

    def burst() -> None:
        try:
            for event in events:
                observer.notify(event)
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=burst) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)
    _require(not errors, "observer.thread-safe", f"concurrent notify() raised: {errors[:1]}")
    if seen is not None:
        passed_on = [
            event for event in events
            if kinds is None or any(fnmatchcase(event.kind, pattern) for pattern in kinds)
        ]
        if kinds is not None:
            _require(
                bool(passed_on), "observer.kinds",
                f"kinds={list(kinds)} matches no event kind an observer is told about "
                f"({[event.kind for event in events]})",
            )
        expected = len(passed_on) * (1 + threads)
        got = len(seen()) - before
        _require(
            got == expected, "observer.delivers-each",
            f"{expected} notifications "
            + ("made" if kinds is None else f"of the kinds {list(kinds)} made")
            + f", {got} arrived",
        )


# -- RunStore ----------------------------------------------------------------------------


def _spec(run_id: str, *, title: str = "Kit run", labels: Mapping[str, str] | None = None,
          created_at: str = "2026-07-07T10:00:00Z", ids: Sequence[str] = DEFAULT_ITEM_IDS) -> dict[str, Any]:
    return build_spec(
        title=title,
        tag_filter="Cart",
        materialized_ids=list(ids),
        source_provider="manifest",
        snapshot="sha256:" + "ab" * 32,
        results={"expect": [{"format": "runcomposer-verdicts"}], "shards": 1},
        labels=dict(labels) if labels else None,
        run_id=run_id,
        created_at=created_at,
    )


def check_store(make_store: Callable[[], Any], *, threads: int = 8) -> None:
    """Check a :class:`runcomposer.sdk.RunStore`. ``make_store`` returns a
    fresh, empty store each time it is called (an in-memory one, or a file in
    a new temporary directory). The checks cover the history queries, the
    dispatch records, the §5 idempotency contract — including concurrent
    deliveries to one shard from ``threads`` threads — the exclusive
    dispatch claim, live status, artifacts and the event stream."""
    store = make_store()
    try:
        _members(store, RunStore, "store.contract")
        _require(isinstance(store.store_id, str) and store.store_id, "store.store-id", "store_id must be a non-empty string")
        _check_runs(store)
        _check_dispatches(store)
        _check_deliveries(store)
        _check_live_and_artifacts(store)
        _check_events(store)
        _check_history(store)
        _check_delete(store)
    finally:
        store.close()
    store = make_store()
    try:
        _check_concurrent_delivery(store, threads)
    finally:
        store.close()


def _check_runs(store: Any) -> None:
    rule = "store.runs"
    spec = _spec("01KITRUNA00000000000000000", title="Zahlungen prüfen", labels={"suite": "nightly"},
                 created_at="2026-07-07T12:00:00+02:00")
    run = store.create_run(spec, origin="kit")
    _require(run.id == "01KITRUNA00000000000000000", rule, f"create_run returned id {run.id!r}")
    _require(run.state == "COMPOSED", rule, f"a new run is COMPOSED, got {run.state!r}")
    _require(run.created_at == "2026-07-07T10:00:00Z", "store.created-at-utc",
             f"created_at is stored normalized to UTC: got {run.created_at!r} for 12:00+02:00")
    _require(run.planned_count == len(DEFAULT_ITEM_IDS), rule, f"planned_count {run.planned_count}")
    _require(dict(run.labels) == {"suite": "nightly"}, rule, f"labels {dict(run.labels)}")
    _require(store.get_spec_document(run.id) == spec, "store.spec-roundtrip", "the stored document differs from the one given")
    changed = {**spec, "runner": {"demo": {"seed": "x"}}}
    store.set_spec_document(run.id, changed)
    _require(store.get_spec_document(run.id) == changed, "store.spec-roundtrip", "set_spec_document did not replace the document")
    _require(store.get_run("01NOSUCHRUN000000000000000") is None, rule, "get_run of an unknown id must be None")
    _require(store.get_spec_document("01NOSUCHRUN000000000000000") is None, rule, "get_spec_document of an unknown id must be None")
    store.create_run(_spec("01KITRUNB00000000000000000", title="Checkout smoke", created_at="2026-07-07T11:00:00Z"), origin="kit")
    store.create_run(_spec("01KITRUNC00000000000000000", title="Cart regression", labels={"suite": "nightly"},
                           created_at="2026-07-07T09:00:00Z"), origin="kit")
    page = store.list_runs()
    ids = [r.id for r in page.runs]
    _require(ids == ["01KITRUNB00000000000000000", "01KITRUNA00000000000000000", "01KITRUNC00000000000000000"],
             "store.list-newest-first", f"list_runs order {ids}")
    _require(page.total == 3, "store.list-total", f"total {page.total}")
    nightly = store.list_runs(labels={"suite": "nightly"})
    _require({r.id for r in nightly.runs} == {"01KITRUNA00000000000000000", "01KITRUNC00000000000000000"} and nightly.total == 2,
             "store.list-labels", f"label filter gave {[r.id for r in nightly.runs]}")
    found = store.list_runs(q="ZAHLUNG")
    _require([r.id for r in found.runs] == ["01KITRUNA00000000000000000"], "store.list-q",
             "q is a case-insensitive title substring (non-ASCII titles included)")
    paged = store.list_runs(limit=1, offset=1)
    _require([r.id for r in paged.runs] == ["01KITRUNA00000000000000000"] and paged.total == 3,
             "store.list-paging", f"limit/offset gave {[r.id for r in paged.runs]}, total {paged.total}")
    since = store.list_runs(since="2026-07-07T10:00:00Z")
    _require({r.id for r in since.runs} == {"01KITRUNA00000000000000000", "01KITRUNB00000000000000000"},
             "store.list-since", f"since gave {[r.id for r in since.runs]}")
    for run_id in ("01KITRUNA00000000000000000", "01KITRUNB00000000000000000", "01KITRUNC00000000000000000"):
        store.delete_run(run_id)


def _check_dispatches(store: Any) -> None:
    rule = "store.dispatches"
    run = store.create_run(_spec("01KITRUND00000000000000000"), origin="kit")
    first = store.add_dispatch(run.id, dispatch_id="01KITD1", mode="demo", state="DISPATCHED",
                               owner="host:1:token", parameters={"seed": "a"}, exclusive=True)
    _require((first.dispatch_id, first.mode, first.state, first.owner) == ("01KITD1", "demo", "DISPATCHED", "host:1:token"),
             rule, f"add_dispatch returned {first!r}")
    _require(dict(first.parameters) == {"seed": "a"}, rule, "parameters are kept")
    try:
        store.add_dispatch(run.id, dispatch_id="01KITD2", mode="demo", state="DISPATCHED", exclusive=True)
    except RunActive:
        pass
    else:
        raise _fail("store.exclusive-dispatch",
                    "add_dispatch(exclusive=True) must raise RunActive while the run's latest dispatch is in "
                    f"one of {ACTIVE_STATES}")
    updated = store.update_dispatch("01KITD1", state="RUNNING", shards=["a", "b"], shard_items={"a": [DEFAULT_ITEM_IDS[0]]},
                                    shard_groups={"a": "staging", "b": "staging"},
                                    plan="two shards", links={"build": "https://ci.shop.example/job/7/"},
                                    error="", spec_sha256="sha256:00")
    _require(dict(updated.shard_groups) == {"a": "staging", "b": "staging"}, "store.shard-groups",
             f"update_dispatch(shard_groups=…) must be kept and read back: {updated.shard_groups!r}")
    _require(updated.shards == ("a", "b") and dict(updated.links) == {"build": "https://ci.shop.example/job/7/"}
             and updated.plan == "two shards" and tuple(updated.shard_items["a"]) == (DEFAULT_ITEM_IDS[0],),
             rule, f"update_dispatch did not keep what it was given: {updated!r}")
    _require(store.get_dispatch("01KITD1") == updated, rule, "get_dispatch differs from update_dispatch's answer")
    _require([d.dispatch_id for d in store.owned_dispatches()] == ["01KITD1"], "store.owned-dispatches",
             "owned_dispatches lists every dispatch that names an owner")
    store.update_dispatch("01KITD1", state="COMPLETE", owner=None, completion="PASS", completed_at="2026-07-07T10:05:00Z")
    _require(store.owned_dispatches() == [], "store.owned-dispatches", "a released dispatch is not owned")
    store.add_dispatch(run.id, dispatch_id="01KITD2", mode="export", state="AWAITING_RESULTS", exclusive=True)
    _require([d.dispatch_id for d in store.get_run(run.id).dispatches] == ["01KITD1", "01KITD2"], rule,
             "get_run lists dispatches oldest first")
    content = b"{\n  \"runspec\": \"1.0\"\n}\n"
    store.set_dispatch_spec("01KITD2", content, "application/json")
    _require(store.get_dispatch_spec("01KITD2") == (content, "application/json"), "store.dispatch-spec-bytes",
             "the handed-out bytes must come back byte for byte")
    store.set_ingest_token(run.id, "01KITD2", "sha256:t1")
    store.set_ingest_token(run.id, "01KITD2", "sha256:t2")
    store.set_ingest_token(run.id, None, "sha256:run")
    _require(store.ingest_tokens(run.id) == {"sha256:t2": "01KITD2", "sha256:run": None}, "store.ingest-tokens",
             f"a new token for a dispatch replaces its previous one: {store.ingest_tokens(run.id)}")
    store.delete_dispatch("01KITD2")
    _require(store.get_dispatch("01KITD2") is None, rule, "delete_dispatch did not remove it")
    store.delete_run(run.id)


def _deliver(store: Any, run_id: str, dispatch_id: str, shard: str, content_hash: str,
             verdicts: Sequence[Verdict]) -> str:
    return str(store.record_delivery(run_id, dispatch_id=dispatch_id, shard=shard, content_hash=content_hash,
                                     format="runcomposer-verdicts", verdicts=verdicts))


def _check_deliveries(store: Any) -> None:
    run = store.create_run(_spec("01KITRUNE00000000000000000"), origin="kit")
    store.add_dispatch(run.id, dispatch_id="01KITE1", mode="demo", state="RUNNING")
    t1, t2, _t3 = DEFAULT_ITEM_IDS
    first = [Verdict(t1, "PASS", 10), Verdict(t2, "FAIL", 20, "card declined", attempt=1),
             Verdict(t2, "PASS", 25, attempt=2)]
    _require(_deliver(store, run.id, "01KITE1", "1", "sha256:a", first) == "new", "store.delivery-new",
             "the first delivery for a shard is 'new'")
    _require(_deliver(store, run.id, "01KITE1", "1", "sha256:a", first) == "duplicate", "store.delivery-duplicate",
             "the same content again for that shard is a 'duplicate' no-op")
    verdicts = store.verdicts_for(run.id, "01KITE1")
    _require(len(verdicts) == 3, "store.delivery-duplicate", f"a duplicate must not add verdicts: {len(verdicts)}")
    _require(all(v.shard == "1" for v in verdicts), "store.verdict-shard", "every verdict read back carries its shard")
    _require(sorted((v.item_id, v.attempt) for v in verdicts) == [(t1, 1), (t2, 1), (t2, 2)], "store.verdict-attempt",
             "every verdict read back carries its attempt")
    corrected = [Verdict(t1, "PASS", 11), Verdict(t2, "PASS", 21)]
    _require(_deliver(store, run.id, "01KITE1", "1", "sha256:b", corrected) == "replaced", "store.delivery-replaced",
             "different content for a delivered shard 'replaced's it")
    _require(sorted((v.item_id, v.status) for v in store.verdicts_for(run.id, "01KITE1")) == [(t1, "PASS"), (t2, "PASS")],
             "store.delivery-replaced", "a replacement leaves only the new delivery's verdicts")
    _require(len(store.get_run(run.id).deliveries) == 1, "store.delivery-replaced", "one delivery per shard")
    _deliver(store, run.id, "01KITE1", "2", "sha256:c", [Verdict(_t3, "SKIP")])
    _require(store.delivered_shards(run.id, "01KITE1") == {"1", "2"}, "store.delivered-shards",
             f"delivered_shards: {store.delivered_shards(run.id, '01KITE1')}")
    _require([v.item_id for v in store.verdicts_for(run.id, shard="2")] == [_t3], "store.verdicts-for",
             "verdicts_for filters by shard")
    store.add_dispatch(run.id, dispatch_id="01KITE2", mode="demo", state="RUNNING")
    _require(_deliver(store, run.id, "01KITE2", "1", "sha256:b", corrected) == "new", "store.delivery-per-dispatch",
             "the same content under another dispatch is a new delivery")
    store.delete_run(run.id)


def _check_live_and_artifacts(store: Any) -> None:
    run = store.create_run(_spec("01KITRUNF00000000000000000"), origin="kit")
    store.add_dispatch(run.id, dispatch_id="01KITF1", mode="demo", state="RUNNING")
    t1, t2, _t3 = DEFAULT_ITEM_IDS
    store.record_live_verdict(run.id, "01KITF1", "1", LiveStatus(t1, "RUNNING"))
    running = [(v.shard, v.item_id, v.status) for v in store.live_verdicts(run.id, "01KITF1")]
    _require(running == [("1", t1, "RUNNING")], "store.live-running",
             f"a RUNNING live status is kept and read back as a LiveStatus: {running}")
    store.record_live_verdict(run.id, "01KITF1", "1", LiveStatus(t1, "FAIL", 5))
    store.record_live_verdict(run.id, "01KITF1", "1", LiveStatus(t1, "PASS", 7))
    store.record_live_verdict(run.id, "01KITF1", "2", LiveStatus(t2, "PASS", 3))
    live = sorted((v.shard, v.item_id, v.status) for v in store.live_verdicts(run.id, "01KITF1"))
    _require(live == [("1", t1, "PASS"), ("2", t2, "PASS")], "store.live-replaced",
             f"live status is replaced per (shard, item): {live}")
    _deliver(store, run.id, "01KITF1", "1", "sha256:l", [Verdict(t1, "PASS")])
    remaining = [(v.shard, v.item_id) for v in store.live_verdicts(run.id, "01KITF1")]
    _require(remaining == [("2", t2)], "store.live-cleared-by-delivery", f"a delivery clears its shard's live status: {remaining}")
    _require(store.verdicts_for(run.id, "01KITF1") and all(v.item_id == t1 for v in store.verdicts_for(run.id, "01KITF1")),
             "store.live-apart", "live status is kept apart from delivered verdicts")
    store.add_artifact_ref(run.id, "01KITF1", name="log.html", media_type="text/html", url_or_path="01KITF1/1/log.html", shard="1")
    store.add_artifact_ref(run.id, None, name="Build", media_type="text/html", url_or_path="https://ci.shop.example/job/7/")
    store.add_artifact_ref(run.id, "01KITF1", name="staging log", media_type="text/html",
                           url_or_path="01KITF1/staging.html", group="staging")
    refs = store.artifact_refs(run.id)
    _require([(r.name, r.dispatch_id, r.shard, r.group) for r in refs]
             == [("log.html", "01KITF1", "1", None), ("Build", None, None, None),
                 ("staging log", "01KITF1", None, "staging")],
             "store.artifacts", f"artifact_refs (name, dispatch, shard, group): {refs}")
    _require([r.name for r in store.artifact_refs(run.id, "01KITF1")] == ["log.html", "staging log"], "store.artifacts",
             "artifact_refs filters by dispatch")
    store.delete_run(run.id)


def _check_events(store: Any) -> None:
    rule = "store.events"
    run = store.create_run(_spec("01KITRUNG00000000000000000"), origin="kit")
    other = store.create_run(_spec("01KITRUNH00000000000000000"), origin="kit")
    written = []
    for n in range(6):
        target = run.id if n % 3 else other.id
        written.append(store.append_event(target, kind=f"log.{n}", at=utc_now(), dispatch_id=None,
                                          data={"n": n, "title": "Zahlungen prüfen"}))
    seqs = [event.seq for event in written]
    _require(seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "store.event-cursor",
             f"seq must increase strictly across runs: {seqs}")
    mine = [e for e in written if e.run_id == run.id]
    _require(store.events(run.id) == mine, rule, "events() returns the run's events, oldest first, data intact")
    _require(store.events(run.id, after=mine[0].seq) == mine[1:], rule, "events(after=) reads after the cursor")
    _require(store.events(run.id, limit=2) == mine[:2], rule, "events(limit=) bounds the read")
    _require(store.latest_events(run.id, limit=2) == mine[-2:], "store.latest-events",
             "latest_events returns the last `limit` events, oldest first")
    _require(store.append_event(run.id, kind="log", at=utc_now()).data == {}, rule, "no data is an empty mapping")
    store.delete_run(run.id)
    store.delete_run(other.id)


def _check_history(store: Any) -> None:
    t1, t2, t3 = DEFAULT_ITEM_IDS
    older = store.create_run(_spec("01KITRUNI00000000000000000", labels={"suite": "nightly"}), origin="kit")
    newer = store.create_run(_spec("01KITRUNJ00000000000000000", labels={"suite": "adhoc"}), origin="kit")
    for run, completed_at, durations in ((older, "2026-07-07T11:00:00Z", (100, 300)), (newer, "2026-07-07T12:00:00Z", (200, 500))):
        dispatch_id = run.id + "D"
        store.add_dispatch(run.id, dispatch_id=dispatch_id, mode="demo", state="RUNNING")
        _deliver(store, run.id, dispatch_id, "1", f"sha256:{run.id}",
                 [Verdict(t1, "PASS", durations[0]), Verdict(t2, "FAIL", durations[1])])
        store.update_dispatch(dispatch_id, state="COMPLETE", completion="FAIL", completed_at=completed_at)
        store.set_run_headline(run.id, "COMPLETE", completion="FAIL", completed_at=completed_at)
    latest = store.latest_completed_run()
    _require(latest is not None and latest.id == newer.id, "store.latest-completed", "latest means latest completed_at")
    scoped = store.latest_completed_run(labels={"suite": "nightly"})
    _require(scoped is not None and scoped.id == older.id, "store.latest-completed-labels", "labels scope the lookup")
    bounded = store.latest_completed_run(completed_before="2026-07-07T11:30:00Z")
    _require(bounded is not None and bounded.id == older.id, "store.latest-completed-before", "completed_before bounds it")
    means = store.duration_aggregates(last_n=5)
    _require(means.get(t1) == 150 and means.get(t2) == 400, "store.duration-aggregates",
             f"mean duration per item over the last dispatches: {means}")
    _require(store.duration_aggregates(labels={"suite": "nightly"}).get(t1) == 100, "store.duration-aggregates",
             "labels scope the aggregates")
    passed = store.duration_aggregates(last_n=5, statuses=("PASS",))
    _require(passed == {t1: 150}, "store.duration-statuses",
             f"statuses restricts the mean to executions that ended with one of them: {passed}")
    stored = store.seed_durations({t1: 9000, t3: 700, t2: 0}, labels={"suite": "nightly"})
    seeded = store.duration_aggregates(labels={"suite": "nightly"})
    _require(stored == 2 and seeded.get(t3) == 700 and seeded.get(t1) == 100, "store.duration-seeds",
             "a seeded duration answers for an item nothing was measured for, never over a "
             f"measurement, and 0 is not a duration: stored {stored}, {seeded}")
    _require(t3 not in store.duration_aggregates(labels={"suite": "adhoc"}), "store.duration-seeds",
             "a seed's labels scope it like a run's labels scope its history")
    store.seed_durations({t3: 900}, labels={"suite": "nightly"})
    _require(store.duration_aggregates(labels={"suite": "nightly"}).get(t3) == 900, "store.duration-seeds",
             "importing an item again under the same labels replaces its seed")
    _require(store.list_runs(state="COMPLETE").total == 2, "store.list-state", "list_runs filters by headline state")
    store.delete_run(older.id)
    store.delete_run(newer.id)


def _check_delete(store: Any) -> None:
    run = store.create_run(_spec("01KITRUNK00000000000000000"), origin="kit")
    store.add_dispatch(run.id, dispatch_id="01KITK1", mode="demo", state="RUNNING")
    _deliver(store, run.id, "01KITK1", "1", "sha256:k", [Verdict(DEFAULT_ITEM_IDS[0], "PASS")])
    store.append_event(run.id, kind="log", at=utc_now())
    store.add_artifact_ref(run.id, "01KITK1", name="x", media_type="text/plain", url_or_path="x")
    _require(store.delete_run(run.id) is True, "store.delete", "delete_run returns True for a run it removed")
    _require(store.get_run(run.id) is None and store.verdicts_for(run.id) == [] and store.events(run.id) == []
             and store.artifact_refs(run.id) == [] and store.get_dispatch("01KITK1") is None,
             "store.delete", "delete_run removes everything recorded for the run")
    _require(store.delete_run(run.id) is False, "store.delete", "delete_run returns False for an unknown run")
    for n, created in enumerate(("2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z", "2026-07-03T00:00:00Z")):
        store.create_run(_spec(f"01KITPRUNE{n}0000000000000000"[:26], created_at=created), origin="kit")
    pruned = store.prune_runs(max_runs=1)
    _require(len(pruned) == 2 and store.list_runs().total == 1, "store.prune", f"prune_runs(max_runs=1) removed {pruned}")


def _check_concurrent_delivery(store: Any, threads: int) -> None:
    run = store.create_run(_spec("01KITRUNL00000000000000000"), origin="kit")
    store.add_dispatch(run.id, dispatch_id="01KITL1", mode="demo", state="RUNNING")
    barrier = threading.Barrier(threads)
    outcomes: list[str] = []
    errors: list[BaseException] = []

    def write(n: int) -> None:
        try:
            barrier.wait(timeout=30)
            marker = f"writer-{n}"
            outcomes.append(_deliver(store, run.id, "01KITL1", "1", f"sha256:{marker}",
                                     [Verdict(item_id, "PASS", message=marker) for item_id in DEFAULT_ITEM_IDS]))
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=write, args=(n,)) for n in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)
    _require(not errors, "store.thread-safe", f"concurrent record_delivery raised: {errors[:1]}")
    _require(sorted(outcomes) == ["new"] + ["replaced"] * (threads - 1), "store.atomic-delivery",
             f"concurrent deliveries to one shard: {sorted(outcomes)}")
    verdicts = store.verdicts_for(run.id, "01KITL1")
    _require(len(verdicts) == len(DEFAULT_ITEM_IDS) and len({v.message for v in verdicts}) == 1,
             "store.atomic-delivery", "exactly one delivery's verdicts survive, never a mixture")
    _require(len(store.get_run(run.id).deliveries) == 1, "store.atomic-delivery", "exactly one delivery is recorded")
