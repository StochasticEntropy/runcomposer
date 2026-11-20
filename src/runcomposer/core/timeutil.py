"""Timestamps: one writer, one reader.

Every stored time is ISO-8601 UTC with a ``Z`` suffix and second resolution,
so stored values compare correctly as strings. Anything arriving from outside
(a filter, a document) goes through :func:`normalize_time` first — a lexical
compare against an unvalidated string silently answers the wrong question.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .errors import InvalidRequest

__all__ = ["epoch_ms", "normalize_time", "parse_time", "utc_now"]

_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(_FORMAT)


def parse_time(text: str) -> datetime:
    """Parse an ISO-8601 time (``Z`` or a numeric offset; a naive time is
    read as UTC). Raises :class:`InvalidRequest` for anything else."""
    if not isinstance(text, str) or not text.strip():
        raise InvalidRequest(f"not an ISO-8601 time: {text!r}")
    candidate = text.strip()
    if candidate[-1] in "zZ":
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise InvalidRequest(
            f"not an ISO-8601 time: {text!r} (expected e.g. 2026-07-06T09:14:03Z)"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_time(text: str) -> str:
    """Any accepted spelling → the stored spelling."""
    return parse_time(text).strftime(_FORMAT)


def epoch_ms(text: str | None) -> int:
    return int(parse_time(text).timestamp() * 1000) if text else 0
