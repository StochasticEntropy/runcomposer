"""`robot-output-xml` ResultParser (DESIGN.md §5): turns Robot Framework
output.xml into ParsedVerdicts. Native name = the test's longname; correlation
to Item ids happens only through TestSource.resolve.

The document is checked with the SDK's defused reader first: a DTD anywhere
in it, in any encoding, is refused before Robot's own reader sees the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runcomposer.sdk import ParsedVerdict, ParseError, ParserBase, forbid_dtd

__all__ = ["RobotOutputXmlParser"]

_STATUS_MAP = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP", "NOT RUN": "SKIP"}


def _require_robot() -> tuple[Any, Any]:
    try:
        from robot.api import ExecutionResult, ResultVisitor
    except ImportError as exc:
        raise ParseError(
            "the robot-output-xml parser needs Robot Framework — install the extra: "
            "pip install 'runcomposer[robot]'"
        ) from exc
    return ExecutionResult, ResultVisitor


def _duration_ms(test: Any) -> int:
    """Whole milliseconds, as Robot reports them — except that a test which
    ran (PASS or FAIL) in under a millisecond is 1 ms, not 0: a duration of 0
    means "not known" to the duration history, and a fast test is known to be
    fast. Weighed as unknown it would count as long as the mean of the known
    ones when a plan is balanced."""
    elapsed = int(test.elapsedtime)
    return 1 if elapsed == 0 and test.status in ("PASS", "FAIL") else elapsed


class RobotOutputXmlParser(ParserBase):
    format_id = "robot-output-xml"

    def __init__(self, pattern: str = "output*.xml"):
        """``pattern``: which files of a bundle directory are Robot outputs."""
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
        execution_result, result_visitor = _require_robot()
        forbid_dtd(file)
        try:
            result = execution_result(str(file))
        except Exception as exc:  # robot raises DataError and friends
            raise ParseError(f"{file}: not a parsable output.xml: {exc}") from None

        collected: list[ParsedVerdict] = []

        class _Collector(result_visitor):  # type: ignore[misc, valid-type]
            def visit_test(self, test: Any) -> None:
                collected.append(
                    ParsedVerdict(
                        native_name=test.longname,
                        status=_STATUS_MAP.get(test.status, "ERROR"),
                        duration_ms=_duration_ms(test),
                        message=test.message or "",
                    )
                )

        result.visit(_Collector())
        return collected
