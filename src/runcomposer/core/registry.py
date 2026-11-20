"""Plugin loading (DESIGN.md §6): import-path declarations and entry points.

Two first-class mechanisms, no env-var loading:

- a config declaration ``module: "mypkg.mod:MyClass"`` — the
  hack-it-in-an-afternoon, self-hosted path;
- Python entry points in the ``runcomposer.*`` groups — the
  packaged-distribution path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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
    "ConfiguredCall",
    "PluginError",
    "PluginInfo",
    "configured_calls",
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


@dataclass(frozen=True)
class ConfiguredCall:
    """A callable named in configuration, together with the settings the
    configuration gives it.

    Wherever a plugin option takes an import path — a hook, a provider, a
    check — it takes two spellings::

        pre_dispatch:
          - "shop_connector.hooks:reserve"                  # the callable
          - call: "shop_connector.hooks:reserve"             # the callable …
            with: { pool: staging, hold_minutes: 30 }        # … and its settings

    Called like the callable itself. With a ``with:`` mapping the callable
    additionally receives it as the keyword argument ``settings`` — so one
    function serves several entries, and nothing it needs to know has to be
    hard-coded or smuggled in through the environment. Without ``with:``
    nothing is added to the call (a callable that takes no ``settings`` keeps
    working). Picklable: it travels to worker processes as ``ref`` +
    ``settings`` and is imported where it is called.
    """

    ref: str
    settings: Mapping[str, Any] | None = None
    #: The option it was configured under — for error messages.
    where: str = field(default="", compare=False)

    @classmethod
    def parse(cls, entry: Any, where: str) -> ConfiguredCall:
        """Accept ``"pkg.mod:callable"`` or ``{call: …, with: {…}}``; anything
        else is a :class:`ConfigError` naming ``where``. The shape is checked
        here; the import happens at the first call."""
        from .errors import ConfigError

        if isinstance(entry, ConfiguredCall):
            return entry
        settings: Mapping[str, Any] | None = None
        ref: Any = entry
        if isinstance(entry, Mapping):
            unknown = sorted(set(entry) - {"call", "with"})
            if unknown or "call" not in entry:
                raise ConfigError(
                    f"{where}: a configured callable is 'package.module:callable' or "
                    f"{{call: 'package.module:callable', with: {{…}}}}, got keys {sorted(entry)}"
                )
            ref = entry["call"]
            if entry.get("with") is not None:
                if not isinstance(entry["with"], Mapping):
                    raise ConfigError(f"{where}: 'with' must be a mapping of settings")
                settings = dict(entry["with"])
        module, sep, attr = str(ref).partition(":") if isinstance(ref, str) else ("", "", "")
        if not sep or not module or not attr:
            raise ConfigError(
                f"{where}: a configured callable is an import path "
                f"'package.module:callable', got {ref!r}"
            )
        return cls(ref=str(ref), settings=settings, where=where)

    def load(self) -> Any:
        """Import the callable. A missing module or attribute, or something
        that is not callable, is a :class:`PluginError` naming the option."""
        try:
            target = load_import_path(self.ref)
        except PluginError as exc:
            raise PluginError(f"{self.where or self.ref}: {exc}") from None
        if not callable(target):
            raise PluginError(f"{self.where or self.ref}: {self.ref!r} is not callable")
        return target

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self.settings is not None:
            kwargs["settings"] = dict(self.settings)
        return self.load()(*args, **kwargs)


def configured_calls(entries: Any, where: str) -> tuple[ConfiguredCall, ...]:
    """A list option of configured callables (``None`` → none); each entry
    in either spelling."""
    from .errors import ConfigError

    if entries is None:
        return ()
    if isinstance(entries, (str, Mapping)) or not isinstance(entries, Sequence):
        raise ConfigError(f"{where} must be a list of configured callables")
    return tuple(
        ConfiguredCall.parse(entry, f"{where}[{index}]") for index, entry in enumerate(entries)
    )


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
