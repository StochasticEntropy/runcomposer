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

Where the workers stand: ``worker_cwd`` (a path, relative to the config
file) is their working directory, ``worker_env`` is set in their
environment, and a ``pre_dispatch`` step may return ``{variables: {…}, env:
{…}, worker_data: {…}}`` — computed once, merged into every shard of the
dispatch (``worker_data`` reaches ``worker_setup`` as ``info["data"]``).
After every execution, in the worker: ``clean_output`` (Robot's own
``remove_keywords`` / ``flatten_keywords`` selectors) rewrites that
execution's ``output.xml`` before anything reads it, then the
``post_execution`` steps run with its path. ``health_checks`` answer within
``health_check_timeout_s`` or are failed rows.

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
import pickle
import re
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
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
    ProcessJob,
    RunnerBase,
    RunnerInfo,
    Verdict,
    configured_calls,
    parameters_from_config,
    plan_chunks,
    split_max,
)
from runcomposer.sdk.robot import (
    VARIABLE_NAME_PATTERN,
    Execution,
    GroupMerger,
    ShardOutcome,
    ShardPayload,
    attribute_roots,
    deliver_shard,
    robot_version,
    supervise,
    variable_name_problem,
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


def _text_mapping(values: Mapping[str, Any], where: str) -> dict[str, str]:
    """An environment mapping: names to text (a number or a boolean is
    written as text); a nested value is refused, naming it."""
    found: dict[str, str] = {}
    for key, value in values.items():
        if isinstance(value, (Mapping, list, tuple, set)) or value is None:
            raise ConfigError(f"{where}.{key}: an environment variable's value is text, got {value!r}")
        found[str(key)] = str(value)
    return found


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


def _timed_health_rows(
    calls: tuple[ConfiguredCall, ...], info: Mapping[str, Any], timeout_s: float
) -> list[dict[str, Any]]:
    """Every ``health_checks`` callable's rows, the checks run side by side,
    each given ``timeout_s`` from the same start: one that has not answered
    by then is a failed row saying so, and the health answer does not wait
    for it. (Its thread is left to end on its own — a thread cannot be
    stopped — and is a daemon, so it never holds the process.)"""
    if not calls:
        return []
    answers: dict[int, list[dict[str, Any]]] = {}
    threads = []
    for index, call in enumerate(calls):

        def check(index: int = index, call: ConfiguredCall = call) -> None:
            answers[index] = _health_rows(call, info)

        thread = threading.Thread(target=check, name=f"health-check-{call.ref}", daemon=True)
        thread.start()
        threads.append(thread)
    deadline = time.monotonic() + timeout_s
    rows: list[dict[str, Any]] = []
    for index, (call, thread) in enumerate(zip(calls, threads, strict=True)):
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if index in answers:
            rows.extend(answers[index])
        else:
            rows.append(
                {
                    "check": call.ref,
                    "ok": False,
                    "detail": f"did not answer within {timeout_s:g}s (health_check_timeout_s)",
                }
            )
    return rows


class RobotPoolRunner(RunnerBase):
    PATH_OPTIONS = ("suite_root", "suite_roots", "pythonpath", "worker_cwd")

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
        health_check_timeout_s: float = 5.0,
        post_execution: list[Any] | None = None,
        clean_output: Mapping[str, Any] | None = None,
        worker_cwd: str | None = None,
        worker_env: Mapping[str, Any] | None = None,
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
        targets = {str(k) for k, v in (parameter_variables or {}).items() if str(v) == "*"}
        # A mapping that becomes Robot variables takes plain variable names
        # only, unless its declaration constrains its keys itself.
        self.run_parameters = tuple(
            replace(parameter, key_pattern=VARIABLE_NAME_PATTERN)
            if parameter.name in targets and parameter.kind == "mapping" and not parameter.key_pattern
            else parameter
            for parameter in self.run_parameters
        )
        for name in self.variables:
            problem = variable_name_problem(name)
            if problem is not None:
                raise ConfigError(f"robot-pool: variables: {problem}")
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
            problem = variable_name_problem(target) if target != "*" else None
            if problem is not None:
                raise ConfigError(f"robot-pool: parameter_variables.{name}: {problem}")
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
        if isinstance(health_check_timeout_s, bool) or float(health_check_timeout_s) <= 0:
            raise ConfigError("robot-pool: health_check_timeout_s must be a positive number of seconds")
        self.health_check_timeout_s = float(health_check_timeout_s)
        self.post_execution = _configured(post_execution, "post_execution")
        try:  # Robot's own selectors, checked where the payload is made
            self.clean_output = dict(
                ShardPayload(shard="-", out_dir="-", executions=(), clean_output=clean_output or {})
                .clean_output
            )
        except ValueError as exc:
            raise ConfigError(f"robot-pool: {exc}") from None
        self.worker_cwd = str(worker_cwd) if worker_cwd else None
        if worker_env is not None and not isinstance(worker_env, Mapping):
            raise ConfigError("robot-pool: worker_env must be a mapping of variable names to text")
        self.worker_env = _text_mapping(worker_env or {}, "robot-pool: worker_env")
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
        """Robot, the suite roots and ``worker_cwd``, as ``data.checks``
        rows, followed by what every ``health_checks`` callable says — each
        within ``health_check_timeout_s``, side by side. A failed row of the
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
        no_cwd = bool(self.worker_cwd) and not Path(str(self.worker_cwd)).is_dir()
        if self.worker_cwd:
            checks.append(
                {
                    "check": f"worker_cwd {self.worker_cwd}",
                    "ok": not no_cwd,
                    "detail": "not a directory" if no_cwd else "found",
                }
            )
        if self._suite_name():
            data["suite_name"] = self._suite_name()
        own = _timed_health_rows(self.health_checks, info, self.health_check_timeout_s)
        checks.extend(own)
        failed = [row["check"] for row in own if not row["ok"]]
        failing = f"health check(s) failed: {', '.join(failed)}" if failed else ""
        if version is None:
            return Health(status="down", detail=_NOT_INSTALLED, data=data)
        if not self.suite_roots:
            return Health(status="down", detail="no suite_root configured", data=data)
        if missing or failed or no_cwd:
            detail = "; ".join(
                part
                for part in (
                    missing and f"suite root(s) not found: {missing}",
                    no_cwd and f"worker_cwd is not a directory: {self.worker_cwd}",
                    failing,
                )
                if part
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

    def _suite_name(self) -> str | None:
        """The top-level suite name ``robot_options`` gives every execution
        (``name``, the one spelling ``robot.run`` accepts for ``--name``)."""
        value = self.robot_options.get("name")
        return str(value) if value not in (None, "") else None

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
            roots_of = attribute_roots(
                self.suite_roots, item_ids, ctx.source.items(), name=self._suite_name()
            )
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
                f"duration history for {chunks.known} of {chunks.total} item(s); "
                + (
                    "the other one is weighed with the mean of the known durations, "
                    f"{_seconds(chunks.fallback_ms)} — the estimates per partition assume it "
                    "takes that long"
                    if chunks.total - chunks.known == 1
                    else f"the other {chunks.total - chunks.known} are weighed with the mean of "
                    f"the known durations, {_seconds(chunks.fallback_ms)} each — the estimates "
                    "per partition assume they take that long"
                )
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
        added = self._pre_dispatch(ctx, plan, variables)
        variables.update(added.variables)
        if self.worker_cwd and not Path(self.worker_cwd).is_dir():
            ctx.refuse(f"worker_cwd {self.worker_cwd!r} is not a directory")
        try:
            pickle.dumps(added.worker_data)
        except Exception as exc:
            ctx.refuse(f"pre_dispatch: worker_data cannot be sent to a worker process: {exc}")

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
                        cwd=self.worker_cwd,
                        env={**self.worker_env, **added.env},
                        worker_data=dict(added.worker_data),
                        clean_output=self.clean_output,
                        post_execution=tuple(self.post_execution),
                    )
                )

        # The hand-off: everything this runner can refuse has been refused.
        ctx.declare(plan.shards, plan=plan.text, groups=plan.groups)
        if plan.drift_skips:
            ctx.deliver("drift", verdicts=plan.drift_skips, format=result_format)
        failures: list[str] = []
        # With merged results, the shard that finishes last is delivered
        # only after they are recorded: its delivery completes the run.
        merger = (
            GroupMerger(
                ctx, payloads, plan.groups, side_by_side=plan.mode == "each",
                log=self.log, report=self.report, rpa=self.rpa,
            )
            if self.merge_groups
            else None
        )

        def deliver(outcome: ShardOutcome) -> None:
            problem = deliver_shard(
                ctx, outcome, format=result_format, group=plan.groups.get(outcome.payload.shard)
            )
            if problem:
                failures.append(problem)

        def finished(_payload: ShardPayload, outcome: ShardOutcome) -> None:
            for ready in merger.shard_ended(outcome) if merger is not None else [outcome]:
                deliver(ready)

        try:
            supervise(
                payloads,
                max_workers=self.max_workers,
                on_done=finished,
                cancelled=lambda: ctx.cancelled,
                timeout_s=self.shard_timeout_s,
            )
        finally:
            for outcome in merger.finish() if merger is not None else []:
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
        dispatch alike. Every key must be a plain Robot variable name in the
        first place (``variable_name_problem``): ``BASE_URL:https`` would set
        ``${BASE_URL}``."""
        for name, target in self.parameter_variables.items():
            if target != "*" or name not in ctx.parameters:
                continue
            for key in dict(ctx.parameters[name]):
                problem = variable_name_problem(key)
                if problem is not None:
                    raise InvalidParameter(
                        name, f"the Robot variable {problem}", problem="key-pattern", key=str(key)
                    )
                protected = self._protected.get(variable_key(str(key)))
                if protected is not None:
                    raise InvalidParameter(
                        f"parameter {name!r}: the Robot variable {str(key)!r} cannot be set per "
                        f"run — this runner protects {protected!r} (protected_variables)",
                        parameter=name, key=str(key),
                    )

    def _pre_dispatch(
        self, ctx: DispatchContext, plan: _Plan, variables: Mapping[str, str]
    ) -> _Additions:
        """``pre_dispatch``: preparation in this process, once the plan stands
        and nothing is handed off yet. Each callable gets the context — so
        it can ``ctx.refuse``, ``ctx.log``, keep files in ``ctx.private_dir``
        — and the plan. What it returns goes into every shard of the
        dispatch (:func:`_additions`): a mapping of Robot variables, or
        ``{variables: {…}, env: {…}, worker_data: {…}}``; ``None`` (or
        anything that is not a mapping) adds nothing."""
        added = _Additions()
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
                "variables": {**variables, **added.variables},
                "workdir": str(ctx.workdir),
            }
            added.take(_call(call, ctx, info), call.ref)
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
            # In a session of its own (on Windows: a job of its own), so that
            # a hook that overruns is stopped together with whatever it
            # started — not just its shell. A hook that ends on its own
            # leaves what it started running, on every platform.
            options: dict[str, Any] = {
                "shell": True,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                # A hook's output only words a refusal: a byte the locale's
                # encoding cannot read must not turn that into a traceback.
                "errors": "replace",
            }
            job: ProcessJob | None = None
            if os.name == "nt":
                process, job = ProcessJob.start(
                    hook, kill_on_close=False, say=lambda message: ctx.log(message, "warning"), **options
                )
            else:
                process = subprocess.Popen(hook, start_new_session=True, **options)
            try:
                stdout, stderr = self._wait_for_hook(ctx, hook, process, job)
            finally:
                if job is not None:
                    job.close()
            if process.returncode != 0:
                ctx.refuse(
                    f"pre_run_hook failed (rc {process.returncode}): {hook!r} — "
                    f"{(stderr or stdout).strip()[:300]}"
                )

    def _wait_for_hook(
        self, ctx: DispatchContext, hook: str, process: subprocess.Popen[str], job: ProcessJob | None
    ) -> tuple[str, str]:
        """The hook's output once it ended — or, cancelled or over its
        timeout, the hook stopped with what it started."""
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
                    _stop_hook(process, job)
                    ctx.raise_if_cancelled()
                if time.monotonic() >= deadline:
                    _stop_hook(process, job)
                    ctx.refuse(
                        f"pre_run_hook did not finish within {self.hook_timeout_s:g}s: {hook!r}"
                    )
        return stdout, stderr


#: The keys of a ``pre_dispatch`` answer that addresses more than Robot
#: variables — each a mapping.
_ADDITION_KEYS = frozenset(("variables", "env", "worker_data"))


@dataclass
class _Additions:
    """What the ``pre_dispatch`` steps of one dispatch give its shards."""

    variables: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    worker_data: dict[str, Any] = field(default_factory=dict)

    def take(self, answer: Any, ref: str) -> None:
        """One step's answer, merged over the earlier ones. A mapping whose
        keys are only ``variables``, ``env`` and ``worker_data``, each a
        mapping, is read as those three: Robot variables, the workers'
        environment, and data each ``worker_setup`` receives as
        ``info["data"]``. Any other mapping is Robot variables (as it has
        always been); ``None``, or anything that is not a mapping, adds
        nothing."""
        if not isinstance(answer, Mapping):
            return
        structured = bool(answer) and set(answer) <= _ADDITION_KEYS and all(
            isinstance(value, Mapping) for value in answer.values()
        )
        if not structured:
            self.variables.update({str(key): str(value) for key, value in answer.items()})
            return
        self.variables.update(
            {str(key): str(value) for key, value in (answer.get("variables") or {}).items()}
        )
        self.env.update(_text_mapping(answer.get("env") or {}, f"pre_dispatch {ref}: env"))
        self.worker_data.update({str(key): value for key, value in (answer.get("worker_data") or {}).items()})


def _spec_format(spec: Mapping[str, Any]) -> str | None:
    """The format the run expects — when it names one other than the
    reference format, which a Robot execution never produces."""
    expect = (spec.get("results") or {}).get("expect") or []
    named = expect[0].get("format") if expect and isinstance(expect[0], Mapping) else None
    return named if named and named != "runcomposer-verdicts" else None


def _stop_hook(process: subprocess.Popen[str], job: ProcessJob | None = None) -> None:
    """Kill an overrunning hook and its whole process group — on Windows
    its job, or the shell alone where Windows refused the job."""
    if os.name == "nt":
        if job is None or not job.terminate(1):
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
