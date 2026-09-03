"""`robot-output-xml` ResultParser (DESIGN.md §5): turns Robot Framework
output.xml into ParsedVerdicts. Native name = the test's longname; correlation
to Item ids happens only through TestSource.resolve.

Defused parsing is a stated ResultParser obligation (§5): any document
carrying a DOCTYPE/ENTITY declaration is refused outright before parsing —
no external entities, no DTD expansion, no entity-blowup surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runcomposer.core.ports import ParsedVerdict

__all__ = ["ParseError", "RobotOutputXmlParser"]

_STATUS_MAP = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP", "NOT RUN": "SKIP"}


class ParseError(ValueError):
    """Raised for malformed or refused result documents."""


def _require_robot():
    try:
        from robot.api import ExecutionResult, ResultVisitor
    except ImportError as exc:
        raise ImportError(
            "the robot-output-xml parser needs Robot Framework — install the extra "
            "pip install 'runcomposer[robot]'"
        ) from exc
    return ExecutionResult, ResultVisitor


def _refuse_doctype(file: Path) -> None:
    head = file.read_bytes()[: 64 * 1024]
    for token in (b"<!DOCTYPE", b"<!ENTITY"):
        if token in head:
            raise ParseError(
                f"{file}: XML contains a {token.decode()} declaration — refused "
                "(defused parsing, DESIGN.md §5: no external entities, no DTD expansion)"
            )


class RobotOutputXmlParser:
    format_id = "robot-output-xml"

    def parse(self, path: Any) -> list[ParsedVerdict]:
        root = Path(path)
        if root.is_file():
            files = [root]
        else:
            files = sorted(p for p in root.rglob("output*.xml") if p.is_file())
        parsed: list[ParsedVerdict] = []
        for file in files:
            parsed.extend(self._parse_file(file))
        return parsed

    def _parse_file(self, file: Path) -> list[ParsedVerdict]:
        ExecutionResult, ResultVisitor = _require_robot()
        _refuse_doctype(file)
        try:
            result = ExecutionResult(str(file))
        except Exception as exc:  # robot raises DataError and friends
            raise ParseError(f"{file}: not a parsable output.xml: {exc}") from None

        collected: list[ParsedVerdict] = []

        class _Collector(ResultVisitor):
            def visit_test(self, test):  # noqa: ANN001 - robot API signature
                collected.append(
                    ParsedVerdict(
                        native_name=test.longname,
                        status=_STATUS_MAP.get(test.status, "ERROR"),
                        duration_ms=int(test.elapsedtime),
                        message=test.message or "",
                    )
                )

        result.visit(_Collector())
        return collected
