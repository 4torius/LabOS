"""AnIML v0.90 parser — converts Tecan .animl files to structured dicts for the web UI."""
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_NS = 'urn:org:astm:animl:schema:core:draft:0.90'


def _t(name: str) -> str:
    return f'{{{_NS}}}{name}'


def parse_animl(path: Path) -> Dict[str, Any]:
    """Parse an AnIML file and return a structured dict ready for JSON serialization."""
    try:
        tree = ET.parse(str(path))
    except ET.ParseError as exc:
        logger.warning("AnIML file %s is malformed: %s", path.name, exc)
        return {"filename": path.name, "plate_name": "", "plate_type": "", "experiment_steps": [], "parse_error": str(exc)}
    root = tree.getroot()

    result: Dict[str, Any] = {
        'filename': path.name,
        'plate_name': '',
        'plate_type': '',
        'experiment_steps': [],
    }

    sample_set = root.find(_t('SampleSet'))
    if sample_set is not None:
        result['plate_name'] = sample_set.get('name', '')
        container = sample_set.find(_t('Container'))
        if container is not None:
            result['plate_name'] = container.get('name', result['plate_name'])
            param = container.find(_t('Parameter'))
            if param is not None:
                s_el = param.find(_t('S'))
                if s_el is not None and s_el.text:
                    result['plate_type'] = s_el.text.strip()

    exp_step_set = root.find(_t('ExperimentStepSet'))
    if exp_step_set is not None:
        for step_el in exp_step_set.findall(_t('ExperimentStep')):
            step_data = _parse_step(step_el)
            if step_data:
                result['experiment_steps'].append(step_data)

    return result


def _parse_step(step_el) -> Optional[Dict[str, Any]]:
    step_name = step_el.get('name', '')

    technique_el = step_el.find(_t('Technique'))
    technique = technique_el.get('name', '') if technique_el is not None else ''

    parameters: Dict[str, Any] = {}
    method_el = step_el.find(_t('Method'))
    if method_el is not None:
        for cat in method_el.findall(_t('Category')):
            for param in cat.findall(_t('Parameter')):
                pname = param.get('name', '')
                val = _read_scalar(param)
                if val is not None:
                    parameters[pname] = val

    result_set = step_el.find(_t('ResultSet'))
    if result_set is None:
        return None
    series_set = result_set.find(_t('SeriesSet'))
    if series_set is None:
        return None

    well_positions: List[str] = []
    float_values: List[Optional[float]] = []
    unit_label = ''
    value_series_name = ''

    # Full per-well spectrum for multi-wavelength scans (Type="Scan" wells) -
    # a separate <Result id="spectra_..."> sibling of the SeriesSet above,
    # written only when at least one well in the section actually scanned.
    # The main well_positions/float_values series still gets one scalar per
    # well either way (the scan's mean, see TecanToAnIMLConverter.cs) so
    # every existing consumer of those two lists keeps working unchanged;
    # this only adds the data that was previously discarded.
    spectra_by_well: Dict[str, List[Dict[str, Any]]] = {}
    for res_el in result_set.findall(_t('Result')):
        if not (res_el.get('id') or '').startswith('spectra_'):
            continue
        for sample_ref in res_el.findall(_t('SampleReference')):
            well_id = sample_ref.get('sampleID', '')
            points: List[Dict[str, Any]] = []
            for f_el in sample_ref.findall(_t('F')):
                try:
                    wl = int(f_el.get('wl'))
                except (TypeError, ValueError):
                    continue
                try:
                    v = float(f_el.text)
                except (TypeError, ValueError):
                    v = None
                points.append({'wl': wl, 'value': v})
            if points:
                spectra_by_well[well_id] = points
        break

    for series in series_set.findall(_t('Series')):
        dependency = series.get('dependency', '')
        sname = series.get('name', '')
        encoded = series.find(_t('EncodedValueSet'))
        if encoded is None:
            continue
        individual = encoded.find(_t('IndividualValueSet'))
        if individual is None:
            continue

        if dependency == 'independent':
            for s_el in individual.findall(_t('S')):
                well_positions.append((s_el.text or '').strip())
        elif dependency == 'dependent':
            value_series_name = sname
            unit_el = series.find(_t('Unit'))
            if unit_el is not None:
                unit_label = unit_el.get('label', '')
            for f_el in individual.findall(_t('F')):
                try:
                    float_values.append(float(f_el.text))
                except (TypeError, ValueError):
                    float_values.append(None)

    measurements: List[Dict[str, Any]] = []
    for i, well in enumerate(well_positions):
        val = float_values[i] if i < len(float_values) else None
        row_letter = well[0].upper() if well else 'A'
        col_str = well[1:] if len(well) > 1 else '1'
        try:
            col = int(col_str)
        except ValueError:
            col = 1

        # A row letter outside A-Z (or a non-positive column) would otherwise
        # compute a silently wrong row/col via ord() arithmetic — e.g. a
        # digit or symbol as row_letter yields a negative row number instead
        # of an error, corrupting the plate display without any signal.
        if 'A' <= row_letter <= 'Z':
            row = ord(row_letter) - ord('A') + 1
        else:
            logger.warning("Well '%s': unrecognized row letter %r, defaulting to row 1", well, row_letter)
            row = 1
        if col < 1:
            logger.warning("Well '%s': non-positive column %d, defaulting to column 1", well, col)
            col = 1

        entry: Dict[str, Any] = {'well': well, 'row': row, 'col': col, 'value': val, 'unit': unit_label}
        if well in spectra_by_well:
            entry['spectrum'] = spectra_by_well[well]
        measurements.append(entry)

    valid = [v for v in float_values if v is not None]
    statistics: Dict[str, Any] = {}
    if valid:
        mean = sum(valid) / len(valid)
        statistics = {
            'count': len(valid),
            'mean': round(mean, 6),
            'min': round(min(valid), 6),
            'max': round(max(valid), 6),
            'std': round((sum((v - mean) ** 2 for v in valid) / len(valid)) ** 0.5, 6),
        }

    # Infer measurement type from technique name
    tech_lower = technique.lower()
    if 'fluorescence' in tech_lower or 'fluo' in tech_lower:
        measurement_type = 'Fluorescence'
    elif 'luminescence' in tech_lower or 'lumin' in tech_lower:
        measurement_type = 'Luminescence'
    elif 'absorbance' in tech_lower or 'abs' in tech_lower:
        measurement_type = 'Absorbance'
    else:
        measurement_type = 'Unknown'

    # Extract wavelength(s) from parameter dict using common Tecan parameter names
    wavelength_nm = _extract_param(parameters, (
        'Wavelength', 'wavelength', 'Wavelength [nm]', 'Detection Wavelength',
        'Absorbance Wavelength', 'Measurement Wavelength',
    ))
    excitation_nm = _extract_param(parameters, (
        'Excitation Wavelength', 'Excitation', 'Ex Wavelength', 'ExcitationWavelength',
    ))
    emission_nm = _extract_param(parameters, (
        'Emission Wavelength', 'Emission', 'Em Wavelength', 'EmissionWavelength',
    ))
    # For absorbance, wavelength_nm is the single wavelength
    # For fluorescence, prefer excitation/emission pair; fall back to the first found
    if measurement_type == 'Fluorescence' and not wavelength_nm:
        wavelength_nm = excitation_nm  # convenience alias

    # Scan steps (multi-wavelength) describe their axis via these three
    # parameters instead of a single 'Wavelength' - not covered by the
    # single-value lookup above, so extracted separately here.
    wavelength_start_nm = _extract_param(parameters, ('Wavelength Start',))
    wavelength_end_nm = _extract_param(parameters, ('Wavelength End',))
    wavelength_step_nm = _extract_param(parameters, ('Wavelength Step Size',))

    return {
        'step_name': step_name,
        'technique': technique,
        'measurement_type': measurement_type,
        'wavelength_nm': wavelength_nm,
        'excitation_nm': excitation_nm,
        'emission_nm': emission_nm,
        'wavelength_start_nm': wavelength_start_nm,
        'wavelength_end_nm': wavelength_end_nm,
        'wavelength_step_nm': wavelength_step_nm,
        'is_scan': bool(spectra_by_well),
        'parameters': parameters,
        'measurements': measurements,
        'value_series': value_series_name,
        'unit': unit_label,
        'statistics': statistics,
    }


def _extract_param(parameters: Dict[str, Any], names: tuple) -> Optional[float]:
    """Return the first numeric value found among candidate parameter names."""
    for name in names:
        val = parameters.get(name)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return None


def _read_scalar(param_el) -> Optional[Any]:
    for tag, cast in [('F', float), ('I', int), ('L', int), ('S', str)]:
        el = param_el.find(_t(tag))
        if el is not None and el.text:
            try:
                return cast(el.text.strip())
            except (ValueError, TypeError):
                return el.text.strip()
    return None
