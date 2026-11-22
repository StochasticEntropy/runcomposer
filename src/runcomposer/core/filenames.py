"""File names that every file system keeps as written.

A bundle travels: pushed to a server, dropped into an inbox, promoted out of
the quarantine, downloaded from a build. A name one file system keeps and
another refuses or changes would make it a different bundle on another
machine, so the names that come from outside — an upload's, a download's —
are held to what all of them keep.

Stdlib only.
"""

from __future__ import annotations

import unicodedata

__all__ = ["name_collision_key", "portable_name_problem"]

#: Characters a Windows file name cannot hold (``:`` would also open an
#: alternate data stream). Refused on every platform: a bundle travels.
_UNPORTABLE_CHARACTERS = frozenset('<>:"|?*')
#: The device names of Windows — with any extension, in any case (``¹``,
#: ``²`` and ``³`` count as digits there).
_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"{device}{digit}" for device in ("COM", "LPT") for digit in "123456789¹²³"]
)


def portable_name_problem(name: str) -> str | None:
    """Why ``name`` — one path component — is not a file name every file
    system keeps as written, or ``None`` when it is. Windows refuses some
    characters and the device names (``CON``, ``NUL``, ``COM1`` …, also
    as ``nul.txt``), and drops a trailing dot or space, so that ``x.``
    and ``x`` are one file there."""
    unportable = "".join(sorted(set(name) & _UNPORTABLE_CHARACTERS))
    if unportable:
        return f"contains {unportable!r}, which a Windows file name cannot hold"
    if name.endswith((".", " ")):
        return "ends in a dot or a space, which Windows drops"
    if name.split(".", 1)[0].rstrip(" ").upper() in _DEVICE_NAMES:
        return "is a device name on Windows"
    return None


def name_collision_key(name: str) -> str:
    """The key under which two names are one file on a file system that
    ignores case or Unicode normalization (Windows, macOS): two names of one
    directory with the same key are one file there."""
    return unicodedata.normalize("NFC", name).casefold()
