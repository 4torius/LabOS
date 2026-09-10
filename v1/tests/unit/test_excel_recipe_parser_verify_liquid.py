"""
Tests for the "Verify aspiration" (VerifyLiquid) per-trip tip splitting and
multi-well liquid-fallback backup wells added to src/excel_recipe_parser.py.

Covers:
  - _split_into_trips / _estimate_distribute_waste (pure bin-packing logic,
    the latter refactored to reuse the former - same numbers as before).
  - generate_recipe_with_config()'s Distribute branch: when a reagent has
    "verify_liquid" on, it must emit one PickUpTip/Distribute/DropTip PER
    RELOAD TRIP (a fresh tip every time, so Opentrons' native liquid-presence
    probe re-arms on every reload) instead of one PickUpTip/DropTip pair for
    the whole reagent.
  - Backup-well allocation: whenever "verify_liquid" is on, the reagent
    automatically gets enough EXTRA wells from its own reservoir to cover its
    own required volume again (ceil(with_excess_ul / well_capacity), at
    least 1), without colliding with wells already claimed by other reagents
    sharing that reservoir; Source becomes a candidate list (primary +
    backups) on every trip step whenever at least one backup was assigned.
  - Transfer strategy needs no per-trip splitting (already fresh tip per
    well), only the Source-as-candidate-list part when backups are assigned.
"""
from src.excel_recipe_parser import _split_into_trips, _estimate_distribute_waste, generate_recipe_with_config


class TestSplitIntoTrips:
    def test_groups_by_tip_capacity_minus_disposal_volume(self):
        # tip_capacity=50, disposal_volume=5 -> usable capacity 45
        volumes = [20, 20, 20, 20]
        dest_wells = ["A1", "A2", "A3", "A4"]
        trips = _split_into_trips(volumes, dest_wells, tip_capacity=50, disposal_volume=5)
        assert trips == [([20, 20], ["A1", "A2"]), ([20, 20], ["A3", "A4"])]

    def test_a_single_destination_exceeding_the_tip_is_still_taken_alone(self):
        trips = _split_into_trips([80], ["A1"], tip_capacity=50, disposal_volume=5)
        assert trips == [([80], ["A1"])]

    def test_empty_input_returns_no_trips(self):
        assert _split_into_trips([], [], tip_capacity=50, disposal_volume=5) == []


class TestEstimateDistributeWasteUnchanged:
    def test_waste_is_one_disposal_volume_per_trip(self):
        # Same 4-well/2-trip case as above -> 2 trips -> 2x disposal volume
        waste = _estimate_distribute_waste([20, 20, 20, 20], tip_capacity=50, disposal_volume=5)
        assert waste == 10.0

    def test_zero_disposal_volume_means_zero_waste(self):
        assert _estimate_distribute_waste([20, 20], tip_capacity=50, disposal_volume=0) == 0.0


def _sheet(reagent: str, wells_and_volumes):
    return {
        "sheet_name": "Test",
        "wells": [{"well": w, "reagents": {reagent: v}} for w, v in wells_and_volumes],
        "summary": {"reagents": [reagent]},
    }


def _steps_by_command(recipe, command):
    return [s for s in recipe["Steps"] if s.get("Command") == command]


class TestDistributeVerifyLiquidPerTripSplitting:
    def _config(self, **overrides):
        config = {
            "groups": [{"name": "All", "columns": ["HA"], "delay_after_seconds": 0}],
            "reservoir_type": "nest_12_reservoir_15ml",
            "pipette": "left",
            "pipette_max_volume_ul": 50,
            "pipette_min_volume_ul": 5,
        }
        config.update(overrides)
        return config

    def test_without_verify_liquid_one_tip_for_the_whole_reagent(self):
        """Baseline/no-regression: default behaviour is unchanged."""
        sheet = _sheet("HA", [("A1", 20), ("A2", 20), ("A3", 20), ("A4", 20)])
        config = self._config()
        recipe = generate_recipe_with_config(sheet, config, recipe_name="test")
        assert len(_steps_by_command(recipe, "PickUpTip")) == 1
        assert len(_steps_by_command(recipe, "DropTip")) == 1
        distributes = _steps_by_command(recipe, "Distribute")
        assert len(distributes) == 1
        assert "VerifyLiquid" not in distributes[0]
        assert distributes[0]["Source"] == "Source_1:A1"

    def test_verify_liquid_emits_one_tip_pair_per_trip(self):
        sheet = _sheet("HA", [("A1", 20), ("A2", 20), ("A3", 20), ("A4", 20)])
        config = self._config(reagent_advanced={"HA": {"verify_liquid": True}})
        recipe = generate_recipe_with_config(sheet, config, recipe_name="test")

        distributes = _steps_by_command(recipe, "Distribute")
        assert len(distributes) == 2  # 2 wells/trip (usable capacity 45) -> 2 trips for 4 wells
        assert len(_steps_by_command(recipe, "PickUpTip")) == 2
        assert len(_steps_by_command(recipe, "DropTip")) == 2
        for d in distributes:
            assert d["VerifyLiquid"] is True
            # Tiny nominal volume vs. nest_12's 15000 uL/well capacity still
            # rounds up to 1 automatic backup well (see the "at least 1" floor).
            assert d["Source"] == ["Source_1:A1", "Source_1:A2"]
        all_dests = [w for d in distributes for w in d["Destinations"]]
        assert sorted(all_dests) == ["TargetPlate:A1", "TargetPlate:A2", "TargetPlate:A3", "TargetPlate:A4"]
        # PickUpTip/Distribute/DropTip triplets, in order, one per trip.
        commands = [s["Command"] for s in recipe["Steps"] if s["Command"] in ("PickUpTip", "Distribute", "DropTip")]
        assert commands == ["PickUpTip", "Distribute", "DropTip", "PickUpTip", "Distribute", "DropTip"]

    def test_backup_wells_are_sized_automatically_to_cover_the_volume_again(self):
        # nest_12_reservoir_15ml is 15000 uL/well. 2 x 8000 uL = 16000 nominal
        # -> ~17650 with the default excess margin, which already exceeds one
        # well's capacity -> the PRIMARY allocation itself needs
        # ceil(17650/15000)=2 wells (A1+A2, existing overflow-split feature),
        # and the automatic backup sizing uses that same math for the reserve
        # -> 2 more wells (A3+A4) to cover the volume again.
        sheet = _sheet("HA", [("A1", 8000), ("A2", 8000)])
        config = self._config(pipette_max_volume_ul=10000, reagent_advanced={"HA": {"verify_liquid": True}})
        recipe = generate_recipe_with_config(sheet, config, recipe_name="test")

        distributes = _steps_by_command(recipe, "Distribute")
        for d in distributes:
            assert d["Source"] == ["Source_1:A1", "Source_1:A2", "Source_1:A3", "Source_1:A4"]
        # Metadata.WellAssignments must include the backups too - it's the
        # only thing plate_tracking.py can use to resolve them back to "HA"
        # when the DB record gets built after a real run.
        assert recipe["Metadata"]["WellAssignments"]["HA"] == "Source_1:A1+A2+A3+A4"

    def test_backup_wells_do_not_collide_across_reagents_sharing_a_reservoir(self):
        sheet = {
            "sheet_name": "Test",
            "wells": [{"well": "A1", "reagents": {"HA": 20, "DMTMM": 20}}],
            "summary": {"reagents": ["HA", "DMTMM"]},
        }
        config = self._config(
            num_reservoirs=1,  # force HA and DMTMM to share one reservoir
            reagent_advanced={"HA": {"verify_liquid": True}},
        )
        config["groups"] = [{"name": "All", "columns": ["HA", "DMTMM"], "delay_after_seconds": 0}]
        recipe = generate_recipe_with_config(sheet, config, recipe_name="test")

        distributes = _steps_by_command(recipe, "Distribute")
        ha_step = next(d for d in distributes if "HA" in d["Description"])
        dmtmm_step = next(d for d in distributes if "DMTMM" in d["Description"])
        # HA's own primary + backup are claimed together, in HA's own turn,
        # before DMTMM gets a chance to claim anything - so HA's whole
        # footprint (A1+A2) stays one contiguous, adjacent run instead of
        # being split apart by DMTMM's primary landing in between.
        assert ha_step["Source"] == ["Source_1:A1", "Source_1:A2"]
        assert dmtmm_step["Source"] == "Source_1:A3"

    def test_not_enough_free_wells_warns_instead_of_failing(self):
        """Partial shortfall on BACKUP specifically: a big reagent (DMTMM,
        processed FIRST, no verify_liquid) claims almost the whole 12-well
        shared reservoir for its own primary allocation, leaving only one
        well free by the time HA (processed second, small volume, verify
        aspiration on) takes its own primary - leaving nothing at all for
        HA's automatic backup. Generation must not raise, it assigns what's
        available (nothing) and warns."""
        sheet = {
            "sheet_name": "Test",
            "wells": [{"well": "A1", "reagents": {"DMTMM": 145405, "HA": 4000}}],
            "summary": {"reagents": ["DMTMM", "HA"]},
        }
        config = self._config(
            num_reservoirs=1,
            reagent_advanced={"HA": {"verify_liquid": True}},
        )
        config["groups"] = [{"name": "All", "columns": ["DMTMM", "HA"], "delay_after_seconds": 0}]
        recipe = generate_recipe_with_config(sheet, config, recipe_name="test")

        distributes = _steps_by_command(recipe, "Distribute")
        ha_step = next(d for d in distributes if "HA" in d["Description"])
        assert ha_step["Source"] == "Source_1:A12"  # no backup well could be claimed at all
        notes = " ".join(recipe.get("Metadata", {}).get("Notes", []))
        assert "only 0 free well(s) available" in notes

    def test_no_free_well_left_falls_back_to_plain_string_source(self):
        """Single-well reservoir - the reagent's own well IS the only one,
        so there's no room to reserve a backup even with verify_liquid on.
        Source stays a plain string (the native first-aspirate probe still
        applies) and a warning note explains why no backup was assigned."""
        sheet = _sheet("HA", [("A1", 20)])
        config = self._config(
            reservoir_type="nest_1_reservoir_290ml",
            reagent_advanced={"HA": {"verify_liquid": True}},
        )
        recipe = generate_recipe_with_config(sheet, config, recipe_name="test")
        distributes = _steps_by_command(recipe, "Distribute")
        assert distributes[0]["Source"] == "Source_1:A1"
        notes = " ".join(recipe.get("Metadata", {}).get("Notes", []))
        assert "only 0 free well(s) available" in notes


class TestTransferVerifyLiquidFallback:
    def test_transfer_keeps_one_tip_per_well_but_source_becomes_a_list_with_backups(self):
        sheet = _sheet("HA", [("A1", 20), ("A2", 20)])
        config = {
            "groups": [{"name": "All", "columns": ["HA"], "delay_after_seconds": 0, "strategy": "transfer"}],
            "reservoir_type": "nest_12_reservoir_15ml",
            "pipette": "left",
            "pipette_max_volume_ul": 1000,
            "pipette_min_volume_ul": 5,
            "reagent_advanced": {"HA": {"verify_liquid": True}},
        }
        recipe = generate_recipe_with_config(sheet, config, recipe_name="test")

        transfers = _steps_by_command(recipe, "Transfer")
        assert len(transfers) == 2  # still one Transfer per destination well
        assert len(_steps_by_command(recipe, "PickUpTip")) == 2
        assert len(_steps_by_command(recipe, "DropTip")) == 2
        for t in transfers:
            # 1 automatic backup well (tiny volume vs. 15000 uL/well capacity).
            assert t["Source"] == ["Source_1:A1", "Source_1:A2"]
            assert t["VerifyLiquid"] is True

    def test_transfer_falls_back_to_plain_string_source_when_no_well_is_free_for_backup(self):
        """Single-well reservoir - the reagent's own well IS the only one
        available, so there's no room to reserve a backup even though
        verify_liquid is on. Source stays a plain string (the native
        first-aspirate probe still applies), with a warning note explaining
        why no backup could be assigned."""
        sheet = _sheet("HA", [("A1", 20)])
        config = {
            "groups": [{"name": "All", "columns": ["HA"], "delay_after_seconds": 0, "strategy": "transfer"}],
            "reservoir_type": "nest_1_reservoir_290ml",
            "pipette": "left",
            "pipette_max_volume_ul": 1000,
            "pipette_min_volume_ul": 5,
            "reagent_advanced": {"HA": {"verify_liquid": True}},
        }
        recipe = generate_recipe_with_config(sheet, config, recipe_name="test")
        transfers = _steps_by_command(recipe, "Transfer")
        assert transfers[0]["Source"] == "Source_1:A1"
        notes = " ".join(recipe.get("Metadata", {}).get("Notes", []))
        assert "only 0 free well(s) available" in notes
