"""Pickers with one option per Library JSON file that has a given field.

The plate reader's PlateType used to be offered from its own table,
Library/Analysis/PlateTypes.json, with names that matched no plate the
Opentrons knew. It is now the plates in Library/Labware/Plates that carry a
`tecan_plate_name`, declared by the Tecan server as `json_files` +
`require_field`. Also covers describe_options(), which reports what a refresh
found (RefreshTasks only answers "success").
"""
import json

import pytest

from src.lab_core import LabCore

DECLARED = {
    "json_files": "Library/Labware/Plates/*.plate.json",
    "require_field": "tecan_plate_name",
    "value_field": "id",
    "label_field": "display_name",
}


def _plate(folder, name, **data):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.plate.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def core(tmp_path):
    plates = tmp_path / "Library" / "Labware" / "Plates"
    _plate(plates, "a", id="corning_24", display_name="Corning 24", tecan_plate_name="COS24ft")
    _plate(plates, "b", id="ot_only", display_name="Opentrons only")
    _plate(plates, "c", id="greiner_96", display_name="", tecan_plate_name="GRE96ft")
    _plate(plates, "d", id="corning_24", display_name="Duplicate", tecan_plate_name="X")
    (plates / "scratch.plate_CUSTOM.json").write_text(
        json.dumps({"id": "scratch", "tecan_plate_name": "Y"}), encoding="utf-8")
    c = LabCore(base_dir=tmp_path)
    c._ui_dropdowns["reader_plate_type"] = dict(DECLARED)
    return c


def test_only_plates_with_the_required_field_are_offered(core):
    assert core._get_options_for_hint("reader_plate_type") == ["corning_24", "greiner_96"]


def test_labels_come_from_the_label_field_or_fall_back_to_the_value(core):
    assert core._option_pairs_for_hint("reader_plate_type") == [
        {"value": "corning_24", "label": "Corning 24"},
        {"value": "greiner_96", "label": "greiner_96"},
    ]


def test_files_outside_library_are_not_read(core, tmp_path):
    _plate(tmp_path / "Elsewhere", "e", id="outside", tecan_plate_name="Z")
    core._ui_dropdowns["reader_plate_type"]["json_files"] = "Elsewhere/*.plate.json"
    assert core._get_options_for_hint("reader_plate_type") == []


def test_a_server_catalog_entry_becomes_the_dropdown(tmp_path):
    from tests.unit.test_catalog import _server

    core = LabCore(base_dir=tmp_path)
    core._server_to_instrument(_server("Reader", {"library_types": [
        {"hint": "reader_plate_type", "parameter_names": ["PlateType"], **DECLARED}]}), {}, {})
    assert core._ui_dropdowns["reader_plate_type"] == DECLARED


def test_the_shipped_tecan_catalog_offers_the_library_plates():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    catalog = json.loads((root / "SiLA2/TecanM200SiLA2Server/library_catalog.json")
                         .read_text(encoding="utf-8"))
    entry = next(e for e in catalog["library_types"] if e["hint"] == "reader_plate_type")
    core = LabCore(base_dir=root)
    core._ui_dropdowns["reader_plate_type"] = {k: entry[k] for k in DECLARED}
    offered = core._get_options_for_hint("reader_plate_type")
    assert "corning_24_wellplate_3.4ml_flat" in offered
    assert "greiner_96_wellplate_382ul" in offered


def test_describe_options_reports_count_names_and_where_they_came_from(core):
    core._dynamic_options_cache["mobile_task"] = [{"id": "x1", "name": "go_home"},
                                                  {"id": "x2", "name": ""}]
    core._dynamic_options_from_server["mobile_task"] = True
    assert core.describe_options(["mobile_task", "never_read"]) == [
        {"hint": "mobile_task", "count": 2, "names": ["go_home", "x2"], "from_server": True},
        {"hint": "never_read", "count": 0, "names": [], "from_server": False},
    ]
