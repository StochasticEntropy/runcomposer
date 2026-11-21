"""Who executes a dispatch, and whether that process is still alive
(DESIGN.md §4, recovery).

A dispatch names its owner — ``host:pid:token`` — for as long as a runner
executes it. Recovery (``app.recovery``) and ``cancel`` (``app.dispatching``,
``cli.runs``) ask :func:`owner_alive` whether that owner can still finish it.
"""

from __future__ import annotations

import os
import socket

from runcomposer.core.ids import new_ulid

__all__ = ["LIVE_OWNERS", "owner_alive", "owner_identity", "parse_owner", "pid_alive"]


#: Owner identities of the dispatch managers alive in this process — added
#: and removed by ``DispatchManager``.
LIVE_OWNERS: set[str] = set()


def owner_identity() -> str:
    """Who is executing a dispatch: ``host:pid:token``. The token tells two
    lives of one pid apart."""
    return f"{socket.gethostname()}:{os.getpid()}:{new_ulid()}"


def parse_owner(owner: str) -> tuple[str, int]:
    host, _, _token = owner.rpartition(":")
    host, _, pid = host.rpartition(":")
    try:
        return host, int(pid)
    except ValueError:
        return owner, -1


def owner_alive(owner: str, me: str | None = None) -> bool:
    """Is the process that owner identity names still executing?

    - On another host: no. One instance owns a store (DESIGN.md §13), so an
      owner from another host is an earlier incarnation of this deployment —
      a container that was replaced.
    - On this host under *my* process id, but not one of the dispatch
      managers of this process: no. That is an earlier life of this pid
      (process id 1 in a restarted container).
    - Otherwise: whether that process id is alive.

    ``me`` is the identity of the asking process.
    """
    if owner == me or owner in LIVE_OWNERS:
        return True  # a dispatch manager living in this very process
    host, pid = parse_owner(owner)
    if host != socket.gethostname():
        return False
    if me is not None and pid == parse_owner(me)[1]:
        return False
    return pid_alive(pid)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # os.kill(pid, 0) would TERMINATE the process on Windows
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return not _ended_unreaped(pid)
    except OSError:
        return False
    return not _ended_unreaped(pid)


def _ended_unreaped(pid: int) -> bool:
    """Whether ``pid`` is a process that has ended but was not collected by
    its parent yet (a zombie). It still answers ``kill(pid, 0)``, but it does
    not run — and under a parent that never collects (a container whose
    process 1 is not an init) it stays that way. Linux says so in
    ``/proc``; elsewhere this answers ``False``."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            stat = handle.read()
    except OSError:
        return False
    # "pid (name) state …" — the name may itself contain ")" and spaces.
    state = stat.rpartition(b")")[2].split()[:1]
    return state in ([b"Z"], [b"X"])


def _pid_alive_windows(pid: int) -> bool:
    """The Windows half of :func:`pid_alive`, mirroring the POSIX rules: a
    process we may not open (``ERROR_ACCESS_DENIED``) exists, as a
    ``PermissionError`` from ``os.kill`` means it does on POSIX; a process
    whose exit code is still ``STILL_ACTIVE`` runs. (A process that exited
    *with* code 259 reads as running — Windows offers no way to tell; for a
    runcomposer owner process that is not an exit code it uses.)

    Not exercised by the test suite on Windows (it is run on Linux and
    macOS); read with that in mind."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    process_query_limited_information, error_access_denied, still_active = 0x1000, 5, 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == error_access_denied  # type: ignore[attr-defined]
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # it exists; its state cannot be read — like EPERM
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)
