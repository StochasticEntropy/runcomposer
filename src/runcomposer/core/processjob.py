"""A process and everything it starts, stopped together on Windows.

POSIX reaches what a process started through its process group: a signal to
the group reaches every member. Windows has no process groups; it has job
objects. A process assigned to a job takes every process it starts into the
same job (nested jobs: Windows 8 and later), and terminating the job ends
them all; with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` the job also ends them
when its last handle is closed — a holder that dies, dies with its handle.

:class:`ProcessJob` is that job, through ``ctypes`` and the Win32 calls
``CreateJobObjectW``, ``SetInformationJobObject``,
``AssignProcessToJobObject``, ``TerminateJobObject`` and ``DuplicateHandle``.
Every Win32 call goes through one small object (:class:`Kernel`), so that
the decisions above it are tested with a stand-in on every platform.

The handle is never inheritable (``CreateJobObjectW`` without security
attributes): a child that held it would keep the job alive after its
holder closed it.

A job created on a machine that refuses it — an assignment refused because
the process is already in a job that allows no nesting — is not an error:
the process runs without it, and :func:`warn_once` says so once per
process, with Windows' error code. A process that is gone before it could
be assigned (a worker that ended at once) is no refusal and is not said.

Stdlib only.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections.abc import Callable, Sequence
from typing import Any, Protocol

__all__ = ["Kernel", "ProcessJob", "warn_once"]

log = logging.getLogger("runcomposer.supervise")

#: ``JOBOBJECT_EXTENDED_LIMIT_INFORMATION``'s information class.
_EXTENDED_LIMIT_INFORMATION = 9
#: End every process of the job when its last handle is closed.
_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_TERMINATE = 0x0001
_PROCESS_DUP_HANDLE = 0x0040
_PROCESS_SET_QUOTA = 0x0100
_THREAD_SUSPEND_RESUME = 0x0002
_DUPLICATE_SAME_ACCESS = 0x2
_SNAPSHOT_THREADS = 0x4
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
#: What ``OpenProcess`` says for a process id no process has.
_ERROR_INVALID_PARAMETER = 87
#: ``CreateProcess`` flag: the first thread waits until resumed.
CREATE_SUSPENDED = 0x4


class Kernel(Protocol):
    """The Win32 calls a :class:`ProcessJob` makes. ``None`` / ``False`` /
    an error code where Win32 fails; never an exception."""

    def create_job(self, kill_on_close: bool) -> int | None: ...

    def assign(self, job: int, pid: int) -> int:
        """0 when ``pid`` is in ``job`` now, else the Win32 error code."""
        ...

    def terminate(self, job: int, exit_code: int) -> bool: ...

    def duplicate_into(self, job: int, pid: int) -> int | None:
        """A handle to ``job`` valid in process ``pid`` (not inheritable)."""
        ...

    def resume(self, pid: int) -> bool:
        """Resume every thread of ``pid`` (started suspended)."""
        ...

    def alive(self, pid: int) -> bool:
        """Whether process ``pid`` still runs (it can be opened and has no
        exit code yet)."""
        ...

    def close(self, handle: int) -> None: ...


_said: set[str] = set()
_said_lock = threading.Lock()


def warn_once(kind: str, message: str, say: Callable[[str], None] | None = None) -> None:
    """``message`` as a warning — once per process for each ``kind`` (a job
    that cannot be made, an assignment Windows refuses), however often it
    happens."""
    with _said_lock:
        if kind in _said:
            return
        _said.add(kind)
    (say or log.warning)(message)


class ProcessJob:
    """One job object. ``kill_on_close``: the job ends its processes when its
    last handle closes. Every method is safe to call after a failure: a job
    that could not be created or assigned does nothing."""

    def __init__(self, *, kill_on_close: bool, kernel: Kernel | None = None,
                 say: Callable[[str], None] | None = None):
        self._kernel = kernel if kernel is not None else _win32()
        self._say = say
        self._lock = threading.Lock()
        self.handle: int | None = self._kernel.create_job(kill_on_close)
        if self.handle is None:
            warn_once("create", "a job object could not be created: what processes start is not "
                      "stopped with them on this machine", say)
        #: Whether a process was assigned — what :meth:`terminate` can reach.
        self.holds = False

    def add(self, pid: int) -> bool:
        """Put process ``pid`` (and so everything it starts from now on)
        into the job. False when it cannot be: said once, with Windows' error
        code, when Windows refuses a process that runs — not when the
        process is simply gone already (a worker that ended before it was
        assigned), which is nobody's refusal and leaves the one warning for
        a real one."""
        if self.handle is None:
            return False
        error = self._kernel.assign(self.handle, pid)
        if error:
            if error == _ERROR_INVALID_PARAMETER or not self._kernel.alive(pid):
                return False  # no such process (any more): nothing to stop, nothing to say
            warn_once(
                "refused",
                f"a process could not be put into a job object (Windows error {error}; the process "
                "may already be in a job that allows no other): what it starts is not stopped "
                "with it", self._say,
            )
            return False
        self.holds = True
        return True

    def share_with(self, pid: int) -> int | None:
        """A handle to the job for process ``pid`` to hold — so that the job
        outlives this process while ``pid`` lives."""
        if self.handle is None or not self.holds:
            return None
        return self._kernel.duplicate_into(self.handle, pid)

    def terminate(self, exit_code: int = 1) -> bool:
        """End every process in the job. False when there is none to end."""
        with self._lock:
            if self.handle is None or not self.holds:
                return False
            return self._kernel.terminate(self.handle, exit_code)

    def close(self) -> None:
        """Close this process's handle (with ``kill_on_close``, the last
        handle closing ends the job's processes)."""
        with self._lock:
            if self.handle is not None:
                self._kernel.close(self.handle)
                self.handle = None

    @classmethod
    def adopt(cls, handle: int, *, kernel: Kernel | None = None) -> ProcessJob:
        """The job behind ``handle`` — one this process was given
        (:meth:`share_with`), which it is in itself."""
        job = cls.__new__(cls)
        job._kernel = kernel if kernel is not None else _win32()
        job._say = None
        job._lock = threading.Lock()
        job.handle = handle
        job.holds = True
        return job

    @classmethod
    def start(cls, args: str | Sequence[str], *, kill_on_close: bool = False,
              kernel: Kernel | None = None, say: Callable[[str], None] | None = None,
              **popen: Any) -> tuple[subprocess.Popen[Any], ProcessJob]:
        """``subprocess.Popen(args, **popen)`` in a job of its own: started
        suspended, put into the job, then resumed — so that nothing it
        starts is started before it is in the job. Windows only.

        Whatever goes wrong on the way — an exception, an interrupt
        included — the process is killed and reaped and the job closed
        before the exception goes on: nothing is left suspended, or running
        outside its job."""
        job = cls(kill_on_close=kill_on_close, kernel=kernel, say=say)
        flags = int(popen.pop("creationflags", 0)) | CREATE_SUSPENDED
        try:
            process = subprocess.Popen(args, creationflags=flags, **popen)
        except BaseException:
            job.close()
            raise
        try:
            job.add(process.pid)
            if not job._kernel.resume(process.pid):
                raise OSError(f"the process {process.pid} was started suspended and could not be resumed")
        except BaseException:
            try:
                process.kill()
                process.wait()
            finally:
                job.close()
            raise
        return process, job


def _win32() -> Kernel:
    """The real calls, made ready once per process. Off Windows there are
    none: a job object is a Windows thing, and asking for one is an
    ``OSError`` that says what to use instead."""
    global _KERNEL
    if os.name != "nt":
        raise OSError(
            "a job object exists on Windows only; on this platform start the process with "
            "start_new_session=True and stop it and what it started with os.killpg"
        )
    if _KERNEL is None:
        _KERNEL = _Win32Kernel()
    return _KERNEL


_KERNEL: Kernel | None = None


class _Win32Kernel:
    """The real calls (Windows only)."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        handle, dword, boolean = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
        k.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        k.CreateJobObjectW.restype = handle
        k.SetInformationJobObject.argtypes = (handle, ctypes.c_int, wintypes.LPVOID, dword)
        k.SetInformationJobObject.restype = boolean
        k.AssignProcessToJobObject.argtypes = (handle, handle)
        k.AssignProcessToJobObject.restype = boolean
        k.TerminateJobObject.argtypes = (handle, wintypes.UINT)
        k.TerminateJobObject.restype = boolean
        k.OpenProcess.argtypes = (dword, boolean, dword)
        k.OpenProcess.restype = handle
        k.GetCurrentProcess.argtypes = ()
        k.GetCurrentProcess.restype = handle
        k.DuplicateHandle.argtypes = (handle, handle, handle, ctypes.POINTER(handle), dword, boolean, dword)
        k.DuplicateHandle.restype = boolean
        k.CloseHandle.argtypes = (handle,)
        k.CloseHandle.restype = boolean
        k.CreateToolhelp32Snapshot.argtypes = (dword, dword)
        k.CreateToolhelp32Snapshot.restype = handle
        k.Thread32First.argtypes = (handle, ctypes.POINTER(_thread_entry()))
        k.Thread32First.restype = boolean
        k.Thread32Next.argtypes = (handle, ctypes.POINTER(_thread_entry()))
        k.Thread32Next.restype = boolean
        k.OpenThread.argtypes = (dword, boolean, dword)
        k.OpenThread.restype = handle
        k.ResumeThread.argtypes = (handle,)
        k.ResumeThread.restype = dword
        k.GetExitCodeProcess.argtypes = (handle, ctypes.POINTER(dword))
        k.GetExitCodeProcess.restype = boolean
        self._k = k

    def create_job(self, kill_on_close: bool) -> int | None:
        job = self._k.CreateJobObjectW(None, None)
        if not job:
            return None
        if kill_on_close:
            info = _extended_limit_information()()
            info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
            size = self._ctypes.sizeof(info)
            if not self._k.SetInformationJobObject(job, _EXTENDED_LIMIT_INFORMATION, self._ctypes.byref(info), size):
                self._k.CloseHandle(job)
                return None
        return int(job)

    def assign(self, job: int, pid: int) -> int:
        process = self._k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            return int(self._ctypes.get_last_error()) or 1  # type: ignore[attr-defined]
        try:
            if self._k.AssignProcessToJobObject(job, process):
                return 0
            return int(self._ctypes.get_last_error()) or 1  # type: ignore[attr-defined]
        finally:
            self._k.CloseHandle(process)

    def terminate(self, job: int, exit_code: int) -> bool:
        return bool(self._k.TerminateJobObject(job, exit_code))

    def duplicate_into(self, job: int, pid: int) -> int | None:
        from ctypes import wintypes

        target = self._k.OpenProcess(_PROCESS_DUP_HANDLE, False, pid)
        if not target:
            return None
        try:
            copy = wintypes.HANDLE()
            ok = self._k.DuplicateHandle(self._k.GetCurrentProcess(), job, target, self._ctypes.byref(copy),
                                         0, False, _DUPLICATE_SAME_ACCESS)
            return int(copy.value) if ok and copy.value else None
        finally:
            self._k.CloseHandle(target)

    def resume(self, pid: int) -> bool:
        entry_type = _thread_entry()
        snapshot = self._k.CreateToolhelp32Snapshot(_SNAPSHOT_THREADS, 0)
        if not snapshot or snapshot in _INVALID_HANDLES:
            return False
        resumed = False
        try:
            entry = entry_type()
            entry.dwSize = self._ctypes.sizeof(entry)
            more = self._k.Thread32First(snapshot, self._ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    thread = self._k.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                    if thread:
                        resumed = self._k.ResumeThread(thread) != 0xFFFFFFFF or resumed
                        self._k.CloseHandle(thread)
                more = self._k.Thread32Next(snapshot, self._ctypes.byref(entry))
        finally:
            self._k.CloseHandle(snapshot)
        return resumed

    def alive(self, pid: int) -> bool:
        from ctypes import wintypes

        process = self._k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return False
        try:
            code = wintypes.DWORD()
            if not self._k.GetExitCodeProcess(process, self._ctypes.byref(code)):
                return True  # it exists; its state cannot be read
            return code.value == _STILL_ACTIVE
        finally:
            self._k.CloseHandle(process)

    def close(self, handle: int) -> None:
        self._k.CloseHandle(handle)


#: ``INVALID_HANDLE_VALUE`` as a ``HANDLE`` reads back, 64-bit and 32-bit.
_INVALID_HANDLES = ((1 << 64) - 1, (1 << 32) - 1, -1)


def _thread_entry() -> Any:
    """``THREADENTRY32``, made once."""
    global _THREAD_ENTRY
    if _THREAD_ENTRY is None:
        import ctypes
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD),
            ]

        _THREAD_ENTRY = ThreadEntry
    return _THREAD_ENTRY


def _extended_limit_information() -> Any:
    """``JOBOBJECT_EXTENDED_LIMIT_INFORMATION``, made once."""
    global _EXTENDED
    if _EXTENDED is None:
        import ctypes
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class Counts(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC), ("IoInfo", Counts),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        _EXTENDED = EXTENDED
    return _EXTENDED


_THREAD_ENTRY: Any = None
_EXTENDED: Any = None
