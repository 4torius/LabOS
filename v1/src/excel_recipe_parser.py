"""
Excel Recipe Parser for Chemical Pipetting Plans

Flexible parser that:
1. Automatically detects reagent columns from Excel headers
2. Allows user configuration of column grouping and delays
3. Generates recipes with embedded Opentrons Delay commands

No hardcoded reagent names - everything is read from Excel.
"""

import io
import json
import re
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path

# Matches a volume unit in parentheses, e.g. "(uL)", "(µL)", "(mL)" - any position in the header.
# Group 1 is the unit letter: 'u'/'µ' -> microliter, 'm' -> milliliter (converted to uL downstream).
_VOLUME_UNIT_RE = re.compile(r'\(\s*([uµm])l\s*\)', re.IGNORECASE)

# Plate catalog helpers

_PLATE_CACHE: Dict[str, Dict] = {}

def _load_plate_catalog(library_dir: Optional[Path] = None) -> Dict[str, Dict]:
    """Load all plate definitions from Library/Labware/Plates/. Cached after first call."""
    global _PLATE_CACHE
    if _PLATE_CACHE:
        return _PLATE_CACHE
    if library_dir is None:
        library_dir = Path(__file__).parent.parent / "Library"
    plates_dir = library_dir / "Labware" / "Plates"
    if not plates_dir.exists():
        return {}
    for f in plates_dir.glob("*.plate.json"):
        try:
            data = json.loads(f.read_text(encoding='utf-8-sig'))
            _PLATE_CACHE[data["id"]] = data
        except Exception:
            pass
    return _PLATE_CACHE


def get_plate_info(plate_id: str, library_dir: Optional[Path] = None) -> Optional[Dict]:
    """Return plate definition dict for the given Opentrons load name, or None."""
    return _load_plate_catalog(library_dir).get(plate_id)


_RESERVOIR_CACHE: Dict[str, Dict] = {}


def _load_reservoir_catalog(library_dir: Optional[Path] = None) -> Dict[str, Dict]:
    """Load all reservoir definitions from Library/Labware/Reservoirs/ (full Opentrons
    v2 labware schema, unlike the compact Plates schema). Cached after first call."""
    global _RESERVOIR_CACHE
    if _RESERVOIR_CACHE:
        return _RESERVOIR_CACHE
    if library_dir is None:
        library_dir = Path(__file__).parent.parent / "Library"
    reservoirs_dir = library_dir / "Labware" / "Reservoirs"
    if not reservoirs_dir.exists():
        return {}
    for f in reservoirs_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding='utf-8-sig'))
            # Keyed by filename (f.stem), matching webapp/routes/hardware.py's
            # catalog/labware endpoint - that's what actually ends up as
            # `reservoir_type` (HAL LoadName / Configuration.reservoir_type)
            # via the UI dropdown. Falling back to the internal
            # parameters.loadName here would silently break lookups the
            # moment a file gets renamed without its internal field updated.
            _RESERVOIR_CACHE[f.stem] = data
        except Exception:
            pass
    return _RESERVOIR_CACHE


def get_reservoir_wells(reservoir_type: str, library_dir: Optional[Path] = None) -> List[str]:
    """Ordered list of well IDs for a reservoir labware (flattened from its `ordering`
    column layout). Falls back to a permissive single-well guess if unknown."""
    data = _load_reservoir_catalog(library_dir).get(reservoir_type)
    if not data:
        return ["A1"]
    well_ids: List[str] = []
    for column in data.get("ordering", []):
        well_ids.extend(column)
    return well_ids or ["A1"]


def get_reservoir_well_capacity(reservoir_type: str, well_id: str, library_dir: Optional[Path] = None) -> Optional[float]:
    """Planning ceiling (uL) for a single well in a reservoir labware - the
    volume the loading-plan math (_claim_wells_for_volume) should fill it
    to, not necessarily its true physical brim.

    Prefers an explicit `workingVolume` on the well if the labware file
    defines one - a deliberate safety margin below `totalLiquidVolume`
    reserving headroom for real per-well dead volume (liquid at the very
    bottom the pipette tip can never reach), so a plan can't put "up to the
    physical max, minus whatever margin happens to survive an uneven split"
    into a well the way `_claim_wells_for_volume` did before this field
    existed (confirmed on real hardware: a well filled to instructed
    capacity ran dry on its last, small reload, well before the physically
    poured volume should have been exhausted).

    Falls back to `totalLiquidVolume` when `workingVolume` isn't set -
    every reservoir file in the library today has no such field, so this is
    the same number this function has always returned for them; opting a
    labware into a working-volume margin is a per-file addition, not a
    behavior change nobody asked for.
    """
    data = _load_reservoir_catalog(library_dir).get(reservoir_type)
    if not data:
        return None
    well = data.get("wells", {}).get(well_id)
    if not well:
        return None
    return well.get("workingVolume", well.get("totalLiquidVolume"))


def _get_explicit_working_volume(reservoir_type: str, well_id: str, library_dir: Optional[Path] = None) -> Optional[float]:
    """The well's `workingVolume` ONLY if the labware file actually sets one
    - unlike get_reservoir_well_capacity(), does NOT fall back to
    totalLiquidVolume when absent. Needed because that fallback makes the
    *effective* capacity trivially equal to the max whenever workingVolume
    is unset, which would make a naive `capacity >= max` check misfire on
    every ordinary reservoir with no working-volume margin at all."""
    data = _load_reservoir_catalog(library_dir).get(reservoir_type)
    if not data:
        return None
    well = data.get("wells", {}).get(well_id)
    return well.get("workingVolume") if well else None


def get_reservoir_well_max_volume(reservoir_type: str, well_id: str, library_dir: Optional[Path] = None) -> Optional[float]:
    """The well's true physical brim (`totalLiquidVolume`), unconditionally -
    ignores `workingVolume` even when the labware file sets one. Used only
    to validate that a labware's `workingVolume` (if present) is actually
    below this, never as a planning ceiling itself (use
    get_reservoir_well_capacity() for that)."""
    data = _load_reservoir_catalog(library_dir).get(reservoir_type)
    if not data:
        return None
    well = data.get("wells", {}).get(well_id)
    return well.get("totalLiquidVolume") if well else None


def get_plate_well_regex(plate_id: str, library_dir: Optional[Path] = None) -> str:
    """Return a regex pattern that matches valid well IDs for the given plate.

    Falls back to 96-well (A-H) if the plate is unknown.
    """
    plate = get_plate_info(plate_id, library_dir)
    if plate:
        row_labels = plate.get("row_labels", ["A","B","C","D","E","F","G","H"])
        max_col = plate.get("columns", 12)
        if len(row_labels) <= 8:
            row_pat = "A-H"
        elif len(row_labels) <= 16:
            row_pat = "A-P"
        else:
            row_pat = "A-Z"
        return rf'^[{row_pat}]\d{{1,{"2" if max_col <= 12 else "2"}}}$'
    return r'^[A-P]\d{1,2}$'  # permissive default: accept up to 384-well


def generate_all_wells(plate_id: str, library_dir: Optional[Path] = None) -> List[str]:
    """Return ordered list of all well IDs for the plate (column-major: A1,B1,...,H1,A2,...).

    Useful when the recipe needs to fill every well regardless of Excel input.
    """
    plate = get_plate_info(plate_id, library_dir)
    if plate is None:
        # Default 96-well
        rows, cols = ["A","B","C","D","E","F","G","H"], 12
    else:
        rows = plate.get("row_labels", ["A","B","C","D","E","F","G","H"])
        cols = plate.get("columns", 12)
    wells = []
    for c in range(1, cols + 1):
        for r in rows:
            wells.append(f"{r}{c}")
    return wells


def parse_excel_columns(file_content: bytes, filename: str = "recipe.xlsx") -> Dict[str, Any]:
    """
    Parse an Excel file and extract column information.
    
    This function ONLY detects columns - it does NOT make decisions about
    phases or delays. The user will configure that separately.
    
    Returns:
        {
            'filename': str,
            'sheets': [
                {
                    'sheet_name': str,
                    'columns': [
                        {'index': int, 'name': str, 'header': str, 'type': 'well'|'reagent'|'other'},
                        ...
                    ],
                    'wells': [{'well': 'A1', 'reagents': {'HA': 10.0, 'DMTMM': 5.0}}],
                    'preview_rows': int
                }
            ],
            'summary': {...}
        }
    """
    try:
        import openpyxl
    except ImportError:
        return {'error': 'openpyxl not installed. Run: pip install openpyxl'}
    
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_content), data_only=True)
    except Exception as e:
        return {'error': f'Failed to load Excel file: {str(e)}'}
    
    sheets_data = []
    
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        sheet_result = _parse_sheet_columns(ws, sheet_name)
        sheets_data.append(sheet_result)
    
    return {
        'filename': filename,
        'sheets': sheets_data,
        'summary': {
            'total_sheets': len(wb.sheetnames),
            'sheet_names': wb.sheetnames
        }
    }


def _parse_sheet_columns(ws, sheet_name: str) -> Dict[str, Any]:
    """Parse a single worksheet to extract column information."""
    
    # Find the header row (contains volume columns with µL or uL)
    header_row = None
    headers = []
    
    for row_idx in range(1, min(20, ws.max_row + 1)):
        row_values = [str(cell.value or '').strip() for cell in ws[row_idx]]
        
        # Look for "Well" column AND volume columns (uL/µL/mL, in parens or as a loose substring)
        has_well = any('well' in v.lower() for v in row_values)
        has_volume = any(
            _VOLUME_UNIT_RE.search(v) or 'ul' in v.lower() or 'µl' in v.lower()
            for v in row_values
        )
        
        if has_well and has_volume:
            header_row = row_idx
            headers = row_values
            break
    
    if header_row is None:
        return {
            'sheet_name': sheet_name,
            'error': 'Could not find header row with Well and volume columns'
        }
    
    # Parse all columns
    columns = []
    well_col = None
    reagent_columns = []
    
    for col_idx, header in enumerate(headers):
        h_lower = header.lower()
        
        col_info = {
            'index': col_idx,
            'header': header,
            'name': header,
            'type': 'other'
        }
        
        unit_match = _VOLUME_UNIT_RE.search(header)

        if 'well' in h_lower and well_col is None:
            well_col = col_idx
            col_info['type'] = 'well'
            col_info['name'] = 'Well'
        elif unit_match:
            # mL headers are converted to uL when reading data rows below
            unit_multiplier = 1000.0 if unit_match.group(1).lower() == 'm' else 1.0

            # Extract clean reagent name: drop the unit parenthetical (it can appear
            # before or after the reagent, e.g. "HA (uL)" or "Volume i (mL) HA")
            # and a leading "Volume i" label some sheets prefix the reagent with.
            reagent_name = _VOLUME_UNIT_RE.sub('', header)
            reagent_name = re.sub(r'^\s*volume\s+i?\s*', '', reagent_name, flags=re.IGNORECASE).strip()
            # Strip a trailing concentration label like " 10mM" / " 20%" - requires a
            # preceding space so chemical names ending in digits (e.g. "GEL-N3") are untouched.
            reagent_name = re.sub(r'\s+\d+[%mM]*\s*$', '', reagent_name).strip()

            # Skip "Total Volume" - it's calculated
            if 'total' in reagent_name.lower() and 'volume' in reagent_name.lower():
                col_info['type'] = 'calculated'
            else:
                col_info['type'] = 'reagent'
                col_info['name'] = reagent_name
                col_info['unit_multiplier'] = unit_multiplier
                reagent_columns.append(col_idx)
        elif 'ratio' in h_lower or 'molar' in h_lower:
            col_info['type'] = 'ratio'
        
        columns.append(col_info)
    
    if well_col is None:
        return {
            'sheet_name': sheet_name,
            'columns': columns,
            'error': 'Could not find Well column'
        }
    
    if not reagent_columns:
        return {
            'sheet_name': sheet_name,
            'columns': columns,
            'error': 'Could not find reagent volume columns'
        }
    
    # Parse data rows
    wells_data = []
    reagent_names = [columns[i]['name'] for i in reagent_columns]
    
    for row_idx in range(header_row + 1, ws.max_row + 1):
        row = list(ws.iter_rows(min_row=row_idx, max_row=row_idx, values_only=True))[0]
        
        if len(row) <= well_col:
            continue
            
        well = str(row[well_col] or '').strip().upper()
        
        # Accept well IDs up to 384-well (A-P, cols 1-24); permissive to support any plate
        if not well or not re.match(r'^[A-P]\d{1,2}$', well):
            continue
        
        well_entry = {
            'well': well,
            'reagents': {}
        }
        
        # Get volumes for each reagent (converted to uL per column's unit_multiplier)
        for reagent_col_idx in reagent_columns:
            reagent_name = columns[reagent_col_idx]['name']
            unit_multiplier = columns[reagent_col_idx].get('unit_multiplier', 1.0)
            if len(row) > reagent_col_idx:
                try:
                    vol = float(row[reagent_col_idx] or 0) * unit_multiplier
                    if vol > 0:
                        well_entry['reagents'][reagent_name] = vol
                except:
                    pass
        
        if well_entry['reagents']:
            wells_data.append(well_entry)
    
    return {
        'sheet_name': sheet_name,
        'header_row': header_row,
        'columns': columns,
        'reagent_columns': [columns[i] for i in reagent_columns],
        'wells': wells_data,
        'summary': {
            'total_wells': len(wells_data),
            'reagent_count': len(reagent_columns),
            'reagents': reagent_names
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# Module treatment catalog for the recipe generator's generic "+ Add step" UI.
#
# This mirrors the relevant subset of COMMAND_SCHEMA / VALID_MODULE_TYPES from
# v1/SiLA2/OpentronsSiLA2Server/src/protocol_generator.py. It is a hand-kept
# mirror rather than a shared import because the webapp and the SiLA2 server
# are deployed as separate services - if protocol_generator.py's schema for
# these commands changes, update this catalog to match.
# ═══════════════════════════════════════════════════════════════════════════

MODULE_TREATMENT_CATALOG: Dict[str, Dict] = {
    "HeaterShaker": {
        "label": "Heater-Shaker (heat / shake)",
        "module_type": "heaterShakerModuleV1",
        "requirement_key": "HeaterShaker",
        "command": "HeaterShaker",
        # The labware latch is not a user preference: Opentrons refuses to shake
        # with it open, and the gripper can't lift labware while it's clamped shut.
        # Both are derived automatically (see _emit_treatment) instead of exposing
        # "close/open latch" checkboxes someone could forget to tick.
        "release_field": "OpenLatch",
        "fields": [
            {"name": "Temperature", "label": "Temperature", "type": "number", "unit": "°C", "min": 4, "max": 95},
            {"name": "RPM", "label": "Speed", "type": "number", "unit": "rpm", "min": 200, "max": 3000},
            {"name": "Duration", "label": "Shake duration", "type": "number", "unit": "s", "min": 0},
            {"name": "WaitForTemp", "label": "Wait until temperature is reached", "type": "bool", "default": True},
            {"name": "KeepShaking", "label": "Keep shaking after this step", "type": "bool", "default": False},
            {"name": "DeactivateHeater", "label": "Turn off heating", "type": "bool", "default": False},
        ],
    },
    "TemperatureModule": {
        "label": "Temperature Module",
        "module_type": "temperatureModuleV2",
        "requirement_key": "TemperatureModule",
        "command": "TemperatureModule",
        "release_field": None,  # flat block, no clamp/lid over the labware
        "fields": [
            {"name": "Temperature", "label": "Temperature", "type": "number", "unit": "°C", "min": -10, "max": 99},
            {"name": "WaitForTemp", "label": "Wait until temperature is reached", "type": "bool", "default": True},
            {"name": "Deactivate", "label": "Turn off the module", "type": "bool", "default": False},
        ],
    },
    "Thermocycler": {
        "label": "Thermocycler",
        "module_type": "thermocyclerModuleV2",
        "requirement_key": "Thermocycler",
        "command": "Thermocycler",
        "release_field": "OpenLid",  # closed lid blocks the gripper the same way a HeaterShaker latch does
        "fields": [
            {"name": "CloseLid", "label": "Close lid", "type": "bool", "default": False},
            {"name": "LidTemperature", "label": "Lid temperature", "type": "number", "unit": "°C"},
            {"name": "BlockTemperature", "label": "Block temperature", "type": "number", "unit": "°C"},
            {"name": "HoldTime", "label": "Hold time", "type": "number", "unit": "s"},
            {"name": "DeactivateLid", "label": "Turn off lid", "type": "bool", "default": False},
            {"name": "DeactivateBlock", "label": "Turn off block", "type": "bool", "default": False},
        ],
    },
    "MagneticModule": {
        "label": "Magnetic Module",
        "module_type": "magneticModuleV2",
        "requirement_key": "MagneticModule",
        "command": "MagneticModule",
        "release_field": None,  # no clamp/lid over the labware
        "fields": [
            {"name": "Engage", "label": "Engage magnet", "type": "bool", "default": True},
            {"name": "Disengage", "label": "Disengage magnet", "type": "bool", "default": False},
            {"name": "Height", "label": "Height", "type": "number", "unit": "mm"},
        ],
    },
    "AbsorbanceReader": {
        "label": "Absorbance Reader",
        "module_type": "absorbanceReaderV1",
        "requirement_key": "AbsorbanceReader",
        "command": "AbsorbanceReader",
        "release_field": "OpenLid",
        "fields": [
            {"name": "Initialize", "label": "Initialize", "type": "bool", "default": True},
            {"name": "Read", "label": "Run reading", "type": "bool", "default": True},
            {"name": "CloseLid", "label": "Close lid", "type": "bool", "default": False},
        ],
    },
}

# Ready-made shortcuts built on top of the generic mechanism above (not a
# separate implementation) - they just pre-fill common field combinations.
TREATMENT_PRESETS: Dict[str, Dict] = {
    "heat_shake": {
        "label": "Heat and shake",
        "kind": "HeaterShaker",
        "target": "TargetPlate",
        "params": {"Temperature": 37, "RPM": 300, "Duration": 300, "WaitForTemp": True},
        "return_after": True,  # latch opens automatically before the return move
    },
    "keep_warm": {
        "label": "Keep warm",
        "kind": "HeaterShaker",
        "target": None,  # user must pick which reagent/source stays warm
        "params": {"Temperature": 37, "WaitForTemp": True},
        "return_after": False,  # stays on the module, latch stays closed, until returned
        # Applied automatically to `post_treatments` to deactivate heating and move
        # it back (the return move auto-opens the latch, no field needed for that).
        "cleanup_params": {"DeactivateHeater": True},
    },
}


def get_step_type_catalog() -> Dict:
    """Module-treatment catalog + presets for the recipe generator's step UI."""
    return {"modules": MODULE_TREATMENT_CATALOG, "presets": TREATMENT_PRESETS}


def _resolve_treatment_target(target: str, reagent_to_source_map: Dict[str, str]) -> str:
    """A treatment target is either the fixed key 'TargetPlate' or a reagent name
    (resolved to whichever Source_N reservoir that reagent has been assigned to)."""
    if target == "TargetPlate":
        return "TargetPlate"
    return reagent_to_source_map.get(target, target)


# Deck-slot convention shared with generate_hal_config_from_recipe(). Module
# treatments need to know a target's *physical deck slot* (e.g. "D2") to move it
# back home after a treatment - the execution engine resolves a MoveLabware
# NewLocation string against loaded labware/module keys first, so returning a
# labware "to itself" (e.g. NewLocation="TargetPlate" for LabwareID="TargetPlate")
# is meaningless and would fail. Both pools must stay in sync with the HAL
# generator, since recipe Steps and the generated HAL are meant to be used together.
MODULE_SLOT_POOL = ['A1', 'D1', 'C1', 'B1']
RESERVOIR_SLOT_POOL = ['A2', 'B2', 'C2', 'D3', 'D1', 'C1', 'B1', 'C3']
TARGET_PLATE_SLOT = 'D2'

# Every plain deck slot generate_generic_hal() can hand out for tip racks or
# reservoirs once A3 (trash) and D2 (target plate) are fixed - the Flex deck
# has no more free slots than this, so this is the real ceiling on how many
# simultaneous tip sizes (or reservoirs) a generated HAL can carry, not an
# arbitrary "2" picked in code. Ordered by tip-rack preference: B3/C3 first
# (today's established default location for 1-2 sizes), then A1 (otherwise
# entirely unused by this generator), then the reservoir pool's own
# lowest-priority slots first - so extra tip sizes eat into the reservoir
# slots reservoirs would have wanted least, for as long as possible.
GENERIC_TIP_SLOT_PREFERENCE = ['B3', 'C3', 'A1', 'B1', 'C1', 'D1', 'D3', 'C2', 'B2', 'A2']


def _compute_home_slots(all_treatments: List[Dict], num_sources: int) -> Dict[str, str]:
    """Predict the deck slot each treatable labware (TargetPlate / Source_N) will be
    assigned in the HAL, following the exact same convention as
    generate_hal_config_from_recipe(), so MoveLabware-back steps target a real slot."""
    distinct_modules: List[str] = []
    for t in all_treatments:
        kind = t.get("kind")
        entry = MODULE_TREATMENT_CATALOG.get(kind) if kind else None
        if entry and entry["requirement_key"] not in distinct_modules:
            distinct_modules.append(entry["requirement_key"])

    reserved_module_slots = MODULE_SLOT_POOL[:len(distinct_modules)]
    reservoir_slots = [s for s in RESERVOIR_SLOT_POOL if s not in reserved_module_slots]

    home_slots = {"TargetPlate": TARGET_PLATE_SLOT}
    for idx in range(num_sources):
        source_key = f"Source_{idx + 1}"
        home_slots[source_key] = reservoir_slots[idx] if idx < len(reservoir_slots) else source_key
    return home_slots


def _emit_treatment(
    treatment: Dict,
    steps: List[Dict],
    requirements: Dict,
    modules_used: Dict[str, str],
    current_location: Dict[str, str],
    reagent_to_source_map: Dict[str, str],
    home_slots: Dict[str, str],
) -> None:
    """Emit MoveLabware -> module command -> (optional) MoveLabware-back for one
    configured treatment. Adds the module to `requirements`/`modules_used` only
    when it is actually used by at least one treatment."""
    treatment_kind = treatment.get("kind")
    catalog_entry = MODULE_TREATMENT_CATALOG.get(treatment_kind) if treatment_kind else None
    raw_target = treatment.get("target")
    if not catalog_entry or not raw_target:
        return

    target_key = _resolve_treatment_target(raw_target, reagent_to_source_map)
    requirement_key = catalog_entry["requirement_key"]

    requirements.setdefault(requirement_key, requirement_key)
    modules_used[requirement_key] = catalog_entry["module_type"]

    # `home_slot` is always the labware's true original deck slot (static, from the
    # precomputed table) - NOT wherever it happens to be right now. Two treatments
    # can target the same labware (e.g. "keep warm" then "deactivate & return"), and
    # the second one still needs the real home slot, not its current position.
    home_slot = home_slots.get(target_key, target_key)
    current_location.setdefault(target_key, home_slot)

    # Move the labware onto the module only if it isn't already there.
    if current_location[target_key] != requirement_key:
        move_cmd = {
            "Command": "MoveLabware",
            "LabwareID": target_key,
            "NewLocation": requirement_key,
            "UseGripper": True,
        }
        gripper_offset = treatment.get("gripper_offset") or {}
        if gripper_offset.get("pick_up"):
            move_cmd["PickUpOffset"] = gripper_offset["pick_up"]
        if gripper_offset.get("drop"):
            move_cmd["DropOffset"] = gripper_offset["drop"]
        steps.append(move_cmd)
        current_location[target_key] = requirement_key

    # The module command itself - only pass through fields this module actually accepts.
    field_names = {f["name"] for f in catalog_entry["fields"]}
    params = {k: v for k, v in (treatment.get("params") or {}).items() if k in field_names and v not in (None, "")}

    will_return = treatment.get("return_after", True)

    # Physical necessity, not a user preference - derived automatically instead of
    # relying on a checkbox that's easy to forget (and whose omission causes
    # exactly the kind of gripper/latch collision this system needs to avoid):
    # - shaking requires the labware latch closed (Opentrons rejects it otherwise);
    # - moving labware away from a module requires its latch/lid already open
    #   (the gripper can't lift labware that's clamped down or covered).
    if requirement_key == "HeaterShaker" and params.get("RPM"):
        params["CloseLatch"] = True
    release_field = catalog_entry.get("release_field")
    if will_return and release_field:
        params[release_field] = True

    module_cmd = {"Command": catalog_entry["command"], "ModuleID": requirement_key, **params}
    if treatment.get("description"):
        module_cmd["Description"] = treatment["description"]
    steps.append(module_cmd)

    # Move back to its original slot, unless the treatment explicitly leaves it
    # on the module (e.g. "keep warm" across several phases).
    if will_return and current_location[target_key] != home_slot:
        steps.append({
            "Command": "MoveLabware",
            "LabwareID": target_key,
            "NewLocation": home_slot,
            "UseGripper": True,
        })
        current_location[target_key] = home_slot


def _claim_wells_for_volume(volume_ul: float, capacity: Optional[float],
                             reservoir_wells: List[str], used: set
                             ) -> Tuple[List[Tuple[str, float]], int]:
    """Claim as many free wells (in reservoir_wells order, so consecutive
    claims stay physically adjacent within a reservoir's own layout) as
    needed to hold volume_ul, filling each one toward `capacity` before
    moving to the next ("load until exhausted", not split evenly). Mutates
    `used`. Returns (fill_plan, wells_needed): fill_plan is
    [(well, volume_to_load_ul), ...], shorter than wells_needed if the
    reservoir ran out of free wells - the caller must warn about that.
    Unknown/zero capacity means "can't tell how many wells this needs" -
    treated as a single well holding the whole volume."""
    if capacity and capacity > 0:
        wells_needed = max(1, -(-int(volume_ul) // int(capacity)))
    else:
        wells_needed = 1
    free = [w for w in reservoir_wells if w not in used]
    claimed = free[:wells_needed]
    used.update(claimed)

    fill_plan: List[Tuple[str, float]] = []
    remaining = volume_ul
    for well in claimed:
        amount = min(capacity, remaining) if capacity else remaining
        fill_plan.append((well, round(amount, 1)))
        remaining -= amount
    return fill_plan, wells_needed


def _bucket_transfers_by_capacity(transfers: List[Dict], capacity: float) -> List[List[Dict]]:
    """Greedily group destination transfers so each bucket's total volume stays
    within a single source well's capacity (used for the Fase-3 auto-split when a
    reagent's own reservoir well can't hold its whole required volume)."""
    buckets: List[List[Dict]] = []
    current: List[Dict] = []
    current_vol = 0.0
    for t in transfers:
        if current and current_vol + t['volume'] > capacity:
            buckets.append(current)
            current = []
            current_vol = 0.0
        current.append(t)
        current_vol += t['volume']
    if current:
        buckets.append(current)
    return buckets


# Optional per-reagent advanced liquid-handling fields, merged directly onto the
# Distribute/Transfer command the generator already builds. The execution engine
# (protocol_generator.py) applies these identically regardless of command type -
# see its shared handler for "Transfer"/"Distribute"/"Consolidate".
_ADVANCED_LIQUID_FIELDS = {"air_gap", "blow_out", "mix_after_cycles", "mix_after_volume", "rate"}


# Known Flex pipette volume ranges (verified against docs.opentrons.com product
# specs). generate_hal_config_from_recipe() always mounts "flex_1channel_1000" on
# 'left' today, so that's the default used for the waste estimate below; override
# via config['pipette_min_volume_ul']/['pipette_max_volume_ul'] if that ever changes.
FLEX_PIPETTE_VOLUMES = {
    "flex_1channel_1000": {"min": 5, "max": 1000},
    "flex_1channel_50": {"min": 1, "max": 50},
    "flex_8channel_1000": {"min": 5, "max": 1000},
    "flex_8channel_50": {"min": 1, "max": 50},
    "flex_96channel_1000": {"min": 5, "max": 1000},
}

# Real Opentrons Flex tip catalog - optimal pipetting range per tip size,
# straight from Opentrons' own "Opentrons Flex Tips" brochure. Deliberately
# OVERLAPPING (e.g. both the 50uL and 200uL tip cover 5-50uL) so
# select_tip_for_volume() below can always prefer the smallest/most precise
# tip that still covers a given volume, rather than a single non-overlapping
# partition that would force a coarser tip than necessary near the edges.
FLEX_TIP_RANGES = {
    20: {"min": 1, "max": 20, "load_name": "opentrons_flex_96_tiprack_20ul"},
    50: {"min": 1, "max": 50, "load_name": "opentrons_flex_96_tiprack_50ul"},
    200: {"min": 5, "max": 200, "load_name": "opentrons_flex_96_tiprack_200ul"},
    1000: {"min": 5, "max": 1000, "load_name": "opentrons_flex_96_tiprack_1000ul"},
}


def select_tip_for_volume(volume_ul: float, available_sizes: List[int]) -> Optional[int]:
    """Pick the smallest/most precise tip (from `available_sizes`, e.g. the
    tip sizes the user has actually loaded on the deck for this HAL) whose
    range covers `volume_ul`. Ranges overlap on purpose (see FLEX_TIP_RANGES)
    so "smallest size whose max covers the volume, among those that also
    cover the volume's min" is well-defined and always the most precise
    valid choice - never a wrong-but-technically-fitting compromise.

    Returns None if no available size covers the volume (e.g. asked to
    pipette 0.5uL, or 1500uL, with only 1000uL tips on hand) - the caller
    must treat that as a hard error, not silently fall back to an imprecise
    or physically impossible tip.
    """
    candidates = [
        size for size in available_sizes
        if size in FLEX_TIP_RANGES
        and FLEX_TIP_RANGES[size]["min"] <= volume_ul <= FLEX_TIP_RANGES[size]["max"]
    ]
    return min(candidates) if candidates else None


def select_tip_for_volume_range(min_volume_ul: float, max_volume_ul: float,
                                 available_sizes: List[int]) -> Optional[int]:
    """Like select_tip_for_volume(), but for a whole reagent's worth of
    per-destination volumes at once: the smallest available tip size whose
    range covers EVERY individual dispense from min_volume_ul to
    max_volume_ul, not just the largest one - a tip whose max covers the
    biggest transfer but whose min is above the smallest one would still be
    physically unable to draw that smallest amount precisely.

    Returns None if no single available size covers the whole range (e.g. a
    reagent going from 2uL to 900uL with only 20uL+1000uL tips on hand - no
    single tip does both) - the caller must raise a clear error rather than
    pick two different tips mid-reagent, which the generator doesn't support.
    """
    candidates = [
        size for size in available_sizes
        if size in FLEX_TIP_RANGES
        and FLEX_TIP_RANGES[size]["min"] <= min_volume_ul
        and FLEX_TIP_RANGES[size]["max"] >= max_volume_ul
    ]
    return min(candidates) if candidates else None


def _partition_transfers_by_tip(transfers: List[Dict], tip_sizes_ul: Optional[List[int]]
                                 ) -> Tuple[List[Tuple[Optional[int], List[Dict]]], List[Dict]]:
    """Group a reagent's per-destination transfers by which available tip
    size EACH INDIVIDUAL transfer's volume needs (select_tip_for_volume, not
    the whole-reagent-range version) - so a reagent spanning e.g. 2-900uL
    with both a 50uL and a 1000uL tip loaded gets the 50uL tip for its small
    destinations and the 1000uL tip for its large ones, instead of either
    picking one tip for everything or refusing the whole reagent because no
    single tip covers the full range.

    Returns (groups, unfit): `groups` is an ordered list of
    (tip_size, transfers_needing_that_size) pairs, LARGEST tip first (coarse
    bulk volume goes down before the fine top-up, and it means whichever
    physical rack the pipette is already holding tends to be the one that
    still has more wells left to serve) - each entry only present if at
    least one transfer needs it. `unfit` holds any
    transfer whose volume no available size covers at all - the caller must
    surface these (e.g. as a per-well note) rather than silently drop them;
    every OTHER well for the same reagent still gets generated normally.

    No `tip_sizes_ul` configured at all (None/empty) means "not opted into
    per-volume tip selection" - returns a single group keyed `None`
    (the caller's sentinel for "use the global pipette_max/min default and
    the plain 'MyTips' rack", today's exact behavior) with everything in it,
    and no unfit transfers.
    """
    if not tip_sizes_ul:
        return [(None, list(transfers))], []
    groups: Dict[int, List[Dict]] = {}
    unfit: List[Dict] = []
    for t in transfers:
        chosen = select_tip_for_volume(t['volume'], tip_sizes_ul)
        if chosen is None:
            unfit.append(t)
        else:
            groups.setdefault(chosen, []).append(t)
    return sorted(groups.items(), reverse=True), unfit


def _split_into_trips(volumes: List[float], dest_wells: List[str],
                       tip_capacity: float, disposal_volume: float
                       ) -> List[Tuple[List[float], List[str]]]:
    """Greedily group a reagent's (volume, destination) pairs into the same
    per-trip batches Opentrons' distribute() forms internally: aspirate enough
    for several destinations plus one disposal_volume, dispense to all of them,
    reload, repeat (docs.opentrons.com, distribute() disposal_volume). The real
    Opentrons algorithm may batch slightly differently, so treat trip boundaries
    as a ballpark - but the trip COUNT is what matters here, since each group
    becomes its own PickUpTip/Distribute/DropTip step (see excel_recipe_parser's
    Distribute branch) when "Verify aspiration" needs a fresh, probe-eligible
    tip on every reload. Also reused by _estimate_distribute_waste below."""
    if not volumes:
        return []
    usable_capacity = tip_capacity - disposal_volume
    if usable_capacity <= 0:
        usable_capacity = tip_capacity
    trips: List[Tuple[List[float], List[str]]] = []
    i = 0
    n = len(volumes)
    while i < n:
        used = 0.0
        start = i
        while i < n and used + volumes[i] <= usable_capacity:
            used += volumes[i]
            i += 1
        if i == start:  # a single destination volume alone exceeds the tip - take it anyway
            i += 1
        trips.append((volumes[start:i], dest_wells[start:i]))
    return trips


def _estimate_distribute_waste(volumes: List[float], tip_capacity: float, disposal_volume: float) -> float:
    """Approximate the liquid Opentrons' distribute() blows to trash as "disposal
    volume" - one disposal_volume per reload trip (see _split_into_trips)."""
    if disposal_volume <= 0 or not volumes:
        return 0.0
    trips = _split_into_trips(volumes, [""] * len(volumes), tip_capacity, disposal_volume)
    return len(trips) * disposal_volume


def _apply_advanced_liquid_options(cmd: Dict, adv: Dict) -> None:
    if not adv:
        return
    if adv.get("air_gap"):
        cmd["AirGap"] = adv["air_gap"]
    if adv.get("blow_out"):
        cmd["BlowOut"] = True
    if adv.get("mix_after_cycles") and adv.get("mix_after_volume"):
        cmd["MixAfter"] = [adv["mix_after_cycles"], adv["mix_after_volume"]]
    if adv.get("rate"):
        cmd["DispenseRate"] = adv["rate"]
    if adv.get("verify_liquid"):
        cmd["VerifyLiquid"] = True


def generate_recipe_with_config(
    sheet_data: Dict,
    config: Dict,
    recipe_name: Optional[str] = None
) -> Dict:
    """
    Generate a recipe from parsed sheet data using user-provided configuration.
    
    The config specifies how columns are grouped and delays between groups:
    {
        "groups": [
            {
                "name": "Phase A",
                "columns": ["HA", "DMTMM"],
                "delay_after_seconds": 1800  # 30 minutes
            },
            {
                "name": "Phase B",
                "columns": ["AMF"],
                "delay_after_seconds": 0
            }
        ],
        "target_plate": "corning_96_wellplate_360ul_flat",
        "reservoir_type": "nest_12_reservoir_15ml",
        "pipette": "left"
    }
    
    This generates a SINGLE recipe with embedded Delay commands.
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    if not recipe_name:
        recipe_name = re.sub(r'[^\w]', '_', sheet_data.get('sheet_name', 'Recipe'))
    
    groups = config.get('groups', [])
    wells = sheet_data.get('wells', [])
    
    if not wells:
        return {'error': 'No well data found'}
    
    if not groups:
        # Default: all reagents in one group, no delays
        all_reagents = sheet_data.get('summary', {}).get('reagents', [])
        groups = [{'name': 'All', 'columns': all_reagents, 'delay_after_seconds': 0}]
    
    # Build Requirements section with GENERIC numbered sources (Source_1, Source_2, ...)
    # This allows reusing the same HAL for different recipes.
    #
    # Reagents can share a physical reservoir (multiple wells on the same plate)
    # instead of always getting one dedicated reservoir each - controlled by
    # config['num_reservoirs'] (default: one reservoir per reagent, unchanged
    # behavior). Reagents fill each reservoir's wells in order before moving on
    # to the next one, mirroring how a reservoir would actually be loaded by hand.
    requirements = {}
    all_reagents_in_config = []
    reagent_to_source_map = {}  # Maps reagent name -> generic source (e.g., "HA" -> "Source_1")
    reagent_to_well_map = {}    # Maps reagent name -> well within its source (e.g., "HA" -> "A1")

    all_group_reagents = [r for group in groups for r in group.get('columns', [])]
    reservoir_type = config.get('reservoir_type', 'nest_12_reservoir_15ml')
    reservoir_wells = get_reservoir_wells(reservoir_type)

    num_reservoirs = config.get('num_reservoirs') or len(all_group_reagents) or 1
    num_reservoirs = max(1, min(int(num_reservoirs), len(all_group_reagents) or 1))
    reagents_per_reservoir = -(-len(all_group_reagents) // num_reservoirs) if all_group_reagents else 1  # ceil

    # Required-quantity estimate per reagent (nominal + excess). `excess_margin_pct`
    # / `dead_volume_ul` are rough, overridable defaults (10% safety margin + a
    # generic reservoir dead volume) - real dead volume depends on the specific
    # labware/liquid, so this is an estimate to double-check, not a measured constant.
    excess_margin_pct = config.get('excess_margin_pct', 10)
    dead_volume_ul = config.get('dead_volume_ul', 50)
    required_volumes: Dict[str, Dict] = {}
    for reagent in set(all_group_reagents):
        nominal = sum(well_data['reagents'].get(reagent, 0) for well_data in wells)
        excess = round(nominal * (excess_margin_pct / 100.0) + dead_volume_ul, 1)
        required_volumes[reagent] = {
            'nominal_ul': round(nominal, 1),
            'excess_ul': excess,
            'with_excess_ul': round(nominal + excess, 1)
        }

    quantity_notes: List[str] = []
    reagent_waste_ul: Dict[str, float] = {}
    pipette_max_volume = config.get('pipette_max_volume_ul', FLEX_PIPETTE_VOLUMES['flex_1channel_1000']['max'])
    pipette_min_volume = config.get('pipette_min_volume_ul', FLEX_PIPETTE_VOLUMES['flex_1channel_1000']['min'])

    # Well capacity is treated as uniform across a reservoir type (true for
    # every reservoir/plate in the library today - each has one
    # totalLiquidVolume per well) - so one lookup covers every reagent.
    #
    # Precedence, highest first: (1) config['reservoir_working_volume_ul'] -
    # a one-off override for THIS generation, e.g. "I know this specific
    # batch of tubing/liquid leaves more dead volume than usual today";
    # (2) the labware file's own `workingVolume` - the physical, reusable
    # default for this labware type, correct for every future recipe using
    # it too; (3) `totalLiquidVolume` (the physical max) when neither is
    # set. Whichever value actually ends up in effect gets the same
    # sanity check below: it must be a real margin BELOW the physical max,
    # never >= it - that would defeat reserving dead-volume headroom at all
    # and silently let a plan fill a well past what the margin was supposed
    # to protect. The config override is intentionally NOT compared against
    # the labware's own workingVolume, only against the physical max - a
    # one-off override is free to be more (or less) conservative than the
    # labware's usual default.
    reservoir_well_max = (
        get_reservoir_well_max_volume(reservoir_type, reservoir_wells[0]) if reservoir_wells else None
    )
    working_volume_override = config.get('reservoir_working_volume_ul')
    if working_volume_override is not None:
        reservoir_well_capacity = working_volume_override
        working_volume_source = "config override"
    elif reservoir_wells:
        reservoir_well_capacity = get_reservoir_well_capacity(reservoir_type, reservoir_wells[0])
        working_volume_source = "labware definition" if _get_explicit_working_volume(reservoir_type, reservoir_wells[0]) is not None else None
    else:
        reservoir_well_capacity = None
        working_volume_source = None

    if working_volume_source and reservoir_well_max is not None and reservoir_well_capacity is not None:
        if reservoir_well_capacity >= reservoir_well_max:
            source_hint = (
                "config['reservoir_working_volume_ul']" if working_volume_source == "config override"
                else "the labware definition's workingVolume"
            )
            return {
                'error': (
                    f"Working volume ({reservoir_well_capacity} uL, from {working_volume_source}) is >= "
                    f"'{reservoir_type}' physical totalLiquidVolume ({reservoir_well_max} uL) - it must be a "
                    f"real margin below the max, not equal to or above it. Lower {source_hint}, "
                    f"or drop it entirely to just use the max."
                )
            }
    used_wells_by_source: Dict[str, set] = {}
    primary_fill_plans: Dict[str, List[Tuple[str, float]]] = {}
    backup_fill_plans: Dict[str, List[Tuple[str, float]]] = {}
    reagent_to_backup_well_map: Dict[str, List[str]] = {}
    already_allocated: set = set()  # guards against a reagent listed in >1 group

    for idx, reagent in enumerate(all_group_reagents):
        source_idx = idx // reagents_per_reservoir
        source_key = f"Source_{source_idx + 1}"
        requirements.setdefault(source_key, source_key)
        reagent_to_source_map[reagent] = source_key
        all_reagents_in_config.append(reagent)

        if reagent in already_allocated:
            continue  # same reagent appears in another group too - wells already claimed
        already_allocated.add(reagent)

        rv = required_volumes.get(reagent, {})
        with_excess = rv.get('with_excess_ul', 0)
        has_manual_batching = any(
            reagent in g.get('reagent_batches', {}) and g['reagent_batches'][reagent].get('batch_size')
            for g in groups
        )
        used = used_wells_by_source.setdefault(source_key, set())

        if has_manual_batching:
            # Manual batching (config['groups'][i]['reagent_batches']) manages
            # its own per-batch wells further down - this loop just needs to
            # reserve this reagent's single starting well, unchanged from
            # before.
            free = [w for w in reservoir_wells if w not in used]
            claimed = free[:1] or (reservoir_wells[-1:] if reservoir_wells else [])
            used.update(claimed)
            fill_plan = [(w, with_excess) for w in claimed]
            wells_needed = 1
        else:
            # Claims as many wells as this reagent's own volume needs
            # ("load until exhausted" per well) regardless of whether it has
            # a reservoir to itself or is sharing one - a shared reservoir
            # used to only ever get ONE well no matter how much volume was
            # needed, silently producing an impossible "load 7042 uL into a
            # 3400 uL well" instruction. Wells are drawn from whatever
            # earlier reagents in this SAME reservoir haven't already
            # claimed, so nothing collides.
            fill_plan, wells_needed = _claim_wells_for_volume(with_excess, reservoir_well_capacity, reservoir_wells, used)
            if not fill_plan:  # reservoir had nothing free at all - reuse the last well rather than crash
                fallback_well = reservoir_wells[-1] if reservoir_wells else 'A1'
                used.add(fallback_well)
                fill_plan = [(fallback_well, with_excess)]

        claimed_wells = [w for w, _ in fill_plan]
        reagent_to_well_map[reagent] = claimed_wells[0] if len(claimed_wells) == 1 else claimed_wells
        primary_fill_plans[reagent] = fill_plan

        if len(claimed_wells) < wells_needed:
            quantity_notes.append(
                f"{reagent}: WARNING - needs {wells_needed} well(s) to hold {with_excess} uL but only "
                f"{len(claimed_wells)} free in {source_key} - use a bigger/additional reservoir, or "
                f"share this reservoir with fewer reagents"
            )

        # Backup wells for the runtime liquid-fallback feature: whenever this
        # reagent has reagent_advanced[reagent]['verify_liquid'] on, it
        # automatically gets enough EXTRA wells to hold its required volume
        # (with_excess_ul) again - same sizing as the primary allocation
        # just above, applied a second time as a reserve. No manual count to
        # guess: if the primary well(s) turn out to be a total loss, the
        # backups alone can cover the reagent from scratch. Claimed right
        # here, in this same reagent's turn (not in a separate pass over all
        # reagents afterwards), so this reagent's ENTIRE footprint (primary
        # + backup) is one contiguous run in the reservoir's own physical
        # ordering instead of being scattered by other reagents' backups
        # claiming the wells in between. Actually trying these at runtime
        # (skipping ahead only if the current one is found dry) happens in
        # protocol_generator.py; this just reserves which physical wells are
        # available for that.
        if not has_manual_batching and config.get('reagent_advanced', {}).get(reagent, {}).get('verify_liquid'):
            backup_plan, backup_needed = _claim_wells_for_volume(with_excess, reservoir_well_capacity, reservoir_wells, used)
            if backup_plan:
                reagent_to_backup_well_map[reagent] = [w for w, _ in backup_plan]
                backup_fill_plans[reagent] = backup_plan
            if len(backup_plan) < backup_needed:
                quantity_notes.append(
                    f"{reagent}: needs {backup_needed} backup well(s) to cover {with_excess} uL again for "
                    f"liquid-fallback but only {len(backup_plan)} free well(s) available in {source_key} - "
                    f"assign fewer reagents to this reservoir or use a reservoir with more/bigger wells"
                )

    # Human-readable loading plan, one block per reagent (primary wells
    # immediately followed by its own backup wells, if any) instead of
    # scattered separately across the whole reagent list - much easier to
    # actually follow at the bench than a flat, unordered note dump.
    seen_reagents: List[str] = []
    for r in all_group_reagents:
        if r not in seen_reagents:
            seen_reagents.append(r)

    for reagent in seen_reagents:
        rv = required_volumes.get(reagent, {})
        src = reagent_to_source_map.get(reagent, '?')
        fill_plan = primary_fill_plans.get(reagent, [])
        total_to_load = round(sum(v for _, v in fill_plan), 1)
        if len(fill_plan) <= 1:
            well_desc = f"{src}:{fill_plan[0][0]}" if fill_plan else "?"
            quantity_notes.append(
                f"{reagent}: {rv.get('nominal_ul')} uL nominal -> load {total_to_load} uL into {well_desc}"
            )
        else:
            per_well = "; ".join(f"{w} = {v} uL" for w, v in fill_plan)
            quantity_notes.append(
                f"{reagent}: {rv.get('nominal_ul')} uL nominal ({total_to_load} uL with margin - exceeds "
                f"one well) -> fill in order until the volume is used up, in {src}: {per_well}"
            )
        backup_plan = backup_fill_plans.get(reagent)
        if backup_plan:
            backup_total = round(sum(v for _, v in backup_plan), 1)
            per_well_backup = "; ".join(f"{w} = {v} uL" for w, v in backup_plan)
            quantity_notes.append(
                f"{reagent}: backup - only used if the well(s) above run dry mid-run - "
                f"{backup_total} uL more in {src}: {per_well_backup}"
            )

    num_sources_used = len(set(reagent_to_source_map.values()))

    requirements["TargetPlate"] = "TargetPlate"
    requirements["MyTrash"] = "MyTrash"
    # Tip rack requirement(s) are added after `steps` is built below (see
    # "Tip rack requirements" near the end of this function) - with
    # tip_sizes_ul configured, Steps can reference several distinct racks
    # (MyTips_50, MyTips_1000, ...) instead of always just "MyTips", and the
    # Requirements section must declare exactly the ones actually used or
    # the HAL match fails at run time ("Requirement 'MyTips' -> 'MyTips' not
    # found in hardware config") even though the HAL does have the right
    # rack(s), just under their real names.

    # Build Steps with delays between groups
    steps = []

    # Module treatments (heater-shaker, temperature module, ...) inserted via the
    # generic "+ Add step" mechanism. `current_location` tracks where each target
    # (TargetPlate / Source_N) physically is right now, so a treatment that leaves
    # a labware on a module (return_after=False) is picked up correctly by a later
    # treatment on the same target instead of re-issuing a redundant MoveLabware.
    modules_used: Dict[str, str] = {}
    current_location: Dict[str, str] = {}

    all_treatments = list(config.get('pre_treatments', []))
    for g in groups:
        all_treatments.extend(g.get('treatments', []))
        for batch_cfg in g.get('reagent_batches', {}).values():
            if batch_cfg.get('treatment'):
                all_treatments.append(batch_cfg['treatment'])
    all_treatments.extend(config.get('post_treatments', []))
    home_slots = _compute_home_slots(all_treatments, num_sources_used)

    def emit(treatment):
        _emit_treatment(treatment, steps, requirements, modules_used, current_location, reagent_to_source_map, home_slots)

    for treatment in config.get('pre_treatments', []):
        emit(treatment)

    for group_idx, group in enumerate(groups):
        group_name = group.get('name', f'Group_{group_idx+1}')
        columns = group.get('columns', [])
        delay_seconds = group.get('delay_after_seconds', 0)
        
        if not columns:
            continue
        
        # Add group comment
        steps.append({
            "Command": "Comment",
            "Text": f"=== {group_name}: {', '.join(columns)} ==="
        })
        
        # Process each reagent in this group
        for reagent in columns:
            # Use the generic source from the mapping
            source_name = reagent_to_source_map.get(reagent, f"Source_{reagent}")
            well = reagent_to_well_map.get(reagent, 'A1')

            # Get LiquidClass for this reagent (if specified)
            # Config can have: reagent_liquid_classes: {"HA": "Aqueous", "DMTMM": "Viscous", ...}
            reagent_liquid_classes = config.get('reagent_liquid_classes', {})
            liquid_class = reagent_liquid_classes.get(reagent, None)

            # Optional per-reagent advanced liquid-handling overrides (air gap, blow
            # out, mix after dispense). These are just extra fields merged onto the
            # SAME Distribute/Transfer command already built below - Opentrons'
            # transfer()/distribute() already implement the correct aspirate -> air
            # gap -> move -> dispense sequencing internally, so there's no need for a
            # separate "DistributeWithAirGap" pseudo-command.
            adv = config.get('reagent_advanced', {}).get(reagent, {})

            # Collect transfers for this reagent
            transfers = []
            for well_data in wells:
                vol = well_data['reagents'].get(reagent, 0)
                if vol > 0:
                    transfers.append({
                        'well': well_data['well'],
                        'volume': round(vol, 2)
                    })

            if not transfers:
                continue

            pipette = config.get('pipette', 'left')
            strategy = group.get('strategy', 'smart')  # 'smart'/'distribute' both -> Distribute; 'transfer' -> Transfer

            tip_sizes_ul = config.get('tip_sizes_ul')

            def _resolve_tip(tip_size):
                """(reagent_tip_rack, pipette_max, pipette_min) for a tip
                partition's size - `None` is the "no tip_sizes_ul configured"
                sentinel (today's exact behavior: always "MyTips", global
                pipette_max_volume/pipette_min_volume)."""
                if tip_size is None:
                    return "MyTips", pipette_max_volume, pipette_min_volume
                rack = "MyTips" if len(tip_sizes_ul) == 1 else f"MyTips_{tip_size}"
                return rack, FLEX_TIP_RANGES[tip_size]["max"], FLEX_TIP_RANGES[tip_size]["min"]

            verify_liquid = bool(adv.get('verify_liquid'))
            backup_wells = reagent_to_backup_well_map.get(reagent, [])
            # Optional per-reagent batching: split this reagent's own destination wells
            # into chunks with a module treatment between them (e.g. periodically
            # reheating a reagent on the source reservoir so it doesn't solidify while
            # sitting on deck during a long 96-well distribution - see the hand-authored
            # SPAAC reference recipe for the pattern this reproduces).
            batch_cfg = group.get('reagent_batches', {}).get(reagent)
            batch_size = batch_cfg.get('batch_size') if batch_cfg else None

            # Reservoir-well/batch chunking is computed ONCE here, on the
            # FULL, un-partitioned `transfers` - exactly as before tip-size
            # selection existed, and deliberately independent of it. This is
            # what makes multi-tip-size support safe to combine with a
            # reagent needing more than one physical reservoir well: well
            # boundaries are decided first, tip size is chosen SEPARATELY
            # and only WITHIN each already-fixed well boundary below (both
            # the "transfer" and plain "Distribute" branches sub-partition
            # each chunk's own transfers by tip afterwards) - so two
            # different tip-size groups can never independently believe
            # they can both draw a full well's capacity from the same
            # physical well.
            if isinstance(well, list):
                well_capacity = get_reservoir_well_capacity(reservoir_type, well[0]) or float('inf')
                chunks = _bucket_transfers_by_capacity(transfers, well_capacity)
                chunk_wells = well[:len(chunks)] if len(well) >= len(chunks) else well + [well[-1]] * (len(chunks) - len(well))
            else:
                chunks = [transfers[i:i + batch_size] for i in range(0, len(transfers), batch_size)] if batch_size else [transfers]
                chunk_wells = [well] * len(chunks)

            if strategy == 'transfer':
                # 1 aspirate -> 1 dispense per well, fresh tip every time (safer, slower).
                # Transfer only accepts a single Dest per step (unlike Distribute), so
                # each well needs its own PickUpTip/Transfer/DropTip triplet. Already a
                # fresh tip on every well, so - unlike Distribute below - no extra
                # per-trip splitting is needed for "Verify aspiration" to fire on every
                # well; only the runtime fallback (Source as a candidate list) applies.
                for chunk, chunk_well in zip(chunks, chunk_wells):
                    if batch_cfg and batch_cfg.get('treatment'):
                        emit(batch_cfg['treatment'])
                    if verify_liquid and backup_wells:
                        source_field = [f"{source_name}:{chunk_well}"] + [f"{source_name}:{w}" for w in backup_wells]
                    else:
                        source_field = f"{source_name}:{chunk_well}"
                    # Tip choice per individual well - already the finest
                    # granularity, no interaction with the well boundary above.
                    tip_groups, unfit = _partition_transfers_by_tip(chunk, tip_sizes_ul)
                    for t in unfit:
                        quantity_notes.append(
                            f"{reagent}: well {t['well']} needs {t['volume']} uL but no configured tip "
                            f"size {tip_sizes_ul} covers it - that well is skipped, add a suitable tip size."
                        )
                    for tip_size, tip_chunk in tip_groups:
                        reagent_tip_rack, _, _ = _resolve_tip(tip_size)
                        for t in tip_chunk:
                            steps.append({"Command": "PickUpTip", "PipetteMount": pipette, "TipRack": reagent_tip_rack})
                            transfer_cmd = {
                                "Command": "Transfer",
                                "PipetteMount": pipette,
                                "Volume": t['volume'],
                                "Source": source_field,
                                "Dest": f"TargetPlate:{t['well']}",
                                "NewTip": "never",
                                "Description": f"Transfer {t['volume']}µL {reagent} to {t['well']}"
                            }
                            if liquid_class:
                                transfer_cmd["LiquidClass"] = liquid_class
                            _apply_advanced_liquid_options(transfer_cmd, adv)
                            steps.append(transfer_cmd)
                            steps.append({"Command": "DropTip", "PipetteMount": pipette, "TrashLocation": "MyTrash"})

            elif verify_liquid:
                # "Verify aspiration" needs a fresh, probe-eligible tip on every
                # reload - Opentrons only re-arms its native liquid-presence probe
                # on a clean tip (see _split_into_trips docstring), and a plain
                # Distribute keeps ONE tip for the whole reagent. So: one
                # PickUpTip/Distribute/DropTip PER TRIP instead of per reagent.
                # The actual source well is then chosen at runtime in
                # protocol_generator.py (detect_liquid_presence() per candidate),
                # so the reservoir-capacity chunking above (chunks/chunk_wells)
                # is skipped here in favor of a flat trip split across ALL this
                # reagent's destinations - safe to partition by tip OUTSIDE any
                # well boundary too, since source-well selection is fully
                # dynamic at runtime here, not pre-assigned to a chunk.
                tip_groups, unfit = _partition_transfers_by_tip(transfers, tip_sizes_ul)
                for t in unfit:
                    quantity_notes.append(
                        f"{reagent}: well {t['well']} needs {t['volume']} uL but no configured tip "
                        f"size {tip_sizes_ul} covers it - that well is skipped, add a suitable tip size."
                    )
                for tip_size, tip_transfers in tip_groups:
                    reagent_tip_rack, reagent_pipette_max, reagent_pipette_min = _resolve_tip(tip_size)
                    all_volumes = [t['volume'] for t in tip_transfers]
                    all_dest_wells = [t['well'] for t in tip_transfers]
                    trips = _split_into_trips(all_volumes, all_dest_wells, reagent_pipette_max, reagent_pipette_min)

                    primary_wells = well if isinstance(well, list) else [well]
                    candidate_wells = primary_wells + backup_wells
                    source_field = (
                        f"{source_name}:{candidate_wells[0]}" if len(candidate_wells) == 1
                        else [f"{source_name}:{w}" for w in candidate_wells]
                    )

                    reagent_waste_ul[reagent] = reagent_waste_ul.get(reagent, 0.0) + _estimate_distribute_waste(
                        all_volumes, reagent_pipette_max, reagent_pipette_min
                    )

                    for volumes_list, dest_wells in trips:
                        if batch_cfg and batch_cfg.get('treatment'):
                            emit(batch_cfg['treatment'])

                        unique_volumes = set(volumes_list)
                        if len(unique_volumes) == 1:
                            volume_param = volumes_list[0]
                            description = f"Distribute {volume_param}µL {reagent} to {len(dest_wells)} wells"
                        else:
                            volume_param = volumes_list
                            description = f"Distribute {min(volumes_list)}-{max(volumes_list)}µL {reagent} to {len(dest_wells)} wells"

                        steps.append({"Command": "PickUpTip", "PipetteMount": pipette, "TipRack": reagent_tip_rack})
                        distribute_cmd = {
                            "Command": "Distribute",
                            "PipetteMount": pipette,
                            "Volume": volume_param,
                            "Source": source_field,
                            "Destinations": [f"TargetPlate:{w}" for w in dest_wells],
                            "NewTip": "never",
                            "DisposalVolume": 0,
                            "Description": description
                        }
                        if liquid_class:
                            distribute_cmd["LiquidClass"] = liquid_class
                        _apply_advanced_liquid_options(distribute_cmd, adv)
                        steps.append(distribute_cmd)
                        steps.append({"Command": "DropTip", "PipetteMount": pipette, "TrashLocation": "MyTrash"})

            else:
                # Distribute: one aspirate -> many dispenses. Tip is chosen PER
                # (reservoir-well chunk x tip-size) pair, but a tip is only
                # actually swapped when the SIZE changes - consecutive chunks
                # needing the SAME size (the common no-tip_sizes_ul case: every
                # chunk resolves to "MyTips") still share one continuous
                # PickUpTip...DropTip span across chunks, exactly as before
                # multi-tip-size support existed, instead of picking up a new
                # tip once per reservoir-well chunk regardless of size.
                distribute_units = []  # (reagent_tip_rack, chunk_well, batch_treatment, volumes_list, dest_wells, max, min)
                for chunk, chunk_well in zip(chunks, chunk_wells):
                    tip_groups, unfit = _partition_transfers_by_tip(chunk, tip_sizes_ul)
                    for t in unfit:
                        quantity_notes.append(
                            f"{reagent}: well {t['well']} needs {t['volume']} uL but no configured tip "
                            f"size {tip_sizes_ul} covers it - that well is skipped, add a suitable tip size."
                        )
                    chunk_treatment_pending = bool(batch_cfg and batch_cfg.get('treatment'))
                    for tip_size, tip_chunk in tip_groups:
                        reagent_tip_rack, reagent_pipette_max, reagent_pipette_min = _resolve_tip(tip_size)
                        distribute_units.append((
                            reagent_tip_rack, chunk_well, chunk_treatment_pending,
                            [t['volume'] for t in tip_chunk], [t['well'] for t in tip_chunk],
                            reagent_pipette_max, reagent_pipette_min,
                        ))
                        chunk_treatment_pending = False  # only emit once, before this chunk's first unit

                for i, (reagent_tip_rack, chunk_well, treatment_pending, volumes_list, dest_wells,
                        reagent_pipette_max, reagent_pipette_min) in enumerate(distribute_units):
                    tip_changing = i == 0 or distribute_units[i - 1][0] != reagent_tip_rack
                    tip_ending = i == len(distribute_units) - 1 or distribute_units[i + 1][0] != reagent_tip_rack

                    if treatment_pending:
                        emit(batch_cfg['treatment'])

                    if tip_changing:
                        steps.append({"Command": "PickUpTip", "PipetteMount": pipette, "TipRack": reagent_tip_rack})

                    unique_volumes = set(volumes_list)
                    reagent_waste_ul[reagent] = reagent_waste_ul.get(reagent, 0.0) + _estimate_distribute_waste(
                        volumes_list, reagent_pipette_max, reagent_pipette_min
                    )

                    if len(unique_volumes) == 1:
                        volume_param = volumes_list[0]
                        description = f"Distribute {volume_param}µL {reagent} to {len(dest_wells)} wells"
                    else:
                        volume_param = volumes_list
                        description = f"Distribute {min(volumes_list)}-{max(volumes_list)}µL {reagent} to {len(dest_wells)} wells"

                    distribute_cmd = {
                        "Command": "Distribute",
                        "PipetteMount": pipette,
                        "Volume": volume_param,
                        "Source": f"{source_name}:{chunk_well}",
                        "Destinations": [f"TargetPlate:{w}" for w in dest_wells],
                        "NewTip": "never",
                        "DisposalVolume": 0,
                        "Description": description
                    }
                    if liquid_class:
                        distribute_cmd["LiquidClass"] = liquid_class
                    _apply_advanced_liquid_options(distribute_cmd, adv)
                    steps.append(distribute_cmd)

                    if tip_ending:
                        steps.append({"Command": "DropTip", "PipetteMount": pipette, "TrashLocation": "MyTrash"})

        # Module treatments configured for this phase (heater-shaker, temperature
        # module, ...) - run after this phase's pipetting, before the numeric delay.
        for treatment in group.get('treatments', []):
            emit(treatment)

        # Add delay AFTER this group (if delay > 0)
        if delay_seconds > 0:
            delay_minutes = delay_seconds / 60
            steps.append({
                "Command": "Delay",
                "Seconds": delay_seconds,
                "Description": f"Wait {delay_minutes:.1f} minutes after {group_name}"
            })

    for treatment in config.get('post_treatments', []):
        emit(treatment)

    # Check if any delays were added
    has_delays = any(g.get('delay_after_seconds', 0) > 0 for g in groups)

    # Waste (Distribute's disposal volume) stays in required_volumes as
    # structured data for anything that wants it, but is deliberately NOT
    # added to quantity_notes - it's already folded into with_excess_ul via
    # dead_volume_ul, and operators only need the one "load this much" total
    # in the notes panel, not a separate waste breakdown to interpret.
    total_waste_ul = 0.0
    for reagent, waste in reagent_waste_ul.items():
        if waste <= 0:
            continue
        total_waste_ul += waste
        if reagent in required_volumes:
            required_volumes[reagent]['estimated_waste_ul'] = round(waste, 1)

    # Includes backup/fallback wells too (not just the primary/overflow ones)
    # so plate_tracking.py's reverse lookup (source_key:well -> reagent) can
    # resolve a well no matter which candidate the run actually drew from.
    well_assignments = {
        reagent: f"{reagent_to_source_map.get(reagent, '?')}:" + '+'.join(
            (w if isinstance(w, list) else [w]) + reagent_to_backup_well_map.get(reagent, [])
        )
        for reagent, w in reagent_to_well_map.items()
    }

    # Tip rack requirements - derived from the TipRack each PickUpTip step
    # actually names, not from tip_sizes_ul directly, so this stays correct
    # even if a requested size never fits any well and never gets a step
    # (see _partition_transfers_by_tip's "unfit" bucket). Falls back to the
    # single legacy "MyTips" requirement if, for whatever reason, no
    # PickUpTip step exists at all - matching the old unconditional default.
    tip_racks_used = sorted({s["TipRack"] for s in steps if s.get("Command") == "PickUpTip" and s.get("TipRack")})
    for rack in tip_racks_used or ["MyTips"]:
        requirements[rack] = rack

    # Build recipe
    recipe = {
        'ProtocolName': recipe_name,
        'Description': f"Generated from Excel. Groups: {', '.join([g.get('name', '?') for g in groups])}",
        'Requirements': requirements,
        'Steps': steps,
        'Metadata': {
            'Generated': timestamp,
            'Source': 'excel_import_configurable',
            'TotalWells': len(wells),
            'PlateId': config.get('target_plate', ''),
            'PlateRows': config.get('plate_rows', 8),
            'PlateColumns': config.get('plate_columns', 12),
            'PlateTotalWells': config.get('plate_total_wells', 96),
            'Reagents': all_reagents_in_config,
            'Groups': [{'name': g.get('name'), 'columns': g.get('columns'), 'delay_seconds': g.get('delay_after_seconds', 0)} for g in groups],
            'ReagentMapping': reagent_to_source_map,
            'WellAssignments': well_assignments,
            'RequiredVolumes': required_volumes,
            'Notes': quantity_notes,
            'HasDelays': has_delays,
            'ModulesUsed': modules_used
        },
        'Configuration': config
    }

    return recipe


def generate_default_config(sheet_data: Dict) -> Dict:
    """
    Generate a default configuration with all reagents in one group (no delays).
    User can modify this in the UI.
    """
    reagents = sheet_data.get('summary', {}).get('reagents', [])
    
    return {
        'groups': [
            {
                'name': 'All Reagents',
                'columns': reagents,
                'delay_after_seconds': 0
            }
        ],
        'target_plate': 'corning_96_wellplate_360ul_flat',
        'reservoir_type': 'nest_12_reservoir_15ml',
        'pipette': 'left'
    }


def save_recipe_to_library(recipe: Dict, library_path: Path) -> str:
    """Save generated recipe to the Library/Recipes folder."""
    import json
    
    recipes_dir = library_path / "Recipes"
    recipes_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = recipe.get('Metadata', {}).get('Generated', datetime.now().strftime('%Y%m%d_%H%M%S'))
    filename = f"{recipe['ProtocolName']}_{timestamp}.json"
    filepath = recipes_dir / filename
    
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(recipe, f, indent=2, ensure_ascii=False)
    
    return filename


def generate_hal_config_from_recipe(recipe: Dict, hal_name: Optional[str] = None) -> Dict:
    """
    Generate a GENERIC HAL configuration that can be reused across recipes.
    Uses numbered sources (Source_1, Source_2, ...) instead of reagent-specific names.
    The recipe's Metadata.ReagentMapping tells which reagent goes where.
    """
    # Reuse the recipe's OWN "Generated" timestamp (set once, when the recipe
    # itself was built) instead of computing a fresh datetime.now() here -
    # otherwise the two calls land a few seconds apart and the recipe/HAL
    # pair gets saved under two different timestamp suffixes, making it look
    # like they don't belong together even though they do.
    timestamp = recipe.get('Metadata', {}).get('Generated') or datetime.now().strftime('%Y%m%d_%H%M%S')

    if not hal_name:
        hal_name = "Generic_HAL"
    
    # Count how many sources are needed
    num_sources = 0
    for key in recipe.get('Requirements', {}):
        if key.startswith('Source_'):
            num_sources += 1
    
    # Modules actually used by the recipe's treatments (heater-shaker, temperature
    # module, ...) get dedicated slots first, from a pool disjoint from the
    # reservoir/tip/target slots below - the common case is a single HeaterShaker
    # at A1 (existing convention), but this scales to a couple of extra modules.
    modules_used = recipe.get('Metadata', {}).get('ModulesUsed', {})  # {requirement_key: module_type}
    modules_section = {}
    reserved_module_slots = []
    for idx, (req_key, module_type) in enumerate(modules_used.items()):
        if idx >= len(MODULE_SLOT_POOL):
            break  # out of deck slots for additional modules
        slot = MODULE_SLOT_POOL[idx]
        modules_section[req_key] = {'Type': module_type, 'Slot': slot}
        reserved_module_slots.append(slot)

    # Slot assignments for reservoirs
    # Priority: A2 → B2 → C2 → D3 → D1 → C1 → B1 → C3
    # A1=HeaterShaker (or other module), A3=Trash, D2=TargetPlate
    available_reservoir_slots = [s for s in RESERVOIR_SLOT_POOL if s not in reserved_module_slots]
    # Tip racks may also claim A1 when no module needs it (see
    # GENERIC_TIP_SLOT_PREFERENCE) - offer it to the reservoir pool too so a
    # recipe with no modules doesn't leave that slot permanently empty.
    if 'A1' not in reserved_module_slots and 'A1' not in available_reservoir_slots:
        available_reservoir_slots.append('A1')

    labware = {}
    slot_assignments = {}
    config = recipe.get('Configuration', {})

    # Tip racks - one physical slot per requested size, claimed from
    # GENERIC_TIP_SLOT_PREFERENCE minus whatever a module already reserved
    # (A1 stays module territory here when one is configured, unlike the
    # module-free generate_generic_hal()). Not capped at 2 - only at how
    # many free deck slots actually remain. One tip size (default, matches
    # config['tip_sizes_ul'] used by generate_recipe_with_config() for the
    # same recipe): first slot = primary, second slot = backup of the same
    # size. Two or more sizes: each gets its own single rack, no backup -
    # same tradeoff as generate_generic_hal()'s matching tip_sizes_ul
    # handling, kept consistent between the two HAL generators since a
    # recipe can be paired with either.
    tip_slot_pool = [s for s in GENERIC_TIP_SLOT_PREFERENCE if s not in reserved_module_slots]
    # Dedupe (preserving order) first - see the matching note in generate_generic_hal().
    tip_sizes = list(dict.fromkeys(config.get('tip_sizes_ul') or [1000]))
    tip_sizes = [s for s in tip_sizes if s in FLEX_TIP_RANGES] or [1000]
    if len(tip_sizes) > len(tip_slot_pool):
        tip_sizes = tip_sizes[:len(tip_slot_pool)]
    if len(tip_sizes) == 1:
        load_name = FLEX_TIP_RANGES[tip_sizes[0]]['load_name']
        slot, backup_slot = tip_slot_pool[0], tip_slot_pool[1]
        labware['MyTips'] = {'LoadName': load_name, 'Slot': slot, 'DisplayName': f'Tip rack {tip_sizes[0]}µL'}
        slot_assignments[slot] = 'MyTips'
        labware['MyTips_2'] = {'LoadName': load_name, 'Slot': backup_slot, 'DisplayName': f'Tip rack {tip_sizes[0]}µL (backup)'}
        slot_assignments[backup_slot] = 'MyTips_2'
    else:
        for size, slot in zip(tip_sizes, tip_slot_pool):
            load_name = FLEX_TIP_RANGES[size]['load_name']
            key = f'MyTips_{size}'
            labware[key] = {'LoadName': load_name, 'Slot': slot, 'DisplayName': f'Tip rack {size}µL'}
            slot_assignments[slot] = key

    # Whatever slot(s) tip racks claimed above are removed from the
    # reservoir pool now so a source-heavy recipe can't try to claim the
    # same physical slot.
    available_reservoir_slots = [s for s in available_reservoir_slots if s not in slot_assignments]

    # Target plate
    target_plate_type = config.get('target_plate', 'corning_96_wellplate_360ul_flat')
    
    labware['TargetPlate'] = {
        'LoadName': target_plate_type,
        'Slot': TARGET_PLATE_SLOT,
        'DisplayName': 'Target plate for reactions'
    }
    slot_assignments[TARGET_PLATE_SLOT] = 'TargetPlate'
    
    # Generic numbered reservoirs (Source_1, Source_2, ...)
    reservoir_type = config.get('reservoir_type', 'nest_12_reservoir_15ml')
    
    # Get reagent mapping if available (for display names)
    reagent_mapping = recipe.get('Metadata', {}).get('ReagentMapping', {})
    source_to_reagent = {v: k for k, v in reagent_mapping.items()}  # Reverse: Source_1 -> HA
    
    for i in range(num_sources):
        if i >= len(available_reservoir_slots):
            break
        
        slot = available_reservoir_slots[i]
        source_key = f'Source_{i+1}'
        reagent_hint = source_to_reagent.get(source_key, f'Reagent {i+1}')
        
        labware[source_key] = {
            'LoadName': reservoir_type,
            'Slot': slot,
            'DisplayName': f'{source_key} (e.g. {reagent_hint})',
            'Well': 'A1'
        }
        slot_assignments[slot] = source_key
    
    # Use ConfigName for compatibility with existing HAL format
    return {
        'ConfigName': hal_name,
        'Description': f'Generic HAL with {num_sources} source slots. Reusable for multiple recipes.',
        'Generated': timestamp,
        'Trash': {
            'MyTrash': {'Type': 'TrashBin', 'Slot': 'A3'}
        },
        'Modules': modules_section,
        'Labware': labware,
        'Pipettes': {
            'left': 'flex_1channel_1000'
        },
        'Metadata': {
            'source': 'excel_recipe_configurable',
            'total_sources': num_sources,
            'slot_assignments': slot_assignments,
            'note': 'This HAL uses generic source names. Check recipe metadata for reagent assignments.'
        }
    }


def generate_generic_hal(
    num_sources: int = 6,
    hal_name: str = "Generic_HAL",
    target_plate: str = "corning_96_wellplate_360ul_flat",
    reservoir_type: str = "nest_12_reservoir_15ml",
    pipette: str = "flex_1channel_1000",
    tip_sizes_ul: Optional[List[int]] = None
) -> Dict:
    """
    Generate a completely generic HAL configuration with numbered source slots.

    This HAL can be reused for ANY recipe - just put the reagents in corresponding Source_N positions.

    Args:
        num_sources: Number of source reservoirs (Source_1 to Source_N)
        hal_name: Name for the HAL configuration
        target_plate: Labware type for target plate
        reservoir_type: Labware type for reagent reservoirs
        pipette: Pipette name for left mount
        tip_sizes_ul: Which Flex tip sizes to physically load, e.g. [1000, 50]
            (see FLEX_TIP_RANGES for the valid catalog). Default [1000] keeps
            today's behavior (one size, primary + backup rack of the same
            size). With 2+ sizes, each size gets its own single rack (no
            backup) on the next free slot from GENERIC_TIP_SLOT_PREFERENCE -
            not capped at 2, only at how many free deck slots actually exist
            once trash and the target plate are placed. Requesting more
            sizes than that is capped at that true ceiling, with a note in
            the returned dict's Metadata so the caller can surface it,
            rather than silently dropping sizes with no trace.

    Returns:
        HAL configuration dict compatible with opentrons_client.py
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    # Available slots for reservoirs
    # Priority: A2 → B2 → C2 → D3 → D1 → C1 → B1 → A1 → C3
    # Whichever of these a tip rack claims below is removed from this pool -
    # otherwise a recipe with enough sources would silently double-book that
    # slot (a real physical collision the robot can't resolve).
    available_reservoir_slots = ['A2', 'B2', 'C2', 'D3', 'D1', 'C1', 'B1', 'A1', 'C3']

    labware = {}
    slot_assignments = {}
    tip_metadata_notes: List[str] = []

    # Tip racks - one physical slot per requested size, claimed from
    # GENERIC_TIP_SLOT_PREFERENCE (see its definition for the ordering
    # rationale). One requested size (default): first slot = primary,
    # second slot = backup of the same size, matching the original
    # single-size behavior exactly. Two or more requested sizes: each gets
    # its own single rack, no backup - trades tip-refill redundancy for
    # actually having every requested size on the deck, which is the point
    # of this feature.
    # Dedupe (preserving order) - a repeated size would otherwise consume a
    # slot per occurrence in slot_assignments while collapsing onto a single
    # Labware dict key, leaving earlier slots "reserved" for a key that no
    # longer points at them.
    sizes = list(dict.fromkeys(tip_sizes_ul)) if tip_sizes_ul else [1000]
    unknown_sizes = [s for s in sizes if s not in FLEX_TIP_RANGES]
    if unknown_sizes:
        tip_metadata_notes.append(
            f"Requested tip size(s) {unknown_sizes} not in the known Flex tip "
            f"catalog ({sorted(FLEX_TIP_RANGES.keys())}) - ignored."
        )
    sizes = [s for s in sizes if s in FLEX_TIP_RANGES]
    if not sizes:
        sizes = [1000]
    max_tip_slots = len(GENERIC_TIP_SLOT_PREFERENCE)
    if len(sizes) > max_tip_slots:
        tip_metadata_notes.append(
            f"Requested {len(sizes)} tip sizes but only {max_tip_slots} free "
            f"deck slots remain once trash and the target plate are placed - "
            f"using the first {max_tip_slots}: {sizes[:max_tip_slots]}."
        )
        sizes = sizes[:max_tip_slots]

    if len(sizes) == 1:
        load_name = FLEX_TIP_RANGES[sizes[0]]["load_name"]
        slot, backup_slot = GENERIC_TIP_SLOT_PREFERENCE[0], GENERIC_TIP_SLOT_PREFERENCE[1]
        labware['MyTips'] = {'LoadName': load_name, 'Slot': slot, 'DisplayName': f'Tip rack {sizes[0]}µL'}
        slot_assignments[slot] = 'MyTips'
        labware['MyTips_2'] = {'LoadName': load_name, 'Slot': backup_slot, 'DisplayName': f'Tip rack {sizes[0]}µL (backup)'}
        slot_assignments[backup_slot] = 'MyTips_2'
    else:
        for size, slot in zip(sizes, GENERIC_TIP_SLOT_PREFERENCE):
            load_name = FLEX_TIP_RANGES[size]["load_name"]
            key = f'MyTips_{size}'
            labware[key] = {'LoadName': load_name, 'Slot': slot, 'DisplayName': f'Tip rack {size}µL'}
            slot_assignments[slot] = key

    # Whatever slot(s) tip racks claimed above are removed from the
    # reservoir pool now so a source-heavy recipe can't try to claim the
    # same physical slot.
    reservoir_pool = [s for s in available_reservoir_slots if s not in slot_assignments]
    max_sources = min(num_sources, len(reservoir_pool))

    # Target plate
    labware['TargetPlate'] = {
        'LoadName': target_plate,
        'Slot': 'D2',
        'DisplayName': 'Target plate'
    }
    slot_assignments['D2'] = 'TargetPlate'
    
    # Generic numbered reservoirs
    for i in range(max_sources):
        slot = reservoir_pool[i]
        source_key = f'Source_{i+1}'
        
        labware[source_key] = {
            'LoadName': reservoir_type,
            'Slot': slot,
            'DisplayName': f'{source_key} - Reagent reservoir',
            'Well': 'A1'
        }
        slot_assignments[slot] = source_key
    
    return {
        'ConfigName': hal_name,
        'Description': f'Generic reusable HAL with {max_sources} source slots (Source_1 to Source_{max_sources}). Works with any recipe using numbered sources.',
        'Generated': timestamp,
        'Trash': {
            'MyTrash': {'Type': 'TrashBin', 'Slot': 'A3'}
        },
        'Labware': labware,
        'Pipettes': {
            'left': pipette
        },
        'Metadata': {
            'source': 'generic_hal_generator',
            'total_sources': max_sources,
            'slot_assignments': slot_assignments,
            'tip_sizes_ul': sizes,
            'note': 'Load reagents into Source_1, Source_2, etc. Check recipe metadata for reagent order.',
            'tip_notes': tip_metadata_notes,
        }
    }


def save_hal_config_to_library(hal_config: Dict, library_path: Path) -> str:
    """Save generated HAL config to the Library/HardwareConfig folder."""
    import json
    
    hal_dir = library_path / "HardwareConfig"
    hal_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = hal_config.get('Generated', datetime.now().strftime('%Y%m%d_%H%M%S'))
    config_name = hal_config.get('ConfigName') or hal_config.get('HALName', 'HAL_Config')
    filename = f"{config_name}_{timestamp}.json"
    filepath = hal_dir / filename
    
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(hal_config, f, indent=2, ensure_ascii=False)
    
    return filename


# Workflow generation (for multi-station orchestration)
def generate_workflow_from_recipes(recipes: List[Dict], workflow_name: Optional[str] = None) -> Dict:
    """
    Generate a workflow from one or more recipes.
    
    Recipes can have a '_delay_after' attribute specifying delay in seconds
    before the next recipe. Wait steps are inserted between recipes.
    """
    from datetime import datetime
    
    if not recipes:
        return {'error': 'No recipes provided'}
    
    first_recipe = recipes[0]
    name = workflow_name or f"Workflow_{first_recipe.get('ProtocolName', 'Recipe')}"
    
    steps = []
    step_id = 1
    total_delay = 0
    
    for i, recipe in enumerate(recipes):
        # Add Opentrons execution step
        steps.append({
            'id': step_id,
            'type': 'opentrons_protocol',
            'name': f"Execute {recipe.get('ProtocolName', 'Recipe')}",
            'server': 'opentrons',
            'command': 'RunRecipe',
            'parameters': {
                'recipe': recipe.get('ProtocolName'),
                'hal_config': recipe.get('HALFile', 'Standard_Flex_Setup')
            },
            'timeout_minutes': 60
        })
        step_id += 1
        
        # Check for delay after this recipe
        delay_seconds = recipe.get('_delay_after', 0)
        if delay_seconds > 0 and i < len(recipes) - 1:  # Don't add delay after last recipe
            total_delay += delay_seconds
            delay_minutes = delay_seconds // 60
            delay_secs = delay_seconds % 60
            delay_str = f"{delay_minutes}min {delay_secs}s" if delay_secs else f"{delay_minutes} minutes"
            
            # Add wait step
            steps.append({
                'id': step_id,
                'type': 'wait',
                'name': f"Wait {delay_str}",
                'server': None,
                'command': 'Wait',
                'parameters': {
                    'duration_seconds': delay_seconds,
                    'message': f"Incubation period: {delay_str}"
                },
                'timeout_minutes': (delay_seconds // 60) + 5
            })
            step_id += 1
            
            # Add extension point for optional Tecan measurement during wait
            steps.append({
                'id': step_id,
                'type': 'extension_point',
                'name': f"Optional: Tecan measurement during incubation",
                'server': 'tecan',
                'command': None,
                'parameters': {},
                'description': 'Add Tecan commands here to measure during incubation'
            })
            step_id += 1
    
    # Final extension point for Tecan analysis
    steps.append({
        'id': step_id,
        'type': 'extension_point',
        'name': 'Final Analysis (optional)',
        'server': 'tecan',
        'command': None,
        'parameters': {},
        'description': 'Add final Tecan analysis commands here'
    })
    
    return {
        'name': name,
        'version': '1.0',
        'created': datetime.now().isoformat(),
        'description': f'Generated workflow with {len(recipes)} recipe(s), total wait time: {total_delay//60} min',
        'steps': steps,
        'metadata': {
            'total_recipes': len(recipes),
            'total_wait_seconds': total_delay,
            'source': 'excel_recipe_parser'
        }
    }


def save_workflow_to_library(workflow: Dict, library_path: Path) -> str:
    """Save workflow JSON to Library/Workflows."""
    workflows_dir = Path(library_path) / "Workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    
    filename = f"{workflow['name']}.json"
    filepath = workflows_dir / filename
    
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(workflow, f, indent=2)
    
    return filename


# Legacy compatibility - wrapper for old API
def parse_excel_recipe(file_content: bytes, filename: str = "recipe.xlsx") -> Dict[str, Any]:
    """
    Legacy function for backward compatibility.
    Parses Excel and generates recipes with default config (no delays).
    """
    result = parse_excel_columns(file_content, filename)
    
    if 'error' in result:
        return result
    
    all_recipes = []
    
    for sheet_data in result.get('sheets', []):
        if 'error' in sheet_data:
            continue
        
        # Use default config (all reagents, no delays)
        config = generate_default_config(sheet_data)
        recipe = generate_recipe_with_config(sheet_data, config)
        
        if 'error' not in recipe:
            all_recipes.append(recipe)
    
    return {
        'filename': filename,
        'sheets': result.get('sheets', []),
        'recipes': all_recipes,
        'summary': {
            **result.get('summary', {}),
            'total_recipes': len(all_recipes)
        }
    }


# For command-line testing
if __name__ == "__main__":
    import sys
    import json
    import logging
    
    if len(sys.argv) < 2:
        print("Usage: python excel_recipe_parser.py <excel_file>")
        sys.exit(1)
    
    filepath = Path(sys.argv[1])
    
    with open(filepath, 'rb') as f:
        content = f.read()
    
    result = parse_excel_columns(content, filepath.name)
    logging.basicConfig(level=logging.INFO)
    _log = logging.getLogger(__name__)
    _log.info("=== Parsed Columns ===")
    _log.info(json.dumps(result, indent=2, default=str))
    
    # Generate with custom config example
    if result.get('sheets') and 'error' not in result['sheets'][0]:
        sheet = result['sheets'][0]
        config = {
            'groups': [
                {
                    'name': 'Phase A',
                    'columns': sheet['summary']['reagents'][:2] if len(sheet['summary']['reagents']) >= 2 else sheet['summary']['reagents'],
                    'delay_after_seconds': 1800
                },
                {
                    'name': 'Phase B',
                    'columns': sheet['summary']['reagents'][2:] if len(sheet['summary']['reagents']) > 2 else [],
                    'delay_after_seconds': 0
                }
            ],
            'pipette': 'left'
        }
        
        recipe = generate_recipe_with_config(sheet, config, 'Test_Recipe')
        _log.info("=== Generated Recipe ===")
        _log.info(json.dumps(recipe, indent=2, default=str))
