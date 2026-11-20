"""`junit-xml` ResultParser (DESIGN.md §5): turns JUnit-style XML into
ParsedVerdicts.

Native-name mapping (documented, not hidden): each ``<testcase>`` yields
``native_name = "<classname>.<name>"`` (or just ``<name>`` when classname is
empty) — exactly as the artifact spells it. The parser never reconstructs
framework ids: that would be normalization, which §5 assigns to the
TestSource that owns the id space. For pytest corpora the manifest declares
``aliases`` mapping these names onto nodeids — see
examples/pytest-shop/manifest.json.

XML is read through the SDK's defused reader: a document carrying a DTD is
refused by the parser, wherever the declaration sits and whatever the
encoding.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runcomposer.sdk import ParsedVerdict, ParseError, parse_xml

__all__ = ["JunitXmlParser"]


class JunitXmlParser:
    format_id = "junit-xml"

    def __init__(self, pattern: str = "junit*.xml"):
        """``pattern``: which files of a bundle directory are JUnit reports."""
        self.pattern = pattern

    def parse(self, path: Any) -> list[ParsedVerdict]:
        root = Path(path)
        files = (
            [root] if root.is_file() else sorted(p for p in root.rglob(self.pattern) if p.is_file())
        )
        parsed: list[ParsedVerdict] = []
        for file in files:
            parsed.extend(self._parse_file(file))
        return parsed

    def _parse_file(self, file: Path) -> list[ParsedVerdict]:
        root = parse_xml(file)
        if root.tag not in ("testsuite", "testsuites"):
            raise ParseError(f"{file}: root element {root.tag!r} is not a JUnit testsuite(s)")
        parsed = []
        for case in root.iter("testcase"):
            classname = case.get("classname") or ""
            name = case.get("name") or ""
            if not name:
                raise ParseError(f"{file}: <testcase> without a name attribute")
            try:
                duration_ms = int(float(case.get("time") or 0) * 1000)
            except ValueError:
                raise ParseError(f"{file}: <testcase {name!r}> has a non-numeric time") from None
            failure, error, skipped = case.find("failure"), case.find("error"), case.find("skipped")
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
                    native_name=f"{classname}.{name}" if classname else name,
                    status=status,
                    duration_ms=duration_ms,
                    message=(message or "").strip()[:500],
                )
            )
        return parsed
