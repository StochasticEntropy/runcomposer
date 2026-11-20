"""runcomposer.sdk.robot — the Robot Framework toolkit.

The building blocks the reference ``robot-pool`` runner is made of, for a
connector that executes Robot Framework in a way of its own: compose these
instead of restating them. ``robot-pool`` itself imports nothing else, which
is the evidence that they suffice.

- :class:`ExactSelection` — a pre-run modifier keeping exactly the tests
  whose id is in a set. No pattern matching: an id containing ``[`` or ``*``
  is that one test.
- :class:`LiveStatusListener` — streams ``RUNNING`` when a test starts and
  its status when it ends, through any ``LiveChannel``;
  :class:`RemoteLiveStatusListener` is the same for an agent that executes
  elsewhere and pushes to the API.
- :class:`ShardPayload` / :func:`run_shard` — what one worker process is
  told, and the worker's entry point (a module-level function, so it can be
  the target of a spawned process).
- :func:`supervise` — runs payloads on worker processes: a bound on
  parallelism, *lanes* (payloads that must not overlap, executed in order),
  a per-shard timeout, and the stop protocol (ask the worker to stop, let
  Robot end the execution in flight, terminate, kill).
- :func:`deliver_shard` / :func:`collect_verdicts` — what a finished worker
  produced, delivered through the dispatch context.
- :func:`merge_outputs` — several ``output.xml`` of one shard merged into
  one readable result, without the "added from merged output" notes a merge
  of disjoint executions would stamp on every test.
- :func:`attribute_roots` — which suite root each item is executed from.

Robot Framework itself is imported only when something is executed (inside
the functions that need it): importing this module works without it, and
``robot_version()`` says whether it is there.

The id space is the Robot plugin family's: an item id is a test's longname.
"""

from __future__ import annotations

import html
import io
import json
import multiprocessing
import os
import re
import sys
import time
import traceback
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from runcomposer.sdk import (
    DispatchContext,
    Item,
    LiveChannel,
    RemoteLiveChannel,
    Verdict,
    load_import_path,
)

__all__ = [
    "RESULT_FILE",
    "STOP_FILE",
    "ExactSelection",
    "Execution",
    "LiveStatusListener",
    "MergeNoiseRemover",
    "RemoteLiveStatusListener",
    "ShardOutcome",
    "ShardOutput",
    "ShardPayload",
    "ShardResult",
    "attribute_roots",
    "collect_verdicts",
    "deliver_shard",
    "merge_outputs",
    "read_shard_result",
    "robot_version",
    "run_shard",
    "stop_worker",
    "supervise",
]

#: What a worker writes into its output directory when it is done (or dies
#: trying): the hand-back the supervising process reads.
RESULT_FILE = ".shard-result.json"
#: Written into a shard's directory before its worker is told to stop. Robot
#: turns the first termination signal into a graceful end of the *current*
#: execution; this marker is how the worker knows not to start the next one.
STOP_FILE = ".stop-requested"
#: Robot's return code for an execution that was forcefully stopped.
_STOPPED_BY_SIGNAL = 253
_STATUS_MAP = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP", "NOT RUN": "SKIP"}


def robot_version() -> str | None:
    """The installed Robot Framework's version, ``None`` when it is not
    installed (``pip install 'runcomposer[robot]'``)."""
    try:
        from robot.version import get_version
    except ImportError:
        return None
    return str(get_version())


# -- selection ---------------------------------------------------------------------------


class ExactSelection:
    """Pre-run modifier: keep exactly the tests whose longname is in ``ids``
    — no pattern matching, so an id is never read as a glob — optionally tag
    every kept test, and drop the suites left empty. ``kept`` counts what was
    found, which tells a caller that a selected id is not a test under the
    root it was executed from.

    Pass an instance as ``prerunmodifier`` (``robot.run(…,
    prerunmodifier=[ExactSelection(ids)])``).
    """

    def __init__(self, ids: Iterable[str], tag: str | None = None):
        self.ids = frozenset(ids)
        self.tag = tag
        self.kept = 0

    def visit_suite(self, suite: Any) -> None:
        suite.tests = [test for test in suite.tests if test.longname in self.ids]
        self.kept += len(suite.tests)
        if self.tag:
            for test in suite.tests:
                test.tags.add(self.tag)
        for child in suite.suites:
            child.visit(self)
        suite.suites = [child for child in suite.suites if child.test_count > 0]


def attribute_roots(
    suite_roots: Sequence[str], item_ids: Sequence[str], items: Iterable[Item] = ()
) -> dict[str, str]:
    """Which suite root each item is executed from. The catalog says so when
    it was built by the Robot source (``Item.meta["source"]`` lies under the
    root); otherwise the root suite's name — the first segment of the id —
    does. Raises ``LookupError`` naming an item that belongs to no root."""
    roots = [str(root) for root in suite_roots]
    if len(roots) == 1:
        return dict.fromkeys(item_ids, roots[0])
    from robot.running import TestSuite

    resolved = {root: Path(root).resolve() for root in roots}
    by_name = {TestSuite.name_from_source(root): root for root in roots}
    meta = {item.id: item.meta for item in items}
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
                (
                    root
                    for name, root in by_name.items()
                    if item_id == name or item_id.startswith(name + ".")
                ),
                None,
            )
        if found is None:
            raise LookupError(
                f"item {item_id!r} belongs to none of the configured suite roots {roots}"
            )
        roots_of[item_id] = found
    return roots_of


# -- live status ---------------------------------------------------------------------------


class LiveStatusListener:
    """Robot listener (API v3): ``RUNNING`` when a test starts, its status
    when it ends — written to a ``LiveChannel``, so it works in a worker
    process and with any RunStore. The parse of the finished output is the
    authoritative result; live status is replaced by it when the shard
    delivers. Streaming is best-effort and never breaks the execution.

    ``stopped`` (optional) says whether the execution was asked to stop. A
    test that is cut short by a stop ends as a failure in Robot's eyes; that
    is not something the test did, so its end is not reported — what stays
    is that it was ``RUNNING`` when the stop came."""

    ROBOT_LISTENER_API_VERSION = 3

    def __init__(
        self, channel: LiveChannel, shard: str, stopped: Callable[[], bool] | None = None
    ):
        self._channel = channel
        self._shard = shard
        self._stopped = stopped

    def start_test(self, data: Any, result: Any) -> None:
        try:
            self._channel.live(self._shard, result.longname, "RUNNING")
        except Exception:
            pass

    def end_test(self, data: Any, result: Any) -> None:
        try:
            if self._stopped is not None and self._stopped():
                return
            self._channel.live(
                self._shard,
                result.longname,
                _STATUS_MAP.get(result.status, "ERROR"),
                duration_ms=int(result.elapsedtime),
                message=result.message or "",
            )
        except Exception:
            pass


class RemoteLiveStatusListener(LiveStatusListener):
    """:class:`LiveStatusListener` for an execution runcomposer did not
    start itself — a CI job, a remote agent: it pushes to the API with the
    dispatch's ingest token instead of writing to a local channel.

    Use it by name; what it needs comes from the arguments or, left out,
    from the environment::

        robot --listener runcomposer.sdk.robot.RemoteLiveStatusListener tests/

    ``RUNCOMPOSER_CALLBACK_URL`` (the run's results URL, or any URL of the
    run's API resource), ``RUNCOMPOSER_INGEST_TOKEN``, ``RUNCOMPOSER_SHARD``
    (default ``1``) and ``RUNCOMPOSER_DISPATCH_ID`` (optional). Without a
    callback URL it does nothing.
    """

    def __init__(
        self,
        callback_url: str | None = None,
        token: str | None = None,
        shard: str | None = None,
        dispatch_id: str | None = None,
    ):
        url = callback_url or os.environ.get("RUNCOMPOSER_CALLBACK_URL") or ""
        channel: Any = _NoChannel()
        if url:
            channel = RemoteLiveChannel(
                url,
                token or os.environ.get("RUNCOMPOSER_INGEST_TOKEN"),
                dispatch_id=dispatch_id or os.environ.get("RUNCOMPOSER_DISPATCH_ID") or None,
            )
        super().__init__(channel, shard or os.environ.get("RUNCOMPOSER_SHARD") or "1")


class _NoChannel:
    def live(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def log(self, *_args: Any, **_kwargs: Any) -> None:
        return None


# -- what a worker is told, and what it hands back ---------------------------------------


@dataclass(frozen=True)
class Execution:
    """One Robot execution inside a shard: these tests, from this root."""

    root: str
    item_ids: tuple[str, ...]


@dataclass(frozen=True)
class ShardPayload:
    """Everything one worker process needs to execute one shard. Picklable:
    it travels to a spawned process.

    - ``shard`` — the name the results are delivered under; ``out_dir`` — the
      shard's own directory (outputs, the result hand-back, the stop marker).
    - ``executions`` — the Robot executions, run one after another; with
      several (or with reruns) their outputs are merged into one
      ``output.xml`` / ``log.html`` / ``report.html`` in ``out_dir``.
    - ``lane`` — payloads with the same lane never run at the same time and
      run in the order given (:func:`supervise`): the chunks one partition
      executes one after another.
    - ``partition`` — a label handed to ``worker_setup`` and usable as a
      shard group; it has no effect on execution.
    - ``variables`` — Robot variables (``name → value``).
    - ``channel`` — where the worker writes log lines and, with
      ``live_status``, per-test status (``ctx.live_channel()``).
    - ``listeners`` — additional Robot listeners (import paths).
    - ``executed_tag`` — a tag added to every executed test.
    - ``worker_setup`` — ``"pkg.mod:callable"`` references called in the
      worker before the first execution with ``{shard, partition, run_id,
      dispatch_id, out_dir, variables}``; a mapping they return is added to
      the variables.
    - ``rerun_failed`` — re-execute failed tests up to this many more times;
      every attempt is reported, the last one counts.
    - ``rpa`` — the execution mode: ``None`` lets Robot decide (it refuses a
      selection that mixes tests and tasks), ``False`` executes everything
      as tests, ``True`` as tasks.
    - ``robot_options`` — further keyword options for ``robot.run`` (for
      example ``{"loglevel": "DEBUG", "variablefile": ["env.py"]}``); the
      options this toolkit sets itself win.
    """

    shard: str
    out_dir: str
    executions: tuple[Execution, ...]
    variables: Mapping[str, str] = field(default_factory=dict)
    partition: str = ""
    lane: str | None = None
    channel: Any = None
    live_status: bool = True
    listeners: tuple[str, ...] = ()
    executed_tag: str | None = None
    worker_setup: tuple[str, ...] = ()
    pythonpath: tuple[str, ...] = ()
    log: bool = True
    report: bool = True
    rerun_failed: int = 0
    rpa: bool | None = None
    robot_options: Mapping[str, Any] = field(default_factory=dict)
    run_id: str = ""
    dispatch_id: str = ""

    @property
    def item_ids(self) -> tuple[str, ...]:
        """Every item this shard is to report."""
        return tuple(item for execution in self.executions for item in execution.item_ids)


@dataclass(frozen=True)
class ShardOutput:
    """One ``output.xml`` a worker produced: which attempt it is, how many
    tests were selected for it and how many of them existed under ``root``."""

    path: str
    attempt: int = 1
    root: str = ""
    selected: int = 0
    tests: int = 0


@dataclass(frozen=True)
class ShardResult:
    """What a worker handed back."""

    shard: str
    outputs: tuple[ShardOutput, ...] = ()
    error: str | None = None
    stopped: bool = False


@dataclass(frozen=True)
class ShardOutcome:
    """How one payload ended under :func:`supervise`."""

    payload: ShardPayload
    exitcode: int | None
    result: ShardResult | None
    timed_out: bool = False
    #: Seconds the timeout was, when it timed out.
    timeout_s: float | None = None

    @property
    def outputs(self) -> tuple[ShardOutput, ...]:
        """The outputs that really exist on disk."""
        if self.result is None:
            return ()
        return tuple(output for output in self.result.outputs if Path(output.path).is_file())

    @property
    def problem(self) -> str | None:
        """Why this shard has nothing to deliver — ``None`` when it has."""
        shard = self.payload.shard
        if self.timed_out:
            return f"{shard}: exceeded the shard timeout of {self.timeout_s:g}s"
        if self.result is None:
            return f"{shard}: the worker process ended (exit code {self.exitcode}) without a result"
        if not self.outputs:
            return f"{shard}: {self.result.error or 'Robot produced no output.xml'}"
        return None


def read_shard_result(out_dir: str | Path) -> ShardResult | None:
    """The hand-back a worker left in its output directory, ``None`` when
    there is none (the worker was killed before it could write one)."""
    try:
        data = json.loads((Path(out_dir) / RESULT_FILE).read_text(encoding="utf-8"))
        return ShardResult(
            shard=str(data["shard"]),
            outputs=tuple(ShardOutput(**entry) for entry in data.get("outputs") or ()),
            error=data.get("error"),
            stopped=bool(data.get("stopped")),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


# -- the worker process -------------------------------------------------------------------


class _Stopped(Exception):
    """The shard was asked to stop."""


def run_shard(payload: ShardPayload) -> None:
    """Worker entry: execute one shard with real Robot Framework and write
    what happened to :data:`RESULT_FILE` in ``payload.out_dir``. Meant as the
    target of a spawned process (:func:`supervise` uses it that way); it can
    also be called in-process."""
    out_dir = Path(payload.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[ShardOutput] = []
    problems: list[str] = []
    error: str | None = None
    stopped = False
    try:
        _execute_shard(payload, out_dir, outputs, problems)
        error = "; ".join(problems) or None
    except _Stopped:
        stopped = True
        error = "the shard was asked to stop"
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"
        raise
    finally:
        document = {
            "shard": payload.shard,
            "outputs": [asdict(output) for output in outputs],
            "error": error,
            "stopped": stopped,
        }
        (out_dir / RESULT_FILE).write_text(json.dumps(document), encoding="utf-8")


def _execute_shard(
    payload: ShardPayload, out_dir: Path, outputs: list[ShardOutput], problems: list[str]
) -> None:
    for entry in payload.pythonpath:
        if entry not in sys.path:
            sys.path.insert(0, entry)
    import robot
    from robot.api import ExecutionResult

    robot_api: Any = robot
    channel = payload.channel

    def say(message: str, level: str = "info") -> None:
        if channel is not None:
            try:
                channel.log(f"shard {payload.shard}: {message}", level)
            except Exception:
                pass

    variables = {str(key): str(value) for key, value in payload.variables.items()}
    info = {
        "shard": payload.shard,
        "partition": payload.partition,
        "run_id": payload.run_id,
        "dispatch_id": payload.dispatch_id,
        "out_dir": payload.out_dir,
    }
    for ref in payload.worker_setup:
        extra = load_import_path(ref)(dict(info, variables=dict(variables)))
        if isinstance(extra, Mapping):  # a setup may contribute Robot variables
            variables.update({str(key): str(value) for key, value in extra.items()})

    def execute(execution: Execution, target: Path, *, final: bool, attempt: int = 1) -> ShardOutput | None:
        """One Robot execution; what it produced, or None without an output."""
        if (out_dir / STOP_FILE).exists():
            raise _Stopped
        target.mkdir(parents=True, exist_ok=True)
        listeners: list[Any] = list(payload.listeners)
        if channel is not None and payload.live_status:
            listeners.append(
                LiveStatusListener(channel, payload.shard, (out_dir / STOP_FILE).exists)
            )
        selection = ExactSelection(execution.item_ids, payload.executed_tag)
        options: dict[str, Any] = dict(payload.robot_options)
        options.update(
            prerunmodifier=[*_as_list(options.get("prerunmodifier")), selection],
            variable=[
                *_as_list(options.get("variable")),
                *(f"{key}:{value}" for key, value in variables.items()),
            ],
            listener=[*_as_list(options.get("listener")), *listeners],
            pythonpath=[*_as_list(options.get("pythonpath")), *payload.pythonpath],
            outputdir=str(target),
            output="output.xml",
            log="log.html" if final and payload.log else "NONE",
            report="report.html" if final and payload.report else "NONE",
            runemptysuite=True,
            consolecolors="off",
        )
        if payload.rpa is not None:
            options["rpa"] = payload.rpa
        complaints = io.StringIO()
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            rc = robot_api.run(execution.root, stdout=devnull, stderr=complaints, **options)
        if rc == _STOPPED_BY_SIGNAL or (out_dir / STOP_FILE).exists():
            # Robot ended the execution in flight; do not go on to the next.
            raise _Stopped
        output = target / "output.xml"
        if not output.is_file():
            # Robot refused to execute at all (a data error): what it said is
            # the only explanation there will be.
            said = " ".join(complaints.getvalue().split())[:600]
            problem = (
                f"Robot produced no output for {len(execution.item_ids)} test(s) of "
                f"{execution.root} (return code {rc})" + (f": {said}" if said else "")
            )
            if "Conflicting execution modes" in said:
                problem += (
                    " — this root mixes tests and tasks: set the execution mode explicitly "
                    "(rpa=False runs everything as tests, rpa=True as tasks)"
                )
            problems.append(problem)
            say(problem, "error")
            return None
        return ShardOutput(
            path=str(output),
            attempt=attempt,
            root=execution.root,
            selected=len(execution.item_ids),
            tests=selection.kept,
        )

    executions = payload.executions
    single = len(executions) == 1 and not payload.rerun_failed
    for index, execution in enumerate(executions, start=1):
        if len(executions) > 1:
            say(f"execution {index}/{len(executions)}: {len(execution.item_ids)} test(s)")
        target = out_dir if single else out_dir / f"execution-{index}"
        produced = execute(execution, target, final=single)
        if produced is not None:
            outputs.append(produced)

    # Re-execute what failed, up to rerun_failed more times.
    roots = sorted({execution.root for execution in executions})
    last_by_root = {root: [o for o in outputs if o.root == root] for root in roots}
    for attempt in range(2, payload.rerun_failed + 2):
        again = False
        for root, previous in list(last_by_root.items()):
            failed = [
                test.longname
                for entry in previous
                if entry.tests
                for test in ExecutionResult(entry.path).suite.all_tests
                if test.status == "FAIL"
            ]
            if not failed:
                last_by_root[root] = []
                continue
            again = True
            say(f"attempt {attempt}: re-executing {len(failed)} failed test(s)")
            name = f"attempt-{attempt}" + (f"-root{roots.index(root) + 1}" if len(roots) > 1 else "")
            rerun = execute(
                Execution(root, tuple(failed)), out_dir / name, final=False, attempt=attempt
            )
            last_by_root[root] = [rerun] if rerun is not None else []
            if rerun is not None:
                outputs.append(rerun)
        if not again:
            break

    if not single and outputs:
        merge_outputs(
            [output.path for output in outputs],
            out_dir,
            log=payload.log,
            report=payload.report,
            merge=len({output.root for output in outputs}) == 1,
            rpa=payload.rpa,
        )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


# -- merging ---------------------------------------------------------------------------------

_ADDED = re.compile(r"^\*HTML\* (?:Test|Task|Suite) added from merged output\.(?:<hr>)?")


class MergeNoiseRemover:
    """Pre-rebot modifier: when outputs of *disjoint* executions are merged,
    every test and suite of a later output is stamped "added from merged
    output" in place of — or in front of — its own message. For the chunks
    of one run that is noise. This restores the message each one had; the
    note about a test that really was re-executed is left alone."""

    def visit_suite(self, suite: Any) -> None:
        self._restore(suite)
        for test in suite.tests:
            self._restore(test)
        for child in suite.suites:
            child.visit(self)

    @staticmethod
    def _restore(item: Any) -> None:
        message = item.message or ""
        match = _ADDED.match(message)
        if match is None:
            return
        rest = message[match.end():]
        plain = html.unescape(rest)
        # The merger escaped a plain message; an HTML one it kept as HTML.
        item.message = plain if "<" not in rest else f"*HTML* {rest}"


def merge_outputs(
    outputs: Sequence[str | Path],
    out_dir: str | Path,
    *,
    log: bool = True,
    report: bool = True,
    merge: bool = True,
    rpa: bool | None = None,
) -> Path:
    """Combine several ``output.xml`` into one ``output.xml`` (and
    ``log.html`` / ``report.html``) in ``out_dir``: later outputs merged
    over earlier ones (``merge=True`` — executions of one root: chunks,
    reruns), or side by side under one top-level suite (``merge=False`` —
    executions of different roots). Returns the merged ``output.xml``."""
    import robot

    robot_api: Any = robot
    options: dict[str, Any] = {
        "merge": merge,
        "outputdir": str(out_dir),
        "output": "output.xml",
        "log": "log.html" if log else "NONE",
        "report": "report.html" if report else "NONE",
        "processemptysuite": True,
        "consolecolors": "off",
        "prerebotmodifier": [MergeNoiseRemover()],
    }
    if rpa is not None:
        options["rpa"] = rpa
    with open(os.devnull, "w", encoding="utf-8") as devnull:
        robot_api.rebot(*[str(path) for path in outputs], stdout=devnull, stderr=devnull, **options)
    return Path(out_dir) / "output.xml"


# -- the supervisor ----------------------------------------------------------------------------


def stop_worker(process: Any, out_dir: str | Path, *, grace_s: float = 5.0) -> None:
    """The stop protocol for one worker process: leave the stop marker (so
    the worker starts no further execution), terminate (Robot ends the
    execution in flight and writes its output), and kill what has not ended
    after ``grace_s``."""
    if not process.is_alive():
        return
    try:
        Path(out_dir, STOP_FILE).touch()
    except OSError:
        pass
    process.terminate()
    process.join(timeout=grace_s)
    if process.is_alive():
        process.kill()
        process.join(timeout=grace_s)


def supervise(
    payloads: Sequence[ShardPayload],
    *,
    max_workers: int,
    on_done: Callable[[ShardOutcome], None] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    timeout_s: float | None = None,
    poll_s: float = 0.05,
) -> list[ShardOutcome]:
    """Run ``payloads`` on spawned worker processes, at most ``max_workers``
    at once, and hand each back as it finishes.

    Payloads that share a ``lane`` never overlap and start in the order
    given — a lane is something only one execution may use at a time (a
    partition's test environment). ``on_done(outcome)`` is called in the
    calling thread for every payload that ended, in the order they ended —
    deliver there (:func:`deliver_shard`), so that a shard's results are in
    while the others still execute. ``timeout_s`` bounds one shard; a shard
    over it is stopped and reported as timed out. When ``cancelled()``
    becomes true, running workers are stopped with :func:`stop_worker` and
    the function returns what had ended by then — payloads that never
    started are not in the result.
    """
    spawn = multiprocessing.get_context("spawn")
    pending = list(payloads)
    running: list[tuple[Any, ShardPayload, float]] = []
    outcomes: list[ShardOutcome] = []

    def finished(outcome: ShardOutcome) -> None:
        outcomes.append(outcome)
        if on_done is not None:
            on_done(outcome)

    try:
        while pending or running:
            if cancelled():
                break
            busy = {payload.lane for _process, payload, _started in running if payload.lane}
            index = 0
            while index < len(pending) and len(running) < max(1, int(max_workers)):
                payload = pending[index]
                if payload.lane and payload.lane in busy:
                    index += 1
                    continue
                pending.pop(index)
                Path(payload.out_dir).mkdir(parents=True, exist_ok=True)
                process = spawn.Process(
                    target=run_shard, args=(payload,), name=f"runcomposer-shard-{payload.shard}"
                )
                process.start()
                running.append((process, payload, time.monotonic()))
                if payload.lane:
                    busy.add(payload.lane)
            still_running = []
            for process, payload, started in running:
                process.join(timeout=poll_s)
                overrun = timeout_s is not None and time.monotonic() - started > timeout_s
                if process.is_alive() and not overrun:
                    still_running.append((process, payload, started))
            ended = [entry for entry in running if entry not in still_running]
            running = still_running
            for process, payload, _started in ended:
                if process.is_alive():
                    stop_worker(process, payload.out_dir)
                    finished(
                        ShardOutcome(
                            payload, process.exitcode, read_shard_result(payload.out_dir),
                            timed_out=True, timeout_s=timeout_s,
                        )
                    )
                else:
                    finished(
                        ShardOutcome(payload, process.exitcode, read_shard_result(payload.out_dir))
                    )
    finally:
        for process, payload, _started in running:
            stop_worker(process, payload.out_dir)
    return outcomes


# -- delivering what a worker produced ------------------------------------------------------------


def collect_verdicts(ctx: DispatchContext, outcome: ShardOutcome, *, format: str) -> list[Verdict]:
    """Parse every output of a finished shard through ``ctx.parse`` and label
    each verdict with the attempt its output is. A selected id that is not a
    test under its root is logged as a warning — nothing ran for it, so
    nothing is reported and it ends up ``MISSING``."""
    shard = outcome.payload.shard
    verdicts: list[Verdict] = []
    for output in outcome.outputs:
        if 0 <= output.tests < output.selected:
            ctx.log(
                f"shard {shard}: {output.selected - output.tests} of {output.selected} selected "
                f"test(s) do not exist under {output.root} — not executed",
                level="warning",
            )
        if output.tests == 0:
            continue  # an output without tests is an empty result, not a broken one
        for verdict in ctx.parse(output.path, format=format):
            verdicts.append(replace(verdict, attempt=output.attempt))
    return verdicts


def deliver_shard(
    ctx: DispatchContext,
    outcome: ShardOutcome,
    *,
    format: str = "robot-output-xml",
    group: str | None = None,
) -> str | None:
    """Deliver one finished shard under its name and record what it left to
    read: ``log.html``, ``report.html`` and ``output.xml`` as artifacts of
    the shard, and the shard's directory itself (so that what a log refers
    to — screenshots — is servable). Returns the reason when there is
    nothing to deliver (:attr:`ShardOutcome.problem`), else ``None``."""
    payload = outcome.payload
    if outcome.result is not None and outcome.result.error and not outcome.result.stopped:
        ctx.log(f"shard {payload.shard}: {outcome.result.error}", level="error")
    problem = outcome.problem
    if problem is not None:
        return problem
    ctx.deliver(payload.shard, verdicts=collect_verdicts(ctx, outcome, format=format), format=format)
    out_dir = Path(payload.out_dir)
    for name, media_type in (
        ("log.html", "text/html"),
        ("report.html", "text/html"),
        ("output.xml", "application/xml"),
    ):
        file = out_dir / name
        if file.is_file():
            ctx.add_artifact(
                f"{name} ({payload.shard})", file, media_type=media_type,
                shard=payload.shard, group=group,
            )
    ctx.add_artifact(
        f"files ({payload.shard})", out_dir, media_type="inode/directory",
        shard=payload.shard, group=group,
    )
    return None
