"""Blanking secrets out of what is recorded, logged and answered (DESIGN.md
§8, "Secrets").

The secrets are the values a configuration takes from the environment
(``Config.secret_values``); this module only blanks them out — of free text,
and of the string values inside runner-defined data — never out of mapping
keys or identifiers.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "FREE_TEXT_KEYS",
    "MIN_SECRET_LENGTH",
    "REDACTED",
    "redact",
    "redact_free_text",
    "without_location",
    "without_locations",
]

#: A secret shorter than this cannot be blanked out of recorded text without
#: mangling it (blanking "4" would rewrite every 4) — refused at startup.
MIN_SECRET_LENGTH = 4

REDACTED = "‹from the environment›"

#: The keys of an event's data that hold free text — what a runner or a
#: parser wrote — and are therefore blanked; every other key holds an
#: identifier or a record the core composed (shards, groups, states, kinds,
#: ids, links, counts) and is never rewritten.
FREE_TEXT_KEYS = ("message", "reason", "error", "plan", "detail")


def redact(value: Any, secrets: frozenset[str] | set[str]) -> Any:
    """``value`` with every secret blanked out — in a string, and in the
    string values inside mappings and lists. Mapping keys are never
    rewritten."""
    if not secrets:
        return value
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, Mapping):
        return {key: redact(entry, secrets) for key, entry in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(entry, secrets) for entry in value]
    return value


def redact_free_text(data: Mapping[str, Any], secrets: frozenset[str] | set[str]) -> dict[str, Any]:
    """An event's data with the secrets blanked out of its free-text fields
    (:data:`FREE_TEXT_KEYS`) only: identifiers and structured records pass
    unchanged, so a shard named like an environment value stays that
    shard."""
    return {
        key: redact(entry, secrets) if key in FREE_TEXT_KEYS else entry
        for key, entry in data.items()
    }


def without_location(text: str, root: Path, name: str) -> str:
    """``text`` with ``root``'s location on this machine taken out: a path
    inside it becomes its path relative to ``root``, ``root``'s own path
    becomes ``name``. Both spellings of the location are covered — as given
    and fully resolved (a temporary directory is often reached through a
    symlink) — the longer first, so neither is cut in half."""
    spellings = {str(root), os.path.abspath(root)}
    try:
        spellings.add(str(root.resolve()))
    except (OSError, ValueError):
        pass
    for location in sorted(spellings, key=len, reverse=True):
        text = text.replace(location + os.sep, "").replace(location, name)
    return text


def without_locations(value: Any, roots: Mapping[str, Path]) -> Any:
    """:func:`without_location` for each ``name → root`` of ``roots``, in a
    string and in the strings inside mappings and lists (keys untouched) —
    what is recorded about a dispatch names no directory of this machine."""
    if isinstance(value, str):
        for name, root in sorted(roots.items(), key=lambda entry: -len(str(entry[1]))):
            value = without_location(value, root, name)
        return value
    if isinstance(value, Mapping):
        return {key: without_locations(entry, roots) for key, entry in value.items()}
    if isinstance(value, (list, tuple)):
        return [without_locations(entry, roots) for entry in value]
    return value
