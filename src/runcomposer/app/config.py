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
(``resolve_config_paths``) when the rule has exceptions. A path may come from
the environment: ``${env:…}`` is substituted *before* the path is anchored,
so ``root: ${env:SHOP_CHECKOUT}/tests`` with an absolute value stays where
the variable points.

**The contract is checked, not guessed.** Every plugin class must provide the
members of ``Configurable`` (``OPTIONS``, ``resolve_config_paths``) and every
built plugin the members of its role's protocol — ``TestSource``, ``Runner``,
``ResultParser``, ``RunStore``, ``RunObserver``. The base classes in
``runcomposer.sdk`` supply the optional ones. A missing member is a
:class:`ConfigError` at startup naming it; afterwards the members are called
directly, never looked up by attribute convention.

**Values from the environment.** A string option of the form
``${env:NAME}`` or ``${env:NAME:-default}`` is replaced from the environment
when the plugin is built; ``${env-public:NAME}`` is the same substitution for
a value that is not a secret. A missing variable is a :class:`ConfigError`
naming it. An environment value is text; an option the plugin's constructor
annotates as ``int``, ``float`` or ``bool`` receives it converted, and a
value that does not convert is a :class:`ConfigError` naming the option and
the variable. Configured options never travel into a run spec, an API
response or a log line.

**What is a secret.** The value of an ``${env:NAME}`` reference is, unless it
is used as a path (an option the plugin names in ``PATH_OPTIONS``) or as a
number or a boolean (it is converted, above) — or it is written
``${env-public:NAME}``. A ``:-default`` is written in the file and is no
secret. Secrets are blanked out of free text — messages, reasons, errors,
plan text and notes, health details, error documents, log lines — and out of
the string values of runner-defined data (health ``data``, plan ``data``);
never out of mapping keys or identifiers (shard, group and item names, ids,
states, kinds, parameter names, links) — ``app.redaction`` does the
blanking. A secret shorter than ``MIN_SECRET_LENGTH`` characters is refused
at startup: it could not be blanked out without mangling ordinary text.

With no config file at all, runcomposer runs on built-in defaults — the
sqlite store in ``./runcomposer.db``, the bundled demo corpus and taxonomy,
and the ``demo`` runner — so every command works out of the box.
"""

from __future__ import annotations

import inspect
import logging
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

from .hosts import parse_host_pattern
from .redaction import MIN_SECRET_LENGTH, redact
from .uiconfig import ui_selections

__all__ = [
    "Config",
    "ConfigError",
    "PluginSpec",
    "load_config",
    "option_types",
    "plugin_secrets",
    "resolve_options",
]

_KNOWN_CORE_KEYS = {
    "api",
    "taxonomy_file",
    "artifact_dir",
    "retention",
    "ingestion",
    "dispatch",
    "locale_default",
    "private_dir",
    "text_language",
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

_DEFAULT_API = {"host": "127.0.0.1", "port": 8100, "cors": [], "docs": False, "hosts": None}
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
_DEFAULT_DISPATCH = {
    "max_concurrent": 2,
    "handoff_wait_s": 2.0,
    "awaiting_timeout_s": None,
    "recovery_interval_s": 15.0,
}

#: What a missing config file means. Spelled as configuration, so the
#: zero-config mode is one more config and not a set of special cases.
_ZERO_CONFIG: dict[str, Any] = {
    "store": {"sqlite": {"path": "runcomposer.db"}},
    "sources": {"demo-corpus": {}},
    "runners": {"demo": {}},
}

#: A BCP 47 language tag (RFC 5646 ``langtag``, any case): language with
#: optional extlang, script, region, variants, extensions, private use.
_LANGUAGE_TAG = re.compile(
    r"(?:[a-z]{2,3}(?:-[a-z]{3}){0,3}|[a-z]{4}|[a-z]{5,8})"
    r"(?:-[a-z]{4})?"
    r"(?:-(?:[a-z]{2}|[0-9]{3}))?"
    r"(?:-(?:[a-z0-9]{5,8}|[0-9][a-z0-9]{3}))*"
    r"(?:-[0-9a-wy-z](?:-[a-z0-9]{2,8})+)*"
    r"(?:-x(?:-[a-z0-9]{1,8})+)?",
    re.IGNORECASE,
)

#: ``${env:NAME}`` (a secret), ``${env-public:NAME}`` (not one), each with an
#: optional ``:-default``. Groups: form, name, default.
_ENV_REF = re.compile(r"\$\{(env|env-public):([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_PUBLIC_FORM = "env-public"


@dataclass(frozen=True)
class PluginSpec:
    """One configured plugin: where it sits, what it is, how it is built."""

    section: str
    key: str
    cls: Any
    #: Options as written in the file (paths resolved, environment references
    #: NOT resolved — except inside a path option, where the reference is
    #: substituted before the path is anchored: a path is no secret).
    options: Mapping[str, Any] = field(default_factory=dict)
    #: The entry exactly as written, every reference still in place.
    written: Mapping[str, Any] = field(default_factory=dict)

    @property
    def where(self) -> str:
        return f"{self.section}.{self.key}"


@dataclass
class Config:
    """A loaded, validated configuration (:func:`load_config`): what a
    :class:`~runcomposer.app.service.Service` is built from."""

    data: dict[str, Any]
    path: Path | None = None
    #: Where relative paths resolve, fixed when the configuration is made:
    #: an absolute directory, so a path anchored to it means the same file
    #: after a change of working directory and in a worker process.
    _base_dir: Path = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.path is not None:
            # Made absolute, not resolved: a config file reached through a
            # symbolic link keeps the directory it was named in.
            self.path = Path(self.path).absolute()
        self._base_dir = self.path.parent if self.path is not None else Path.cwd()

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
    def text_language(self) -> str:
        """The language of the free text the server and its runners write —
        error details, health reasons, plan text (``core.text_language``, a
        BCP 47 tag; ``en`` by default). The UI marks such text with it."""
        return str(self.core.get("text_language", "en"))

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
        """The ``ui:`` section as a client reads it: every key verbatim,
        except the three that carry selections — ``quick_filters``,
        ``default_filter`` and ``presets`` — which are validated against the
        filter grammar and served in one normalized shape (see
        ``app.uiconfig.ui_selections``)."""
        section = self.data.get("ui") or {}
        if not isinstance(section, Mapping):
            raise ConfigError("'ui' must be a mapping")
        return {**section, **ui_selections(section)}

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
        working directory when running on built-in defaults — absolute
        either way, taken when the configuration was loaded."""
        return self._base_dir

    def resolve_path(self, value: Any) -> Any:
        """Anchor one configured relative path to :attr:`base_dir`.

        Total and conservative — the *caller* decides what is a path, this
        only decides where a relative one points: non-strings, empty strings
        and absolute paths come back untouched; a list is resolved entry by
        entry. A relative path comes back absolute, so a later change of
        working directory, or a worker process started elsewhere, finds the
        same file. Returns ``str``, never ``Path``.
        """
        if isinstance(value, (list, tuple)):
            return [self.resolve_path(entry) for entry in value]
        if not isinstance(value, str) or not value:
            return value
        if Path(value).is_absolute():
            return value
        return str(self.base_dir / value)

    def resolve_env_path(self, value: Any, where: str) -> Any:
        """:meth:`resolve_path` for a value that may reference the
        environment: the reference is resolved before the path is anchored. An
        absolute value then needs no anchoring, so a path that comes from a
        variable keeps pointing where the variable says."""
        return self.resolve_path(_resolve_env(value, where))

    def _resolve(self, relative: str, where: str = "core") -> Path:
        return Path(self.resolve_env_path(relative, where))

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
        return self._resolve(self.core.get("artifact_dir") or "artifacts", "core.artifact_dir")

    @property
    def private_dir(self) -> Path:
        """Where a runner keeps files of a dispatch that must never be
        downloadable (``ctx.private_dir``): outside the artifact directory,
        so nothing in it can be served."""
        return self._resolve(self.core.get("private_dir") or "private", "core.private_dir")

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
                    section=section,
                    key=str(key),
                    cls=cls,
                    options=self._anchor(cls, options, f"{section}.{key}"),
                    written=dict(options),
                )
            )
        return specs

    def plugin_spec(self, section: str, key: str) -> PluginSpec | None:
        for spec in self.plugin_specs(section):
            if spec.key == key:
                return spec
        return None

    def _anchor(self, cls: Any, options: dict[str, Any], where: str) -> dict[str, Any]:
        """Resolve the options a plugin declares as paths (§8) — environment
        references inside them first, so that the anchoring sees the path."""
        resolved = cls.resolve_config_paths(
            dict(options), lambda value: self.resolve_env_path(value, where)
        )
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
        options = resolve_options(spec)
        secrets = plugin_secrets(spec)
        connector = spec.cls.CONNECTOR
        if connector is not None:
            raw = self.connector_settings(str(connector))
            options[_CONNECTOR_KEYWORD] = _resolve_env(raw, f"connectors.{connector}")
            secrets |= _env_secrets(raw, f"connectors.{connector}")

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
        """Every secret this configuration takes from the environment, for
        blanking out of the free text that is recorded or answered: the
        values of ``${env:…}`` references — not of ``${env-public:…}``, not
        of a reference inside a path option, not of one an option converts to
        a number or a boolean (see the module's "What is a secret").

        Raises :class:`ConfigError` for a secret shorter than
        :data:`MIN_SECRET_LENGTH` characters."""
        found: set[str] = set()
        for section in _PLUGIN_SECTIONS:
            try:
                specs = self.plugin_specs(section)
            except ConfigError:
                # A plugin that cannot be loaded says nothing about which of
                # its options are paths or numbers: every reference counts.
                for key, raw in self._section(section).items():
                    found |= _env_secrets(raw, f"{section}.{key}")
                continue
            for spec in specs:
                found |= plugin_secrets(spec)
        for name, raw in self._section("connectors").items():
            found |= _env_secrets(raw, f"connectors.{name}")
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


#: The constructor keyword a plugin that declares ``CONNECTOR`` receives its
#: ``connectors.<name>`` mapping under — supplied by runcomposer, never
#: written in a plugin entry.
_CONNECTOR_KEYWORD = "connector_settings"


def check_options(spec: PluginSpec) -> None:
    accepted, required, is_open = accepted_options(spec.cls)
    if _CONNECTOR_KEYWORD in spec.options:
        raise ConfigError(
            f"{spec.where}: '{_CONNECTOR_KEYWORD}' is not an option you write — a plugin that "
            "declares CONNECTOR = '<name>' receives connectors.<name> under that keyword"
        )
    if spec.cls.CONNECTOR is not None:
        declared = spec.cls.OPTIONS is not None  # then the constructor is not inspected
        if not declared and _CONNECTOR_KEYWORD not in accepted and not is_open:
            raise ConfigError(
                f"{spec.where}: {_name(spec.cls)} declares CONNECTOR = {spec.cls.CONNECTOR!r} but "
                f"its constructor takes no '{_CONNECTOR_KEYWORD}' keyword"
            )
        accepted = [name for name in accepted if name != _CONNECTOR_KEYWORD]
        required = [name for name in required if name != _CONNECTOR_KEYWORD]
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
    """Replace ``${env:NAME}`` / ``${env-public:NAME}`` (each optionally
    with ``:-default``) in strings, at any depth of an option value."""
    if isinstance(value, str):

        def substitute(match: re.Match[str]) -> str:
            form, name, default = match.group(1), match.group(2), match.group(3)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(
                f"{where}: environment variable {name!r} is not set "
                f"(referenced as ${{{form}:{name}}}; use ${{{form}:{name}:-default}} to make it "
                "optional)"
            )

        return _ENV_REF.sub(substitute, value)
    if isinstance(value, Mapping):
        return {key: _resolve_env(entry, where) for key, entry in value.items()}
    if isinstance(value, (list, tuple)):
        return [_resolve_env(entry, where) for entry in value]
    return value


# -- converting environment text to the option's type -------------------------------

_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")


def option_types(cls: Any) -> dict[str, tuple[type, bool]]:
    """The options of a plugin class whose constructor annotation is ``int``,
    ``float`` or ``bool`` — optionally ``| None`` — as ``{name: (type,
    accepts_none)}``. An annotation that cannot be evaluated, or names
    anything else (``str``, a list, a union of several kinds), gives no
    entry: the value is handed over as written."""
    import typing
    from types import UnionType

    try:
        hints = typing.get_type_hints(cls.__init__)
    except Exception:
        return {}
    types: dict[str, tuple[type, bool]] = {}
    for name, hint in hints.items():
        if name == "return":
            continue
        origin = typing.get_origin(hint)
        members = typing.get_args(hint) if origin in (typing.Union, UnionType) else (hint,)
        concrete = [member for member in members if member is not type(None)]
        if len(concrete) == 1 and concrete[0] in (int, float, bool):
            types[name] = (concrete[0], len(concrete) < len(members))
    return types


def _references(value: Any) -> list[str]:
    """The variable names a written option value references, in order."""
    if isinstance(value, str):
        return [match.group(2) for match in _ENV_REF.finditer(value)]
    return []


def _convert(text: str, kind: type, accepts_none: bool, where: str, variables: list[str]) -> Any:
    """Environment text as the option's annotated type, or a ConfigError
    naming the option and the variable(s). The value is not quoted: an
    option that does not convert may have been handed the wrong variable."""
    named = "environment variable " + ", ".join(repr(name) for name in variables)
    stripped = text.strip()
    if accepts_none and stripped == "":
        return None
    try:
        if kind is bool:
            if stripped.lower() in _TRUE:
                return True
            if stripped.lower() in _FALSE:
                return False
            raise ValueError(stripped)
        if kind is int:
            return int(stripped)
        return float(stripped)
    except ValueError:
        expected = {bool: "true or false", int: "an integer", float: "a number"}[kind]
        raise ConfigError(
            f"{where}: the option takes {expected}, and the value of {named} is not "
            f"{expected} — an environment value is text, converted to the type the plugin "
            "declares for the option"
        ) from None


def resolve_options(spec: PluginSpec) -> dict[str, Any]:
    """A plugin entry's options as its constructor receives them: every
    environment reference substituted, and an option the constructor
    annotates as ``int`` / ``float`` / ``bool`` converted when its value
    came from the environment."""
    types = option_types(spec.cls)
    resolved: dict[str, Any] = {}
    for name, value in spec.options.items():
        substituted = _resolve_env(value, spec.where)
        variables = _references(value)
        if variables and name in types and isinstance(substituted, str):
            substituted = _convert(substituted, *types[name], f"{spec.where}.{name}", variables)
        resolved[name] = substituted
    return resolved


# -- secrets ---------------------------------------------------------------------------

def _env_secrets(value: Any, where: str) -> set[str]:
    """The secrets an option value takes from the environment: the values
    of its ``${env:…}`` references, at any depth. ``${env-public:…}`` is no
    secret, and neither is a ``:-default`` — it is written in the file. A
    secret shorter than :data:`MIN_SECRET_LENGTH` is refused."""
    found: set[str] = set()
    if isinstance(value, str):
        for match in _ENV_REF.finditer(value):
            form, name = match.group(1), match.group(2)
            resolved = os.environ.get(name)
            if form == _PUBLIC_FORM or not resolved:
                continue
            if len(resolved) < MIN_SECRET_LENGTH:
                raise ConfigError(
                    f"{where}: the value of environment variable {name!r} is {len(resolved)} "
                    f"character(s) long — a secret that short cannot be blanked out of what is "
                    f"recorded and answered without mangling ordinary text, so it is refused "
                    f"(a secret needs at least {MIN_SECRET_LENGTH}). If the value is not a "
                    f"secret, write ${{env-public:{name}}}"
                )
            found.add(resolved)
    elif isinstance(value, Mapping):
        for key, entry in value.items():
            found |= _env_secrets(entry, f"{where}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, entry in enumerate(value):
            found |= _env_secrets(entry, f"{where}[{index}]")
    return found


def plugin_secrets(spec: PluginSpec) -> set[str]:
    """The secrets of one plugin entry. A path option arrives with its
    references already substituted (a path is no secret), and an option
    converted to a number or a boolean is none either."""
    types = option_types(spec.cls)
    found: set[str] = set()
    for name, value in spec.options.items():
        if name in types and _references(value):
            continue
        found |= _env_secrets(value, f"{spec.where}.{name}")
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
    try:
        _ = config.ui
    except ConfigError as exc:
        raise ConfigError(f"{file_path}: {exc}") from None
    # A configured filter string read only by the fallback for single
    # patterns works, and says once, at load, how to write it as text.
    notes: list[str] = []
    ui_selections(config.data.get("ui") or {}, notes)
    for note in notes:
        logging.getLogger(__name__).warning("%s: %s", file_path, note)
    language = config.core.get("text_language", "en")
    if not isinstance(language, str) or not _LANGUAGE_TAG.fullmatch(language):
        raise ConfigError(
            f"{file_path}: core.text_language must be a BCP 47 language tag — the language "
            f"the server and its runners write free text in, such as 'en', 'de' or 'de-CH' — "
            f"got {language!r}"
        )
    artifact_dir, private_dir = config.artifact_dir.resolve(), config.private_dir.resolve()
    if private_dir == artifact_dir or artifact_dir in private_dir.parents:
        raise ConfigError(
            f"{file_path}: core.private_dir ({private_dir}) must lie outside core.artifact_dir "
            f"({artifact_dir}) — what is inside the artifact directory can be served"
        )
    dispatch = config.dispatch
    expiry = dispatch.get("awaiting_timeout_s")
    if expiry is not None and (
        isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or expiry <= 0
    ):
        raise ConfigError(
            f"{file_path}: core.dispatch.awaiting_timeout_s must be a positive number of "
            f"seconds or null (null = wait until someone finalizes or cancels), got {expiry!r}"
        )
    recovery = dispatch.get("recovery_interval_s")
    if recovery is not None and (
        isinstance(recovery, bool) or not isinstance(recovery, (int, float)) or recovery <= 0
    ):
        raise ConfigError(
            f"{file_path}: core.dispatch.recovery_interval_s must be a positive number of "
            f"seconds or null (null = recover only when the server starts), got {recovery!r}"
        )
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
    if not isinstance(config.api.get("docs"), bool):
        raise ConfigError(
            f"{file_path}: core.api.docs must be true or false (serve /docs and /redoc), "
            f"got {config.api.get('docs')!r}"
        )
    _check_api_hosts(config.api.get("hosts"), file_path)


def _check_api_hosts(hosts: Any, file_path: Path) -> None:
    """``core.api.hosts``: null, or a non-empty list of host entries."""
    if hosts is None:
        return
    if not isinstance(hosts, list) or not hosts:
        raise ConfigError(
            f"{file_path}: core.api.hosts must be a non-empty list of the host names the server "
            f"answers to (such as runcomposer.shop.example, *.shop.example, localhost:8100), "
            f"or null for every name, got {hosts!r}"
        )
    for index, entry in enumerate(hosts):
        if not isinstance(entry, str):
            raise ConfigError(f"{file_path}: core.api.hosts[{index}] must be a string, got {entry!r}")
        try:
            parse_host_pattern(entry)
        except ValueError as exc:
            raise ConfigError(f"{file_path}: core.api.hosts[{index}] {entry!r}: {exc}") from None
