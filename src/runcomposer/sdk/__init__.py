"""runcomposer.sdk — THE connector surface.

Everything a connector package may import from runcomposer is here — this
module, and its two toolkits ``runcomposer.sdk.robot`` and
``runcomposer.sdk.jenkins`` — and a connector imports from nowhere else in
the package: the reference plugins in ``runcomposer.plugins`` are held to
exactly that rule by a guard test, which is the proof that this surface is
sufficient.

The toolkits are the building blocks the reference ``robot-pool`` and
``jenkins`` runners are made of (exact-id selection, the live listener, the
shard worker and its supervisor; the Jenkins HTTP client), exported so that a
connector with needs of its own composes them instead of restating them.
Importing this module loads neither of them, and neither loads Robot
Framework until something is executed.

Stability: the names in ``__all__`` keep their meaning within a minor line
(0.2.x). Additions happen in minor releases; a removal or a changed signature
is a breaking change and is listed as one in CHANGELOG.md.

What is here, by role:

- **Protocols** — ``TestSource``, ``Runner``, ``ResultParser``,
  ``RunStore``, ``RunObserver``; ``Configurable`` (what every plugin class
  tells the config loader); and what runcomposer hands to a runner:
  ``DispatchContext``, ``LiveChannel``, ``History``. ``contract_members``
  lists a protocol's members — the list startup checks a plugin against.
- **Base classes** — ``SourceBase``, ``RunnerBase``, ``ParserBase``,
  ``StoreBase``, ``ObserverBase`` (all derived from ``PluginBase``): the
  defaults for every optional member, and ``OPTIONS`` / ``PATH_OPTIONS`` /
  ``resolve_config_paths`` for configuration. A plugin need not derive from
  them, but must then provide every member itself — startup refuses one
  that does not, naming what is missing.
- **Records** — ``Item``, ``Verdict``, ``LiveStatus``, ``ParsedVerdict``,
  ``RunRecord``, ``DispatchRecord``, ``DeliveryRecord``, ``ArtifactRef``,
  ``RunEvent``, ``RunPage``, ``RunnerInfo``, ``Parameter``, ``Health``,
  ``PlanPreview``.
- **Errors** — the taxonomy of ``runcomposer.core.errors``.
- **Helpers** — the chunk planner, content hashes, the marker reader, the
  defused XML reader, lifecycle functions, ids and timestamps,
  ``load_import_path`` (for a plugin's own ``"pkg.mod:callable"`` options)
  and ``RemoteLiveChannel`` (live status and log lines pushed to the API by
  an executor that runs elsewhere).
- **Embedding** — ``load_config`` and ``Service``, for driving runcomposer
  from a Python program instead of through the CLI or the HTTP API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from runcomposer.core.errors import (
    AuthRequired,
    ConfigError,
    Conflict,
    DispatchCancelled,
    DispatchRefused,
    Forbidden,
    IngestError,
    InvalidParameter,
    InvalidRequest,
    InvalidTransition,
    NotFound,
    ParseError,
    PayloadTooLarge,
    PluginError,
    RunActive,
    RunComposerError,
    SpecConflict,
    StoreError,
    UnknownRun,
    UnknownRunner,
)
from runcomposer.core.events import LIFECYCLE_KINDS
from runcomposer.core.filter import FilterError, format_filter, parse_filter
from runcomposer.core.handoff import corpus_drifted
from runcomposer.core.hashing import bundle_hash, canonical_json, sha256_hex, verdicts_hash
from runcomposer.core.ids import new_ulid
from runcomposer.core.lifecycle import (
    ACTIVE_STATES,
    COMPLETIONS,
    RUN_STATES,
    TERMINAL_STATES,
    completion_of,
    effective_verdicts,
    missing_verdicts,
    outstanding_shards,
    summarize,
)
from runcomposer.core.marker import MARKER_FILENAME, read_marker
from runcomposer.core.model import (
    LIVE_STATUSES,
    REPORTABLE_STATUSES,
    VERDICT_STATUSES,
    ArtifactRef,
    DeliveryRecord,
    DispatchRecord,
    Item,
    LiveStatus,
    RunEvent,
    RunPage,
    RunRecord,
    Verdict,
)
from runcomposer.core.parameters import Parameter, parameters_from_config, validate_parameters
from runcomposer.core.planning import ChunkPlan, plan_chunks, split_max
from runcomposer.core.ports import (
    Configurable,
    DispatchContext,
    Health,
    History,
    LiveChannel,
    ObserverBase,
    ParsedVerdict,
    ParserBase,
    PlanContext,
    PlanPreview,
    PluginBase,
    ResultParser,
    Runner,
    RunnerBase,
    RunnerInfo,
    RunObserver,
    RunStore,
    SourceBase,
    StoreBase,
    TestSource,
    contract_members,
)
from runcomposer.core.registry import ConfiguredCall, configured_calls, load_import_path
from runcomposer.core.remote import RemoteLiveChannel, ResultsPushRefused, push_results
from runcomposer.core.selection import Selection, SelectionError
from runcomposer.core.spec import SPEC_VERSION, load_document, validate_document
from runcomposer.core.timeutil import epoch_ms, normalize_time, parse_time, utc_now
from runcomposer.core.xmlsafe import forbid_dtd, parse_xml

if TYPE_CHECKING:  # the embedding surface is loaded on first use (below)
    from runcomposer.app.config import Config, load_config
    from runcomposer.app.service import Service

__all__ = [
    "ACTIVE_STATES",
    "COMPLETIONS",
    "LIFECYCLE_KINDS",
    "LIVE_STATUSES",
    "MARKER_FILENAME",
    "REPORTABLE_STATUSES",
    "RUN_STATES",
    "SPEC_VERSION",
    "TERMINAL_STATES",
    "VERDICT_STATUSES",
    "ArtifactRef",
    "AuthRequired",
    "ChunkPlan",
    "Config",
    "ConfigError",
    "Configurable",
    "ConfiguredCall",
    "Conflict",
    "DeliveryRecord",
    "DispatchCancelled",
    "DispatchContext",
    "DispatchRecord",
    "DispatchRefused",
    "FilterError",
    "Forbidden",
    "Health",
    "History",
    "IngestError",
    "InvalidParameter",
    "InvalidRequest",
    "InvalidTransition",
    "Item",
    "LiveChannel",
    "LiveStatus",
    "NotFound",
    "ObserverBase",
    "Parameter",
    "ParseError",
    "ParsedVerdict",
    "ParserBase",
    "PayloadTooLarge",
    "PlanContext",
    "PlanPreview",
    "PluginBase",
    "PluginError",
    "RemoteLiveChannel",
    "ResultParser",
    "ResultsPushRefused",
    "RunActive",
    "RunComposerError",
    "RunEvent",
    "RunObserver",
    "RunPage",
    "RunRecord",
    "RunStore",
    "Runner",
    "RunnerBase",
    "RunnerInfo",
    "Selection",
    "SelectionError",
    "Service",
    "SourceBase",
    "SpecConflict",
    "StoreBase",
    "StoreError",
    "TestSource",
    "UnknownRun",
    "UnknownRunner",
    "Verdict",
    "bundle_hash",
    "canonical_json",
    "completion_of",
    "configured_calls",
    "contract_members",
    "corpus_drifted",
    "effective_verdicts",
    "epoch_ms",
    "forbid_dtd",
    "format_filter",
    "load_config",
    "load_document",
    "load_import_path",
    "missing_verdicts",
    "new_ulid",
    "normalize_time",
    "outstanding_shards",
    "parameters_from_config",
    "parse_filter",
    "parse_time",
    "parse_xml",
    "plan_chunks",
    "push_results",
    "read_marker",
    "sha256_hex",
    "split_max",
    "summarize",
    "utc_now",
    "validate_document",
    "validate_parameters",
    "verdicts_hash",
]

# The embedding surface lives in the application layer, which a worker
# process running a plugin has no reason to import. Resolved on first access.
_EMBEDDING = {
    "Config": ("runcomposer.app.config", "Config"),
    "load_config": ("runcomposer.app.config", "load_config"),
    "Service": ("runcomposer.app.service", "Service"),
}


def __getattr__(name: str) -> Any:
    target = _EMBEDDING.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value
