"""`robot-pool` Runner (DESIGN.md §6.2a): Robot Framework executed on this
machine, in worker processes.

What it does with one dispatch:

1. **Drift check** (§3.3): the live catalog's snapshot against the spec's.
   Refuse by default; with ``allow_drift`` execute the intersection and report
   the rest as ``SKIP`` / ``drift``.
2. **Readiness hooks**: ``pre_run_hooks`` shell commands; a failing one
   refuses the dispatch.
3. **Plan**: the selection × ``partitions``, each partition cut into
   duration-balanced chunks (history from the store; round-robin on a cold
   start, and the plan says so). One (partition, chunk) is one shard.
4. **Hand-off**: ``ctx.declare`` with every shard's item ids.
5. **Execute**: one spawned worker process per shard, at most ``max_workers``
   at a time. A worker runs its items in executions of at most
   ``max_tests_per_chunk`` tests, re-executes failures ``rerun_failed`` times,
   and merges everything into one ``output.xml`` / ``log.html`` /
   ``report.html`` for people to read.
6. **Deliver** each shard as it finishes, parsed with the configured
   ResultParser, every attempt labelled.

Tests are selected by **exact id**, not by name pattern: an id containing
``[`` or ``*`` is that one test. Several suite roots are supported — items
are attributed to the root they came from.

Live per-item status comes from a listener streaming through the context's
live channel (``live_status``, on by default); without it the run shows
planned → final.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import multiprocessing
import os
import subprocess
import sys
import time
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from runcomposer.sdk import (
    ConfigError,
    DispatchContext,
    Health,
    Parameter,
    RunnerBase,
    RunnerInfo,
    Verdict,
    plan_chunks,
    split_max,
)

__all__ = ["RobotPoolRunner"]

_RESULT_FILE = ".shard-result.json"
#: Robot's return code for an execution that was forcefully stopped.
_STOPPED_BY_SIGNAL = 253
#: Written into a shard's directory before its worker is told to stop. Robot
#: turns the first termination signal into a graceful end of the *current*
#: execution; the marker is how the worker knows not to start the next one.
_STOP_FILE = ".stop-requested"


class RobotPoolRunner(RunnerBase):
    PATH_OPTIONS = ("suite_root", "suite_roots", "pythonpath")

    def __init__(
        self,
        suite_root: str | None = None,
        suite_roots: list[str] | None = None,
        max_workers: int = 2,
        partitions: list[str] | None = None,
        variables: Mapping[str, str] | None = None,
        partition_variable: str = "PARTITION",
        partition_variables: Mapping[str, Mapping[str, str]] | None = None,
        executed_tag: str | None = None,
        live_status: bool = True,
        allow_drift: bool = False,
        listener: str | list[str] | None = None,
        pre_run_hooks: list[str] | None = None,
        hook_timeout_s: float = 600.0,
        worker_setup: list[str] | None = None,
        pythonpath: list[str] | None = None,
        log: bool = True,
        report: bool = True,
        max_tests_per_chunk: int | None = None,
        rerun_failed: int = 0,
        shard_timeout_s: float | None = None,
        history_selector: Mapping[str, Any] | None = None,
        history_depth: int = 5,
        format: str | None = None,
    ):
        roots = ([suite_root] if suite_root else []) + list(suite_roots or [])
        self.suite_roots = [str(root) for root in roots]
        if int(max_workers) < 1:
            raise ConfigError("robot-pool: max_workers must be at least 1")
        self.max_workers = int(max_workers)
        self.partitions = [str(p) for p in (partitions or [])]
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
        self.worker_setup = list(worker_setup or [])
        for ref in self.worker_setup:
            module, sep, attr = str(ref).partition(":")
            if not sep or not module or not attr:
                raise ConfigError(
                    f"robot-pool: worker_setup entries are import paths 'package.module:callable', "
                    f"got {ref!r}"
                )
        self.pythonpath = [str(entry) for entry in (pythonpath or [])]
        self.log = bool(log)
        self.report = bool(report)
        self.max_tests_per_chunk = int(max_tests_per_chunk) if max_tests_per_chunk else None
        if int(rerun_failed) < 0:
            raise ConfigError("robot-pool: rerun_failed must not be negative")
        self.rerun_failed = int(rerun_failed)
        self.shard_timeout_s = float(shard_timeout_s) if shard_timeout_s else None
        self.history_labels = dict((history_selector or {}).get("labels") or {})
        self.history_depth = int(history_depth)
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
                    help="Run the selection once per chosen partition.",
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
        return RunnerInfo(
            id="robot-pool",
            title="Robot Framework (local pool)",
            description="Runs Robot Framework on this machine in worker processes.",
            capabilities=("live_status", "partitions", "cancel"),
            parameters=tuple(parameters),
            result_format="robot-output-xml",
        )

    def health(self) -> Health:
        try:
            import robot
        except ImportError:
            return Health(
                status="down",
                detail="Robot Framework is not installed (pip install 'runcomposer[robot]')",
            )
        missing = [root for root in self.suite_roots if not Path(root).exists()]
        if not self.suite_roots:
            return Health(status="down", detail="no suite_root configured")
        if missing:
            return Health(status="degraded", detail=f"suite root(s) not found: {missing}")
        return Health(
            status="ok",
            detail=f"Robot Framework {getattr(robot, '__version__', '')}".strip(),
            data={"max_workers": self.max_workers, "suite_roots": self.suite_roots},
        )

    # -- dispatch ---------------------------------------------------------------------

    def dispatch(self, ctx: DispatchContext) -> None:
        if not self.suite_roots:
            ctx.refuse(
                "robot-pool needs a suite_root (or suite_roots) in its runner configuration"
            )
        try:
            import robot  # noqa: F401
        except ImportError:
            ctx.refuse("Robot Framework is not installed (pip install 'runcomposer[robot]')")

        item_ids, drift_skips = self._check_drift(ctx)
        self._run_hooks(ctx)
        partitions = list(ctx.options.get("partitions") or self.partitions) or ["default"]
        chunk_count = max(1, min(len(item_ids) or 1, self.max_workers // len(partitions) or 1))
        durations = ctx.history.durations(labels=self.history_labels or None, last_n=self.history_depth)
        plan = plan_chunks(item_ids, chunk_count, durations)
        roots_of = self._attribute(ctx, item_ids)

        result_format = self.format or _spec_format(ctx.spec) or "robot-output-xml"
        channel = ctx.live_channel() if self.live_status else None
        shards: dict[str, list[str]] = {}
        payloads: list[dict[str, Any]] = []
        for partition in partitions:
            for index, chunk in enumerate(plan.chunks, start=1):
                shard = (
                    f"{partition}-{index}" if len(partitions) > 1 or len(plan.chunks) > 1
                    else partition
                )
                shards[shard] = list(chunk)
                executions: list[dict[str, Any]] = []
                for root in self.suite_roots:
                    in_root = [item for item in chunk if roots_of[item] == root]
                    executions.extend(
                        {"root": root, "item_ids": piece}
                        for piece in split_max(in_root, self.max_tests_per_chunk)
                    )
                variables = {**self.variables, **self.partition_variables.get(partition, {})}
                variables[self.partition_variable] = partition
                payloads.append(
                    {
                        "shard": shard,
                        "partition": partition,
                        "executions": executions,
                        "variables": variables,
                        "out_dir": str(ctx.workdir / shard),
                        "listeners": self.listeners,
                        "channel": channel,
                        "executed_tag": (
                            self.executed_tag.format(partition=partition, shard=shard)
                            if self.executed_tag
                            else None
                        ),
                        "worker_setup": self.worker_setup,
                        "pythonpath": self.pythonpath,
                        "log": self.log,
                        "report": self.report,
                        "rerun_failed": self.rerun_failed,
                        "run_id": ctx.run_id,
                        "dispatch_id": ctx.dispatch_id,
                    }
                )
        if drift_skips:
            shards["drift"] = [verdict.item_id for verdict in drift_skips]

        lines = [
            f"robot-pool plan: {len(item_ids)} item(s) × {len(partitions)} partition(s) "
            f"→ {len(plan.chunks)} chunk(s)/partition, max_workers={self.max_workers}"
        ]
        lines.extend("  " + line for line in plan.describe().splitlines())
        if self.max_tests_per_chunk:
            lines.append(f"  at most {self.max_tests_per_chunk} test(s) per execution, merged per shard")
        if self.rerun_failed:
            lines.append(f"  failures are re-executed up to {self.rerun_failed} time(s)")
        if drift_skips:
            lines.append(f"  drift: {len(drift_skips)} planned item(s) no longer exist — reported as SKIP")

        # The hand-off: everything this runner can refuse has been refused.
        ctx.declare(shards, plan="\n".join(lines))
        if drift_skips:
            ctx.deliver("drift", verdicts=drift_skips, format=result_format)
        failures = self._execute(ctx, payloads, result_format)
        ctx.raise_if_cancelled()
        if failures:
            raise RuntimeError(
                f"{len(failures)} of {len(payloads)} shard(s) produced no results: "
                + "; ".join(failures)
            )

    # -- §3.3 drift --------------------------------------------------------------------

    def _check_drift(self, ctx: DispatchContext) -> tuple[list[str], list[Verdict]]:
        item_ids = list(ctx.item_ids)
        composed = (ctx.spec.get("source") or {}).get("snapshot")
        live = ctx.source.snapshot()
        if live == composed:
            return item_ids, []
        if not ctx.options.get("allow_drift", self.allow_drift):
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
            try:
                stdout, stderr = process.communicate(timeout=self.hook_timeout_s)
            except subprocess.TimeoutExpired:
                _stop_hook(process)
                ctx.refuse(f"pre_run_hook did not finish within {self.hook_timeout_s:g}s: {hook!r}")
            if process.returncode != 0:
                ctx.refuse(
                    f"pre_run_hook failed (rc {process.returncode}): {hook!r} — "
                    f"{(stderr or stdout).strip()[:300]}"
                )

    def _attribute(self, ctx: DispatchContext, item_ids: Sequence[str]) -> dict[str, str]:
        """Which suite root each item is executed from. The catalog says so
        when it was built by the Robot source (``meta.root`` / ``meta.source``);
        otherwise the root suite's name — the first segment of the id — does."""
        if len(self.suite_roots) == 1:
            return dict.fromkeys(item_ids, self.suite_roots[0])
        from robot.running import TestSuite

        resolved = {root: Path(root).resolve() for root in self.suite_roots}
        by_name = {TestSuite.name_from_source(root): root for root in self.suite_roots}
        meta = {item.id: item.meta for item in ctx.source.items()}
        roots_of: dict[str, str] = {}
        for item_id in item_ids:
            found = None
            source = str((meta.get(item_id) or {}).get("source") or "")
            if source:
                path = Path(source).resolve()
                found = next(
                    (root for root, base in resolved.items() if path == base or base in path.parents),
                    None,
                )
            if found is None:
                found = next(
                    (root for name, root in by_name.items()
                     if item_id == name or item_id.startswith(name + ".")),
                    None,
                )
            if found is None:
                ctx.refuse(
                    f"item {item_id!r} belongs to none of the configured suite roots "
                    f"{self.suite_roots}"
                )
            roots_of[item_id] = found
        return roots_of

    # -- execution ----------------------------------------------------------------------

    def _execute(
        self, ctx: DispatchContext, payloads: list[dict[str, Any]], result_format: str
    ) -> list[str]:
        """Run the shards on spawned worker processes, at most ``max_workers``
        at once; deliver each as it finishes. Returns the failures."""
        mp = multiprocessing.get_context("spawn")
        pending = list(payloads)
        running: list[tuple[Any, dict[str, Any], float]] = []
        failures: list[str] = []
        try:
            while pending or running:
                if ctx.cancelled:
                    break
                while pending and len(running) < self.max_workers:
                    payload = pending.pop(0)
                    Path(payload["out_dir"]).mkdir(parents=True, exist_ok=True)
                    process = mp.Process(
                        target=run_shard, args=(payload,), name=f"robot-pool-{payload['shard']}"
                    )
                    process.start()
                    running.append((process, payload, time.monotonic()))
                still_running = []
                for process, payload, started in running:
                    process.join(timeout=0.05)
                    overrun = (
                        self.shard_timeout_s is not None
                        and time.monotonic() - started > self.shard_timeout_s
                    )
                    if process.is_alive() and not overrun:
                        still_running.append((process, payload, started))
                        continue
                    if process.is_alive():
                        _stop(process, payload["out_dir"])
                        failures.append(
                            f"{payload['shard']}: exceeded shard_timeout_s={self.shard_timeout_s:g}"
                        )
                        continue
                    problem = self._deliver(ctx, payload, process.exitcode, result_format)
                    if problem:
                        failures.append(problem)
                running = still_running
        finally:
            for process, payload, _started in running:
                _stop(process, payload["out_dir"])
        return failures

    def _deliver(
        self, ctx: DispatchContext, payload: dict[str, Any], exitcode: int | None,
        result_format: str,
    ) -> str | None:
        shard = payload["shard"]
        out_dir = Path(payload["out_dir"])
        try:
            result = json.loads((out_dir / _RESULT_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return f"{shard}: the worker process ended (exit code {exitcode}) without a result"
        if result.get("error"):
            ctx.log(f"shard {shard}: {result['error']}", level="error")
        outputs = [entry for entry in result.get("outputs", []) if Path(entry["path"]).is_file()]
        if not outputs:
            return f"{shard}: {result.get('error') or 'Robot produced no output.xml'}"
        verdicts: list[Verdict] = []
        for entry in outputs:
            selected, found = int(entry.get("selected", 0)), int(entry.get("tests", -1))
            if 0 <= found < selected:
                # Selected ids that are not tests under this root: nothing ran
                # for them, so nothing is reported — they end up MISSING.
                ctx.log(
                    f"shard {shard}: {selected - found} of {selected} selected test(s) do not "
                    f"exist under {entry.get('root')} — not executed",
                    level="warning",
                )
            if found == 0:
                continue  # an output without tests is an empty result, not a broken one
            for verdict in ctx.parse(entry["path"], format=result_format):
                verdicts.append(dataclasses.replace(verdict, attempt=int(entry["attempt"])))
        ctx.deliver(shard, verdicts=verdicts, format=result_format)
        for name, media_type in (
            ("log.html", "text/html"),
            ("report.html", "text/html"),
            ("output.xml", "application/xml"),
        ):
            file = out_dir / name
            if file.is_file():
                ctx.add_artifact(f"{name} ({shard})", file, media_type=media_type, shard=shard)
        return None


def _spec_format(spec: Mapping[str, Any]) -> str | None:
    """The format the run expects — when it names one other than the
    reference format, which a Robot execution never produces."""
    expect = (spec.get("results") or {}).get("expect") or []
    named = expect[0].get("format") if expect and isinstance(expect[0], Mapping) else None
    return named if named and named != "runcomposer-verdicts" else None


def _stop(process: Any, out_dir: str) -> None:
    if process.is_alive():
        try:
            Path(out_dir, _STOP_FILE).touch()
        except OSError:
            pass
        process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


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


# -- the worker process -----------------------------------------------------------------


def run_shard(payload: dict[str, Any]) -> None:
    """Worker entry (spawned): execute one shard with real Robot and write
    what happened to a result file the parent reads."""
    out_dir = Path(payload["out_dir"])
    result: dict[str, Any] = {"shard": payload["shard"], "outputs": [], "error": None}
    try:
        _run_shard(payload, out_dir, result)
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"
        raise
    finally:
        (out_dir / _RESULT_FILE).write_text(json.dumps(result), encoding="utf-8")


def _run_shard(payload: dict[str, Any], out_dir: Path, result: dict[str, Any]) -> None:
    for entry in payload["pythonpath"]:
        if entry not in sys.path:
            sys.path.insert(0, entry)
    import robot
    from robot.api import ExecutionResult, ResultVisitor, SuiteVisitor

    robot_api: Any = robot

    from runcomposer.plugins.robot_listener import LiveStatusListener

    variables = dict(payload["variables"])
    info = {
        key: payload[key] for key in ("shard", "partition", "run_id", "dispatch_id", "out_dir")
    }
    for ref in payload["worker_setup"]:
        module_name, _, attr = ref.partition(":")
        extra = getattr(importlib.import_module(module_name), attr)(dict(info, variables=dict(variables)))
        if isinstance(extra, Mapping):  # a setup may contribute Robot variables
            variables.update({str(k): str(v) for k, v in extra.items()})

    class ExactSelection(SuiteVisitor):  # type: ignore[misc, unused-ignore]
        """Keep exactly the tests whose longname is in the set — no pattern
        matching, so an id is never read as a glob."""

        def __init__(self, ids: Sequence[str], tag: str | None):
            self.ids = set(ids)
            self.tag = tag
            self.kept = 0

        def start_suite(self, suite: Any) -> None:
            suite.tests = [test for test in suite.tests if test.longname in self.ids]
            self.kept += len(suite.tests)
            if self.tag:
                for test in suite.tests:
                    test.tags.add(self.tag)

        def end_suite(self, suite: Any) -> None:
            suite.suites = [child for child in suite.suites if child.test_count > 0]

        def visit_test(self, test: Any) -> None:
            return None

    class Failed(ResultVisitor):  # type: ignore[misc, unused-ignore]
        def __init__(self) -> None:
            self.ids: list[str] = []

        def visit_test(self, test: Any) -> None:
            if test.status == "FAIL":
                self.ids.append(test.longname)

    def execute(
        root: str, ids: Sequence[str], target: Path, *, final: bool, attempt: int = 1
    ) -> dict[str, Any] | None:
        """One Robot execution; what it produced, or None without an output."""
        target.mkdir(parents=True, exist_ok=True)
        listeners: list[Any] = list(payload["listeners"])
        if payload["channel"] is not None:
            listeners.append(LiveStatusListener(payload["channel"], payload["shard"]))
        selection = ExactSelection(ids, payload["executed_tag"])
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            rc = robot_api.run(
                root,
                prerunmodifier=[selection],
                variable=[f"{key}:{value}" for key, value in variables.items()],
                listener=listeners,
                pythonpath=payload["pythonpath"],
                outputdir=str(target),
                output="output.xml",
                log="log.html" if final and payload["log"] else "NONE",
                report="report.html" if final and payload["report"] else "NONE",
                runemptysuite=True,
                consolecolors="off",
                stdout=devnull,
                stderr=devnull,
            )
        if rc == _STOPPED_BY_SIGNAL or (out_dir / _STOP_FILE).exists():
            # This shard was asked to stop: Robot ended the execution in
            # flight; do not go on to the next one.
            raise KeyboardInterrupt("the shard was asked to stop")
        output = target / "output.xml"
        if not output.is_file():
            return None
        return {
            "path": str(output),
            "attempt": attempt,
            "root": root,
            "selected": len(ids),
            "tests": selection.kept,
        }

    executions = payload["executions"]
    single = len(executions) == 1 and not payload["rerun_failed"]
    outputs: list[dict[str, Any]] = []
    for index, execution in enumerate(executions, start=1):
        target = out_dir if single else out_dir / f"execution-{index}"
        produced = execute(execution["root"], execution["item_ids"], target, final=single)
        if produced is not None:
            outputs.append(produced)
        result["outputs"] = list(outputs)

    # Re-execute what failed, up to rerun_failed more times.
    roots = sorted({execution["root"] for execution in executions})
    last_by_root = {root: [o for o in outputs if o["root"] == root] for root in roots}
    for attempt in range(2, payload["rerun_failed"] + 2):
        again = False
        for root, previous in list(last_by_root.items()):
            collector = Failed()
            for entry in previous:
                if entry["tests"]:
                    ExecutionResult(entry["path"]).visit(collector)
            if not collector.ids:
                last_by_root[root] = []
                continue
            again = True
            name = f"attempt-{attempt}" + (f"-root{roots.index(root) + 1}" if len(roots) > 1 else "")
            rerun = execute(root, collector.ids, out_dir / name, final=False, attempt=attempt)
            last_by_root[root] = [rerun] if rerun is not None else []
            if rerun is not None:
                outputs.append(rerun)
                result["outputs"] = list(outputs)
        if not again:
            break

    if not single and outputs:
        # One readable result per shard: later executions merged over earlier.
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            robot_api.rebot(
                *[entry["path"] for entry in outputs],
                merge=len({entry["root"] for entry in outputs}) == 1,
                outputdir=str(out_dir),
                output="output.xml",
                log="log.html" if payload["log"] else "NONE",
                report="report.html" if payload["report"] else "NONE",
                processemptysuite=True,
                consolecolors="off",
                stdout=devnull,
                stderr=devnull,
            )
