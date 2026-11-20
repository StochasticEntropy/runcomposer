"""Defused XML reading (DESIGN.md §5): the one helper every XML-reading
ResultParser goes through.

A DTD is refused by the parser itself, wherever in the document it sits and
in whatever encoding the document is written — no entity can be declared, so
none can be expanded and none can point outside the file. A byte scan of the
first block cannot promise that: a padded prolog or a two-byte encoding walks
straight past it.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from xml.parsers import expat

from .errors import ParseError

__all__ = ["forbid_dtd", "parse_xml"]

_REASON = (
    "refused (defused parsing, DESIGN.md §5: no DTD, no entity declarations, "
    "no external entities)"
)


def forbid_dtd(path: str | Path) -> None:
    """Read the whole document once with a parser that refuses any DOCTYPE or
    entity declaration. Raises :class:`ParseError` for those and for XML that
    is not well-formed."""
    file = Path(path)

    def refuse_doctype(*_args: object) -> None:
        raise ParseError(f"{file}: XML carries a DOCTYPE declaration — {_REASON}")

    def refuse_entity(*_args: object) -> None:
        raise ParseError(f"{file}: XML declares an entity — {_REASON}")

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = refuse_doctype
    parser.EntityDeclHandler = refuse_entity
    parser.UnparsedEntityDeclHandler = refuse_entity
    parser.ExternalEntityRefHandler = lambda *_args: 0
    try:
        with open(file, "rb") as handle:
            parser.ParseFile(handle)
    except expat.ExpatError as exc:
        raise ParseError(f"{file}: not well-formed XML: {exc}") from None
    except OSError as exc:
        raise ParseError(f"{file}: cannot read: {exc}") from None


def parse_xml(path: str | Path) -> ET.Element:
    """:func:`forbid_dtd`, then the document's root element."""
    forbid_dtd(path)
    try:
        return ET.parse(Path(path)).getroot()
    except ET.ParseError as exc:  # unreachable after forbid_dtd; kept as a guard
        raise ParseError(f"{path}: not well-formed XML: {exc}") from None
