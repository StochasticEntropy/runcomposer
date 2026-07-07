"""Configuration (DESIGN.md §8): a single file with layered sections.

Rules: plugin sections are owned and validated by the plugin; the core
validates only ``core``; unknown active plugin ids fail startup loudly.
Plugin *selection* lives here — an entry-point name (the section key) or an
explicit ``module: "pkg.mod:Class"`` inside the section — never env vars.

With no config file at all, runcomposer runs on built-in defaults: the sqlite
store in ``./runcomposer.db`` and the bundled demo corpus/taxonomy, so every
command works out of the box.
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
            base = self.path.parent if self.path else Path(".")
            return base / configured
        return resources.files("runcomposer.demo") / "taxonomy.yaml"

    @property
    def ingestion(self) -> dict[str, Any]:
        return {"tokens": "required", **(self.core.get("ingestion") or {})}

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
        return cls(**options)

    def build_source(self) -> TestSource:
        plugin_id, options = self._single_section("sources", {"manifest": {}})
        cls = self._load(plugin_id, options, SOURCE_GROUP)
        if plugin_id == "manifest" and not options.get("path"):
            # Zero-config default: the bundled demo corpus.
            options = {**options, "path": resources.files("runcomposer.demo") / "corpus.json"}
        return cls(**options)

    def runner_options(self, runner_id: str) -> dict[str, Any]:
        runners = self.data.get("runners") or {}
        return dict(runners.get(runner_id) or {})

    def build_runner(self, runner_id: str) -> Any:
        options = self.runner_options(runner_id)
        cls = self._load(runner_id, options, RUNNER_GROUP)
        return cls(**options)

    def resolve_runner_class(self, runner_id: str) -> Any:
        """Resolve without instantiating — the startup validation path."""
        return self._load(runner_id, self.runner_options(runner_id), RUNNER_GROUP)

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
