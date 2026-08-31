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
            load_name = data.get("parameters", {}).get("loadName", f.stem)
            _RESERVOIR_CACHE[load_name] = data
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
    """Max liquid volume (uL) of a single well in a reservoir labware, or None if unknown."""
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
        "label": "Heater-Shaker (scalda / agita)",
        "module_type": "heaterShakerModuleV1",
        "requirement_key": "HeaterShaker",
        "command": "HeaterShaker",
        # The labware latch is not a user preference: Opentrons refuses to shake
        # with it open, and the gripper can't lift labware while it's clamped shut.
        # Both are derived automatically (see _emit_treatment) instead of exposing
        # "close/open latch" checkboxes someone could forget to tick.
        "release_field": "OpenLatch",
        "fields": [
            {"name": "Temperature", "label": "Temperatura", "type": "number", "unit": "°C", "min": 4, "max": 95},
            {"name": "RPM", "label": "Velocita'", "type": "number", "unit": "rpm", "min": 200, "max": 3000},
            {"name": "Duration", "label": "Durata agitazione", "type": "number", "unit": "s", "min": 0},
            {"name": "WaitForTemp", "label": "Attendi il raggiungimento della temperatura", "type": "bool", "default": True},
            {"name": "KeepShaking", "label": "Continua ad agitare anche dopo questo step", "type": "bool", "default": False},
            {"name": "DeactivateHeater", "label": "Spegni il riscaldamento", "type": "bool", "default": False},
        ],
    },
    "TemperatureModule": {
        "label": "Modulo Temperatura",
        "module_type": "temperatureModuleV2",
        "requirement_key": "TemperatureModule",
        "command": "TemperatureModule",
        "release_field": None,  # flat block, no clamp/lid over the labware
        "fields": [
            {"name": "Temperature", "label": "Temperatura", "type": "number", "unit": "°C", "min": -10, "max": 99},
            {"name": "WaitForTemp", "label": "Attendi il raggiungimento della temperatura", "type": "bool", "default": True},
            {"name": "Deactivate", "label": "Spegni il modulo", "type": "bool", "default": False},
        ],
    },
    "Thermocycler": {
        "label": "Termociclatore",
        "module_type": "thermocyclerModuleV2",
        "requirement_key": "Thermocycler",
        "command": "Thermocycler",
        "release_field": "OpenLid",  # closed lid blocks the gripper the same way a HeaterShaker latch does
        "fields": [
            {"name": "CloseLid", "label": "Chiudi coperchio", "type": "bool", "default": False},
            {"name": "LidTemperature", "label": "Temperatura coperchio", "type": "number", "unit": "°C"},
            {"name": "BlockTemperature", "label": "Temperatura blocco", "type": "number", "unit": "°C"},
            {"name": "HoldTime", "label": "Tempo di mantenimento", "type": "number", "unit": "s"},
            {"name": "DeactivateLid", "label": "Spegni coperchio", "type": "bool", "default": False},
            {"name": "DeactivateBlock", "label": "Spegni blocco", "type": "bool", "default": False},
        ],
    },
    "MagneticModule": {
        "label": "Modulo Magnetico",
        "module_type": "magneticModuleV2",
        "requirement_key": "MagneticModule",
        "command": "MagneticModule",
        "release_field": None,  # no clamp/lid over the labware
        "fields": [
            {"name": "Engage", "label": "Attiva magnete", "type": "bool", "default": True},
            {"name": "Disengage", "label": "Disattiva magnete", "type": "bool", "default": False},
            {"name": "Height", "label": "Altezza", "type": "number", "unit": "mm"},
        ],
    },
    "AbsorbanceReader": {
        "label": "Lettore di Assorbanza",
        "module_type": "absorbanceReaderV1",
        "requirement_key": "AbsorbanceReader",
        "command": "AbsorbanceReader",
        "release_field": "OpenLid",
        "fields": [
            {"name": "Initialize", "label": "Inizializza", "type": "bool", "default": True},
            {"name": "Read", "label": "Esegui lettura", "type": "bool", "default": True},
            {"name": "CloseLid", "label": "Chiudi coperchio", "type": "bool", "default": False},
        ],
    },
}

# Ready-made shortcuts built on top of the generic mechanism above (not a
# separate implementation) - they just pre-fill common field combinations.
TREATMENT_PRESETS: Dict[str, Dict] = {
    "heat_shake": {
        "label": "Scalda e agita",
        "kind": "HeaterShaker",
        "target": "TargetPlate",
        "params": {"Temperature": 37, "RPM": 300, "Duration": 300, "WaitForTemp": True},
        "return_after": True,  # latch opens automatically before the return move
    },
    "keep_warm": {
        "label": "Mantieni caldo",
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


def _compute_home_slots(all_treatments: List[Dict], num_sources: int) -> Dict[str, str]:
    """Predict the deck slot each treatable labware (TargetPlate / Source_N) will be
    assigned in the HAL, following the exact same convention as
    generate_hal_config_from_recipe(), so MoveLabware-back steps target a real slot."""
    distinct_modules: List[str] = []
    for t in all_treatments:
        entry = MODULE_TREATMENT_CATALOG.get(t.get("kind"))
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
    catalog_entry = MODULE_TREATMENT_CATALOG.get(treatment.get("kind"))
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


def _estimate_distribute_waste(volumes: List[float], tip_capacity: float, disposal_volume: float) -> float:
    """Approximate the liquid Opentrons' distribute() blows to trash as "disposal
    volume": by default it aspirates enough for several destination wells plus one
    disposal_volume per aspirate trip, then blows that extra out after dispensing
    all wells in that trip (docs.opentrons.com, distribute() disposal_volume).
    This mirrors that greedy per-trip chunking to estimate total waste - the real
    Opentrons algorithm may batch slightly differently, so treat this as a ballpark."""
    if disposal_volume <= 0 or not volumes:
        return 0.0
    usable_capacity = tip_capacity - disposal_volume
    if usable_capacity <= 0:
        usable_capacity = tip_capacity
    waste = 0.0
    i = 0
    n = len(volumes)
    while i < n:
        used = 0.0
        while i < n and used + volumes[i] <= usable_capacity:
            used += volumes[i]
            i += 1
        if used == 0.0:  # a single destination volume alone exceeds the tip - take it anyway
            i += 1
        waste += disposal_volume
    return waste


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

    for idx, reagent in enumerate(all_group_reagents):
        source_idx = idx // reagents_per_reservoir
        well_idx = idx % reagents_per_reservoir
        source_key = f"Source_{source_idx + 1}"
        dedicated = reagents_per_reservoir == 1
        base_well = reservoir_wells[well_idx] if well_idx < len(reservoir_wells) else reservoir_wells[-1]

        requirements.setdefault(source_key, source_key)
        reagent_to_source_map[reagent] = source_key
        all_reagents_in_config.append(reagent)

        rv = required_volumes.get(reagent, {})
        with_excess = rv.get('with_excess_ul', 0)
        capacity = get_reservoir_well_capacity(reservoir_type, base_well)
        has_manual_batching = any(
            reagent in g.get('reagent_batches', {}) and g['reagent_batches'][reagent].get('batch_size')
            for g in groups
        )

        if dedicated and capacity and with_excess > capacity and not has_manual_batching:
            # This reagent has its own reservoir plate to itself, so spilling over
            # into more of ITS wells is always safe (no other reagent to collide with).
            n_wells_needed = min(len(reservoir_wells), -(-int(with_excess) // int(capacity)))
            split_wells = reservoir_wells[:n_wells_needed]
            reagent_to_well_map[reagent] = split_wells
            quantity_notes.append(
                f"{reagent}: {rv.get('nominal_ul')} uL nominali (~{rv.get('with_excess_ul')} uL con margine) "
                f"superano la capacita' di un pozzetto ({capacity} uL) - diviso automaticamente su "
                f"{source_key}:{'+'.join(split_wells)}"
            )
        else:
            reagent_to_well_map[reagent] = base_well
            note = f"{reagent}: {rv.get('nominal_ul')} uL nominali, carica almeno {rv.get('with_excess_ul')} uL in {source_key}:{base_well}"
            if capacity and with_excess > capacity:
                note += f" (ATTENZIONE: supera la capacita' del pozzetto condiviso di {capacity} uL - riduci reagenti per reservoir o gestisci il rabbocco a mano)"
            quantity_notes.append(note)

    num_sources_used = len(set(reagent_to_source_map.values()))

    requirements["TargetPlate"] = "TargetPlate"
    requirements["MyTips"] = "MyTips"
    requirements["MyTrash"] = "MyTrash"

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

            # Optional per-reagent batching: split this reagent's own destination wells
            # into chunks with a module treatment between them (e.g. periodically
            # reheating a reagent on the source reservoir so it doesn't solidify while
            # sitting on deck during a long 96-well distribution - see the hand-authored
            # SPAAC reference recipe for the pattern this reproduces).
            batch_cfg = group.get('reagent_batches', {}).get(reagent)
            batch_size = batch_cfg.get('batch_size') if batch_cfg else None

            if isinstance(well, list):
                # Auto-split across this reagent's own reservoir wells because the
                # required volume (Fase 3 estimate) exceeds one well's capacity -
                # bucket destinations by cumulative volume instead of well count.
                well_capacity = get_reservoir_well_capacity(reservoir_type, well[0]) or float('inf')
                chunks = _bucket_transfers_by_capacity(transfers, well_capacity)
                chunk_wells = well[:len(chunks)] if len(well) >= len(chunks) else well + [well[-1]] * (len(chunks) - len(well))
            else:
                chunks = [transfers[i:i + batch_size] for i in range(0, len(transfers), batch_size)] if batch_size else [transfers]
                chunk_wells = [well] * len(chunks)

            if strategy == 'transfer':
                # 1 aspirate -> 1 dispense per well, fresh tip every time (safer, slower).
                # Transfer only accepts a single Dest per step (unlike Distribute), so
                # each well needs its own PickUpTip/Transfer/DropTip triplet.
                for chunk, chunk_well in zip(chunks, chunk_wells):
                    if batch_cfg and batch_cfg.get('treatment'):
                        emit(batch_cfg['treatment'])
                    for t in chunk:
                        steps.append({"Command": "PickUpTip", "PipetteMount": pipette, "TipRack": "MyTips"})
                        transfer_cmd = {
                            "Command": "Transfer",
                            "PipetteMount": pipette,
                            "Volume": t['volume'],
                            "Source": f"{source_name}:{chunk_well}",
                            "Dest": f"TargetPlate:{t['well']}",
                            "NewTip": "never",
                            "Description": f"Transfer {t['volume']}µL {reagent} to {t['well']}"
                        }
                        if liquid_class:
                            transfer_cmd["LiquidClass"] = liquid_class
                        _apply_advanced_liquid_options(transfer_cmd, adv)
                        steps.append(transfer_cmd)
                        steps.append({"Command": "DropTip", "PipetteMount": pipette, "TrashLocation": "MyTrash"})
            else:
                # Distribute: one aspirate -> many dispenses, single tip for the whole
                # reagent (or per chunk if split by batch size or well capacity).
                steps.append({"Command": "PickUpTip", "PipetteMount": pipette, "TipRack": "MyTips"})
                for chunk, chunk_well in zip(chunks, chunk_wells):
                    if batch_cfg and batch_cfg.get('treatment'):
                        emit(batch_cfg['treatment'])

                    volumes_list = [t['volume'] for t in chunk]
                    dest_wells = [t['well'] for t in chunk]
                    unique_volumes = set(volumes_list)

                    reagent_waste_ul[reagent] = reagent_waste_ul.get(reagent, 0.0) + _estimate_distribute_waste(
                        volumes_list, pipette_max_volume, pipette_min_volume
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

    # Fold the waste estimate (Fase 7) into the same quantity metadata as Fase 3.
    total_waste_ul = 0.0
    for reagent, waste in reagent_waste_ul.items():
        if waste <= 0:
            continue
        total_waste_ul += waste
        if reagent in required_volumes:
            required_volumes[reagent]['estimated_waste_ul'] = round(waste, 1)
        pct = (waste / required_volumes[reagent]['nominal_ul'] * 100) if required_volumes.get(reagent, {}).get('nominal_ul') else 0
        quantity_notes.append(
            f"{reagent}: ~{round(waste, 1)} uL stimati sprecati (disposal volume di Distribute, "
            f"~{pct:.0f}% del nominale) - stima basata sul comportamento di default di Opentrons distribute(), da verificare"
        )

    well_assignments = {
        reagent: f"{reagent_to_source_map.get(reagent, '?')}:" + (
            '+'.join(w) if isinstance(w, list) else w
        )
        for reagent, w in reagent_to_well_map.items()
    }

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
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
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
    # A1=HeaterShaker (or other module), A3=Trash, D2=TargetPlate, B3=TipRack
    available_reservoir_slots = [s for s in RESERVOIR_SLOT_POOL if s not in reserved_module_slots]

    labware = {}
    slot_assignments = {}
    
    # Tip racks - Column 3: B3 primary, C3 backup
    # A1=HeaterShaker, A3=Trash
    labware['MyTips'] = {
        'LoadName': 'opentrons_flex_96_tiprack_1000ul',
        'Slot': 'B3',
        'DisplayName': 'Tip rack 1000µL'
    }
    slot_assignments['B3'] = 'MyTips'
    
    labware['MyTips_2'] = {
        'LoadName': 'opentrons_flex_96_tiprack_1000ul',
        'Slot': 'C3',
        'DisplayName': 'Tip rack 1000µL (backup)'
    }
    slot_assignments['C3'] = 'MyTips_2'
    
    # Target plate
    config = recipe.get('Configuration', {})
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
    pipette: str = "flex_1channel_1000"
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
        
    Returns:
        HAL configuration dict compatible with opentrons_client.py
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    # Available slots for reservoirs
    # Priority: A2 → B2 → C2 → D3 → D1 → C1 → B1 → C3
    available_reservoir_slots = ['A2', 'B2', 'C2', 'D3', 'D1', 'C1', 'B1', 'C3']
    max_sources = min(num_sources, len(available_reservoir_slots))
    
    labware = {}
    slot_assignments = {}
    
    # Tip racks - Column 3: B3 primary, C3 backup
    labware['MyTips'] = {
        'LoadName': 'opentrons_flex_96_tiprack_1000ul',
        'Slot': 'B3',
        'DisplayName': 'Tip rack 1000µL'
    }
    slot_assignments['B3'] = 'MyTips'
    
    labware['MyTips_2'] = {
        'LoadName': 'opentrons_flex_96_tiprack_1000ul',
        'Slot': 'C3',
        'DisplayName': 'Tip rack 1000µL (backup)'
    }
    slot_assignments['C3'] = 'MyTips_2'
    
    # Target plate
    labware['TargetPlate'] = {
        'LoadName': target_plate,
        'Slot': 'D2',
        'DisplayName': 'Target plate'
    }
    slot_assignments['D2'] = 'TargetPlate'
    
    # Generic numbered reservoirs
    for i in range(max_sources):
        slot = available_reservoir_slots[i]
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
            'note': 'Load reagents into Source_1, Source_2, etc. Check recipe metadata for reagent order.'
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
