"""Edge-case tests for src/animl_parser.py — malformed XML and out-of-range well IDs."""
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from src.animl_parser import _NS, _parse_step, parse_animl


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "sample.animl"
    path.write_text(content, encoding="utf-8")
    return path


class TestMalformedXml:
    def test_malformed_xml_returns_structured_error_not_raise(self, tmp_path):
        path = _write(tmp_path, "<not-valid-xml")
        result = parse_animl(path)
        assert result["parse_error"]
        assert result["experiment_steps"] == []
        assert result["filename"] == "sample.animl"

    def test_empty_file_returns_structured_error(self, tmp_path):
        path = _write(tmp_path, "")
        result = parse_animl(path)
        assert result["parse_error"]

    def test_well_formed_but_empty_root_does_not_raise(self, tmp_path):
        path = _write(tmp_path, f'<AnIML xmlns="{_NS}"/>')
        result = parse_animl(path)
        assert "parse_error" not in result
        assert result["experiment_steps"] == []


def _make_step_el(well_positions, values=None):
    """Build a minimal ExperimentStep XML element with one independent Series
    of well positions and one dependent Series of float values."""
    values = values if values is not None else [1.0] * len(well_positions)
    ns = _NS
    wells_xml = "".join(f"<S xmlns='{ns}'>{w}</S>" for w in well_positions)
    vals_xml = "".join(f"<F xmlns='{ns}'>{v}</F>" for v in values)
    xml_str = f"""
    <ExperimentStep xmlns="{ns}" name="Step1">
      <Technique name="Absorbance"/>
      <ResultSet>
        <SeriesSet>
          <Series dependency="independent" name="wells">
            <EncodedValueSet><IndividualValueSet>{wells_xml}</IndividualValueSet></EncodedValueSet>
          </Series>
          <Series dependency="dependent" name="values">
            <Unit label="AU"/>
            <EncodedValueSet><IndividualValueSet>{vals_xml}</IndividualValueSet></EncodedValueSet>
          </Series>
        </SeriesSet>
      </ResultSet>
    </ExperimentStep>
    """
    return ET.fromstring(xml_str)


class TestWellIdBounds:
    def test_normal_well_parses_correctly(self):
        step_el = _make_step_el(["A1", "B12"])
        data = _parse_step(step_el)
        assert data is not None
        assert data["measurements"][0] == {"well": "A1", "row": 1, "col": 1, "value": 1.0, "unit": "AU"}
        assert data["measurements"][1]["row"] == 2
        assert data["measurements"][1]["col"] == 12

    def test_lowercase_row_letter_is_normalized(self):
        step_el = _make_step_el(["a1"])
        data = _parse_step(step_el)
        assert data is not None
        assert data["measurements"][0]["row"] == 1

    def test_row_letter_outside_a_z_defaults_to_row_one_without_raising(self):
        step_el = _make_step_el(["1A"])  # well[0] == '1', not a letter
        data = _parse_step(step_el)  # must not raise
        assert data is not None
        assert data["measurements"][0]["row"] == 1

    def test_non_positive_column_defaults_to_one_without_raising(self):
        step_el = _make_step_el(["A0"])
        data = _parse_step(step_el)
        assert data is not None
        assert data["measurements"][0]["col"] == 1

    def test_non_numeric_column_defaults_to_one(self):
        step_el = _make_step_el(["AX"])
        data = _parse_step(step_el)
        assert data is not None
        assert data["measurements"][0]["col"] == 1

    def test_empty_well_string_does_not_raise(self):
        step_el = _make_step_el([""])
        data = _parse_step(step_el)
        assert data is not None
        assert data["measurements"][0]["row"] == 1
        assert data["measurements"][0]["col"] == 1
