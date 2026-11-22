"""Who executes a dispatch, and whether that process is still alive
(DESIGN.md §4, recovery).

A dispatch names its owner — ``host:pid:token.machine`` — for as long as a
runner executes it. Recovery (``app.recovery``) and ``cancel``
(``app.dispatching``, ``cli.runs``) ask :func:`owner_alive` whether that
owner can still finish it.

``machine`` is :func:`machine_key`: which machine the owner ran on, by
something that does not change with the network — the operating system's
machine id — so that a laptop whose host name changes does not take its
own live owners for gone. Where the system has no machine id (a slim
container image) the key is made of the host name, and says so.
"""

from __future__ import annotations

import functools
import hashlib
import os
import socket
import subprocess
import sys
from typing import Any

from runcomposer.core.ids import new_ulid

__all__ = [
    "LIVE_OWNERS",
    "machine_key",
    "owner_alive",
    "owner_identity",
    "parse_owner",
    "pid_alive",
]

#: The two kinds of machine key: from the system's machine id, which does not
#: change with the network, or — where there is none — from the host name.
_STABLE, _FROM_HOSTNAME = "m-", "h-"


#: Owner identities of the dispatch managers alive in this process — added
#: and removed by ``DispatchManager``.
LIVE_OWNERS: set[str] = set()


def _system_machine_id() -> str | None:
    """The operating system's own id of this machine, or ``None``: Linux's
    ``/etc/machine-id``, macOS's platform UUID, Windows's ``MachineGuid``."""
    platform: str = str(sys.platform)  # a value, not a type-checker's platform constant
    if platform.startswith("linux"):
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                with open(path, encoding="ascii") as handle:
                    value = handle.read().strip()
            except OSError:
                continue
            if value:
                return value
        return None
    if platform == "darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for line in out.splitlines():
            if "IOPlatformUUID" in line:
                return line.rsplit("=", 1)[-1].strip().strip('"') or None
        return None
    if platform == "win32":
        return _windows_machine_guid()
    return None


def _windows_machine_guid() -> str | None:
    """Windows's ``MachineGuid`` from the registry, or ``None``."""
    try:
        import importlib

        registry: Any = importlib.import_module("winreg")
        key = registry.OpenKey(registry.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
        value, _kind = registry.QueryValueEx(key, "MachineGuid")
        return str(value) or None
    except (OSError, ImportError):
        return None


@functools.cache
def machine_key() -> str:
    """Which machine this is, as a short key: ``m-…`` from the system's
    machine id (it does not change when the host name does), ``h-…`` from
    the host name where the system has none."""
    stable = _system_machine_id()
    if stable:
        return _STABLE + hashlib.sha256(stable.encode()).hexdigest()[:12]
    return _FROM_HOSTNAME + hashlib.sha256(socket.gethostname().encode()).hexdigest()[:12]


def owner_identity() -> str:
    """Who is executing a dispatch: ``host:pid:token.machine``. The token
    tells two lives of one pid apart; ``machine`` is :func:`machine_key`."""
    return f"{socket.gethostname()}:{os.getpid()}:{new_ulid()}.{machine_key()}"


def parse_owner(owner: str) -> tuple[str, int]:
    host, _, _token = owner.rpartition(":")
    host, _, pid = host.rpartition(":")
    try:
        return host, int(pid)
    except ValueError:
        return owner, -1


def _machine_of(owner: str) -> str | None:
    """The machine key an owner identity carries; ``None`` for one written
    before identities carried it."""
    token = owner.rpartition(":")[2]
    _, dot, machine = token.partition(".")
    return machine if dot and machine.startswith((_STABLE, _FROM_HOSTNAME)) else None


def owner_alive(owner: str, me: str | None = None) -> bool:
    """Is the process that owner identity names still executing?

    An owner is declared gone only when it provably ran on *this* machine
    and its process is gone:

    - Same machine (same :func:`machine_key`, whatever the host name is
      now): under *my* process id but not one of the dispatch managers of
      this process, it is an earlier life of this pid (process id 1 in a
      restarted container) — gone; otherwise whether that process id lives.
    - Another machine, both identified by the system's machine id: not
      known to be gone — it is alive as far as this process can tell (a
      dispatch it left is closed with ``cancel --force``).
    - Another machine identified by its host name only (a container image
      without a machine id): gone. One instance owns a store (DESIGN.md
      §13), and a new host name there means the container was replaced.
    - An identity written before identities carried a machine key: by the
      host name, as then — another host is gone.

    ``me`` is the identity of the asking process.
    """
    if owner == me or owner in LIVE_OWNERS:
        return True  # a dispatch manager living in this very process
    host, pid = parse_owner(owner)
    theirs, mine = _machine_of(owner), machine_key()
    if theirs is None:
        same = host == socket.gethostname()
    else:
        same = theirs == mine
        if not same and theirs.startswith(_STABLE) and mine.startswith(_STABLE):
            return True  # another machine: its process cannot be looked at from here
    if not same:
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
