"""Core: domain model, filter grammar, selection compile, run-spec handling.

The core knows only the vocabulary of DESIGN.md §2. It imports no plugin and
contains no framework-specific concepts — guarded by a test.
"""

from .filter import BoolOp, FilterError, FilterNode, NotNode, TagPattern, format_filter, parse_filter
from .ids import new_ulid
from .lifecycle import COMPLETIONS, RUN_STATES, all_shards_delivered, completion_of
from .model import (
    VERDICT_STATUSES,
    DeliveryRecord,
    DispatchRecord,
    Item,
    RunRecord,
    Verdict,
)
from .ports import (
    DispatchHandle,
    ParsedVerdict,
    ResultParser,
    Runner,
    RunnerInfo,
    RunStore,
    TestSource,
)
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
    "COMPLETIONS",
    "DeliveryRecord",
    "DispatchHandle",
    "DispatchRecord",
    "FilterError",
    "FilterNode",
    "Item",
    "NotNode",
    "ParsedVerdict",
    "ResultParser",
    "RUN_STATES",
    "Runner",
    "RunnerInfo",
    "RunRecord",
    "RunStore",
    "SPEC_VERSION",
    "Selection",
    "SelectionError",
    "SpecLoadError",
    "TagPattern",
    "TestSource",
    "ValidationReport",
    "VERDICT_STATUSES",
    "Verdict",
    "all_shards_delivered",
    "build_spec",
    "completion_of",
    "format_filter",
    "load_document",
    "new_ulid",
    "parse_filter",
    "runspec_schema",
    "validate_document",
]
