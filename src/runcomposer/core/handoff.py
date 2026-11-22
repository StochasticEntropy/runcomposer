"""The rules of one dispatch's hand-off, as code both contexts share
(DESIGN.md §4, §6.2).

The real ``DispatchContext`` (in the application layer) and the
conformance kit's ``FakeDispatchContext`` (``runcomposer.testing``) enforce
the order of a runner's calls — *refuse only before declare, declare exactly
once, deliver only after declare* — and the shape of what it hands over with
the functions here. A runner that passes its unit tests against the fake
therefore cannot meet a different rule in production.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

from .errors import DispatchRefused, IngestError, ParseError
from .model import REPORTABLE_STATUSES, Verdict

__all__ = [
    "HandoffOrder",
    "check_delivery_args",
    "check_reportable",
    "corpus_drifted",
    "correlate",
    "normalize_groups",
    "normalize_shards",
]


class HandoffOrder:
    """Tracks one dispatch's position in the hand-off and refuses calls made
    out of order, with the same messages everywhere."""

    def __init__(self, dispatch_id: str, *, declared: bool = False):
        self.dispatch_id = dispatch_id
        self.declared = declared

    def refuse(self, reason: str) -> NoReturn:
        if self.declared:
            raise RuntimeError(
                f"refused after the hand-off ({reason}) — refuse() is only legal before "
                f"declare(); dispatch {self.dispatch_id} fails instead (DESIGN.md §4)"
            )
        raise DispatchRefused(reason)

    def before_declare(self) -> None:
        if self.declared:
            raise RuntimeError(
                f"dispatch {self.dispatch_id} is already declared — one hand-off per dispatch "
                "(re-running a spec is a new dispatch, DESIGN.md §4)"
            )

    def before_deliver(self) -> None:
        if not self.declared:
            raise RuntimeError("deliver() before declare(): declare the shard set first")


def normalize_shards(
    shards: Sequence[str] | Mapping[str, Sequence[str]],
) -> tuple[list[str], dict[str, list[str]]]:
    """``declare``'s argument as ``(names, shard_items)``: shard names, or
    names mapped to the item ids each shard will report."""
    if isinstance(shards, Mapping):
        names = [str(name) for name in shards]
        shard_items = {str(name): [str(i) for i in items] for name, items in shards.items()}
    else:
        if isinstance(shards, str):
            raise TypeError("declare() takes a sequence of shard names, not one string")
        names = [str(name) for name in shards]
        shard_items = {}
    if len(set(names)) != len(names):
        raise ValueError(f"shard names must be distinct, got {names}")
    return names, shard_items


def corpus_drifted(ctx: Any) -> bool:
    """Has the corpus changed since the run of ``ctx`` (a plan or dispatch
    context) was composed? True when the live catalog's snapshot differs from
    the one recorded in the run's document (``source.snapshot``, §3.3) — the
    question a runner asks before it executes ids that may no longer mean
    what they meant. What it does about a yes is the runner's decision:
    refuse, or execute what still exists."""
    composed = (ctx.spec.get("source") or {}).get("snapshot")
    return bool(ctx.source.snapshot() != composed)


def normalize_groups(
    groups: Mapping[str, str] | None, names: Sequence[str]
) -> dict[str, str]:
    """``declare``'s ``groups`` as ``{shard: label}`` over declared shards
    only — a label for a shard that is not declared is a mistake."""
    if not groups:
        return {}
    if not isinstance(groups, Mapping):
        raise TypeError("declare(groups=…) takes a mapping of shard name → group label")
    unknown = sorted(str(shard) for shard in groups if str(shard) not in set(names))
    if unknown:
        raise ValueError(f"groups names shard(s) that are not declared: {unknown}")
    return {str(shard): str(label) for shard, label in groups.items() if str(label)}


def check_delivery_args(verdicts: object, bundle: object) -> None:
    if (verdicts is None) == (bundle is None):
        raise TypeError("deliver() takes exactly one of verdicts= or bundle=")


def check_reportable(verdicts: Iterable[Verdict]) -> list[Verdict]:
    delivered = list(verdicts)
    for verdict in delivered:
        if verdict.status not in REPORTABLE_STATUSES:
            raise ValueError(
                f"a delivered verdict carries status {verdict.status!r} — "
                f"report one of {REPORTABLE_STATUSES}"
            )
    return delivered


def _as_text(message: str) -> str:
    """A result's message as text a store can write: a lone UTF-16 surrogate
    (an escaped one in a JSON result file) becomes the replacement character.
    One stray character in a tool's output is no reason to lose the result
    it belongs to."""
    if not any("\ud800" <= char <= "\udfff" for char in message):
        return message
    return "".join("\ufffd" if "\ud800" <= char <= "\udfff" else char for char in message)


def correlate(
    parser: Any, bundle: Path, format_id: str, resolve: Callable[[str], str | None]
) -> tuple[list[Verdict], list[str]]:
    """Parse native artifacts and correlate every native name through
    ``resolve`` (``TestSource.resolve``, §5). Names it does not know are
    warnings, never invented as ids. A parser that fails — ``ParseError`` or
    anything else — and a bundle with no results are ``bad-bundle``. A
    message is stored as text (``_as_text``)."""
    try:
        parsed = list(parser.parse(bundle))
    except ParseError as exc:
        raise IngestError(str(exc), reason="bad-bundle") from exc
    except IngestError:
        raise
    except Exception as exc:  # a parser bug is still a bundle we cannot take in
        raise IngestError(
            f"{bundle}: the {format_id!r} parser failed: {type(exc).__name__}: {exc}",
            reason="bad-bundle",
        ) from exc
    if not parsed:
        raise IngestError(f"bundle contains no {format_id!r} results: {bundle}", reason="bad-bundle")
    verdicts: list[Verdict] = []
    warnings: list[str] = []
    for entry in parsed:
        if entry.status not in REPORTABLE_STATUSES:
            raise IngestError(
                f"{bundle}: result {entry.native_name!r} carries status {entry.status!r} — "
                f"an executor reports one of {REPORTABLE_STATUSES}",
                reason="bad-bundle",
            )
        item_id = resolve(entry.native_name)
        if item_id is None:
            warnings.append(f"unknown native name {entry.native_name!r} — verdict skipped")
            continue
        verdicts.append(
            Verdict(
                item_id=item_id,
                status=entry.status,
                duration_ms=entry.duration_ms,
                message=_as_text(entry.message) if isinstance(entry.message, str) else entry.message,
                attempt=entry.attempt,
            )
        )
    return verdicts, warnings
