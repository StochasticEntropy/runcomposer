"""`junit-xml` ResultParser (DESIGN.md §5, §14 P3): turns JUnit-style XML
into ParsedVerdicts.

Native-name mapping (documented, not hidden): each ``<testcase>`` yields
``native_name = "<classname>.<name>"`` (or just ``<name>`` when classname is
empty) — exactly as the artifact spells it. The parser never reconstructs
framework ids (that would be normalization, which §5 assigns to the
TestSource that owns the id space). For pytest corpora the manifest declares
``aliases`` mapping these junit names onto nodeids — see
examples/pytest-shop/manifest.json and the manifest source's alias support.

Defused parsing (§5 obligation): documents carrying DOCTYPE/ENTITY
declarations are refused before any XML machinery runs.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from runcomposer.core.ports import ParsedVerdict

__all__ = ["ParseError", "JunitXmlParser"]


class ParseError(ValueError):
    """Raised for malformed or refused result documents."""


def _refuse_doctype(file: Path) -> None:
    head = file.read_bytes()[: 64 * 1024]
    for token in (b"<!DOCTYPE", b"<!ENTITY"):
        if token in head:
            raise ParseError(
                f"{file}: XML contains a {token.decode()} declaration — refused "
                "(defused parsing, DESIGN.md §5: no external entities, no DTD expansion)"
            )


class JunitXmlParser:
    format_id = "junit-xml"

    def parse(self, path: Any) -> list[ParsedVerdict]:
        """Parse a junit file, or every ``junit*.xml`` in a bundle directory."""
        root = Path(path)
        files = [root] if root.is_file() else sorted(p for p in root.rglob("junit*.xml") if p.is_file())
        parsed: list[ParsedVerdict] = []
        for file in files:
            parsed.extend(self._parse_file(file))
        return parsed

    def _parse_file(self, file: Path) -> list[ParsedVerdict]:
        _refuse_doctype(file)
        try:
            tree = ET.parse(file)
        except ET.ParseError as exc:
            raise ParseError(f"{file}: not parsable JUnit XML: {exc}") from None
        root = tree.getroot()
        if root.tag not in ("testsuite", "testsuites"):
            raise ParseError(f"{file}: root element {root.tag!r} is not a JUnit testsuite(s)")
        parsed = []
        for case in root.iter("testcase"):
            classname = case.get("classname") or ""
            name = case.get("name") or ""
            if not name:
                raise ParseError(f"{file}: <testcase> without a name attribute")
            native = f"{classname}.{name}" if classname else name
            duration_ms = int(float(case.get("time") or 0) * 1000)
            failure = case.find("failure")
            error = case.find("error")
            skipped = case.find("skipped")
            if error is not None:
                status, message = "ERROR", error.get("message") or (error.text or "")
            elif failure is not None:
                status, message = "FAIL", failure.get("message") or (failure.text or "")
            elif skipped is not None:
                status, message = "SKIP", skipped.get("message") or (skipped.text or "")
            else:
                status, message = "PASS", ""
            parsed.append(
                ParsedVerdict(
                    native_name=native,
                    status=status,
                    duration_ms=duration_ms,
                    message=(message or "").strip()[:500],
                )
            )
        return parsed
