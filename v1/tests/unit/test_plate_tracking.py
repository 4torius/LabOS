"""
Tests for src/plate_tracking.py — the DB-write logic shared between the
workflow-execution path (webapp/routes/workflows.py) and the manual/direct
command path (webapp/routes/instruments.py).
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import database as db
from src import plate_tracking as pt
from src.catalog import Traceability

PREPARES = Traceability(role="prepares_plate", recipe_parameter="RecipeName")
MEASURES = Traceability(role="measures_plate", plate_parameter="PlateID",
                        protocol_parameter="ProtocolFile")


@pytest.fixture
def fresh_db(tmp_path):
    db.configure(tmp_path / "test.db")
    db.init_db()
    yield db
    db._DB_PATH = None


def _fake_result(data: dict, success: bool = True):
    return SimpleNamespace(success=success, data=data, message="", error=None)


class TestFindPlateDefinition:
    def test_matches_by_internal_id_field_when_filename_differs(self, tmp_path):
        """Regression for the '?×?' dimensions bug: the real repo has
        corning_24_wellplate_3400ul_flat.plate.json whose internal "id" is
        "corning_24_wellplate_3.4ml_flat" — the HAL LoadName that must
        resolve against it."""
        plates_dir = tmp_path / "Plates"
        plates_dir.mkdir()
        (plates_dir / "corning_24_wellplate_3400ul_flat.plate.json").write_text(
            json.dumps({"id": "corning_24_wellplate_3.4ml_flat", "rows": 4, "columns": 6}),
            encoding="utf-8",
        )
        found = pt.find_plate_definition(plates_dir, "corning_24_wellplate_3.4ml_flat")
        assert found is not None
        assert found["rows"] == 4 and found["columns"] == 6

    def test_matches_by_filename_stem_as_fallback(self, tmp_path):
        plates_dir = tmp_path / "Plates"
        plates_dir.mkdir()
        (plates_dir / "my_plate.plate.json").write_text(
            json.dumps({"id": "something_else", "rows": 8, "columns": 12}), encoding="utf-8"
        )
        found = pt.find_plate_definition(plates_dir, "my_plate")
        assert found is not None

    def test_returns_none_when_nothing_matches(self, tmp_path):
        plates_dir = tmp_path / "Plates"
        plates_dir.mkdir()
        assert pt.find_plate_definition(plates_dir, "nonexistent") is None


class TestExtractWellsFromRecipe:
    def _write_recipe(self, base_dir: Path, name: str, steps: list, metadata: dict = None):
        recipes_dir = base_dir / "Library" / "Recipes"
        recipes_dir.mkdir(parents=True, exist_ok=True)
        recipe = {"Steps": steps}
        if metadata:
            recipe["Metadata"] = metadata
        (recipes_dir / f"{name}.json").write_text(json.dumps(recipe), encoding="utf-8")

    def test_two_different_reagents_into_same_well_both_kept(self, tmp_path):
        """Direct regression test for the corrected bug #1 root cause: the
        well_records accumulator used to be keyed by well_id alone, so a
        second reagent distributed into an already-seen well silently
        overwrote the first."""
        self._write_recipe(tmp_path, "test_recipe", [
            {"Command": "Distribute", "Source": "Source_1:A1", "Destinations": ["TargetPlate:A1"], "Volume": 50},
            {"Command": "Distribute", "Source": "Source_2:A1", "Destinations": ["TargetPlate:A1"], "Volume": 20},
        ])
        wells = pt._extract_wells_from_recipe("test_recipe", "P1", tmp_path)
        by_well = {}
        for w in wells:
            by_well.setdefault(w["well_id"], []).append(w["reagent_name"])
        assert set(by_well["A1"]) == {"Source_1", "Source_2"}
        assert len(wells) == 2

    def test_same_reagent_twice_into_same_well_accumulates_volume(self, tmp_path):
        self._write_recipe(tmp_path, "test_recipe", [
            {"Command": "Distribute", "Source": "Source_1:A1", "Destinations": ["TargetPlate:A1"], "Volume": 30},
            {"Command": "Distribute", "Source": "Source_1:A1", "Destinations": ["TargetPlate:A1"], "Volume": 15},
        ])
        wells = pt._extract_wells_from_recipe("test_recipe", "P1", tmp_path)
        assert len(wells) == 1
        assert wells[0]["volume_ul"] == 45

    def test_missing_recipe_file_returns_empty_list(self, tmp_path):
        assert pt._extract_wells_from_recipe("does_not_exist", "P1", tmp_path) == []

    def test_volume_as_list_is_resolved_per_destination_not_crashed_on(self, tmp_path):
        """Regression: Distribute's "Volume" can be a LIST with one value per
        destination (e.g. a combinatorial dilution series - see the real
        01_SPAAC_hydrogel recipe this was found in), not just a scalar shared
        by all destinations. float(list) used to raise, and the try/except
        around the whole function swallowed it silently, so a single
        list-valued step emptied the well extraction for the ENTIRE recipe -
        not just its own destinations."""
        self._write_recipe(tmp_path, "test_recipe", [
            {"Command": "Distribute", "Source": "Source_1:A1",
             "Destinations": ["TargetPlate:A1", "TargetPlate:A2", "TargetPlate:A3"],
             "Volume": [50, 40, 30]},
        ])
        wells = pt._extract_wells_from_recipe("test_recipe", "P1", tmp_path)
        by_well = {w["well_id"]: w["volume_ul"] for w in wells}
        assert by_well == {"A1": 50, "A2": 40, "A3": 30}

    def test_volume_as_scalar_still_applies_to_every_destination(self, tmp_path):
        """Same code path as above, scalar case - must stay exactly as before."""
        self._write_recipe(tmp_path, "test_recipe", [
            {"Command": "Distribute", "Source": "Source_1:A1",
             "Destinations": ["TargetPlate:A1", "TargetPlate:A2"], "Volume": 25},
        ])
        wells = pt._extract_wells_from_recipe("test_recipe", "P1", tmp_path)
        by_well = {w["well_id"]: w["volume_ul"] for w in wells}
        assert by_well == {"A1": 25, "A2": 25}

    def test_resolves_real_reagent_name_via_well_assignments_not_the_generic_source_key(self, tmp_path):
        """Regression: recipes generated with the numbered-source convention
        (Source_1, Source_2, ...) never put the real reagent name in a step's
        "Source" field - it's always "Source_N:well". Naively taking the
        prefix (the old behaviour) gave "Source_1" as the "reagent", not the
        actual chemical - Metadata.WellAssignments (built by
        excel_recipe_parser.py) is what actually says which reagent owns
        which well."""
        self._write_recipe(tmp_path, "test_recipe", [
            {"Command": "Distribute", "Source": "Source_1:A1",
             "Destinations": ["TargetPlate:A1"], "Volume": 50},
            {"Command": "Distribute", "Source": "Source_1:B1",
             "Destinations": ["TargetPlate:A2"], "Volume": 30},
        ], metadata={"WellAssignments": {"GEL-N3": "Source_1:A1", "HA-N3": "Source_1:B1"}})
        wells = pt._extract_wells_from_recipe("test_recipe", "P1", tmp_path)
        by_well = {w["well_id"]: w["reagent_name"] for w in wells}
        assert by_well == {"A1": "GEL-N3", "A2": "HA-N3"}

    def test_falls_back_to_source_prefix_when_no_well_assignments_metadata(self, tmp_path):
        """Old-style/hand-written recipes with no Metadata.WellAssignments at
        all (e.g. a reagent name used directly as the Source prefix) must
        keep working exactly as before."""
        self._write_recipe(tmp_path, "test_recipe", [
            {"Command": "Distribute", "Source": "HA:A1", "Destinations": ["TargetPlate:A1"], "Volume": 50},
        ])
        wells = pt._extract_wells_from_recipe("test_recipe", "P1", tmp_path)
        assert wells[0]["reagent_name"] == "HA"

    def test_liquid_fallback_candidate_list_source_resolves_via_first_candidate(self, tmp_path):
        """Source can be a LIST of fallback candidates (VerifyLiquid + backup
        wells - see protocol_generator.py's runtime fallback branch). All
        candidates belong to the same reagent, so resolution just needs the
        first one; the full list is kept (joined) in source_well for display."""
        self._write_recipe(tmp_path, "test_recipe", [
            {"Command": "Distribute", "Source": ["Source_1:A1", "Source_1:B1"],
             "Destinations": ["TargetPlate:A1"], "Volume": 50, "VerifyLiquid": True},
        ], metadata={"WellAssignments": {"GEL-N3": "Source_1:A1+B1"}})
        wells = pt._extract_wells_from_recipe("test_recipe", "P1", tmp_path)
        assert wells[0]["reagent_name"] == "GEL-N3"
        assert wells[0]["source_well"] == "Source_1:A1+Source_1:B1"


class TestApplyConcentrations:
    def test_computes_when_stock_and_volume_known(self, fresh_db):
        fresh_db.get_or_create_reagent("HA", {"stock_concentration_mm": 100})
        wells = [{"well_id": "A1", "reagent_name": "HA", "volume_ul": 50, "concentration_mm": None}]
        pt._apply_concentrations(wells)
        assert wells[0]["concentration_mm"] == pytest.approx(100.0)  # sole reagent -> full stock conc

    def test_dilutes_across_multiple_reagents_in_same_well(self, fresh_db):
        fresh_db.get_or_create_reagent("HA", {"stock_concentration_mm": 100})
        wells = [
            {"well_id": "A1", "reagent_name": "HA", "volume_ul": 50, "concentration_mm": None},
            {"well_id": "A1", "reagent_name": "DMTMM", "volume_ul": 50, "concentration_mm": None},
        ]
        pt._apply_concentrations(wells)
        assert wells[0]["concentration_mm"] == pytest.approx(50.0)  # diluted 1:1 by the second reagent's volume

    def test_leaves_null_when_stock_unknown(self, fresh_db):
        wells = [{"well_id": "A1", "reagent_name": "Unregistered", "volume_ul": 50, "concentration_mm": None}]
        pt._apply_concentrations(wells)
        assert wells[0]["concentration_mm"] is None


class TestTrackPlatePreparation:
    """Gated on the role the server declared, not on the instrument's name."""

    def test_ignores_a_command_with_no_declared_role(self, fresh_db, tmp_path):
        result = pt.track_plate_preparation(
            "opentrons", "ExecuteRecipe", {"RecipeName": "r"}, _fake_result({}),
            role=None, run_id=None, base_dir=tmp_path,
        )
        assert result is None, "a name that used to match is no longer enough"

    def test_ignores_a_measuring_command(self, fresh_db, tmp_path):
        assert pt.track_plate_preparation(
            "reader", "Read", {}, _fake_result({}),
            role=MEASURES, run_id=None, base_dir=tmp_path,
        ) is None

    def test_any_instrument_that_declares_the_role_is_tracked(self, fresh_db, tmp_path):
        plate_id = pt.track_plate_preparation(
            "second_liquid_handler", "RunProtocol", {"RecipeName": "my_recipe"}, _fake_result({}),
            role=PREPARES, run_id=None, base_dir=tmp_path,
        )
        assert plate_id and plate_id.startswith("PLT-")

    def test_creates_plate_row_on_execute_recipe(self, fresh_db, tmp_path):
        # plates.run_id has FOREIGN KEY REFERENCES run_results(run_id) — the
        # real caller (workflows.py) now pre-creates this row before any step
        # runs (see the "Failed to save plate: FOREIGN KEY constraint
        # failed" bug this test caught before that fix existed).
        fresh_db.save_run({"run_id": "R1", "workflow_name": "wf", "status": "running", "step_results": []})

        plate_id = pt.track_plate_preparation(
            "opentrons", "ExecuteRecipe", {"RecipeName": "my_recipe"}, _fake_result({}),
            role=PREPARES, run_id="R1", base_dir=tmp_path,
        )
        assert plate_id is not None
        assert plate_id.startswith("PLT-")
        plate = fresh_db.get_plate(plate_id)
        assert plate is not None
        assert plate["run_id"] == "R1"
        assert plate["status"] == "prepared"

    def test_run_id_referencing_nonexistent_run_fails_loudly_not_silently(self, fresh_db, tmp_path, caplog):
        """Documents the FK-constraint behavior directly: saving a plate
        with a run_id that has no run_results row logs an error and the
        plate is NOT created — this is exactly the bug the pre-create-the-
        run-row fix in workflows.py exists to avoid hitting in production."""
        plate_id = pt.track_plate_preparation(
            "opentrons", "ExecuteRecipe", {"RecipeName": "my_recipe"}, _fake_result({}),
            role=PREPARES, run_id="R-does-not-exist", base_dir=tmp_path,
        )
        assert plate_id is not None  # the id is still generated/returned...
        assert fresh_db.get_plate(plate_id) is None  # ...but nothing was actually persisted
        assert "FOREIGN KEY constraint failed" in caplog.text


class TestTrackPlateMeasurement:
    def test_ignores_a_command_with_no_declared_role(self, fresh_db, tmp_path):
        result = pt.track_plate_measurement(
            "tecan", "RunMeasurement", {"PlateID": "P1"}, _fake_result({}),
            role=None, run_id=None, current_plate_id=None, base_dir=tmp_path,
        )
        assert result is None

    def test_the_result_field_is_found_whatever_the_server_called_it(self, fresh_db, tmp_path):
        """A SiLA2 response comes back keyed by its own identifier."""
        animl_file = tmp_path / "P7_20260115_143022.animl"
        animl_file.write_text("<AnIML/>", encoding="utf-8")
        result = pt.track_plate_measurement(
            "fluorimeter", "Read", {}, _fake_result({"ResultFile": str(animl_file)}),
            role=MEASURES, run_id=None, current_plate_id=None, base_dir=tmp_path,
        )
        assert result == "P7"

    def test_derives_plate_id_from_animl_filename_when_none_supplied(self, fresh_db, tmp_path):
        """Regression for the corrected PlateID-reconciliation fix (§3.4):
        the AnIML filename is "{cleanPlateId}_{yyyyMMdd_HHmmss}.animl" — the
        effective plate id must be recovered by stripping that trailing
        timestamp, not by using the raw filename stem."""
        animl_dir = tmp_path / "AnIML"
        animl_dir.mkdir()
        animl_file = animl_dir / "abs_450nm_20260115_143022.animl"
        animl_file.write_text("<AnIML/>", encoding="utf-8")

        result = pt.track_plate_measurement(
            "tecan", "RunMeasurement", {"PlateID": ""},
            _fake_result({"animl_file_path": str(animl_file)}),
            role=MEASURES, run_id=None, current_plate_id=None, base_dir=tmp_path,
        )
        assert result == "abs_450nm"

    def test_manual_measurement_with_no_plate_still_creates_plate_row(self, fresh_db, tmp_path, monkeypatch):
        """Bug #13: a measurement fired with no run/plate context (the
        Dashboard's direct-command path) must still result in a discoverable
        plates row, not silently vanish into the ephemeral command log."""
        animl_dir = tmp_path / "AnIML"
        animl_dir.mkdir()
        animl_file = animl_dir / "TestProtocol_20260115_150000.animl"
        animl_file.write_text("<AnIML/>", encoding="utf-8")

        monkeypatch.setattr(
            pt, "parse_animl", lambda p: {"experiment_steps": []}, raising=False
        )
        # parse_animl is imported lazily inside track_plate_measurement via
        # `from src.animl_parser import parse_animl` — patch at the source.
        import src.animl_parser as animl_parser
        monkeypatch.setattr(animl_parser, "parse_animl", lambda p: {"experiment_steps": []})

        plate_id = pt.track_plate_measurement(
            "tecan", "RunMeasurement", {"PlateID": ""},
            _fake_result({"animl_file_path": str(animl_file)}),
            role=MEASURES, run_id=None, current_plate_id=None, base_dir=tmp_path,
        )
        assert plate_id == "TestProtocol"
        plate = fresh_db.get_plate(plate_id)
        assert plate is not None
        assert plate["status"] == "measured"
        assert "manuale" in (plate["notes"] or "").lower() or "manual" in (plate["notes"] or "").lower()

    def test_does_not_overwrite_existing_richer_plate_record(self, fresh_db, tmp_path):
        """ensure_plate_for_measurement() must not clobber a plate already
        created by an earlier ExecuteRecipe step in the same workflow."""
        fresh_db.save_plate({"plate_id": "PLT-existing", "recipe_name": "my_recipe.json", "status": "prepared"})
        pt.ensure_plate_for_measurement("PLT-existing", run_id="R1")
        plate = fresh_db.get_plate("PLT-existing")
        assert plate["recipe_name"] == "my_recipe.json"
        assert plate["status"] == "prepared"  # unchanged, not reset to "measured"
