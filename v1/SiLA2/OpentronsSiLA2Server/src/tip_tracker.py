"""
Tip Tracker - Tracks tip consumption across runs
================================================

Persistent tip usage tracking for deterministic tip management.
Supports crash recovery and partial run analysis.
"""

import json
import logging
import os
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)


class TipTracker:
    """
    Tracks tip usage for deterministic tip management.
    
    Features:
        - Persistent state (JSON file)
        - Multi-rack type support
        - Reset/refill capability
        - Availability checking
    """
    
    def __init__(self, state_file: str = "./tip_state.json"):
        """
        Initialize TipTracker.
        
        Args:
            state_file: Path to JSON file for persisting state
        """
        self._file_path = state_file
        self._usage: Dict[str, int] = {}
        self._load()
        
    def _load(self):
        """Load tip state from file."""
        if os.path.exists(self._file_path):
            try:
                with open(self._file_path, 'r', encoding='utf-8') as f:
                    self._usage = json.load(f)
                logger.debug(f"Tip state loaded: {self._usage}")
            except Exception as e:
                logger.warning(f"Failed to load tip state: {e}")
                self._usage = {}
        else:
            self._usage = {}
    
    def reload(self):
        """Reload tip state from file (external changes)."""
        self._load()
            
    def save(self):
        """Save tip state to file."""
        try:
            with open(self._file_path, 'w', encoding='utf-8') as f:
                json.dump(self._usage, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save tip state: {e}")
            
    def get_usage(self, rack_type: str) -> int:
        """
        Get current usage count for a rack type.
        
        NOTE: Always reloads from file to capture external changes (webapp refill).
        
        Args:
            rack_type: Opentrons load name of the tip rack
            
        Returns:
            Number of tips used
        """
        self._load()  # Reload to capture external changes
        return self._usage.get(rack_type, 0)
        
    def add_usage(self, rack_type: str, count: int):
        """
        Add to usage count for a rack type.
        
        Args:
            rack_type: Opentrons load name of the tip rack
            count: Number of tips to add
        """
        self._load()  # Reload to avoid overwriting external changes
        if rack_type not in self._usage:
            self._usage[rack_type] = 0
        self._usage[rack_type] += count
        self.save()
        logger.debug(f"Tip usage: {rack_type} += {count} (total: {self._usage[rack_type]})")
        
    def reset(self, rack_type: str):
        """
        Reset usage count for a rack type (refill).
        
        Args:
            rack_type: Opentrons load name of the tip rack
        """
        self._load()  # Reload to avoid overwriting external changes
        self._usage[rack_type] = 0
        self.save()
        logger.info(f"Tip rack reset: {rack_type}")
        
    def reset_all(self):
        """Reset all tip counts."""
        self._load()  # Reload first
        for key in self._usage:
            self._usage[key] = 0
        self.save()
        logger.info("All tip racks reset")
        
    def get_all_usage(self) -> Dict[str, int]:
        """Get all usage data."""
        return self._usage.copy()
        
    def get_tracked_types(self) -> List[str]:
        """Get list of tracked rack types."""
        return list(self._usage.keys())
        
    def check_availability(self, rack_type: str, required: int, capacity: int = 96) -> bool:
        """
        Check if enough tips are available.
        
        Args:
            rack_type: Opentrons load name
            required: Number of tips needed
            capacity: Total capacity of rack (default 96)
            
        Returns:
            True if enough tips available
        """
        used = self.get_usage(rack_type)
        available = capacity - used
        return available >= required
        
    def to_json(self) -> str:
        """Get state as JSON string."""
        return json.dumps(self._usage, indent=2)

    def sync_with_allowed_types(self, allowed_types: Set[str]) -> Dict[str, List[str]]:
        """
        Synchronize persisted tip state with a set of allowed rack load names.

        Policy:
            - Keep counters for rack types present in both current state and allowed set
            - Initialize new allowed rack types to 0
            - Remove rack types no longer present in allowed set

        Args:
            allowed_types: Set of rack load names currently present in HAL

        Returns:
            Summary dict with keys: kept, added, removed
        """
        self._load()  # Reload first to avoid overwriting external changes.

        current_keys = set(self._usage.keys())
        kept = sorted(list(current_keys & allowed_types))
        added = sorted(list(allowed_types - current_keys))
        removed = sorted(list(current_keys - allowed_types))

        new_usage: Dict[str, int] = {}
        for rack_type in kept:
            new_usage[rack_type] = self._usage.get(rack_type, 0)
        for rack_type in added:
            new_usage[rack_type] = 0

        self._usage = new_usage
        self.save()

        return {
            "kept": kept,
            "added": added,
            "removed": removed,
        }


def calculate_tips_from_recipe(recipe: Dict[str, Any]) -> Dict[str, int]:
    """
    Calculate tip requirements from a JSON recipe.
    
    Analyzes recipe steps to predict tip consumption.
    
    Args:
        recipe: JSON recipe dictionary
        
    Returns:
        Dict mapping rack load names to required tip counts
    """
    usage: Dict[str, int] = {}
    
    # Find default tip rack - check both Labware (post-HAL) and Requirements.Labware (pre-HAL)
    default_rack = ""
    
    # Check Labware (post-HAL format with LoadName)
    if "Labware" in recipe:
        for key, value in recipe["Labware"].items():
            if isinstance(value, dict):
                load_name = value.get("LoadName", value.get("type", ""))
            else:
                load_name = str(value)
            if "tip" in load_name.lower():
                if not default_rack:
                    default_rack = load_name
                    
    # Check Requirements.Labware (pre-HAL format with type)
    if not default_rack and "Requirements" in recipe:
        req = recipe["Requirements"]
        if isinstance(req, dict) and "Labware" in req:
            for key, value in req["Labware"].items():
                if isinstance(value, dict):
                    load_name = value.get("type", value.get("LoadName", ""))
                else:
                    load_name = str(value)
                if "tip" in load_name.lower():
                    if not default_rack:
                        default_rack = load_name
                    
    if not default_rack:
        return usage
        
    # Calculate from steps
    for step in recipe.get("Steps", []):
        cmd = step.get("Command", "").lower()  # Normalize to lowercase
        tip_strategy = step.get("NewTip", "once")
        tips_to_add = 0
        
        if cmd in ["pickuptip", "pick_up_tip"]:
            tips_to_add = 1
        elif cmd in ["consumetips", "consume_tips"]:
            tips_to_add = step.get("Quantity", 1)
        elif cmd in ["transfer", "distribute", "consolidate", "mix"]:
            if tip_strategy == "never":
                tips_to_add = 0
            elif tip_strategy == "always":
                if cmd == "distribute":
                    dests = step.get("Destinations", [])
                    tips_to_add = len(dests) if isinstance(dests, list) else 1
                elif cmd == "consolidate":
                    sources = step.get("Sources", [])
                    tips_to_add = len(sources) if isinstance(sources, list) else 1
                else:
                    tips_to_add = 1
            else:  # "once"
                tips_to_add = 1
                
        if tips_to_add > 0:
            if default_rack not in usage:
                usage[default_rack] = 0
            usage[default_rack] += tips_to_add
            
    return usage


def count_racks_in_recipe(recipe: Dict[str, Any], rack_type: str) -> int:
    """
    Count how many racks of a type are in the recipe.
    
    Args:
        recipe: JSON recipe dictionary
        rack_type: Opentrons load name to count
        
    Returns:
        Number of racks
    """
    count = 0
    if "Labware" in recipe:
        for value in recipe["Labware"].values():
            if value.get("LoadName") == rack_type:
                count += 1
    return count
