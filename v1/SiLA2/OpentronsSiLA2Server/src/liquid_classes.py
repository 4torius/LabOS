"""
Liquid Class Definitions for Opentrons Flex
============================================

Opentrons-verified liquid class definitions for different liquid types.
Based on the official Opentrons API 2.24+ documentation.

Opentrons-verified Liquid Classes:
- Aqueous (water): Based on deionized water
- Viscous (glycerol_50): Based on 50% glycerol
- Volatile (ethanol_80): Based on 80% ethanol

Usage in recipes:
    - Use "LiquidClass": "Viscous" with TransferWithLiquidClass command
    - Or use "LiquidClass": "glycerol_50" (load name)
    
The native Opentrons API automatically handles:
- Submerge speed and position
- Aspirate/dispense flow rates (by volume)
- Delays after aspirate/dispense
- Retract speed and position
- Push out volumes
- Air gaps
- Touch tip
- Volume corrections
"""

from typing import Dict, Any, Optional
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#                 OPENTRONS VERIFIED LIQUID CLASSES (API 2.24+)
# ═══════════════════════════════════════════════════════════════════════════

# Mapping from friendly names to Opentrons load names
LIQUID_CLASS_LOAD_NAMES = {
    # Friendly name -> Opentrons load name
    "Aqueous": "water",
    "Water": "water",
    "Viscous": "glycerol_50",
    "Glycerol": "glycerol_50",
    "Glycerol50": "glycerol_50",
    "Volatile": "ethanol_80",
    "Ethanol": "ethanol_80",
    "Ethanol80": "ethanol_80",
    # Direct load names (pass-through)
    "water": "water",
    "glycerol_50": "glycerol_50",
    "ethanol_80": "ethanol_80",
}

# Valid Opentrons-verified liquid class load names
VALID_OPENTRONS_LIQUID_CLASSES = ["water", "glycerol_50", "ethanol_80"]


def get_liquid_class_load_name(name: str) -> Optional[str]:
    """
    Get the Opentrons load name for a liquid class.
    
    Args:
        name: Friendly name (Aqueous, Viscous, Volatile) or load name
        
    Returns:
        Opentrons load name (water, glycerol_50, ethanol_80) or None
    """
    # Case-insensitive lookup
    for key, value in LIQUID_CLASS_LOAD_NAMES.items():
        if key.lower() == name.lower():
            return value
    return None


def is_opentrons_liquid_class(name: str) -> bool:
    """Check if name maps to an Opentrons-verified liquid class."""
    return get_liquid_class_load_name(name) is not None


# ═══════════════════════════════════════════════════════════════════════════
#                      LIQUID CLASS REFERENCE DATA
# ═══════════════════════════════════════════════════════════════════════════

# Reference data for documentation/validation (actual values managed by Opentrons API)
LIQUID_CLASSES_REFERENCE = {
    "water": {
        "display_name": "Aqueous",
        "description": "Based on deionized water - standard liquid handling",
        "aspirate": {
            "submerge_speed": 100,  # mm/sec
            "flow_rate": "varies by volume",  # e.g., 35 µL/sec for 50µL tips
            "delay_after": 0.2,  # seconds
            "retract_speed": 50,  # mm/sec
        },
        "dispense": {
            "submerge_speed": 100,
            "flow_rate": 50,  # µL/sec
            "delay_after": 0.2,
            "retract_speed": 50,
            "push_out": "varies by volume",  # 2-7 µL depending on volume
        },
    },
    "glycerol_50": {
        "display_name": "Viscous",
        "description": "Based on 50% glycerol - slow speeds to prevent bubbles",
        "aspirate": {
            "submerge_speed": 4,  # mm/sec - MUCH slower
            "flow_rate": "varies by volume",  # 7-50 µL/sec
            "delay_after": 1.0,  # seconds - longer delay
            "retract_speed": 4,  # mm/sec - slow retract
            "air_gap": 0,  # No air gap for viscous
        },
        "dispense": {
            "submerge_speed": 4,
            "flow_rate": 25,  # µL/sec - slow dispense
            "delay_after": 0.5,
            "retract_speed": 4,
            "push_out": "3.9-11.7 µL",  # Higher push out for viscous
        },
    },
    "ethanol_80": {
        "display_name": "Volatile",
        "description": "Based on 80% ethanol - air gaps to prevent dripping",
        "aspirate": {
            "submerge_speed": 100,  # mm/sec
            "flow_rate": "varies by volume",  # 7-30 µL/sec
            "delay_after": 0.2,
            "retract_speed": 100,  # fast retract
            "delay_after_retract": 0.5,
            "air_gap": "5 µL",  # Air gap to prevent dripping
        },
        "dispense": {
            "submerge_speed": 100,
            "flow_rate": 30,  # µL/sec
            "delay_after": 0.2,
            "retract_speed": 100,
            "delay_after_retract": 0.5,
            "push_out": 1.0,
            "air_gap": "5 µL",
        },
    },
}


# ═══════════════════════════════════════════════════════════════════════════
#                 SIMPLIFIED PRESETS (for Rate-based fallback)
# ═══════════════════════════════════════════════════════════════════════════

# These are used when NOT using transfer_with_liquid_class (legacy support)
LIQUID_CLASS_PRESETS = {
    "Aqueous": {
        "load_name": "water",
        "description": "Water-like liquids (water, buffers, media)",
        "aspirate_rate": 1.0,      # Normal speed
        "dispense_rate": 1.0,      # Normal speed
        "blow_out": False,
        "touch_tip": False,
        "mix_after": None,
        "air_gap": 0,
        "push_out": 2.0,
        "delay_aspirate": 0.2,
        "delay_dispense": 0.2,
    },
    "Viscous": {
        "load_name": "glycerol_50",
        "description": "Viscous liquids (glycerol, PEG, honey, oils)",
        "aspirate_rate": 0.25,     # 25% speed - much slower
        "dispense_rate": 0.25,     # 25% speed
        "blow_out": True,          # Always blow out
        "touch_tip": True,         # Touch tip to remove residue
        "mix_after": [3, 0.5],     # 3 cycles at 50% volume
        "air_gap": 0,              # No air gap for viscous
        "push_out": 5.0,           # Higher push out
        "delay_aspirate": 1.0,     # 1 second delay
        "delay_dispense": 0.5,     # 0.5 second delay
    },
    "Volatile": {
        "load_name": "ethanol_80",
        "description": "Volatile liquids (ethanol, acetone, solvents)",
        "aspirate_rate": 0.5,      # 50% speed
        "dispense_rate": 0.5,      # 50% speed
        "blow_out": True,          # Blow out to clear
        "touch_tip": False,        # No touch tip (evaporation)
        "mix_after": None,
        "air_gap": 5,              # Air gap to prevent dripping
        "push_out": 1.0,
        "delay_aspirate": 0.2,
        "delay_dispense": 0.2,
    },
    "HighlyViscous": {
        "load_name": "glycerol_50",  # Use glycerol_50 as base, but with slower rates
        "description": "Very viscous liquids (>70% glycerol, very thick oils)",
        "aspirate_rate": 0.1,      # 10% speed - very slow
        "dispense_rate": 0.1,
        "blow_out": True,
        "touch_tip": True,
        "mix_after": [5, 0.5],     # More mixing cycles
        "air_gap": 0,
        "push_out": 10.0,          # Maximum push out
        "delay_aspirate": 2.0,     # 2 second delay
        "delay_dispense": 1.0,
    },
    "Foaming": {
        "load_name": "water",  # Use water as base
        "description": "Foaming liquids (detergents, proteins, surfactants)",
        "aspirate_rate": 0.3,      # Slow to prevent bubbles
        "dispense_rate": 0.3,
        "blow_out": False,         # No blow out (creates foam)
        "touch_tip": True,
        "mix_after": None,         # No mixing (creates foam)
        "air_gap": 2,
        "push_out": 3.0,
        "delay_aspirate": 0.5,
        "delay_dispense": 0.5,
    },
}


def get_liquid_class(name: str) -> Optional[Dict[str, Any]]:
    """
    Get a liquid class definition by name.
    
    Args:
        name: Name of liquid class (Aqueous, Viscous, Volatile, etc.)
        
    Returns:
        Liquid class preset dictionary or None if not found
    """
    # Check presets first (case-insensitive)
    for key, value in LIQUID_CLASS_PRESETS.items():
        if key.lower() == name.lower():
            return value
    return None


def list_liquid_classes() -> Dict[str, str]:
    """
    List available liquid classes with descriptions.
    
    Returns:
        Dictionary of {name: description}
    """
    result = {}
    for name, preset in LIQUID_CLASS_PRESETS.items():
        load_name = preset.get("load_name", "custom")
        result[name] = f"{preset['description']} (Opentrons: {load_name})"
    return result


# ═══════════════════════════════════════════════════════════════════════════
#                      RECIPE VALIDATION HELPERS
# ═══════════════════════════════════════════════════════════════════════════

VALID_LIQUID_CLASSES = list(LIQUID_CLASS_PRESETS.keys()) + VALID_OPENTRONS_LIQUID_CLASSES

def validate_liquid_class(name: str) -> bool:
    """Check if liquid class name is valid."""
    return name.lower() in [lc.lower() for lc in VALID_LIQUID_CLASSES]
