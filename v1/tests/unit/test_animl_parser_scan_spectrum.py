"""animl_parser.py must surface the full per-well spectrum for multi-wavelength
scan steps (a <Result id="spectra_..."> sibling of the SeriesSet, written by
TecanToAnIMLConverter.cs for Type="Scan" wells) instead of only the scalar
scan-mean each well already gets in the main measurement series.
"""
import xml.etree.ElementTree as ET

from src.animl_parser import _NS, _parse_step


def _make_scan_step_el(well_positions, means, spectra_by_well):
    """Build an ExperimentStep with the usual wellpos/values SeriesSet plus a
    spectra_ Result carrying one SampleReference per scanned well."""
    ns = _NS
    wells_xml = "".join(f"<S xmlns='{ns}'>{w}</S>" for w in well_positions)
    vals_xml = "".join(f"<F xmlns='{ns}'>{v}</F>" for v in means)

    sample_refs_xml = ""
    for well, points in spectra_by_well.items():
        points_xml = "".join(f"<F xmlns='{ns}' wl='{wl}'>{v}</F>" for wl, v in points)
        sample_refs_xml += f"<SampleReference xmlns='{ns}' sampleID='{well}'>{points_xml}</SampleReference>"

    xml_str = f"""
    <ExperimentStep xmlns="{ns}" name="Label1">
      <Technique name="Absorbance"/>
      <Method name="Absorbance Measurement">
        <Category name="Measurement Settings">
          <Parameter name="Wavelength Start"><S>230</S></Parameter>
          <Parameter name="Wavelength End"><S>400</S></Parameter>
          <Parameter name="Wavelength Step Size"><S>1</S></Parameter>
        </Category>
      </Method>
      <ResultSet>
        <SeriesSet>
          <Series dependency="independent" name="wells">
            <EncodedValueSet><IndividualValueSet>{wells_xml}</IndividualValueSet></EncodedValueSet>
          </Series>
          <Series dependency="dependent" name="Optical Density (scan mean)">
            <Unit label="OD"/>
            <EncodedValueSet><IndividualValueSet>{vals_xml}</IndividualValueSet></EncodedValueSet>
          </Series>
        </SeriesSet>
        <Result id="spectra_Label1" name="Wavelength Scan Data"
                wavelengthStartNm="230" wavelengthEndNm="400" wavelengthStepNm="1">
          {sample_refs_xml}
        </Result>
      </ResultSet>
    </ExperimentStep>
    """
    return ET.fromstring(xml_str)


class TestScanSpectrumParsing:
    def test_scanned_well_gets_its_full_spectrum_not_just_the_mean(self):
        step_el = _make_scan_step_el(
            ["A1"], [2.5],
            {"A1": [(230, 3.6167), (231, 3.3238), (232, 3.052)]},
        )
        data = _parse_step(step_el)
        assert data["is_scan"] is True
        a1 = data["measurements"][0]
        assert a1["value"] == 2.5  # scalar mean still present, unchanged
        assert a1["spectrum"] == [
            {"wl": 230, "value": 3.6167},
            {"wl": 231, "value": 3.3238},
            {"wl": 232, "value": 3.052},
        ]

    def test_wavelength_axis_extracted_from_scan_specific_parameter_names(self):
        step_el = _make_scan_step_el(["A1"], [1.0], {"A1": [(230, 1.0)]})
        data = _parse_step(step_el)
        assert data["wavelength_start_nm"] == 230.0
        assert data["wavelength_end_nm"] == 400.0
        assert data["wavelength_step_nm"] == 1.0

    def test_well_without_a_matching_sample_reference_has_no_spectrum_key(self):
        # A2 is in the main series but never scanned (no SampleReference for it) -
        # must not crash and must not fabricate a spectrum out of thin air.
        step_el = _make_scan_step_el(
            ["A1", "A2"], [2.5, 0.1],
            {"A1": [(230, 3.6167)]},
        )
        data = _parse_step(step_el)
        a2 = next(m for m in data["measurements"] if m["well"] == "A2")
        assert "spectrum" not in a2

    def test_non_scan_step_has_is_scan_false_and_no_spectrum_keys(self):
        ns = _NS
        xml_str = f"""
        <ExperimentStep xmlns="{ns}" name="Step1">
          <Technique name="Absorbance"/>
          <ResultSet>
            <SeriesSet>
              <Series dependency="independent" name="wells">
                <EncodedValueSet><IndividualValueSet><S>A1</S></IndividualValueSet></EncodedValueSet>
              </Series>
              <Series dependency="dependent" name="values">
                <Unit label="AU"/>
                <EncodedValueSet><IndividualValueSet><F>1.0</F></IndividualValueSet></EncodedValueSet>
              </Series>
            </SeriesSet>
          </ResultSet>
        </ExperimentStep>
        """
        data = _parse_step(ET.fromstring(xml_str))
        assert data["is_scan"] is False
        assert "spectrum" not in data["measurements"][0]
