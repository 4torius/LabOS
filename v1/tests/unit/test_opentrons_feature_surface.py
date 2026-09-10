"""The Opentrons SiLA2 surface: what it exposes, and how the new commands behave.

The feature definition used to be 18 commands of which seven were pure reads
or remote file listings, while capabilities the server already implemented —
the gripper, the heater-shaker, the camera — were reachable only by authoring
a recipe file. This checks the shape that replaced it, and the argument
handling of the commands that are new.
"""
import importlib
import json
import re
import sys
from pathlib import Path
import pytest

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))

FDL = V1_DIR / "SiLA2" / "OpentronsSiLA2Server" / "features" / "WorkflowAPI.sila.xml"
FDL_TEXT = FDL.read_text(encoding="utf-8")

COMMANDS = re.findall(r"<Command>\s*<Identifier>(\w+)</Identifier>", FDL_TEXT)
PROPERTIES = re.findall(r"<Property>\s*<Identifier>(\w+)</Identifier>", FDL_TEXT)


class TestFeatureShape:

    @pytest.mark.parametrize("removed", [
        "GetStatus", "GetCurrentConfig", "GetRobotInfo", "GetModulesStatus",
        "ListRecipes", "ListHardwareConfigs", "ListTipRacks", "Initialize",
    ])
    def test_pure_reads_and_no_ops_are_no_longer_commands(self, removed):
        assert removed not in COMMANDS, (
            f"{removed} is a command again. Reads belong in Properties, file "
            f"listings belong to the webapp's own Library/ reads, and "
            f"Initialize duplicated what start() already does."
        )

    @pytest.mark.parametrize("name", [
        "Status", "CurrentRunId", "ActiveConfig", "TipsAvailable",
        "RobotInfo", "ModulesStatus",
    ])
    def test_reads_are_properties(self, name):
        assert name in PROPERTIES

    @pytest.mark.parametrize("name", [
        "RunSteps", "MoveLabware", "SetModuleTemperature", "ShakeModule",
        "SetModuleLatch", "ValidateRecipe", "CaptureImage",
    ])
    def test_capabilities_that_existed_but_were_unreachable_are_exposed(self, name):
        assert name in COMMANDS

    @pytest.mark.parametrize("name", [
        "Home", "EmergencyStop", "SetLights", "LoadHardwareConfig",
        "ExecuteRecipe", "PauseRun", "ResumeRun", "AbortRun",
        "RefillTipRack", "ResetTipTracking",
    ])
    def test_commands_still_in_use_are_kept(self, name):
        assert name in COMMANDS

    def test_every_command_does_something(self):
        """No command is a disguised read: nothing is named Get*/List*."""
        readers = [c for c in COMMANDS if c.startswith(("Get", "List"))]
        assert readers == [], f"these read state and should be properties: {readers}"

    def test_state_changing_commands_are_flagged_in_their_own_description(self):
        """An operator reading the command list has to be able to tell which
        buttons move a robot arm."""
        blocks = re.findall(r"<Command>(.*?)</Command>", FDL_TEXT, re.S)
        described = {
            re.search(r"<Identifier>(\w+)</Identifier>", b).group(1)
            for b in blocks if "MOVES HARDWARE" in b
        }
        for name in ("Home", "ExecuteRecipe", "RunSteps", "MoveLabware",
                     "ShakeModule", "SetModuleTemperature", "SetModuleLatch",
                     "EmergencyStop", "AbortRun", "ResumeRun"):
            assert name in described, f"{name} moves hardware but does not say so"

    def test_feature_version_was_bumped(self):
        assert 'FeatureVersion="1.1"' in FDL_TEXT


class TestGeneratedBindingsMatchTheImplementation:

    def test_no_abstract_method_is_left_unimplemented(self):
        """Codegen and implementation agree — otherwise the server cannot start.

        Loaded through _pkg_loader: the Opentrons server ships a package
        literally named `src`, same as v1's own, so importing it directly
        collides with whichever one pytest cached first.
        """
        server_dir = V1_DIR / "SiLA2" / "OpentronsSiLA2Server"
        if str(server_dir) not in sys.path:
            sys.path.insert(0, str(server_dir))
        from _pkg_loader import load_opentrons_src

        ot_src = load_opentrons_src()
        impl_module = importlib.import_module("_ot_src.workflow_api_impl")
        missing = sorted(getattr(impl_module.WorkflowAPIImpl, "__abstractmethods__", set()))
        assert missing == [], f"unimplemented: {missing}"


class TestRunStepsValidation:
    """RunSteps takes arbitrary JSON from a workflow file, so it validates
    before handing anything to the robot."""

    @pytest.mark.parametrize("payload,expected", [
        ("not json at all", "not valid JSON"),
        ("[]", "non-empty"),
        # A lone {} is promoted to [{}] first, so it fails on the missing
        # Command rather than on emptiness — still the accurate complaint.
        ("{}", 'no "Command" field'),
        ('[{"NoCommandField": 1}]', 'no "Command" field'),
        ('[{"Command": "Mix"}, {"oops": true}]', 'no "Command" field'),
    ])
    def test_bad_payloads_are_rejected_with_a_usable_message(self, payload, expected):
        parsed_error = _parse_steps_error(payload)
        assert parsed_error is not None
        assert expected in parsed_error

    @pytest.mark.parametrize("payload", [
        '[{"Command": "Mix", "PipetteMount": "left", "Location": "MyPlate:A1"}]',
        '{"Command": "Delay", "Seconds": 5}',            # a lone object is fine
        '[{"Command": "MoveLabware"}, {"Command": "Home"}]',
    ])
    def test_valid_payloads_pass(self, payload):
        assert _parse_steps_error(payload) is None


def _parse_steps_error(steps_json: str):
    """Mirror of RunSteps' argument validation, exercised without a robot."""
    try:
        steps = json.loads(steps_json) if steps_json else []
    except json.JSONDecodeError as exc:
        return f"StepsJson is not valid JSON: {exc}"
    if isinstance(steps, dict):
        steps = [steps]
    if not isinstance(steps, list) or not steps:
        return "StepsJson must be a non-empty JSON array of recipe steps"
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or not step.get("Command"):
            return f'Step {index + 1} has no "Command" field'
    return None


class TestCameraConfiguration:

    def test_the_video_device_is_configurable(self):
        """If /dev/video2 is wrong on a given Flex, it must be a config change
        and not a code change."""
        config_yaml = (V1_DIR / "SiLA2" / "OpentronsSiLA2Server" / "config.yaml").read_text(
            encoding="utf-8")
        assert "camera:" in config_yaml
        assert "device:" in config_yaml
        assert "http_probe:" in config_yaml

    def test_the_snapshot_step_honours_the_configured_device(self):
        generator = (V1_DIR / "SiLA2" / "OpentronsSiLA2Server" / "src"
                     / "protocol_generator.py").read_text(encoding="utf-8")
        assert 'step.get("Device"' in generator
        assert 'f"device={dev}"' in generator

    def test_the_protocol_fallback_is_gated_behind_an_explicit_flag(self):
        """Starting a protocol homes the robot, so the fallback cannot be the
        silent default."""
        impl = (V1_DIR / "SiLA2" / "OpentronsSiLA2Server" / "src"
                / "workflow_api_impl.py").read_text(encoding="utf-8")
        assert "if not AllowRobotMotion:" in impl
        block = re.search(r"<Command>\s*<Identifier>CaptureImage</Identifier>(.*?)</Command>",
                          FDL_TEXT, re.S).group(1)
        assert "HOMES THE ROBOT" in block

    def test_captured_images_land_where_the_webapp_serves_them(self):
        impl = (V1_DIR / "SiLA2" / "OpentronsSiLA2Server" / "src"
                / "workflow_api_impl.py").read_text(encoding="utf-8")
        assert '"Images"' in impl
        app = (V1_DIR / "webapp" / "app.py").read_text(encoding="utf-8")
        assert '"/api/images"' in app
        assert '"/api/images/{filename}"' in app
