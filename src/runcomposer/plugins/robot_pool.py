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
     partition is cut into duration-balanced chunks that run in parallel.
   - ``partition_mode: split`` — every test runs *once*; the chosen
     partitions are parallel lanes the selection is distributed over,
     balanced by duration.

   A chunk is one shard. With ``max_tests_per_chunk`` a chunk is executed in
   pieces of at most that many tests, one after another; with
   ``chunk_shards`` each piece is a shard of its own, delivered when it is
   done — instead of the pieces being merged into one result per chunk.
   Shards are grouped by partition. History comes from the store
   (round-robin on a cold start, and the plan says so).
3. **Readiness**: ``pre_run_hooks`` (shell commands) and ``pre_dispatch``
   (``"pkg.mod:callable"`` references called with the context and the plan:
   which partitions, which shards, the per-run parameters). Either may
   refuse the dispatch; nothing has been handed off yet.
4. **Hand-off**: ``ctx.declare`` with every shard's item ids and group.
5. **Execute**: one spawned worker process per shard, at most
   ``max_workers`` at a time; failures are re-executed ``rerun_failed`` times.
6. **Deliver** each shard as it finishes, parsed with the configured
   ResultParser, every attempt labelled.

Tests are selected by **exact id**, not by name pattern: an id containing
``[`` or ``*`` is that one test. Several suite roots are supported — items
are attributed to the root they came from. A root that mixes tests and tasks
needs ``rpa: false`` (or ``true``): left to itself Robot refuses a selection
that keeps both.

Per-run parameters: ``partitions`` and ``allow_drift``, plus whatever
``run_parameters`` declares; ``parameter_variables`` turns those into Robot
variables.

Live per-item status comes from a listener streaming through the context's
live channel (``live_status``, on by default): ``RUNNING`` when a test
starts, its status when it ends. Workers log their progress to the run's
event stream through the same channel.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runcomposer.sdk import (
    ConfigError,
    DispatchContext,
    Health,
    Parameter,
    PlanContext,
    PlanPreview,
    PluginError,
    RunnerBase,
    RunnerInfo,
    Verdict,
    load_import_path,
    parameters_from_config,
    plan_chunks,
    split_max,
)
from runcomposer.sdk.robot import (
    Execution,
    ShardOutcome,
    ShardPayload,
    attribute_roots,
    deliver_shard,
    robot_version,
    supervise,
)

__all__ = ["RobotPoolRunner"]

#: How often a running pre_run_hook is checked for a cancellation.
_HOOK_POLL_S = 0.2
_PARTITION_MODES = ("each", "split")
_OWN_PARAMETERS = ("partitions", "allow_drift")
_NOT_INSTALLED = "Robot Framework is not installed (pip install 'runcomposer[robot]')"


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
    lanes: list[_Lane]
    shards: dict[str, list[str]]
    groups: dict[str, str]
    estimates_ms: dict[str, float]
    drift_skips: list[Verdict]
    text: str
    notes: list[str]


def _callables(refs: list[str] | None, option: str) -> list[tuple[str, Callable[..., Any]]]:
    loaded = []
    for ref in refs or []:
        try:
            target = load_import_path(str(ref))
        except PluginError as exc:
            raise ConfigError(f"robot-pool: {option}: {exc}") from None
        if not callable(target):
            raise ConfigError(f"robot-pool: {option}: {ref!r} is not callable")
        loaded.append((str(ref), target))
    return loaded


class RobotPoolRunner(RunnerBase):
    PATH_OPTIONS = ("suite_root", "suite_roots", "pythonpath")

    def __init__(
        self,
        suite_root: str | None = None,
        suite_roots: list[str] | None = None,
        max_workers: int = 2,
        partitions: list[str] | None = None,
        partition_mode: str = "each",
        chunk_shards: bool = False,
        variables: Mapping[str, str] | None = None,
        partition_variable: str = "PARTITION",
        partition_variables: Mapping[str, Mapping[str, str]] | None = None,
        run_parameters: list[Mapping[str, Any]] | None = None,
        parameter_variables: Mapping[str, str] | None = None,
        executed_tag: str | None = None,
        live_status: bool = True,
        allow_drift: bool = False,
        listener: str | list[str] | None = None,
        pre_run_hooks: list[str] | None = None,
        hook_timeout_s: float = 600.0,
        pre_dispatch: list[str] | None = None,
        worker_setup: list[str] | None = None,
        pythonpath: list[str] | None = None,
        log: bool = True,
        report: bool = True,
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
        clash = sorted({p.name for p in self.run_parameters} & set(_OWN_PARAMETERS))
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
        self.pre_dispatch = [str(ref) for ref in (pre_dispatch or [])]
        self.worker_setup = [str(ref) for ref in (worker_setup or [])]
        for option, refs in (("pre_dispatch", self.pre_dispatch), ("worker_setup", self.worker_setup)):
            for ref in refs:
                module, sep, attr = ref.partition(":")
                if not sep or not module or not attr:
                    raise ConfigError(
                        f"robot-pool: {option} entries are import paths "
                        f"'package.module:callable', got {ref!r}"
                    )
        self.pythonpath = [str(entry) for entry in (pythonpath or [])]
        self.log = bool(log)
        self.report = bool(report)
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
                        "Run the selection once per chosen partition."
                        if self.partition_mode == "each"
                        else "Distribute the selection over the chosen partitions "
                        "(every test runs once)."
                    ),
                    choices=tuple(self.partitions),
                    default=list(self.partitions),
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
        version = robot_version()
        if version is None:
            return Health(status="down", detail=_NOT_INSTALLED)
        missing = [root for root in self.suite_roots if not Path(root).exists()]
        if not self.suite_roots:
            return Health(status="down", detail="no suite_root configured")
        data = {
            "robot_version": version,
            "max_workers": self.max_workers,
            "suite_roots": self.suite_roots,
            "partitions": self.partitions,
            "partition_mode": self.partition_mode,
        }
        if missing:
            return Health(
                status="degraded", detail=f"suite root(s) not found: {missing}", data=data
            )
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
            data={
                "partitions": plan.partitions,
                "partition_mode": self.partition_mode,
                "max_workers": self.max_workers,
            },
        )

    def _plan(self, ctx: PlanContext) -> _Plan:
        """Everything that is decided before anything runs — shared by the
        preview and the dispatch, so that the one shows what the other does."""
        if not self.suite_roots:
            ctx.refuse(
                "robot-pool needs a suite_root (or suite_roots) in its runner configuration"
            )
        if robot_version() is None:
            ctx.refuse(_NOT_INSTALLED)
        item_ids, drift_skips = self._check_drift(ctx)
        partitions = list(ctx.parameters.get("partitions") or self.partitions) or ["default"]
        durations = ctx.history.durations(
            labels=self.history_labels or None,
            last_n=self.history_depth,
            statuses=self.history_statuses,
        )
        try:
            roots_of = attribute_roots(
                self.suite_roots,
                item_ids,
                ctx.source.items() if len(self.suite_roots) > 1 else (),
            )
        except LookupError as exc:
            ctx.refuse(str(exc))

        lanes: list[_Lane] = []
        if self.partition_mode == "split":
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

        lines = [headline]
        lines.extend("  " + line for line in chunks.describe().splitlines())
        notes: list[str] = []
        if not chunks.balanced and item_ids:
            notes.append(
                "cold start: no duration history for this selection — the distribution is "
                "round-robin until executions (or imported durations) say how long tests take"
            )
        if self.max_tests_per_chunk:
            lines.append(
                f"  at most {self.max_tests_per_chunk} test(s) per execution, "
                + ("each execution a shard of its own" if self.chunk_shards else "merged per shard")
            )
        if self.partitions:
            for partition in partitions:
                own = [shard for shard, label in groups.items() if label == partition]
                lines.append(
                    f"  {partition}: {len(own)} shard(s), "
                    f"{sum(len(shards[shard]) for shard in own)} item(s)"
                )
        if self.rerun_failed:
            lines.append(f"  failures are re-executed up to {self.rerun_failed} time(s)")
        if drift_skips:
            lines.append(
                f"  drift: {len(drift_skips)} planned item(s) no longer exist — reported as SKIP"
            )
        return _Plan(
            item_ids=item_ids,
            partitions=partitions,
            lanes=lanes,
            shards=shards,
            groups=groups,
            estimates_ms=estimates,
            drift_skips=drift_skips,
            text="\n".join(lines),
            notes=notes,
        )

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

        def finished(outcome: ShardOutcome) -> None:
            problem = deliver_shard(
                ctx, outcome, format=result_format, group=plan.groups.get(outcome.payload.shard)
            )
            if problem:
                failures.append(problem)

        supervise(
            payloads,
            max_workers=self.max_workers,
            on_done=finished,
            cancelled=lambda: ctx.cancelled,
            timeout_s=self.shard_timeout_s,
        )
        ctx.raise_if_cancelled()
        if failures:
            raise RuntimeError(
                f"{len(failures)} of {len(payloads)} shard(s) produced no results: "
                + "; ".join(failures)
            )

    def _parameter_variables(self, ctx: DispatchContext) -> dict[str, str]:
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

    def _pre_dispatch(
        self, ctx: DispatchContext, plan: _Plan, variables: Mapping[str, str]
    ) -> dict[str, str]:
        """``pre_dispatch``: preparation in this process, before the hand-off,
        that knows what is about to run. Each callable gets the context — so
        it can ``ctx.refuse``, ``ctx.log``, keep files in ``ctx.private_dir``
        — and the plan; a mapping it returns is added to the Robot variables
        of every shard."""
        added: dict[str, str] = {}
        for ref, step in _callables(self.pre_dispatch, "pre_dispatch"):
            ctx.raise_if_cancelled()
            info = {
                "partitions": list(plan.partitions),
                "parameters": dict(ctx.parameters),
                "shards": {shard: list(items) for shard, items in plan.shards.items()},
                "groups": dict(plan.groups),
                "plan": plan.text,
                "item_ids": list(plan.item_ids),
                "variables": {**variables, **added},
                "workdir": str(ctx.workdir),
            }
            result = step(ctx, info)
            if isinstance(result, Mapping):
                added.update({str(key): str(value) for key, value in result.items()})
            ctx.log(f"pre_dispatch {ref}: done")
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
