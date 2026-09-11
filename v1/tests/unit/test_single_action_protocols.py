"""What a single-action command actually sends the robot.

Each of MoveLabware, ShakeModule, SetModuleLatch and SetModuleTemperature is
turned into a one-step protocol and played. Playing a run homes the Flex, so
anything wrong with that protocol costs a home before it is discovered — the
failure arrives after the arm has already moved, which reads to an operator
like the command half worked.

The deck reaches a generated protocol only through the recipe's `Requirements`
map. A recipe that declares none gets a protocol with no `load_module` and no
`load_labware` past the auto-injected tip racks, so the step refers to a name
the protocol never defined. On hardware that was a bare `KeyError:
'HeaterShaker'` on a heater-shaker that was both declared in the hardware
config and physically attached.

These tests build the same recipe the implementation builds and generate the
protocol locally. No robot, and no run.
"""
import ast
import json
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))
sys.path.insert(0, str(V1_DIR / "tests" / "unit"))

from _pkg_loader import load_opentrons_src  # noqa: E402

_ot = load_opentrons_src()
from _ot_src.hardware_manager import HardwareManager  # noqa: E402
from _ot_src.protocol_generator import ProtocolGenerator  # noqa: E402

CONFIG = {
    "ConfigName": "TestSetup",
    "Modules": {"HeaterShaker": {"Type": "heaterShakerModuleV1", "Slot": "A1"}},
    "Trash": {"MyTrash": {"Type": "TrashBin", "Slot": "A3"}},
    "Labware": {
        "MyTips": {"LoadName": "opentrons_flex_96_tiprack_1000ul", "Slot": "B3"},
        "TargetPlate": {"LoadName": "corning_96_wellplate_360ul_flat", "Slot": "D2"},
    },
    "Pipettes": {"left": "flex_1channel_1000"},
}


@pytest.fixture
def hardware(tmp_path):
    path = tmp_path / "TestSetup.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    return HardwareManager(config_path=str(path))


@pytest.fixture
def generator(tmp_path):
    return ProtocolGenerator(temp_dir=str(tmp_path / "temp"), enable_simulation=False)


def _protocol(hardware, generator, steps, requirements):
    """Exactly what _run_inline builds, then generated."""
    recipe = {
        "ProtocolName": "test",
        "Description": "test",
        "Requirements": dict(requirements),
        "Steps": steps,
    }
    return generator.generate_content(json.dumps(hardware.apply_hardware_to_recipe(recipe)))


class TestAModuleStepLoadsItsModule:

    def test_declaring_the_module_puts_it_in_the_recipe(self, hardware):
        recipe = hardware.apply_hardware_to_recipe({
            "Requirements": {"HeaterShaker": "HeaterShaker"},
            "Steps": [{"Command": "HeaterShakerCloseLatch", "ModuleID": "HeaterShaker"}],
        })
        assert recipe["Modules"]["HeaterShaker"]["Type"] == "heaterShakerModuleV1"
        assert recipe["Modules"]["HeaterShaker"]["Slot"] == "A1"

    def test_declaring_nothing_leaves_the_module_out(self, hardware):
        """The shape that reached the robot and failed with KeyError."""
        recipe = hardware.apply_hardware_to_recipe({
            "Requirements": {},
            "Steps": [{"Command": "HeaterShakerCloseLatch", "ModuleID": "HeaterShaker"}],
        })
        assert recipe["Modules"] == {}, (
            "a step naming a module the recipe never requires produces a "
            "protocol with no load_module"
        )

    @pytest.mark.parametrize("step", [
        {"Command": "HeaterShakerCloseLatch", "ModuleID": "HeaterShaker"},
        {"Command": "HeaterShakerOpenLatch", "ModuleID": "HeaterShaker"},
        {"Command": "HeaterShakerSetShake", "ModuleID": "HeaterShaker", "RPM": 200},
        {"Command": "HeaterShakerSetTemperature", "ModuleID": "HeaterShaker",
         "Temperature": 37},
    ])
    def test_the_generated_protocol_carries_the_module(self, hardware, generator, step):
        code = _protocol(hardware, generator, [step],
                         {"HeaterShaker": "HeaterShaker"})
        # The template embeds the resolved recipe as a Python literal.
        settings = ast.literal_eval(code.split("SETTINGS = ", 1)[1].split("\n", 1)[0])
        assert "HeaterShaker" in settings["Modules"]
        assert settings["Modules"]["HeaterShaker"]["Type"] == "heaterShakerModuleV1"
        assert "load_module" in code


class TestAMoveLoadsWhatItMoves:

    def test_the_labware_being_moved_is_in_the_recipe(self, hardware):
        recipe = hardware.apply_hardware_to_recipe({
            "Requirements": {"TargetPlate": "TargetPlate"},
            "Steps": [{"Command": "MoveLabware", "LabwareID": "TargetPlate",
                       "NewLocation": "D3", "UseGripper": True}],
        })
        assert "TargetPlate" in recipe["Labware"]

    def test_moving_onto_a_module_loads_that_module_too(self, hardware):
        """A move onto a heater-shaker needs somewhere to go."""
        recipe = hardware.apply_hardware_to_recipe({
            "Requirements": {"TargetPlate": "TargetPlate",
                             "HeaterShaker": "HeaterShaker"},
            "Steps": [{"Command": "MoveLabware", "LabwareID": "TargetPlate",
                       "NewLocation": "HeaterShaker", "UseGripper": True}],
        })
        assert "TargetPlate" in recipe["Labware"]
        assert "HeaterShaker" in recipe["Modules"]

    def test_a_tip_rack_is_not_loaded_twice(self, hardware):
        """Tip racks are auto-injected; requiring one by name must not collide
        with that, or the slot-collision check rejects the move."""
        recipe = hardware.apply_hardware_to_recipe({
            "Requirements": {"MyTips": "MyTips"},
            "Steps": [{"Command": "MoveLabware", "LabwareID": "MyTips",
                       "NewLocation": "C1", "UseGripper": True}],
        })
        assert sorted(recipe["Labware"]) == ["MyTips"]


class TestTheImplementationDeclaresThem:
    """Read statically — importing the feature implementation registers its
    .proto in protobuf's process-global pool (see test_opentrons_feature_surface)."""

    IMPL = (V1_DIR / "SiLA2" / "OpentronsSiLA2Server" / "src"
            / "workflow_api_impl.py").read_text(encoding="utf-8")

    @pytest.mark.parametrize("command", [
        "SetModuleTemperature", "ShakeModule", "SetModuleLatch",
    ])
    def test_each_module_command_requires_its_module(self, command):
        start = self.IMPL.index(f"    def {command}(")
        body = self.IMPL[start:self.IMPL.index("requirements=", start) + 200]
        assert "requirements={ModuleName: ModuleName}" in body

    def test_a_move_requires_both_ends(self):
        assert "requirements=self._move_requirements(LabwareName, NewLocation)" in self.IMPL
        assert "_move_requirements" in self.IMPL

    def test_run_inline_passes_them_through(self):
        start = self.IMPL.index("    def _run_inline(")
        body = self.IMPL[start:self.IMPL.index("Steps", start) + 40]
        assert '"Requirements": dict(requirements or {})' in body


class TestTheDestinationOfAMoveIsChecked:
    """'Z9' reached the robot and failed there with "not a valid deck slot" —
    which cost a home first, because the run was already playing."""

    IMPL = TestTheImplementationDeclaresThem.IMPL

    def test_a_destination_that_is_not_a_slot_is_refused(self):
        assert "if NewLocation not in _FLEX_SLOTS:" in self.IMPL

    def test_the_refusal_lists_the_real_destinations(self):
        start = self.IMPL.index("if NewLocation not in _FLEX_SLOTS:")
        body = self.IMPL[start:start + 600]
        assert "Free slots" in body
        assert "Modules" in body

    def test_a_module_is_still_a_valid_destination(self):
        """The check is inside the branch that already excluded modules."""
        modules_branch = self.IMPL.index("if NewLocation not in modules:")
        slot_check = self.IMPL.index("if NewLocation not in _FLEX_SLOTS:")
        assert modules_branch < slot_check
