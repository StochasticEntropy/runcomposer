"""Plugin loading (DESIGN.md §6): import-path declarations and entry points.

Two first-class mechanisms, no env-var loading:

- a config declaration ``module: "mypkg.mod:MyClass"`` — the
  hack-it-in-an-afternoon, self-hosted path;
- Python entry points in the ``runcomposer.*`` groups — the
  packaged-distribution path.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import entry_points
from typing import Any

from .errors import PluginError

__all__ = [
    "GROUPS",
    "OBSERVER_GROUP",
    "PARSER_GROUP",
    "RUNNER_GROUP",
    "SOURCE_GROUP",
    "STORE_GROUP",
    "PluginError",
    "PluginInfo",
    "installed_plugins",
    "load_entry_point",
    "load_import_path",
]

SOURCE_GROUP = "runcomposer.sources"
RUNNER_GROUP = "runcomposer.runners"
STORE_GROUP = "runcomposer.stores"
PARSER_GROUP = "runcomposer.parsers"
OBSERVER_GROUP = "runcomposer.observers"
GROUPS = (SOURCE_GROUP, RUNNER_GROUP, STORE_GROUP, PARSER_GROUP, OBSERVER_GROUP)


@dataclass(frozen=True)
class PluginInfo:
    """One installed entry-point plugin and the distribution it came from."""

    group: str
    name: str
    target: str
    distribution: str
    version: str


def load_import_path(ref: str) -> Any:
    """Load ``"package.module:Attribute"``."""
    module_name, sep, attr = ref.partition(":")
    if not sep or not module_name or not attr:
        raise PluginError(f"import path must look like 'package.module:ClassName', got {ref!r}")
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise PluginError(f"cannot import plugin module {module_name!r}: {exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError:
        raise PluginError(f"module {module_name!r} has no attribute {attr!r}") from None


def load_entry_point(group: str, name: str) -> Any:
    for ep in entry_points(group=group, name=name):
        try:
            return ep.load()
        except ImportError as exc:
            raise PluginError(
                f"entry point {name!r} in group {group!r} cannot be imported: {exc}"
            ) from exc
    raise PluginError(
        f"no entry point {name!r} in group {group!r} "
        f"(installed: {sorted(ep.name for ep in entry_points(group=group)) or 'none'})"
    )


def installed_plugins(group: str | None = None) -> list[PluginInfo]:
    """Every entry-point plugin installed in this environment — which
    distribution provides it, at which version."""
    found = []
    for candidate in GROUPS if group is None else (group,):
        for ep in entry_points(group=candidate):
            dist = getattr(ep, "dist", None)
            found.append(
                PluginInfo(
                    group=candidate,
                    name=ep.name,
                    target=ep.value,
                    distribution=dist.name if dist is not None else "",
                    version=dist.version if dist is not None else "",
                )
            )
    return sorted(found, key=lambda info: (info.group, info.name))
