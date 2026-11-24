"""Per-run runner parameters (DESIGN.md §6.2): what a runner lets the person
starting a run decide, declared as data so an API can validate it and a UI
can render a form from it.

A value that is not declared here cannot reach a runner through a run spec —
that is the rule that keeps a document from reconfiguring the machine it is
executed on.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import ConfigError, InvalidParameter

__all__ = ["PARAMETER_KINDS", "Parameter", "parameters_from_config", "validate_parameters"]

PARAMETER_KINDS = (
    "string", "text", "integer", "boolean", "choice", "multi_choice", "mapping",
)
#: The kinds an optional ``pattern`` applies to.
_PATTERN_KINDS = ("string", "text", "mapping")
#: The kinds an optional ``key_pattern`` applies to.
_KEY_PATTERN_KINDS = ("mapping",)
#: The most characters a value may have unless its declaration says
#: otherwise (``max_length``): a ``string`` is one line, a ``text`` a few
#: pages; for a ``mapping`` the limit is each value's. A per-run value is
#: stored with the run and shown with it — it is no place for data.
DEFAULT_MAX_LENGTH = {"string": 1000, "text": 100_000, "mapping": 1000}
#: A ``mapping``'s bounds, whatever its declaration: names and entries.
MAX_MAPPING_KEY_LENGTH = 200
MAX_MAPPING_ENTRIES = 100


@dataclass(frozen=True)
class Parameter:
    """One per-run option.

    Kinds: ``string`` (one line), ``text`` (several lines — a form renders a
    text area), ``integer``, ``boolean``, ``choice`` and ``multi_choice``
    (drawn from ``choices``; a ``multi_choice`` value is a list) and
    ``mapping`` (string → string — name/value pairs such as extra variables
    of a test framework).

    ``pattern`` is an optional regular expression the whole value must match
    (``re.fullmatch``): the value of a ``string``, the whole of a ``text``,
    every *value* of a ``mapping``. ``key_pattern`` is the same for every
    *key* (name) of a ``mapping``. Both are checked when the run is requested
    — not inside the runner — and the error names the parameter (and the
    offending key).

    A value is bounded (problem ``too-long``): ``max_length`` characters for
    a ``string``, a ``text`` and each value of a ``mapping`` — ``None`` takes
    the kind's default (:data:`DEFAULT_MAX_LENGTH`: 1000, 100 000, 1000);
    a ``mapping`` holds at most 100 names of at most 200 characters; a
    ``multi_choice`` names each choice at most once.
    """

    name: str
    kind: str = "string"
    label: str = ""
    help: str = ""
    default: Any = None
    choices: tuple[str, ...] = ()
    required: bool = False
    pattern: str | None = None
    key_pattern: str | None = None
    max_length: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in PARAMETER_KINDS:
            raise ValueError(
                f"parameter {self.name!r}: kind must be one of {PARAMETER_KINDS}, got {self.kind!r}"
            )
        if self.max_length is not None:
            if self.kind not in DEFAULT_MAX_LENGTH:
                raise ValueError(
                    f"parameter {self.name!r}: 'max_length' applies to the kinds "
                    f"{tuple(DEFAULT_MAX_LENGTH)}, not to {self.kind!r}"
                )
            if isinstance(self.max_length, bool) or not isinstance(self.max_length, int) or self.max_length < 1:
                raise ValueError(
                    f"parameter {self.name!r}: max_length must be an integer ≥ 1, got {self.max_length!r}"
                )
        self._check_expression("pattern", self.pattern, _PATTERN_KINDS)
        self._check_expression("key_pattern", self.key_pattern, _KEY_PATTERN_KINDS)

    def _check_expression(
        self, field_name: str, expression: str | None, kinds: tuple[str, ...]
    ) -> None:
        if expression is None:
            return
        if self.kind not in kinds:
            raise ValueError(
                f"parameter {self.name!r}: '{field_name}' applies to the kinds {kinds}, "
                f"not to {self.kind!r}"
            )
        try:
            re.compile(expression)
        except re.error as exc:
            raise ValueError(
                f"parameter {self.name!r}: {field_name} {expression!r} is not a regular "
                f"expression ({exc})"
            ) from None

    def check(self, value: Any) -> Any:
        """Return the accepted value, or raise :class:`InvalidParameter`
        naming this parameter."""

        def bad(expected: str, problem: str = "type") -> InvalidParameter:
            return InvalidParameter(
                f"parameter {self.name!r} must be {expected}, got {value!r}", parameter=self.name,
                problem=problem,
            )

        if self.kind == "string":
            if not isinstance(value, str):
                raise bad("a string")
            if "\n" in value or "\r" in value:
                raise bad("a single line (kind 'text' takes several)")
            self._bound(value)
            self._match(value)
        elif self.kind == "text":
            if not isinstance(value, str):
                raise bad("text")
            self._bound(value)
            self._match(value)
        elif self.kind == "mapping":
            if not isinstance(value, Mapping) or not all(
                isinstance(key, str) and key and isinstance(entry, str)
                for key, entry in value.items()
            ):
                raise bad("a mapping of names to strings")
            if len(value) > MAX_MAPPING_ENTRIES:
                raise InvalidParameter(
                    f"parameter {self.name!r} holds {len(value)} names — at most {MAX_MAPPING_ENTRIES}",
                    parameter=self.name, problem="too-long",
                )
            for key, entry in value.items():
                if len(key) > MAX_MAPPING_KEY_LENGTH:
                    raise InvalidParameter(
                        f"parameter {self.name!r}: a name of {len(key)} characters — at most "
                        f"{MAX_MAPPING_KEY_LENGTH}",
                        parameter=self.name, problem="too-long", key=key[:MAX_MAPPING_KEY_LENGTH],
                    )
                self._bound(entry, f"[{key!r}]", key)
            for key, entry in value.items():
                if self.key_pattern is not None and re.fullmatch(self.key_pattern, key) is None:
                    raise InvalidParameter(
                        f"parameter {self.name!r}: key {key!r} does not match the key pattern "
                        f"'{self.key_pattern}'",  # as written, not as a repr doubles a backslash
                        parameter=self.name, problem="key-pattern", key=key,
                    )
                self._match(entry, f"[{key!r}]", key)
            return dict(value)
        elif self.kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise bad("an integer")
        elif self.kind == "boolean":
            if not isinstance(value, bool):
                raise bad("true or false")
        elif self.kind == "choice":
            if not isinstance(value, str) or value not in self.choices:
                raise bad(f"one of {list(self.choices)}", "choice")
        elif self.kind == "multi_choice":
            if (
                not isinstance(value, (list, tuple))
                or not all(isinstance(entry, str) for entry in value)
                or any(entry not in self.choices for entry in value)
                or len(set(value)) != len(value)
            ):
                raise bad(f"a list drawn from {list(self.choices)}, each at most once", "choice")
            return list(value)
        return value

    def _bound(self, value: str, where: str = "", key: str | None = None) -> None:
        limit = self.max_length or DEFAULT_MAX_LENGTH[self.kind]
        if len(value) > limit:
            # Named by its length, never quoted: the value is what is too big.
            raise InvalidParameter(
                f"parameter {self.name!r}{where} has {len(value)} characters — at most {limit}",
                parameter=self.name, problem="too-long", key=key,
            )

    def _match(self, value: str, where: str = "", key: str | None = None) -> None:
        if self.pattern is not None and re.fullmatch(self.pattern, value) is None:
            raise InvalidParameter(
                f"parameter {self.name!r}{where}: {value!r} does not match the pattern "
                f"'{self.pattern}'",  # as written, not as a repr doubles a backslash
                parameter=self.name, problem="pattern", key=key,
            )


def validate_parameters(
    declared: Sequence[Parameter], values: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Check submitted values against the declaration and fill in defaults.

    Unknown name, wrong type, value outside the choices, missing required
    value → :class:`InvalidParameter` naming the parameter.
    """
    given = dict(values or {})
    by_name = {parameter.name: parameter for parameter in declared}
    for name in given:
        if name not in by_name:
            raise InvalidParameter(
                f"unknown parameter {name!r} (this runner accepts: {sorted(by_name) or 'none'})",
                parameter=name, problem="unknown",
            )
    accepted: dict[str, Any] = {}
    for parameter in declared:
        if parameter.name in given:
            accepted[parameter.name] = parameter.check(given[parameter.name])
        elif parameter.required:
            raise InvalidParameter(
                f"parameter {parameter.name!r} is required", parameter=parameter.name,
                problem="required",
            )
        elif parameter.default is not None:
            accepted[parameter.name] = parameter.default
    return accepted


_PARAMETER_KEYS = (
    "name", "kind", "label", "help", "default", "choices", "required", "pattern", "key_pattern",
    "max_length",
)


def parameters_from_config(raw: Sequence[Any] | None, where: str) -> tuple[Parameter, ...]:
    """Build parameter declarations from configuration — for a runner whose
    per-run parameters are themselves configured (a list of mappings with the
    fields of :class:`Parameter`). ``where`` names the option in errors."""
    parameters = []
    for index, entry in enumerate(raw or ()):
        if not isinstance(entry, Mapping) or not entry.get("name"):
            raise ConfigError(f"{where}[{index}] must be a mapping with a 'name'")
        unknown = sorted(set(entry) - set(_PARAMETER_KEYS))
        if unknown:
            raise ConfigError(
                f"{where}[{index}] has unknown key(s) {unknown} (known: {list(_PARAMETER_KEYS)})"
            )
        try:
            parameters.append(
                Parameter(
                    name=str(entry["name"]),
                    kind=str(entry.get("kind", "string")),
                    label=str(entry.get("label", "")),
                    help=str(entry.get("help", "")),
                    default=entry.get("default"),
                    choices=tuple(str(choice) for choice in entry.get("choices") or ()),
                    required=bool(entry.get("required", False)),
                    pattern=None if entry.get("pattern") is None else str(entry["pattern"]),
                    key_pattern=(
                        None if entry.get("key_pattern") is None else str(entry["key_pattern"])
                    ),
                    max_length=entry.get("max_length"),
                )
            )
            if entry.get("default") is not None:
                parameters[-1].check(entry["default"])
        except (ValueError, InvalidParameter) as exc:
            raise ConfigError(f"{where}[{index}]: {exc}") from None
    return tuple(parameters)
