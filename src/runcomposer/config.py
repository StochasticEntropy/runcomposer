"""Configuration (DESIGN.md §8): a single file with layered sections.

Rules: plugin sections are owned and validated by the plugin; the core
validates only ``core``; unknown active plugin ids fail startup loudly.
Plugin *selection* lives here — an entry-point name (the section key) or an
explicit ``module: "pkg.mod:Class"`` inside the section — never env vars.

**One path base.** Every relative path in the config file resolves against the
config file's own directory, so a config directory is portable and the same
``--config`` behaves identically from any working directory. The core applies
that to the keys it owns; for the keys it does not own it cannot — a plugin
section is opaque to the core, and guessing which of its values are paths
would be the core interpreting plugin config. Instead the plugin says so
itself, by exposing ``resolve_config_paths(options, resolve)``
(:meth:`Config._plugin_options`). A plugin without that hook gets its options
verbatim, exactly as before.

With no config file at all, runcomposer runs on built-in defaults: the sqlite
store in ``./runcomposer.db`` and the bundled demo corpus/taxonomy, so every
command works out of the box — there is no config file to anchor to, so the
base is the working directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Mapping

import yaml

from runcomposer.core.ports import RunStore, TestSource
from runcomposer.core.registry import (
    RUNNER_GROUP,
    SOURCE_GROUP,
    STORE_GROUP,
    PluginError,
    resolve_plugin,
)

__all__ = ["Config", "ConfigError", "load_config"]

_KNOWN_CORE_KEYS = {
    "api",
    "taxonomy_file",
    "artifact_dir",
    "retention",
    "ingestion",
    "locale_default",
}
_KNOWN_TOP_LEVEL = {"core", "store", "sources", "runners", "ui"}

_DEFAULT_API = {"host": "127.0.0.1", "port": 8100, "cors": []}


class ConfigError(ValueError):
    """Raised for invalid configuration — loudly, at startup."""


@dataclass
class Config:
    data: dict[str, Any]
    path: Path | None = None

    # -- core section (validated here) --------------------------------------

    @property
    def core(self) -> dict[str, Any]:
        return self.data.get("core") or {}

    @property
    def api(self) -> dict[str, Any]:
        return {**_DEFAULT_API, **(self.core.get("api") or {})}

    @property
    def locale_default(self) -> str:
        return self.core.get("locale_default", "en")

    @property
    def taxonomy_source(self) -> Any:
        """A readable for the taxonomy YAML: configured file, or the bundled
        demo taxonomy when none is configured."""
        configured = self.core.get("taxonomy_file")
        if configured:
            return self._resolve(configured)
        return resources.files("runcomposer.demo") / "taxonomy.yaml"

    @property
    def ingestion(self) -> dict[str, Any]:
        defaults = {
            "tokens": "required",
            "inbox": "results_inbox",
            "quarantine_dir": "quarantine",
            "quarantine_max": 100,
            "max_upload_mb": 200,
            "poll_interval_s": 2.0,
        }
        return {**defaults, **(self.core.get("ingestion") or {})}

    @property
    def retention(self) -> dict[str, Any]:
        return {"max_age_days": 90, **(self.core.get("retention") or {})}

    # -- path resolution (§8: one base — the config file's directory) --------

    @property
    def base_dir(self) -> Path:
        """The directory relative paths in this config resolve against: the
        config file's own directory, or the working directory when running on
        built-in defaults (there is no file to anchor to)."""
        return self.path.parent if self.path else Path(".")

    def resolve_path(self, value: Any) -> Any:
        """Anchor one configured relative path to :attr:`base_dir`.

        The public helper plugins are handed in ``resolve_config_paths``.
        Deliberately total and conservative — the *caller* decides what is a
        path, this only decides where a relative one points:

        - a non-string (``None``, a number, a list) is returned untouched;
        - an empty string is returned untouched (``inbox: ""`` is not a path);
        - an absolute path is returned untouched;
        - anything else is joined onto the base.

        Returns a ``str``, never a ``Path``: a resolved plugin option can end
        up in a runspec document, and those are serialized as YAML/JSON.
        """
        if not isinstance(value, str) or not value:
            return value
        if Path(value).is_absolute():
            return value
        return str(self.base_dir / value)

    def _resolve(self, relative: str) -> Path:
        return Path(self.resolve_path(relative))

    def _plugin_options(self, cls: Any, options: dict[str, Any]) -> dict[str, Any]:
        """Let a plugin anchor its own path options to the config file (§8).

        The core does not know which of a plugin's options are filesystem
        paths — that section is owned by the plugin, and several of them are
        deliberately *not* paths (a Robot listener spec, a shell hook, a base
        URL, sqlite's ``:memory:``). So the core does not guess. It offers:
        a plugin that defines ``resolve_config_paths(options, resolve)``
        receives a copy of its own options plus :meth:`resolve_path`, and
        returns the options it wants to be constructed with.

        Opt-in by design: a plugin written against 0.1.0 has no such
        attribute, so its options are passed through untouched and its
        behaviour is bit-for-bit what it was.
        """
        hook = getattr(cls, "resolve_config_paths", None)
        if hook is None:
            return options
        resolved = hook(dict(options), self.resolve_path)
        if not isinstance(resolved, dict):
            raise ConfigError(
                f"{getattr(cls, '__name__', cls)}.resolve_config_paths must return a dict "
                f"of options, got {type(resolved).__name__}"
            )
        return resolved

    @property
    def inbox_dir(self) -> Path | None:
        """The file-drop inbox (§5). ``inbox: null`` disables the watcher."""
        configured = self.ingestion["inbox"]
        return self._resolve(configured) if configured else None

    @property
    def quarantine_dir(self) -> Path:
        return self._resolve(self.ingestion["quarantine_dir"])

    @property
    def artifact_dir(self) -> Path:
        return self._resolve(self.core.get("artifact_dir") or "artifacts")

    @property
    def ui(self) -> dict[str, Any]:
        return self.data.get("ui") or {}

    # -- plugin sections (owned by the plugins) ------------------------------

    def _single_section(self, section: str, default: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        block = self.data.get(section) or default
        if not isinstance(block, Mapping) or len(block) != 1:
            raise ConfigError(
                f"'{section}' must configure exactly one active plugin, got {sorted(block)!r}"
                if isinstance(block, Mapping)
                else f"'{section}' must be a mapping"
            )
        ((plugin_id, options),) = block.items()
        return plugin_id, dict(options or {})

    def build_store(self) -> RunStore:
        plugin_id, options = self._single_section("store", {"sqlite": {"path": "runcomposer.db"}})
        cls = self._load(plugin_id, options, STORE_GROUP)
        return cls(**self._plugin_options(cls, options))

    def build_source(self) -> TestSource:
        plugin_id, options = self._single_section("sources", {"manifest": {}})
        cls = self._load(plugin_id, options, SOURCE_GROUP)
        options = self._plugin_options(cls, options)
        if plugin_id == "manifest" and not options.get("path"):
            # Zero-config default: the bundled demo corpus. Not a configured
            # path, so it is never resolved — it is a package resource.
            options = {**options, "path": resources.files("runcomposer.demo") / "corpus.json"}
        return cls(**options)

    def _raw_runner_options(self, runner_id: str) -> dict[str, Any]:
        runners = self.data.get("runners") or {}
        return dict(runners.get(runner_id) or {})

    def _runner(self, runner_id: str) -> tuple[Any, dict[str, Any]]:
        """The runner's class plus its resolved options. ``_load`` pops
        ``module``, so it gets a copy: the key stays in the options the
        caller sees, as it always has."""
        raw = self._raw_runner_options(runner_id)
        cls = self._load(runner_id, dict(raw), RUNNER_GROUP)
        return cls, self._plugin_options(cls, raw)

    def runner_options(self, runner_id: str) -> dict[str, Any]:
        """This runner's configured options, paths resolved (§8).

        Composing *for* a runner this installation does not have is
        legitimate — the export workflow hands the spec to an executor
        runcomposer never talks to (§6.2c) — so an unresolvable plugin is not
        an error here; it only means there is nobody to say which options are
        paths. ``build_runner``/``resolve_runner_class`` still fail loudly."""
        try:
            _cls, options = self._runner(runner_id)
        except ConfigError:
            return self._raw_runner_options(runner_id)
        return options

    def build_runner(self, runner_id: str) -> Any:
        cls, options = self._runner(runner_id)
        options.pop("module", None)
        return cls(**options)

    def resolve_runner_class(self, runner_id: str) -> Any:
        """Resolve without instantiating — the startup validation path."""
        cls, _options = self._runner(runner_id)
        return cls

    def configured_runner_ids(self) -> list[str]:
        configured = list((self.data.get("runners") or {}).keys())
        return configured or ["demo"]

    @staticmethod
    def _load(plugin_id: str, options: dict[str, Any], group: str) -> Any:
        """An explicit ``module:`` beats the entry-point lookup; the section
        key is the plugin id either way."""
        ref = options.pop("module", None) or plugin_id
        try:
            return resolve_plugin(ref, group)
        except PluginError as exc:
            raise ConfigError(f"unknown active plugin {plugin_id!r} in group {group!r}: {exc}") from exc


def load_config(path: str | None = None) -> Config:
    """Load config from ``path``, else ``./config.yaml`` if present, else
    built-in defaults."""
    if path is not None:
        file_path = Path(path)
        if not file_path.is_file():
            raise ConfigError(f"config file not found: {path}")
    else:
        candidate = Path("config.yaml")
        file_path = candidate if candidate.is_file() else None
    if file_path is None:
        return Config(data={})
    try:
        data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{file_path}: not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{file_path}: config must be a mapping")
    unknown_top = set(data) - _KNOWN_TOP_LEVEL
    if unknown_top:
        raise ConfigError(f"{file_path}: unknown top-level section(s): {sorted(unknown_top)}")
    unknown_core = set(data.get("core") or {}) - _KNOWN_CORE_KEYS
    if unknown_core:
        raise ConfigError(f"{file_path}: unknown key(s) in 'core': {sorted(unknown_core)}")
    return Config(data=data, path=file_path)
