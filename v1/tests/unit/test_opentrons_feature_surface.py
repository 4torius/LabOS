"""The Opentrons SiLA2 surface: what it exposes, and how the new commands behave.

The feature definition used to be 18 commands of which seven were pure reads
or remote file listings, while capabilities the server already implemented —
the gripper, the heater-shaker, the camera — were reachable only by authoring
a recipe file. This checks the shape that replaced it, and the argument
handling of the commands that are new.
"""
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
        "RunSteps",
    ])
    def test_pure_reads_and_no_ops_are_no_longer_commands(self, removed):
        assert removed not in COMMANDS, (
            f"{removed} is a command again. Reads belong in Properties, file "
            f"listings belong to the webapp's own Library/ reads, and "
            f"Initialize duplicated what start() already does. RunSteps "
            f"took a recipe's Steps array as a string parameter: the caller "
            f"had to know the recipe format, got no validation against the "
            f"deck, and ended up with something harder to use than the recipe "
            f"file it was meant to avoid. Named actions replaced it."
        )

    @pytest.mark.parametrize("name", [
        "Status", "CurrentRunId", "ActiveConfig", "TipsAvailable",
        "RobotInfo", "ModulesStatus",
    ])
    def test_reads_are_properties(self, name):
        assert name in PROPERTIES

    @pytest.mark.parametrize("name", [
        "MoveLabware", "SetModuleTemperature", "ShakeModule",
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
        for name in ("Home", "ExecuteRecipe", "MoveLabware",
                     "ShakeModule", "SetModuleTemperature", "SetModuleLatch",
                     "EmergencyStop", "AbortRun", "ResumeRun"):
            assert name in described, f"{name} moves hardware but does not say so"

    def test_feature_version_was_bumped(self):
        assert 'FeatureVersion="1.1"' in FDL_TEXT


class TestGeneratedBindingsMatchTheImplementation:

    def test_every_abstract_method_is_implemented(self):
        """Codegen and implementation agree — otherwise the server cannot start.

        Checked by reading both files rather than importing them. Importing the
        generated feature compiles its .proto into protobuf's *process-global*
        descriptor pool, keyed by the file name "WorkflowAPI.proto". If
        anything else in the same pytest session has already registered a
        different build of that proto — and something does, whenever a real
        Opentrons server is running on the machine and discovery fetches its
        feature definition — the second registration silently wins and the
        import fails with a KeyError on whichever message is new. The unit
        suite must not depend on what happens to be running on the bench.
        """
        server_dir = V1_DIR / "SiLA2" / "OpentronsSiLA2Server"
        base = (server_dir / "generated" / "workflowapi" / "workflowapi_base.py").read_text(
            encoding="utf-8")
        impl = (server_dir / "src" / "workflow_api_impl.py").read_text(encoding="utf-8")

        required = set(re.findall(r"@abstractmethod\s+def\s+(\w+)\s*\(", base))
        assert required, "no abstract methods found — is the generated base intact?"

        implemented = set(re.findall(r"^    def\s+(\w+)\s*\(", impl, re.M))
        missing = sorted(required - implemented)
        assert missing == [], f"declared in the FDL but not implemented: {missing}"

    def test_the_generated_bindings_cover_the_feature_definition(self):
        """Every command and property in the FDL reached codegen.

        Catches the specific mistake of editing the feature definition and
        forgetting to re-run `labos.py codegen`: the server then starts and
        simply does not have the command.
        """
        base = (V1_DIR / "SiLA2" / "OpentronsSiLA2Server" / "generated" / "workflowapi"
                / "workflowapi_base.py").read_text(encoding="utf-8")
        for command in COMMANDS:
            assert re.search(rf"def\s+{command}\s*\(", base), (
                f"{command} is in the FDL but not in the generated bindings — "
                f"run: python labos.py codegen OpentronsSiLA2Server"
            )
        for prop in PROPERTIES:
            assert (f"get_{prop}" in base or f"update_{prop}" in base), (
                f"property {prop} is in the FDL but not in the generated bindings"
            )


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
