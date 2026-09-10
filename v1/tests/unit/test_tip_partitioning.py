"""_partition_transfers_by_tip() / generate_recipe_with_config() integration:
a reagent whose destinations span more than one tip's range (e.g. 2-900uL)
must get EACH destination its own appropriate tip, not be blocked entirely
because no single tip covers the whole range.
"""
from src.excel_recipe_parser import _partition_transfers_by_tip, generate_recipe_with_config


class TestPartitionTransfersByTip:
    def test_no_tip_sizes_configured_returns_one_group_with_none_sentinel(self):
        transfers = [{"well": "A1", "volume": 10}, {"well": "A2", "volume": 500}]
        groups, unfit = _partition_transfers_by_tip(transfers, None)
        assert groups == [(None, transfers)]
        assert unfit == []

    def test_splits_by_which_size_each_transfer_individually_needs(self):
        transfers = [
            {"well": "A1", "volume": 2}, {"well": "A2", "volume": 5},
            {"well": "A3", "volume": 900}, {"well": "A4", "volume": 850},
        ]
        groups, unfit = _partition_transfers_by_tip(transfers, [1000, 50])
        assert unfit == []
        assert dict(groups) == {
            50: [{"well": "A1", "volume": 2}, {"well": "A2", "volume": 5}],
            1000: [{"well": "A3", "volume": 900}, {"well": "A4", "volume": 850}],
        }

    def test_groups_ordered_largest_tip_first(self):
        # Bulk volume goes down before the fine top-up - a reagent needing
        # both sizes does all its 1000uL wells, then all its 50uL wells.
        transfers = [{"well": "A1", "volume": 900}, {"well": "A2", "volume": 5}]
        groups, _ = _partition_transfers_by_tip(transfers, [1000, 50])
        assert [size for size, _ in groups] == [1000, 50]

    def test_transfer_below_every_minimum_is_unfit_not_dropped_silently(self):
        transfers = [{"well": "A1", "volume": 0.5}, {"well": "A2", "volume": 10}]
        groups, unfit = _partition_transfers_by_tip(transfers, [1000, 50])
        assert unfit == [{"well": "A1", "volume": 0.5}]
        assert dict(groups) == {50: [{"well": "A2", "volume": 10}]}

    def test_transfer_above_every_maximum_is_unfit(self):
        transfers = [{"well": "A1", "volume": 5000}]
        groups, unfit = _partition_transfers_by_tip(transfers, [1000, 50])
        assert groups == []
        assert unfit == [{"well": "A1", "volume": 5000}]


class TestGenerateRecipeUsesAppropriateTipPerWell:
    def _sheet(self, well_volumes):
        return {
            "sheet_name": "Test",
            "wells": [{"well": w, "reagents": {"Mixed": v}} for w, v in well_volumes.items()],
            "summary": {"reagents": ["Mixed"]},
        }

    def _config(self, tip_sizes_ul):
        return {
            "groups": [{"name": "All", "columns": ["Mixed"], "delay_after_seconds": 0}],
            "reservoir_type": "nest_12_reservoir_15ml",
            "tip_sizes_ul": tip_sizes_ul,
        }

    def test_small_and_large_wells_get_different_tip_racks(self):
        sheet = self._sheet({"A1": 2, "A2": 5, "A3": 900, "A4": 850})
        recipe = generate_recipe_with_config(sheet, self._config([1000, 50]), recipe_name="test")

        pickups = [s["TipRack"] for s in recipe["Steps"] if s["Command"] == "PickUpTip"]
        assert pickups == ["MyTips_1000", "MyTips_50"]

        distributes = [s for s in recipe["Steps"] if s["Command"] == "Distribute"]
        small_dest = next(d for d in distributes if d["Destinations"] == ["TargetPlate:A1", "TargetPlate:A2"])
        assert small_dest["Volume"] == [2, 5]
        large_dest = next(d for d in distributes if d["Destinations"] == ["TargetPlate:A3", "TargetPlate:A4"])
        assert large_dest["Volume"] == [900, 850]

    def test_multiple_reagents_each_finish_completely_before_the_next_starts(self):
        """A recipe is meant to be run in reagent-sized phases at the bench -
        interleaving reagent A's steps with reagent B's (e.g. all A's 1000uL
        wells, then all B's 1000uL wells, then back to A's 50uL wells) would
        defeat that. Each reagent's own PickUpTip...DropTip block (across
        however many tip sizes it needs, largest first) must be fully
        contiguous before the next reagent's block starts."""
        sheet = {
            "sheet_name": "Test",
            "wells": [
                {"well": "A1", "reagents": {"Solvent": 5, "GEL": 900}},
                {"well": "A2", "reagents": {"Solvent": 900, "GEL": 5}},
            ],
            "summary": {"reagents": ["GEL", "Solvent"]},
        }
        config = {
            "groups": [
                {"name": "GEL", "columns": ["GEL"], "delay_after_seconds": 0, "strategy": "distribute"},
                {"name": "Solvent", "columns": ["Solvent"], "delay_after_seconds": 0, "strategy": "distribute"},
            ],
            "reservoir_type": "nest_12_reservoir_15ml",
            "tip_sizes_ul": [1000, 50],
        }
        recipe = generate_recipe_with_config(sheet, config, recipe_name="test")

        # Walk the step list and record which reagent each Distribute belongs
        # to (from its own Description, which always names the reagent) -
        # the reagent must never change and then change back.
        reagent_sequence = []
        for s in recipe["Steps"]:
            if s["Command"] == "Distribute":
                reagent_sequence.append("GEL" if "GEL" in s["Description"] else "Solvent")
        # Collapse consecutive duplicates: e.g. [GEL, GEL, Solvent, Solvent] -> [GEL, Solvent].
        # If a reagent's steps were non-contiguous (interleaved with another
        # reagent's), it would show up twice in this collapsed list, making
        # the block count exceed the number of distinct reagents.
        collapsed = [r for i, r in enumerate(reagent_sequence) if i == 0 or r != reagent_sequence[i - 1]]
        assert len(collapsed) == len(set(reagent_sequence)), (
            f"a reagent's steps were split across non-contiguous blocks: {reagent_sequence}"
        )

    def test_well_no_tip_covers_is_skipped_with_a_note_not_blocking_the_reagent(self):
        sheet = self._sheet({"A1": 0.5, "A2": 10, "A3": 900})
        recipe = generate_recipe_with_config(sheet, self._config([1000, 50]), recipe_name="test")

        assert "error" not in recipe
        distributed_wells = {
            w for s in recipe["Steps"] if s["Command"] == "Distribute" for w in s["Destinations"]
        }
        assert distributed_wells == {"TargetPlate:A2", "TargetPlate:A3"}
        assert any("A1" in n and "0.5" in n for n in recipe["Metadata"]["Notes"])

    def test_requirements_declares_every_tip_rack_steps_actually_use(self):
        """Regression: Requirements used to always hardcode {"MyTips": "MyTips"}
        regardless of tip_sizes_ul, so a HAL match (Requirements value looked up
        literally in the HAL's Labware) failed with "Requirement 'MyTips' ->
        'MyTips' not found in hardware config" even though the HAL had the
        right racks all along, just under their real names (MyTips_50,
        MyTips_1000, ...)."""
        sheet = self._sheet({"A1": 2, "A2": 900})
        recipe = generate_recipe_with_config(sheet, self._config([1000, 50]), recipe_name="test")
        assert recipe["Requirements"]["MyTips_50"] == "MyTips_50"
        assert recipe["Requirements"]["MyTips_1000"] == "MyTips_1000"
        assert "MyTips" not in recipe["Requirements"]

    def test_no_tip_sizes_configured_keeps_single_mytips_rack(self):
        sheet = self._sheet({"A1": 2, "A2": 900})
        config = {
            "groups": [{"name": "All", "columns": ["Mixed"], "delay_after_seconds": 0}],
            "reservoir_type": "nest_12_reservoir_15ml",
        }
        recipe = generate_recipe_with_config(sheet, config, recipe_name="test")
        pickups = [s["TipRack"] for s in recipe["Steps"] if s["Command"] == "PickUpTip"]
        assert pickups == ["MyTips"]
        assert recipe["Requirements"]["MyTips"] == "MyTips"


class TestTipSizeNeverDoubleBooksAReservoirWell:
    """Regression: a reagent needing MULTIPLE physical reservoir wells
    (total volume exceeds one well's capacity) combined with MULTIPLE tip
    sizes must never let two tip-size groups independently believe they can
    each draw a full well's capacity from the SAME physical well - well
    boundaries are decided once, tip size only refines what happens inside
    an already-fixed boundary."""

    def _inject_cache(self, monkeypatch, load_name, capacity_ul, well_ids):
        import src.excel_recipe_parser as erp
        wells = {w: {"totalLiquidVolume": capacity_ul} for w in well_ids}
        monkeypatch.setattr(erp, "_RESERVOIR_CACHE", {
            load_name: {"ordering": [[w] for w in well_ids], "wells": wells}
        })

    def test_no_source_well_ever_exceeds_its_own_capacity(self, monkeypatch):
        self._inject_cache(monkeypatch, "small_wells", 2000, ["A1", "B1", "C1", "D1", "A2", "B2", "C2", "D2"])
        wells = (
            [{"well": f"A{i}", "reagents": {"Combo": 5}} for i in range(1, 10)]
            + [{"well": f"B{i}", "reagents": {"Combo": 900}} for i in range(1, 15)]
        )
        sheet = {"sheet_name": "Test", "wells": wells, "summary": {"reagents": ["Combo"]}}
        config = {
            "groups": [{"name": "All", "columns": ["Combo"], "delay_after_seconds": 0}],
            "reservoir_type": "small_wells",
            "tip_sizes_ul": [1000, 50],
        }
        recipe = generate_recipe_with_config(sheet, config, recipe_name="test")
        assert "error" not in recipe

        totals_per_source_well = {}
        for step in recipe["Steps"]:
            if step["Command"] != "Distribute":
                continue
            source_well = step["Source"].split(":", 1)[1]
            volumes = step["Volume"] if isinstance(step["Volume"], list) else [step["Volume"]] * len(step["Destinations"])
            totals_per_source_well[source_well] = totals_per_source_well.get(source_well, 0) + sum(volumes)

        for well_id, total in totals_per_source_well.items():
            assert total <= 2000, f"{well_id} was asked to supply {total} uL, exceeding its 2000 uL capacity"

    def test_same_tip_size_across_two_chunks_still_shares_one_pickup(self, monkeypatch):
        """No tip_sizes_ul configured - a reagent needing 2 reservoir wells
        must still hold ONE continuous tip across both, not pick up a fresh
        one per well (today's pre-multi-tip-size behavior, preserved)."""
        self._inject_cache(monkeypatch, "plate_x", 2000, ["A1", "B1"])
        wells = [{"well": f"A{i}", "reagents": {"Big": 100}} for i in range(1, 30)]
        sheet = {"sheet_name": "Test", "wells": wells, "summary": {"reagents": ["Big"]}}
        config = {
            "groups": [{"name": "All", "columns": ["Big"], "delay_after_seconds": 0}],
            "reservoir_type": "plate_x",
        }
        recipe = generate_recipe_with_config(sheet, config, recipe_name="test")
        cmds = [s["Command"] for s in recipe["Steps"]]
        assert cmds.count("PickUpTip") == 1
        assert cmds.count("DropTip") == 1
        assert cmds.count("Distribute") == 2  # still one Distribute per reservoir-well chunk
