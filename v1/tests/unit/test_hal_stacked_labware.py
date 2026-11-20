"""Labware that sits on an adapter or a module, resolved from the HAL.

A recipe names the plate it pipettes into - say the plate on the
Heater-Shaker - not the adapter under it. The adapter was never copied into
the recipe, so the generated protocol failed to load the plate (confirmed in
opentrons_simulate: KeyError on the adapter). The generator also loaded labware
in the order the recipe listed it, so a plate before its adapter failed the
same way.
"""
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

CONFIG = {
    "ConfigName": "HS_Stack",
    "Modules": {"HeaterShaker": {"Type": "heaterShakerModuleV1", "Slot": "D1"}},
    "Trash": {"MyTrash": {"Type": "TrashBin", "Slot": "A3"}},
    "Labware": {
        "Tips": {"LoadName": "opentrons_flex_96_tiprack_200ul", "Slot": "C3"},
        "HS_Adapter": {"LoadName": "opentrons_96_flat_bottom_adapter", "OnModule": "HeaterShaker"},
        "HS_Plate": {"LoadName": "nest_96_wellplate_200ul_flat", "OnAdapter": "HS_Adapter"},
        "Lonely": {"LoadName": "nest_96_wellplate_200ul_flat", "OnAdapter": "NotThere"},
    },
    "Pipettes": {"left": "flex_1channel_1000"},
}


@pytest.fixture
def hardware(tmp_path):
    path = tmp_path / "HS_Stack.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    return HardwareManager(config_path=str(path))


def _apply(hardware, requirements):
    return hardware.apply_hardware_to_recipe({"Requirements": dict(requirements), "Steps": []})


def test_the_adapter_and_module_under_a_plate_come_along(hardware):
    recipe = _apply(hardware, {"HS_Plate": "HS_Plate"})
    assert recipe["Labware"]["HS_Plate"]["OnAdapter"] == "HS_Adapter"
    assert recipe["Labware"]["HS_Adapter"]["OnModule"] == "HeaterShaker"
    assert recipe["Modules"]["HeaterShaker"]["Slot"] == "D1"


def test_a_module_the_recipe_names_differently_is_not_loaded_twice(hardware):
    recipe = _apply(hardware, {"HS_Plate": "HS_Plate", "Shaker": "HeaterShaker"})
    assert list(recipe["Modules"]) == ["Shaker"]
    assert recipe["Labware"]["HS_Adapter"]["OnModule"] == "Shaker"


def test_an_adapter_the_recipe_names_differently_is_referenced_by_that_name(hardware):
    recipe = _apply(hardware, {"Plate": "HS_Plate", "Adapter": "HS_Adapter",
                               "HeaterShaker": "HeaterShaker"})
    assert recipe["Labware"]["Plate"]["OnAdapter"] == "Adapter"
    assert "HS_Adapter" not in recipe["Labware"]


def test_the_hal_copy_is_not_changed(hardware):
    _apply(hardware, {"Plate": "HS_Plate", "Adapter": "HS_Adapter"})
    assert hardware.get_labware()["HS_Plate"]["OnAdapter"] == "HS_Adapter"


def test_a_plate_on_a_missing_adapter_is_a_clear_hal_error(hardware):
    with pytest.raises(ValueError, match="NotThere"):
        _apply(hardware, {"Lonely": "Lonely"})


def test_a_recipe_without_stacked_labware_is_unchanged(hardware):
    recipe = _apply(hardware, {"Tips": "Tips"})
    assert set(recipe["Labware"]) == {"Tips"}
    assert recipe["Modules"] == {}


def test_the_generator_loads_an_adapter_before_the_plate_on_it():
    """Whatever order the recipe lists them in (plate first here)."""
    source = (V1_DIR / "SiLA2" / "OpentronsSiLA2Server" / "src"
              / "protocol_generator.py").read_text(encoding="utf-8")
    start = source.index("# 3. LOAD LABWARE")
    body = source[start:start + 1500]
    assert 'c["OnAdapter"] in objects["Labware"]' in body


def test_a_recipe_using_the_module_gets_the_adapter_mounted_on_it(hardware):
    """The recipe page moves plates "onto the HeaterShaker": with an adapter
    mounted, the protocol has to know it is there, or the gripper drops the
    plate at the height of a bare module."""
    recipe = _apply(hardware, {"HeaterShaker": "HeaterShaker", "Tips": "Tips"})
    assert recipe["Labware"]["HS_Adapter"]["OnModule"] == "HeaterShaker"
    assert "HS_Plate" not in recipe["Labware"], "plates come only when the recipe names them"


def test_a_recipe_not_using_the_module_does_not_get_its_adapter(hardware):
    recipe = _apply(hardware, {"Tips": "Tips"})
    assert "HS_Adapter" not in recipe["Labware"]


def test_a_move_onto_a_module_lands_on_its_adapter():
    source = (V1_DIR / "SiLA2" / "OpentronsSiLA2Server" / "src"
              / "protocol_generator.py").read_text(encoding="utf-8")
    start = source.index('elif cmd == "MoveLabware":')
    body = source[start:source.index('elif cmd == "MoveToWell":', start)]
    assert 'getattr(_lw, "is_adapter", False)' in body
    assert body.index("is_adapter") < body.index("ctx.move_labware")
