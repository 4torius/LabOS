"""Tests for src/deck.py — what is on the deck, per the active hardware config.

The pickers these feed used to be fiction: six invented labware load names that
match nothing Opentrons knows, and a destination list of the discovered
*instruments* because the parameter happened to be called NewLocation.
"""
import json
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))

from src import deck  # noqa: E402


CONFIG = {
    "ConfigName": "TestSetup",
    "Modules": {"HeaterShaker": {"Type": "heaterShakerModuleV1", "Slot": "A1"}},
    "Trash": {"MyTrash": {"Type": "TrashBin", "Slot": "A3"}},
    "Labware": {
        "MyTips": {"LoadName": "opentrons_flex_96_tiprack_1000ul", "Slot": "B3",
                   "DisplayName": "Tip rack 1000uL"},
        "TargetPlate": {"LoadName": "corning_96_wellplate_360ul_flat", "Slot": "D2",
                        "DisplayName": "Target plate"},
        "Source_1": {"LoadName": "corning_24_wellplate_3.4ml_flat", "Slot": "D1"},
    },
    "Pipettes": {"left": "flex_1channel_1000"},
}


@pytest.fixture
def hal_dir(tmp_path):
    directory = tmp_path / "HardwareConfig"
    directory.mkdir()
    (directory / "TestSetup.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    (directory / ".active").write_text("TestSetup.json", encoding="utf-8")
    return directory


class TestLoading:

    def test_reads_the_active_config(self, hal_dir):
        assert deck.active_config_name(hal_dir) == "TestSetup.json"
        assert deck.load_layout(hal_dir).config_name == "TestSetup"

    def test_no_active_config_is_none_not_a_crash(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert deck.active_config_name(empty) is None
        assert deck.load_layout(empty) is None

    def test_a_marker_pointing_at_a_missing_file_is_none(self, hal_dir):
        (hal_dir / ".active").write_text("NotThere.json", encoding="utf-8")
        assert deck.load_layout(hal_dir) is None

    def test_a_name_escaping_the_config_directory_is_refused(self, hal_dir):
        assert deck.load_layout(hal_dir, "../../../lab_config") is None

    def test_the_extension_is_optional(self, hal_dir):
        assert deck.load_layout(hal_dir, "TestSetup").config_name == "TestSetup"


class TestOccupancy:

    def test_every_section_occupies_slots(self, hal_dir):
        layout = deck.load_layout(hal_dir)
        # labware + the module + the trash bin
        assert layout.occupied_slots == ["A1", "A3", "B3", "D1", "D2"]

    def test_free_slots_exclude_everything_standing_somewhere(self, hal_dir):
        free = deck.load_layout(hal_dir).free_slots()
        assert "A1" not in free and "A3" not in free and "D2" not in free
        assert "C2" in free

    def test_staging_slots_are_offered_but_can_be_excluded(self, hal_dir):
        layout = deck.load_layout(hal_dir)
        assert "A4" in layout.free_slots()
        assert "A4" not in layout.free_slots(include_staging=False)


class TestPickers:

    def test_only_labware_can_be_moved(self, hal_dir):
        """A module is bolted down and the trash is a fixture."""
        names = {i.name for i in deck.load_layout(hal_dir).movable()}
        assert names == {"MyTips", "TargetPlate", "Source_1"}
        assert "HeaterShaker" not in names
        assert "MyTrash" not in names

    def test_move_targets_are_free_slots_plus_modules(self, hal_dir):
        values = [o["value"] for o in deck.options(hal_dir, "deck_target")]
        assert "C2" in values                 # free
        assert "HeaterShaker" in values       # a module is a valid destination
        assert "D2" not in values             # occupied by TargetPlate
        assert "A3" not in values             # the trash bin

    def test_labels_say_what_a_thing_is_and_where(self, hal_dir):
        labels = {o["value"]: o["label"] for o in deck.options(hal_dir, "hal_labware")}
        assert "Tip rack 1000uL" in labels["MyTips"]
        assert "B3" in labels["MyTips"]

    def test_a_slot_label_does_not_repeat_itself(self, hal_dir):
        labels = {o["value"]: o["label"] for o in deck.options(hal_dir, "deck_target")}
        assert labels["C2"] == "C2"
        assert labels["A4"] == "A4 — staging area"

    def test_values_are_what_the_command_receives(self, hal_dir):
        """The label is for the human; the value has to be the logical name."""
        for option in deck.options(hal_dir, "hal_labware"):
            assert option["value"] in CONFIG["Labware"]

    def test_labware_on_a_module_reports_where_it_really_is(self, hal_dir):
        config = json.loads((hal_dir / "TestSetup.json").read_text(encoding="utf-8"))
        config["Labware"]["TargetPlate"]["OnModule"] = "HeaterShaker"
        (hal_dir / "TestSetup.json").write_text(json.dumps(config), encoding="utf-8")
        labels = {o["value"]: o["label"] for o in deck.options(hal_dir, "hal_labware")}
        assert "on HeaterShaker" in labels["TargetPlate"]

    def test_a_long_display_name_is_trimmed_for_a_dropdown(self, hal_dir):
        config = json.loads((hal_dir / "TestSetup.json").read_text(encoding="utf-8"))
        config["Labware"]["Source_1"]["DisplayName"] = "x" * 200
        (hal_dir / "TestSetup.json").write_text(json.dumps(config), encoding="utf-8")
        label = next(o["label"] for o in deck.options(hal_dir, "hal_labware")
                     if o["value"] == "Source_1")
        assert len(label) < 80

    def test_no_active_config_yields_no_options_rather_than_wrong_ones(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        for hint in deck.DECK_HINTS:
            assert deck.options(empty, hint) == []

    def test_an_unknown_hint_is_empty(self, hal_dir):
        assert deck.options(hal_dir, "not_a_hint") == []
