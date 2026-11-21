"""`robot-output-xml` ResultParser (DESIGN.md §5): turns Robot Framework
output.xml into ParsedVerdicts. Native name = the test's longname; correlation
to Item ids happens only through TestSource.resolve.

The document is checked with the SDK's defused reader first: a DTD anywhere
in it, in any encoding, is refused before it is read.

It is read with the standard library's streaming XML reader, not with Robot
Framework's result model: a server parses what its workers produced, and a
server process loads nothing of Robot Framework for it (importing any part
of the ``robot`` package loads all of it, its process-global logger
included), nor does it hold a large output's keyword tree in memory. What it
reads is Robot's own reading of the file, restated: the longname (the suite
names and the test name joined with ``.``), the test's status and message,
its elapsed time (``elapsed`` of Robot 7 outputs, ``starttime``/``endtime``
of earlier ones, without either the sum of what it contains), and a failed
or skipped suite teardown applied to every test below it — the way Robot
applies it to an output that Robot (not rebot) wrote.
tests/test_robot_output_reader.py compares it field by field with Robot's
own result model.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from runcomposer.sdk import ParsedVerdict, ParseError, ParserBase, forbid_dtd

__all__ = ["RobotOutputXmlParser"]

_STATUS_MAP = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP", "NOT RUN": "SKIP"}
#: Elements whose elapsed time counts towards their parent's when the parent
#: has no time of its own (Robot's ``_elapsed_time_from_children``).
_TIMED = frozenset((
    "kw", "for", "iter", "if", "branch", "try", "while", "group", "variable", "return",
    "break", "continue", "error",
))  # fmt: skip
#: Robot's messages for a test below a suite whose teardown failed or skipped.
_TEARDOWN_FAILED = "Parent suite teardown failed:\n%s"
_ALSO_TEARDOWN_FAILED = "\n\nAlso parent suite teardown failed:\n%s"
_TEARDOWN_SKIPPED = "Skipped in parent suite teardown:\n%s"
_ALSO_TEARDOWN_SKIPPED = "Skipped in parent suite teardown:\n%s\n\nEarlier message:\n%s"


def _duration_ms(status: str, elapsed: int) -> int:
    """Whole milliseconds, as Robot reports them — except that a test which
    ran (PASS or FAIL) in under a millisecond is 1 ms, not 0: a duration of 0
    means "not known" to the duration history, and a fast test is known to be
    fast. Weighed as unknown it would count as long as the mean of the known
    ones when a plan is balanced."""
    return 1 if elapsed == 0 and status in ("PASS", "FAIL") else elapsed


def _legacy_time(value: str | None) -> datetime | None:
    """A Robot < 7 timestamp (``20240101 12:00:00.000``), read as Robot reads it."""
    if not value or value == "N/A":
        return None
    text = value.ljust(24, "0")
    return datetime(
        int(text[:4]), int(text[4:6]), int(text[6:8]),
        int(text[9:11]), int(text[12:14]), int(text[15:17]), int(text[18:24]),
    )


@dataclass
class _Test:
    name: str
    status: str = "FAIL"
    message: str = ""
    elapsed: timedelta = field(default_factory=timedelta)


@dataclass
class _Suite:
    name: str
    tests: list[_Test] = field(default_factory=list)
    suites: list[_Suite] = field(default_factory=list)
    #: (status, message) of the suite's teardown, when it has one.
    teardown: tuple[str, str] | None = None

    def all_tests(self) -> list[_Test]:
        found = list(self.tests)
        for child in self.suites:
            found.extend(child.all_tests())
        return found


@dataclass
class _Frame:
    """One open element: what it is, and what it gathered so far."""

    tag: str
    node: _Suite | _Test | None = None
    status: str | None = None
    message: str = ""
    own: timedelta | None = None
    children: timedelta = field(default_factory=timedelta)
    suite_teardown: bool = False


def read_output(file: str | Path) -> list[tuple[str, str, str, int]]:
    """``(longname, status, message, elapsed_ms)`` of every test in a Robot
    output.xml, in document order — Robot's reading of it, without Robot.
    Raises ``ValueError`` for a document that is not a Robot output."""
    roots: list[_Suite] = []
    stack: list[_Frame] = []
    by_robot = True
    for event, elem in ET.iterparse(str(file), events=("start", "end")):
        tag = elem.tag
        if event == "start":
            parent = stack[-1] if stack else None
            frame = _Frame(tag)
            if parent is None:
                if tag != "robot":
                    raise ValueError(f"the root element is <{tag}>, not <robot>")
                generator = (elem.get("generator") or "unknown").split() or ["unknown"]
                by_robot = generator[0].upper() == "ROBOT"
            elif tag == "suite":
                suite = _Suite(elem.get("name", ""))
                frame.node = suite
                if isinstance(parent.node, _Suite):
                    parent.node.suites.append(suite)
                else:
                    roots.append(suite)
            elif tag == "test" and isinstance(parent.node, _Suite):
                test = _Test(elem.get("name", ""))
                frame.node = test
                parent.node.tests.append(test)
            elif tag == "kw" and isinstance(parent.node, _Suite):
                frame.suite_teardown = (elem.get("type") or "").lower() == "teardown"
            stack.append(frame)
            continue
        frame = stack.pop()
        parent = stack[-1] if stack else None
        if tag == "status":
            if parent is not None:
                parent.status = elem.get("status", "FAIL")
                if elem.text:
                    parent.message = elem.text
                if "elapsed" in elem.attrib:  # Robot >= 7
                    parent.own = timedelta(seconds=float(elem.attrib["elapsed"]))
                else:  # Robot < 7
                    start = _legacy_time(elem.get("starttime"))
                    end = _legacy_time(elem.get("endtime"))
                    if start and end:
                        parent.own = end - start
        else:
            elapsed = frame.own if frame.own is not None else frame.children
            if isinstance(frame.node, _Test):
                if frame.status is not None:
                    frame.node.status = frame.status
                frame.node.message = frame.message
                frame.node.elapsed = elapsed
            elif frame.suite_teardown and parent is not None and isinstance(parent.node, _Suite):
                parent.node.teardown = (frame.status or "FAIL", frame.message)
            if tag in _TIMED and parent is not None:
                parent.children += elapsed
        if parent is not None:
            elem.clear()  # what was read is let go: a large output stays small here
    if by_robot:
        for root in roots:
            _apply_teardowns(root)
    found: list[tuple[str, str, str, int]] = []

    def collect(suite: _Suite, prefix: str) -> None:
        name = f"{prefix}.{suite.name}" if prefix else suite.name
        for test in suite.tests:
            found.append(
                (f"{name}.{test.name}", test.status, test.message,
                 round(test.elapsed.total_seconds() * 1000))
            )
        for child in suite.suites:
            collect(child, name)

    for root in roots:
        collect(root, "")
    return found


def _apply_teardowns(suite: _Suite) -> None:
    """Robot's handling of a suite teardown that failed or skipped, applied
    innermost suite first: every test below it fails (or is skipped), its
    message saying so."""
    for child in suite.suites:
        _apply_teardowns(child)
    if suite.teardown is None:
        return
    status, message = suite.teardown
    if status not in ("FAIL", "SKIP"):
        return
    for test in suite.all_tests():
        if status == "FAIL":
            if test.status != "SKIP":
                test.status = "FAIL"
            test.message += (_ALSO_TEARDOWN_FAILED if test.message else _TEARDOWN_FAILED) % message
        else:
            test.status = "SKIP"
            test.message = (
                _ALSO_TEARDOWN_SKIPPED % (message, test.message)
                if test.message
                else _TEARDOWN_SKIPPED % message
            )


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
        forbid_dtd(file)
        try:
            tests = read_output(file)
        except (ET.ParseError, ValueError) as exc:
            raise ParseError(f"{file}: not a parsable output.xml: {exc}") from None
        return [
            ParsedVerdict(
                native_name=name,
                status=_STATUS_MAP.get(status, "ERROR"),
                duration_ms=_duration_ms(status, elapsed),
                message=message or "",
            )
            for name, status, message, elapsed in tests
        ]
