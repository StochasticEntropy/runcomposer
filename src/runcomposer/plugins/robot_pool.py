"""`robot-pool` Runner (DESIGN.md §6.2a): Robot Framework executed on this
machine, in worker processes. Built on ``runcomposer.sdk.robot`` — the same
toolkit a connector composes when this runner's switches are not enough.

What it does with one dispatch:

1. **Drift check** (§3.3): the live catalog's snapshot against the spec's.
   Refuse by default; with ``allow_drift`` execute the intersection and report
   the rest as ``SKIP`` / ``drift``.
2. **Plan**: how the selection meets the ``partitions``.

   - ``partition_mode: each`` (default) — the selection runs once *per*
     chosen partition (the same tests against every environment); each
     partition is cut into duration-balanced chunks that run in parallel —
     at most ``max_chunks_per_partition`` of them (``1``: a partition never
     executes two chunks at once).
   - ``partition_mode: split`` — every test runs *once*; the chosen
     partitions are parallel lanes the selection is distributed over,
     balanced by duration.

   With ``partition_mode_per_run`` the mode is a per-run parameter as well
   (``partition_mode``, default the configured one).

   A chunk is one shard. With ``max_tests_per_chunk`` a chunk is executed in
   pieces of at most that many tests, one after another; with
   ``chunk_shards`` each piece is a shard of its own, delivered when it is
   done — instead of the pieces being merged into one result per chunk.
   Shards are grouped by partition. History comes from the store
   (round-robin on a cold start, and the plan says so). Every item is
   checked against the suite roots: one the roots would not find refuses
   the dispatch.
3. **Checks**: ``plan_checks`` — configured callables, free of side
   effects — run for a preview and for a dispatch alike, and may refuse.
4. **Readiness**: ``pre_run_hooks`` (shell commands) and ``pre_dispatch``
   (configured callables called with the context and the plan: which
   partitions, which shards, the per-run parameters). Either may refuse the
   dispatch; nothing has been handed off yet. A preview does not run them.
5. **Hand-off**: ``ctx.declare`` with every shard's item ids and group.
6. **Execute**: one spawned worker process per shard, at most
   ``max_workers`` at a time; failures are re-executed ``rerun_failed`` times.
7. **Deliver** each shard as it finishes, parsed with the configured
   ResultParser, every attempt labelled — its artifacts recorded first.
   With ``merge_groups`` one merged result per group and one for the run are
   produced in a spawned process and recorded before the last delivery.

Tests are selected by **exact id**, not by name pattern: an id containing
``[`` or ``*`` is that one test. Several suite roots are supported — items
are attributed to the root they came from. A root that mixes tests and tasks
needs ``rpa: false`` (or ``true``): left to itself Robot refuses a selection
that keeps both.

Per-run parameters: ``partitions`` and ``allow_drift`` (and
``partition_mode`` when opted in), plus whatever ``run_parameters``
declares; ``parameter_variables`` turns those into Robot variables, and
``protected_variables`` names variables a per-run mapping may not set.

Every option that names a callable takes ``"pkg.mod:callable"`` or
``{call: "pkg.mod:callable", with: {…}}`` — the ``with`` mapping reaches the
callable as ``settings=``.

Live per-item status comes from a listener streaming through the context's
live channel (``live_status``, on by default): ``RUNNING`` when a test
starts, its status when it ends. Workers log their progress to the run's
event stream through the same channel.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runcomposer.sdk import (
    ChunkPlan,
    ConfigError,
    ConfiguredCall,
    DispatchContext,
    Health,
    InvalidParameter,
    Parameter,
    PlanContext,
    PlanPreview,
    PluginError,
    RunnerBase,
    RunnerInfo,
    Verdict,
    configured_calls,
    parameters_from_config,
    plan_chunks,
    split_max,
)
from runcomposer.sdk.robot import (
    Execution,
    MergePayload,
    MergeResult,
    NameTopSuites,
    ShardOutcome,
    ShardPayload,
    attribute_roots,
    deliver_shard,
    robot_version,
    spawn_merge,
    supervise,
)

__all__ = ["RobotPoolRunner"]

#: How often a running pre_run_hook is checked for a cancellation.
_HOOK_POLL_S = 0.2
_PARTITION_MODES = ("each", "split")
_OWN_PARAMETERS = ("partitions", "allow_drift")
_NOT_INSTALLED = "Robot Framework is not installed (pip install 'runcomposer[robot]')"
#: ``${NAME}``, ``@{NAME}``, ``&{NAME}``, ``%{NAME}`` — a variable written
#: the way a Robot file writes it.
_DECORATED = re.compile(r"^[$@&%]\{(.*)\}$", re.DOTALL)


def variable_key(name: str) -> str:
    """A Robot variable name the way Robot compares them: with or without
    its ``${…}`` decoration, case-insensitive (case-folded), spaces and
    underscores ignored — ``${Base URL}``, ``base_url`` and ``BASEURL`` are
    one variable. (The rule of ``robot.utils.normalize(name, ignore="_")``,
    restated so that planning needs no Robot import.)"""
    text = str(name).strip()
    match = _DECORATED.match(text)
    if match:
        text = match.group(1)
    return "".join(text.split()).casefold().replace("_", "")


def _seconds(ms: float) -> str:
    return f"{ms / 1000:.1f}s"


@dataclass
class _Lane:
    """One sequence of shards that execute one after another."""

    name: str
    partition: str
    #: shard name → the executions it consists of
    shards: dict[str, list[Execution]] = field(default_factory=dict)


@dataclass
class _Plan:
    item_ids: list[str]
    partitions: list[str]
    mode: str
    lanes: list[_Lane]
    shards: dict[str, list[str]]
    groups: dict[str, str]
    estimates_ms: dict[str, float]
    drift_skips: list[Verdict]
    text: str
    notes: list[str]
    data: dict[str, Any]


def _configured(entries: Any, option: str) -> tuple[ConfiguredCall, ...]:
    """A list option of configured callables, each ``"pkg.mod:callable"``
    or ``{call: …, with: {…}}`` — the shape checked here, the import at the
    first call."""
    if isinstance(entries, (list, tuple)):
        for entry in entries:
            module, sep, attr = entry.partition(":") if isinstance(entry, str) else ("x", ":", "x")
            if not sep or not module or not attr:
                raise ConfigError(
                    f"robot-pool: {option} entries are import paths 'package.module:callable' "
                    f"(or {{call: 'package.module:callable', with: {{…}}}}), got {entry!r}"
                )
    return configured_calls(entries, f"robot-pool: {option}")


def _call(call: ConfiguredCall, *args: Any) -> Any:
    """Call a configured callable; one that cannot be imported is a
    configuration error naming its option."""
    try:
        call.load()
    except PluginError as exc:
        raise ConfigError(str(exc)) from None
    return call(*args)


def _health_rows(call: ConfiguredCall, info: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What one ``health_checks`` callable says, as ``{check, ok, detail}``
    rows. A check that raises, or answers in another shape, is a failed row
    — never an error of the health answer."""
    try:
        answer = call(dict(info))
    except Exception as exc:
        return [{"check": call.ref, "ok": False, "detail": f"{type(exc).__name__}: {exc}"}]
    entries = [answer] if isinstance(answer, Mapping) else answer
    rows: list[dict[str, Any]] = []
    if not isinstance(entries, (list, tuple)):
        entries = [entries]
    for entry in entries:
        if isinstance(entry, Mapping) and "check" in entry and "ok" in entry:
            rows.append(
                {
                    "check": str(entry["check"]),
                    "ok": entry["ok"] is True,
                    "detail": str(entry.get("detail") or ""),
                }
            )
        else:
            rows.append(
                {
                    "check": call.ref,
                    "ok": False,
                    "detail": f"answered {entry!r} — a health check returns "
                    "{check, ok, detail} rows (one or a list)",
                }
            )
    return rows


class RobotPoolRunner(RunnerBase):
    PATH_OPTIONS = ("suite_root", "suite_roots", "pythonpath")

    def __init__(
        self,
        suite_root: str | None = None,
        suite_roots: list[str] | None = None,
        max_workers: int = 2,
        partitions: list[str] | None = None,
        partition_mode: str = "each",
        partition_mode_per_run: bool = False,
        max_chunks_per_partition: int | None = None,
        chunk_shards: bool = False,
        variables: Mapping[str, str] | None = None,
        partition_variable: str = "PARTITION",
        partition_variables: Mapping[str, Mapping[str, str]] | None = None,
        run_parameters: list[Mapping[str, Any]] | None = None,
        parameter_variables: Mapping[str, str] | None = None,
        protected_variables: list[str] | None = None,
        executed_tag: str | None = None,
        live_status: bool = True,
        allow_drift: bool = False,
        listener: str | list[str] | None = None,
        pre_run_hooks: list[str] | None = None,
        hook_timeout_s: float = 600.0,
        plan_checks: list[Any] | None = None,
        pre_dispatch: list[Any] | None = None,
        worker_setup: list[Any] | None = None,
        health_checks: list[Any] | None = None,
        pythonpath: list[str] | None = None,
        log: bool = True,
        report: bool = True,
        merge_groups: bool = False,
        max_tests_per_chunk: int | None = None,
        rerun_failed: int = 0,
        shard_timeout_s: float | None = None,
        history_selector: Mapping[str, Any] | None = None,
        history_depth: int = 5,
        history_statuses: list[str] | None = None,
        rpa: bool | None = None,
        robot_options: Mapping[str, Any] | None = None,
        format: str | None = None,
    ):
        #: The options as this constructor received them — what a health
        #: check is told.
        self.options = {name: value for name, value in locals().items() if name != "self"}
        roots = ([suite_root] if suite_root else []) + list(suite_roots or [])
        self.suite_roots = [str(root) for root in roots]
        if int(max_workers) < 1:
            raise ConfigError("robot-pool: max_workers must be at least 1")
        self.max_workers = int(max_workers)
        self.partitions = [str(p) for p in (partitions or [])]
        if partition_mode not in _PARTITION_MODES:
            raise ConfigError(
                f"robot-pool: partition_mode must be one of {list(_PARTITION_MODES)}, "
                f"got {partition_mode!r}"
            )
        self.partition_mode = partition_mode
        self.partition_mode_per_run = bool(partition_mode_per_run)
        if max_chunks_per_partition is not None and (
            isinstance(max_chunks_per_partition, bool) or int(max_chunks_per_partition) < 1
        ):
            raise ConfigError(
                "robot-pool: max_chunks_per_partition must be at least 1 (1: a partition never "
                "executes two chunks at once), or left out"
            )
        self.max_chunks_per_partition = (
            int(max_chunks_per_partition) if max_chunks_per_partition is not None else None
        )
        self.chunk_shards = bool(chunk_shards)
        self.variables = {str(k): str(v) for k, v in (variables or {}).items()}
        self.partition_variable = str(partition_variable)
        self.partition_variables = {
            str(partition): {str(k): str(v) for k, v in (values or {}).items()}
            for partition, values in (partition_variables or {}).items()
        }
        unknown = sorted(set(self.partition_variables) - set(self.partitions))
        if unknown:
            raise ConfigError(
                f"robot-pool: partition_variables names {unknown}, which are not in "
                f"partitions {self.partitions}"
            )
        self.run_parameters = parameters_from_config(run_parameters, "robot-pool: run_parameters")
        own = _OWN_PARAMETERS + (("partition_mode",) if self.partition_mode_per_run else ())
        clash = sorted({p.name for p in self.run_parameters} & set(own))
        if clash:
            raise ConfigError(
                f"robot-pool: run_parameters declares {clash}, which the runner declares itself"
            )
        declared = {parameter.name: parameter for parameter in self.run_parameters}
        self.parameter_variables = {str(k): str(v) for k, v in (parameter_variables or {}).items()}
        for name, target in self.parameter_variables.items():
            if name not in declared:
                raise ConfigError(
                    f"robot-pool: parameter_variables names {name!r}, which run_parameters does "
                    f"not declare (declared: {sorted(declared) or 'none'})"
                )
            if (declared[name].kind == "mapping") != (target == "*"):
                raise ConfigError(
                    f"robot-pool: parameter_variables.{name}: a 'mapping' parameter contributes "
                    "its entries as variables and is written as '*'; any other kind names the "
                    "one variable it sets"
                )
        if protected_variables is not None and (
            isinstance(protected_variables, (str, Mapping))
            or not isinstance(protected_variables, (list, tuple))
        ):
            raise ConfigError("robot-pool: protected_variables must be a list of variable names")
        self.protected_variables = [str(name) for name in (protected_variables or [])]
        self._protected = {variable_key(name): name for name in self.protected_variables}
        for name, target in self.parameter_variables.items():
            if target != "*" and variable_key(target) in self._protected:
                raise ConfigError(
                    f"robot-pool: parameter_variables.{name} sets {target!r}, which "
                    "protected_variables protects from per-run values"
                )
        self.executed_tag = executed_tag
        if executed_tag is not None:
            try:
                executed_tag.format(partition="x", shard="x")
            except (KeyError, IndexError, ValueError) as exc:
                raise ConfigError(
                    f"robot-pool: executed_tag {executed_tag!r} may use {{partition}} and "
                    f"{{shard}} only ({exc})"
                ) from None
        self.live_status = bool(live_status)
        self.allow_drift = bool(allow_drift)
        self.listeners = [listener] if isinstance(listener, str) else list(listener or [])
        self.pre_run_hooks = list(pre_run_hooks or [])
        self.hook_timeout_s = float(hook_timeout_s)
        self.plan_checks = _configured(plan_checks, "plan_checks")
        self.pre_dispatch = _configured(pre_dispatch, "pre_dispatch")
        self.worker_setup = _configured(worker_setup, "worker_setup")
        self.health_checks = _configured(health_checks, "health_checks")
        self.pythonpath = [str(entry) for entry in (pythonpath or [])]
        self.log = bool(log)
        self.report = bool(report)
        self.merge_groups = bool(merge_groups)
        self.max_tests_per_chunk = int(max_tests_per_chunk) if max_tests_per_chunk else None
        if self.chunk_shards and not self.max_tests_per_chunk:
            raise ConfigError(
                "robot-pool: chunk_shards makes every max_tests_per_chunk piece a shard — set "
                "max_tests_per_chunk too"
            )
        if int(rerun_failed) < 0:
            raise ConfigError("robot-pool: rerun_failed must not be negative")
        self.rerun_failed = int(rerun_failed)
        self.shard_timeout_s = float(shard_timeout_s) if shard_timeout_s else None
        self.history_labels = dict((history_selector or {}).get("labels") or {})
        self.history_depth = int(history_depth)
        self.history_statuses = (
            tuple(str(status) for status in history_statuses) if history_statuses else None
        )
        if rpa is not None and not isinstance(rpa, bool):
            raise ConfigError("robot-pool: rpa must be true, false or null (let Robot decide)")
        self.rpa = rpa
        self.robot_options = dict(robot_options or {})
        self.format = format

    # -- self-description -----------------------------------------------------------

    def describe(self) -> RunnerInfo:
        parameters = []
        if self.partitions:
            parameters.append(
                Parameter(
                    "partitions",
                    kind="multi_choice",
                    label="Partitions",
                    help=(
                        "The partitions to run on — the selection once per partition "
                        "(each) or distributed over them (split), as partition_mode says."
                        if self.partition_mode_per_run
                        else "Run the selection once per chosen partition."
                        if self.partition_mode == "each"
                        else "Distribute the selection over the chosen partitions "
                        "(every test runs once)."
                    ),
                    choices=tuple(self.partitions),
                    default=list(self.partitions),
                )
            )
        if self.partition_mode_per_run:
            parameters.append(
                Parameter(
                    "partition_mode",
                    kind="choice",
                    label="Partition mode",
                    help="split: every test once, distributed over the chosen partitions; "
                    "each: everything on every chosen partition.",
                    choices=("split", "each"),
                    default=self.partition_mode,
                )
            )
        parameters.append(
            Parameter(
                "allow_drift",
                kind="boolean",
                label="Allow drift",
                help="If the corpus changed since composing, run what still exists "
                "instead of refusing.",
                default=self.allow_drift,
            )
        )
        parameters.extend(self.run_parameters)
        return RunnerInfo(
            id="robot-pool",
            title="Robot Framework (local pool)",
            description="Runs Robot Framework on this machine in worker processes.",
            capabilities=("live_status", "partitions", "cancel", "plan"),
            parameters=tuple(parameters),
            result_format="robot-output-xml",
        )

    def health(self) -> Health:
        """Robot and the suite roots, as ``data.checks`` rows, followed by
        what every ``health_checks`` callable says. A failed row of the
        latter makes the health ``degraded``, naming it."""
        version = robot_version()
        checks: list[dict[str, Any]] = [
            {
                "check": "Robot Framework installed",
                "ok": version is not None,
                "detail": f"Robot Framework {version}" if version else _NOT_INSTALLED,
            }
        ]
        missing = [root for root in self.suite_roots if not Path(root).exists()]
        if self.suite_roots:
            checks.extend(
                {
                    "check": f"suite root {root}",
                    "ok": root not in missing,
                    "detail": "not found" if root in missing else "found",
                }
                for root in self.suite_roots
            )
        else:
            checks.append(
                {"check": "suite root configured", "ok": False, "detail": "no suite_root configured"}
            )
        data: dict[str, Any] = {
            "robot_version": version,
            "max_workers": self.max_workers,
            "suite_roots": self.suite_roots,
            "partitions": self.partitions,
            "partition_mode": self.partition_mode,
            "checks": checks,
        }
        info = {
            "options": dict(self.options),
            "robot_version": version,
            "suite_roots": list(self.suite_roots),
            "partitions": list(self.partitions),
            "partition_mode": self.partition_mode,
            "max_workers": self.max_workers,
        }
        own = [row for call in self.health_checks for row in _health_rows(call, info)]
        checks.extend(own)
        failed = [row["check"] for row in own if not row["ok"]]
        failing = f"health check(s) failed: {', '.join(failed)}" if failed else ""
        if version is None:
            return Health(status="down", detail=_NOT_INSTALLED, data=data)
        if not self.suite_roots:
            return Health(status="down", detail="no suite_root configured", data=data)
        if missing or failed:
            detail = "; ".join(
                part for part in (missing and f"suite root(s) not found: {missing}", failing) if part
            )
            return Health(status="degraded", detail=detail, data=data)
        return Health(status="ok", detail=f"Robot Framework {version}", data=data)

    # -- plan -------------------------------------------------------------------------

    def plan(self, ctx: PlanContext) -> PlanPreview:
        plan = self._plan(ctx)
        notes = list(plan.notes)
        steps = len(self.pre_run_hooks) + len(self.pre_dispatch)
        if steps:
            notes.append(
                f"{steps} readiness step(s) (pre_run_hooks, pre_dispatch) run at dispatch "
                "and may still refuse it — a preview does not run them"
            )
        return PlanPreview(
            text=plan.text,
            shards=plan.shards,
            groups=plan.groups,
            estimates_ms=plan.estimates_ms,
            notes=tuple(notes),
            data=plan.data,
        )

    def _mode(self, ctx: PlanContext) -> str:
        if self.partition_mode_per_run:
            chosen = str(ctx.parameters.get("partition_mode") or self.partition_mode)
            if chosen not in _PARTITION_MODES:
                raise InvalidParameter(
                    f"parameter 'partition_mode': must be one of {list(_PARTITION_MODES)}, "
                    f"got {chosen!r}",
                    parameter="partition_mode",
                )
            return chosen
        return self.partition_mode

    def _plan(self, ctx: PlanContext) -> _Plan:
        """Everything that is decided before anything runs — shared by the
        preview and the dispatch, so that the one shows what the other does."""
        if not self.suite_roots:
            ctx.refuse(
                "robot-pool needs a suite_root (or suite_roots) in its runner configuration"
            )
        if robot_version() is None:
            ctx.refuse(_NOT_INSTALLED)
        self._check_protected(ctx)
        mode = self._mode(ctx)
        item_ids, drift_skips = self._check_drift(ctx)
        partitions = list(ctx.parameters.get("partitions") or self.partitions) or ["default"]
        durations = ctx.history.durations(
            labels=self.history_labels or None,
            last_n=self.history_depth,
            statuses=self.history_statuses,
        )
        try:
            roots_of = attribute_roots(self.suite_roots, item_ids, ctx.source.items())
        except LookupError as exc:
            ctx.refuse(f"{exc} (checked before anything is handed off)")

        lanes: list[_Lane] = []
        if mode == "split":
            chunks = plan_chunks(item_ids, len(partitions), durations)
            for partition, chunk in zip(partitions, chunks.chunks, strict=False):
                lanes.append(self._lane(partition, partition, chunk, roots_of))
            headline = (
                f"robot-pool plan: {len(item_ids)} item(s) distributed over "
                f"{len(chunks.chunks)} of {len(partitions)} partition(s) — each test once, "
                f"max_workers={self.max_workers}"
            )
        else:
            count = max(1, min(len(item_ids) or 1, self.max_workers // len(partitions) or 1))
            if self.max_chunks_per_partition is not None:
                count = min(count, self.max_chunks_per_partition)
            chunks = plan_chunks(item_ids, count, durations)
            numbered = len(partitions) > 1 or len(chunks.chunks) > 1
            for partition in partitions:
                for index, chunk in enumerate(chunks.chunks, start=1):
                    name = f"{partition}-{index}" if numbered else partition
                    lanes.append(self._lane(name, partition, chunk, roots_of))
            headline = (
                f"robot-pool plan: {len(item_ids)} item(s) × {len(partitions)} partition(s) "
                f"→ {len(chunks.chunks)} chunk(s)/partition, max_workers={self.max_workers}"
            )
            if self.max_chunks_per_partition is not None:
                headline += (
                    f", at most {self.max_chunks_per_partition} chunk(s) of one partition "
                    "at a time"
                )

        shards: dict[str, list[str]] = {}
        groups: dict[str, str] = {}
        for lane in lanes:
            for shard, executions in lane.shards.items():
                shards[shard] = [item for execution in executions for item in execution.item_ids]
                if self.partitions:
                    groups[shard] = lane.partition
        estimates = {
            shard: sum(durations.get(item) or 0.0 for item in items)
            for shard, items in shards.items()
            if any(durations.get(item) for item in items)
        }
        if drift_skips:
            shards["drift"] = [verdict.item_id for verdict in drift_skips]

        lane_estimates = self._partition_estimates(mode, partitions, chunks)
        lines = [headline]
        lines.extend("  " + line for line in chunks.describe().splitlines())
        notes: list[str] = []
        if not chunks.balanced and item_ids:
            notes.append(
                "cold start: no duration history for this selection — the distribution is "
                "round-robin until executions (or imported durations) say how long tests take"
            )
        elif chunks.known < chunks.total:
            notes.append(
                f"duration history for {chunks.known} of {chunks.total} item(s); the other "
                f"{chunks.total - chunks.known} are weighed with the mean of the known "
                f"durations, {_seconds(chunks.fallback_ms)} each — the estimates per partition "
                "assume they take that long"
            )
        if chunks.dominant is not None and len(chunks.chunks) > 1:
            item, weight = chunks.dominant
            notes.append(
                f"{item} alone takes {_seconds(weight)}, more than an even share of the "
                f"selection: the {'partition' if mode == 'split' else 'chunk'} holding it is the "
                "longest whatever the split, and the other items are balanced around it — an "
                "uneven count of items per partition is the balance working, not a fault"
            )
        if self.max_tests_per_chunk:
            lines.append(
                f"  at most {self.max_tests_per_chunk} test(s) per execution, "
                + ("each execution a shard of its own" if self.chunk_shards else "merged per shard")
            )
        if self.partitions:
            for partition in partitions:
                own = [shard for shard, label in groups.items() if label == partition]
                line = (
                    f"  {partition}: {len(own)} shard(s), "
                    f"{sum(len(shards[shard]) for shard in own)} item(s)"
                )
                if partition in lane_estimates:
                    line += f", est ~{_seconds(lane_estimates[partition])}"
                lines.append(line)
        if self.rerun_failed:
            lines.append(f"  failures are re-executed up to {self.rerun_failed} time(s)")
        if self.merge_groups:
            lines.append(
                "  one merged result per group and one for the whole run, recorded before "
                "the last shard delivers"
            )
        if drift_skips:
            lines.append(
                f"  drift: {len(drift_skips)} planned item(s) no longer exist — reported as SKIP"
            )
        data: dict[str, Any] = {
            "partitions": partitions,
            "partition_mode": mode,
            "max_workers": self.max_workers,
            "durations": {
                "known": chunks.known,
                "total": chunks.total,
                "fallback_ms": chunks.fallback_ms,
            },
        }
        if lane_estimates:
            data["partition_estimates_ms"] = lane_estimates
        if self.max_chunks_per_partition is not None:
            data["max_chunks_per_partition"] = self.max_chunks_per_partition
        plan = _Plan(
            item_ids=item_ids,
            partitions=partitions,
            mode=mode,
            lanes=lanes,
            shards=shards,
            groups=groups,
            estimates_ms=estimates,
            drift_skips=drift_skips,
            text="\n".join(lines),
            notes=notes,
            data=data,
        )
        self._run_plan_checks(ctx, plan)
        return plan

    def _partition_estimates(
        self, mode: str, partitions: list[str], chunks: ChunkPlan
    ) -> dict[str, float]:
        """How long each partition is estimated to take — the weights the
        balancer used (items without history at the fallback weight): in
        ``split`` its own lane, in ``each`` the longest of its chunks, which
        run side by side."""
        if not chunks.loads_ms or not self.partitions:
            return {}
        if mode == "split":
            return {
                partition: round(load, 1)
                for partition, load in zip(partitions, chunks.loads_ms, strict=False)
            }
        longest = max(chunks.loads_ms)
        return {partition: round(longest, 1) for partition in partitions}

    def _run_plan_checks(self, ctx: PlanContext, plan: _Plan) -> None:
        """``plan_checks``: called for a preview and for a dispatch alike,
        once the plan stands — free of side effects by contract. Each may
        raise ``InvalidParameter`` (a per-run value is at fault) or refuse
        (``ctx.refuse`` / ``DispatchRefused``: the run cannot be executed as
        planned)."""
        if not self.plan_checks:
            return
        info = {
            "partitions": list(plan.partitions),
            "partition_mode": plan.mode,
            "parameters": dict(ctx.parameters),
            "shards": {shard: list(items) for shard, items in plan.shards.items()},
            "groups": dict(plan.groups),
            "item_ids": list(plan.item_ids),
            "plan": plan.text,
        }
        for call in self.plan_checks:
            _call(call, ctx, dict(info))
        plan.notes.append(f"{len(self.plan_checks)} plan check(s) (plan_checks) passed")

    def _lane(
        self, name: str, partition: str, chunk: tuple[str, ...], roots_of: Mapping[str, str]
    ) -> _Lane:
        """One chunk as a lane: its executions per root, each of at most
        ``max_tests_per_chunk`` tests — one shard holding all of them, or
        (``chunk_shards``) one shard per execution."""
        executions: list[Execution] = []
        for root in self.suite_roots:
            in_root = [item for item in chunk if roots_of[item] == root]
            executions.extend(
                Execution(root, tuple(piece))
                for piece in split_max(in_root, self.max_tests_per_chunk)
            )
        lane = _Lane(name=name, partition=partition)
        if self.chunk_shards:
            for index, execution in enumerate(executions, start=1):
                lane.shards[f"{name}-{index:02d}"] = [execution]
        else:
            lane.shards[name] = executions
        return lane

    # -- dispatch ---------------------------------------------------------------------

    def dispatch(self, ctx: DispatchContext) -> None:
        plan = self._plan(ctx)
        self._run_hooks(ctx)
        variables = {**self.variables, **self._parameter_variables(ctx)}
        variables.update(self._pre_dispatch(ctx, plan, variables))

        result_format = self.format or _spec_format(ctx.spec) or "robot-output-xml"
        channel = ctx.live_channel()
        payloads: list[ShardPayload] = []
        for lane in plan.lanes:
            for shard, executions in lane.shards.items():
                shard_variables = {
                    **variables,
                    **self.partition_variables.get(lane.partition, {}),
                    self.partition_variable: lane.partition,
                }
                payloads.append(
                    ShardPayload(
                        shard=shard,
                        out_dir=str(ctx.workdir / shard),
                        executions=tuple(executions),
                        variables=shard_variables,
                        partition=lane.partition,
                        # Pieces of one chunk run one after another; chunks in
                        # parallel.
                        lane=lane.name if len(lane.shards) > 1 else None,
                        channel=channel,
                        live_status=self.live_status,
                        listeners=tuple(self.listeners),
                        executed_tag=(
                            self.executed_tag.format(partition=lane.partition, shard=shard)
                            if self.executed_tag
                            else None
                        ),
                        worker_setup=tuple(self.worker_setup),
                        pythonpath=tuple(self.pythonpath),
                        log=self.log,
                        report=self.report,
                        rerun_failed=self.rerun_failed,
                        rpa=self.rpa,
                        robot_options=self.robot_options,
                        run_id=ctx.run_id,
                        dispatch_id=ctx.dispatch_id,
                    )
                )

        # The hand-off: everything this runner can refuse has been refused.
        ctx.declare(plan.shards, plan=plan.text, groups=plan.groups)
        if plan.drift_skips:
            ctx.deliver("drift", verdicts=plan.drift_skips, format=result_format)
        failures: list[str] = []
        merger = _Merger(self, ctx, plan, payloads) if self.merge_groups else None
        #: With merged results, the shard that finishes last is delivered
        #: only after they are recorded: its delivery completes the run.
        held: list[ShardOutcome] = []
        ended = 0

        def deliver(outcome: ShardOutcome) -> None:
            problem = deliver_shard(
                ctx, outcome, format=result_format, group=plan.groups.get(outcome.payload.shard)
            )
            if problem:
                failures.append(problem)

        def finished(outcome: ShardOutcome) -> None:
            nonlocal ended
            ended += 1
            if merger is not None:
                merger.shard_ended(outcome, last=ended == len(payloads))
                if ended == len(payloads):
                    held.append(outcome)
                    return
                merger.record_finished()
            deliver(outcome)

        try:
            supervise(
                payloads,
                max_workers=self.max_workers,
                on_done=finished,
                cancelled=lambda: ctx.cancelled,
                timeout_s=self.shard_timeout_s,
            )
        finally:
            if merger is not None:
                merger.finish(merge=bool(held) and not ctx.cancelled)
            for outcome in held:
                deliver(outcome)
        ctx.raise_if_cancelled()
        if failures:
            raise RuntimeError(
                f"{len(failures)} of {len(payloads)} shard(s) produced no results: "
                + "; ".join(failures)
            )

    def _parameter_variables(self, ctx: PlanContext) -> dict[str, str]:
        """``parameter_variables``: per-run parameters as Robot variables."""
        variables: dict[str, str] = {}
        for name, target in self.parameter_variables.items():
            if name not in ctx.parameters:
                continue
            value = ctx.parameters[name]
            if target == "*":
                variables.update({str(k): str(v) for k, v in dict(value).items()})
            elif isinstance(value, (list, tuple)):
                variables[target] = ",".join(str(entry) for entry in value)
            else:
                variables[target] = str(value)
        return variables

    def _check_protected(self, ctx: PlanContext) -> None:
        """``protected_variables``: a per-run mapping of Robot variables may
        not name one of them — in any spelling Robot would read as the same
        variable. Refused as an invalid parameter, in a preview and a
        dispatch alike."""
        if not self._protected:
            return
        for name, target in self.parameter_variables.items():
            if target != "*" or name not in ctx.parameters:
                continue
            for key in dict(ctx.parameters[name]):
                protected = self._protected.get(variable_key(str(key)))
                if protected is not None:
                    raise InvalidParameter(
                        f"parameter {name!r}: the Robot variable {str(key)!r} cannot be set per "
                        f"run — this runner protects {protected!r} (protected_variables)",
                        parameter=name,
                    )

    def _pre_dispatch(
        self, ctx: DispatchContext, plan: _Plan, variables: Mapping[str, str]
    ) -> dict[str, str]:
        """``pre_dispatch``: preparation in this process, once the plan stands
        and nothing is handed off yet. Each callable gets the context — so
        it can ``ctx.refuse``, ``ctx.log``, keep files in ``ctx.private_dir``
        — and the plan; a mapping it returns is added to the Robot variables
        of every shard."""
        added: dict[str, str] = {}
        for call in self.pre_dispatch:
            ctx.raise_if_cancelled()
            info = {
                "partitions": list(plan.partitions),
                "partition_mode": plan.mode,
                "parameters": dict(ctx.parameters),
                "shards": {shard: list(items) for shard, items in plan.shards.items()},
                "groups": dict(plan.groups),
                "plan": plan.text,
                "item_ids": list(plan.item_ids),
                "variables": {**variables, **added},
                "workdir": str(ctx.workdir),
            }
            result = _call(call, ctx, info)
            if isinstance(result, Mapping):
                added.update({str(key): str(value) for key, value in result.items()})
            ctx.log(f"pre_dispatch {call.ref}: done")
        return added

    # -- §3.3 drift --------------------------------------------------------------------

    def _check_drift(self, ctx: PlanContext) -> tuple[list[str], list[Verdict]]:
        item_ids = list(ctx.item_ids)
        composed = (ctx.spec.get("source") or {}).get("snapshot")
        live = ctx.source.snapshot()
        if live == composed:
            return item_ids, []
        if not ctx.parameters.get("allow_drift", self.allow_drift):
            ctx.refuse(
                "live corpus snapshot differs from source.snapshot in the spec — refusing "
                f"(§3.3 drift check; composed {composed}, live {live}). "
                "Set the allow_drift parameter to execute the intersection."
            )
        live_ids = {item.id for item in ctx.source.items()}
        kept = [item_id for item_id in item_ids if item_id in live_ids]
        skips = [
            Verdict(item_id=item_id, status="SKIP", message="drift")
            for item_id in item_ids
            if item_id not in live_ids
        ]
        return kept, skips

    def _run_hooks(self, ctx: DispatchContext) -> None:
        """``pre_run_hooks``: shell commands run once per dispatch, before
        anything executes. A failing or overrunning hook refuses the dispatch."""
        for hook in self.pre_run_hooks:
            ctx.raise_if_cancelled()
            # In a session of its own, so that a hook that overruns is stopped
            # together with whatever it started — not just its shell.
            process = subprocess.Popen(
                hook,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=os.name != "nt",
            )
            # Waited for in short steps, so that a cancellation stops a hook
            # that is still running instead of waiting for its timeout.
            # (Retrying communicate() after TimeoutExpired loses no output.)
            deadline = time.monotonic() + self.hook_timeout_s
            while True:
                try:
                    step = max(0.0, min(_HOOK_POLL_S, deadline - time.monotonic()))
                    stdout, stderr = process.communicate(timeout=step)
                    break
                except subprocess.TimeoutExpired:
                    if ctx.cancelled:
                        _stop_hook(process)
                        ctx.raise_if_cancelled()
                    if time.monotonic() >= deadline:
                        _stop_hook(process)
                        ctx.refuse(
                            f"pre_run_hook did not finish within {self.hook_timeout_s:g}s: {hook!r}"
                        )
            if process.returncode != 0:
                ctx.refuse(
                    f"pre_run_hook failed (rc {process.returncode}): {hook!r} — "
                    f"{(stderr or stdout).strip()[:300]}"
                )


class _Merger:
    """``merge_groups``: one merged result per group of shards and one for
    the whole run, each produced by a spawned process (the dispatching
    process never runs Robot itself) and recorded as group / run artifacts.

    A group is merged as soon as its last shard has ended, while the others
    still execute; what that leaves — the last group and the whole run — is
    merged before the shard that ended last is delivered, so that every
    merged artifact is recorded before the run completes. A merge never
    decides a verdict: one that fails is a log line and no artifact, and a
    cancelled dispatch merges nothing more."""

    def __init__(
        self,
        runner: RobotPoolRunner,
        ctx: DispatchContext,
        plan: _Plan,
        payloads: list[ShardPayload],
    ):
        self._runner = runner
        self._ctx = ctx
        self._plan = plan
        self._base = ctx.workdir / "_merged"
        self._groups = sorted(set(plan.groups.values()), key=list(plan.partitions).index)
        self._members = {
            group: [payload.shard for payload in payloads if plan.groups.get(payload.shard) == group]
            for group in self._groups
        }
        self._outcomes: dict[str, ShardOutcome] = {}
        self._results: list[tuple[str | None, MergeResult, int]] = []
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._started: set[str] = set()

    def shard_ended(self, outcome: ShardOutcome, *, last: bool) -> None:
        self._outcomes[outcome.payload.shard] = outcome
        group = self._plan.groups.get(outcome.payload.shard)
        if group is None or last or group in self._started:
            return
        if all(shard in self._outcomes for shard in self._members[group]):
            self._started.add(group)
            thread = threading.Thread(
                target=self._merge_group, args=(group,), name=f"runcomposer-merge-{group}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def record_finished(self) -> None:
        """Record what the merges that have finished produced — called in the
        dispatching thread, between deliveries."""
        with self._lock:
            results, self._results = self._results, []
        for group, result, count in results:
            self._record(group, result, count)

    def finish(self, *, merge: bool) -> None:
        for thread in self._threads:
            thread.join()
        if merge:
            for group in self._groups:
                if group not in self._started and not self._ctx.cancelled:
                    self._started.add(group)
                    self._merge_group(group)
            if not self._ctx.cancelled:
                self._merge_run()
        self.record_finished()

    # -- the merges ------------------------------------------------------------------

    def _outputs(self, shards: list[str]) -> list[tuple[str, frozenset[str]]]:
        """The merged ``output.xml`` of every delivered shard among
        ``shards``, with the roots it executed."""
        found = []
        for shard in shards:
            outcome = self._outcomes.get(shard)
            if outcome is None or outcome.problem is not None:
                continue
            output = Path(outcome.payload.out_dir) / "output.xml"
            if output.is_file():
                found.append(
                    (str(output), frozenset(execution.root for execution in outcome.payload.executions))
                )
        return found

    def _merge_group(self, group: str) -> None:
        outputs = self._outputs(self._members[group])
        index = self._groups.index(group) + 1
        result = self._merge(
            [path for path, _roots in outputs],
            self._base / f"group-{index}",
            merge=len({roots for _path, roots in outputs}) <= 1,
            titles=group,
        )
        with self._lock:
            self._results.append((group, result, len(outputs)))

    def _merge_run(self) -> None:
        if self._groups:
            sources = []
            names = []
            for index, group in enumerate(self._groups, start=1):
                merged = self._base / f"group-{index}" / "output.xml"
                own = self._outputs(self._members[group])
                if len(own) == 1:
                    sources.append(own[0][0])
                    names.append(group)
                elif merged.is_file():
                    sources.append(str(merged))
                    names.append(group)
            # The same tests on every partition are put side by side, named
            # after their partitions; distributed ones are merged into one tree.
            side_by_side = self._plan.mode == "each" and len(sources) > 1
            result = self._merge(
                sources, self._base / "run", merge=not side_by_side,
                names=names if side_by_side else None,
            )
            count = len(sources)
        else:
            outputs = self._outputs(list(self._plan.shards))
            result = self._merge(
                [path for path, _roots in outputs], self._base / "run",
                merge=len({roots for _path, roots in outputs}) <= 1,
            )
            count = len(outputs)
        with self._lock:
            self._results.append((None, result, count))

    def _merge(
        self,
        outputs: list[str],
        out_dir: Path,
        *,
        merge: bool,
        titles: str | None = None,
        names: list[str] | None = None,
    ) -> MergeResult:
        if not outputs:
            return MergeResult(error="no shard delivered a result to merge")
        if len(outputs) == 1:
            # Nothing to merge: the one result is the merged one.
            return MergeResult(output=outputs[0])
        options: dict[str, Any] = {}
        if titles:
            options.update(logtitle=f"{titles} Log", reporttitle=f"{titles} Report")
        if names:
            options["prerebotmodifier"] = [NameTopSuites(names)]
        runner = self._runner
        return spawn_merge(
            MergePayload(
                outputs=tuple(outputs), out_dir=str(out_dir), log=runner.log,
                report=runner.report, merge=merge, rpa=runner.rpa, rebot_options=options,
            ),
            cancelled=lambda: self._ctx.cancelled,
        )

    def _record(self, group: str | None, result: MergeResult, count: int) -> None:
        ctx = self._ctx
        what = f"group {group}" if group is not None else "the run"
        if not result.ok or result.output is None:
            ctx.log(f"merged result of {what} not produced: {result.error}", level="warning")
            return
        label = f"group {group}" if group is not None else "run"
        base = Path(result.output).parent
        for name, media_type in (
            ("log.html", "text/html"),
            ("report.html", "text/html"),
            ("output.xml", "application/xml"),
        ):
            file = base / name
            if file.is_file():
                ctx.add_artifact(f"{name} ({label})", file, media_type=media_type, group=group)
        ctx.log(f"merged result of {what} recorded ({count} result(s) merged)")


def _spec_format(spec: Mapping[str, Any]) -> str | None:
    """The format the run expects — when it names one other than the
    reference format, which a Robot execution never produces."""
    expect = (spec.get("results") or {}).get("expect") or []
    named = expect[0].get("format") if expect and isinstance(expect[0], Mapping) else None
    return named if named and named != "runcomposer-verdicts" else None


def _stop_hook(process: subprocess.Popen[str]) -> None:
    """Kill an overrunning hook and its whole process group."""
    if os.name == "nt":
        process.kill()
    else:
        import signal

        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pass
