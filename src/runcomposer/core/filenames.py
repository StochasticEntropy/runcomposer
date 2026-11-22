"""File names that come from outside, and the file systems that keep them.

A name from outside — an upload's, a build's archived file, a URL's last
segment — becomes one file in one directory only when it is one path
component: no separator of either kind, no ``.`` or ``..``, no drive, no
character the file system refuses. What a file system refuses differs: Linux
keeps ``a|b`` and ``results-10:00.json``; Windows does not, and drops a
trailing dot or space. And whether ``Results.json`` and ``results.json`` are
one file is a question only the file system answers.

- :func:`single_name_problem` — one path component on any platform;
- :func:`windows_name_problem` — what Windows refuses besides;
- :func:`local_name_problem` — what *this* machine refuses: the first, and
  the second when it runs Windows;
- :func:`child_path` — ``directory / name``, refused unless ``name`` is
  one component this machine keeps; nothing is resolved;
- :func:`portable_name_problem` and :func:`name_collision_key` — strict and
  the same on every platform, for an author who wants names that every file
  system keeps as written.

Stdlib only.
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

__all__ = [
    "MAX_NAME_BYTES",
    "child_path",
    "local_name_problem",
    "name_collision_key",
    "portable_name_problem",
    "single_name_problem",
    "windows_name_problem",
]

#: The longest file name most file systems keep, in UTF-8 bytes.
MAX_NAME_BYTES = 255
#: Characters a Windows file name cannot hold (``:`` would also open an
#: alternate data stream: ``name:stream``, ``name::$DATA``).
_WINDOWS_CHARACTERS = frozenset('<>:"|?*')
#: The device names of Windows — with any extension, in any case, and with
#: ``0`` and ``¹``, ``²``, ``³`` among the digits (Windows' own naming rules).
_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"{device}{digit}" for device in ("COM", "LPT") for digit in "0123456789¹²³"]
)


def single_name_problem(name: str) -> str | None:
    """Why ``name`` is not one path component on every platform, or
    ``None``: empty; ``.`` or ``..`` (also as look-alikes that normalize to
    them, such as fullwidth dots); a separator, ``/`` or ``\\``, also as a
    look-alike; a control character (NUL included); a drive or a root of its
    own (``C:x``); longer than :data:`MAX_NAME_BYTES`."""
    if not name:
        return "is empty"
    if any(unicodedata.category(char) == "Cc" for char in name):
        return "contains a control character"
    folded = unicodedata.normalize("NFKC", name)
    if name in (".", "..") or folded in (".", ".."):
        return "is '.' or '..'"
    if any(separator in text for separator in ("/", "\\") for text in (name, folded)):
        return "contains a path separator"
    windows = PureWindowsPath(name)
    if windows.drive or windows.anchor or PurePosixPath(name).anchor:
        return "names a drive or a root"
    if len(name.encode("utf-8", "replace")) > MAX_NAME_BYTES:
        return f"is longer than {MAX_NAME_BYTES} bytes"
    return None


def windows_name_problem(name: str) -> str | None:
    """Why Windows cannot keep ``name`` as written, or ``None``: a character
    it refuses (``<>:"|?*`` — ``:`` opens another stream of a file), a
    trailing dot or space (dropped, so that ``x.`` and ``x`` are one file),
    a device name (``CON``, ``nul.txt``, ``CON .txt``, ``COM0``, ``LPT¹``).
    Only the name's own form: what one directory already holds is the file
    system's to say."""
    refused = "".join(sorted(set(name) & _WINDOWS_CHARACTERS))
    if refused:
        return f"contains {refused!r}, which a Windows file name cannot hold"
    if name.endswith((".", " ")):
        return "ends in a dot or a space, which Windows drops"
    if name.split(".", 1)[0].rstrip(" ").upper() in _DEVICE_NAMES:
        return "is a device name on Windows"
    return None


def local_name_problem(name: str) -> str | None:
    """Why this machine's file system cannot keep ``name`` as one file of a
    directory: :func:`single_name_problem` everywhere, and on Windows
    :func:`windows_name_problem` too."""
    problem = single_name_problem(name)
    if problem is None and os.name == "nt":
        problem = windows_name_problem(name)
    return problem


def child_path(directory: str | Path, name: str) -> Path:
    """``directory / name`` — when ``name`` is one component this machine
    keeps (:func:`local_name_problem`); else ``ValueError``. Decided from the
    name alone, before anything is opened or resolved: resolving a path on
    Windows can reach out to a share it names."""
    problem = local_name_problem(name)
    if problem is not None:
        raise ValueError(f"{name!r} {problem}")
    base = Path(directory)
    path = base / name
    if path.parent != base or path.name != name:  # what the checks above guarantee
        raise ValueError(f"{name!r} does not name a file in {base}")
    return path


def portable_name_problem(name: str) -> str | None:
    """Why ``name`` is not a file name every file system keeps as written,
    or ``None`` — strict, and the same answer on every platform, for an
    author who wants portable names: :func:`single_name_problem`,
    :func:`windows_name_problem`, and an invisible format character
    (zero-width, bidirectional controls), which makes two names that read
    alike. Who stores what a client sent uses :func:`local_name_problem`."""
    problem = single_name_problem(name) or windows_name_problem(name)
    if problem is None and any(unicodedata.category(char) == "Cf" for char in name):
        problem = "contains an invisible format character"
    return problem


def name_collision_key(name: str) -> str:
    """The key under which two names may be one file where case or Unicode
    normalization does not count (Windows, a default macOS volume): NFC,
    then Unicode case folding. It says more than such a file system does in
    places — ``ß`` and ``SS`` share a key, NTFS keeps both — and less in
    others: it cannot see the 8.3 short names Windows gives long ones
    (``PROGRA~1``), nor NTFS's own case table, which differs from Unicode's
    (NTFS upper-cases a dotless ``ı`` to ``I``, so ``ı`` and ``i`` are one
    file there). Whether two names are one file is answered for certain only
    by the file system: create the second exclusively."""
    return unicodedata.normalize("NFC", name).casefold()
