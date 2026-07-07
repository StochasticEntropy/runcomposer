"""Plugin loading (DESIGN.md §6): import-path declarations and entry points.

Two first-class mechanisms, no env-var loading:

- a config declaration ``module: "mypkg.mod:MyClass"`` — the
  hack-it-in-an-afternoon, self-hosted path;
- Python entry points in the ``runcomposer.*`` groups — the
  packaged-distribution path.
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from typing import Any

SOURCE_GROUP = "runcomposer.sources"
RUNNER_GROUP = "runcomposer.runners"


class PluginError(RuntimeError):
    """Raised when a declared plugin cannot be loaded."""


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
        return ep.load()
    raise PluginError(f"no entry point {name!r} in group {group!r}")


def available_entry_points(group: str) -> dict[str, str]:
    return {ep.name: ep.value for ep in entry_points(group=group)}


def resolve_plugin(ref: str, group: str) -> Any:
    """A ref containing ':' is an import path; otherwise an entry-point name."""
    if ":" in ref:
        return load_import_path(ref)
    return load_entry_point(group, ref)
