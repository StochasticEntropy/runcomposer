"""`runcomposer-verdicts` ResultParser — the reference results format (§5).

A plain JSON document any executor can emit with a few lines of stdlib code::

    {"format": "runcomposer-verdicts",
     "verdicts": [{"name": "<native test name>", "status": "PASS",
                   "duration_ms": 120, "message": "", "attempt": 1}]}

``name`` is the executor's *native* name; correlation to Item ids happens in
the core via TestSource.resolve, never here. Framework-native parsers
(robot-output-xml, junit-xml) arrive with P2/P3 (DESIGN.md §14).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from runcomposer.core.model import VERDICT_STATUSES
from runcomposer.core.ports import ParsedVerdict

__all__ = ["ParseError", "VerdictsParser"]


class ParseError(ValueError):
    """Raised for malformed result documents."""


class VerdictsParser:
    format_id = "runcomposer-verdicts"

    def parse(self, path: Any) -> list[ParsedVerdict]:
        """Parse a results file, or every matching ``*.json`` in a bundle
        directory (files that are not this format are skipped)."""
        root = Path(path)
        files = [root] if root.is_file() else sorted(root.rglob("*.json"))
        parsed: list[ParsedVerdict] = []
        for file in files:
            document = self._try_load(file)
            if document is None:
                continue
            parsed.extend(self._parse_document(document, file))
        return parsed

    @staticmethod
    def _try_load(file: Path) -> dict[str, Any] | None:
        try:
            document = json.loads(file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if isinstance(document, dict) and document.get("format") == VerdictsParser.format_id:
            return document
        return None

    @staticmethod
    def _parse_document(document: dict[str, Any], file: Path) -> list[ParsedVerdict]:
        entries = document.get("verdicts")
        if not isinstance(entries, list):
            raise ParseError(f"{file}: 'verdicts' must be a list")
        parsed = []
        for index, entry in enumerate(entries):
            where = f"{file}: verdicts[{index}]"
            if not isinstance(entry, dict):
                raise ParseError(f"{where}: must be an object")
            name = entry.get("name")
            status = entry.get("status")
            if not isinstance(name, str) or not name:
                raise ParseError(f"{where}: 'name' is required")
            if status not in VERDICT_STATUSES:
                raise ParseError(f"{where}: 'status' must be one of {VERDICT_STATUSES}, got {status!r}")
            parsed.append(
                ParsedVerdict(
                    native_name=name,
                    status=status,
                    duration_ms=int(entry.get("duration_ms", 0)),
                    message=str(entry.get("message", "")),
                    attempt=int(entry.get("attempt", 1)),
                )
            )
        return parsed
