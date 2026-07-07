"""In-process plugin protocols (DESIGN.md §6).

P0 declares the two ports the skeleton exercises: TestSource and Runner.
RunStore (§6.3) arrives with P1 and ResultParser (§5) with P2 — they are not
declared here yet, so nothing can claim conformance to an unproven contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from .model import Item


@dataclass(frozen=True)
class RunnerInfo:
    """Runner identity + capability flags (e.g. live_status, cancel)."""

    id: str
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class DispatchHandle:
    """Returned by Runner.dispatch: dispatch id, declared shards, links."""

    dispatch_id: str
    shards: int = 1
    links: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class TestSource(Protocol):
    """Enumerates Items, produces a content-hashed catalog snapshot, and owns
    native-name→id resolution (DESIGN.md §6.1)."""

    provider_id: str

    def items(self) -> list[Item]: ...

    def snapshot(self) -> str: ...

    def resolve(self, native_name: str) -> str | None: ...


@runtime_checkable
class Runner(Protocol):
    """Fulfills a run spec. ``dispatch`` takes the document only (§6.2)."""

    def describe(self) -> RunnerInfo: ...

    def dispatch(self, spec: Mapping[str, Any]) -> DispatchHandle: ...
