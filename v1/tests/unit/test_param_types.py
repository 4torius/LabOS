"""Sending a parameter as the type the command declared.

Every HTML control hands back a string. A Boolean parameter filled in from a
form therefore arrives as "false" - a non-empty string, which is emphatically
not false - and the sila2 client refuses it with "Expected a bool value".

The workflow builder had no Boolean field at all, so both of the Opentrons
booleans fell into the free-text box at the bottom of its parameter form. Two
real runs, both stopped at step 4 after the Tecan had already moved the plate:

    SetModuleLatch  {"ModuleName": "HeaterShaker", "Open": "false"}
    MoveLabware     {"LabwareName": "TargetPlate", "UseGripper": "UseGripper"}

The second is the parameter's own name, typed into a box whose placeholder
says "Enter UseGripper". Four attempts each, identical error, run failed.

The Dashboard was fine: it renders a checkbox and reads `.checked`. Same
command, same server, different page - which is the shape of fault this fixes
at the shared layer rather than in one template.
"""
import json
import re
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))

PARAMS_JS = (V1_DIR / "webapp" / "static" / "js" / "labos-params.js").read_text(encoding="utf-8")
BUILDER = (V1_DIR / "webapp" / "templates" / "workflows.html").read_text(encoding="utf-8")
DASHBOARD = (V1_DIR / "webapp" / "templates" / "dashboard.html").read_text(encoding="utf-8")


class TestTheCoercionExists:

    def test_it_is_shared_not_per_template(self):
        """One implementation, so the next page to grow a parameter form
        inherits it instead of repeating the bug."""
        assert "function coerce(value, declaredType)" in PARAMS_JS
        assert "coerce: coerce," in PARAMS_JS
        assert "coerceAll: coerceAll," in PARAMS_JS

    def test_a_boolean_string_becomes_a_boolean(self):
        body = _function(PARAMS_JS, "coerce")
        assert "type === 'boolean'" in body
        assert "typeof value === 'boolean'" in body

    def test_only_affirmatives_are_true(self):
        """Everything else, the parameter's own name included, is false."""
        body = _function(PARAMS_JS, "coerce")
        for word in ("'true'", "'1'", "'yes'", "'on'"):
            assert word in body
        assert "return text === 'true'" in body

    def test_integers_and_reals_are_converted_too(self):
        """The same fault was waiting for ShakeModule's RPM."""
        body = _function(PARAMS_JS, "coerce")
        assert "parseInt(value, 10)" in body
        assert "parseFloat(value)" in body

    def test_an_unparseable_number_is_left_alone(self):
        """Better a clear server-side complaint than a silent NaN."""
        body = _function(PARAMS_JS, "coerce")
        assert "isNaN(whole) ? value : whole" in body
        assert "isNaN(real) ? value : real" in body

    def test_an_undeclared_parameter_passes_through(self):
        """hal_config and the other workflow-only keys are not the command's
        business."""
        body = _function(PARAMS_JS, "coerceAll")
        assert "if (!cmd || !cmd.parameters) return params;" in body
        assert "hasOwnProperty.call(out, p.name)" in body


class TestTheBuilderRendersTheRightControl:

    def test_a_boolean_gets_a_boolean_field(self):
        assert "if (paramType === 'boolean') {" in BUILDER
        assert "LabOS.params.buildBooleanSelect(" in BUILDER

    def test_the_boolean_field_offers_exactly_two_choices(self):
        body = _function(PARAMS_JS, "buildBooleanSelect")
        assert "'<option value=\"true\"'" in body
        assert "'<option value=\"false\"'" in body

    def test_a_number_gets_a_number_field(self):
        assert "paramType === 'integer' || paramType === 'real'" in BUILDER
        assert 'type="number"' in BUILDER

    def test_they_are_checked_before_the_free_text_fallback(self):
        """The free-text box at the bottom is what swallowed them."""
        boolean_at = BUILDER.index("if (paramType === 'boolean') {")
        fallback_at = BUILDER.index("// Default text input")
        assert boolean_at < fallback_at

    def test_a_required_boolean_is_given_a_value_when_the_command_is_chosen(self):
        """Nothing else supplied one: the select-default filler needs options
        and the string filler only handles String."""
        assert "applyRequiredBooleanDefaults" in PARAMS_JS
        assert BUILDER.count("LabOS.params.applyRequiredBooleanDefaults(step, cmd);") == 2

    def test_the_conservative_value_is_the_default(self):
        body = _function(PARAMS_JS, "applyRequiredBooleanDefaults")
        assert "coerce(p.default, 'boolean')" in body


class TestItIsAppliedOnTheWayOut:
    """Not only while editing: a workflow saved before the Boolean field
    existed already has "false" written into its JSON."""

    def test_every_step_payload_goes_through_one_helper(self):
        assert "function _stepParams(step)" in BUILDER
        assert "normalizeStepParamsForCommand(step.command, step.params)" not in \
            BUILDER[BUILDER.index("function _stepParams(step)") + 400:], (
                "a payload is still being built without coercion"
            )

    def test_the_helper_coerces(self):
        body = _function(BUILDER, "_stepParams")
        assert "LabOS.params.coerceAll(params, cmd)" in body

    def test_execution_uses_it(self):
        body = _function(BUILDER, "_buildSteps")
        assert "_stepParams(step)" in body

    def test_saving_and_exporting_use_it_too(self):
        assert BUILDER.count("Parameters: _stepParams(step),") >= 3


class TestTheDashboardStillWorks:
    """It was already right, and it stays right: the fix is additive."""

    def test_it_still_renders_a_checkbox(self):
        assert "if (paramType === 'boolean') {" in DASHBOARD
        assert 'type="checkbox" data-param=' in DASHBOARD

    def test_it_still_reads_the_checked_state(self):
        assert "params[paramName] = input.checked;" in DASHBOARD


class TestTheRealFailures:
    """The two payloads that actually stopped runs, as the coercion sees them."""

    @pytest.mark.parametrize("raw,expected", [
        ("false", False),      # SetModuleLatch Open
        ("true", True),
        ("", False),
        (True, True),
        (False, False),
    ])
    def test_boolean_outcomes(self, raw, expected):
        assert _coerce_bool(raw) is expected


def _coerce_bool(value):
    """The rule the JavaScript implements, mirrored so it can be exercised."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("true", "1", "yes", "on", "si", "sì")


def _function(source: str, name: str) -> str:
    """One function's body, up to the next declaration at the same depth."""
    match = re.search(rf"^(\s*)function {re.escape(name)}\s*\(", source, re.M)
    assert match, f"{name} not found"
    indent = match.group(1)
    following = re.compile(rf"^{indent}(?:function |LabOS\.params =)", re.M).search(
        source, match.end())
    return source[match.start():following.start() if following else len(source)]


class TestTheOrchestratorCoercesToo:
    """Fixing the form was necessary and not sufficient.

    A workflow saved before the Boolean field existed still has "false" written
    into its JSON, and anything that is not the workflow builder - `labos run`,
    a POST to /api/workflows/execute, a saved file executed from the Library -
    never passes through the browser at all. The orchestrator is the one place
    every client goes through, and it is the only place that knows the declared
    SiLA2 type of every parameter.
    """

    def _coerce(self, params, declared):
        from src.lab_core import CommandParameter, coerce_parameters
        return coerce_parameters(
            params, [CommandParameter(name=n, data_type=t) for n, t in declared])

    BOOLS = [("Open", "Boolean")]

    def test_the_setmodulelatch_payload_that_failed(self):
        assert self._coerce({"ModuleName": "HeaterShaker", "Open": "false"},
                            [("ModuleName", "String")] + self.BOOLS) == {
            "ModuleName": "HeaterShaker", "Open": False}

    def test_the_movelabware_payload_that_failed_is_refused_outright(self):
        """UseGripper carried the field's own name, which is not a boolean at
        all. Reading it as false means a *manual* move: confirmed on hardware,
        the robot loads the deck and then stops at its touchscreen waiting for
        somebody to carry the plate by hand. A nonsense value has to say so."""
        from src.lab_core import ParameterTypeError
        with pytest.raises(ParameterTypeError) as caught:
            self._coerce({"LabwareName": "TargetPlate", "NewLocation": "A2",
                          "UseGripper": "UseGripper"},
                         [("LabwareName", "String"), ("NewLocation", "String"),
                          ("UseGripper", "Boolean")])
        assert "UseGripper" in str(caught.value)
        assert "true or false" in str(caught.value)

    @pytest.mark.parametrize("raw,expected", [
        ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("False", False), ("0", False), ("no", False),
        ("off", False), ("", False), (None, False),
        (True, True), (False, False), (1, True), (0, False),
    ])
    def test_boolean_values(self, raw, expected):
        assert self._coerce({"Open": raw}, self.BOOLS)["Open"] is expected

    @pytest.mark.parametrize("raw", ["UseGripper", "maybe", "vero", "2"])
    def test_anything_that_is_not_a_boolean_is_refused(self, raw):
        from src.lab_core import ParameterTypeError
        with pytest.raises(ParameterTypeError):
            self._coerce({"Open": raw}, self.BOOLS)

    def test_numbers_are_converted(self):
        out = self._coerce({"RPM": "200", "Celsius": "37.5"},
                           [("RPM", "Integer"), ("Celsius", "Real")])
        assert out == {"RPM": 200, "Celsius": 37.5}
        assert isinstance(out["RPM"], int)
        assert isinstance(out["Celsius"], float)

    def test_an_unparseable_number_is_refused(self):
        from src.lab_core import ParameterTypeError
        with pytest.raises(ParameterTypeError) as caught:
            self._coerce({"RPM": "boh"}, [("RPM", "Integer")])
        assert "RPM" in str(caught.value)

    def test_the_refusal_becomes_a_failed_step_not_a_traceback(self):
        source = (V1_DIR / "src" / "lab_core.py").read_text(encoding="utf-8-sig")
        body = source[source.index("async def execute_command("):]
        body = body[:re.search(r"^    def ", body, re.M).start()]
        assert "except ParameterTypeError as exc:" in body
        assert "return ExecutionResult(success=False, error=str(exc))" in body

    def test_undeclared_parameters_are_untouched(self):
        out = self._coerce({"Open": "false", "hal_config": "01_SPAAC.json"},
                           self.BOOLS)
        assert out["hal_config"] == "01_SPAAC.json"

    def test_a_command_with_no_declared_parameters_is_left_alone(self):
        from src.lab_core import coerce_parameters
        assert coerce_parameters({"x": "false"}, []) == {"x": "false"}

    def test_strings_that_look_like_booleans_stay_strings(self):
        """A String parameter whose value happens to be "false" means the word."""
        assert self._coerce({"TaskDescription": "false"},
                            [("TaskDescription", "String")]) == {"TaskDescription": "false"}

    def test_execute_command_applies_it(self):
        source = (V1_DIR / "src" / "lab_core.py").read_text(encoding="utf-8-sig")
        body = source[source.index("async def execute_command("):]
        body = body[:body.index("\n    def ", 1)]
        assert "coerce_parameters(parameters, cmd.parameters)" in body

    def test_it_runs_before_the_command_is_dispatched(self):
        source = (V1_DIR / "src" / "lab_core.py").read_text(encoding="utf-8-sig")
        body = source[source.index("async def execute_command("):]
        body = body[:body.index("\n    def ", 1)]
        assert body.index("coerce_parameters(") < body.index("self._client.execute(")
