"""
Tests for the reagent loading plan in src/excel_recipe_parser.py:
_claim_wells_for_volume() and how generate_recipe_with_config() uses it to
size a reagent's PRIMARY wells (not just its backup wells).

Regression coverage for a real bug found on a live 4-reagent SPAAC recipe
sharing one 24-well Corning reservoir (num_reservoirs=1): a reagent needing
more volume than a single well can hold used to only get an overflow-split
across multiple wells when it had a reservoir entirely to itself
("dedicated"). Reagents SHARING a reservoir instead got exactly one well no
matter how much volume they actually needed, producing an impossible
instruction ("load 7042.7 uL into a 3400 uL well") with only a WARNING note,
never actually fixing it. Primary allocation must split across wells the
same way regardless of whether the reservoir is shared.

Also covers the reorganized quantity_notes: one block per reagent (primary
wells immediately followed by its own backup wells), phrased as a concrete
per-well "fill until exhausted" loading plan instead of a single vague
"load at least X" line - and confirms the disposal-volume waste estimate is
no longer spammed into the notes (still tracked in required_volumes as
structured data, just not surfaced as a note).
"""
from src.excel_recipe_parser import _claim_wells_for_volume, generate_recipe_with_config, get_reservoir_well_capacity


class TestClaimWellsForVolume:
    def test_volume_fits_in_one_well(self):
        used = set()
        fill_plan, wells_needed = _claim_wells_for_volume(2000, 3400, ["A1", "B1", "C1"], used)
        assert fill_plan == [("A1", 2000)]
        assert wells_needed == 1
        assert used == {"A1"}

    def test_volume_exceeding_one_well_fills_until_exhausted_not_evenly(self):
        used = set()
        fill_plan, wells_needed = _claim_wells_for_volume(7042.7, 3400, ["D1", "D2", "D3", "D4"], used)
        assert wells_needed == 3  # ceil(7042.7 / 3400)
        assert fill_plan == [("D1", 3400), ("D2", 3400), ("D3", 242.7)]
        assert used == {"D1", "D2", "D3"}

    def test_stops_at_whatever_free_wells_remain(self):
        used = {"A1", "A2"}
        fill_plan, wells_needed = _claim_wells_for_volume(10000, 3400, ["A1", "A2", "A3"], used)
        assert wells_needed == 3
        assert fill_plan == [("A3", 3400)]  # only one well was free

    def test_unknown_capacity_treats_it_as_a_single_well(self):
        used = set()
        fill_plan, wells_needed = _claim_wells_for_volume(50000, None, ["A1", "A2"], used)
        assert wells_needed == 1
        assert fill_plan == [("A1", 50000)]


def _sheet_multi(reagent_volumes: dict):
    """One well ("A1") receiving several reagents at once, like a real
    combinatorial recipe row."""
    return {
        "sheet_name": "Test",
        "wells": [{"well": "A1", "reagents": reagent_volumes}],
        "summary": {"reagents": list(reagent_volumes.keys())},
    }


class TestSharedReservoirOverflowSplit:
    def _config(self, **overrides):
        config = {
            "groups": [{"name": "All", "columns": ["GEL-N3", "HA-N3", "4arm", "Solvent"], "delay_after_seconds": 0}],
            "reservoir_type": "corning_24_wellplate_3.4ml_flat",
            "num_reservoirs": 1,  # force all 4 reagents to share one reservoir
            "pipette": "left",
        }
        config.update(overrides)
        return config

    def test_reagent_needing_more_than_one_well_gets_split_even_when_sharing_a_reservoir(self):
        """Direct regression for the real bug: Solvent needs far more than
        one well can hold, while sharing the reservoir with 3 other
        small-volume reagents - it must now get multiple wells instead of an
        impossible single-well instruction + a warning nobody can act on.

        GEL-N3/HA-N3/4arm are sized relative to the reservoir's REAL current
        well capacity (read from the actual Library/Labware/Reservoirs file,
        not a hardcoded number) specifically so this test can't silently
        drift out of sync with that file again the way it did once already -
        capacity dropped from 3400 to 2000uL (corning_24_wellplate_3.4ml_flat
        was corrected to a realistic working volume) with no code change on
        either side, only this test's own hardcoded assumption went stale.
        """
        capacity = get_reservoir_well_capacity("corning_24_wellplate_3.4ml_flat", "A1")
        # with_excess_ul = nominal*1.1 + 50 (excess_margin_pct=10, dead_volume_ul=50
        # defaults) - keep these three comfortably under one well's capacity,
        # Solvent comfortably over several wells' worth, regardless of what
        # `capacity` actually is today.
        small_nominal = round((capacity * 0.5 - 50) / 1.1)
        sheet = _sheet_multi({"GEL-N3": small_nominal, "HA-N3": 480, "4arm": 423, "Solvent": capacity * 3 + 500})
        recipe = generate_recipe_with_config(sheet, self._config(), recipe_name="test")

        well_assignments = recipe["Metadata"]["WellAssignments"]
        solvent_wells = well_assignments["Solvent"].split(":", 1)[1].split("+")
        assert len(solvent_wells) >= 2  # with-margin total > one well's real capacity
        # Every other reagent still fits in a single well of its own.
        for reagent in ("GEL-N3", "HA-N3", "4arm"):
            assert len(well_assignments[reagent].split(":", 1)[1].split("+")) == 1
        # No collisions between any of the 4 reagents' assigned wells.
        all_wells = [w for spec in well_assignments.values() for w in spec.split(":", 1)[1].split("+")]
        assert len(all_wells) == len(set(all_wells))

        notes = " ".join(recipe["Metadata"]["Notes"])
        assert "impossible" not in notes.lower()
        assert "exceeds the shared well's capacity" not in notes  # the old, unresolved warning is gone

    def test_no_reservoir_capacity_warning_when_everything_fits(self):
        sheet = _sheet_multi({"GEL-N3": 2340, "HA-N3": 480, "4arm": 423, "Solvent": 6357})
        recipe = generate_recipe_with_config(sheet, self._config(), recipe_name="test")
        notes = " ".join(recipe["Metadata"]["Notes"])
        assert "WARNING" not in notes  # 24 wells is plenty once Solvent is allowed to split


class TestLoadingPlanNotes:
    def _config(self, **overrides):
        config = {
            "groups": [{"name": "All", "columns": ["HA"], "delay_after_seconds": 0}],
            "reservoir_type": "nest_12_reservoir_15ml",
            "pipette": "left",
        }
        config.update(overrides)
        return config

    def test_single_well_reagent_gets_one_concise_load_line(self):
        sheet = {"sheet_name": "Test", "wells": [{"well": "A1", "reagents": {"HA": 100}}],
                  "summary": {"reagents": ["HA"]}}
        recipe = generate_recipe_with_config(sheet, self._config(), recipe_name="test")
        notes = recipe["Metadata"]["Notes"]
        assert any(n.startswith("HA:") and "load" in n and "Source_1:A1" in n for n in notes)

    def test_multi_well_reagent_gets_a_per_well_fill_breakdown(self):
        sheet = {"sheet_name": "Test", "wells": [{"well": "A1", "reagents": {"HA": 20000}}],
                  "summary": {"reagents": ["HA"]}}
        recipe = generate_recipe_with_config(sheet, self._config(), recipe_name="test")
        notes = " ".join(recipe["Metadata"]["Notes"])
        assert "fill in order until the volume is used up" in notes
        assert "A1 = 15000" in notes  # first well filled to capacity...
        assert "A2 = " in notes       # ...remainder in the next one

    def test_backup_note_is_a_separate_line_placed_right_after_the_primary_one(self):
        sheet = {"sheet_name": "Test", "wells": [{"well": "A1", "reagents": {"HA": 100}}],
                  "summary": {"reagents": ["HA"]}}
        config = self._config(reagent_advanced={"HA": {"verify_liquid": True}})
        recipe = generate_recipe_with_config(sheet, config, recipe_name="test")
        notes = recipe["Metadata"]["Notes"]
        primary_idx = next(i for i, n in enumerate(notes) if n.startswith("HA:") and "load" in n)
        assert notes[primary_idx + 1].startswith("HA: backup")

    def test_waste_estimate_is_not_surfaced_as_a_note(self):
        sheet = {"sheet_name": "Test", "wells": [{"well": w, "reagents": {"HA": 20}} for w in ["A1", "A2", "A3"]],
                  "summary": {"reagents": ["HA"]}}
        config = self._config(pipette_max_volume_ul=30, pipette_min_volume_ul=5)
        recipe = generate_recipe_with_config(sheet, config, recipe_name="test")
        notes = " ".join(recipe["Metadata"]["Notes"])
        assert "waste" not in notes.lower()
        # Still tracked as structured data for anything that wants it.
        assert recipe["Metadata"]["RequiredVolumes"]["HA"].get("estimated_waste_ul", 0) > 0