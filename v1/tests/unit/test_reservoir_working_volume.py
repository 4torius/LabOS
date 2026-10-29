"""get_reservoir_well_capacity() prefers an explicit per-well `workingVolume`
over `totalLiquidVolume` when the labware file defines one - a deliberate
margin below the true physical max reserving headroom for real dead volume
at the bottom of a well (confirmed on real hardware: a well filled to its
instructed capacity ran dry on its last, small reload before the poured
volume should have been exhausted). Falls back to `totalLiquidVolume`
unchanged when `workingVolume` isn't set, so every reservoir file in the
library today (none of which define it) behaves exactly as before.
"""
import json

import src.excel_recipe_parser as erp
from src.excel_recipe_parser import generate_recipe_with_config


def _reset_cache(monkeypatch):
    monkeypatch.setattr(erp, "_RESERVOIR_CACHE", {})


def _write_reservoir(tmp_path, load_name: str, wells: dict):
    reservoirs_dir = tmp_path / "Labware" / "Reservoirs"
    reservoirs_dir.mkdir(parents=True)
    data = {
        "ordering": [[well_id] for well_id in wells],
        "parameters": {"loadName": load_name},
        "wells": wells,
    }
    (reservoirs_dir / f"{load_name}.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


class TestWorkingVolumeFallback:
    def test_prefers_working_volume_when_present(self, tmp_path, monkeypatch):
        _reset_cache(monkeypatch)
        library_dir = _write_reservoir(
            tmp_path, "custom_plate",
            {"A1": {"totalLiquidVolume": 2000, "workingVolume": 1800}},
        )
        assert erp.get_reservoir_well_capacity("custom_plate", "A1", library_dir=library_dir) == 1800

    def test_falls_back_to_total_liquid_volume_when_working_volume_absent(self, tmp_path, monkeypatch):
        _reset_cache(monkeypatch)
        library_dir = _write_reservoir(
            tmp_path, "custom_plate",
            {"A1": {"totalLiquidVolume": 2000}},
        )
        assert erp.get_reservoir_well_capacity("custom_plate", "A1", library_dir=library_dir) == 2000

    def test_unknown_reservoir_type_returns_none(self, tmp_path, monkeypatch):
        _reset_cache(monkeypatch)
        library_dir = _write_reservoir(tmp_path, "custom_plate", {"A1": {"totalLiquidVolume": 2000}})
        assert erp.get_reservoir_well_capacity("does_not_exist", "A1", library_dir=library_dir) is None

    def test_unknown_well_id_returns_none(self, tmp_path, monkeypatch):
        _reset_cache(monkeypatch)
        library_dir = _write_reservoir(tmp_path, "custom_plate", {"A1": {"totalLiquidVolume": 2000}})
        assert erp.get_reservoir_well_capacity("custom_plate", "Z9", library_dir=library_dir) is None

    def test_claim_wells_for_volume_respects_the_lower_working_volume_ceiling(self, tmp_path, monkeypatch):
        """End-to-end: a well with workingVolume < totalLiquidVolume must
        get filled to the LOWER (working) ceiling before spilling to the
        next well, not the physical max."""
        _reset_cache(monkeypatch)
        library_dir = _write_reservoir(
            tmp_path, "custom_plate",
            {"A1": {"totalLiquidVolume": 2000, "workingVolume": 1800}, "B1": {"totalLiquidVolume": 2000, "workingVolume": 1800}},
        )
        capacity = erp.get_reservoir_well_capacity("custom_plate", "A1", library_dir=library_dir)
        used = set()
        fill_plan, wells_needed = erp._claim_wells_for_volume(2500, capacity, ["A1", "B1"], used)
        assert wells_needed == 2
        assert fill_plan == [("A1", 1800), ("B1", 700)]


def _inject_cache(monkeypatch, load_name: str, wells: dict):
    """generate_recipe_with_config() doesn't accept a library_dir override -
    it always resolves the real repo Library/ path internally. Pre-populate
    _RESERVOIR_CACHE directly instead, so _load_reservoir_catalog() returns
    this synthetic entry without ever touching disk (real reservoir files
    stay untouched by these tests)."""
    monkeypatch.setattr(erp, "_RESERVOIR_CACHE", {
        load_name: {"ordering": [[w] for w in wells], "wells": wells}
    })


class TestWorkingVolumeMustBeBelowMax:
    """generate_recipe_with_config() must refuse to generate (return an
    error, not a silently-wrong loading plan) when a labware's own
    workingVolume is >= its physical totalLiquidVolume - that misconfigures
    away the entire point of the field (reserving real dead-volume margin)."""

    def _sheet(self):
        return {
            "sheet_name": "Test",
            "wells": [{"well": "A1", "reagents": {"X": 100}}],
            "summary": {"reagents": ["X"]},
        }

    def _config(self, reservoir_type):
        return {
            "groups": [{"name": "All", "columns": ["X"], "delay_after_seconds": 0}],
            "reservoir_type": reservoir_type,
        }

    def test_working_volume_equal_to_max_is_rejected(self, monkeypatch):
        _inject_cache(monkeypatch, "bad_plate", {"A1": {"totalLiquidVolume": 2000, "workingVolume": 2000}})
        recipe = generate_recipe_with_config(self._sheet(), self._config("bad_plate"), recipe_name="test")
        assert "error" in recipe
        assert "workingVolume" in recipe["error"]

    def test_working_volume_above_max_is_rejected(self, monkeypatch):
        _inject_cache(monkeypatch, "bad_plate", {"A1": {"totalLiquidVolume": 2000, "workingVolume": 2100}})
        recipe = generate_recipe_with_config(self._sheet(), self._config("bad_plate"), recipe_name="test")
        assert "error" in recipe

    def test_working_volume_below_max_generates_normally(self, monkeypatch):
        _inject_cache(monkeypatch, "good_plate", {"A1": {"totalLiquidVolume": 2000, "workingVolume": 1800}})
        recipe = generate_recipe_with_config(self._sheet(), self._config("good_plate"), recipe_name="test")
        assert "error" not in recipe


class TestReservoirWorkingVolumeConfigOverride:
    """config['reservoir_working_volume_ul'] - a one-off override for a
    single generation, taking priority over the labware file's own
    workingVolume (which stays the reusable per-labware default for every
    OTHER recipe that doesn't override it)."""

    def _sheet(self, nominal):
        # with_excess_ul = nominal*1.1 + 50 (default excess_margin_pct=10,
        # dead_volume_ul=50) - callers pick `nominal` so with_excess lands
        # wherever they need relative to whichever ceiling is in play.
        return {
            "sheet_name": "Test",
            "wells": [{"well": "A1", "reagents": {"X": nominal}}],
            "summary": {"reagents": ["X"]},
        }

    def _config(self, reservoir_type, override=None):
        config = {
            "groups": [{"name": "All", "columns": ["X"], "delay_after_seconds": 0}],
            "reservoir_type": reservoir_type,
        }
        if override is not None:
            config["reservoir_working_volume_ul"] = override
        return config

    def test_override_takes_priority_over_the_labware_files_working_volume(self, monkeypatch):
        """File says workingVolume=1800; override says 1500. X needs
        ~1600uL with_excess - fits under 1800 but NOT under 1500. If the
        override actually took priority, X must split across 2 wells."""
        _inject_cache(monkeypatch, "plate_a", {
            "A1": {"totalLiquidVolume": 2000, "workingVolume": 1800},
            "B1": {"totalLiquidVolume": 2000, "workingVolume": 1800},
        })
        nominal = round((1600 - 50) / 1.1)
        recipe = generate_recipe_with_config(self._sheet(nominal), self._config("plate_a", override=1500), recipe_name="test")
        assert "error" not in recipe
        assert len(recipe["Metadata"]["WellAssignments"]["X"].split(":", 1)[1].split("+")) == 2

    def test_override_used_even_when_labware_has_no_working_volume_at_all(self, monkeypatch):
        """No workingVolume in the file at all (would fall back to the
        2000uL max, fitting X in 1 well) - override of 1500 must still
        force a 2-well split."""
        _inject_cache(monkeypatch, "plate_b", {
            "A1": {"totalLiquidVolume": 2000}, "B1": {"totalLiquidVolume": 2000},
        })
        nominal = round((1600 - 50) / 1.1)
        recipe = generate_recipe_with_config(self._sheet(nominal), self._config("plate_b", override=1500), recipe_name="test")
        assert "error" not in recipe
        assert len(recipe["Metadata"]["WellAssignments"]["X"].split(":", 1)[1].split("+")) == 2

    def test_override_at_or_above_the_physical_max_is_rejected(self, monkeypatch):
        _inject_cache(monkeypatch, "plate_c", {"A1": {"totalLiquidVolume": 2000}})
        recipe = generate_recipe_with_config(self._sheet(100), self._config("plate_c", override=2000), recipe_name="test")
        assert "error" in recipe
        assert "reservoir_working_volume_ul" in recipe["error"]

    def test_no_override_falls_back_to_labware_definition_as_before(self, monkeypatch):
        """File says workingVolume=1800, no override given. X needs
        ~1600uL with_excess - fits under 1800, so 1 well; would need 2 if
        the fallback wrongly used the 2000uL physical max instead? No - it
        would still fit in 1 well under 2000 too, so use a value that
        distinguishes: ~1900uL, fits under 2000 (max) but NOT under 1800
        (the file's own workingVolume) - must split into 2 to prove the
        file's workingVolume (not the max) is really what's in effect."""
        _inject_cache(monkeypatch, "plate_d", {
            "A1": {"totalLiquidVolume": 2000, "workingVolume": 1800},
            "B1": {"totalLiquidVolume": 2000, "workingVolume": 1800},
        })
        nominal = round((1900 - 50) / 1.1)
        recipe = generate_recipe_with_config(self._sheet(nominal), self._config("plate_d"), recipe_name="test")
        assert "error" not in recipe
        assert len(recipe["Metadata"]["WellAssignments"]["X"].split(":", 1)[1].split("+")) == 2
