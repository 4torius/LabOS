"""
Tests for src/database.py's chemical/automation schema — regression coverage
for the bugs found analyzing Run History / Plates screenshots:

- bug #1/#9: a well can hold multiple reagents (real UNIQUE index, not a
  Python dict keyed by well_id alone — that fix lives in plate_tracking.py,
  this file just verifies the DB-level constraint behaves as intended).
- bug #3/#4: instrument_measurements.animl_path was always NULL because it
  was only ever written by a lossy end-of-run replay in save_run(); now
  save_instrument_measurement() writes it eagerly and returns a usable id
  immediately, no query race.
- bug #6: get_well_heatmap() returned a shape (`wells: {well_id: {...}}`)
  that plates.html/database.html never actually consumed (`grid`/
  `row_labels`/`value_min`/`value_max`) — the heatmap literally never
  rendered for any plate. Also verifies the union-of-wells-and-measurements
  query fix (a measured well with no pipetting record still appears).
- bug #7: multiple experiment_steps/techniques in one AnIML file get their
  own protocol_channels row instead of collapsing into one protocols row.
"""
import sqlite3

import pytest

from src import database as db


@pytest.fixture
def fresh_db(tmp_path):
    db.configure(tmp_path / "test.db")
    db.init_db()
    yield db
    db._DB_PATH = None  # avoid leaking configuration into unrelated tests


class TestSchema:
    def test_init_db_creates_all_tables_including_protocol_channels(self, fresh_db):
        with fresh_db._conn() as con:
            tables = {
                r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        expected = set(fresh_db.CHEMICAL_TABLES) | set(fresh_db.AUTOMATION_TABLES)
        assert expected.issubset(tables)
        assert "protocol_channels" in tables


class TestMultiReagentWells:
    def test_two_reagents_in_same_well_both_persist(self, fresh_db):
        fresh_db.save_plate({"plate_id": "P1", "rows": 8, "columns": 12})
        fresh_db.save_wells([
            {"plate_id": "P1", "well_id": "A1", "row_label": "A", "col_number": 1,
             "reagent_name": "HA", "volume_ul": 50},
            {"plate_id": "P1", "well_id": "A1", "row_label": "A", "col_number": 1,
             "reagent_name": "DMTMM", "volume_ul": 20},
        ])
        plate = fresh_db.get_plate("P1")
        reagents = {w["reagent_name"] for w in plate["wells"]}
        assert reagents == {"HA", "DMTMM"}

    def test_true_duplicate_plate_well_reagent_is_rejected(self, fresh_db):
        fresh_db.save_plate({"plate_id": "P1", "rows": 8, "columns": 12})
        row = {"plate_id": "P1", "well_id": "A1", "row_label": "A", "col_number": 1,
               "reagent_name": "HA", "volume_ul": 50}
        fresh_db.save_wells([row])
        fresh_db.save_wells([dict(row, volume_ul=999)])  # same (plate, well, reagent) again
        plate = fresh_db.get_plate("P1")
        ha_rows = [w for w in plate["wells"] if w["reagent_name"] == "HA"]
        assert len(ha_rows) == 1
        assert ha_rows[0]["volume_ul"] == 50  # INSERT OR IGNORE kept the first row


class TestInstrumentMeasurements:
    def test_save_instrument_measurement_returns_id_immediately(self, fresh_db):
        fresh_db.save_plate({"plate_id": "P1"})
        mid = fresh_db.save_instrument_measurement({
            "run_id": None, "plate_id": "P1", "instrument": "tecan",
            "command": "RunMeasurement", "animl_path": "/x/P1.animl",
        })
        assert mid is not None
        with fresh_db._conn() as con:
            row = con.execute(
                "SELECT animl_path, plate_id FROM instrument_measurements WHERE id = ?", (mid,)
            ).fetchone()
        assert row["animl_path"] == "/x/P1.animl"
        assert row["plate_id"] == "P1"

    def test_get_last_measurement_id_no_longer_exists(self, fresh_db):
        # Confirms the dead, race-prone function was actually removed, not
        # just unused.
        assert not hasattr(fresh_db, "get_last_measurement_id")

    def test_save_run_no_longer_touches_instrument_measurements(self, fresh_db):
        fresh_db.save_run({
            "run_id": "R1", "workflow_name": "wf", "status": "completed",
            "step_results": [{"instrument": "Tecan", "command": "RunMeasurement",
                               "result": {"data": {"animl_file_path": "/should/not/be/used.animl"}}}],
        })
        with fresh_db._conn() as con:
            count = con.execute(
                "SELECT COUNT(*) AS c FROM instrument_measurements WHERE run_id = ?", ("R1",)
            ).fetchone()["c"]
        assert count == 0


class TestExportPlateJsonPrefersXml:
    """export_plate_json()'s per-raw-result 'animl_data' must carry the FULL
    measurement (spectrum included, for a scan) and must come from the same
    source plate_tracking.ingest_animl_measurement() prefers - the raw
    Tecan XML sibling, when present - so this export can never disagree
    with what's actually in well_measurements."""

    def _xml(self, well_value="9.99"):
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<MeasurementResultData Date="2026-01-01T00:00:00Z">
    <Header Application="Tecan i-control">
        <Instrument><Name>x</Name><SerialNumber>1</SerialNumber></Instrument>
        <Parameters><Parameter Name="Plate" Value="P" /></Parameters>
    </Header>
    <Section Name="Label1" Time_Start="2026-01-01T00:00:00Z" Time_End="2026-01-01T00:01:00Z">
        <Parameters><Parameter Name="Mode" Value="Absorbance" /></Parameters>
        <Data Cycle="1"><Well Pos="A1" Type="Single"><Single>{well_value}</Single></Well></Data>
    </Section>
</MeasurementResultData>
"""

    def _animl(self, well_value="0.01"):
        ns = "urn:org:astm:animl:schema:core:draft:0.90"
        return f"""<?xml version="1.0" encoding="utf-8"?>
<animl:AnIML version="0.90" xmlns:animl="{ns}">
  <animl:SampleSet id="ss1" name="P"><animl:Container id="c" name="P" containerType="MicrotiterPlate"/></animl:SampleSet>
  <animl:ExperimentStepSet id="e"><animl:ExperimentStep id="s" name="Label1">
    <animl:Technique name="Absorbance"/>
    <animl:ResultSet id="r"><animl:SeriesSet id="ss" name="Well Readings" length="1">
      <animl:Series id="w" name="Well Position" dependency="independent" seriesType="String" plotScale="none">
        <animl:EncodedValueSet startIndex="0" endIndex="0"><animl:IndividualValueSet><animl:S>A1</animl:S></animl:IndividualValueSet></animl:EncodedValueSet>
      </animl:Series>
      <animl:Series id="v" name="Optical Density" dependency="dependent" seriesType="Float64" plotScale="linear">
        <animl:Unit label="OD"/>
        <animl:EncodedValueSet startIndex="0" endIndex="0"><animl:IndividualValueSet><animl:F>{well_value}</animl:F></animl:IndividualValueSet></animl:EncodedValueSet>
      </animl:Series>
    </animl:SeriesSet></animl:ResultSet>
  </animl:ExperimentStep></animl:ExperimentStepSet>
</animl:AnIML>
"""

    def test_prefers_sibling_xml_value_over_the_animl_files_value(self, fresh_db, tmp_path):
        (tmp_path / "AnIML").mkdir()
        (tmp_path / "XML").mkdir()
        (tmp_path / "XML" / "p1.xml").write_text(self._xml("9.99"), encoding="utf-8")
        (tmp_path / "AnIML" / "p1.animl").write_text(self._animl("0.01"), encoding="utf-8")

        fresh_db.save_plate({"plate_id": "P1"})
        fresh_db.save_instrument_measurement({
            "plate_id": "P1", "instrument": "tecan", "command": "RunMeasurement",
            "animl_path": str(tmp_path / "AnIML" / "p1.animl"),
        })

        data = fresh_db.export_plate_json("P1")
        rr = data["plate"]["raw_results"][0]
        assert rr["animl_data"]["experiment_steps"][0]["measurements"][0]["value"] == 9.99

    def test_falls_back_to_animl_when_no_sibling_xml_exists(self, fresh_db, tmp_path):
        (tmp_path / "AnIML").mkdir()
        (tmp_path / "AnIML" / "p2.animl").write_text(self._animl("0.42"), encoding="utf-8")

        fresh_db.save_plate({"plate_id": "P2"})
        fresh_db.save_instrument_measurement({
            "plate_id": "P2", "instrument": "tecan", "command": "RunMeasurement",
            "animl_path": str(tmp_path / "AnIML" / "p2.animl"),
        })

        data = fresh_db.export_plate_json("P2")
        rr = data["plate"]["raw_results"][0]
        assert rr["animl_data"]["experiment_steps"][0]["measurements"][0]["value"] == 0.42


class TestProtocolChannels:
    def test_distinct_step_names_get_distinct_channels(self, fresh_db):
        pid = fresh_db.get_or_create_protocol("abs_450nm.mdfx")
        c1 = fresh_db.get_or_create_protocol_channel(pid, "AbsorbanceStep", {"measurement_type": "Absorbance"})
        c2 = fresh_db.get_or_create_protocol_channel(pid, "FluorescenceStep", {"measurement_type": "Fluorescence"})
        assert c1 != c2
        # Re-requesting the same step_name returns the same row, not a duplicate.
        c1_again = fresh_db.get_or_create_protocol_channel(pid, "AbsorbanceStep")
        assert c1_again == c1

    def test_well_measurement_protocol_channel_id_round_trips(self, fresh_db):
        fresh_db.save_plate({"plate_id": "P1"})
        pid = fresh_db.get_or_create_protocol("abs_450nm.mdfx")
        cid = fresh_db.get_or_create_protocol_channel(pid, "Step1", {"measurement_type": "Absorbance"})
        mid = fresh_db.save_instrument_measurement({"plate_id": "P1", "instrument": "tecan", "command": "RunMeasurement"})
        fresh_db.save_well_measurements("P1", mid, [
            {"well": "A1", "value": 0.5, "unit": "AU", "protocol_channel_id": cid},
        ], protocol_id=pid)
        plate = fresh_db.get_plate("P1")
        assert plate["measurements"][0]["protocol_channel_id"] == cid


class TestWellHeatmap:
    def test_shape_has_grid_row_labels_and_value_range(self, fresh_db):
        fresh_db.save_plate({"plate_id": "P1", "rows": 2, "columns": 3})
        fresh_db.save_wells([
            {"plate_id": "P1", "well_id": "A1", "row_label": "A", "col_number": 1, "reagent_name": "HA", "volume_ul": 50},
        ])
        mid = fresh_db.save_instrument_measurement({"plate_id": "P1", "instrument": "tecan", "command": "RunMeasurement"})
        fresh_db.save_well_measurements("P1", mid, [{"well": "A1", "value": 0.2, "unit": "AU"}])

        hm = fresh_db.get_well_heatmap("P1")
        assert set(hm.keys()) >= {"plate_id", "rows", "columns", "row_labels", "value_min", "value_max", "grid"}
        assert hm["rows"] == 2 and hm["columns"] == 3
        assert hm["row_labels"] == ["A", "B"]
        assert len(hm["grid"]) == 2 and len(hm["grid"][0]) == 3
        a1 = hm["grid"][0][0]
        assert a1["well_id"] == "A1"
        assert a1["value"] == 0.2
        assert hm["value_min"] == 0.2 and hm["value_max"] == 0.2

    def test_measured_well_without_wells_row_still_appears(self, fresh_db):
        """A manual/no-recipe measurement has no `wells` row at all — the
        heatmap must still show the measured value instead of the well
        silently disappearing (the old query was driven by `wells` only)."""
        fresh_db.save_plate({"plate_id": "P1", "rows": 1, "columns": 1})
        mid = fresh_db.save_instrument_measurement({"plate_id": "P1", "instrument": "tecan", "command": "RunMeasurement"})
        fresh_db.save_well_measurements("P1", mid, [{"well": "A1", "value": 0.9, "unit": "AU"}])

        hm = fresh_db.get_well_heatmap("P1")
        assert hm["grid"][0][0]["value"] == 0.9
        assert hm["grid"][0][0]["reagent_name"] is None

    def test_empty_plate_returns_no_values(self, fresh_db):
        fresh_db.save_plate({"plate_id": "P1", "rows": 1, "columns": 1})
        hm = fresh_db.get_well_heatmap("P1")
        assert hm["value_min"] is None and hm["value_max"] is None
        assert hm["grid"][0][0]["value"] is None

    def test_multiple_timepoints_of_the_same_technique_average_by_default(self, fresh_db):
        """Regression: a plate read at several timepoints (t0/3h/24h, all
        'Absorbance') gets one instrument_measurements row per timepoint -
        with no measurement_id filter, the well's value is the AVG across
        every one of them (today's existing/default behavior, kept), which
        looks like real data but matches no single actual reading."""
        fresh_db.save_plate({"plate_id": "P1", "rows": 1, "columns": 1})
        mid1 = fresh_db.save_instrument_measurement({"plate_id": "P1", "instrument": "tecan", "command": "RunMeasurement"})
        mid2 = fresh_db.save_instrument_measurement({"plate_id": "P1", "instrument": "tecan", "command": "RunMeasurement"})
        fresh_db.save_well_measurements("P1", mid1, [{"well": "A1", "value": 0.2, "unit": "AU", "measurement_type": "Absorbance"}])
        fresh_db.save_well_measurements("P1", mid2, [{"well": "A1", "value": 0.8, "unit": "AU", "measurement_type": "Absorbance"}])

        combined = fresh_db.get_well_heatmap("P1")
        assert combined["grid"][0][0]["value"] == pytest.approx(0.5)

    def test_measurement_id_selects_one_raw_result_instead_of_averaging(self, fresh_db):
        fresh_db.save_plate({"plate_id": "P1", "rows": 1, "columns": 1})
        mid1 = fresh_db.save_instrument_measurement({"plate_id": "P1", "instrument": "tecan", "command": "RunMeasurement"})
        mid2 = fresh_db.save_instrument_measurement({"plate_id": "P1", "instrument": "tecan", "command": "RunMeasurement"})
        fresh_db.save_well_measurements("P1", mid1, [{"well": "A1", "value": 0.2, "unit": "AU"}])
        fresh_db.save_well_measurements("P1", mid2, [{"well": "A1", "value": 0.8, "unit": "AU"}])

        hm1 = fresh_db.get_well_heatmap("P1", measurement_id=mid1)
        hm2 = fresh_db.get_well_heatmap("P1", measurement_id=mid2)
        assert hm1["grid"][0][0]["value"] == 0.2
        assert hm2["grid"][0][0]["value"] == 0.8


class TestConcentration:
    def test_null_concentration_round_trips_untouched(self, fresh_db):
        fresh_db.save_plate({"plate_id": "P1"})
        fresh_db.save_wells([
            {"plate_id": "P1", "well_id": "A1", "row_label": "A", "col_number": 1,
             "reagent_name": "HA", "volume_ul": 50, "concentration_mm": None},
        ])
        plate = fresh_db.get_plate("P1")
        assert plate["wells"][0]["concentration_mm"] is None


class TestSearchPlates:
    """search_plates() mirrors search_runs() - same query/date-range shape,
    added so the Plates tab can offer the same "search by name and date" the
    Runs & Log tab already had."""

    def test_matches_recipe_name_plate_id_or_display_name(self, fresh_db):
        fresh_db.save_plate({"plate_id": "PLT-1", "recipe_name": "SPAAC_hydrogel_20260901"})
        fresh_db.save_plate({"plate_id": "PLT-2", "recipe_name": "Other_recipe"})
        fresh_db.save_plate({"plate_id": "PLT-3", "recipe_name": "Third", "display_name": "My SPAAC run"})

        by_recipe = fresh_db.search_plates(query="spaac")
        assert {p["plate_id"] for p in by_recipe} == {"PLT-1", "PLT-3"}

        by_id = fresh_db.search_plates(query="PLT-2")
        assert {p["plate_id"] for p in by_id} == {"PLT-2"}

    def test_date_range_filters_by_prepared_at(self, fresh_db):
        fresh_db.save_plate({"plate_id": "OLD", "prepared_at": "2026-01-01T10:00:00"})
        fresh_db.save_plate({"plate_id": "NEW", "prepared_at": "2026-06-01T10:00:00"})

        recent = fresh_db.search_plates(date_from="2026-03-01")
        assert {p["plate_id"] for p in recent} == {"NEW"}

        old = fresh_db.search_plates(date_to="2026-03-01")
        assert {p["plate_id"] for p in old} == {"OLD"}

    def test_no_filters_returns_everything_like_get_plates(self, fresh_db):
        fresh_db.save_plate({"plate_id": "A"})
        fresh_db.save_plate({"plate_id": "B"})
        assert len(fresh_db.search_plates()) == 2


class TestRunSteps:
    """Per-step execution timeline (run_steps).

    run_results alone recorded a run's start and its end and nothing between,
    so a run that crashed mid-way left no trace of where it got to.
    """

    def _seed_run(self, fresh_db, run_id="RUN-1"):
        fresh_db.save_run({
            "run_id": run_id, "workflow_name": "W",
            "started_at": "2026-09-10T10:00:00", "status": "running",
            "step_results": [],
        })
        return run_id

    def test_start_then_finish_updates_the_same_row(self, fresh_db):
        run_id = self._seed_run(fresh_db)
        fresh_db.save_run_step({
            "run_id": run_id, "step_number": 1, "attempt": 1,
            "instrument": "opentrons", "action": "ExecuteRecipe",
            "parameters": {"RecipeName": "x.json"},
            "status": "running", "started_at": "t1",
        })
        fresh_db.save_run_step({
            "run_id": run_id, "step_number": 1, "attempt": 1,
            "status": "success", "ended_at": "t2", "duration_seconds": 12.5,
            "result": {"RunId": "abc"}, "plate_id": "PLT-1",
        })
        steps = fresh_db.get_run_steps(run_id)
        assert len(steps) == 1
        assert steps[0]["status"] == "success"
        assert steps[0]["duration_seconds"] == 12.5

    def test_completion_does_not_blank_out_what_the_start_recorded(self, fresh_db):
        """The finishing call reports an outcome and legitimately omits the
        parameters; they must survive."""
        run_id = self._seed_run(fresh_db)
        fresh_db.save_run_step({
            "run_id": run_id, "step_number": 1, "attempt": 1,
            "instrument": "opentrons", "action": "ExecuteRecipe",
            "parameters": {"RecipeName": "x.json"}, "status": "running",
            "started_at": "t1",
        })
        fresh_db.save_run_step({
            "run_id": run_id, "step_number": 1, "attempt": 1, "status": "success",
        })
        step = fresh_db.get_run_steps(run_id)[0]
        assert step["parameters"] == {"RecipeName": "x.json"}
        assert step["instrument"] == "opentrons"
        assert step["action"] == "ExecuteRecipe"
        assert step["started_at"] == "t1"

    def test_a_retry_is_a_new_row_so_history_survives(self, fresh_db):
        run_id = self._seed_run(fresh_db)
        for attempt, status, err in ((1, "failed", "timeout"), (2, "success", None)):
            fresh_db.save_run_step({
                "run_id": run_id, "step_number": 3, "attempt": attempt,
                "instrument": "tecan", "action": "RunMeasurement",
                "status": status, "error": err,
            })
        steps = fresh_db.get_run_steps(run_id)
        assert [(s["attempt"], s["status"]) for s in steps] == [(1, "failed"), (2, "success")]
        assert steps[0]["error"] == "timeout"

    def test_steps_are_ordered_by_step_then_attempt(self, fresh_db):
        run_id = self._seed_run(fresh_db)
        for number, attempt in ((2, 1), (1, 2), (1, 1)):
            fresh_db.save_run_step({
                "run_id": run_id, "step_number": number, "attempt": attempt,
                "status": "success",
            })
        steps = fresh_db.get_run_steps(run_id)
        assert [(s["step_number"], s["attempt"]) for s in steps] == [(1, 1), (1, 2), (2, 1)]

    def test_get_run_carries_the_timeline(self, fresh_db):
        run_id = self._seed_run(fresh_db)
        fresh_db.save_run_step({
            "run_id": run_id, "step_number": 1, "attempt": 1, "status": "success",
        })
        assert len(fresh_db.get_run(run_id)["steps"]) == 1

    def test_update_run_status_patches_in_place(self, fresh_db):
        run_id = self._seed_run(fresh_db)
        fresh_db.update_run_status(run_id, "completed",
                                   completed_at="t9", steps_completed=4)
        run = fresh_db.get_run(run_id)
        assert run["status"] == "completed"
        assert run["completed_at"] == "t9"
        assert run["steps_completed"] == 4
        # the original workflow name is untouched
        assert run["workflow_name"] == "W"

    def test_update_run_status_ignores_unknown_columns(self, fresh_db):
        """A caller passing a stray key must not produce broken SQL."""
        run_id = self._seed_run(fresh_db)
        fresh_db.update_run_status(run_id, "failed", not_a_column="x")
        assert fresh_db.get_run(run_id)["status"] == "failed"

    def test_deleting_a_run_deletes_its_steps(self, fresh_db):
        run_id = self._seed_run(fresh_db)
        fresh_db.save_run_step({
            "run_id": run_id, "step_number": 1, "attempt": 1, "status": "success",
        })
        fresh_db.delete_run(run_id)
        assert fresh_db.get_run_steps(run_id) == []
