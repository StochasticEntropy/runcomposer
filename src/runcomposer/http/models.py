"""Request bodies (DESIGN.md §9), validated before anything acts on them.

Strict about types — a title is a string, a label value is a string — and
closed: a field these models do not know is a mistake in the request, not
something to drop silently.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "AttachRequest",
    "CompileRequest",
    "ComposeRequest",
    "DispatchRequest",
    "PromoteRequest",
    "RerunRequest",
    "SpecPreviewRequest",
]


class _Body(BaseModel):
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
    title: str = "Untitled run"
    labels: dict[str, str] = Field(default_factory=dict)
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
    title: str = "Untitled run"
    labels: dict[str, str] = Field(default_factory=dict)
    expect_format: str | None = Field(default=None, min_length=1)
    runner: dict[str, Any] | None = None


class CompileRequest(BaseModel):
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


class AttachRequest(_Body):
    run_id: str = Field(min_length=1)
    format: str | None = Field(default=None, min_length=1)


class PromoteRequest(_Body):
    format: str | None = Field(default=None, min_length=1)
