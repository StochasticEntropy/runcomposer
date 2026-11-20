"""Configuration (DESIGN.md §8): one file, layered sections.

```
core:        validated here
store:       exactly one entry
sources:     one or more entries → one merged catalog
runners:     zero or more entries; the key IS the runner id
parsers:     options per result format id
observers:   zero or more entries
ui:          served to the UI verbatim
connectors:  free-form — a connector package's own settings, never interpreted
```

**Plugin sections share one spelling.** The key of an entry is its id. The
plugin class comes from ``module: "pkg.mod:Class"`` (an import path) or
``plugin: <entry-point name>`` inside the entry, and defaults to the key as
an entry-point name. So one plugin can be configured several times under
different keys, and every remaining key of the entry is an option handed to
the plugin's constructor.

**Options are checked at startup**, against the constructor's signature: an
unknown or missing option is a :class:`ConfigError` naming the accepted keys.
Every plugin is constructed once at startup (construction must be free of
side effects), so a typo cannot survive until the first dispatch.

**One path base.** Every relative path resolves against the config file's
directory. The core does that for its own keys; a plugin declares which of
its options are paths (``PATH_OPTIONS``), or resolves them itself
(``resolve_config_paths``) when the rule has exceptions.

**The contract is checked, not guessed.** Every plugin class must provide the
members of ``Configurable`` (``OPTIONS``, ``resolve_config_paths``) and every
built plugin the members of its role's protocol — ``TestSource``, ``Runner``,
``ResultParser``, ``RunStore``, ``RunObserver``. The base classes in
``runcomposer.sdk`` supply the optional ones. A missing member is a
:class:`ConfigError` at startup naming it; afterwards the members are called
directly, never looked up by attribute convention.

**Secrets.** A string option of the form ``${env:NAME}`` or
``${env:NAME:-default}`` is replaced from the environment when the plugin is
built. A missing variable is a :class:`ConfigError` naming it. Configured
options never travel into a run spec, an API response or a log line.

With no config file at all, runcomposer runs on built-in defaults — the
sqlite store in ``./runcomposer.db``, the bundled demo corpus and taxonomy,
and the ``demo`` runner — so every command works out of the box.
"""

from __future__ import annotations

import inspect
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from runcomposer.core.errors import ConfigError, PluginError
from runcomposer.core.ports import (
    Configurable,
    ResultParser,
    Runner,
    RunObserver,
    RunStore,
    TestSource,
    missing_members,
)
from runcomposer.core.registry import (
    OBSERVER_GROUP,
    PARSER_GROUP,
    RUNNER_GROUP,
    SOURCE_GROUP,
    STORE_GROUP,
    load_entry_point,
    load_import_path,
)

__all__ = ["REDACTED", "Config", "ConfigError", "PluginSpec", "load_config", "redact"]

_KNOWN_CORE_KEYS = {
    "api",
    "taxonomy_file",
    "artifact_dir",
    "retention",
    "ingestion",
    "dispatch",
    "locale_default",
}
_KNOWN_TOP_LEVEL = (
    "core",
    "store",
    "sources",
    "runners",
    "parsers",
    "observers",
    "ui",
    "connectors",
)
_PLUGIN_SECTIONS = {
    "store": STORE_GROUP,
    "sources": SOURCE_GROUP,
    "runners": RUNNER_GROUP,
    "parsers": PARSER_GROUP,
    "observers": OBSERVER_GROUP,
}
#: The protocol a built plugin of each section is checked against, and the
#: SDK base class that supplies the optional members of that role.
_ROLES: dict[str, tuple[type, str]] = {
    "store": (RunStore, "StoreBase"),
    "sources": (TestSource, "SourceBase"),
    "runners": (Runner, "RunnerBase"),
    "parsers": (ResultParser, "ParserBase"),
    "observers": (RunObserver, "ObserverBase"),
}
#: Keys of a plugin entry that select the plugin rather than configure it.
_SELECTOR_KEYS = ("module", "plugin")

_DEFAULT_API = {"host": "127.0.0.1", "port": 8100, "cors": []}
_DEFAULT_INGESTION = {
    "tokens": "required",
    "inbox": "results_inbox",
    "quarantine_dir": "quarantine",
    "quarantine_max": 100,
    "max_upload_mb": 200,
    "poll_interval_s": 2.0,
    "settle_s": None,
}
_DEFAULT_RETENTION = {"max_age_days": 90, "max_runs": None}
_DEFAULT_DISPATCH = {"max_concurrent": 2, "handoff_wait_s": 2.0}

#: What a missing config file means. Spelled as configuration, so the
#: zero-config mode is one more config and not a set of special cases.
_ZERO_CONFIG: dict[str, Any] = {
    "store": {"sqlite": {"path": "runcomposer.db"}},
    "sources": {"demo-corpus": {}},
    "runners": {"demo": {}},
}

_ENV_REF = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


@dataclass(frozen=True)
class PluginSpec:
    """One configured plugin: where it sits, what it is, how it is built."""

    section: str
    key: str
    cls: Any
    #: Options as written in the file (paths resolved, secrets NOT resolved).
    options: Mapping[str, Any] = field(default_factory=dict)

    @property
    def where(self) -> str:
        return f"{self.section}.{self.key}"


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
        return str(self.core.get("locale_default", "en"))

    @property
    def taxonomy_configured(self) -> bool:
        return bool(self.core.get("taxonomy_file"))

    @property
    def taxonomy_source(self) -> Any:
        """A readable for the taxonomy YAML: the configured file, or the
        bundled demo taxonomy when none is configured."""
        configured = self.core.get("taxonomy_file")
        if configured:
            return self._resolve(configured)
        return resources.files("runcomposer.demo") / "taxonomy.yaml"

    @property
    def ingestion(self) -> dict[str, Any]:
        return {**_DEFAULT_INGESTION, **(self.core.get("ingestion") or {})}

    @property
    def tokens_required(self) -> bool:
        return self.ingestion.get("tokens") != "disabled"

    @property
    def retention(self) -> dict[str, Any]:
        return {**_DEFAULT_RETENTION, **(self.core.get("retention") or {})}

    @property
    def dispatch(self) -> dict[str, Any]:
        return {**_DEFAULT_DISPATCH, **(self.core.get("dispatch") or {})}

    @property
    def ui(self) -> dict[str, Any]:
        return self.data.get("ui") or {}

    def connector_settings(self, name: str) -> dict[str, Any]:
        """A connector package's own settings: the ``connectors.<name>``
        mapping, which the core never interprets. Empty when absent."""
        settings = (self.data.get("connectors") or {}).get(name) or {}
        if not isinstance(settings, Mapping):
            raise ConfigError(f"connectors.{name} must be a mapping")
        return dict(settings)

    # -- path resolution (§8: one base — the config file's directory) --------

    @property
    def base_dir(self) -> Path:
        """Where relative paths resolve: the config file's directory, or the
        working directory when running on built-in defaults."""
        return self.path.parent if self.path else Path(".")

    def resolve_path(self, value: Any) -> Any:
        """Anchor one configured relative path to :attr:`base_dir`.

        Total and conservative — the *caller* decides what is a path, this
        only decides where a relative one points: non-strings, empty strings
        and absolute paths come back untouched; a list is resolved entry by
        entry. Returns ``str``, never ``Path``.
        """
        if isinstance(value, (list, tuple)):
            return [self.resolve_path(entry) for entry in value]
        if not isinstance(value, str) or not value:
            return value
        if Path(value).is_absolute():
            return value
        return str(self.base_dir / value)

    def _resolve(self, relative: str) -> Path:
        return Path(self.resolve_path(relative))

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

    # -- plugin sections -----------------------------------------------------

    def _section(self, section: str) -> dict[str, Any]:
        block = self.data.get(section)
        if block is None:
            return {}
        if not isinstance(block, Mapping):
            raise ConfigError(f"'{section}' must be a mapping of id → options")
        return dict(block)

    def plugin_specs(self, section: str) -> list[PluginSpec]:
        """The configured plugins of one section, classes resolved and path
        options anchored. Raises :class:`ConfigError` for an unloadable
        plugin — never constructs anything."""
        group = _PLUGIN_SECTIONS[section]
        specs = []
        for key, raw in self._section(section).items():
            if raw is not None and not isinstance(raw, Mapping):
                raise ConfigError(f"{section}.{key} must be a mapping of options")
            options = dict(raw or {})
            module_ref = options.pop("module", None)
            plugin_ref = options.pop("plugin", None)
            if module_ref and plugin_ref:
                raise ConfigError(
                    f"{section}.{key}: give 'module' (an import path) or 'plugin' "
                    "(an entry-point name), not both"
                )
            try:
                if module_ref:
                    cls = load_import_path(str(module_ref))
                else:
                    cls = load_entry_point(group, str(plugin_ref or key))
            except PluginError as exc:
                raise ConfigError(f"{section}.{key}: {exc}") from exc
            _require_members(cls, Configurable, f"{section}.{key}", section)
            specs.append(
                PluginSpec(
                    section=section, key=str(key), cls=cls, options=self._anchor(cls, options)
                )
            )
        return specs

    def plugin_spec(self, section: str, key: str) -> PluginSpec | None:
        for spec in self.plugin_specs(section):
            if spec.key == key:
                return spec
        return None

    def _anchor(self, cls: Any, options: dict[str, Any]) -> dict[str, Any]:
        """Resolve the options a plugin declares as paths (§8)."""
        resolved = cls.resolve_config_paths(dict(options), self.resolve_path)
        if not isinstance(resolved, dict):
            raise ConfigError(
                f"{_name(cls)}.resolve_config_paths must return a dict "
                f"of options, got {type(resolved).__name__}"
            )
        return resolved

    def build(self, spec: PluginSpec) -> Any:
        """Construct a configured plugin: options checked against the
        constructor, ``${env:…}`` references resolved, errors turned into
        :class:`ConfigError` naming the entry."""
        check_options(spec)
        options = {name: _resolve_env(value, spec.where) for name, value in spec.options.items()}
        secrets = _env_values(spec.options)

        def redacted(message: object) -> str:
            # A plugin that refuses a value tends to quote it. Whatever came
            # out of the environment does not get into an error message.
            return str(redact(str(message), secrets))

        try:
            plugin = spec.cls(**options)
        except ConfigError as exc:
            # The plugin's own refusal, kept as it is (type and message) —
            # with the entry it came from in front, since one plugin may be
            # configured under several keys.
            message = redacted(exc.args[0]) if exc.args else ""
            if not message.startswith(spec.where):
                message = f"{spec.where}: {message}"
            exc.args = (message, *exc.args[1:])
            raise
        except Exception as exc:
            raise ConfigError(
                f"{spec.where}: {type(exc).__name__}: {redacted(exc)}"
            ) from None
        _require_members(plugin, _ROLES[spec.section][0], spec.where, spec.section)
        return plugin

    # -- the single-entry sections --------------------------------------------

    def store_spec(self) -> PluginSpec:
        specs = self.plugin_specs("store")
        if len(specs) != 1:
            raise ConfigError(
                f"'store' must configure exactly one plugin, got {[s.key for s in specs]}"
            )
        return specs[0]

    def source_specs(self) -> list[PluginSpec]:
        specs = self.plugin_specs("sources")
        if not specs:
            raise ConfigError("'sources' must configure at least one plugin")
        return specs

    def runner_ids(self) -> list[str]:
        return [str(key) for key in self._section("runners")]

    def secret_values(self) -> frozenset[str]:
        """Every value this configuration takes from the environment
        (``${env:…}``), for blanking out of anything that is recorded or
        answered. Values shorter than four characters are not included —
        blanking those would mangle ordinary text."""
        found: set[str] = set()
        for section in _PLUGIN_SECTIONS:
            for raw in self._section(section).values():
                found |= _env_values(raw)
        return frozenset(found)

    def validate(self) -> None:
        """Resolve every configured plugin class and check every option name
        without constructing anything. :meth:`Service.validate` goes further
        and builds each one."""
        for section in _PLUGIN_SECTIONS:
            for spec in self.plugin_specs(section):
                check_options(spec)


# -- the contract and option checking ---------------------------------------------


def _name(cls: Any) -> str:
    return str(cls.__name__) if isinstance(cls, type) else repr(cls)


def _require_members(plugin: Any, protocol: type, where: str, section: str) -> None:
    """Refuse a plugin that does not provide every member of ``protocol``."""
    missing = missing_members(plugin, protocol)
    if missing:
        owner = plugin if isinstance(plugin, type) else type(plugin)
        base = _ROLES[section][1]
        raise ConfigError(
            f"{where}: {_name(owner)} does not provide {missing} of the "
            f"{protocol.__name__} contract — derive it from runcomposer.sdk.{base}, which "
            "supplies the optional members, or define them"
        )


def accepted_options(cls: Any) -> tuple[list[str], list[str], bool]:
    """``(accepted, required, open)`` for a plugin class: an explicit
    ``OPTIONS`` declaration, else the constructor's keyword parameters.
    ``open`` is true when the constructor takes ``**kwargs``."""
    declared = cls.OPTIONS
    if declared is not None:
        return list(declared), [], False
    try:
        signature = inspect.signature(cls)
    except (TypeError, ValueError):
        return [], [], True
    accepted, required, is_open = [], [], False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            is_open = True
        elif parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            accepted.append(parameter.name)
            if parameter.default is inspect.Parameter.empty:
                required.append(parameter.name)
    return accepted, required, is_open


def check_options(spec: PluginSpec) -> None:
    accepted, required, is_open = accepted_options(spec.cls)
    # `events` on an observer entry is read by the event bus (which kinds the
    # observer is told about), not by the observer.
    reserved = {"events"} if spec.section == "observers" else set()
    unknown = sorted(set(spec.options) - set(accepted) - reserved)
    if unknown and not is_open:
        raise ConfigError(
            f"{spec.where}: unknown option(s) {unknown} — "
            f"{_name(spec.cls)} accepts: {sorted(accepted) or 'none'}"
        )
    missing = sorted(set(required) - set(spec.options))
    if missing:
        raise ConfigError(
            f"{spec.where}: missing required option(s) {missing} — "
            f"{_name(spec.cls)} accepts: {sorted(accepted)}"
        )


def _resolve_env(value: Any, where: str) -> Any:
    """Replace ``${env:NAME}`` / ``${env:NAME:-default}`` in strings, at any
    depth of an option value."""
    if isinstance(value, str):

        def substitute(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(
                f"{where}: environment variable {name!r} is not set "
                f"(referenced as ${{env:{name}}}; use ${{env:{name}:-default}} to make it optional)"
            )

        return _ENV_REF.sub(substitute, value)
    if isinstance(value, Mapping):
        return {key: _resolve_env(entry, where) for key, entry in value.items()}
    if isinstance(value, (list, tuple)):
        return [_resolve_env(entry, where) for entry in value]
    return value


REDACTED = "‹from the environment›"


def redact(value: Any, secrets: frozenset[str] | set[str]) -> Any:
    """``value`` with every secret blanked out — in a string, and in the
    strings inside mappings and lists."""
    if not secrets:
        return value
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, Mapping):
        return {key: redact(entry, secrets) for key, entry in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(entry, secrets) for entry in value]
    return value


def _env_values(value: Any) -> set[str]:
    """Every value an option takes from the environment (``${env:…}``)."""
    found: set[str] = set()
    if isinstance(value, str):
        for match in _ENV_REF.finditer(value):
            resolved = os.environ.get(match.group(1), match.group(2) or "")
            if len(resolved) >= 4:  # shorter values would redact ordinary text
                found.add(resolved)
    elif isinstance(value, Mapping):
        for entry in value.values():
            found |= _env_values(entry)
    elif isinstance(value, (list, tuple)):
        for entry in value:
            found |= _env_values(entry)
    return found


# -- loading ----------------------------------------------------------------------


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load config from ``path``, else ``./config.yaml`` if present, else
    built-in defaults."""
    file_path: Path | None
    if path is not None:
        file_path = Path(path)
        if not file_path.is_file():
            raise ConfigError(f"config file not found: {path}")
    else:
        candidate = Path("config.yaml")
        file_path = candidate if candidate.is_file() else None
    if file_path is None:
        return Config(data={key: dict(value) for key, value in _ZERO_CONFIG.items()})
    try:
        data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{file_path}: not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{file_path}: config must be a mapping")
    unknown_top = sorted(set(data) - set(_KNOWN_TOP_LEVEL))
    if unknown_top:
        raise ConfigError(
            f"{file_path}: unknown top-level section(s) {unknown_top} — the sections are "
            f"{list(_KNOWN_TOP_LEVEL)}. Settings of your own connector package belong under "
            "'connectors:' (connectors.<name>: …), which runcomposer never interprets"
        )
    core = data.get("core") or {}
    if not isinstance(core, Mapping):
        raise ConfigError(f"{file_path}: 'core' must be a mapping")
    unknown_core = sorted(set(core) - _KNOWN_CORE_KEYS)
    if unknown_core:
        raise ConfigError(
            f"{file_path}: unknown key(s) in 'core': {unknown_core} "
            f"(known: {sorted(_KNOWN_CORE_KEYS)})"
        )
    if data.get("connectors") is not None and not isinstance(data["connectors"], Mapping):
        raise ConfigError(f"{file_path}: 'connectors' must be a mapping of name → settings")
    # A file that leaves the store or the catalog out gets the defaults for
    # those two; runners are never implied — only what is configured under
    # `runners:` can be dispatched to.
    data.setdefault("store", dict(_ZERO_CONFIG["store"]))
    data.setdefault("sources", dict(_ZERO_CONFIG["sources"]))
    config = Config(data=data, path=file_path)
    _check_core_values(config, file_path)
    return config


def _check_core_values(config: Config, file_path: Path) -> None:
    retention = config.retention
    for key in ("max_age_days", "max_runs"):
        value = retention.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
            raise ConfigError(
                f"{file_path}: core.retention.{key} must be a non-negative number or null "
                f"(null = no limit), got {value!r}"
            )
    dispatch = config.dispatch
    concurrent = dispatch.get("max_concurrent")
    if isinstance(concurrent, bool) or not isinstance(concurrent, int) or concurrent < 1:
        raise ConfigError(
            f"{file_path}: core.dispatch.max_concurrent must be an integer ≥ 1, got {concurrent!r}"
        )
    if config.ingestion.get("tokens") not in ("required", "disabled"):
        raise ConfigError(
            f"{file_path}: core.ingestion.tokens must be 'required' or 'disabled', "
            f"got {config.ingestion.get('tokens')!r}"
        )
