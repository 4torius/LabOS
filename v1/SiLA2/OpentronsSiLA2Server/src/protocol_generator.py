"""
Protocol Generator - Creates Python protocols from JSON recipes
===============================================================

Generates Opentrons Python protocols from JSON recipe definitions
using the Universal Template pattern.

Features:
    - JSON Schema validation before generation
    - Local simulation with opentrons_simulate
    - Line mapping from Python errors to JSON step IDs
    - Comprehensive error reporting
"""

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#                        VALIDATION & ERROR HANDLING
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ValidationError:
    """Represents a validation error."""
    step_index: Optional[int]
    step_id: Optional[str]
    command: Optional[str]
    field: str
    message: str
    severity: str = "error"  # error, warning
    
    def to_dict(self) -> dict:
        return {
            "step_index": self.step_index,
            "step_id": self.step_id,
            "command": self.command,
            "field": self.field,
            "message": self.message,
            "severity": self.severity
        }
    
    def __str__(self) -> str:
        loc = f"Step {self.step_index}" if self.step_index is not None else "Global"
        cmd = f" ({self.command})" if self.command else ""
        return f"[{self.severity.upper()}] {loc}{cmd}: {self.field} - {self.message}"


@dataclass
class SimulationResult:
    """Result of protocol simulation."""
    success: bool
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationError] = field(default_factory=list)
    output: str = ""
    duration_estimate: Optional[float] = None
    
    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
            "output": self.output,
            "duration_estimate": self.duration_estimate
        }


@dataclass
class GenerationResult:
    """Result of protocol generation."""
    success: bool
    code: Optional[str] = None
    filepath: Optional[str] = None
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationError] = field(default_factory=list)
    line_map: Dict[int, int] = field(default_factory=dict)  # python_line -> step_index
    simulation: Optional[SimulationResult] = None
    
    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "filepath": self.filepath,
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
            "simulation": self.simulation.to_dict() if self.simulation else None
        }


# ═══════════════════════════════════════════════════════════════════════════
#                    LIQUID CLASS LIBRARY LOADER
# ═══════════════════════════════════════════════════════════════════════════

def load_liquid_classes_from_library() -> Dict[str, dict]:
    """
    Load liquid class definitions from Library/LiquidClasses/*.json
    Returns a dict mapping class name to parameters.
    """
    liquid_classes = {}
    
    # Find the Library folder (relative to this file or workspace root)
    possible_paths = [
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "Library", "LiquidClasses"),
        os.path.join(os.getcwd(), "Library", "LiquidClasses"),
        r"C:\Users\PC\Desktop\AutoLAB\v1\Library\LiquidClasses"
    ]
    
    library_path = None
    for p in possible_paths:
        if os.path.isdir(p):
            library_path = p
            break
    
    if not library_path:
        logger.warning("LiquidClasses library folder not found, using defaults")
        return liquid_classes
    
    # Load all JSON files (except schema)
    for filename in os.listdir(library_path):
        if filename.endswith('.json') and not filename.endswith('.schema.json'):
            filepath = os.path.join(library_path, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                name = data.get('name', filename.replace('.json', ''))
                params = data.get('parameters', {})
                
                # Also store loadName for native Opentrons API
                if 'loadName' in data:
                    params['_loadName'] = data['loadName']
                if 'description' in data:
                    params['description'] = data['description']
                
                liquid_classes[name] = params
                logger.debug(f"Loaded liquid class: {name}")
                
            except Exception as e:
                logger.warning(f"Failed to load liquid class {filename}: {e}")
    
    return liquid_classes

# Load liquid classes from Library at module import time
LIBRARY_LIQUID_CLASSES = load_liquid_classes_from_library()


# ═══════════════════════════════════════════════════════════════════════════
#                        JSON SCHEMA DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════

# Required fields per command
# Note: For location-based commands, ONE OF the location fields is required (Location, Labware, Well)
# LiquidClass can be: Aqueous, Viscous, Volatile, HighlyViscous, Foaming
COMMAND_SCHEMA = {
    # ═══════════════════════════════════════════════════════════════════════
    #                    TIP HANDLING
    # ═══════════════════════════════════════════════════════════════════════
    "PickUpTip": {"required": ["PipetteMount"], "optional": ["TipRack", "Well", "Location"]},
    "DropTip": {"required": ["PipetteMount"], "optional": ["TrashLocation", "Force", "Location"]},
    "DropTipInPlace": {"required": ["PipetteMount"], "optional": ["HomeAfter"]},
    "ReturnTip": {"required": ["PipetteMount"], "optional": []},
    "ConsumeTips": {"required": ["PipetteMount"], "optional": ["Quantity", "TrashLocation"]},
    "VerifyTipPresence": {"required": ["PipetteMount"], "optional": ["ExpectedState"]},
    "GetTipPresence": {"required": ["PipetteMount"], "optional": []},
    "GetNextTip": {"required": ["PipetteMount"], "optional": ["TipRack", "StartingTip"]},
    
    # ═══════════════════════════════════════════════════════════════════════
    #                    LIQUID HANDLING - LOW LEVEL
    # ═══════════════════════════════════════════════════════════════════════
    # Location can be specified as "Location", "Labware", or "Labware:Well"
    # LiquidClass: Aqueous (default), Viscous, Volatile, HighlyViscous, Foaming
    "Aspirate": {"required": ["PipetteMount", "Volume"], "optional": ["Location", "Labware", "Rate", "LiquidClass", "DelayAfter"], "location_fields": ["Location", "Labware"]},
    "AspirateInPlace": {"required": ["PipetteMount", "Volume"], "optional": ["Rate", "LiquidClass"]},
    "Dispense": {"required": ["PipetteMount", "Volume"], "optional": ["Location", "Labware", "Rate", "PushOut", "LiquidClass", "DelayAfter"], "location_fields": ["Location", "Labware"]},
    "DispenseInPlace": {"required": ["PipetteMount", "Volume"], "optional": ["Rate", "PushOut", "LiquidClass"]},
    "Mix": {"required": ["PipetteMount"], "optional": ["Location", "Labware", "Volume", "Repetitions", "Cycles", "Rate", "LiquidClass"], "location_fields": ["Location", "Labware"]},
    "BlowOut": {"required": ["PipetteMount"], "optional": ["Location", "Labware"]},
    "BlowOutInPlace": {"required": ["PipetteMount"], "optional": []},
    "TouchTip": {"required": ["PipetteMount"], "optional": ["Location", "Labware", "Radius", "VerticalOffset", "Speed"]},
    "AirGap": {"required": ["PipetteMount"], "optional": ["Volume", "Height"]},
    "AirGapInPlace": {"required": ["PipetteMount"], "optional": ["Volume"]},
    "PrepareToAspirate": {"required": ["PipetteMount"], "optional": []},
    
    # ═══════════════════════════════════════════════════════════════════════
    #                    LIQUID HANDLING - HIGH LEVEL
    # ═══════════════════════════════════════════════════════════════════════
    # LiquidClass auto-applies: Rate, BlowOut, TouchTip, AirGap, MixAfter based on liquid type
    "Transfer": {"required": ["PipetteMount", "Volume", "Source"], "optional": ["Dest", "Destination", "NewTip", "BlowOut", "BlowOutLocation", "TouchTip", "MixBefore", "MixAfter", "AirGap", "LiquidClass", "DispenseRate", "AspirateRate", "TrashLocation"]},
    "Distribute": {"required": ["PipetteMount", "Volume", "Source", "Destinations"], "optional": ["NewTip", "LiquidClass", "DispenseRate", "TrashLocation"]},
    "Consolidate": {"required": ["PipetteMount", "Volume", "Sources"], "optional": ["Dest", "Destination", "NewTip", "LiquidClass", "DispenseRate", "TrashLocation"]},
    
    # Native Opentrons Liquid Class commands (API 2.24+)
    # Uses get_liquid_class() and transfer_with_liquid_class() for optimal accuracy
    # LiquidClass: "water" (Aqueous), "glycerol_50" (Viscous), "ethanol_80" (Volatile)
    "TransferWithLiquidClass": {"required": ["PipetteMount", "Volume", "Source", "LiquidClass"], "optional": ["Dest", "Destination", "NewTip", "TrashLocation"]},
    "DistributeWithLiquidClass": {"required": ["PipetteMount", "Volume", "Source", "Destinations", "LiquidClass"], "optional": ["NewTip", "TrashLocation"]},
    "ConsolidateWithLiquidClass": {"required": ["PipetteMount", "Volume", "Sources", "LiquidClass"], "optional": ["Dest", "Destination", "NewTip", "TrashLocation"]},
    
    # ═══════════════════════════════════════════════════════════════════════
    #                    LIQUID LEVEL DETECTION (API 2.20+)
    # ═══════════════════════════════════════════════════════════════════════
    "LiquidProbe": {"required": ["PipetteMount", "Location"], "optional": ["WellLocation"]},
    "TryLiquidProbe": {"required": ["PipetteMount", "Location"], "optional": ["WellLocation"]},
    
    # ═══════════════════════════════════════════════════════════════════════
    #                    MOVEMENT
    # ═══════════════════════════════════════════════════════════════════════
    "MoveLabware": {"required": ["LabwareID", "NewLocation"], "optional": ["UseGripper", "PickUpOffset", "DropOffset"]},
    "MoveToWell": {"required": ["PipetteMount", "Location"], "optional": ["Speed", "ForceDirect", "MinimumZHeight"]},
    "MoveRelative": {"required": ["PipetteMount", "Distance"], "optional": ["Axis", "Speed"]},
    "MoveToCoordinates": {"required": ["PipetteMount", "X", "Y", "Z"], "optional": ["Speed", "MinimumZHeight"]},
    "MoveToAddressableArea": {"required": ["PipetteMount", "AddressableArea"], "optional": ["Offset", "Speed", "MinimumZHeight"]},
    "RetractAxis": {"required": [], "optional": ["Axis", "Mount"]},
    "SavePosition": {"required": ["PipetteMount"], "optional": ["PositionId"]},
    "Home": {"required": [], "optional": ["Axes", "SkipIfHomed"]},
    
    # ═══════════════════════════════════════════════════════════════════════
    #                    HEATER-SHAKER MODULE
    # ═══════════════════════════════════════════════════════════════════════
    "HeaterShaker": {"required": ["ModuleID"], "optional": ["Temperature", "RPM", "Duration", "CloseLatch", "OpenLatch", "WaitForTemp", "StopShaking", "DeactivateHeater", "KeepShaking"]},
    "HeaterShakerSetTemperature": {"required": ["ModuleID", "Temperature"], "optional": []},
    "HeaterShakerWaitForTemperature": {"required": ["ModuleID"], "optional": []},
    "HeaterShakerSetShake": {"required": ["ModuleID", "RPM"], "optional": []},
    "HeaterShakerDeactivateShaker": {"required": ["ModuleID"], "optional": []},
    "HeaterShakerDeactivateHeater": {"required": ["ModuleID"], "optional": []},
    "HeaterShakerOpenLatch": {"required": ["ModuleID"], "optional": []},
    "HeaterShakerCloseLatch": {"required": ["ModuleID"], "optional": []},
    
    # ═══════════════════════════════════════════════════════════════════════
    #                    THERMOCYCLER MODULE
    # ═══════════════════════════════════════════════════════════════════════
    "Thermocycler": {"required": ["ModuleID"], "optional": ["OpenLid", "CloseLid", "LidTemperature", "BlockTemperature", "HoldTime", "BlockMaxVolume", "DeactivateLid", "DeactivateBlock"]},
    "ThermocyclerRunProfile": {"required": ["ModuleID", "Profile"], "optional": ["Repetitions", "BlockMaxVolume"]},
    "ThermocyclerOpenLid": {"required": ["ModuleID"], "optional": []},
    "ThermocyclerCloseLid": {"required": ["ModuleID"], "optional": []},
    "ThermocyclerSetLidTemperature": {"required": ["ModuleID", "Temperature"], "optional": []},
    "ThermocyclerWaitForLidTemperature": {"required": ["ModuleID"], "optional": []},
    "ThermocyclerSetBlockTemperature": {"required": ["ModuleID", "Temperature"], "optional": ["HoldTime", "BlockMaxVolume"]},
    "ThermocyclerWaitForBlockTemperature": {"required": ["ModuleID"], "optional": []},
    "ThermocyclerDeactivate": {"required": ["ModuleID"], "optional": ["DeactivateLid", "DeactivateBlock"]},
    
    # ═══════════════════════════════════════════════════════════════════════
    #                    TEMPERATURE MODULE
    # ═══════════════════════════════════════════════════════════════════════
    "TemperatureModule": {"required": ["ModuleID"], "optional": ["Temperature", "WaitForTemp", "Deactivate"]},
    "TemperatureModuleSetTemperature": {"required": ["ModuleID", "Temperature"], "optional": []},
    "TemperatureModuleWaitForTemperature": {"required": ["ModuleID"], "optional": ["Temperature"]},
    "TemperatureModuleDeactivate": {"required": ["ModuleID"], "optional": []},
    
    # ═══════════════════════════════════════════════════════════════════════
    #                    MAGNETIC MODULE
    # ═══════════════════════════════════════════════════════════════════════
    "MagneticModule": {"required": ["ModuleID"], "optional": ["Engage", "Disengage", "Height"]},
    "MagneticModuleEngage": {"required": ["ModuleID"], "optional": ["Height"]},
    "MagneticModuleDisengage": {"required": ["ModuleID"], "optional": []},
    
    # ═══════════════════════════════════════════════════════════════════════
    #                    ABSORBANCE READER MODULE
    # ═══════════════════════════════════════════════════════════════════════
    "AbsorbanceReader": {"required": ["ModuleID"], "optional": ["OpenLid", "CloseLid", "Initialize", "Read", "ExportFilename"]},
    "AbsorbanceReaderInitialize": {"required": ["ModuleID"], "optional": ["Wavelengths", "Mode", "ReferenceWavelength"]},
    "AbsorbanceReaderRead": {"required": ["ModuleID"], "optional": ["ExportFilename"]},
    "AbsorbanceReaderOpenLid": {"required": ["ModuleID"], "optional": []},
    "AbsorbanceReaderCloseLid": {"required": ["ModuleID"], "optional": []},
    
    # ═══════════════════════════════════════════════════════════════════════
    #                    FLEX STACKER MODULE
    # ═══════════════════════════════════════════════════════════════════════
    "FlexStackerRetrieve": {"required": ["ModuleID"], "optional": ["LabwareID"]},
    "FlexStackerStore": {"required": ["ModuleID"], "optional": ["LabwareID"]},
    "FlexStackerFill": {"required": ["ModuleID"], "optional": ["LabwareID", "Count", "Strategy"]},
    "FlexStackerEmpty": {"required": ["ModuleID"], "optional": ["LabwareID", "Strategy"]},
    
    # ═══════════════════════════════════════════════════════════════════════
    #                    PIPETTE CONFIGURATION
    # ═══════════════════════════════════════════════════════════════════════
    "ConfigureNozzleLayout": {"required": ["PipetteMount"], "optional": ["Layout", "Start", "End", "FrontRight", "BackLeft"]},
    "ConfigureForVolume": {"required": ["PipetteMount", "Volume"], "optional": []},
    "SetFlowRates": {"required": ["PipetteMount"], "optional": ["Aspirate", "Dispense", "BlowOut"]},
    
    # ═══════════════════════════════════════════════════════════════════════
    #                    LIQUID & LABWARE DEFINITION
    # ═══════════════════════════════════════════════════════════════════════
    "DefineLiquid": {"required": ["Name"], "optional": ["Color", "Description"]},
    "LoadLiquid": {"required": ["LiquidID", "Labware", "Volume"], "optional": ["Wells"]},
    "LoadLiquidClass": {"required": ["Name"], "optional": []},
    "LoadLidStack": {"required": ["Slot", "Quantity"], "optional": ["LoadName", "Adapter"]},
    "LoadLid": {"required": ["LabwareID"], "optional": []},
    "ReloadLabware": {"required": ["LabwareID"], "optional": []},
    
    # ═══════════════════════════════════════════════════════════════════════
    #                    CONTROL & UI
    # ═══════════════════════════════════════════════════════════════════════
    "Comment": {"required": [], "optional": ["Text", "Message"]},
    "Delay": {"required": [], "optional": ["Minutes", "Seconds"]},
    "WaitForDuration": {"required": [], "optional": ["Seconds"]},
    "Pause": {"required": [], "optional": ["Message", "Text"]},
    "WaitForResume": {"required": [], "optional": ["Message"]},
    "SetRailLights": {"required": [], "optional": ["On"]},
    "SetStatusBar": {"required": [], "optional": ["Status", "Color"]},
    "TakeSnapshot": {"required": [], "optional": ["Filename"]},
    
    # ═══════════════════════════════════════════════════════════════════════
    #                    ADVANCED / CUSTOM
    # ═══════════════════════════════════════════════════════════════════════
    "CustomCommand": {"required": ["Name"], "optional": ["Params", "PythonCode"]},
    "DynamicAspirate": {"required": ["PipetteMount", "Volume", "Location"], "optional": ["StartPosition", "EndPosition"]},
    "DynamicDispense": {"required": ["PipetteMount", "Volume", "Location"], "optional": []},
    "DynamicMix": {"required": ["PipetteMount", "Location"], "optional": ["Volume", "Repetitions", "Locations"]},
    "ConcurrentHeaterShaker": {"required": ["ModuleID"], "optional": ["Temperature", "RPM"]},
    "ConcurrentThermocycler": {"required": ["ModuleID"], "optional": ["LidTemperature", "BlockTemperature"]},
    "ConcurrentTemperatureModule": {"required": ["ModuleID", "Temperature"], "optional": []},
}

# Valid module types
VALID_MODULE_TYPES = [
    "heaterShakerModuleV1",
    "temperatureModuleV1", "temperatureModuleV2",
    "magneticModuleV1", "magneticModuleV2",
    "thermocyclerModuleV1", "thermocyclerModuleV2",
    "absorbanceReaderV1",
    "flexStackerModuleV1",
]

# Valid pipette names
VALID_PIPETTES = [
    "flex_1channel_50", "flex_1channel_1000",
    "flex_8channel_50", "flex_8channel_1000",
    "flex_96channel_1000",
]

# Valid slots
# A1=HeaterShaker, A3=Trash, B3/C3=Tips, D2=Target, Column 2=Reservoirs
VALID_SLOTS = ["A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3", "D1", "D2", "D3"]
SLOTS_FOR_LABWARE = ["A2", "B1", "B2", "B3", "C1", "C2", "C3", "D1", "D2", "D3"]  # Exclude A1 (module), A3 (trash)


# ═══════════════════════════════════════════════════════════════════════════
#                        UNIVERSAL TEMPLATE
# ═══════════════════════════════════════════════════════════════════════════

UNIVERSAL_TEMPLATE = '''
from opentrons import protocol_api
import json
import subprocess
import os
import base64

metadata = { 'protocolName': '__PROTOCOL_NAME__', 'author': 'SiLA2Server' }
requirements = { "robotType": "Flex", "apiLevel": "2.27" }
PROTOCOL_SETTINGS = __PROTOCOL_SETTINGS__

# Liquid Class Presets - Opentrons verified parameters
LIQUID_CLASS_PRESETS = {
    "Aqueous": {"description": "Water-like liquids", "aspirate_rate": 1.0, "dispense_rate": 1.0, "blow_out": False, "touch_tip": False, "mix_after": None, "air_gap": 0, "push_out": 2.0, "delay_aspirate": 0.2, "delay_dispense": 0.2},
    "Viscous": {"description": "Viscous liquids (glycerol, PEG)", "aspirate_rate": 0.25, "dispense_rate": 0.25, "blow_out": True, "touch_tip": True, "mix_after": [3, 0.5], "air_gap": 0, "push_out": 5.0, "delay_aspirate": 1.0, "delay_dispense": 0.5},
    "Volatile": {"description": "Volatile liquids (ethanol)", "aspirate_rate": 0.5, "dispense_rate": 0.5, "blow_out": True, "touch_tip": False, "mix_after": None, "air_gap": 5, "push_out": 1.0, "delay_aspirate": 0.2, "delay_dispense": 0.2},
    "HighlyViscous": {"description": "Very viscous (>70% glycerol)", "aspirate_rate": 0.1, "dispense_rate": 0.1, "blow_out": True, "touch_tip": True, "mix_after": [5, 0.5], "air_gap": 0, "push_out": 10.0, "delay_aspirate": 2.0, "delay_dispense": 1.0},
    "Foaming": {"description": "Foaming liquids (detergents)", "aspirate_rate": 0.3, "dispense_rate": 0.3, "blow_out": False, "touch_tip": True, "mix_after": None, "air_gap": 2, "push_out": 3.0, "delay_aspirate": 0.5, "delay_dispense": 0.5},
}

# Opentrons Native Liquid Class Load Names (API 2.24+)
LIQUID_CLASS_LOAD_NAMES = {
    "Aqueous": "water", "Water": "water", "water": "water",
    "Viscous": "glycerol_50", "Glycerol": "glycerol_50", "glycerol_50": "glycerol_50",
    "Volatile": "ethanol_80", "Ethanol": "ethanol_80", "ethanol_80": "ethanol_80",
}

def run(ctx: protocol_api.ProtocolContext):
    settings = PROTOCOL_SETTINGS if PROTOCOL_SETTINGS else {}
    ctx.comment(f"Run: {metadata['protocolName']}")
    
    objects = { "Labware": {}, "Modules": {}, "Pipettes": {}, "Liquids": {}, "LiquidClasses": {}, "Trash": {} }

    # Load Custom Liquid Classes from recipe (merge with presets)
    custom_lc = settings.get("CustomLiquidClasses", {})
    for name, params in custom_lc.items():
        LIQUID_CLASS_PRESETS[name] = params
        ctx.comment(f"Loaded custom liquid class: {name}")

    # 1. LOAD MODULES
    if "Modules" in settings:
        for i, c in settings["Modules"].items():
            m = ctx.load_module(c["Type"], c["Slot"])
            objects["Modules"][i] = m
            if "heaterShaker" in c["Type"]: m.open_labware_latch()

    # 2. LOAD TRASH
    if "Trash" in settings:
        for i, c in settings["Trash"].items():
            if c["Type"]=="WasteChute": objects["Trash"][i]=ctx.load_waste_chute()
            elif c["Type"]=="TrashBin": objects["Trash"][i]=ctx.load_trash_bin(c["Slot"])
    else:
        try: objects["Trash"]["default"] = ctx.load_trash_bin("A3")
        except: pass

    # 3. LOAD LABWARE (Fixed for Adapters)
    if "Labware" in settings:
        for i, c in settings["Labware"].items():
            l = c.get("DisplayName")
            
            if "OnModule" in c: 
                mod = objects["Modules"][c["OnModule"]]
                if "adapter" in c["LoadName"]:
                    objects["Labware"][i] = mod.load_adapter(c["LoadName"])
                else:
                    objects["Labware"][i] = mod.load_labware(c["LoadName"], label=l)
            
            elif "OnAdapter" in c: 
                adapter = objects["Labware"][c["OnAdapter"]]
                objects["Labware"][i] = adapter.load_labware(c["LoadName"], label=l)
            
            else: 
                objects["Labware"][i] = ctx.load_labware(c["LoadName"], c["Slot"], label=l)

    # 4. LOAD PIPETTES & OFFSET
    if "Pipettes" in settings:
        u = settings.get("TipUsageMap", {})
        for m, c in settings["Pipettes"].items():
            n = c if isinstance(c, str) else c["Name"]
            ts = [v for k, v in objects["Labware"].items() if "tip" in k.lower()]
            p = ctx.load_instrument(n, m, tip_racks=ts)
            objects["Pipettes"][m] = p
            for r in ts:
                if r.load_name in u:
                    used = u[r.load_name]
                    same = [x for x in ts if x.load_name == r.load_name]
                    same.sort(key=lambda x: x.parent if isinstance(x.parent, str) else "Z")
                    try: rank = same.index(r)
                    except: continue
                    skip = used - (rank * 96)
                    if skip > 0 and skip < 96: p.starting_tip = r.wells()[skip]

    # 5. LIQUIDS & LIQUID CLASSES (API 2.20+)
    if "Liquids" in settings:
        for l, d in settings["Liquids"].items(): 
            objects["Liquids"][l] = ctx.define_liquid(name=l, display_color=d.get("Color"), description=d.get("Description", ""))
    
    if "LiquidClasses" in settings:
        for lc_name, lc_data in settings["LiquidClasses"].items():
            try:
                lc = ctx.define_liquid_class(lc_name)
                objects["LiquidClasses"][lc_name] = lc
            except: pass

    def get_loc(t):
        if isinstance(t, str):
            if ":" in t: return objects["Labware"][t.split(':')[0]][t.split(':')[1]]
            for c in ["Trash", "Modules", "Labware"]: 
                if t in objects[c]: return objects[c][t]
            if t=="MyTrash" and "default" in objects["Trash"]: return objects["Trash"]["default"]
        return t
    
    def get_pip(mount_or_id):
        if mount_or_id in objects["Pipettes"]: return objects["Pipettes"][mount_or_id]
        return objects["Pipettes"].get("left") or objects["Pipettes"].get("right")

    # EXECUTE STEPS
    for step in settings.get("Steps", []):
        # Handle legacy comment-only steps
        if "Comment" in step and "Command" not in step:
            ctx.comment(step["Comment"])
            continue
        
        cmd = step["Command"]
        ctx.comment(f"Step: {cmd}")

        # ═══════════════════════════════════════════════════════════════════
        #                    TIP HANDLING COMMANDS
        # ═══════════════════════════════════════════════════════════════════

        if cmd == "DropTip":
            pip = get_pip(step["PipetteMount"])
            loc = get_loc(step.get("TrashLocation", "default"))
            if step.get("Force", False):
                try: pip.drop_tip(loc, home_after=True)
                except Exception as e: ctx.comment(f"Force Drop ignored: {e}")
            else:
                if pip.has_tip: pip.drop_tip(loc)

        elif cmd == "PickUpTip": 
            pip = get_pip(step["PipetteMount"])
            if "Well" in step:
                rack = get_loc(step.get("TipRack"))
                pip.pick_up_tip(rack[step["Well"]])
            else:
                pip.pick_up_tip()

        elif cmd == "ConsumeTips":
            q = step.get("Quantity", 1); p = get_pip(step["PipetteMount"]); t = get_loc(step.get("TrashLocation", "default"))
            for _ in range(q):
                if p.has_tip: p.drop_tip(t)
                p.pick_up_tip(); p.drop_tip(t)

        elif cmd == "ReturnTip":
            pip = get_pip(step["PipetteMount"])
            if pip.has_tip: pip.return_tip()

        # ═══════════════════════════════════════════════════════════════════
        #               HIGH-LEVEL LIQUID HANDLING COMMANDS
        # ═══════════════════════════════════════════════════════════════════

        elif cmd in ["Transfer", "Distribute", "Consolidate"]:
            p = get_pip(step["PipetteMount"]); t = get_loc(step.get("TrashLocation", "default"))
            a = { "volume": step["Volume"], "new_tip": step.get("NewTip", "once"), "trash_location": t }
            
            # Apply LiquidClass presets if specified (can be overridden by explicit params)
            lc_name = step.get("LiquidClass")
            lc_params = LIQUID_CLASS_PRESETS.get(lc_name, {}) if lc_name else {}
            
            # Advanced options - check explicit values first, then liquid class preset
            if "BlowOut" in step: a["blow_out"] = step["BlowOut"]
            elif lc_params.get("blow_out"): a["blow_out"] = lc_params["blow_out"]
            
            if "BlowOutLocation" in step: a["blowout_location"] = step["BlowOutLocation"]
            
            if "TouchTip" in step: a["touch_tip"] = step["TouchTip"]
            elif lc_params.get("touch_tip"): a["touch_tip"] = lc_params["touch_tip"]
            
            if "MixBefore" in step: a["mix_before"] = tuple(step["MixBefore"])
            
            if "MixAfter" in step: a["mix_after"] = tuple(step["MixAfter"])
            elif lc_params.get("mix_after"): 
                # Convert mix_after from [cycles, volume_fraction] to (cycles, actual_volume)
                mix_cfg = lc_params["mix_after"]
                if isinstance(mix_cfg, list) and len(mix_cfg) == 2:
                    a["mix_after"] = (mix_cfg[0], step["Volume"] * mix_cfg[1])
            
            if "AirGap" in step: a["air_gap"] = step["AirGap"]
            elif lc_params.get("air_gap"): a["air_gap"] = lc_params["air_gap"]
            
            if "DispenseRate" in step: a["rate"] = step["DispenseRate"]
            elif lc_params.get("dispense_rate"): a["rate"] = lc_params["dispense_rate"]
            
            if cmd=="Transfer": 
                a["source"]=get_loc(step["Source"]); a["dest"]=get_loc(step.get("Dest") or step.get("Destination"))
            elif cmd=="Distribute": 
                a["source"]=get_loc(step["Source"]); a["dest"]=[get_loc(d) for d in step["Destinations"]]
            elif cmd=="Consolidate": 
                a["source"]=[get_loc(s) for s in step["Sources"]]; a["dest"]=get_loc(step.get("Dest") or step.get("Destination"))
            getattr(p, cmd.lower())(**a)

        # ═══════════════════════════════════════════════════════════════════
        #     NATIVE OPENTRONS LIQUID CLASS COMMANDS (API 2.24+)
        # ═══════════════════════════════════════════════════════════════════
        # Uses get_liquid_class() and transfer_with_liquid_class() for optimal accuracy
        # Automatically handles: submerge speed, flow rates, delays, push out, air gaps

        elif cmd in ["TransferWithLiquidClass", "DistributeWithLiquidClass", "ConsolidateWithLiquidClass"]:
            pip = get_pip(step["PipetteMount"])
            trash = get_loc(step.get("TrashLocation", "default"))
            
            # Get Opentrons liquid class load name
            lc_input = step["LiquidClass"]
            lc_load_name = LIQUID_CLASS_LOAD_NAMES.get(lc_input, lc_input)
            
            # Load the liquid class using native Opentrons API
            liquid_class = ctx.get_liquid_class(name=lc_load_name)
            
            # Build arguments
            args = {
                "liquid_class": liquid_class,
                "volume": step["Volume"],
                "new_tip": step.get("NewTip", "always"),
                "trash_location": trash,
            }
            
            # Execute the appropriate command
            if cmd == "TransferWithLiquidClass":
                args["source"] = get_loc(step["Source"])
                args["dest"] = get_loc(step.get("Dest") or step.get("Destination"))
                pip.transfer_with_liquid_class(**args)
            elif cmd == "DistributeWithLiquidClass":
                args["source"] = get_loc(step["Source"])
                args["dest"] = [get_loc(d) for d in step["Destinations"]]
                pip.distribute_with_liquid_class(**args)
            elif cmd == "ConsolidateWithLiquidClass":
                args["source"] = [get_loc(s) for s in step["Sources"]]
                args["dest"] = get_loc(step.get("Dest") or step.get("Destination"))
                pip.consolidate_with_liquid_class(**args)

        # ═══════════════════════════════════════════════════════════════════
        #               LOW-LEVEL LIQUID HANDLING COMMANDS
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "Aspirate":
            pip = get_pip(step["PipetteMount"])
            loc = get_loc(step["Location"])
            vol = step["Volume"]
            
            # Apply LiquidClass preset if specified
            lc_name = step.get("LiquidClass")
            lc_params = LIQUID_CLASS_PRESETS.get(lc_name, {}) if lc_name else {}
            
            rate = step.get("Rate") or lc_params.get("aspirate_rate", 1.0)
            pip.aspirate(vol, loc, rate=rate)
            
            # Apply delay after aspirate if specified
            delay = step.get("DelayAfter") or lc_params.get("delay_aspirate", 0)
            if delay > 0:
                ctx.delay(seconds=delay)

        elif cmd == "Dispense":
            pip = get_pip(step["PipetteMount"])
            loc = get_loc(step["Location"])
            vol = step["Volume"]
            
            # Apply LiquidClass preset if specified
            lc_name = step.get("LiquidClass")
            lc_params = LIQUID_CLASS_PRESETS.get(lc_name, {}) if lc_name else {}
            
            rate = step.get("Rate") or lc_params.get("dispense_rate", 1.0)
            push_out = step.get("PushOut") or lc_params.get("push_out")
            
            if push_out:
                pip.dispense(vol, loc, rate=rate, push_out=push_out)
            else:
                pip.dispense(vol, loc, rate=rate)
            
            # Apply delay after dispense if specified
            delay = step.get("DelayAfter") or lc_params.get("delay_dispense", 0)
            if delay > 0:
                ctx.delay(seconds=delay)

        elif cmd == "Mix":
            pip = get_pip(step["PipetteMount"])
            loc = get_loc(step["Location"])
            vol = step.get("Volume", pip.max_volume * 0.8)
            reps = step.get("Repetitions") or step.get("Cycles", 3)
            
            # Apply LiquidClass preset if specified
            lc_name = step.get("LiquidClass")
            lc_params = LIQUID_CLASS_PRESETS.get(lc_name, {}) if lc_name else {}
            
            rate = step.get("Rate") or lc_params.get("aspirate_rate", 1.0)
            pip.mix(reps, vol, loc, rate=rate)

        elif cmd == "BlowOut":
            pip = get_pip(step["PipetteMount"])
            loc = get_loc(step.get("Location", "default"))
            pip.blow_out(loc)

        elif cmd == "TouchTip":
            pip = get_pip(step["PipetteMount"])
            loc = get_loc(step.get("Location"))
            radius = step.get("Radius", 1.0)
            v_offset = step.get("VerticalOffset", -1.0)
            speed = step.get("Speed", 60.0)
            pip.touch_tip(loc, radius=radius, v_offset=v_offset, speed=speed)

        elif cmd == "AirGap":
            pip = get_pip(step["PipetteMount"])
            vol = step.get("Volume", pip.min_volume)
            height = step.get("Height")
            if height:
                pip.air_gap(vol, height=height)
            else:
                pip.air_gap(vol)

        elif cmd == "PrepareToAspirate":
            pip = get_pip(step["PipetteMount"])
            pip.prepare_to_aspirate()

        # ═══════════════════════════════════════════════════════════════════
        #               MOVEMENT COMMANDS
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "MoveLabware":
            l = get_loc(step["LabwareID"])
            n = get_loc(step["NewLocation"])
            target_to_check = n
            if hasattr(target_to_check, "parent"):
                if target_to_check.parent is not None:
                    target_to_check = target_to_check.parent
            if hasattr(target_to_check, 'open_labware_latch'):
                ctx.comment(f"Auto-Opening Latch on {target_to_check}")
                target_to_check.open_labware_latch()
            ctx.move_labware(l, n, use_gripper=step.get("UseGripper", True))

        elif cmd == "MoveToWell":
            pip = get_pip(step["PipetteMount"])
            loc = get_loc(step["Location"])
            speed = step.get("Speed")
            force_direct = step.get("ForceDirect", False)
            minimum_z_height = step.get("MinimumZHeight")
            if speed:
                pip.move_to(loc, speed=speed, force_direct=force_direct, minimum_z_height=minimum_z_height)
            else:
                pip.move_to(loc, force_direct=force_direct, minimum_z_height=minimum_z_height)

        elif cmd == "MoveRelative":
            pip = get_pip(step["PipetteMount"])
            axis = step.get("Axis", "z")
            distance = step["Distance"]
            speed = step.get("Speed")
            # API 2.22+ robot control - using relative coordinates
            if hasattr(pip, 'move_to'):
                # Get current position and move relative
                curr = pip.current_location
                if curr:
                    from opentrons.types import Point
                    offset = Point(
                        x=distance if axis == "x" else 0,
                        y=distance if axis == "y" else 0,
                        z=distance if axis == "z" else 0
                    )
                    new_loc = curr.move(offset)
                    if speed:
                        pip.move_to(new_loc, speed=speed)
                    else:
                        pip.move_to(new_loc)

        # ═══════════════════════════════════════════════════════════════════
        #               HEATER-SHAKER MODULE COMMANDS
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "HeaterShaker":
            hs = objects["Modules"][step["ModuleID"]]
            if step.get("CloseLatch"): hs.close_labware_latch()
            if "Temperature" in step: hs.set_target_temperature(step["Temperature"])
            if step.get("WaitForTemp"): hs.wait_for_temperature()
            if "RPM" in step: hs.set_and_wait_for_shake_speed(step["RPM"])
            if "Duration" in step: ctx.delay(seconds=step["Duration"])
            if step.get("StopShaking") or step.get("OpenLatch") or not step.get("KeepShaking", False):
                try: hs.deactivate_shaker()
                except: pass
            if step.get("DeactivateHeater"): hs.deactivate_heater()
            if step.get("OpenLatch"): hs.open_labware_latch()

        elif cmd == "HeaterShakerSetTemperature":
            hs = objects["Modules"][step["ModuleID"]]
            hs.set_target_temperature(step["Temperature"])

        elif cmd == "HeaterShakerWaitForTemperature":
            hs = objects["Modules"][step["ModuleID"]]
            hs.wait_for_temperature()

        elif cmd == "HeaterShakerSetShake":
            hs = objects["Modules"][step["ModuleID"]]
            hs.set_and_wait_for_shake_speed(step["RPM"])

        elif cmd == "HeaterShakerDeactivateShaker":
            hs = objects["Modules"][step["ModuleID"]]
            hs.deactivate_shaker()

        elif cmd == "HeaterShakerDeactivateHeater":
            hs = objects["Modules"][step["ModuleID"]]
            hs.deactivate_heater()

        elif cmd == "HeaterShakerOpenLatch":
            hs = objects["Modules"][step["ModuleID"]]
            hs.open_labware_latch()

        elif cmd == "HeaterShakerCloseLatch":
            hs = objects["Modules"][step["ModuleID"]]
            hs.close_labware_latch()

        # ═══════════════════════════════════════════════════════════════════
        #               THERMOCYCLER MODULE COMMANDS
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "Thermocycler":
            tc = objects["Modules"][step["ModuleID"]]
            if step.get("OpenLid"): tc.open_lid()
            if step.get("CloseLid"): tc.close_lid()
            if "LidTemperature" in step: tc.set_lid_temperature(step["LidTemperature"])
            if "BlockTemperature" in step:
                hold_time = step.get("HoldTime", 0)
                block_max_vol = step.get("BlockMaxVolume")
                if block_max_vol:
                    tc.set_block_temperature(step["BlockTemperature"], hold_time_seconds=hold_time, block_max_volume=block_max_vol)
                else:
                    tc.set_block_temperature(step["BlockTemperature"], hold_time_seconds=hold_time)
            if step.get("DeactivateLid"): tc.deactivate_lid()
            if step.get("DeactivateBlock"): tc.deactivate_block()

        elif cmd == "ThermocyclerRunProfile":
            tc = objects["Modules"][step["ModuleID"]]
            # Profile format: [{"temperature": 95, "hold_time_seconds": 30}, ...]
            profile = step["Profile"]
            repetitions = step.get("Repetitions", 1)
            block_max_vol = step.get("BlockMaxVolume")
            if block_max_vol:
                tc.execute_profile(steps=profile, repetitions=repetitions, block_max_volume=block_max_vol)
            else:
                tc.execute_profile(steps=profile, repetitions=repetitions)

        elif cmd == "ThermocyclerOpenLid":
            tc = objects["Modules"][step["ModuleID"]]
            tc.open_lid()

        elif cmd == "ThermocyclerCloseLid":
            tc = objects["Modules"][step["ModuleID"]]
            tc.close_lid()

        elif cmd == "ThermocyclerSetLidTemperature":
            tc = objects["Modules"][step["ModuleID"]]
            tc.set_lid_temperature(step["Temperature"])

        elif cmd == "ThermocyclerSetBlockTemperature":
            tc = objects["Modules"][step["ModuleID"]]
            hold_time = step.get("HoldTime", 0)
            tc.set_block_temperature(step["Temperature"], hold_time_seconds=hold_time)

        elif cmd == "ThermocyclerDeactivate":
            tc = objects["Modules"][step["ModuleID"]]
            if step.get("DeactivateLid", True): tc.deactivate_lid()
            if step.get("DeactivateBlock", True): tc.deactivate_block()

        # ═══════════════════════════════════════════════════════════════════
        #               TEMPERATURE MODULE COMMANDS
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "TemperatureModule":
            temp_mod = objects["Modules"][step["ModuleID"]]
            if "Temperature" in step:
                temp_mod.set_temperature(step["Temperature"])
            if step.get("WaitForTemp"): 
                temp_mod.await_temperature(step.get("Temperature"))
            if step.get("Deactivate"): 
                temp_mod.deactivate()

        elif cmd == "TemperatureModuleSetTemperature":
            temp_mod = objects["Modules"][step["ModuleID"]]
            temp_mod.set_temperature(step["Temperature"])

        elif cmd == "TemperatureModuleWaitForTemperature":
            temp_mod = objects["Modules"][step["ModuleID"]]
            temp_mod.await_temperature(step.get("Temperature"))

        elif cmd == "TemperatureModuleDeactivate":
            temp_mod = objects["Modules"][step["ModuleID"]]
            temp_mod.deactivate()

        # ═══════════════════════════════════════════════════════════════════
        #               MAGNETIC MODULE/BLOCK COMMANDS
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "MagneticModule":
            mag = objects["Modules"][step["ModuleID"]]
            if step.get("Engage"):
                height = step.get("Height")
                if height is not None:
                    mag.engage(height_from_base=height)
                else:
                    mag.engage()
            if step.get("Disengage"):
                mag.disengage()

        elif cmd == "MagneticModuleEngage":
            mag = objects["Modules"][step["ModuleID"]]
            height = step.get("Height")
            if height is not None:
                mag.engage(height_from_base=height)
            else:
                mag.engage()

        elif cmd == "MagneticModuleDisengage":
            mag = objects["Modules"][step["ModuleID"]]
            mag.disengage()

        # ═══════════════════════════════════════════════════════════════════
        #               ABSORBANCE READER MODULE COMMANDS (API 2.21+)
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "AbsorbanceReader":
            reader = objects["Modules"][step["ModuleID"]]
            if step.get("OpenLid"): reader.open_lid()
            if step.get("CloseLid"): reader.close_lid()
            if "Initialize" in step:
                wavelengths = step["Initialize"].get("Wavelengths", [450])
                mode = step["Initialize"].get("Mode", "single")
                reference = step["Initialize"].get("ReferenceWavelength")
                if reference:
                    reader.initialize(mode, wavelengths, reference_wavelength=reference)
                else:
                    reader.initialize(mode, wavelengths)
            if step.get("Read"):
                export_fn = step.get("ExportFilename")
                if export_fn:
                    result = reader.read(export_filename=export_fn)
                else:
                    result = reader.read()
                ctx.comment(f"Absorbance read complete: {result}")

        elif cmd == "AbsorbanceReaderInitialize":
            reader = objects["Modules"][step["ModuleID"]]
            wavelengths = step.get("Wavelengths", [450])
            mode = step.get("Mode", "single")
            reference = step.get("ReferenceWavelength")
            if reference:
                reader.initialize(mode, wavelengths, reference_wavelength=reference)
            else:
                reader.initialize(mode, wavelengths)

        elif cmd == "AbsorbanceReaderRead":
            reader = objects["Modules"][step["ModuleID"]]
            export_fn = step.get("ExportFilename")
            if export_fn:
                reader.read(export_filename=export_fn)
            else:
                reader.read()

        elif cmd == "AbsorbanceReaderOpenLid":
            reader = objects["Modules"][step["ModuleID"]]
            reader.open_lid()

        elif cmd == "AbsorbanceReaderCloseLid":
            reader = objects["Modules"][step["ModuleID"]]
            reader.close_lid()

        # ═══════════════════════════════════════════════════════════════════
        #               FLEX STACKER MODULE COMMANDS (API 2.23+)
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "FlexStackerRetrieve":
            stacker = objects["Modules"][step["ModuleID"]]
            labware_id = step.get("LabwareID")
            if labware_id and labware_id in objects["Labware"]:
                stacker.retrieve(objects["Labware"][labware_id])
            else:
                stacker.retrieve()

        elif cmd == "FlexStackerStore":
            stacker = objects["Modules"][step["ModuleID"]]
            labware_id = step.get("LabwareID")
            if labware_id and labware_id in objects["Labware"]:
                stacker.store(objects["Labware"][labware_id])
            else:
                stacker.store()

        # ═══════════════════════════════════════════════════════════════════
        #               PIPETTE CONFIGURATION COMMANDS
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "ConfigureNozzleLayout":
            pip = get_pip(step["PipetteMount"])
            layout = step.get("Layout", "ALL")
            start = step.get("Start")
            end = step.get("End")
            front_right = step.get("FrontRight")
            back_left = step.get("BackLeft")
            
            # Import nozzle layout configurations
            from opentrons.protocol_api import (
                ALL, COLUMN, ROW, SINGLE, PARTIAL_COLUMN, QUADRANT
            )
            
            layout_map = {
                "ALL": ALL, "COLUMN": COLUMN, "ROW": ROW, 
                "SINGLE": SINGLE, "PARTIAL_COLUMN": PARTIAL_COLUMN, "QUADRANT": QUADRANT
            }
            
            layout_style = layout_map.get(layout.upper(), ALL)
            
            kwargs = {"style": layout_style}
            if start: kwargs["start"] = start
            if end: kwargs["end"] = end
            if front_right: kwargs["front_right"] = front_right
            if back_left: kwargs["back_left"] = back_left
            
            pip.configure_nozzle_layout(**kwargs)

        elif cmd == "ConfigureForVolume":
            pip = get_pip(step["PipetteMount"])
            volume = step["Volume"]
            pip.configure_for_volume(volume)

        # ═══════════════════════════════════════════════════════════════════
        #               LIQUID DEFINITION COMMANDS
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "DefineLiquid":
            name = step["Name"]
            color = step.get("Color", "#000000")
            description = step.get("Description", "")
            liquid = ctx.define_liquid(name=name, display_color=color, description=description)
            objects["Liquids"][name] = liquid

        elif cmd == "LoadLiquid":
            liquid = objects["Liquids"].get(step["LiquidID"])
            labware = get_loc(step["Labware"])
            wells = step.get("Wells", ["A1"])
            volume = step["Volume"]
            for well in wells:
                labware[well].load_liquid(liquid, volume)

        # ═══════════════════════════════════════════════════════════════════
        #               CONTROL & FLOW COMMANDS
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "Home": 
            ctx.home()

        elif cmd == "Comment": 
            ctx.comment(step["Text"])

        elif cmd == "Delay": 
            minutes = step.get("Minutes", 0)
            seconds = step.get("Seconds", 0)
            total_seconds = minutes * 60 + seconds
            ctx.delay(seconds=total_seconds)

        elif cmd == "Pause":
            msg = step.get("Message") or step.get("Text", "Protocol paused")
            ctx.pause(msg)

        elif cmd == "WaitForResume":
            msg = step.get("Message", "Waiting for resume")
            ctx.pause(msg)

        elif cmd == "SetRailLights":
            on = step.get("On", True)
            ctx.set_rail_lights(on)

        elif cmd == "TakeSnapshot":
            f = step.get("Filename", "snapshot.jpg"); p = f"/var/lib/jupyter/{f}"
            subprocess.run(["gst-launch-1.0", "v4l2src", "device=/dev/video2", "num-buffers=1", "!", "jpegenc", "!", "filesink", f"location={p}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(p):
                with open(p, "rb") as fh: ctx.comment(f"IMG_START:{f}"); ctx.comment(base64.b64encode(fh.read()).decode('utf-8')); ctx.comment("IMG_END")

        # ═══════════════════════════════════════════════════════════════════
        #               DYNAMIC PIPETTING COMMANDS (API 2.27+)
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "DynamicAspirate":
            pip = get_pip(step["PipetteMount"])
            loc = get_loc(step["Location"])
            vol = step["Volume"]
            # Start and end positions for dynamic tracking
            start_position = step.get("StartPosition", "well_bottom")
            end_position = step.get("EndPosition", "meniscus")
            pip.aspirate(vol, loc)  # API handles meniscus tracking if enabled

        elif cmd == "DynamicDispense":
            pip = get_pip(step["PipetteMount"])
            loc = get_loc(step["Location"])
            vol = step["Volume"]
            pip.dispense(vol, loc)

        elif cmd == "DynamicMix":
            pip = get_pip(step["PipetteMount"])
            loc = get_loc(step["Location"])
            vol = step.get("Volume", pip.max_volume * 0.8)
            reps = step.get("Repetitions", 3)
            # Dynamic mix allows different locations per cycle
            locations = step.get("Locations", [])
            if locations:
                for i, mix_loc in enumerate(locations[:reps]):
                    l = get_loc(mix_loc)
                    pip.aspirate(vol, l)
                    pip.dispense(vol, l)
            else:
                pip.mix(reps, vol, loc)

        # ═══════════════════════════════════════════════════════════════════
        #               CONCURRENT MODULE COMMANDS (API 2.27+)
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "ConcurrentHeaterShaker":
            hs = objects["Modules"][step["ModuleID"]]
            # Run in non-blocking mode
            if "Temperature" in step:
                hs.set_target_temperature(step["Temperature"])
            if "RPM" in step:
                hs.set_and_wait_for_shake_speed(step["RPM"])
            # Don't wait - allow concurrent operations

        elif cmd == "ConcurrentThermocycler":
            tc = objects["Modules"][step["ModuleID"]]
            if "LidTemperature" in step:
                tc.set_lid_temperature(step["LidTemperature"])
            if "BlockTemperature" in step:
                tc.set_block_temperature(step["BlockTemperature"])
            # Don't wait - allow concurrent operations

        elif cmd == "ConcurrentTemperatureModule":
            temp_mod = objects["Modules"][step["ModuleID"]]
            temp_mod.set_temperature(step["Temperature"])
            # Don't wait - allow concurrent operations

        # ═══════════════════════════════════════════════════════════════════
        #               IN-PLACE LIQUID HANDLING (API 2.20+)
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "AspirateInPlace":
            pip = get_pip(step["PipetteMount"])
            vol = step["Volume"]
            rate = step.get("Rate", 1.0)
            pip.aspirate(vol, rate=rate)

        elif cmd == "DispenseInPlace":
            pip = get_pip(step["PipetteMount"])
            vol = step["Volume"]
            rate = step.get("Rate", 1.0)
            push_out = step.get("PushOut")
            if push_out:
                pip.dispense(vol, rate=rate, push_out=push_out)
            else:
                pip.dispense(vol, rate=rate)

        elif cmd == "BlowOutInPlace":
            pip = get_pip(step["PipetteMount"])
            pip.blow_out()

        elif cmd == "AirGapInPlace":
            pip = get_pip(step["PipetteMount"])
            vol = step.get("Volume", pip.min_volume)
            pip.air_gap(vol)

        elif cmd == "DropTipInPlace":
            pip = get_pip(step["PipetteMount"])
            home_after = step.get("HomeAfter", True)
            try:
                pip.drop_tip(home_after=home_after)
            except:
                pip.drop_tip()

        # ═══════════════════════════════════════════════════════════════════
        #               TIP PRESENCE & TRACKING (API 2.20+)
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "VerifyTipPresence":
            pip = get_pip(step["PipetteMount"])
            expected = step.get("ExpectedState", "present")
            from opentrons.protocol_api import TipPresenceStatus
            status = pip.detect_tip_presence()
            if expected == "present" and status != TipPresenceStatus.PRESENT:
                raise RuntimeError(f"Expected tip present but got: {status}")
            elif expected == "absent" and status != TipPresenceStatus.ABSENT:
                raise RuntimeError(f"Expected tip absent but got: {status}")
            ctx.comment(f"Tip presence verified: {status}")

        elif cmd == "GetTipPresence":
            pip = get_pip(step["PipetteMount"])
            status = pip.detect_tip_presence()
            ctx.comment(f"Tip presence: {status}")

        elif cmd == "GetNextTip":
            pip = get_pip(step["PipetteMount"])
            tip_rack = step.get("TipRack")
            starting_tip = step.get("StartingTip")
            if tip_rack:
                rack = get_loc(tip_rack)
                if starting_tip:
                    pip.starting_tip = rack[starting_tip]
            next_tip = pip.next_tip()
            ctx.comment(f"Next available tip: {next_tip}")

        # ═══════════════════════════════════════════════════════════════════
        #               LIQUID LEVEL DETECTION (API 2.20+)
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "LiquidProbe":
            pip = get_pip(step["PipetteMount"])
            loc = get_loc(step["Location"])
            well_location = step.get("WellLocation", "top")
            try:
                height = pip.detect_liquid_presence(loc)
                ctx.comment(f"Liquid detected at height: {height}")
            except Exception as e:
                ctx.comment(f"Liquid probe error: {e}")

        elif cmd == "TryLiquidProbe":
            pip = get_pip(step["PipetteMount"])
            loc = get_loc(step["Location"])
            try:
                height = pip.detect_liquid_presence(loc)
                ctx.comment(f"Liquid detected at height: {height}")
            except:
                ctx.comment("No liquid detected (try probe)")

        # ═══════════════════════════════════════════════════════════════════
        #               ADVANCED MOVEMENT COMMANDS
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "MoveToCoordinates":
            pip = get_pip(step["PipetteMount"])
            from opentrons.types import Point
            coords = Point(x=step["X"], y=step["Y"], z=step["Z"])
            speed = step.get("Speed")
            min_z = step.get("MinimumZHeight")
            pip.move_to(coords, speed=speed, minimum_z_height=min_z)

        elif cmd == "MoveToAddressableArea":
            pip = get_pip(step["PipetteMount"])
            area = step["AddressableArea"]
            offset = step.get("Offset", [0, 0, 0])
            speed = step.get("Speed")
            from opentrons.types import Point
            offset_point = Point(x=offset[0], y=offset[1], z=offset[2]) if isinstance(offset, list) else None
            # API 2.27+ supports addressable areas
            try:
                dest = ctx.deck.get_addressable_area(area)
                if offset_point:
                    dest = dest.move(offset_point)
                pip.move_to(dest, speed=speed)
            except:
                ctx.comment(f"Addressable area '{area}' not available")

        elif cmd == "RetractAxis":
            axis = step.get("Axis", "z")
            mount = step.get("Mount")
            # Retract specified axis to safe position
            ctx.comment(f"Retracting axis: {axis}")
            ctx.home()  # Full home as fallback

        elif cmd == "SavePosition":
            pip = get_pip(step["PipetteMount"])
            pos_id = step.get("PositionId", "saved_position")
            current_pos = pip.current_location
            ctx.comment(f"Saved position '{pos_id}': {current_pos}")

        # ═══════════════════════════════════════════════════════════════════
        #               FLOW RATE CONFIGURATION
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "SetFlowRates":
            pip = get_pip(step["PipetteMount"])
            if "Aspirate" in step:
                pip.flow_rate.aspirate = step["Aspirate"]
            if "Dispense" in step:
                pip.flow_rate.dispense = step["Dispense"]
            if "BlowOut" in step:
                pip.flow_rate.blow_out = step["BlowOut"]
            ctx.comment(f"Flow rates set: asp={pip.flow_rate.aspirate}, disp={pip.flow_rate.dispense}")

        # ═══════════════════════════════════════════════════════════════════
        #               LABWARE MANAGEMENT (API 2.21+)
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "LoadLiquidClass":
            lc_name = step["Name"]
            try:
                lc = ctx.get_liquid_class(name=lc_name)
                objects["LiquidClasses"][lc_name] = lc
                ctx.comment(f"Loaded native liquid class: {lc_name}")
            except Exception as e:
                ctx.comment(f"Failed to load liquid class '{lc_name}': {e}")

        elif cmd == "LoadLidStack":
            slot = step["Slot"]
            quantity = step.get("Quantity", 5)
            load_name = step.get("LoadName", "opentrons_tough_pcr_auto_sealing_lid")
            adapter = step.get("Adapter")
            try:
                if adapter:
                    adapter_obj = ctx.load_adapter(adapter, slot)
                    lid_stack = adapter_obj.load_lid_stack(load_name, quantity)
                else:
                    lid_stack = ctx.load_lid_stack(load_name, slot, quantity)
                objects["Labware"][f"LidStack_{slot}"] = lid_stack
                ctx.comment(f"Loaded lid stack: {quantity} lids at {slot}")
            except Exception as e:
                ctx.comment(f"Failed to load lid stack: {e}")

        elif cmd == "LoadLid":
            labware_id = step["LabwareID"]
            labware = get_loc(labware_id)
            try:
                labware.load_lid()
                ctx.comment(f"Loaded lid on {labware_id}")
            except Exception as e:
                ctx.comment(f"Failed to load lid: {e}")

        elif cmd == "ReloadLabware":
            labware_id = step["LabwareID"]
            labware = get_loc(labware_id)
            try:
                ctx.reload_labware(labware)
                ctx.comment(f"Reloaded labware: {labware_id}")
            except Exception as e:
                ctx.comment(f"Failed to reload labware: {e}")

        # ═══════════════════════════════════════════════════════════════════
        #               FLEX STACKER ADVANCED COMMANDS (API 2.23+)
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "FlexStackerFill":
            stacker = objects["Modules"][step["ModuleID"]]
            labware_id = step.get("LabwareID")
            count = step.get("Count", 1)
            strategy = step.get("Strategy", "manual")
            for i in range(count):
                try:
                    if labware_id and labware_id in objects["Labware"]:
                        stacker.store(objects["Labware"][labware_id])
                    else:
                        stacker.store()
                    ctx.comment(f"Stored plate {i+1}/{count}")
                except Exception as e:
                    ctx.comment(f"Fill error at {i+1}: {e}")
                    break

        elif cmd == "FlexStackerEmpty":
            stacker = objects["Modules"][step["ModuleID"]]
            strategy = step.get("Strategy", "manual")
            count = 0
            while True:
                try:
                    stacker.retrieve()
                    count += 1
                    ctx.comment(f"Retrieved plate {count}")
                except:
                    break
            ctx.comment(f"Emptied {count} plates from stacker")

        # ═══════════════════════════════════════════════════════════════════
        #               THERMOCYCLER WAIT COMMANDS
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "ThermocyclerWaitForLidTemperature":
            tc = objects["Modules"][step["ModuleID"]]
            tc.wait_for_lid_temperature()

        elif cmd == "ThermocyclerWaitForBlockTemperature":
            tc = objects["Modules"][step["ModuleID"]]
            tc.wait_for_block_temperature()

        # ═══════════════════════════════════════════════════════════════════
        #               UI & STATUS COMMANDS
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "SetStatusBar":
            status = step.get("Status", "idle")
            color = step.get("Color")
            try:
                from opentrons.protocol_api import StatusBarState
                state_map = {
                    "idle": StatusBarState.IDLE,
                    "running": StatusBarState.RUNNING,
                    "paused": StatusBarState.PAUSED,
                    "error": StatusBarState.HARDWARE_ERROR,
                    "confirmation": StatusBarState.CONFIRMATION,
                }
                state = state_map.get(status, StatusBarState.IDLE)
                ctx.set_status_bar(state)
            except:
                ctx.comment(f"Status bar set to: {status}")

        elif cmd == "WaitForDuration":
            seconds = step.get("Seconds", 0)
            ctx.delay(seconds=seconds)

        # ═══════════════════════════════════════════════════════════════════
        #               CUSTOM/FALLBACK COMMAND
        # ═══════════════════════════════════════════════════════════════════

        elif cmd == "CustomCommand":
            name = step.get("Name", "custom")
            params = step.get("Params", {})
            ctx.comment(f"Custom command: {name} with params: {params}")
            # Execute custom logic if defined
            if "PythonCode" in step:
                exec(step["PythonCode"], {"ctx": ctx, "objects": objects, "get_loc": get_loc, "step": step})

        else:
            ctx.comment(f"Unknown command: {cmd}")

    ctx.comment("Done.")
'''


# ═══════════════════════════════════════════════════════════════════════════
#                        RECIPE VALIDATOR
# ═══════════════════════════════════════════════════════════════════════════

class RecipeValidator:
    """
    Validates JSON recipes against schema before generation.
    
    Checks:
    - Required fields present
    - Field types correct
    - References valid (labware, modules, pipettes)
    - Logical consistency
    """
    
    def __init__(self):
        self.errors: List[ValidationError] = []
        self.warnings: List[ValidationError] = []
        self._labware_ids: set = set()
        self._module_ids: set = set()
        self._pipette_mounts: set = set()
        self._liquid_ids: set = set()
        
    def validate(self, recipe: dict) -> Tuple[bool, List[ValidationError], List[ValidationError]]:
        """
        Validate a recipe dictionary.
        
        Returns:
            Tuple of (is_valid, errors, warnings)
        """
        self.errors = []
        self.warnings = []
        self._labware_ids = set()
        self._module_ids = set()
        self._pipette_mounts = set()
        self._liquid_ids = set()
        
        # Validate top-level structure
        self._validate_structure(recipe)
        
        # Collect defined IDs
        self._collect_definitions(recipe)
        
        # Validate steps
        if "Steps" in recipe:
            self._validate_steps(recipe["Steps"])
        
        return len(self.errors) == 0, self.errors, self.warnings
    
    def _add_error(self, step_idx: Optional[int], step_id: Optional[str], 
                   command: Optional[str], field: str, message: str):
        self.errors.append(ValidationError(step_idx, step_id, command, field, message, "error"))
        
    def _add_warning(self, step_idx: Optional[int], step_id: Optional[str],
                     command: Optional[str], field: str, message: str):
        self.warnings.append(ValidationError(step_idx, step_id, command, field, message, "warning"))
    
    def _validate_structure(self, recipe: dict):
        """Validate top-level structure."""
        # ProtocolName is recommended
        if "ProtocolName" not in recipe:
            self._add_warning(None, None, None, "ProtocolName", "Protocol name not specified")
        
        # Must have at least one of: Steps, Pipettes
        if "Steps" not in recipe:
            self._add_error(None, None, None, "Steps", "No steps defined in recipe")
        elif not isinstance(recipe["Steps"], list):
            self._add_error(None, None, None, "Steps", "Steps must be an array")
        elif len(recipe["Steps"]) == 0:
            self._add_warning(None, None, None, "Steps", "Recipe has no steps")
        
        # Validate Modules
        if "Modules" in recipe:
            for mod_id, mod_def in recipe["Modules"].items():
                if "Type" not in mod_def:
                    self._add_error(None, None, None, f"Modules.{mod_id}.Type", "Module type not specified")
                elif mod_def["Type"] not in VALID_MODULE_TYPES:
                    self._add_warning(None, None, None, f"Modules.{mod_id}.Type", 
                                     f"Unknown module type: {mod_def['Type']}")
                if "Slot" not in mod_def:
                    self._add_error(None, None, None, f"Modules.{mod_id}.Slot", "Module slot not specified")
        
        # Validate Pipettes
        if "Pipettes" in recipe:
            for mount, pip_def in recipe["Pipettes"].items():
                if mount not in ["left", "right"]:
                    self._add_error(None, None, None, f"Pipettes.{mount}", 
                                   "Pipette mount must be 'left' or 'right'")
                pip_name = pip_def if isinstance(pip_def, str) else pip_def.get("Name", "")
                if pip_name not in VALID_PIPETTES:
                    self._add_warning(None, None, None, f"Pipettes.{mount}", 
                                     f"Unknown pipette: {pip_name}")
        
        # Validate Labware
        if "Labware" in recipe:
            for lw_id, lw_def in recipe["Labware"].items():
                if "LoadName" not in lw_def and "Type" not in lw_def:
                    self._add_error(None, None, None, f"Labware.{lw_id}", 
                                   "Labware must have LoadName or Type")
                if "Slot" not in lw_def and "OnModule" not in lw_def and "OnAdapter" not in lw_def:
                    self._add_error(None, None, None, f"Labware.{lw_id}", 
                                   "Labware must have Slot, OnModule, or OnAdapter")
    
    def _collect_definitions(self, recipe: dict):
        """Collect all defined IDs for reference validation."""
        if "Labware" in recipe:
            self._labware_ids = set(recipe["Labware"].keys())
        if "Modules" in recipe:
            self._module_ids = set(recipe["Modules"].keys())
        if "Pipettes" in recipe:
            self._pipette_mounts = set(recipe["Pipettes"].keys())
        if "Liquids" in recipe:
            self._liquid_ids = set(recipe["Liquids"].keys())
        if "Trash" in recipe:
            self._labware_ids.update(recipe["Trash"].keys())
    
    def _validate_steps(self, steps: list):
        """Validate all steps."""
        for idx, step in enumerate(steps):
            self._validate_step(idx, step)
    
    def _validate_step(self, idx: int, step: dict):
        """Validate a single step."""
        step_id = step.get("StepID") or step.get("ID")
        
        # Must have Command
        if "Command" not in step:
            self._add_error(idx, step_id, None, "Command", "Step has no Command")
            return
        
        cmd = step["Command"]
        
        # Check if command is known
        if cmd not in COMMAND_SCHEMA:
            # Check if it's a known command with different casing
            cmd_lower = cmd.lower()
            known_cmds = [c for c in COMMAND_SCHEMA.keys() if c.lower() == cmd_lower]
            if known_cmds:
                self._add_warning(idx, step_id, cmd, "Command", 
                                 f"Command case mismatch. Use '{known_cmds[0]}'")
            else:
                self._add_warning(idx, step_id, cmd, "Command", 
                                 f"Unknown command: {cmd}. Will use fallback handler.")
            return
        
        schema = COMMAND_SCHEMA[cmd]
        
        # Check required fields
        for field in schema["required"]:
            if field not in step:
                self._add_error(idx, step_id, cmd, field, f"Required field '{field}' missing")
        
        # Check location fields - at least one must be present if command uses locations
        if "location_fields" in schema:
            loc_fields = schema["location_fields"]
            has_location = any(f in step for f in loc_fields)
            if not has_location:
                self._add_warning(idx, step_id, cmd, "Location",
                                f"No location specified. One of {loc_fields} should be provided.")
        
        # Validate references
        self._validate_references(idx, step_id, cmd, step)
    
    def _validate_references(self, idx: int, step_id: Optional[str], cmd: str, step: dict):
        """Validate that referenced IDs exist."""
        # Check PipetteMount
        if "PipetteMount" in step:
            mount = step["PipetteMount"]
            if mount not in self._pipette_mounts and self._pipette_mounts:
                self._add_error(idx, step_id, cmd, "PipetteMount", 
                               f"Unknown pipette mount: {mount}. Available: {self._pipette_mounts}")
        
        # Check ModuleID
        if "ModuleID" in step:
            mod_id = step["ModuleID"]
            if mod_id not in self._module_ids and self._module_ids:
                self._add_error(idx, step_id, cmd, "ModuleID",
                               f"Unknown module: {mod_id}. Available: {self._module_ids}")
        
        # Check labware references
        for field in ["Labware", "LabwareID", "TipRack", "Source", "Dest", "Destination"]:
            if field in step:
                ref = step[field]
                if isinstance(ref, str) and ":" in ref:
                    ref = ref.split(":")[0]
                if isinstance(ref, str) and ref not in self._labware_ids and self._labware_ids:
                    # Could be a special reference like "default"
                    if ref not in ["default", "MyTrash"]:
                        self._add_warning(idx, step_id, cmd, field,
                                         f"Labware '{ref}' not defined in Labware section")
        
        # Check liquid references
        if "LiquidID" in step:
            liquid = step["LiquidID"]
            if liquid not in self._liquid_ids and self._liquid_ids:
                self._add_error(idx, step_id, cmd, "LiquidID",
                               f"Unknown liquid: {liquid}. Define it first with DefineLiquid")


# ═══════════════════════════════════════════════════════════════════════════
#                        PROTOCOL SIMULATOR
# ═══════════════════════════════════════════════════════════════════════════

class ProtocolSimulator:
    """
    Simulates protocols locally using opentrons_simulate.
    
    This catches runtime errors BEFORE uploading to the robot.
    """
    
    def __init__(self):
        self._opentrons_available = self._check_opentrons()
    
    def _check_opentrons(self) -> bool:
        """Check if opentrons package is available."""
        try:
            import opentrons.simulate
            return True
        except ImportError:
            logger.warning("opentrons package not installed. Simulation disabled.")
            return False
    
    def simulate(self, code: str, line_map: Dict[int, int]) -> SimulationResult:
        """
        Simulate protocol and return results.
        
        Args:
            code: Python protocol code
            line_map: Mapping from Python line numbers to step indices
            
        Returns:
            SimulationResult with any errors
        """
        if not self._opentrons_available:
            return SimulationResult(
                success=True,
                warnings=[ValidationError(None, None, None, "simulation", 
                                         "Simulation skipped - opentrons not installed")],
                output="Simulation skipped"
            )
        
        # Write to temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(code)
            temp_path = f.name
        
        try:
            # Run opentrons_simulate
            result = subprocess.run(
                ['python', '-m', 'opentrons.simulate', temp_path],
                capture_output=True,
                text=True,
                timeout=120
            )
            
            if result.returncode == 0:
                # Parse output for duration estimate
                duration = self._parse_duration(result.stdout)
                return SimulationResult(
                    success=True,
                    output=result.stdout,
                    duration_estimate=duration
                )
            else:
                # Parse errors
                errors = self._parse_errors(result.stderr, line_map)
                return SimulationResult(
                    success=False,
                    errors=errors,
                    output=result.stdout + result.stderr
                )
                
        except subprocess.TimeoutExpired:
            return SimulationResult(
                success=False,
                errors=[ValidationError(None, None, None, "simulation",
                                       "Simulation timed out after 120 seconds")],
                output="Timeout"
            )
        except Exception as e:
            return SimulationResult(
                success=False,
                errors=[ValidationError(None, None, None, "simulation", str(e))],
                output=str(e)
            )
        finally:
            # Cleanup
            try:
                os.unlink(temp_path)
            except:
                pass
    
    def _parse_duration(self, output: str) -> Optional[float]:
        """Parse estimated duration from simulation output."""
        # Look for duration in output
        match = re.search(r'Estimated duration:\s*([\d.]+)\s*(?:seconds|minutes|hours)', output, re.I)
        if match:
            return float(match.group(1))
        return None
    
    def _parse_errors(self, stderr: str, line_map: Dict[int, int]) -> List[ValidationError]:
        """Parse errors from simulation stderr and map to JSON steps."""
        errors = []
        
        # Find Python traceback line numbers
        line_matches = re.findall(r'File "[^"]+", line (\d+)', stderr)
        
        # Extract the actual error message
        error_match = re.search(r'(\w+Error): (.+)$', stderr, re.MULTILINE)
        error_msg = error_match.group(0) if error_match else stderr.split('\n')[-1]
        
        # Map to step if possible
        step_idx = None
        for line_num_str in reversed(line_matches):
            line_num = int(line_num_str)
            if line_num in line_map:
                step_idx = line_map[line_num]
                break
        
        errors.append(ValidationError(
            step_index=step_idx,
            step_id=None,
            command=None,
            field="simulation",
            message=error_msg
        ))
        
        return errors


# ═══════════════════════════════════════════════════════════════════════════
#                    LIQUID CLASS LIBRARY LOADER
# ═══════════════════════════════════════════════════════════════════════════

def load_liquid_classes_from_library(library_path: Optional[str] = None) -> dict:
    """
    Load liquid classes from Library/LiquidClasses/ folder.
    
    Args:
        library_path: Path to Library folder (auto-detected if None)
        
    Returns:
        Dictionary of {name: parameters} for all liquid classes
        
    Environment:
        BICOCCALAB_LIBRARY_PATH: Override path to Library folder
    """
    liquid_classes = {}
    
    # Priority 1: Environment variable
    env_library = os.environ.get("BICOCCALAB_LIBRARY_PATH")
    if env_library:
        library_path = os.path.join(env_library, "LiquidClasses")
    
    # Priority 2: Auto-detect from relative paths
    if library_path is None:
        # Try relative paths from common locations
        candidates = [
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "Library", "LiquidClasses"),
            os.path.join(os.getcwd(), "Library", "LiquidClasses"),
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "Library", "LiquidClasses"),
        ]
        for candidate in candidates:
            candidate = os.path.abspath(candidate)
            if os.path.isdir(candidate):
                library_path = candidate
                break
    
    if not library_path or not os.path.isdir(library_path):
        logger.warning(f"LiquidClasses library not found, using defaults only")
        return liquid_classes
    
    # Load all .json files (except schema)
    for filename in os.listdir(library_path):
        if filename.endswith('.json') and not filename.endswith('.schema.json'):
            filepath = os.path.join(library_path, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    lc_data = json.load(f)
                
                name = lc_data.get("name", filename.replace(".json", ""))
                params = lc_data.get("parameters", {})
                
                # Add loadName if present
                if "loadName" in lc_data:
                    params["load_name"] = lc_data["loadName"]
                
                # Add description if present
                if "description" in lc_data:
                    params["description"] = lc_data["description"]
                
                liquid_classes[name] = params
                logger.debug(f"Loaded liquid class: {name} from {filename}")
                
            except Exception as e:
                logger.warning(f"Failed to load liquid class from {filename}: {e}")
    
    logger.info(f"Loaded {len(liquid_classes)} liquid classes from library")
    return liquid_classes


class ProtocolGenerator:
    """
    Generates Python protocols from JSON recipes using the Universal Template.
    
    The Universal Template is a flexible Python protocol that interprets
    JSON settings at runtime, supporting all Opentrons Flex features.
    
    Improvements over basic generation:
    - JSON Schema validation before generation
    - Local simulation with opentrons_simulate  
    - Line mapping from Python errors to JSON step IDs
    - Comprehensive error reporting
    """
    
    def __init__(self, temp_dir: str = "./temp", enable_simulation: bool = True):
        """
        Initialize the generator.
        
        Args:
            temp_dir: Directory for temporary protocol files
            enable_simulation: Whether to run local simulation
        """
        self.temp_dir = temp_dir
        self.enable_simulation = enable_simulation
        os.makedirs(temp_dir, exist_ok=True)
        
        self.validator = RecipeValidator()
        self.simulator = ProtocolSimulator() if enable_simulation else None
        
    def generate_and_validate(self, json_recipe: str) -> GenerationResult:
        """
        Generate and validate a protocol with full error checking.
        
        This is the recommended method - it:
        1. Validates JSON schema
        2. Generates Python code with line mapping
        3. Simulates locally (if enabled)
        4. Returns detailed results
        
        Args:
            json_recipe: JSON recipe string
            
        Returns:
            GenerationResult with code, errors, and simulation results
        """
        result = GenerationResult(success=False)
        
        # Parse JSON
        try:
            recipe = json.loads(json_recipe)
        except json.JSONDecodeError as e:
            result.errors.append(ValidationError(
                None, None, None, "JSON", f"Invalid JSON: {e}"
            ))
            return result
        
        # Validate schema
        is_valid, errors, warnings = self.validator.validate(recipe)
        result.errors.extend(errors)
        result.warnings.extend(warnings)
        
        if not is_valid:
            logger.error(f"Recipe validation failed: {len(errors)} errors")
            return result
        
        # Generate code with line mapping
        code, line_map = self._generate_with_mapping(recipe)
        result.code = code
        result.line_map = line_map
        
        # Simulate if enabled
        if self.enable_simulation and self.simulator:
            logger.info("Running local simulation...")
            sim_result = self.simulator.simulate(code, line_map)
            result.simulation = sim_result
            
            if not sim_result.success:
                result.errors.extend(sim_result.errors)
                logger.error(f"Simulation failed: {sim_result.errors}")
                return result
            
            result.warnings.extend(sim_result.warnings)
        
        # Save to file
        timestamp = datetime.now().strftime("%H%M%S")
        protocol_name = recipe.get("ProtocolName", "Generated").replace(" ", "_")
        filename = f"Gen_{protocol_name}_{timestamp}.py"
        filepath = os.path.join(self.temp_dir, filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(code)
        
        result.filepath = filepath
        result.success = True
        
        logger.info(f"Protocol generated successfully: {filepath}")
        if result.warnings:
            logger.warning(f"  {len(result.warnings)} warnings")
        
        return result
    
    def _generate_with_mapping(self, recipe: dict) -> Tuple[str, Dict[int, int]]:
        """
        Generate code and create line mapping for error tracking.
        
        Returns:
            Tuple of (code, line_map) where line_map maps Python lines to step indices
        """
        protocol_name = recipe.get("ProtocolName", "Generated")
        
        # Load liquid classes from Library and merge with recipe's custom classes
        library_lc = load_liquid_classes_from_library()
        if library_lc:
            # Merge library liquid classes into CustomLiquidClasses
            if "CustomLiquidClasses" not in recipe:
                recipe["CustomLiquidClasses"] = {}
            # Library classes are added first, recipe classes can override
            merged = {**library_lc, **recipe.get("CustomLiquidClasses", {})}
            recipe["CustomLiquidClasses"] = merged
            logger.info(f"Injected {len(library_lc)} library liquid classes")
        
        code = UNIVERSAL_TEMPLATE
        code = code.replace("__PROTOCOL_NAME__", protocol_name)
        code = code.replace("__PROTOCOL_SETTINGS__", repr(recipe))
        
        # Build line map
        # The UNIVERSAL_TEMPLATE executes steps in a loop starting at a known line
        # We need to map the "for step in settings.get('Steps')" line area
        line_map = {}
        
        # Find the line where steps are executed
        lines = code.split('\n')
        for i, line in enumerate(lines):
            if 'for step in settings.get("Steps"' in line:
                # Steps execute after this line
                # Each step gets mapped roughly
                step_start_line = i + 1
                for step_idx in range(len(recipe.get("Steps", []))):
                    # Rough mapping - each step is about 2-5 lines
                    line_map[step_start_line + step_idx * 3] = step_idx
                break
        
        return code, line_map
        
    def generate(self, json_recipe: str) -> str:
        """
        Generate a Python protocol file from JSON recipe (simple version).
        
        For full validation, use generate_and_validate() instead.
        
        Args:
            json_recipe: JSON recipe string
            
        Returns:
            Path to the generated protocol file
        """
        result = self.generate_and_validate(json_recipe)
        
        if not result.success:
            error_msgs = [str(e) for e in result.errors]
            raise ValueError(f"Protocol generation failed:\n" + "\n".join(error_msgs))
        
        assert result.filepath is not None, "filepath should be set on success"
        return result.filepath
        
    def generate_content(self, json_recipe: str) -> str:
        """
        Generate Python protocol code without saving to file.
        
        Args:
            json_recipe: JSON recipe string
            
        Returns:
            Protocol code as string
        """
        try:
            recipe = json.loads(json_recipe)
            protocol_name = recipe.get("ProtocolName", "Generated")
        except json.JSONDecodeError:
            protocol_name = "Generated"
            recipe = {}
        
        # Load and inject library liquid classes
        library_lc = load_liquid_classes_from_library()
        if library_lc:
            if "CustomLiquidClasses" not in recipe:
                recipe["CustomLiquidClasses"] = {}
            merged = {**library_lc, **recipe.get("CustomLiquidClasses", {})}
            recipe["CustomLiquidClasses"] = merged
            
        code = UNIVERSAL_TEMPLATE
        code = code.replace("__PROTOCOL_NAME__", protocol_name)
        code = code.replace("__PROTOCOL_SETTINGS__", repr(recipe))
        
        return code
    
    def validate_only(self, json_recipe: str) -> Tuple[bool, List[ValidationError], List[ValidationError]]:
        """
        Validate recipe without generating code.
        
        Args:
            json_recipe: JSON recipe string
            
        Returns:
            Tuple of (is_valid, errors, warnings)
        """
        try:
            recipe = json.loads(json_recipe)
        except json.JSONDecodeError as e:
            return False, [ValidationError(None, None, None, "JSON", str(e))], []
        
        return self.validator.validate(recipe)
        
    def cleanup(self, filepath: str):
        """Delete a generated protocol file."""
        try:
            if os.path.exists(filepath) and "Gen_" in filepath:
                os.remove(filepath)
                logger.debug(f"Cleaned up: {filepath}")
        except Exception as e:
            logger.warning(f"Failed to cleanup {filepath}: {e}")
