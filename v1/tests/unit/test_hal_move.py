"""Recording a gripper move back into the hardware config.

Without this the config lies the moment a plate moves: it still claims the
labware is in the slot it started in, so the next recipe generates
`load_labware(..., "D2")` for a plate now sitting somewhere else, and the deck
the operator sees stops matching the deck the software believes in.
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
    "ConfigName": "TestSetup",
    "Modules": {"HeaterShaker": {"Type": "heaterShakerModuleV1", "Slot": "A1"}},
    "Trash": {"MyTrash": {"Type": "TrashBin", "Slot": "A3"}},
    "Labware": {
        "MyTips": {"LoadName": "opentrons_flex_96_tiprack_1000ul", "Slot": "B3"},
        "TargetPlate": {"LoadName": "corning_96_wellplate_360ul_flat", "Slot": "D2"},
    },
    "Pipettes": {"left": "flex_1channel_1000"},
    "Metadata": {"slot_assignments": {"A1": "HeaterShaker", "B3": "MyTips", "D2": "TargetPlate"}},
}


@pytest.fixture
def manager(tmp_path):
    folder = tmp_path / "HardwareConfig"
    folder.mkdir()
    (folder / "TestSetup.json").write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
    (folder / ".active").write_text("TestSetup.json", encoding="utf-8")
    return HardwareManager(config_folder=str(folder))


def _saved(manager):
    path = Path(manager._config_folder) / manager.get_current_config_file()
    return json.loads(path.read_text(encoding="utf-8"))


class TestRecordingAMove:

    def test_a_move_to_a_free_slot_is_written_to_disk(self, manager):
        manager.record_labware_move("TargetPlate", "C2")
        assert _saved(manager)["Labware"]["TargetPlate"]["Slot"] == "C2"

    def test_the_description_names_where_it_came_from_and_went(self, manager):
        assert manager.record_labware_move("TargetPlate", "C2") == "TargetPlate: D2 -> C2"

    def test_moving_onto_a_module_records_the_module(self, manager):
        manager.record_labware_move("TargetPlate", "HeaterShaker")
        entry = _saved(manager)["Labware"]["TargetPlate"]
        # OnModule is what the protocol generator reads to use
        # module.load_labware() instead of ctx.load_labware().
        assert entry["OnModule"] == "HeaterShaker"
        # ...and Slot follows the module, so anything reading Slot alone is right
        assert entry["Slot"] == "A1"

    def test_moving_off_a_module_clears_it(self, manager):
        manager.record_labware_move("TargetPlate", "HeaterShaker")
        manager.record_labware_move("TargetPlate", "C2")
        entry = _saved(manager)["Labware"]["TargetPlate"]
        assert "OnModule" not in entry
        assert entry["Slot"] == "C2"

    def test_a_plate_can_return_to_the_slot_it_left(self, manager):
        manager.record_labware_move("TargetPlate", "C2")
        manager.record_labware_move("TargetPlate", "D2")
        assert _saved(manager)["Labware"]["TargetPlate"]["Slot"] == "D2"

    def test_everything_else_in_the_config_survives(self, manager):
        manager.record_labware_move("TargetPlate", "C2")
        saved = _saved(manager)
        assert saved["Pipettes"] == CONFIG["Pipettes"]
        assert saved["Modules"] == CONFIG["Modules"]
        assert saved["Labware"]["MyTips"]["Slot"] == "B3"


class TestRefusals:
    """Recording a move that cannot be true is worse than refusing it."""

    def test_unknown_labware(self, manager):
        with pytest.raises(ValueError) as excinfo:
            manager.record_labware_move("NoSuchPlate", "C2")
        assert "NoSuchPlate" in str(excinfo.value)
        assert "MyTips" in str(excinfo.value)      # says what it does know

    def test_a_slot_occupied_by_other_labware(self, manager):
        with pytest.raises(ValueError) as excinfo:
            manager.record_labware_move("TargetPlate", "B3")
        assert "MyTips" in str(excinfo.value)

    def test_a_slot_occupied_by_a_module(self, manager):
        with pytest.raises(ValueError) as excinfo:
            manager.record_labware_move("MyTips", "A1")
        assert "HeaterShaker" in str(excinfo.value)

    def test_the_slot_the_labware_is_already_in_is_not_a_clash(self, manager):
        manager.record_labware_move("TargetPlate", "D2")   # must not raise

    def test_nothing_is_written_when_a_move_is_refused(self, manager):
        with pytest.raises(ValueError):
            manager.record_labware_move("TargetPlate", "B3")
        assert _saved(manager)["Labware"]["TargetPlate"]["Slot"] == "D2"


class TestSlotAssignmentsMetadata:

    def test_the_stale_metadata_map_is_refreshed(self, manager):
        """The Recipe Generator writes this map and it goes stale on the first
        move — the shipped 01_SPAAC config already disagrees with its own
        Labware section about where Source_1 is."""
        manager.record_labware_move("TargetPlate", "C2")
        assignments = _saved(manager)["Metadata"]["slot_assignments"]
        assert assignments["C2"] == "TargetPlate"
        assert "D2" not in assignments

    def test_a_config_without_the_map_does_not_grow_one(self, tmp_path):
        folder = tmp_path / "HardwareConfig"
        folder.mkdir()
        without = {k: v for k, v in CONFIG.items() if k != "Metadata"}
        (folder / "Plain.json").write_text(json.dumps(without), encoding="utf-8")
        (folder / ".active").write_text("Plain.json", encoding="utf-8")
        manager = HardwareManager(config_folder=str(folder))
        manager.record_labware_move("TargetPlate", "C2")
        assert "Metadata" not in json.loads((folder / "Plain.json").read_text(encoding="utf-8"))


class TestTheActiveConfigPropertyPublishesTheDeck:
    """Two objects in this server are called a config, and the property that
    is meant to answer "what is on the deck?" published the wrong one.

    ActiveConfig serialised ot.config - the ServerConfig dataclass read from
    the server's own config.yaml: host, port, robot IP, timeouts - under a
    property whose description says "this is what ExecuteRecipe runs against".
    """

    IMPL = ((V1_DIR / "SiLA2" / "OpentronsSiLA2Server" / "src"
             / "workflow_api_impl.py").read_text(encoding="utf-8"))

    def test_the_hardware_manager_can_hand_over_its_config(self, manager):
        config = manager.get_config()
        assert config["ConfigName"] == "TestSetup"
        assert "Labware" in config and "Modules" in config

    def test_what_it_hands_over_is_a_copy(self, manager):
        """A client publishing the deck must not be able to edit it."""
        manager.get_config()["Labware"] = {}
        assert manager.get_labware(), "the real config was modified"

    def test_the_property_reads_the_hardware_manager(self):
        start = self.IMPL.index("    def _active_config_json(")
        body = self.IMPL[start:self.IMPL.index("\n    def ", start + 1)]
        assert "ot.hardware.get_config()" in body

    def test_it_no_longer_serialises_the_server_settings(self):
        start = self.IMPL.index("    def _active_config_json(")
        body = self.IMPL[start:self.IMPL.index("\n    def ", start + 1)]
        assert "dataclasses.asdict" not in body, (
            "that is the server's own config.yaml - host, port, timeouts - "
            "not the deck"
        )

    def test_a_move_shows_up_in_what_the_property_would_publish(self, manager):
        manager.record_labware_move("TargetPlate", "C2")
        assert manager.get_config()["Labware"]["TargetPlate"]["Slot"] == "C2"
