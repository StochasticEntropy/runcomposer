"""Request bodies (DESIGN.md §9), validated before anything acts on them.

Strict about types — a title is a string, a label value is a string — and
closed: a field these models do not know is a mistake in the request, not
something to drop silently.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

__all__ = [
    "MAX_LABELS",
    "MAX_LABEL_KEY_LENGTH",
    "MAX_LABEL_VALUE_LENGTH",
    "MAX_TITLE_LENGTH",
    "AttachRequest",
    "CompileRequest",
    "ComposeRequest",
    "DispatchRequest",
    "LivePush",
    "LogPush",
    "PlanRequest",
    "PromoteRequest",
    "RerunRequest",
    "RunPlanRequest",
    "SpecPreviewRequest",
]


#: The longest title a request may give a run, in characters.
MAX_TITLE_LENGTH = 1000
#: How many labels a request may give a run, and how long a key and a value
#: may be, in characters: labels name a run (suite, branch, environment) —
#: a handful of short words, never data.
MAX_LABELS = 50
MAX_LABEL_KEY_LENGTH = 100
MAX_LABEL_VALUE_LENGTH = 500

#: A run's labels, bounded in number and length.
Labels = Annotated[
    dict[
        Annotated[str, Field(min_length=1, max_length=MAX_LABEL_KEY_LENGTH)],
        Annotated[str, Field(max_length=MAX_LABEL_VALUE_LENGTH)],
    ],
    Field(max_length=MAX_LABELS),
]


def _has_surrogate(text: str) -> bool:
    return any("\ud800" <= char <= "\udfff" for char in text)


def _lone_surrogate(value: Any) -> tuple[str, ...] | None:
    """The path of the first key or string in ``value`` holding a lone UTF-16
    surrogate (an escaped one in JSON) — text no store can write
    as UTF-8. Walked with a stack of its own, in document order: a body nested
    deeper than the interpreter recurses is still only read, and refused by
    whatever reads it next."""
    pending: list[tuple[Any, tuple[str, ...]]] = [(value, ())]
    while pending:
        current, path = pending.pop()
        if isinstance(current, str):
            if _has_surrogate(current):
                return path
        elif isinstance(current, dict):
            children: list[tuple[Any, tuple[str, ...]]] = []
            for key, item in current.items():
                if isinstance(key, str):
                    # A key is named with its surrogate escaped: the answer is text.
                    here = (*path, key.encode("utf-8", "backslashreplace").decode())
                    if _has_surrogate(key):
                        children.append((key, here))
                else:
                    here = path
                children.append((item, here))
            pending.extend(reversed(children))
        elif isinstance(current, (list, tuple)):
            pending.extend(
                reversed([(item, (*path, str(index))) for index, item in enumerate(current)])
            )
    return None


class _Text(BaseModel):
    """Every request body: text in it is text. A lone surrogate anywhere — in
    a value or a key, however deep — is a 400 naming the field, before any
    field is read."""

    @model_validator(mode="before")
    @classmethod
    def _no_lone_surrogates(cls, data: Any) -> Any:
        path = _lone_surrogate(data)
        if path is not None:
            raise PydanticCustomError(
                "lone_surrogate",
                "contains a lone UTF-16 surrogate, which is not text",
                {"field": ".".join(path)},
            )
        return data


class _Body(_Text):
    model_config = ConfigDict(extra="forbid", strict=True)


class DispatchRequest(_Body):
    """Either a configured ``runner`` (with its per-run ``parameters``), or
    ``mode: "export"`` — hand the document out for somebody else to execute."""

    runner: str | None = Field(default=None, min_length=1)
    parameters: dict[str, Any] | None = None
    mode: Literal["export"] | None = None
    format: Literal["json", "yaml"] = "json"

    @model_validator(mode="after")
    def _one_of(self) -> DispatchRequest:
        if (self.runner is None) == (self.mode is None):
            raise ValueError("give exactly one of 'runner' or 'mode: export'")
        if self.mode is not None and self.parameters is not None:
            raise ValueError("'parameters' belong to a runner, not to an export")
        return self


class ComposeRequest(_Body):
    selection: dict[str, Any]
    title: str = Field(default="Untitled run", max_length=MAX_TITLE_LENGTH)
    labels: Labels = Field(default_factory=dict)
    #: The result format expected back; defaults to the runner's, else the
    #: reference format.
    expect_format: str | None = Field(default=None, min_length=1)
    #: A free-form ``runner`` section for an external executor (§3: the one
    #: open section). In-process runners honour their declared parameters only.
    runner: dict[str, Any] | None = None
    dispatch: DispatchRequest | None = None
    #: Keep the composed run when the dispatch asked for with it is refused.
    keep_run: bool = False


class SpecPreviewRequest(_Body):
    selection: dict[str, Any]
    title: str = Field(default="Untitled run", max_length=MAX_TITLE_LENGTH)
    labels: Labels = Field(default_factory=dict)
    expect_format: str | None = Field(default=None, min_length=1)
    runner: dict[str, Any] | None = None


class CompileRequest(_Text):
    """The selection itself is the body (``tag_filter`` / ``item_ids`` /
    ``history``); ``limit`` bounds how many matched items are returned."""

    model_config = ConfigDict(extra="forbid")

    tag_filter: Any = None
    item_ids: list[str] | None = None
    history: Any = None
    limit: int | None = Field(default=None, ge=0)

    def selection(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.tag_filter is not None:
            data["tag_filter"] = self.tag_filter
        if self.item_ids:
            data["item_ids"] = self.item_ids
        if self.history:
            data["history"] = self.history
        return data


class RerunRequest(_Body):
    scope: Literal["all", "failed"] = "all"
    runner: str | None = Field(default=None, min_length=1)
    parameters: dict[str, Any] | None = None
    #: Title of the run a ``failed`` rerun composes; default: the source
    #: run's title. (That it is a rerun, and of what, is data in the answer
    #: and in the new run — not something to read out of a title.)
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_LENGTH)


class PlanRequest(_Body):
    """A plan preview for a selection that is not stored."""

    selection: dict[str, Any]
    runner: str = Field(min_length=1)
    parameters: dict[str, Any] | None = None
    title: str = Field(default="Plan preview", max_length=MAX_TITLE_LENGTH)
    labels: Labels = Field(default_factory=dict)


class RunPlanRequest(_Body):
    """A plan preview for a stored run. ``parameters`` left out means the
    ones the run's document carries for the runner."""

    runner: str = Field(min_length=1)
    parameters: dict[str, Any] | None = None


class _LiveUpdate(_Body):
    shard: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    #: ``RUNNING`` while the item executes, then how it ended.
    status: str
    duration_ms: int = Field(default=0, ge=0)
    message: str = ""


class LivePush(_Body):
    """Live status pushed by an external executor (the dispatch's ingest
    token authorizes it)."""

    dispatch: str | None = Field(default=None, min_length=1)
    updates: list[_LiveUpdate] = Field(min_length=1, max_length=1000)


class _LogLine(_Body):
    message: str
    level: Literal["debug", "info", "warning", "error"] = "info"


class LogPush(_Body):
    """Log lines pushed by an external executor for the run's event stream."""

    dispatch: str | None = Field(default=None, min_length=1)
    lines: list[_LogLine] = Field(min_length=1, max_length=500)


class AttachRequest(_Body):
    run_id: str = Field(min_length=1)
    format: str | None = Field(default=None, min_length=1)


class PromoteRequest(_Body):
    format: str | None = Field(default=None, min_length=1)
