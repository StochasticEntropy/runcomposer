"""core.parameters (DESIGN.md §6.2): per-run runner parameters declared as
data, validated before they can reach a runner."""

from __future__ import annotations

import pytest

from runcomposer.core.errors import ConfigError, InvalidParameter
from runcomposer.core.parameters import (
    PARAMETER_KINDS,
    Parameter,
    parameters_from_config,
    validate_parameters,
)

DECLARED = (
    Parameter("seed", kind="string", default="demo"),
    Parameter("retries", kind="integer"),
    Parameter("allow_drift", kind="boolean", default=False),
    Parameter("browser", kind="choice", choices=("firefox", "chromium")),
    Parameter("partitions", kind="multi_choice", choices=("env1", "env2"), default=["env1", "env2"]),
)


class TestDeclaration:
    def test_the_seven_kinds(self):
        assert PARAMETER_KINDS == (
            "string", "text", "integer", "boolean", "choice", "multi_choice", "mapping",
        )

    def test_an_unknown_kind_is_refused_at_declaration(self):
        with pytest.raises(ValueError, match="kind must be one of"):
            Parameter("seed", kind="float")

    def test_a_pattern_must_be_a_regular_expression_and_fit_the_kind(self):
        with pytest.raises(ValueError, match=r"parameter 'sku': pattern '\[' is not a regular expression"):
            Parameter("sku", pattern="[")
        with pytest.raises(ValueError, match="'pattern' applies to the kinds"):
            Parameter("retries", kind="integer", pattern=r"\d+")


class TestTextMappingAndPattern:
    """The kinds beyond the scalar ones, and validation by pattern — all of
    it checked when the run is requested, with the parameter named."""

    DECLARED = (
        Parameter("release_notes", kind="text"),
        Parameter("variables", kind="mapping", pattern=r"[^\n]*"),
        Parameter("coupon", pattern=r"[A-Z]{4}\d{2}"),
        Parameter("headers", kind="mapping", default={"X-Shop-Region": "eu"}),
    )

    def test_text_takes_several_lines_and_a_string_does_not(self):
        accepted = validate_parameters(self.DECLARED, {"release_notes": "line one\nline two"})
        assert accepted["release_notes"] == "line one\nline two"
        with pytest.raises(InvalidParameter, match="parameter 'coupon' must be a single line") as caught:
            validate_parameters(self.DECLARED, {"coupon": "SAVE10\nSAVE20"})
        assert caught.value.parameter == "coupon"

    def test_a_mapping_is_names_to_strings_and_comes_back_as_a_dict(self):
        accepted = validate_parameters(self.DECLARED, {"variables": {"BROWSER": "firefox", "LOCALE": "de"}})
        assert accepted["variables"] == {"BROWSER": "firefox", "LOCALE": "de"}
        assert accepted["headers"] == {"X-Shop-Region": "eu"}, "a mapping default applies like any other"

    @pytest.mark.parametrize("value", ["BROWSER=firefox", ["BROWSER"], {"RETRIES": 3}, {"": "x"}, {1: "x"}])
    def test_anything_else_is_refused_naming_the_parameter(self, value):
        with pytest.raises(InvalidParameter, match="parameter 'variables' must be a mapping of names to strings") as caught:
            validate_parameters(self.DECLARED, {"variables": value})
        assert caught.value.parameter == "variables"

    def test_a_pattern_must_match_the_whole_value(self):
        assert validate_parameters(self.DECLARED, {"coupon": "SAVE10"})["coupon"] == "SAVE10"
        with pytest.raises(InvalidParameter, match=r"parameter 'coupon': 'SAVE100' does not match the pattern") as caught:
            validate_parameters(self.DECLARED, {"coupon": "SAVE100"})
        assert caught.value.parameter == "coupon"

    def test_a_mapping_pattern_is_checked_per_value_and_names_the_key(self):
        with pytest.raises(InvalidParameter, match=r"parameter 'variables'\['NOTE'\]: 'two\\nlines' does not match") as caught:
            validate_parameters(self.DECLARED, {"variables": {"BROWSER": "firefox", "NOTE": "two\nlines"}})
        assert caught.value.parameter == "variables"

    def test_from_config_takes_the_kinds_and_the_pattern_and_checks_the_default(self):
        (variables, coupon) = parameters_from_config(
            [
                {"name": "variables", "kind": "mapping", "default": {"BROWSER": "firefox"}},
                {"name": "coupon", "pattern": "[A-Z]+", "default": "SAVE"},
            ],
            "runner: parameters",
        )
        assert (variables.kind, variables.default, coupon.pattern) == ("mapping", {"BROWSER": "firefox"}, "[A-Z]+")
        with pytest.raises(ConfigError, match=r"parameters\[0\]: parameter 'coupon': 'save' does not match"):
            parameters_from_config([{"name": "coupon", "pattern": "[A-Z]+", "default": "save"}], "runner: parameters")
        with pytest.raises(ConfigError, match=r"parameters\[0\]: parameter 'coupon': pattern '\(' is not a regular"):
            parameters_from_config([{"name": "coupon", "pattern": "("}], "runner: parameters")


class TestAcceptedValues:
    def test_every_kind_accepts_its_own_type(self):
        accepted = validate_parameters(
            DECLARED,
            {
                "seed": "nightly-2",
                "retries": 3,
                "allow_drift": True,
                "browser": "firefox",
                "partitions": ["env2"],
            },
        )
        assert accepted == {
            "seed": "nightly-2",
            "retries": 3,
            "allow_drift": True,
            "browser": "firefox",
            "partitions": ["env2"],
        }

    def test_defaults_fill_in_and_absent_optionals_stay_absent(self):
        assert validate_parameters(DECLARED, {}) == {
            "seed": "demo",
            "allow_drift": False,
            "partitions": ["env1", "env2"],
        }
        assert validate_parameters(DECLARED, None) == validate_parameters(DECLARED, {})

    def test_a_multi_choice_value_comes_back_as_a_list(self):
        accepted = validate_parameters(DECLARED, {"partitions": ("env1",)})
        assert accepted["partitions"] == ["env1"]

    def test_no_declaration_accepts_nothing_and_needs_nothing(self):
        assert validate_parameters((), None) == {}


class TestRefusals:
    """Every refusal is an InvalidParameter that names the parameter."""

    def refused(self, values, declared=DECLARED) -> InvalidParameter:
        with pytest.raises(InvalidParameter) as caught:
            validate_parameters(declared, values)
        return caught.value

    def test_unknown_name_lists_what_is_accepted(self):
        error = self.refused({"sed": "x"})
        assert error.parameter == "sed"
        assert "unknown parameter 'sed'" in str(error) and "'seed'" in str(error)
        assert error.code == "invalid-parameter"

    def test_unknown_name_when_nothing_is_declared(self):
        assert "accepts: none" in str(self.refused({"seed": "x"}, declared=()))

    @pytest.mark.parametrize(
        ("name", "value", "expected"),
        [
            ("seed", 5, "a string"),
            ("retries", "3", "an integer"),
            ("retries", True, "an integer"),  # a bool is not a count
            ("retries", 1.5, "an integer"),
            ("allow_drift", "true", "true or false"),
            ("allow_drift", 1, "true or false"),
            ("browser", "safari", "one of ['firefox', 'chromium']"),
            ("browser", ["firefox"], "one of"),
            ("partitions", "env1", "a list drawn from ['env1', 'env2']"),
            ("partitions", ["env1", "env9"], "a list drawn from"),
            ("partitions", [1], "a list drawn from"),
        ],
    )
    def test_ill_typed_values_name_the_parameter(self, name, value, expected):
        error = self.refused({name: value})
        assert error.parameter == name
        assert f"parameter {name!r} must be {expected}" in str(error)

    def test_a_required_parameter_must_be_given(self):
        declared = (Parameter("target", kind="string", required=True),)
        error = self.refused({}, declared=declared)
        assert error.parameter == "target" and "is required" in str(error)
        assert validate_parameters(declared, {"target": "staging"}) == {"target": "staging"}


class TestFromConfig:
    def test_builds_declarations_from_mappings(self):
        parameters = parameters_from_config(
            [
                {"name": "browser", "kind": "choice", "choices": ["firefox", "chromium"],
                 "label": "Browser", "help": "which one", "default": "firefox"},
                {"name": "ticket", "required": True},
            ],
            "command runner: parameters",
        )
        assert parameters[0] == Parameter(
            "browser", kind="choice", label="Browser", help="which one", default="firefox",
            choices=("firefox", "chromium"),
        )
        assert parameters[1] == Parameter("ticket", kind="string", required=True)

    def test_nothing_configured_is_no_parameters(self):
        assert parameters_from_config(None, "x") == ()
        assert parameters_from_config([], "x") == ()

    @pytest.mark.parametrize("entry", ["browser", {}, {"kind": "string"}, {"name": ""}])
    def test_an_entry_needs_a_name(self, entry):
        with pytest.raises(ConfigError, match=r"jenkins: run_parameters\[0\] must be a mapping"):
            parameters_from_config([entry], "jenkins: run_parameters")

    def test_an_unknown_key_is_named_with_the_known_ones(self):
        with pytest.raises(ConfigError, match=r"\[1\] has unknown key\(s\) \['choises'\]") as caught:
            parameters_from_config(
                [{"name": "seed"}, {"name": "browser", "choises": ["firefox"]}], "runner: parameters"
            )
        assert "'choices'" in str(caught.value)

    def test_an_unknown_kind_is_a_config_error_naming_the_entry(self):
        with pytest.raises(ConfigError, match=r"parameters\[0\]: parameter 'seed': kind must be"):
            parameters_from_config([{"name": "seed", "kind": "float"}], "runner: parameters")
