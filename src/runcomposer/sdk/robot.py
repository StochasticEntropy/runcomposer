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
- :class:`GroupMerger` — one merged result per shard group and one for the
  run, each merged in a spawned process and recorded before the delivery
  that completes the run.
- :func:`clean_output` — one ``output.xml`` rewritten without the keyword
  data Robot's own selectors pick, verdicts unchanged: the step
  ``ShardPayload.clean_output`` runs in the worker after every execution.
- :func:`merge_outputs` — several ``output.xml`` of one shard merged into
  one readable result, without the "added from merged output" notes a merge
  of disjoint executions would stamp on every test; ``rebot_options`` pass
  further options to rebot. :class:`MergePayload` / :func:`run_merge` /
  :func:`spawn_merge` — the same merge in a spawned process, for a server
  that must not run Robot itself; :class:`NameTopSuites` names the
  top-level suites of outputs combined side by side.
- :func:`attribute_roots` / :func:`root_mismatches` — which suite root each
  item is executed from, checked: an item of the catalog that the roots
  would not find is named before anything runs.

Robot Framework itself is imported only when something is executed (inside
the functions that need it): importing this module works without it, and
``robot_version()`` says whether it is there.

The id space is the Robot plugin family's: an item id is a test's longname.
"""

from __future__ import annotations

import html
import io
import json
import os
import re
import sys
import traceback
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from runcomposer.sdk import (
    STOP_FILE,
    ConfiguredCall,
    DispatchContext,
    Item,
    LiveChannel,
    RemoteLiveChannel,
    Verdict,
    WorkerOutcome,
    WorkerPayload,
    supervise_workers,
)

__all__ = [
    "RESULT_FILE",
    "STOP_FILE",
    "VARIABLE_NAME_PATTERN",
    "ExactSelection",
    "Execution",
    "GroupMerger",
    "LiveStatusListener",
    "MergeNoiseRemover",
    "MergePayload",
    "MergeResult",
    "NameTopSuites",
    "RemoteLiveStatusListener",
    "ShardOutcome",
    "ShardOutput",
    "ShardPayload",
    "ShardResult",
    "attribute_roots",
    "clean_output",
    "collect_verdicts",
    "deliver_shard",
    "merge_outputs",
    "read_shard_result",
    "robot_version",
    "root_mismatches",
    "run_merge",
    "run_shard",
    "spawn_merge",
    "supervise",
    "variable_name_problem",
]

#: What a worker writes into its output directory when it is done (or dies
#: trying): the hand-back the supervising process reads.
RESULT_FILE = ".shard-result.json"
# ``STOP_FILE`` is the SDK's own (``runcomposer.sdk.STOP_FILE``), re-exported:
# the supervisor leaves it in a shard's directory when the worker is asked to
# stop. Robot turns the first termination signal into a graceful end of the
# *current* execution; the file is how :func:`run_shard` knows not to start
# the next one.
#: The shape of a Robot variable name a payload may set (a regular
#: expression, matched whole): letters, digits, ``_``, spaces, ``.`` and
#: ``-``, beginning with a letter, a digit or ``_`` — no ``${…}``, and in
#: particular no ``:`` (Robot splits ``--variable NAME:value`` at the first
#: one, so ``BASE_URL:https`` would set ``${BASE_URL}``) and no ``=``. Robot
#: compares names ignoring case, spaces and underscores; that is left to it.
VARIABLE_NAME_PATTERN = r"[A-Za-z0-9_][A-Za-z0-9_ .\-]*"
_VARIABLE_NAME = re.compile(VARIABLE_NAME_PATTERN)


def variable_name_problem(name: object) -> str | None:
    """Why ``name`` cannot be set as a Robot variable by a payload — a
    sentence naming it — or ``None`` when it can (:data:`VARIABLE_NAME_PATTERN`).
    A name with ``:`` would set another variable than it names: Robot reads
    ``NAME:value`` up to the first colon."""
    text = name if isinstance(name, str) else None
    if text is not None and _VARIABLE_NAME.fullmatch(text) and text == text.strip():
        return None
    why = " — Robot reads NAME:value up to the first ':'" if isinstance(name, str) and ":" in name else ""
    return (
        f"{name!r} is not a plain Robot variable name (letters, digits, '_', spaces, '.', '-'; "
        f"no ':', '=' or ${{…}}){why}"
    )


#: Robot's return code for an execution that was forcefully stopped.
_STOPPED_BY_SIGNAL = 253
_STATUS_MAP = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP", "NOT RUN": "SKIP"}


def robot_version() -> str | None:
    """The installed Robot Framework's version, ``None`` when it is not
    installed (``pip install 'runcomposer[robot]'``). Read from the
    distribution's metadata: importing any part of the ``robot`` package
    loads all of it — result model and process-global logger included —
    which a server answering ``health`` has no use for."""
    from importlib import metadata

    try:
        return metadata.version("robotframework")
    except metadata.PackageNotFoundError:
        return None


def _suite_name(source: str | Path) -> str:
    """The name Robot gives the suite of a directory or file — the rule of
    ``robot.running.TestSuite.name_from_source``, restated so that the
    dispatching process needs no Robot import: the base name (a file without
    its extension), a ``prefix__`` dropped, underscores as spaces, an
    all-lower-case name title-cased."""
    if not str(source):
        return ""
    path = Path(source)
    name = path.name if path.is_dir() else path.stem
    if "__" in name:
        name = name.split("__", 1)[1] or name
    name = name.replace("_", " ").strip()
    return name.title() if name.islower() else name


# -- selection ---------------------------------------------------------------------------


class ExactSelection:
    """Pre-run modifier: keep exactly the tests whose longname is in ``ids``
    — no pattern matching, so an id is never read as a glob — optionally tag
    every kept test, and drop the suites left empty. ``kept`` counts what was
    found, which tells a caller that a selected id is not a test under the
    root it was executed from.

    Pass an instance as ``prerunmodifier`` (``robot.run(…,
    prerunmodifier=[ExactSelection(ids)])``).

    ``name`` is the top-level suite's name when the execution renames it
    (``robot.run(…, name=…)``): Robot applies that name only after the
    pre-run modifiers, so without it the ids — which carry the configured
    name — would match nothing.
    """

    def __init__(self, ids: Iterable[str], tag: str | None = None, name: str | None = None):
        self.ids = frozenset(ids)
        self.tag = tag
        self.name = name
        self.kept = 0

    def visit_suite(self, suite: Any) -> None:
        if self.name and suite.parent is None:
            suite.name = self.name
        suite.tests = [test for test in suite.tests if test.longname in self.ids]
        self.kept += len(suite.tests)
        if self.tag:
            for test in suite.tests:
                test.tags.add(self.tag)
        for child in suite.suites:
            child.visit(self)
        suite.suites = [child for child in suite.suites if child.test_count > 0]


#: How many mismatched items a refusal names before it only counts the rest.
_NAMED_MISMATCHES = 3


def attribute_roots(
    suite_roots: Sequence[str],
    item_ids: Sequence[str],
    items: Iterable[Item] = (),
    *,
    name: str | None = None,
    base: str | Path | None = None,
) -> dict[str, str]:
    """Which suite root each item is executed from — checked, also for a
    single root. Raises ``LookupError`` naming the items (the first few, and
    how many there are) that belong to no configured root, so that a run is
    refused before anything executes instead of ending ``MISSING``.

    With several roots, the catalog says which when it was built by the
    Robot source (``Item.meta["source"]`` lies under the root); otherwise the
    root suite's name — the first segment of the id — does. A single root is
    checked by :func:`root_mismatches`. ``name`` is the top-level suite name
    the execution sets (``robot_options: {name: …}``), which every root's
    suite then carries instead of its own.

    ``base`` is the directory a relative root is joined to for the check
    (default: the current directory). Pass it when the roots are spelled
    for a different copy of the same suites — the one an execution will
    use, ``suites/checkout`` — and the catalog's paths point into a local
    copy: each root is looked for under ``base``, and the answer keeps
    every root as given."""
    roots_of, problems = _assign_roots(suite_roots, item_ids, items, name, base)
    if problems:
        raise LookupError(_describe_mismatches(problems))
    return roots_of


def _describe_mismatches(problems: Sequence[str]) -> str:
    """:func:`root_mismatches` as one message: the first few, and how many
    there are."""
    named = "; ".join(problems[:_NAMED_MISMATCHES])
    if len(problems) == 1:
        return named
    more = len(problems) - _NAMED_MISMATCHES
    return (
        f"{len(problems)} item(s) do not belong to the configured suite roots: {named}"
        + (f"; … and {more} more" if more > 0 else "")
    )


def root_mismatches(
    suite_roots: Sequence[str],
    item_ids: Sequence[str],
    items: Iterable[Item] = (),
    *,
    name: str | None = None,
    base: str | Path | None = None,
) -> list[str]:
    """Every item of ``item_ids`` that belongs to none of ``suite_roots``,
    each as a sentence naming the item and the root — empty when all belong.
    The check :func:`attribute_roots` refuses on; ``runcomposer doctor``
    runs it over the whole catalog.

    With a single root, an item belongs to it when the first segment of its
    id is the name Robot gives that directory or file, and its
    recorded source (``Item.meta["source"]``, when the catalog records one)
    lies under the root. An item the Robot source collected from this very
    root (``Item.meta["root"]`` is the root) belongs whatever the root suite
    is called — a suite may rename itself. With ``name`` (the execution
    renames the top-level suite, ``robot_options: {name: …}``) that is the
    root suite's name. ``base`` anchors relative roots, as for
    :func:`attribute_roots`."""
    return _assign_roots(suite_roots, item_ids, items, name, base)[1]


def _assign_roots(
    suite_roots: Sequence[str],
    item_ids: Sequence[str],
    items: Iterable[Item],
    name: str | None = None,
    base: str | Path | None = None,
) -> tuple[dict[str, str], list[str]]:
    roots = [str(root) for root in suite_roots]
    meta = {item.id: item.meta for item in items}
    if not roots:
        return {}, [f"item {item_id!r}: no suite root is configured" for item_id in item_ids]

    def anchored(root: str) -> Path:
        path = Path(root)
        return path if base is None or path.is_absolute() else Path(base) / path

    resolved = {root: anchored(root).resolve() for root in roots}
    names = {root: name or _suite_name(anchored(root)) for root in roots}

    def under(path: str, root: str) -> bool:
        base, found = resolved[root], Path(path).resolve()
        return found == base or base in found.parents

    def named(item_id: str, root: str) -> bool:
        return item_id == names[root] or item_id.startswith(names[root] + ".")

    roots_of: dict[str, str] = {}
    problems: list[str] = []
    if len(roots) == 1:
        (root,) = roots
        for item_id in item_ids:
            recorded = meta.get(item_id) or {}
            source = str(recorded.get("source") or "")
            collected_here = bool(recorded.get("root")) and (
                Path(str(recorded["root"])).resolve() == resolved[root]
            )
            if source and not under(source, root):
                problems.append(
                    f"item {item_id!r} comes from {source}, which is not under the suite root "
                    f"{root!r} — the catalog was built from other suites than this runner executes"
                )
            elif not collected_here and not named(item_id, root):
                problems.append(
                    f"item {item_id!r} is not under the suite root {root!r}: executed from there, "
                    f"every test's id begins with {names[root] + '.'!r} — the catalog was built "
                    "from another directory than this runner executes"
                )
            else:
                roots_of[item_id] = root
        return roots_of, problems
    for item_id in item_ids:
        found = None
        source = str((meta.get(item_id) or {}).get("source") or "")
        if source:
            found = next((root for root in roots if under(source, root)), None)
        if found is None:
            found = next((root for root in roots if named(item_id, root)), None)
        if found is None:
            problems.append(
                f"item {item_id!r} belongs to none of the configured suite roots {roots}"
            )
        else:
            roots_of[item_id] = found
    return roots_of, problems


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
    start itself — a CI job, a remote agent: it pushes to the API, authorized by the
    ingest token of its dispatch, instead of writing to a local channel.

    Use it by name; what it needs comes from the arguments or, left out,
    from the environment::

        robot --listener runcomposer.sdk.robot.RemoteLiveStatusListener tests/

    ``RUNCOMPOSER_CALLBACK_URL`` (the run's results URL, or any URL of the
    run's API resource), ``RUNCOMPOSER_INGEST_TOKEN``, ``RUNCOMPOSER_SHARD``
    (default ``1``) and ``RUNCOMPOSER_DISPATCH_ID`` (optional). Without a
    callback URL it does nothing. Over HTTPS, ``RUNCOMPOSER_CA_BUNDLE`` names
    a file of the CA certificates to trust instead of the system's, and
    ``RUNCOMPOSER_VERIFY_TLS=false`` skips the check — the two variables the
    ``runcomposer-exec`` consumer reads, with the same meaning.
    """

    def __init__(
        self,
        callback_url: str | None = None,
        token: str | None = None,
        shard: str | None = None,
        dispatch_id: str | None = None,
        ca_bundle: str | None = None,
        verify_tls: bool | str | None = None,
    ):
        url = callback_url or os.environ.get("RUNCOMPOSER_CALLBACK_URL") or ""
        channel: Any = _NoChannel()
        if url:
            channel = RemoteLiveChannel(
                url,
                token or os.environ.get("RUNCOMPOSER_INGEST_TOKEN"),
                dispatch_id=dispatch_id or os.environ.get("RUNCOMPOSER_DISPATCH_ID") or None,
                ca_bundle=ca_bundle or os.environ.get("RUNCOMPOSER_CA_BUNDLE") or None,
                verify_tls=_verify_tls(verify_tls),
            )
        super().__init__(channel, shard or os.environ.get("RUNCOMPOSER_SHARD") or "1")


def _verify_tls(given: bool | str | None) -> bool:
    """A listener argument arrives as text (``robot --listener name:…``), the
    environment's value is text too: true or false in the words the consumer
    accepts; left out, the certificate is checked."""
    if isinstance(given, bool):
        return given
    word = (given if given is not None else os.environ.get("RUNCOMPOSER_VERIFY_TLS", "")).strip().lower()
    if not word or word in ("1", "true", "yes", "on"):
        return True
    if word in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"RUNCOMPOSER_VERIFY_TLS / verify_tls: not true or false: {word!r}")


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
    - ``variables`` — Robot variables (``name → value``); a name that is not
      a plain variable name (:func:`variable_name_problem` — a ``:`` in it
      would set another variable) is a ``ValueError``.
    - ``channel`` — where the worker writes log lines and, with
      ``live_status``, per-test status (``ctx.live_channel()``).
    - ``listeners`` — additional Robot listeners (import paths).
    - ``executed_tag`` — a tag added to every executed test.
    - ``worker_setup`` — ``"pkg.mod:callable"`` references (or
      :class:`~runcomposer.sdk.ConfiguredCall` entries, whose settings reach
      the callable as ``settings=``) called in the worker before the first
      execution with ``{shard, partition, run_id, dispatch_id, out_dir,
      variables, data}``; a mapping they return is added to the variables.
    - ``cwd`` — the worker changes into this directory before anything is
      executed (for suites that open data files by relative path);
      ``env`` — set in the worker's process environment (they
      are not Robot variables); ``worker_data`` — a picklable mapping made
      by the dispatching process, handed to ``worker_setup`` as
      ``info["data"]``.
    - ``rerun_failed`` — re-execute failed tests up to this many more times;
      every attempt is reported, the last one counts.
    - ``rpa`` — the execution mode: ``None`` lets Robot decide (it refuses a
      selection that mixes tests and tasks), ``False`` executes everything
      as tests, ``True`` as tasks.
    - ``robot_options`` — further keyword options for ``robot.run`` (for
      example ``{"loglevel": "DEBUG", "variablefile": ["env.py"]}``); the
      options this toolkit sets itself win. Robot's ``removekeywords`` /
      ``flattenkeywords`` among them shape the log and report Robot writes,
      never the execution's ``output.xml``; ``name`` renames the top-level
      suite (the ids then begin with it).
    - ``clean_output`` — ``{remove_keywords: [...], flatten_keywords:
      [...]}``, Robot's own selectors (``passed``, ``for``, ``wuks``,
      ``name:<pattern>``, ``tag:<pattern>`` …): every execution's
      ``output.xml`` is rewritten with them in the worker, right after the
      execution and before anything reads it (:func:`clean_output`) — so
      what is parsed, merged, recorded and pushed is the smaller file. The
      verdicts it holds are unchanged. The same selectors shape the log and
      report too, so that they show what the output holds.
    - ``post_execution`` — configured callables called in the worker after
      every execution, in order (after ``clean_output``), as ``step(output,
      info)`` (``settings=`` with ``{call, with}``): ``output`` the path of
      that execution's ``output.xml``, ``info`` what ``worker_setup`` gets,
      the final ``variables`` included, plus ``execution`` (its 1-based
      index in ``executions``; ``None`` for a re-execution of failures),
      ``attempt`` (1, or the attempt a re-execution is), ``rerun``,
      ``root`` and ``item_ids`` (what was selected for it). A step that
      raises fails that execution: its tests are reported ``ERROR`` with a
      message naming the step — the results the execution produced are
      neither used nor lost without a word — and the shard goes on with
      its next execution.
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
    worker_setup: tuple[str | ConfiguredCall, ...] = ()
    pythonpath: tuple[str, ...] = ()
    log: bool = True
    report: bool = True
    rerun_failed: int = 0
    rpa: bool | None = None
    robot_options: Mapping[str, Any] = field(default_factory=dict)
    run_id: str = ""
    dispatch_id: str = ""
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    worker_data: Mapping[str, Any] = field(default_factory=dict)
    clean_output: Mapping[str, Sequence[str]] = field(default_factory=dict)
    post_execution: tuple[str | ConfiguredCall, ...] = ()

    def __post_init__(self) -> None:
        # Checked where the payload is made, not in the worker: a selector
        # Robot would refuse is a ValueError naming it, and so is a variable
        # name that would set another variable than it names.
        object.__setattr__(self, "clean_output", _clean_settings(self.clean_output))
        for name in self.variables:
            problem = variable_name_problem(name)
            if problem is not None:
                raise ValueError(f"ShardPayload {self.shard!r}: variables: {problem}")

    @property
    def item_ids(self) -> tuple[str, ...]:
        """Every item this shard is to report."""
        return tuple(item for execution in self.executions for item in execution.item_ids)


@dataclass(frozen=True)
class ShardOutput:
    """One ``output.xml`` a worker produced: which attempt it is, how many
    tests were selected for it and how many of them existed under ``root``.
    ``failed`` says that a step after the execution (``clean_output``,
    ``post_execution``) raised: the file is not to be trusted, and the
    tests selected for it (``item_ids``) are reported ``ERROR`` with that
    message instead."""

    path: str
    attempt: int = 1
    root: str = ""
    selected: int = 0
    tests: int = 0
    item_ids: tuple[str, ...] = ()
    failed: str | None = None


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
    #: The worker process's id.
    pid: int | None = None
    #: How the worker ended, as ``WorkerOutcome.status`` says: ``finished``,
    #: ``raised``, ``died``, ``timed out``, or ``stopped`` (by a
    #: cancellation — only ``on_end`` is handed those).
    status: str = "finished"

    @property
    def outputs(self) -> tuple[ShardOutput, ...]:
        """The outputs that really exist on disk — and those a step after
        the execution failed on, whatever the step left (their tests are
        reported ``ERROR``)."""
        if self.result is None:
            return ()
        return tuple(
            output
            for output in self.result.outputs
            if output.failed is not None or Path(output.path).is_file()
        )

    @property
    def problem(self) -> str | None:
        """Why this shard has nothing to deliver — ``None`` when it has."""
        shard = self.payload.shard
        if self.timed_out:
            return f"{shard}: exceeded the shard timeout of {self.timeout_s:g}s"
        if self.status == "stopped":
            return f"{shard}: stopped before it ended"
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
            outputs=tuple(
                ShardOutput(**{**entry, "item_ids": tuple(entry.get("item_ids") or ())})
                for entry in data.get("outputs") or ()
            ),
            error=data.get("error"),
            stopped=bool(data.get("stopped")),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


# -- the worker process -------------------------------------------------------------------


class _Stopped(Exception):
    """The shard was asked to stop."""


def run_shard(payload: ShardPayload, stop: Callable[[], bool] | None = None) -> None:
    """Worker entry: execute one shard with real Robot Framework and write
    what happened to :data:`RESULT_FILE` in ``payload.out_dir``. Meant as the
    target of a spawned process (:func:`supervise` runs it that way); it can
    also be called in-process, where ``payload.cwd`` and ``payload.env``
    apply for the duration of the call.

    ``stop()`` (optional) says the shard was asked to stop — the
    supervisor's stop request; :data:`STOP_FILE` in ``out_dir`` says so
    too. No further execution starts once it is true."""
    out_dir = Path(os.path.abspath(payload.out_dir))  # unaffected by payload.cwd
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[ShardOutput] = []
    problems: list[str] = []
    error: str | None = None
    stopped = False

    def asked() -> bool:
        return (stop is not None and stop()) or (out_dir / STOP_FILE).exists()

    try:
        with _working_in(payload.cwd, payload.env):
            _execute_shard(payload, out_dir, outputs, problems, asked)
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


@contextmanager
def _working_in(cwd: str | None, env: Mapping[str, str]) -> Iterator[None]:
    """``cwd`` as the working directory and ``env`` in the process
    environment, both restored afterwards."""
    before_cwd = os.getcwd()
    before_env = {str(key): os.environ.get(str(key)) for key in env}
    try:
        if cwd:
            os.chdir(cwd)
        os.environ.update({str(key): str(value) for key, value in env.items()})
        yield
    finally:
        os.chdir(before_cwd)
        for key, value in before_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _supervised_shard(work: WorkerPayload, stop: Callable[[], bool]) -> None:
    """The worker target :func:`supervise` hands to ``supervise_workers``."""
    run_shard(work.data["payload"], stop)


def _execute_shard(
    payload: ShardPayload,
    out_dir: Path,
    outputs: list[ShardOutput],
    problems: list[str],
    asked: Callable[[], bool],
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
    renamed = payload.robot_options.get("name") or None  # robot.run's --name
    info = {
        "shard": payload.shard,
        "partition": payload.partition,
        "run_id": payload.run_id,
        "dispatch_id": payload.dispatch_id,
        "out_dir": str(out_dir),
        "data": dict(payload.worker_data),
    }
    for ref in payload.worker_setup:
        extra = ConfiguredCall.parse(ref, "worker_setup")(dict(info, variables=dict(variables)))
        if isinstance(extra, Mapping):  # a setup may contribute Robot variables
            variables.update({str(key): str(value) for key, value in extra.items()})

    # What runs after every execution: the built-in cleaning, then the
    # deployment's own steps — each as (what it is called, the callable).
    cleaning = _clean_settings(payload.clean_output)
    steps: list[tuple[str, Callable[..., Any]]] = []
    if cleaning:
        steps.append(("clean_output", ConfiguredCall(f"{__name__}:clean_output", cleaning)))
    for ref in payload.post_execution:
        call = ConfiguredCall.parse(ref, "post_execution")
        steps.append((f"post_execution step {call.ref!r}", call))

    def after(output: ShardOutput, execution: Execution, index: int | None) -> ShardOutput:
        """The steps after one execution, in order; the first that raises
        fails the execution and the rest do not run."""
        step_info = dict(
            info, variables=dict(variables), execution=index, attempt=output.attempt,
            rerun=output.attempt > 1, root=execution.root, item_ids=list(execution.item_ids),
        )
        for label, step in steps:
            try:
                step(output.path, dict(step_info))
            except Exception as exc:
                what = f"execution {index}" if index is not None else f"attempt {output.attempt}"
                problem = (
                    f"{label} raised after {what} of {execution.root}: {type(exc).__name__}: {exc}"
                    f" — its {len(execution.item_ids)} test(s) are reported as ERROR; what the "
                    "execution produced is not used"
                )
                problems.append(problem)
                say(problem, "error")
                return replace(output, failed=problem)
        return output

    def execute(execution: Execution, target: Path, *, final: bool, attempt: int = 1) -> ShardOutput | None:
        """One Robot execution; what it produced, or None without an output."""
        if asked():
            raise _Stopped
        target.mkdir(parents=True, exist_ok=True)
        listeners: list[Any] = list(payload.listeners)
        if channel is not None and payload.live_status:
            listeners.append(LiveStatusListener(channel, payload.shard, asked))
        selection = ExactSelection(execution.item_ids, payload.executed_tag, renamed)
        options: dict[str, Any] = dict(payload.robot_options)
        # The log and report show what the cleaned output holds.
        for option, key in (("removekeywords", "remove_keywords"), ("flattenkeywords", "flatten_keywords")):
            if cleaning.get(key):
                options[option] = [*_as_list(options.get(option)), *cleaning[key]]
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
        if rc == _STOPPED_BY_SIGNAL or asked():
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
        produced = ShardOutput(
            path=str(output),
            attempt=attempt,
            root=execution.root,
            selected=len(execution.item_ids),
            tests=selection.kept,
            item_ids=tuple(execution.item_ids),
        )
        return after(produced, execution, index_of.get(id(execution)))

    executions = payload.executions
    index_of = {id(execution): index for index, execution in enumerate(executions, start=1)}
    single = len(executions) == 1 and not payload.rerun_failed
    for index, execution in enumerate(executions, start=1):
        if len(executions) > 1:
            say(f"execution {index}/{len(executions)}: {len(execution.item_ids)} test(s)")
        target = out_dir if single else out_dir / f"execution-{index}"
        produced = execute(execution, target, final=single)
        if produced is not None:
            outputs.append(produced)

    # Re-execute what failed, up to rerun_failed more times — from results
    # that are trusted only.
    roots = sorted({execution.root for execution in executions})
    last_by_root = {root: [o for o in outputs if o.root == root] for root in roots}
    for attempt in range(2, payload.rerun_failed + 2):
        again = False
        for root, previous in list(last_by_root.items()):
            failed = [
                test.longname
                for entry in previous
                if entry.tests and entry.failed is None
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

    trusted = [output for output in outputs if output.failed is None]
    if not single and trusted:
        merge_outputs(
            [output.path for output in trusted],
            out_dir,
            log=payload.log,
            report=payload.report,
            merge=len({output.root for output in trusted}) == 1,
            rpa=payload.rpa,
        )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


# -- cleaning an output -------------------------------------------------------------------

#: Robot's own selectors (``--removekeywords`` / ``--flattenkeywords``),
#: besides ``name:<pattern>`` and ``tag:<pattern>``; case does not matter.
_REMOVE_SELECTORS = ("all", "passed", "for", "while", "wuks")
_FLATTEN_SELECTORS = ("for", "while", "iteration", "foritem")
_CLEAN_KEYS = {"remove_keywords": _REMOVE_SELECTORS, "flatten_keywords": _FLATTEN_SELECTORS}


def _clean_settings(settings: Any, where: str = "clean_output") -> dict[str, list[str]]:
    """``{remove_keywords: [...], flatten_keywords: [...]}`` checked — Robot's
    own selectors, without importing Robot — and with the empty ones left
    out. Raises ``ValueError`` naming what is wrong."""
    if not settings:
        return {}
    if not isinstance(settings, Mapping):
        raise ValueError(
            f"{where} is a mapping {{remove_keywords: [...], flatten_keywords: [...]}}, "
            f"got {settings!r}"
        )
    unknown = sorted(str(key) for key in settings if key not in _CLEAN_KEYS)
    if unknown:
        raise ValueError(f"{where} takes remove_keywords and flatten_keywords, got {unknown}")
    checked: dict[str, list[str]] = {}
    for key, words in _CLEAN_KEYS.items():
        values = settings.get(key) or []
        values = [values] if isinstance(values, str) else values
        if not isinstance(values, (list, tuple)):
            raise ValueError(f"{where}.{key} is a list of selectors, got {values!r}")
        for value in values:
            text = str(value)
            kind, sep, _pattern = text.partition(":")
            if text.lower() not in words and not (sep and kind.lower() in ("name", "tag")):
                raise ValueError(
                    f"{where}.{key}: {text!r} is not a selector — use "
                    + ", ".join(words) + ", name:<pattern> or tag:<pattern>"
                )
        if values:
            checked[key] = [str(value) for value in values]
    return checked


def clean_output(
    output: str | Path, info: Mapping[str, Any] | None = None, *, settings: Mapping[str, Any]
) -> Path:
    """Rewrite one ``output.xml`` without the keyword data ``settings``
    selects — ``{remove_keywords: [...], flatten_keywords: [...]}``,
    Robot's own ``--removekeywords`` / ``--flattenkeywords`` selectors —
    through Robot's result model, atomically: the cleaned file is written
    beside the output and then replaces it. The tests, their statuses,
    messages, tags and times are what they were; only keywords (and what
    they logged) go. Returns the path.

    A ``post_execution`` step (``info`` is accepted and not needed), and what
    ``ShardPayload.clean_output`` runs after every execution. It runs Robot
    in the calling process: call it in a worker, not in a server."""
    selectors = _clean_settings(settings)
    path = Path(output)
    if not selectors:
        return path
    from robot.api import ExecutionResult

    result = ExecutionResult(str(path), flattened_keywords=selectors.get("flatten_keywords", []))
    for how in selectors.get("remove_keywords", []):
        result.suite.remove_keywords(how)
    beside = path.with_name(f".{path.name}.cleaning")
    try:
        result.save(str(beside))
        os.replace(beside, path)
    finally:
        beside.unlink(missing_ok=True)
    return path


# -- merging ---------------------------------------------------------------------------------

_ADDED = re.compile(r"^\*HTML\* (?:Test|Task|Suite) added from merged output\.(?:<hr>)?")


class MergeNoiseRemover:
    """Pre-rebot modifier: when outputs of *disjoint* executions are merged,
    every test and suite of a later output is stamped "added from merged
    output" in place of — or in front of — its own message. Between the
    chunks of a single run that note tells nobody anything. This restores the message each one had; the
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


class NameTopSuites:
    """Pre-rebot modifier for outputs combined side by side
    (``merge=False``): gives the top-level suites, in the order the outputs
    were given, the names in ``names`` — so that the same suite executed on
    two partitions reads as ``eu`` and ``us`` instead of twice ``Tests``.
    Only the copy for people is renamed; the outputs merged are untouched."""

    def __init__(self, names: Sequence[str]):
        self.names = tuple(str(name) for name in names)

    def visit_suite(self, suite: Any) -> None:
        for child, name in zip(suite.suites, self.names, strict=False):
            child.name = name
        suite.name = " & ".join(child.name for child in suite.suites)


#: What :func:`merge_outputs` decides itself — not to be given again through
#: ``rebot_options`` (compared case-insensitively, ``-`` and ``_`` ignored, as
#: Robot reads option names).
_MERGE_OWN_OPTIONS = {
    "merge": "merge=",
    "outputdir": "out_dir",
    "output": "out_dir (the merged file is always output.xml)",
    "log": "log=",
    "report": "report=",
    "rpa": "rpa=",
    "processemptysuite": "nothing — an empty merge is still written",
    "consolecolors": "nothing — rebot writes to no console",
    "stdout": "nothing — rebot writes to no console",
    "stderr": "nothing — rebot writes to no console",
}


def _rebot_options(rebot_options: Mapping[str, Any] | None) -> dict[str, Any]:
    """The extra rebot options, checked: none of those merge_outputs sets
    itself. Raises ``ValueError`` naming each and what to use instead."""
    extra = {str(name): value for name, value in (rebot_options or {}).items()}
    refused = [
        name for name in extra if name.lower().replace("-", "").replace("_", "") in _MERGE_OWN_OPTIONS
    ]
    if refused:
        raise ValueError(
            "rebot_options may not set "
            + ", ".join(
                f"{name!r} (use {_MERGE_OWN_OPTIONS[name.lower().replace('-', '').replace('_', '')]})"
                for name in refused
            )
            + " — merge_outputs sets these itself"
        )
    return extra


def merge_outputs(
    outputs: Sequence[str | Path],
    out_dir: str | Path,
    *,
    log: bool = True,
    report: bool = True,
    merge: bool = True,
    rpa: bool | None = None,
    rebot_options: Mapping[str, Any] | None = None,
) -> Path:
    """Combine several ``output.xml`` into one ``output.xml`` (and
    ``log.html`` / ``report.html``) in ``out_dir``: later outputs merged
    over earlier ones (``merge=True`` — executions of one root: chunks,
    reruns), or side by side under one top-level suite (``merge=False`` —
    executions of different roots). Returns the merged ``output.xml``.

    ``rebot_options`` are further keyword options for Robot's ``rebot``
    (``{"removekeywords": ["passed"], "logtitle": "Checkout on eu"}``). The
    options this function sets itself (``merge``, ``outputdir``, ``output``,
    ``log``, ``report``, ``rpa``, ``processemptysuite``, ``consolecolors``,
    ``stdout``, ``stderr``) are refused by name with a ``ValueError``; a
    ``prerebotmodifier`` given there runs after the toolkit's own.

    Robot executes inside the calling process here, its shared logging
    state included. A server process merges with
    :func:`spawn_merge` instead."""
    return _rebot(
        outputs, out_dir, log=log, report=report, merge=merge, rpa=rpa,
        rebot_options=rebot_options,
    )[0]


def _rebot(
    outputs: Sequence[str | Path],
    out_dir: str | Path,
    *,
    log: bool,
    report: bool,
    merge: bool,
    rpa: bool | None,
    rebot_options: Mapping[str, Any] | None,
) -> tuple[Path, str]:
    """:func:`merge_outputs`, and what rebot complained about."""
    extra = _rebot_options(rebot_options)
    import robot

    robot_api: Any = robot
    sources, made = _relinked(outputs, out_dir)
    try:
        return _rebot_sources(sources, out_dir, robot_api, extra, log=log, report=report, merge=merge, rpa=rpa)
    finally:
        for copy in made:
            copy.unlink(missing_ok=True)


#: An ``src=`` / ``href=`` attribute in an HTML message, and a link that is
#: not relative (``scheme:``, ``/…``, ``#…``).
_LINK = re.compile(r"""(?P<attr>\b(?:src|href)\s*=\s*)(?P<q>["'])(?P<url>.*?)(?P=q)""", re.IGNORECASE | re.DOTALL)
_NOT_RELATIVE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|[/\\#])")


def _relink_html(text: str, prefix: str) -> str:
    """Every relative link of an HTML text, prefixed with ``prefix``."""

    def moved(match: re.Match[str]) -> str:
        url = match.group("url")
        if not url or _NOT_RELATIVE.match(url):
            return match.group(0)
        return f"{match.group('attr')}{match.group('q')}{prefix}/{url}{match.group('q')}"

    return _LINK.sub(moved, text)


def _mentions_html(path: Path) -> bool:
    """Whether an output holds an HTML message at all — read in pieces,
    since an output can be large."""
    needles = (b'html="true"', b"*HTML*")
    keep = max(len(needle) for needle in needles)
    tail = b""
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            window = tail + chunk
            if any(needle in window for needle in needles):
                return True
            tail = window[-keep:]
    return False


def _relinked(outputs: Sequence[str | Path], out_dir: str | Path) -> tuple[list[str], list[Path]]:
    """The outputs to merge into ``out_dir``, with the relative links of
    their HTML messages (a screenshot a library logged, a file linked from a
    message) rewritten to be relative to ``out_dir`` — where the merged log
    is written — instead of to the directory each output came from. An
    output that needs it is copied (a hidden file in ``out_dir``, removed
    after the merge); the outputs themselves are never changed. Returns the
    sources to merge and the copies made."""
    target = Path(out_dir).resolve()
    sources: list[str] = []
    made: list[Path] = []
    for index, output in enumerate(outputs, start=1):
        path = Path(output)
        prefix = Path(os.path.relpath(path.resolve().parent, target)).as_posix()
        if prefix == "." or not path.is_file() or not _mentions_html(path):
            sources.append(str(output))
            continue
        from urllib.parse import quote

        from robot.api import ExecutionResult, ResultVisitor

        class Relink(ResultVisitor):
            def __init__(self, moved: str):
                self.moved = moved

            def visit_message(self, message: Any) -> None:
                if message.html:
                    message.message = _relink_html(message.message, self.moved)

            def start_suite(self, suite: Any) -> None:
                if (suite.message or "").startswith("*HTML*"):
                    suite.message = _relink_html(suite.message, self.moved)

            def start_test(self, test: Any) -> None:
                if (test.message or "").startswith("*HTML*"):
                    test.message = _relink_html(test.message, self.moved)

        result = ExecutionResult(str(path))
        result.visit(Relink(quote(prefix)))
        target.mkdir(parents=True, exist_ok=True)
        copy = target / f".relinked-{index}.xml"
        result.save(str(copy))
        sources.append(str(copy))
        made.append(copy)
    return sources, made


def _rebot_sources(
    outputs: Sequence[str],
    out_dir: str | Path,
    robot_api: Any,
    extra: dict[str, Any],
    *,
    log: bool,
    report: bool,
    merge: bool,
    rpa: bool | None,
) -> tuple[Path, str]:
    modifiers = [MergeNoiseRemover(), *_as_list(extra.pop("prerebotmodifier", None))]
    options: dict[str, Any] = {
        **extra,
        "merge": merge,
        "outputdir": str(out_dir),
        "output": "output.xml",
        "log": "log.html" if log else "NONE",
        "report": "report.html" if report else "NONE",
        "processemptysuite": True,
        "consolecolors": "off",
        "prerebotmodifier": modifiers,
    }
    if rpa is not None:
        options["rpa"] = rpa
    complaints = io.StringIO()
    with open(os.devnull, "w", encoding="utf-8") as devnull:
        robot_api.rebot(*[str(path) for path in outputs], stdout=devnull, stderr=complaints, **options)
    return Path(out_dir) / "output.xml", " ".join(complaints.getvalue().split())[:600]


#: What a merge process writes into its output directory when it is done.
_MERGE_RESULT_FILE = ".merge-result.json"


@dataclass(frozen=True)
class MergePayload:
    """Everything a merge process needs — the arguments of
    :func:`merge_outputs`. Picklable: it travels to a spawned process."""

    outputs: tuple[str, ...]
    out_dir: str
    log: bool = True
    report: bool = True
    merge: bool = True
    rpa: bool | None = None
    rebot_options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MergeResult:
    """How a merge ended: the merged ``output.xml`` (and ``log.html`` /
    ``report.html`` beside it when asked for), or why there is none."""

    output: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.output is not None and self.error is None


def run_merge(payload: MergePayload) -> None:
    """Merge-process entry: :func:`merge_outputs` with the payload's
    arguments, and what happened written to :data:`_MERGE_RESULT_FILE` in
    ``payload.out_dir``. A module-level function, so it can be the target of
    a spawned process (:func:`spawn_merge` uses it that way)."""
    out_dir = Path(payload.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {"output": None, "error": None}
    try:
        merged, said = _rebot(
            list(payload.outputs), out_dir, log=payload.log, report=payload.report,
            merge=payload.merge, rpa=payload.rpa, rebot_options=payload.rebot_options,
        )
        if merged.is_file():
            document["output"] = str(merged)
        else:
            document["error"] = "rebot wrote no output.xml" + (f": {said}" if said else "")
    except BaseException as exc:
        document["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        (out_dir / _MERGE_RESULT_FILE).write_text(json.dumps(document), encoding="utf-8")


def spawn_merge(
    payload: MergePayload,
    *,
    timeout_s: float | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    poll_s: float = 0.05,
) -> MergeResult:
    """:func:`run_merge` in a spawned process, waited for: the calling
    process imports nothing of Robot Framework and never touches its
    process-global logger, so a server can merge while it serves — and
    several merges can run at once. ``rebot_options`` are checked here,
    before anything is started (a ``ValueError``). A merge over
    ``timeout_s``, or one still running when ``cancelled()`` becomes true,
    is stopped and ends with an error.

    The process is the SDK's worker supervisor's (``supervise_workers``,
    one payload, the merge as its target): one implementation of starting,
    stopping and reaping a worker for every process this toolkit runs."""
    _rebot_options(payload.rebot_options)
    out_dir = Path(payload.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / _MERGE_RESULT_FILE).unlink(missing_ok=True)
    outcomes = supervise_workers(
        [WorkerPayload(shard="merge", out_dir=str(out_dir), data={"payload": payload})],
        _supervised_merge,
        max_workers=1,
        cancelled=cancelled,
        timeout_s=timeout_s,
        poll_s=poll_s,
    )
    if not outcomes:  # stopped (or never started) because of the cancellation
        return MergeResult(error="the merge was cancelled")
    (outcome,) = outcomes
    if outcome.timed_out:
        return MergeResult(error=f"the merge exceeded its timeout of {timeout_s:g}s")
    try:
        data = json.loads((out_dir / _MERGE_RESULT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MergeResult(
            error=f"the merge process ended (exit code {outcome.exitcode}) without a result"
        )
    return MergeResult(output=data.get("output"), error=data.get("error"))


def _supervised_merge(work: WorkerPayload, stop: Callable[[], bool]) -> None:
    """The worker target :func:`spawn_merge` hands to ``supervise_workers``.
    A merge has nothing to bring to an end gracefully, so the first stop
    request ends it (the supervisor's first ``SIGTERM`` otherwise only
    asks)."""
    if os.name != "nt":
        import signal

        signal.signal(signal.SIGTERM, _end_merge)
    if stop():  # asked while it was starting
        raise SystemExit(1)
    run_merge(work.data["payload"])


def _end_merge(signum: int, _frame: Any) -> None:
    raise SystemExit(128 + signum)


# -- the supervisor ----------------------------------------------------------------------------


def supervise(
    payloads: Sequence[ShardPayload],
    *,
    max_workers: int,
    on_done: Callable[[ShardPayload, ShardOutcome], None] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    timeout_s: float | None = None,
    poll_s: float = 0.05,
    log: Callable[[str, str], None] | None = None,
    grace_s: float = 5.0,
    on_start: Callable[[ShardPayload, int], None] | None = None,
    on_end: Callable[[ShardPayload, int, ShardOutcome], None] | None = None,
) -> list[ShardOutcome]:
    """Run ``payloads`` on spawned worker processes, at most ``max_workers``
    at once, and hand each back as it finishes — ``supervise_workers`` of
    the SDK with :func:`run_shard` as the worker.

    Payloads that share a ``lane`` never overlap and start in the order
    given — a lane is something only one execution may use at a time (a
    partition's test environment). ``on_done(payload, outcome)`` — the
    convention of ``supervise_workers`` — is called in the calling thread
    for every payload that ended, in the order they ended —
    deliver there (:func:`deliver_shard`), so that a shard's results are in
    while the others still execute. ``timeout_s`` bounds one shard; a shard
    over it is stopped and reported as timed out. When ``cancelled()``
    becomes true, running workers are stopped — asked first, so that Robot
    ends the execution in flight and writes its output, terminated and
    killed after ``grace_s`` each, together with what they started — and
    the function returns what had ended by then: payloads that never
    started, and those stopped, are not in the result. ``log(message,
    level)`` is told of every shard's start and of its end; by default
    that is the ``log`` of the payloads' ``channel``. ``on_start(payload,
    pid)`` and ``on_end(payload, pid, outcome)`` say which worker processes
    were started and how each ended, as for ``supervise_workers``; ``outcome``
    is a :class:`ShardOutcome`, as ``on_done`` gets — for every worker, a
    stopped one included (``status`` ``stopped``). Every worker stops itself
    when the supervising process is gone.
    """
    shard_payloads = list(payloads)
    work = [
        WorkerPayload(
            shard=payload.shard,
            out_dir=payload.out_dir,
            item_ids=payload.item_ids,
            lane=payload.lane or None,
            data={"payload": payload},
        )
        for payload in shard_payloads
    ]
    if log is None:
        channel = next((p.channel for p in shard_payloads if p.channel is not None), None)
        log = channel.log if channel is not None else None
    outcomes: list[ShardOutcome] = []

    # One ShardOutcome per worker, handed to on_done and to on_end alike.
    seen: dict[tuple[str, int | None], ShardOutcome] = {}

    def shard_outcome_of(done: WorkerPayload, outcome: WorkerOutcome) -> ShardOutcome:
        key = (done.shard, outcome.pid)
        if key not in seen:
            seen[key] = ShardOutcome(
                done.data["payload"],
                outcome.exitcode,
                read_shard_result(done.out_dir),
                timed_out=outcome.timed_out,
                timeout_s=timeout_s if outcome.timed_out else None,
                pid=outcome.pid,
                status=outcome.status,
            )
        return seen[key]

    def finished(done: WorkerPayload, outcome: WorkerOutcome) -> None:
        shard_outcome = shard_outcome_of(done, outcome)
        outcomes.append(shard_outcome)
        if on_done is not None:
            on_done(shard_outcome.payload, shard_outcome)

    supervise_workers(
        work,
        _supervised_shard,
        max_workers=max_workers,
        on_done=finished,
        cancelled=cancelled,
        timeout_s=timeout_s,
        grace_s=grace_s,
        log=log,
        poll_s=poll_s,
        on_start=(lambda work, pid: on_start(work.data["payload"], pid)) if on_start else None,
        on_end=(
            (lambda work, pid, outcome: on_end(work.data["payload"], pid, shard_outcome_of(work, outcome)))
            if on_end else None
        ),
    )
    return outcomes


# -- merged results per group ----------------------------------------------------------------


class GroupMerger:
    """One merged result per shard group and one for the whole dispatch,
    recorded as artifacts of the group and of the run — each merged in a
    spawned process (:func:`spawn_merge`), so the dispatching process never
    runs Robot itself.

    A group is merged in the background as soon as its last shard has
    ended, while the others still execute. What that leaves — the group of
    the shard that ends last, and the whole run — is merged in
    :meth:`finish`, and the shard that ended last is held back until then:
    its delivery completes the run, and every merged artifact is recorded
    before it. A merge never decides a verdict: one that fails is a log line
    and no artifact, and once the dispatch is cancelled nothing more is
    merged.

    Use it from ``on_done`` and after :func:`supervise` returns::

        merger = GroupMerger(ctx, payloads, groups)
        supervise(payloads, max_workers=4, cancelled=lambda: ctx.cancelled,
                  on_done=lambda _p, outcome: [deliver(o) for o in merger.shard_ended(outcome)])
        for outcome in merger.finish():
            deliver(outcome)

    - ``groups`` maps a shard to its group (the ``groups`` of
      ``ctx.declare``); a shard in no group is merged into the run only.
      Groups are merged in the order their first shard appears in
      ``payloads``.
    - ``side_by_side`` — the run's result puts the groups' results next to
      each other, each top-level suite named after its group (the same tests
      executed on every partition), instead of merging them into one tree.
    - ``log`` / ``report`` / ``rpa`` — as for :func:`merge_outputs`;
      ``out_dir`` — where the merges are written (default
      ``ctx.workdir / "_merged"``).
    """

    def __init__(
        self,
        ctx: DispatchContext,
        payloads: Sequence[ShardPayload],
        groups: Mapping[str, str],
        *,
        side_by_side: bool = False,
        log: bool = True,
        report: bool = True,
        rpa: bool | None = None,
        out_dir: str | Path | None = None,
    ):
        import threading

        self._ctx = ctx
        self._payloads = list(payloads)
        self._group_of = dict(groups)
        self._side_by_side = side_by_side
        self._log, self._report, self._rpa = log, report, rpa
        self._base = Path(out_dir) if out_dir is not None else ctx.workdir / "_merged"
        self._groups: list[str] = []
        for payload in self._payloads:
            group = self._group_of.get(payload.shard)
            if group is not None and group not in self._groups:
                self._groups.append(group)
        self._members = {
            group: [p.shard for p in self._payloads if self._group_of.get(p.shard) == group]
            for group in self._groups
        }
        self._outcomes: dict[str, ShardOutcome] = {}
        self._results: list[tuple[str | None, MergeResult, int]] = []
        self._lock = threading.Lock()
        self._thread_class = threading.Thread
        self._threads: list[Any] = []
        self._started: set[str] = set()
        self._held: list[ShardOutcome] = []

    def shard_ended(self, outcome: ShardOutcome) -> list[ShardOutcome]:
        """Note a shard that ended; returns what is to be delivered now —
        the shard itself, unless it is the last of all payloads (held back
        for :meth:`finish`). Records the merges that have finished since,
        so call it in the dispatching thread (``on_done`` is)."""
        self._outcomes[outcome.payload.shard] = outcome
        if len(self._outcomes) >= len(self._payloads):
            self._held.append(outcome)
            return []
        group = self._group_of.get(outcome.payload.shard)
        if group is not None and group not in self._started and all(
            shard in self._outcomes for shard in self._members[group]
        ):
            self._started.add(group)
            thread = self._thread_class(
                target=self._merge_group, args=(group,), name=f"runcomposer-merge-{group}", daemon=True
            )
            self._threads.append(thread)
            thread.start()
        self._record_finished()
        return [outcome]

    def finish(self) -> list[ShardOutcome]:
        """Wait for the background merges, merge what is left — unless the
        dispatch was cancelled or no shard is held back — record every
        merged result, and return the held-back shard(s) to deliver now.
        Call it after :func:`supervise` returned, also when it raised (in a
        ``finally``)."""
        for thread in self._threads:
            thread.join()
        if self._held and not self._ctx.cancelled:
            for group in self._groups:
                if group not in self._started and not self._ctx.cancelled:
                    self._started.add(group)
                    self._merge_group(group)
            if not self._ctx.cancelled:
                self._merge_run()
        self._record_finished()
        held, self._held = self._held, []
        return held

    # -- the merges --------------------------------------------------------------------------

    def _record_finished(self) -> None:
        with self._lock:
            results, self._results = self._results, []
        for group, result, count in results:
            self._record(group, result, count)

    def _outputs(self, shards: Sequence[str]) -> list[tuple[str, frozenset[str]]]:
        """The ``output.xml`` of every delivered shard among ``shards``,
        with the roots it executed."""
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
            sources: list[str] = []
            names: list[str] = []
            for index, group in enumerate(self._groups, start=1):
                merged = self._base / f"group-{index}" / "output.xml"
                own = self._outputs(self._members[group])
                if len(own) == 1:
                    sources.append(own[0][0])
                    names.append(group)
                elif merged.is_file():
                    sources.append(str(merged))
                    names.append(group)
            side_by_side = self._side_by_side and len(sources) > 1
            result = self._merge(
                sources, self._base / "run", merge=not side_by_side,
                names=names if side_by_side else None,
            )
            count = len(sources)
        else:
            outputs = self._outputs([payload.shard for payload in self._payloads])
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
        return spawn_merge(
            MergePayload(
                outputs=tuple(outputs), out_dir=str(out_dir), log=self._log,
                report=self._report, merge=merge, rpa=self._rpa, rebot_options=options,
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


# -- delivering what a worker produced ------------------------------------------------------------


def collect_verdicts(ctx: DispatchContext, outcome: ShardOutcome, *, format: str) -> list[Verdict]:
    """Parse every output of a finished shard through ``ctx.parse`` and label
    each verdict with the attempt its output is. A selected id that is not a
    test under its root is logged as a warning — nothing ran for it, so
    nothing is reported and it ends up ``MISSING``. An output a step after
    its execution failed on (:attr:`ShardOutput.failed`) is not parsed: its
    selected tests are ``ERROR`` with the step's message."""
    shard = outcome.payload.shard
    verdicts: list[Verdict] = []
    for output in outcome.outputs:
        if output.failed is not None:
            verdicts.extend(
                Verdict(item_id=item_id, status="ERROR", message=output.failed, attempt=output.attempt)
                for item_id in output.item_ids
            )
            continue
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
    nothing to deliver (:attr:`ShardOutcome.problem`), else ``None``.

    The artifacts are recorded *before* the delivery: the delivery of the
    last outstanding shard completes the run, and what a runner records
    after it arrives after the run's completion event — an observer
    reacting to ``run.completed`` would not see it."""
    payload = outcome.payload
    if outcome.result is not None and outcome.result.error and not outcome.result.stopped:
        ctx.log(f"shard {payload.shard}: {outcome.result.error}", level="error")
    problem = outcome.problem
    if problem is not None:
        return problem
    verdicts = collect_verdicts(ctx, outcome, format=format)
    _record_shard_artifacts(ctx, payload, group)
    ctx.deliver(payload.shard, verdicts=verdicts, format=format)
    return None


def _record_shard_artifacts(ctx: DispatchContext, payload: ShardPayload, group: str | None) -> None:
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
