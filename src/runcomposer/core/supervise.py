"""Shards executed in worker processes: the supervisor every in-process
runner needs, whatever executes inside the worker.

:func:`supervise_workers` runs one spawned process per shard. Shards that
share a *lane* run one after another, in the order given; lanes run side by
side, at most ``max_workers`` shards at a time. Each worker calls ``target``
— a module-level callable, so that it can be sent to a spawned process —
with its :class:`WorkerPayload` and a ``stop`` callable; what ``target``
returns travels back to the supervising process. ``on_done(payload,
outcome)`` is called there as each shard ends, so a runner delivers a
shard's results while the others still execute.

The stop protocol, used for a cancellation and for a shard over its
timeout — on POSIX each worker runs in a session of its own, so that a
signal to its process group reaches what it started as well:

1. **ask**: ``stop()`` becomes true in the worker, the file
   :data:`STOP_FILE` appears in its ``out_dir`` (for what the worker started
   that cannot see ``stop``), and its process group receives ``SIGTERM``.
   The worker itself turns that first signal into the stop request; what it
   started ends the way a terminated process does.
2. after ``grace_s``: **terminate** — ``SIGTERM`` to the group again, which
   ends a worker that did not stop on its own (``SystemExit`` in it).
3. after ``grace_s``: **kill** — ``SIGKILL`` to the group.

Finally whatever is left of the worker's group is killed, and the worker
process is reaped by the supervisor. (On Windows there is no process group
to signal: ``stop()`` is the request, then the worker process is terminated
and killed.)

A worker also watches the process that supervises it, through a pipe that
only the supervisor holds open: when the supervisor is gone — killed, its
machine's server stopped — the pipe reads its end, and the worker stops
itself the same way, ask then kill, together with what it started (on
Windows, the worker process alone). So a re-dispatch after a restart never
executes beside workers of the dispatch before it.

Stdlib only.
"""

from __future__ import annotations

import multiprocessing
import os
import pickle
import signal
import sys
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from multiprocessing.connection import wait as wait_for
from pathlib import Path
from typing import Any

__all__ = ["ORPHAN_GRACE_S", "STOP_FILE", "WorkerOutcome", "WorkerPayload", "supervise_workers"]

#: Left in a worker's ``out_dir`` when it is asked to stop.
STOP_FILE = ".stop-requested"

#: How a shard ended: its target returned, raised, the worker process ended
#: without saying either, it ran over its timeout and was stopped, or it was
#: stopped by a cancellation (or because ``on_done`` raised).
FINISHED, RAISED, DIED, TIMED_OUT, STOPPED = "finished", "raised", "died", "timed out", "stopped"
#: Seconds a worker whose supervisor is gone waits for what it asked to stop
#: before it kills its process group: what a supervised worker started is
#: gone at most this long (plus a moment) after the supervising process.
ORPHAN_GRACE_S = 5.0


@dataclass(frozen=True)
class WorkerPayload:
    """What one worker process is told. Picklable: it travels to a spawned
    process — :func:`supervise_workers` refuses one that is not before it
    starts anything.

    - ``shard`` — the name results are delivered under; ``out_dir`` — the
      shard's own directory, created before the worker starts.
    - ``item_ids`` — what the shard executes.
    - ``lane`` — shards with the same lane never run at the same time and
      run in the order given; ``None`` is a lane of its own.
    - ``cwd`` — the worker changes into it before ``target`` is called.
    - ``env`` — set in the worker's environment before ``target`` is called
      (and so inherited by whatever it starts).
    - ``data`` — anything else the target needs, picklable.
    """

    shard: str
    out_dir: str
    item_ids: tuple[str, ...] = ()
    lane: str | None = None
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerOutcome:
    """How one shard ended under :func:`supervise_workers`.

    ``status`` is ``"finished"`` (``result`` is what the target returned),
    ``"raised"`` (``error`` is the exception, with its traceback), ``"died"``
    (the process ended without either — ``exitcode`` says how), ``"timed
    out"`` (stopped by the supervisor; ``result`` is there if the target
    still returned) or ``"stopped"`` (by a cancellation — only ``on_end``
    is told of these). ``pid`` is the worker process's id."""

    payload: WorkerPayload
    status: str
    exitcode: int | None
    seconds: float
    result: Any = None
    error: str | None = None
    pid: int | None = None

    @property
    def timed_out(self) -> bool:
        return self.status == TIMED_OUT

    @property
    def problem(self) -> str | None:
        """Why the shard has no result — ``None`` when its target returned."""
        shard = self.payload.shard
        if self.status == FINISHED:
            return None
        if self.status == RAISED:
            return f"{shard}: {_first_line(self.error, 'the worker raised')}"
        if self.status == DIED:
            return f"{shard}: the worker process ended (exit code {self.exitcode}) without a result"
        if self.status == STOPPED:
            return f"{shard}: stopped before it ended"
        return f"{shard}: exceeded its timeout and was stopped"


def _first_line(text: str | None, default: str) -> str:
    lines = (text or "").splitlines()
    return lines[0] if lines else default


# -- the worker side ---------------------------------------------------------------------


class _StopFlag:
    """``stop()`` in the worker: true once the supervisor asked. The first
    ``SIGTERM`` is that request; a further one ends the worker."""

    def __init__(self, event: Any):
        self._event = event
        self._signals = 0

    def __call__(self) -> bool:
        return self._signals > 0 or bool(self._event.is_set())

    def on_signal(self, _signum: int, _frame: Any) -> None:
        self._signals += 1
        if self._signals > 1:
            raise SystemExit(128 + signal.SIGTERM)


def _watch_supervisor(lifeline: Any, event: Any) -> None:
    """In the worker: wait until the supervisor's end of ``lifeline`` is
    closed — it never sends, so the read returns only when the supervising
    process is gone — then stop this worker and what it started."""
    import threading

    def watch() -> None:
        try:
            lifeline.recv()
        except (EOFError, OSError):
            pass
        except Exception:  # anything else means the supervisor went as well
            pass
        event.set()
        if os.name == "nt":
            os._exit(1)
        try:  # ask, as the supervisor would …
            os.killpg(0, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + ORPHAN_GRACE_S
        while time.monotonic() < deadline:
            time.sleep(0.05)
        try:  # … then kill what is left of the group, this worker included
            os.killpg(0, signal.SIGKILL)
        except OSError:
            os._exit(1)

    threading.Thread(target=watch, name="runcomposer-supervisor-watch", daemon=True).start()


def _worker_main(
    target: Callable[..., Any], payload: WorkerPayload, event: Any, conn: Any, lifeline: Any = None
) -> None:
    """The spawned process: its own session, the payload's working directory
    and environment, then ``target``; the outcome goes back through
    ``conn``. ``lifeline`` is read until the supervisor is gone."""
    stop = _StopFlag(event)
    if os.name != "nt":
        signal.signal(signal.SIGTERM, stop.on_signal)
        try:
            os.setsid()
        except OSError:
            pass
    if lifeline is not None:
        _watch_supervisor(lifeline, event)
    try:
        if payload.cwd:
            os.chdir(payload.cwd)
        os.environ.update({str(key): str(value) for key, value in payload.env.items()})
        value = target(payload, stop)
    except BaseException as exc:
        details = traceback.format_exc()
        sys.stderr.write(f"shard {payload.shard}: {details}")
        _send(conn, (RAISED, f"{type(exc).__name__}: {exc}\n{details}"))
        raise SystemExit(1) from None
    _send(conn, (FINISHED, value))


def _send(conn: Any, message: tuple[str, Any]) -> None:
    try:
        conn.send(message)
    except Exception as exc:  # the result could not be pickled
        try:
            conn.send((RAISED, f"the result of the worker could not be sent back: {exc}"))
        except Exception:
            pass
    finally:
        conn.close()


# -- the supervising side ----------------------------------------------------------------


@dataclass
class _Running:
    process: Any
    payload: WorkerPayload
    event: Any
    conn: Any
    started: float
    #: The supervisor's end of the worker's lifeline: held open, never written.
    lifeline: Any = None
    message: tuple[str, Any] | None = None
    closed: bool = False

    def ended(self) -> bool:
        """Whether the worker process has ended — without reaping it, so
        that its process id (and with it its group's) stays reserved until
        the supervisor joins it."""
        return bool(wait_for([self.process.sentinel], timeout=0))

    def drain(self) -> None:
        """Take what the worker sent, if it sent something."""
        if self.closed:
            return
        try:
            while self.conn.poll():
                self.message = self.conn.recv()
        except (EOFError, OSError):
            self.conn.close()
            self.closed = True

    def outcome(self, status: str | None = None) -> WorkerOutcome:
        self.drain()
        seconds = time.monotonic() - self.started
        kind, value = self.message if self.message is not None else (DIED, None)
        if status is None:
            status = kind
        return WorkerOutcome(
            payload=self.payload,
            status=status,
            exitcode=self.process.exitcode,
            seconds=seconds,
            result=value if kind == FINISHED else None,
            error=value if kind == RAISED else None,
            pid=self.process.pid,
        )

    def release(self) -> None:
        """The worker is reaped: its lifeline is no longer needed."""
        if self.lifeline is not None:
            try:
                self.lifeline.close()
            except OSError:
                pass
            self.lifeline = None


def supervise_workers(
    payloads: Sequence[WorkerPayload],
    target: Callable[[WorkerPayload, Callable[[], bool]], Any],
    *,
    max_workers: int = 1,
    on_done: Callable[[WorkerPayload, WorkerOutcome], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    timeout_s: float | None = None,
    grace_s: float = 5.0,
    log: Callable[[str, str], None] | None = None,
    poll_s: float = 0.05,
    on_start: Callable[[WorkerPayload, int], None] | None = None,
    on_end: Callable[[WorkerPayload, int, WorkerOutcome], None] | None = None,
) -> list[WorkerOutcome]:
    """Run each payload in a spawned worker process as ``target(payload,
    stop)``, at most ``max_workers`` at once, and return how each ended, in
    the order they ended.

    - ``target`` is a module-level callable; ``stop()`` says the worker was
      asked to stop — poll it, and end what you are doing when it is true.
      What it returns (picklable) is :attr:`WorkerOutcome.result`.
    - ``on_done(payload, outcome)`` is called in the calling thread for each
      shard as it ends. An exception from it stops every running worker and
      propagates.
    - ``cancelled()`` is polled every ``poll_s``: once true, no further shard
      starts and running workers are stopped (the stop protocol, above).
      Stopped shards are not reported to ``on_done`` and are not in the
      result; shards that never started are not either. Pass
      ``lambda: ctx.cancelled`` from a dispatch.
    - ``timeout_s`` bounds one shard, counted from its worker's start; a
      shard over it is stopped and reported with status ``"timed out"``.
    - ``log(message, level)`` is told of each shard's start, and of its end
      (shard, lane, how it ended, seconds) — pass ``ctx.log``,
      or the ``log`` of a live channel.
    - ``on_start(payload, pid)`` is called as each worker process has
      started, ``on_end(payload, pid, outcome)`` once it has been reaped —
      every started worker, a stopped one included (status ``"stopped"``),
      before ``on_done`` for those it reports. They say which processes
      the supervisor started; an exception from them is logged and
      otherwise ignored.

    Every worker watches the supervising process and stops itself, with
    what it started, when that process is gone (see the module docstring).

    A payload or target that cannot be sent to a worker process is refused
    with ``TypeError`` before anything starts.
    """
    payloads = list(payloads)
    _refuse_what_cannot_travel(target, payloads)
    spawn = multiprocessing.get_context("spawn")
    bound = max(1, int(max_workers))
    pending = list(payloads)
    running: list[_Running] = []
    outcomes: list[WorkerOutcome] = []

    def say(message: str, level: str = "info") -> None:
        if log is not None:
            try:
                log(message, level)
            except Exception:
                pass

    def tell(callback: Callable[..., None] | None, *args: Any) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as exc:
            say(f"on_start/on_end raised {type(exc).__name__}: {exc}", "warning")

    def ended(entry: _Running, outcome: WorkerOutcome) -> None:
        entry.release()
        say(f"{_named(entry.payload)}: {_how(outcome, timeout_s)} after {outcome.seconds:.1f}s",
            "info" if outcome.status == FINISHED else "warning")
        tell(on_end, entry.payload, entry.process.pid, outcome)
        outcomes.append(outcome)
        if on_done is not None:
            on_done(entry.payload, outcome)

    try:
        while pending or running:
            if cancelled is not None and cancelled():
                break
            busy = {entry.payload.lane for entry in running if entry.payload.lane is not None}
            index = 0
            while index < len(pending) and len(running) < bound:
                payload = pending[index]
                if payload.lane is not None and payload.lane in busy:
                    index += 1
                    continue
                pending.pop(index)
                started = _start(spawn, target, payload)
                running.append(started)
                say(f"{_named(payload)}: started")
                tell(on_start, payload, started.process.pid)
                if payload.lane is not None:
                    busy.add(payload.lane)
            handles = [entry.process.sentinel for entry in running]
            handles += [entry.conn for entry in running if not entry.closed]
            wait_for(handles, timeout=poll_s)
            now = time.monotonic()
            finished, overrun, still = [], [], []
            for entry in running:
                entry.drain()
                if entry.ended():
                    finished.append(entry)
                elif timeout_s is not None and now - entry.started > timeout_s:
                    overrun.append(entry)
                else:
                    still.append(entry)
            running = still + overrun  # stopped below; still owned by the finally until then
            for entry in finished:
                entry.process.join()  # reaped by the supervisor itself
            for entry in finished:
                ended(entry, entry.outcome())
            if overrun:
                _stop(overrun, grace_s)
                running = still
                for entry in overrun:
                    ended(entry, entry.outcome(TIMED_OUT))
    finally:
        if running:
            _stop(running, grace_s)
            for entry in running:
                entry.release()
                say(f"{_named(entry.payload)}: stopped after {time.monotonic() - entry.started:.1f}s")
                tell(on_end, entry.payload, entry.process.pid, entry.outcome(STOPPED))
    return outcomes


def _refuse_what_cannot_travel(target: Callable[..., Any], payloads: Sequence[WorkerPayload]) -> None:
    try:
        pickle.dumps(target)
    except Exception as exc:
        raise TypeError(
            f"the worker target {target!r} cannot be sent to a worker process — it must be a "
            f"module-level callable: {exc}"
        ) from None
    for payload in payloads:
        try:
            pickle.dumps(payload)
        except Exception as exc:
            raise TypeError(
                f"the payload of shard {payload.shard!r} cannot be sent to a worker process: {exc}"
            ) from None


def _start(spawn: Any, target: Callable[..., Any], payload: WorkerPayload) -> _Running:
    Path(payload.out_dir).mkdir(parents=True, exist_ok=True)
    try:
        Path(payload.out_dir, STOP_FILE).unlink(missing_ok=True)
    except OSError:
        pass
    event = spawn.Event()
    receive, send = spawn.Pipe(duplex=False)
    # The lifeline: the worker reads one end, this process holds the other
    # open and never writes — the worker's read returns when this process
    # is gone.
    watched, held = spawn.Pipe(duplex=False)
    process = spawn.Process(
        target=_worker_main,
        args=(target, payload, event, send, watched),
        name=f"runcomposer-shard-{payload.shard}",
    )
    process.start()
    send.close()  # the worker holds its end; EOF here means it is gone
    watched.close()
    return _Running(process, payload, event, receive, time.monotonic(), lifeline=held)


def _named(payload: WorkerPayload) -> str:
    return f"shard {payload.shard}" + (f" (lane {payload.lane})" if payload.lane is not None else "")


def _how(outcome: WorkerOutcome, timeout_s: float | None) -> str:
    if outcome.status == FINISHED:
        return "finished"
    if outcome.status == RAISED:
        return "raised " + _first_line(outcome.error, "an exception")
    if outcome.status == DIED:
        return f"ended without a result (exit code {outcome.exitcode})"
    if outcome.status == STOPPED:
        return "was stopped"
    return f"exceeded the timeout of {timeout_s:g}s and was stopped"


# -- the stop protocol ---------------------------------------------------------------------


def _signal(entry: _Running, signum: int) -> None:
    """``signum`` to the worker's process group — or to the worker alone
    while it has no group of its own yet (it is still starting), and on
    Windows."""
    process = entry.process
    if os.name != "nt":
        try:
            os.killpg(process.pid, signum)
            return
        except ProcessLookupError:
            pass
        except PermissionError:
            return
    if not entry.ended():
        try:
            if signum == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except (ProcessLookupError, OSError):
            pass


def _wait(entries: Sequence[_Running], grace_s: float) -> None:
    deadline = time.monotonic() + grace_s
    while True:
        alive = [entry for entry in entries if not entry.ended()]
        for entry in entries:
            entry.drain()
        if not alive or time.monotonic() >= deadline:
            return
        wait_for([entry.process.sentinel for entry in alive], timeout=min(0.05, grace_s))


def _stop(entries: Sequence[_Running], grace_s: float) -> None:
    """Ask, terminate, kill — every worker in ``entries`` at the same time —
    then kill what is left of their process groups and reap them."""
    for entry in entries:
        entry.event.set()
        try:
            Path(entry.payload.out_dir, STOP_FILE).touch()
        except OSError:
            pass
        if os.name != "nt":
            _signal(entry, signal.SIGTERM)
    _wait(entries, grace_s)
    for entry in entries:
        if not entry.ended():
            _signal(entry, signal.SIGTERM)
    _wait(entries, grace_s)
    kill = signal.SIGTERM if os.name == "nt" else signal.SIGKILL
    for entry in entries:
        if not entry.ended():
            _signal(entry, kill)
    _wait(entries, grace_s)
    for entry in entries:
        if os.name != "nt" and entry.ended():
            # What the worker started and left behind. The worker is not
            # reaped yet (``ended`` does not reap), so its group id cannot
            # have passed to another process.
            try:
                os.killpg(entry.process.pid, kill)
            except (ProcessLookupError, PermissionError):
                pass
        entry.process.join(timeout=grace_s)
        entry.drain()
