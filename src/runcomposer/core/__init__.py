"""Core: domain model, filter grammar, selection compile, run-spec handling.

The core knows only the vocabulary of DESIGN.md §2. It imports no plugin and
contains no framework-specific concepts — guarded by a test.
"""

from .filter import BoolOp, FilterError, FilterNode, NotNode, TagPattern, format_filter, parse_filter
from .ids import new_ulid
from .model import VERDICT_STATUSES, Item, Verdict
from .ports import DispatchHandle, Runner, RunnerInfo, TestSource
from .selection import Selection, SelectionError
from .spec import (
    SPEC_VERSION,
    SpecLoadError,
    ValidationReport,
    build_spec,
    load_document,
    runspec_schema,
    validate_document,
)

__all__ = [
    "BoolOp",
    "DispatchHandle",
    "FilterError",
    "FilterNode",
    "Item",
    "NotNode",
    "Runner",
    "RunnerInfo",
    "SPEC_VERSION",
    "Selection",
    "SelectionError",
    "SpecLoadError",
    "TagPattern",
    "TestSource",
    "ValidationReport",
    "VERDICT_STATUSES",
    "Verdict",
    "build_spec",
    "format_filter",
    "load_document",
    "new_ulid",
    "parse_filter",
    "runspec_schema",
    "validate_document",
]
