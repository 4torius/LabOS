"""generate_generic_hal() / generate_hal_config_from_recipe(): the number of
simultaneous tip sizes a generated HAL can carry is bounded only by how many
free deck slots actually exist (GENERIC_TIP_SLOT_PREFERENCE), not by a
hardcoded "2" - more sizes than that spill onto further free slots (A1, then
the reservoir pool's own lowest-priority slots) instead of being silently
capped.
"""
from src.excel_recipe_parser import (
    GENERIC_TIP_SLOT_PREFERENCE,
    generate_generic_hal,
    generate_hal_config_from_recipe,
)


class TestGenerateGenericHalTipSlots:
    def test_single_size_keeps_primary_plus_backup_on_b3_c3(self):
        hal = generate_generic_hal(num_sources=4, tip_sizes_ul=[1000])
        assert hal["Labware"]["MyTips"]["Slot"] == "B3"
        assert hal["Labware"]["MyTips_2"]["Slot"] == "C3"
        assert hal["Metadata"]["tip_sizes_ul"] == [1000]

    def test_two_sizes_no_longer_capped_use_b3_and_c3(self):
        hal = generate_generic_hal(num_sources=4, tip_sizes_ul=[1000, 50])
        assert hal["Labware"]["MyTips_1000"]["Slot"] == "B3"
        assert hal["Labware"]["MyTips_50"]["Slot"] == "C3"
        assert "MyTips_2" not in hal["Labware"]

    def test_three_sizes_use_a_third_slot_instead_of_being_capped_at_two(self):
        hal = generate_generic_hal(num_sources=4, tip_sizes_ul=[1000, 200, 50])
        slots = {k: v["Slot"] for k, v in hal["Labware"].items() if k.startswith("MyTips_")}
        assert set(slots.values()) == {"B3", "C3", "A1"}
        assert slots["MyTips_1000"] == "B3"
        assert slots["MyTips_200"] == "C3"
        assert slots["MyTips_50"] == "A1"
        assert hal["Metadata"]["tip_notes"] == []  # no truncation happened

    def test_four_sizes_the_real_flex_catalog_all_fit_without_truncation(self):
        hal = generate_generic_hal(num_sources=1, tip_sizes_ul=[1000, 200, 50, 20])
        tip_keys = [k for k in hal["Labware"] if k.startswith("MyTips_")]
        assert len(tip_keys) == 4
        assert hal["Metadata"]["tip_notes"] == []

    def test_more_sizes_than_free_slots_is_capped_at_the_true_deck_ceiling(self, monkeypatch):
        # Only 4 distinct sizes exist in the real Flex catalog, so the real
        # ceiling (10 slots) can't actually be exceeded with genuine values -
        # shrink the slot pool instead to exercise the truncation path.
        import src.excel_recipe_parser as erp
        monkeypatch.setattr(erp, "GENERIC_TIP_SLOT_PREFERENCE", ["B3", "C3", "A1"])
        hal = generate_generic_hal(num_sources=1, tip_sizes_ul=[1000, 200, 50, 20])
        tip_keys = [k for k in hal["Labware"] if k.startswith("MyTips_")]
        assert len(tip_keys) == 3
        assert any("free deck slots" in n for n in hal["Metadata"]["tip_notes"])

    def test_no_reservoir_slot_ever_collides_with_a_tip_slot(self):
        hal = generate_generic_hal(num_sources=8, tip_sizes_ul=[1000, 200, 50])
        tip_slots = {v["Slot"] for k, v in hal["Labware"].items() if k.startswith("MyTips")}
        source_slots = {v["Slot"] for k, v in hal["Labware"].items() if k.startswith("Source_")}
        assert tip_slots.isdisjoint(source_slots)

    def test_extra_tip_sizes_shrink_available_reservoir_slots(self):
        hal_one_size = generate_generic_hal(num_sources=8, tip_sizes_ul=[1000])
        hal_three_sizes = generate_generic_hal(num_sources=8, tip_sizes_ul=[1000, 200, 50])
        assert hal_three_sizes["Metadata"]["total_sources"] < hal_one_size["Metadata"]["total_sources"]


class TestGenerateHalConfigFromRecipeTipSlots:
    def _recipe(self, tip_sizes_ul):
        return {
            "Requirements": {"Source_1": {}, "Source_2": {}, "Source_3": {}},
            "Configuration": {"tip_sizes_ul": tip_sizes_ul},
            "Metadata": {},
        }

    def test_three_sizes_use_b3_c3_and_a1_when_no_module_reserves_it(self):
        hal = generate_hal_config_from_recipe(self._recipe([1000, 200, 50]), hal_name="test")
        slots = {k: v["Slot"] for k, v in hal["Labware"].items() if k.startswith("MyTips_")}
        assert set(slots.values()) == {"B3", "C3", "A1"}

    def test_single_size_keeps_primary_plus_backup_on_b3_c3(self):
        hal = generate_hal_config_from_recipe(self._recipe([1000]), hal_name="test")
        assert hal["Labware"]["MyTips"]["Slot"] == "B3"
        assert hal["Labware"]["MyTips_2"]["Slot"] == "C3"
