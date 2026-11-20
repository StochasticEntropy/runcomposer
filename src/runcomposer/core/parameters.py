"""Per-run runner parameters (DESIGN.md §6.2): what a runner lets the person
starting a run decide, declared as data so an API can validate it and a UI
can render a form from it.

A value that is not declared here cannot reach a runner through a run spec —
that is the rule that keeps a document from reconfiguring the machine it is
executed on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import ConfigError, InvalidParameter

__all__ = ["PARAMETER_KINDS", "Parameter", "parameters_from_config", "validate_parameters"]

PARAMETER_KINDS = ("string", "integer", "boolean", "choice", "multi_choice")


@dataclass(frozen=True)
class Parameter:
    """One per-run option. ``choices`` applies to ``choice`` and
    ``multi_choice``; a ``multi_choice`` value is a list."""

    name: str
    kind: str = "string"
    label: str = ""
    help: str = ""
    default: Any = None
    choices: tuple[str, ...] = ()
    required: bool = False

    def __post_init__(self) -> None:
        if self.kind not in PARAMETER_KINDS:
            raise ValueError(
                f"parameter {self.name!r}: kind must be one of {PARAMETER_KINDS}, got {self.kind!r}"
            )

    def check(self, value: Any) -> Any:
        """Return the accepted value, or raise :class:`InvalidParameter`
        naming this parameter."""

        def bad(expected: str) -> InvalidParameter:
            return InvalidParameter(
                f"parameter {self.name!r} must be {expected}, got {value!r}", parameter=self.name
            )

        if self.kind == "string":
            if not isinstance(value, str):
                raise bad("a string")
        elif self.kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise bad("an integer")
        elif self.kind == "boolean":
            if not isinstance(value, bool):
                raise bad("true or false")
        elif self.kind == "choice":
            if not isinstance(value, str) or value not in self.choices:
                raise bad(f"one of {list(self.choices)}")
        elif self.kind == "multi_choice":
            if (
                not isinstance(value, (list, tuple))
                or not all(isinstance(entry, str) for entry in value)
                or any(entry not in self.choices for entry in value)
            ):
                raise bad(f"a list drawn from {list(self.choices)}")
            return list(value)
        return value


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
                parameter=name,
            )
    accepted: dict[str, Any] = {}
    for parameter in declared:
        if parameter.name in given:
            accepted[parameter.name] = parameter.check(given[parameter.name])
        elif parameter.required:
            raise InvalidParameter(
                f"parameter {parameter.name!r} is required", parameter=parameter.name
            )
        elif parameter.default is not None:
            accepted[parameter.name] = parameter.default
    return accepted


_PARAMETER_KEYS = ("name", "kind", "label", "help", "default", "choices", "required")


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
                )
            )
        except ValueError as exc:
            raise ConfigError(f"{where}[{index}]: {exc}") from None
    return tuple(parameters)
