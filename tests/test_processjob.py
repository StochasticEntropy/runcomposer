"""The decisions around Windows job objects (``runcomposer.core.processjob``
and the supervisor's stop protocol on Windows), with a stand-in for the
Win32 calls — so that they are covered on every platform. What real job
objects do is proved by the process tests (tests/test_worker_supervisor.py,
tests/test_command_runner.py, tests/test_robot_pool.py) in the Windows job.
"""

from __future__ import annotations

import multiprocessing
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from runcomposer.core import processjob, supervise
from runcomposer.core.processjob import CREATE_SUSPENDED, ProcessJob
from runcomposer.core.supervise import STOP_FILE, WorkerPayload


class FakeKernel:
    """Records every call; ``refuse`` makes assignments fail with that error."""

    def __init__(self, *, create: bool = True, refuse: int = 0, gone: frozenset[int] = frozenset()):
        self.calls: list[tuple[Any, ...]] = []
        self.create, self.refuse, self.gone = create, refuse, gone
        self.next_handle = 100

    def create_job(self, kill_on_close: bool) -> int | None:
        self.calls.append(("create", kill_on_close))
        if not self.create:
            return None
        self.next_handle += 1
        return self.next_handle

    def assign(self, job: int, pid: int) -> int:
        self.calls.append(("assign", job, pid))
        if pid in self.gone:
            return 5  # what assigning a process that has ended answers
        return self.refuse

    def alive(self, pid: int) -> bool:
        self.calls.append(("alive", pid))
        return pid not in self.gone

    def terminate(self, job: int, exit_code: int) -> bool:
        self.calls.append(("terminate", job, exit_code))
        return True

    def duplicate_into(self, job: int, pid: int) -> int | None:
        self.calls.append(("duplicate", job, pid))
        return 7000 + job

    def resume(self, pid: int) -> bool:
        self.calls.append(("resume", pid))
        return True

    def close(self, handle: int) -> None:
        self.calls.append(("close", handle))

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


@pytest.fixture(autouse=True)
def fresh_warning():
    processjob._said.clear()
    yield
    processjob._said.clear()


class TestAJob:
    def test_an_assigned_process_is_terminated_with_its_job_and_the_handle_closed_once(self):
        kernel = FakeKernel()
        job = ProcessJob(kill_on_close=True, kernel=kernel)
        assert job.add(4242) and job.holds
        assert job.share_with(5151) == 7000 + job.handle
        assert job.terminate(3)
        handle = job.handle
        job.close()
        job.close()
        assert not job.terminate(), "a closed job ends nothing"
        assert kernel.calls == [
            ("create", True), ("assign", handle, 4242), ("duplicate", handle, 5151),
            ("terminate", handle, 3), ("close", handle),
        ]

    def test_a_refused_assignment_goes_on_without_the_job_and_is_said_once(self):
        kernel, said = FakeKernel(refuse=5), []
        first = ProcessJob(kill_on_close=True, kernel=kernel, say=said.append)
        second = ProcessJob(kill_on_close=False, kernel=kernel, say=said.append)
        assert not first.add(1) and not second.add(2)
        assert first.share_with(9) is None and not first.terminate() and not second.terminate()
        assert len(said) == 1 and "Windows error 5" in said[0] and "not stopped with it" in said[0]
        assert "terminate" not in kernel.names() and "duplicate" not in kernel.names()

    def test_a_process_gone_before_it_is_assigned_is_no_refusal_and_a_later_one_still_warns(self):
        """A worker that ended at once cannot be assigned. That is nobody's
        refusal: nothing is said, and the one warning is left for a real
        refusal later — which says Windows' error code."""
        kernel, said = FakeKernel(refuse=5, gone=frozenset({11})), []
        assert not ProcessJob(kill_on_close=True, kernel=kernel, say=said.append).add(11)
        assert said == []
        assert not ProcessJob(kill_on_close=True, kernel=kernel, say=said.append).add(12)
        assert len(said) == 1 and "Windows error 5" in said[0]

    def test_a_process_id_no_process_has_is_no_refusal(self):
        kernel, said = FakeKernel(refuse=87), []
        assert not ProcessJob(kill_on_close=True, kernel=kernel, say=said.append).add(13)
        assert said == [] and "alive" not in kernel.names()

    def test_a_job_that_cannot_be_made_is_said_apart_from_a_refusal(self):
        said: list[str] = []
        ProcessJob(kill_on_close=True, kernel=FakeKernel(create=False), say=said.append)
        ProcessJob(kill_on_close=True, kernel=FakeKernel(refuse=5), say=said.append).add(1)
        assert len(said) == 2 and "could not be created" in said[0] and "Windows error 5" in said[1]

    def test_a_job_that_cannot_be_made_does_nothing_and_is_said_once(self):
        kernel, said = FakeKernel(create=False), []
        job = ProcessJob(kill_on_close=True, kernel=kernel, say=said.append)
        assert job.handle is None and not job.add(1) and not job.terminate()
        job.close()
        assert kernel.names() == ["create"] and len(said) == 1

    def test_an_adopted_handle_terminates_the_job_it_names(self):
        kernel = FakeKernel()
        job = ProcessJob.adopt(321, kernel=kernel)
        assert job.terminate(1)
        assert kernel.calls == [("terminate", 321, 1)]


class TestStartingInAJob:
    """``ProcessJob.start``: suspended, assigned, then resumed — whatever the
    assignment says — so nothing the process starts is started outside."""

    def popen(self, monkeypatch, order: list[str], fail: bool = False) -> list[dict[str, Any]]:
        seen: list[dict[str, Any]] = []

        class Started:
            def __init__(self, args: Any, **options: Any):
                order.append("popen")
                if fail:
                    raise OSError("no such program")
                seen.append({"args": args, **options})
                self.pid = 777

        monkeypatch.setattr(processjob.subprocess, "Popen", Started)
        return seen

    @pytest.mark.parametrize("refuse", [0, 5], ids=["assigned", "refused"])
    def test_suspended_then_assigned_then_resumed(self, monkeypatch, refuse):
        order: list[str] = []
        kernel = FakeKernel(refuse=refuse)
        seen = self.popen(monkeypatch, order)
        process, job = ProcessJob.start(["hook"], kernel=kernel, creationflags=0x200, shell=True)
        assert seen[0]["creationflags"] == 0x200 | CREATE_SUSPENDED and seen[0]["shell"] is True
        assert [n for n in kernel.names() if n in ("assign", "resume")] == ["assign", "resume"]
        assert kernel.calls[-1] == ("resume", 777) and process.pid == 777
        assert job.holds == (refuse == 0)

    def test_a_process_that_cannot_be_resumed_is_ended_not_left_waiting(self, monkeypatch):
        kernel, ended = FakeKernel(), []
        kernel.resume = lambda pid: kernel.calls.append(("resume", pid)) and False  # type: ignore[method-assign]
        self.popen(monkeypatch, [])
        monkeypatch.setattr(processjob.subprocess.Popen, "kill", lambda self: ended.append("kill"), raising=False)
        monkeypatch.setattr(processjob.subprocess.Popen, "wait", lambda self: ended.append("wait"), raising=False)
        with pytest.raises(OSError, match="could not be resumed"):
            ProcessJob.start("hook", kernel=kernel)
        assert ended == ["kill", "wait"] and kernel.names()[-1] == "close"

    @pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError], ids=["interrupt", "error"])
    def test_anything_raised_on_the_way_ends_the_process_and_closes_the_job(self, monkeypatch, failure):
        """Between the suspended start and the resume nothing may be left
        behind: an interrupt while a hook is being started included."""
        kernel, ended = FakeKernel(), []

        def raising_assign(job: int, pid: int) -> int:
            kernel.calls.append(("assign", job, pid))
            raise failure("while assigning")

        kernel.assign = raising_assign  # type: ignore[method-assign]
        self.popen(monkeypatch, [])
        monkeypatch.setattr(processjob.subprocess.Popen, "kill", lambda self: ended.append("kill"), raising=False)
        monkeypatch.setattr(processjob.subprocess.Popen, "wait", lambda self: ended.append("wait"), raising=False)
        with pytest.raises(failure, match="while assigning"):
            ProcessJob.start("hook", kernel=kernel)
        assert ended == ["kill", "wait"]
        assert kernel.names()[-1] == "close" and "resume" not in kernel.names()

    def test_a_process_that_cannot_start_leaves_no_job_open(self, monkeypatch):
        kernel = FakeKernel()
        self.popen(monkeypatch, [], fail=True)
        with pytest.raises(OSError):
            ProcessJob.start("hook", kernel=kernel)
        assert kernel.names() == ["create", "close"]


# -- the supervisor's stop protocol on Windows -------------------------------------------


class FakeProcess:
    """A worker process: ``sentinel`` becomes ready when it has ended — a
    pipe's receiving end, which ``multiprocessing``'s ``wait`` waits on on
    every platform (a plain descriptor is no handle on Windows)."""

    def __init__(self) -> None:
        self.sentinel, self._write = multiprocessing.Pipe(duplex=False)
        self.pid = 4040
        self.exitcode: int | None = None
        self.terminated = 0

    def end(self) -> None:
        if self.exitcode is None:
            self.exitcode = 1
            self._write.send_bytes(b"x")

    def terminate(self) -> None:
        self.terminated += 1
        self.end()

    kill = terminate

    def join(self, timeout: float | None = None) -> None:
        pass


class FakeJob:
    def __init__(self, process: FakeProcess, journal: list[str]):
        self.process, self.journal = process, journal
        self.closed = False

    def terminate(self, exit_code: int = 1) -> bool:
        self.journal.append("job terminated")
        self.process.end()
        return True

    def close(self) -> None:
        self.closed = True


class Flag:
    """The stop flag: says whether the stop file was there when it was raised."""

    def __init__(self, out_dir: Path, journal: list[str], on_set: Any = None):
        self.out_dir, self.journal, self.on_set = out_dir, journal, on_set
        self._set = threading.Event()

    def set(self) -> None:
        self.journal.append("flag" + (" after the file" if (self.out_dir / STOP_FILE).exists() else ""))
        self._set.set()
        if self.on_set is not None:
            self.on_set()

    def is_set(self) -> bool:
        return self._set.is_set()


def windows_entry(tmp_path: Path, journal: list[str], *, stops_on_request: bool) -> Any:
    process = FakeProcess()
    receive, send = multiprocessing.Pipe(duplex=False)
    send.close()
    out_dir = tmp_path / "1"
    out_dir.mkdir()
    flag = Flag(out_dir, journal, on_set=process.end if stops_on_request else None)
    entry = supervise._Running(process, WorkerPayload(shard="1", out_dir=str(out_dir)), flag, receive,
                               time.monotonic())
    entry.job = FakeJob(process, journal)
    return entry


class TestWindowsStop:
    def test_the_grace_comes_first_then_the_job_is_terminated_and_finally_what_is_left(
        self, tmp_path, monkeypatch
    ):
        """A worker that does not stop on the request: file, flag, the whole
        grace period, then its job — and at the end the job once more, for
        what the worker left behind. The worker is never signalled alone."""
        monkeypatch.setattr(supervise, "_windows", lambda: True)
        journal: list[str] = []
        entry = windows_entry(tmp_path, journal, stops_on_request=False)
        started = time.monotonic()
        supervise._stop([entry], grace_s=0.3)
        assert journal == ["flag after the file", "job terminated", "job terminated"]
        assert time.monotonic() - started >= 0.3, "the grace period was given before the job was terminated"
        assert entry.process.terminated == 0

    def test_a_worker_that_stops_in_its_grace_is_not_terminated_but_its_leftovers_are(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(supervise, "_windows", lambda: True)
        journal: list[str] = []
        entry = windows_entry(tmp_path, journal, stops_on_request=True)
        supervise._stop([entry], grace_s=5)
        assert journal == ["flag after the file", "job terminated"]

    def test_without_a_job_the_worker_alone_is_terminated(self, tmp_path, monkeypatch):
        """Where Windows refused the job: as before, the worker process."""
        monkeypatch.setattr(supervise, "_windows", lambda: True)
        journal: list[str] = []
        entry = windows_entry(tmp_path, journal, stops_on_request=False)
        entry.job = None
        supervise._stop([entry], grace_s=0.1)
        assert journal == ["flag after the file"] and entry.process.terminated == 1

    def test_reaping_closes_the_job(self, tmp_path):
        journal: list[str] = []
        entry = windows_entry(tmp_path, journal, stops_on_request=True)
        job = entry.job
        entry.release()
        assert job.closed and entry.job is None


class TestAJobForAWorker:
    def test_a_refused_job_leaves_the_worker_without_one_and_says_so(self, monkeypatch):
        kernel, said = FakeKernel(refuse=5), []
        monkeypatch.setattr(processjob, "_win32", lambda: kernel)
        assert supervise._job_for(31, said.append) is None
        assert kernel.names() == ["create", "assign", "alive", "close"] and len(said) == 1

    def test_a_job_ends_its_processes_when_its_last_handle_closes(self, monkeypatch):
        kernel = FakeKernel()
        monkeypatch.setattr(processjob, "_win32", lambda: kernel)
        job = supervise._job_for(31, lambda message: None)
        assert job is not None and job.holds
        assert kernel.calls[0] == ("create", True)
