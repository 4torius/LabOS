"""Tecan raw XML parser — converts a Tecan i-control MeasurementResultData
XML file (Results/XML/*.xml, written directly by the instrument software)
into the SAME structured dict shape animl_parser.parse_animl() returns.

Why this exists instead of always going through AnIML: the raw XML is the
one artifact in this pipeline nothing of ours ever transforms before the
webapp reads it - TecanBridge.cs writes it straight from what the
instrument itself produced. Every AnIML-related bug found in this codebase
(Type="Scan" wells collapsing to 0, missing wavelength axis, ...) was a bug
in OUR OWN C# conversion step, never in this file. Parsing the same source
data directly, with one parser instead of two, removes that whole class of
bug at the ingestion path used for the chemical database - AnIML itself
stays available as an EXPORT format (real ASTM E1947 interop with other
instruments/institutions), just no longer a mandatory hop for our own
DB/visualization pipeline.

Deliberately mirrors TecanToAnIMLConverter.cs's own field mappings
(DetermineMeasurementType/GetSeriesName/GetUnitLabel) so a plate read
through this path and one read through the (still-supported, for AnIML
files already on disk) animl_parser path produce identical numbers.
"""
import logging
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MEASUREMENT_TYPE_UNIT = {
    "Absorbance": "OD",
    "Fluorescence": "RFU",
    "Luminescence": "RLU",
}
_MEASUREMENT_TYPE_SERIES_NAME = {
    "Absorbance": "Optical Density",
    "Fluorescence": "Relative Fluorescence Units",
    "Luminescence": "Relative Light Units",
}


def _determine_measurement_type(mode: str) -> str:
    """Mirrors TecanToAnIMLConverter.cs::DetermineMeasurementType exactly."""
    mode_lower = (mode or "").lower()
    if "absorbance" in mode_lower or "abs" in mode_lower:
        return "Absorbance"
    if "fluorescence" in mode_lower or "flu" in mode_lower:
        return "Fluorescence"
    if "luminescence" in mode_lower or "lum" in mode_lower:
        return "Luminescence"
    return "Unknown"


def _parse_reading(text: Optional[str]) -> Optional[float]:
    """A Tecan reading is either a plain float or the literal "OVER" (detector
    saturation, most common at the low-wavelength end of a UV scan) - OVER
    becomes NaN (a real, meaningful reading: "off scale", never silently 0),
    matching TecanToAnIMLConverter.cs's own OVER handling exactly."""
    if text is None:
        return None
    text = text.strip()
    if text == "OVER":
        return float("nan")
    try:
        return float(text)
    except ValueError:
        return None


def _extract_param(parameters: Dict[str, str], names: tuple) -> Optional[float]:
    for name in names:
        val = parameters.get(name)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return None


def parse_tecan_xml(path: Path) -> Dict[str, Any]:
    """Parse a raw Tecan XML file into the same shape animl_parser.parse_animl()
    returns - see that function's docstring for the field-by-field contract;
    every field it produces is also produced here, with identical semantics."""
    try:
        tree = ET.parse(str(path))
    except ET.ParseError as exc:
        logger.warning("Tecan XML file %s is malformed: %s", path.name, exc)
        return {"filename": path.name, "plate_name": "", "plate_type": "", "experiment_steps": [], "parse_error": str(exc)}
    root = tree.getroot()

    result: Dict[str, Any] = {
        "filename": path.name,
        "plate_name": "",
        "plate_type": "",
        "experiment_steps": [],
    }
    if root.tag != "MeasurementResultData":
        result["parse_error"] = f"Unexpected root element: {root.tag}"
        return result

    header = root.find("Header")
    header_params: Dict[str, str] = {}
    if header is not None:
        params_el = header.find("Parameters")
        if params_el is not None:
            for p in params_el.findall("Parameter"):
                name = p.get("Name")
                if name:
                    header_params[name] = p.get("Value", "")
    plate_info = header_params.get("Plate", "")
    result["plate_name"] = plate_info
    result["plate_type"] = plate_info

    for section in root.findall("Section"):
        step = _parse_section(section)
        if step:
            result["experiment_steps"].append(step)

    return result


def _parse_section(section) -> Optional[Dict[str, Any]]:
    step_name = section.get("Name", "")

    parameters: Dict[str, str] = {}
    params_el = section.find("Parameters")
    if params_el is not None:
        for p in params_el.findall("Parameter"):
            name = p.get("Name")
            if name:
                parameters[name] = p.get("Value", "")

    mode = parameters.get("Mode", "")
    measurement_type = _determine_measurement_type(mode)
    technique = measurement_type  # animl_parser derives `technique` from the AnIML Technique/@name, which the C# converter itself sets to measurement_type
    unit_label = _MEASUREMENT_TYPE_UNIT.get(measurement_type, "AU")
    value_series_name = _MEASUREMENT_TYPE_SERIES_NAME.get(measurement_type, "Measurement Value")

    data_el = section.find("Data")
    wells = data_el.findall("Well") if data_el is not None else []

    measurements: List[Dict[str, Any]] = []
    float_values: List[float] = []
    is_scan = False

    for well in wells:
        pos = well.get("Pos", "Unknown")
        well_type = well.get("Type", "Single")

        row_letter = pos[0].upper() if pos else "A"
        col_str = pos[1:] if len(pos) > 1 else "1"
        try:
            col = int(col_str)
        except ValueError:
            col = 1
        if "A" <= row_letter <= "Z":
            row = ord(row_letter) - ord("A") + 1
        else:
            logger.warning("Well '%s': unrecognized row letter %r, defaulting to row 1", pos, row_letter)
            row = 1
        if col < 1:
            logger.warning("Well '%s': non-positive column %d, defaulting to column 1", pos, col)
            col = 1

        entry: Dict[str, Any] = {"well": pos, "row": row, "col": col, "unit": unit_label}

        if well_type == "Scan":
            is_scan = True
            spectrum = []
            for scan_el in well.findall("Scan"):
                wl_attr = scan_el.get("WL")
                try:
                    wl = int(wl_attr)
                except (TypeError, ValueError):
                    continue
                spectrum.append({"wl": wl, "value": _parse_reading(scan_el.text)})
            entry["spectrum"] = spectrum
            finite = [p["value"] for p in spectrum if p["value"] is not None and not math.isnan(p["value"])]
            value = sum(finite) / len(finite) if finite else None
        else:
            single_el = well.find("Single")
            value = _parse_reading(single_el.text if single_el is not None else None)

        entry["value"] = value
        measurements.append(entry)
        if value is not None:
            float_values.append(value)

    if is_scan:
        value_series_name += " (scan mean)"

    valid = [v for v in float_values if not math.isnan(v)]
    statistics: Dict[str, Any] = {}
    if valid:
        mean = sum(valid) / len(valid)
        statistics = {
            "count": len(valid),
            "mean": round(mean, 6),
            "min": round(min(valid), 6),
            "max": round(max(valid), 6),
            "std": round((sum((v - mean) ** 2 for v in valid) / len(valid)) ** 0.5, 6),
        }

    wavelength_nm = _extract_param(parameters, (
        "Wavelength", "Wavelength [nm]", "Detection Wavelength",
        "Absorbance Wavelength", "Measurement Wavelength",
    ))
    excitation_nm = _extract_param(parameters, (
        "Excitation Wavelength", "Excitation", "Ex Wavelength", "ExcitationWavelength",
    ))
    emission_nm = _extract_param(parameters, (
        "Emission Wavelength", "Emission", "Em Wavelength", "EmissionWavelength",
    ))
    if measurement_type == "Fluorescence" and not wavelength_nm:
        wavelength_nm = excitation_nm
    wavelength_start_nm = _extract_param(parameters, ("Wavelength Start",))
    wavelength_end_nm = _extract_param(parameters, ("Wavelength End",))
    wavelength_step_nm = _extract_param(parameters, ("Wavelength Step Size",))

    return {
        "step_name": step_name,
        "technique": technique,
        "measurement_type": measurement_type,
        "wavelength_nm": wavelength_nm,
        "excitation_nm": excitation_nm,
        "emission_nm": emission_nm,
        "wavelength_start_nm": wavelength_start_nm,
        "wavelength_end_nm": wavelength_end_nm,
        "wavelength_step_nm": wavelength_step_nm,
        "is_scan": is_scan,
        "parameters": parameters,
        "measurements": measurements,
        "value_series": value_series_name,
        "unit": unit_label,
        "statistics": statistics,
    }
