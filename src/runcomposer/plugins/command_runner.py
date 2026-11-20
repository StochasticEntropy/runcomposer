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
delivers ``{out_dir}``.

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
import time
from collections.abc import Mapping, Sequence
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
    parameters_from_config,
    plan_chunks,
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

        pending = list(shards.items())
        running: list[tuple[subprocess.Popen[bytes], str, Path, Any, float]] = []
        failures: list[str] = []
        try:
            while pending or running:
                if ctx.cancelled:
                    break
                while pending and len(running) < self.max_parallel:
                    shard, ids = pending.pop(0)
                    running.append(self._start(ctx, shard, ids, spec_file))
                still = []
                for process, shard, out_dir, log, started in running:
                    code = process.poll()
                    overrun = self.timeout_s is not None and time.monotonic() - started > self.timeout_s
                    if code is None and not overrun:
                        still.append((process, shard, out_dir, log, started))
                        continue
                    if code is None:
                        _stop(process)
                        log.close()
                        self._record_artifacts(ctx, shard, out_dir)  # command.log says why
                        failures.append(f"shard {shard}: exceeded timeout_s={self.timeout_s:g}")
                        continue
                    log.close()
                    problem = self._finish(ctx, shard, out_dir, code)
                    if problem:
                        failures.append(problem)
                running = still
                if running:
                    time.sleep(0.05)
        finally:
            for process, _shard, _out, log, _started in running:
                _stop(process)
                log.close()
        ctx.raise_if_cancelled()
        if failures:
            raise RuntimeError("; ".join(failures))

    def _start(
        self, ctx: DispatchContext, shard: str, ids: Sequence[str], spec_file: Path
    ) -> tuple[subprocess.Popen[bytes], str, Path, Any, float]:
        out_dir = ctx.workdir / f"shard-{shard}"
        out_dir.mkdir(parents=True, exist_ok=True)
        ids_file = out_dir / "item_ids.txt"
        ids_file.write_text("\n".join(ids) + "\n", encoding="utf-8")
        values = {
            "ids_file": str(ids_file),
            "out_dir": str(out_dir),
            "spec_file": str(spec_file),
            "run_id": ctx.run_id,
            "dispatch_id": ctx.dispatch_id,
            "shard": shard,
        }
        env = {**os.environ, **self.env}
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
        # Closed by the dispatch loop when the process ends (it outlives this call).
        log = open(out_dir / "command.log", "wb")  # noqa: SIM115
        try:
            process = subprocess.Popen(
                command,
                shell=isinstance(command, str),
                cwd=self.cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=os.name != "nt",  # its own group: children stop with it
            )
        except OSError as exc:
            log.close()
            raise RuntimeError(f"shard {shard}: the command could not be started: {exc}") from None
        ctx.log(f"shard {shard}: command started (pid {process.pid})")
        return process, shard, out_dir, log, time.monotonic()

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


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Stop the command and whatever it started (its process group)."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.terminate()
    else:
        import signal

        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
