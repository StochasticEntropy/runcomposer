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
process.

Stdlib only.
"""

from __future__ import annotations

import logging
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

    def close(self, handle: int) -> None: ...


_refusal_said = threading.Event()


def warn_once(message: str, say: Callable[[str], None] | None = None) -> None:
    """``message`` as a warning — once per process, however many jobs are
    refused."""
    if _refusal_said.is_set():
        return
    _refusal_said.set()
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
            warn_once("a job object could not be created: what processes start is not "
                      "stopped with them on this machine", say)
        #: Whether a process was assigned — what :meth:`terminate` can reach.
        self.holds = False

    def add(self, pid: int) -> bool:
        """Put process ``pid`` (and so everything it starts from now on)
        into the job. False, said once, when Windows refuses."""
        if self.handle is None:
            return False
        error = self._kernel.assign(self.handle, pid)
        if error:
            warn_once(
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
        starts is started before it is in the job. Windows only."""
        job = cls(kill_on_close=kill_on_close, kernel=kernel, say=say)
        flags = int(popen.pop("creationflags", 0)) | CREATE_SUSPENDED
        try:
            process = subprocess.Popen(args, creationflags=flags, **popen)
        except BaseException:
            job.close()
            raise
        try:
            job.add(process.pid)
        finally:
            resumed = job._kernel.resume(process.pid)
        if not resumed:  # never leave a process waiting for a resume that cannot come
            process.kill()
            process.wait()
            job.close()
            raise OSError(f"the process {process.pid} was started suspended and could not be resumed")
        return process, job


def _win32() -> Kernel:
    """The real calls, made ready once per process."""
    global _KERNEL
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
