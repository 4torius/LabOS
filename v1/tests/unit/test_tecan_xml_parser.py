"""tecan_xml_parser.py: parses the raw Tecan i-control XML directly - must
produce the same shape (and, for a real file, the same numbers) as
animl_parser.parse_animl(), since plate_tracking.py now prefers this path
over AnIML for ingestion (see _resolve_source_path).
"""
import math
from pathlib import Path

from src.tecan_xml_parser import parse_tecan_xml


def _write(tmp_path: Path, content: str, name: str = "sample.xml") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _scan_xml(wells):
    """wells: dict well_pos -> list of (wl, text) pairs."""
    wells_xml = ""
    for pos, points in wells.items():
        scans = "".join(f'<Scan WL="{wl}" LabelId="1">{text}</Scan>' for wl, text in points)
        wells_xml += f'<Well Pos="{pos}" Type="Scan">{scans}</Well>'
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<MeasurementResultData Date="2026-01-01T00:00:00Z">
    <Header Application="Tecan i-control">
        <Instrument><Name>infinite 200Pro</Name><SerialNumber>123</SerialNumber></Instrument>
        <Parameters><Parameter Name="Plate" Value="Test Plate [x.pdfx]" /></Parameters>
    </Header>
    <Section Name="Label1" Time_Start="2026-01-01T00:00:00Z" Time_End="2026-01-01T00:01:00Z">
        <Parameters>
            <Parameter Name="Mode" Value="Absorbance" />
            <Parameter Name="Wavelength Start" Value="230" Unit="nm" />
            <Parameter Name="Wavelength End" Value="232" Unit="nm" />
            <Parameter Name="Wavelength Step Size" Value="1" Unit="nm" />
        </Parameters>
        <Data Cycle="1">{wells_xml}</Data>
    </Section>
</MeasurementResultData>
"""


def _single_xml(wells):
    """wells: dict well_pos -> reading text."""
    wells_xml = "".join(f'<Well Pos="{pos}" Type="Single"><Single>{text}</Single></Well>' for pos, text in wells.items())
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<MeasurementResultData Date="2026-01-01T00:00:00Z">
    <Header Application="Tecan i-control">
        <Instrument><Name>infinite 200Pro</Name><SerialNumber>123</SerialNumber></Instrument>
        <Parameters><Parameter Name="Plate" Value="Test Plate" /></Parameters>
    </Header>
    <Section Name="Label1" Time_Start="2026-01-01T00:00:00Z" Time_End="2026-01-01T00:01:00Z">
        <Parameters><Parameter Name="Mode" Value="Absorbance" /></Parameters>
        <Data Cycle="1">{wells_xml}</Data>
    </Section>
</MeasurementResultData>
"""


class TestMalformedXml:
    def test_malformed_xml_returns_structured_error_not_raise(self, tmp_path):
        path = _write(tmp_path, "<not-valid-xml")
        result = parse_tecan_xml(path)
        assert result["parse_error"]
        assert result["experiment_steps"] == []

    def test_wrong_root_element_returns_structured_error(self, tmp_path):
        path = _write(tmp_path, "<NotTecanData/>")
        result = parse_tecan_xml(path)
        assert result["parse_error"]


class TestScanWells:
    def test_full_spectrum_and_mean_extracted(self, tmp_path):
        path = _write(tmp_path, _scan_xml({"A1": [(230, "1.0"), (231, "2.0"), (232, "3.0")]}))
        result = parse_tecan_xml(path)
        step = result["experiment_steps"][0]
        assert step["is_scan"] is True
        a1 = step["measurements"][0]
        assert a1["well"] == "A1" and a1["row"] == 1 and a1["col"] == 1
        assert a1["value"] == 2.0  # mean of 1,2,3
        assert a1["spectrum"] == [
            {"wl": 230, "value": 1.0}, {"wl": 231, "value": 2.0}, {"wl": 232, "value": 3.0},
        ]

    def test_over_becomes_nan_not_zero(self, tmp_path):
        path = _write(tmp_path, _scan_xml({"A1": [(230, "OVER"), (231, "2.0")]}))
        result = parse_tecan_xml(path)
        a1 = result["experiment_steps"][0]["measurements"][0]
        assert math.isnan(a1["spectrum"][0]["value"])
        assert a1["spectrum"][1]["value"] == 2.0
        assert a1["value"] == 2.0  # NaN excluded from the mean

    def test_all_over_gives_no_value_not_zero(self, tmp_path):
        path = _write(tmp_path, _scan_xml({"A1": [(230, "OVER"), (231, "OVER")]}))
        result = parse_tecan_xml(path)
        a1 = result["experiment_steps"][0]["measurements"][0]
        assert a1["value"] is None

    def test_wavelength_axis_and_series_name(self, tmp_path):
        path = _write(tmp_path, _scan_xml({"A1": [(230, "1.0")]}))
        result = parse_tecan_xml(path)
        step = result["experiment_steps"][0]
        assert step["wavelength_start_nm"] == 230.0
        assert step["wavelength_end_nm"] == 232.0
        assert step["wavelength_step_nm"] == 1.0
        assert step["value_series"] == "Optical Density (scan mean)"
        assert step["unit"] == "OD"


class TestSingleWells:
    def test_plain_reading(self, tmp_path):
        path = _write(tmp_path, _single_xml({"A1": "0.234", "A2": "1.5"}))
        result = parse_tecan_xml(path)
        step = result["experiment_steps"][0]
        assert step["is_scan"] is False
        values = {m["well"]: m["value"] for m in step["measurements"]}
        assert values == {"A1": 0.234, "A2": 1.5}
        assert "spectrum" not in step["measurements"][0]
        assert step["value_series"] == "Optical Density"  # no "(scan mean)" suffix

    def test_over_single_reading_becomes_nan(self, tmp_path):
        path = _write(tmp_path, _single_xml({"A1": "OVER"}))
        result = parse_tecan_xml(path)
        val = result["experiment_steps"][0]["measurements"][0]["value"]
        assert math.isnan(val)


class TestPlateAndStatistics:
    def test_plate_name_and_type_from_header_parameter(self, tmp_path):
        path = _write(tmp_path, _single_xml({"A1": "1.0"}))
        result = parse_tecan_xml(path)
        assert result["plate_name"] == "Test Plate"
        assert result["plate_type"] == "Test Plate"

    def test_statistics_exclude_nan(self, tmp_path):
        path = _write(tmp_path, _single_xml({"A1": "1.0", "A2": "3.0", "A3": "OVER"}))
        result = parse_tecan_xml(path)
        stats = result["experiment_steps"][0]["statistics"]
        assert stats["count"] == 2
        assert stats["mean"] == 2.0
        assert stats["min"] == 1.0
        assert stats["max"] == 3.0
