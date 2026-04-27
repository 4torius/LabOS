"""
Hardware Manager - Hardware Abstraction Layer (HAL)
===================================================

Maps logical requirements to physical hardware configurations.
Supports multiple hardware setups with runtime switching.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class HardwareManager:
    """
    Hardware Abstraction Layer - Maps logical requirements to physical hardware.
    
    Features:
        - Multi-configuration support (switch between deck setups)
        - Automatic tip rack injection
        - Pipette configuration inheritance
        - HAL mapping (Requirements -> physical Labware/Modules)
    """
    
    def __init__(
        self, 
        config_path: Optional[str] = None, 
        config_folder: Optional[str] = None
    ):
        """
        Initialize HardwareManager.
        
        Args:
            config_path: Direct path to a specific config file
            config_folder: Folder containing multiple config files
        """
        self._config: Dict[str, Any] = {}
        self._config_folder = config_folder
        self._current_config_file: Optional[str] = None
        
        if config_path and os.path.exists(config_path):
            self._load_config(config_path)
        elif config_folder:
            self._config_folder = config_folder
            configs = self.list_available_configs()
            if configs:
                self._load_config(os.path.join(config_folder, configs[0]))
                
    def _load_config(self, config_path: str):
        """Load a hardware configuration file."""
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Hardware config not found: {config_path}")
            
        with open(config_path, 'r', encoding='utf-8') as f:
            self._config = json.load(f)
            
        self._current_config_file = os.path.basename(config_path)
        config_name = self._config.get('ConfigName', self._current_config_file)
        logger.info(f"Hardware config loaded: {config_name}")
        
    def list_available_configs(self) -> List[str]:
        """List available hardware configuration files."""
        if not self._config_folder or not os.path.exists(self._config_folder):
            return []
            
        configs = []
        for f in os.listdir(self._config_folder):
            if f.endswith('.json'):
                configs.append(f)
        return sorted(configs)
        
    def switch_config(self, config_name: str) -> bool:
        """
        Switch to a different hardware configuration.
        
        Args:
            config_name: Name of the config file (with or without .json)
            
        Returns:
            True if successful
        """
        if not config_name.endswith('.json'):
            config_name = f"{config_name}.json"
            
        if self._config_folder:
            config_path = os.path.join(self._config_folder, config_name)
        else:
            config_path = config_name
            
        try:
            self._load_config(config_path)
            return True
        except Exception as e:
            logger.error(f"Failed to switch config: {e}")
            return False
            
    def get_current_config_name(self) -> str:
        """Get the name of the currently loaded configuration."""
        return self._config.get('ConfigName', self._current_config_file or 'Unknown')
        
    def get_current_config_file(self) -> str:
        """Get the filename of the currently loaded configuration."""
        return self._current_config_file or ""
    
    def validate_recipe_requirements(self, recipe: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        Validate that a recipe's Requirements match the loaded HAL config.
        
        Args:
            recipe: Recipe dictionary with Requirements section
            
        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []
        
        if "Requirements" not in recipe:
            return True, []  # No requirements to validate
        
        requirements = recipe.get("Requirements", {})
        
        # Handle both dict format and nested Labware/Modules format
        if isinstance(requirements, dict):
            # Check if it's the nested format with Labware/Modules sections
            if "Labware" in requirements or "Modules" in requirements:
                # Nested format
                for section in ["Labware", "Modules"]:
                    if section in requirements:
                        for logical_name, spec in requirements[section].items():
                            # spec could be a string (physical ID) or dict with 'type' key
                            if isinstance(spec, dict):
                                physical_id = spec.get("type", "")
                            else:
                                physical_id = str(spec)
                            
                            if physical_id:
                                node, _ = self._find_in_hardware(physical_id)
                                if node is None:
                                    errors.append(
                                        f"'{logical_name}' requires '{physical_id}' which is "
                                        f"not in the loaded HAL config ({self.get_current_config_name()})"
                                    )
            else:
                # Direct mapping format (logical_name -> physical_id)
                for logical_name, physical_id in requirements.items():
                    node, _ = self._find_in_hardware(physical_id)
                    if node is None:
                        errors.append(
                            f"'{logical_name}' requires '{physical_id}' which is "
                            f"not in the loaded HAL config ({self.get_current_config_name()})"
                        )
        
        return len(errors) == 0, errors
            
    def apply_hardware_to_recipe(self, recipe: Dict[str, Any]) -> Dict[str, Any]:
        """
        Apply hardware configuration to a recipe.
        
        Operations:
            1. Inject Pipettes from hardware config
            2. Auto-inject tip racks (only those NOT referenced by Requirements)
            3. Map Requirements to physical Labware/Modules
        
        Args:
            recipe: JSON recipe dictionary
            
        Returns:
            Modified recipe with hardware applied
        """
        # Ensure sections exist
        for section in ["Labware", "Modules", "Trash", "Pipettes"]:
            if section not in recipe:
                recipe[section] = {}
                
        # Copy pipettes from hardware (fixed setup)
        if "Pipettes" in self._config:
            recipe["Pipettes"] = self._config["Pipettes"].copy()
        
        # Get list of physical IDs referenced by Requirements (to avoid duplicates)
        required_physical_ids = set()
        if "Requirements" in recipe:
            required_physical_ids = set(recipe["Requirements"].values())
            
        # Auto-inject tip racks (ONLY those not referenced by Requirements)
        if "Labware" in self._config:
            for key, value in self._config["Labware"].items():
                # Skip if this item is referenced by a Requirement
                # (it will be added with the logical name instead)
                if key in required_physical_ids:
                    continue

                is_tip = "tip" in key.lower() or "tip" in value.get("LoadName", "").lower()
                if is_tip and key not in recipe["Labware"]:
                    recipe["Labware"][key] = value.copy()
                    
        # Resolve Requirements (HAL mapping)
        if "Requirements" in recipe:
            for logical_name, physical_id in recipe["Requirements"].items():
                physical_node, category = self._find_in_hardware(physical_id)
                if physical_node is None:
                    raise ValueError(
                        f"HAL Error: Requirement '{logical_name}' -> '{physical_id}' "
                        f"not found in hardware config"
                    )
                recipe[category][logical_name] = physical_node.copy()

        # Validate duplicate slot assignments and fail fast with a clear HAL error.
        if "Labware" in recipe:
            slot_to_names: Dict[str, List[str]] = {}
            for logical_name, node in recipe["Labware"].items():
                if not isinstance(node, dict):
                    continue
                slot = node.get("Slot")
                if not slot:
                    continue
                slot_to_names.setdefault(slot, []).append(logical_name)

            collisions = [
                f"{slot}: {', '.join(names)}"
                for slot, names in slot_to_names.items()
                if len(names) > 1
            ]
            if collisions:
                raise ValueError(
                    "HAL Error: duplicate labware slots after mapping -> "
                    + " | ".join(collisions)
                )
                
        return recipe
        
    def _find_in_hardware(self, item_id: str) -> Tuple[Optional[Dict], str]:
        """
        Find an item in hardware config.
        
        Args:
            item_id: ID to look for
            
        Returns:
            Tuple of (node dict, category name)
        """
        for category in ["Labware", "Modules", "Trash"]:
            if category in self._config and item_id in self._config[category]:
                return self._config[category][item_id], category
        return None, ""
        
    def get_configured_tip_racks(self) -> Dict[str, str]:
        """
        Get dict of tip rack logical names -> load names.
        
        Returns:
            Dictionary mapping tip rack IDs to Opentrons load names
        """
        tips = {}
        if "Labware" not in self._config:
            return tips
            
        for key, value in self._config["Labware"].items():
            load_name = value.get("LoadName", "")
            if "tip" in key.lower() or "tip" in load_name.lower():
                if load_name:
                    tips[key] = load_name
                    
        return tips
        
    def get_modules(self) -> Dict[str, Any]:
        """Get configured modules."""
        return self._config.get("Modules", {})
        
    def get_labware(self) -> Dict[str, Any]:
        """Get configured labware."""
        return self._config.get("Labware", {})
        
    def get_pipettes(self) -> Dict[str, Any]:
        """Get configured pipettes."""
        return self._config.get("Pipettes", {})
        
    @property
    def config(self) -> Dict[str, Any]:
        """Get raw hardware config."""
        return self._config
