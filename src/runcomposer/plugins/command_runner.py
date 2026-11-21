"""`command` Runner (DESIGN.md §6.2d): the afternoon-sized connector.

Runs a configured command per shard on this machine and delivers what it
wrote through the configured ResultParser. Any framework that can be started
from a command line and can write a result file is connected with a few lines
of configuration::

    runners:
      pytest:
        plugin: command
        command: "python -m pytest --junitxml {out_dir}/junit.xml @{ids_file}"
        format: junit-xml
        ok_exit_codes: [0, 1]        # 1 = tests failed, which is a result

Per shard the runner writes the shard's item ids, one per line, to
``{ids_file}``, runs the command, and — if it exited with an accepted code —
delivers ``{out_dir}``. Each shard's command is started by a worker process
under the SDK's ``supervise_workers``: at most ``max_parallel`` at once, a
shard over ``timeout_s`` stopped, and a cancellation stopping the command
together with whatever it started.

Placeholders, replaced literally (nothing else in braces is touched, so
``awk '{print $1}'`` is safe): ``{ids_file}`` ``{out_dir}`` ``{spec_file}``
``{run_id}`` ``{dispatch_id}`` ``{shard}``. In a command given as one string
(run by the shell) the values are shell-quoted; in an argv list they are
inserted as they are. The same values are in the environment as
``RUNCOMPOSER_IDS_FILE`` etc., and each per-run parameter as
``RUNCOMPOSER_PARAM_<NAME>``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from runcomposer.sdk import (
    ConfigError,
    DispatchContext,
    Health,
    PlanContext,
    PlanPreview,
    RunnerBase,
    RunnerInfo,
    Verdict,
    WorkerOutcome,
    WorkerPayload,
    parameters_from_config,
    plan_chunks,
    supervise_workers,
)

__all__ = ["CommandRunner"]

PLACEHOLDERS = ("ids_file", "out_dir", "spec_file", "run_id", "dispatch_id", "shard")
_MAX_ARTIFACTS = 25


def substitute(template: str, values: Mapping[str, str], *, quote: bool) -> str:
    """Replace the known ``{placeholders}`` and nothing else."""
    rendered = template
    for name in PLACEHOLDERS:
        value = values[name]
        if quote:
            value = subprocess.list2cmdline([value]) if os.name == "nt" else shlex.quote(value)
        rendered = rendered.replace("{" + name + "}", value)
    return rendered


class CommandRunner(RunnerBase):
    PATH_OPTIONS = ("cwd",)

    def __init__(
        self,
        command: str | list[str],
        format: str | None = None,
        shards: int = 1,
        max_parallel: int = 1,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        ok_exit_codes: list[int] | None = None,
        allow_drift: bool = False,
        parameters: list[Mapping[str, Any]] | None = None,
        history_selector: Mapping[str, Any] | None = None,
        history_depth: int = 5,
    ):
        if isinstance(command, (list, tuple)):
            if not command or not all(isinstance(part, str) for part in command):
                raise ConfigError("command runner: an argv list must be non-empty strings")
            self.command: str | list[str] = list(command)
        elif isinstance(command, str) and command.strip():
            self.command = command
        else:
            raise ConfigError("command runner: 'command' must be a string or an argv list")
        if int(shards) < 1 or int(max_parallel) < 1:
            raise ConfigError("command runner: shards and max_parallel must be at least 1")
        self.format = format
        self.shards = int(shards)
        self.max_parallel = int(max_parallel)
        self.cwd = cwd
        self.env = {str(k): str(v) for k, v in (env or {}).items()}
        self.timeout_s = float(timeout_s) if timeout_s else None
        self.ok_exit_codes = {int(code) for code in (ok_exit_codes if ok_exit_codes is not None else [0])}
        self.allow_drift = bool(allow_drift)
        self.parameters = parameters_from_config(parameters, "command runner: parameters")
        self.history_labels = dict((history_selector or {}).get("labels") or {})
        self.history_depth = int(history_depth)

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="command",
            title="Command",
            description="Runs a configured command per shard on this machine.",
            capabilities=("cancel", "plan") + (("shards",) if self.shards > 1 else ()),
            parameters=self.parameters,
            result_format=self.format,
        )

    def health(self) -> Health:
        if self.cwd and not Path(self.cwd).is_dir():
            return Health(status="down", detail=f"working directory not found: {self.cwd}")
        return Health(status="ok", detail="command configured")

    # -- dispatch ---------------------------------------------------------------------

    def plan(self, ctx: PlanContext) -> PlanPreview:
        shards, skips, text, estimates = self._plan(ctx)
        declared = dict(shards)
        if skips:
            declared["drift"] = [verdict.item_id for verdict in skips]
        return PlanPreview(text=text, shards=declared, estimates_ms=estimates)

    def _plan(
        self, ctx: PlanContext
    ) -> tuple[dict[str, list[str]], list[Verdict], str, dict[str, float]]:
        """``(shards, drift skips, plan text, estimates)`` — what the preview
        shows and the dispatch does."""
        if self.cwd and not Path(self.cwd).is_dir():
            ctx.refuse(f"working directory not found: {self.cwd}")
        item_ids = list(ctx.item_ids)
        skips: list[Verdict] = []
        composed = (ctx.spec.get("source") or {}).get("snapshot")
        live = ctx.source.snapshot()
        if live != composed:
            if not self.allow_drift:
                ctx.refuse(
                    "live corpus snapshot differs from source.snapshot in the spec — refusing "
                    f"(§3.3 drift check; composed {composed}, live {live}). Configure "
                    "allow_drift to execute the intersection."
                )
            known = {item.id for item in ctx.source.items()}
            skips = [Verdict(i, "SKIP", message="drift") for i in item_ids if i not in known]
            item_ids = [i for i in item_ids if i in known]

        durations = ctx.history.durations(labels=self.history_labels or None, last_n=self.history_depth)
        plan = plan_chunks(item_ids, self.shards, durations)
        shards = {str(index): list(chunk) for index, chunk in enumerate(plan.chunks, start=1)}
        text = (
            f"command runner: {len(item_ids)} item(s) in {len(shards)} shard(s), "
            f"max_parallel={self.max_parallel}\n" + plan.describe()
        )
        estimates = {
            str(index): estimate
            for index, estimate in enumerate(plan.estimates_ms, start=1)
            if estimate
        }
        return shards, skips, text, estimates

    def dispatch(self, ctx: DispatchContext) -> None:
        shards, skips, text, _estimates = self._plan(ctx)
        declared = dict(shards)
        if skips:
            declared["drift"] = [verdict.item_id for verdict in skips]
        spec_file = ctx.workdir / "spec.json"
        spec_file.write_bytes(ctx.export_spec())
        ctx.declare(declared, plan=text)
        if skips:
            ctx.deliver("drift", verdicts=skips, format=self.format)

        channel = ctx.live_channel()
        payloads = [
            self._payload(ctx, shard, ids, spec_file, channel) for shard, ids in shards.items()
        ]
        failures: list[str] = []

        def finished(payload: WorkerPayload, outcome: WorkerOutcome) -> None:
            shard, out_dir = payload.shard, Path(payload.out_dir)
            result = outcome.result if isinstance(outcome.result, Mapping) else {}
            if "not_started" in result:
                # As before: a command that cannot start fails the dispatch now.
                raise RuntimeError(
                    f"shard {shard}: the command could not be started: {result['not_started']}"
                )
            if outcome.timed_out:
                self._record_artifacts(ctx, shard, out_dir)  # command.log says why
                failures.append(f"shard {shard}: exceeded timeout_s={self.timeout_s:g}")
            elif "exit_code" not in result:
                self._record_artifacts(ctx, shard, out_dir)
                failures.append(f"shard {shard}: {outcome.problem}")
            else:
                problem = self._finish(ctx, shard, out_dir, int(result["exit_code"]))
                if problem:
                    failures.append(problem)

        supervise_workers(
            payloads,
            _run_command,
            max_workers=self.max_parallel,
            on_done=finished,
            cancelled=lambda: ctx.cancelled,
            timeout_s=self.timeout_s,
        )
        ctx.raise_if_cancelled()
        if failures:
            raise RuntimeError("; ".join(failures))

    def _payload(
        self, ctx: DispatchContext, shard: str, ids: Sequence[str], spec_file: Path, channel: Any
    ) -> WorkerPayload:
        """What the worker of one shard is told: the command with its
        placeholders replaced, and the environment it runs in."""
        out_dir = ctx.workdir / f"shard-{shard}"
        ids_file = out_dir / "item_ids.txt"
        values = {
            "ids_file": str(ids_file),
            "out_dir": str(out_dir),
            "spec_file": str(spec_file),
            "run_id": ctx.run_id,
            "dispatch_id": ctx.dispatch_id,
            "shard": shard,
        }
        env = dict(self.env)
        for name, value in values.items():
            env[f"RUNCOMPOSER_{name.upper()}"] = value
        for name, value in ctx.parameters.items():
            if isinstance(value, (list, tuple)):
                rendered = ",".join(map(str, value))
            elif isinstance(value, Mapping):  # a `mapping` parameter: name=value lines
                rendered = "\n".join(f"{key}={entry}" for key, entry in value.items())
            else:
                rendered = str(value)
            env[f"RUNCOMPOSER_PARAM_{name.upper()}"] = rendered
        command: str | list[str]
        if isinstance(self.command, str):
            command = substitute(self.command, values, quote=True)
        else:
            command = [substitute(part, values, quote=False) for part in self.command]
        return WorkerPayload(
            shard=shard,
            out_dir=str(out_dir),
            item_ids=tuple(ids),
            cwd=self.cwd,
            env=env,
            data={"command": command, "channel": channel},
        )

    @staticmethod
    def _record_artifacts(ctx: DispatchContext, shard: str, out_dir: Path) -> None:
        count = 0
        for file in sorted(out_dir.rglob("*")):
            if file.is_file() and file.name != "item_ids.txt" and count < _MAX_ARTIFACTS:
                name = f"{file.relative_to(out_dir).as_posix()} (shard {shard})"
                ctx.add_artifact(name, file, shard=shard)
                count += 1

    def _finish(self, ctx: DispatchContext, shard: str, out_dir: Path, code: int) -> str | None:
        self._record_artifacts(ctx, shard, out_dir)
        if code not in self.ok_exit_codes:
            return (
                f"shard {shard}: the command exited {code}, which is not an accepted exit code "
                f"({sorted(self.ok_exit_codes)}) — see command.log"
            )
        try:
            ctx.deliver(shard, bundle=out_dir, format=self.format)
        except Exception as exc:
            return f"shard {shard}: {exc}"
        return None


def _run_command(payload: WorkerPayload, stop: Callable[[], bool]) -> dict[str, Any]:
    """The worker of one shard (a spawned process, already in the shard's
    working directory and environment): write the ids file, run the command
    with its output in ``command.log``, and hand back its exit code.

    The command runs in the worker's process group, so the supervisor's stop
    protocol reaches it and whatever it started; once asked to stop, the
    worker also terminates the command itself (on Windows there is no group
    to signal)."""
    out_dir = Path(payload.out_dir)
    (out_dir / "item_ids.txt").write_text("\n".join(payload.item_ids) + "\n", encoding="utf-8")
    command = payload.data["command"]
    channel = payload.data.get("channel")
    if stop():  # stopped while the worker was starting: nothing to start
        return {"stopped": True}
    with open(out_dir / "command.log", "wb") as log:
        try:
            process = subprocess.Popen(
                command,
                shell=isinstance(command, str),
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            return {"not_started": str(exc)}
        if channel is not None:
            channel.log(f"shard {payload.shard}: command started (pid {process.pid})", "info")
        asked = False
        try:
            while True:
                try:
                    code = process.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    if stop() and not asked:
                        # The group's signal has reached the command already —
                        # unless it started just after; and on Windows there is
                        # no group signal at all.
                        asked = True
                        process.terminate()
        finally:
            if process.poll() is None:  # the worker itself is being terminated
                process.kill()
                process.wait()
    return {"exit_code": code}
