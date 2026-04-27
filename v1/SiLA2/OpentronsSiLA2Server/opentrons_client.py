"""
Opentrons SiLA2 Client - Versione Ottimizzata
=============================================
Client avanzato con:
- HAL (Hardware Abstraction Layer) completo
- Risoluzione automatica Requirements -> Hardware Config
- Supporto ricette con riferimenti simbolici
- Logging migliorato
- Batch execution
- Async/await ottimizzato
"""

import asyncio
import json
import sys
import os
import glob
import copy
import logging
from datetime import datetime

# Ensure src/ is in sys.path for dynamic imports
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
import yaml


# ═══════════════════════════════════════════════════════════════════════════
#                              LOGGING SETUP
# ═══════════════════════════════════════════════════════════════════════════

class Colors:
    """ANSI color codes for terminal output."""
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    MAGENTA = '\033[95m'
    WHITE = '\033[97m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'


def print_header(text: str):
    print(f"\n{Colors.HEADER}{Colors.BOLD}{'═'*60}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}  {text}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{'═'*60}{Colors.ENDC}\n")


def print_section(text: str):
    print(f"\n{Colors.CYAN}{Colors.BOLD}─── {text} ───{Colors.ENDC}\n")


def print_success(text: str):
    print(f"{Colors.GREEN}✓ {text}{Colors.ENDC}")


def print_error(text: str):
    print(f"{Colors.RED}✗ {text}{Colors.ENDC}")


def print_info(text: str):
    print(f"{Colors.CYAN}ℹ {text}{Colors.ENDC}")


def print_warning(text: str):
    print(f"{Colors.YELLOW}⚠ {text}{Colors.ENDC}")


def print_step(num: int, total: int, text: str):
    print(f"  {Colors.BLUE}[{num}/{total}]{Colors.ENDC} {text}")


# ═══════════════════════════════════════════════════════════════════════════
#                              DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════

class RunStatus(Enum):
    """Protocol run status."""
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHING = "finishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class RobotInfo:
    """Robot information."""
    name: str = "Unknown"
    model: str = "Unknown"
    serial: str = "Unknown"
    api_version: str = "Unknown"
    firmware: str = "Unknown"
    
    
@dataclass
class Instrument:
    """Instrument (pipette/gripper) information."""
    name: str
    mount: str
    model: str
    serial: str
    
    
@dataclass 
class Module:
    """Module information."""
    type: str
    serial: str
    slot: str = ""
    temperature: Optional[float] = None
    target_temp: Optional[float] = None
    latch_status: Optional[str] = None
    speed: Optional[int] = None


@dataclass
class HardwareConfig:
    """Hardware configuration data."""
    name: str
    labware: Dict[str, Dict] = field(default_factory=dict)
    modules: Dict[str, Dict] = field(default_factory=dict)
    pipettes: Dict[str, str] = field(default_factory=dict)
    trash: Dict[str, Dict] = field(default_factory=dict)
    
    @classmethod
    def from_file(cls, filepath: str) -> 'HardwareConfig':
        """Load from JSON file."""
        with open(filepath, 'r') as f:
            data = json.load(f)
        return cls(
            name=Path(filepath).stem,
            labware=data.get("Labware", {}),
            modules=data.get("Modules", {}),
            pipettes=data.get("Pipettes", {}),
            trash=data.get("Trash", {})
        )


# ═══════════════════════════════════════════════════════════════════════════
#                    HARDWARE ABSTRACTION LAYER (HAL)
# ═══════════════════════════════════════════════════════════════════════════

class HardwareAbstractionLayer:
    """
    HAL - Risolve i riferimenti simbolici nelle ricette.
    
    Permette di scrivere ricette con nomi logici tipo:
    - "Reservoir_Main:A1" -> risolve a labware effettivo
    - "Piastra_Target:B2" -> risolve a piastra su slot specifico
    - "HeaterShaker" -> risolve a modulo con serial corretto
    
    La risoluzione avviene in base al file HardwareConfig caricato.
    """
    
    def __init__(self):
        self.config: Optional[HardwareConfig] = None
        self.resolution_map: Dict[str, Dict] = {}
        
    def load_config(self, config: HardwareConfig):
        """Load hardware configuration."""
        self.config = config
        self._build_resolution_map()
        
    def _build_resolution_map(self):
        """Build the symbol -> hardware resolution map."""
        if not self.config:
            return
            
        self.resolution_map = {}
        
        # Map labware names
        for name, details in self.config.labware.items():
            self.resolution_map[name] = {
                "type": "labware",
                "load_name": details.get("LoadName"),
                "slot": details.get("Slot"),
                "display_name": details.get("DisplayName", name)
            }
            
        # Map modules
        for name, details in self.config.modules.items():
            self.resolution_map[name] = {
                "type": "module",
                "module_type": details.get("Type"),
                "slot": details.get("Slot")
            }
            
        # Map trash
        for name, details in self.config.trash.items():
            self.resolution_map[name] = {
                "type": "trash",
                "trash_type": details.get("Type"),
                "slot": details.get("Slot")
            }
            
    def resolve_requirements(self, requirements: Dict[str, str]) -> Dict[str, Dict]:
        """
        Resolve recipe requirements to actual hardware.
        
        Args:
            requirements: {"LogicalName": "HardwareConfigName"}
            
        Returns:
            {"LogicalName": {"resolved_info": ...}}
        """
        resolved = {}
        
        for logical_name, hw_name in requirements.items():
            if hw_name in self.resolution_map:
                resolved[logical_name] = self.resolution_map[hw_name]
            else:
                # Try direct match in config
                if hw_name in self.config.labware:
                    resolved[logical_name] = {
                        "type": "labware",
                        **self.config.labware[hw_name]
                    }
                elif hw_name in self.config.modules:
                    resolved[logical_name] = {
                        "type": "module",
                        **self.config.modules[hw_name]
                    }
                elif hw_name in self.config.trash:
                    resolved[logical_name] = {
                        "type": "trash",
                        **self.config.trash[hw_name]
                    }
                else:
                    print_warning(f"Cannot resolve requirement: {logical_name} -> {hw_name}")
                    
        return resolved
        
    def transform_recipe(self, recipe: Dict) -> Dict:
        """
        Transform a recipe with Requirements into a fully-resolved recipe.
        
        This applies the HAL transformation:
        1. Read Requirements section
        2. Resolve each requirement to hardware config
        3. Build Labware/Modules/Trash sections
        4. Update Steps to use resolved slots
        """
        if not self.config:
            print_warning("No hardware config loaded - recipe unchanged")
            return recipe
            
        recipe = copy.deepcopy(recipe)
        
        # Get requirements
        requirements = recipe.get("Requirements", {})
        if not requirements:
            # No requirements - just merge config
            return self._merge_hardware_config(recipe)
            
        # Resolve requirements
        resolved = self.resolve_requirements(requirements)
        
        # Build labware section
        if "Labware" not in recipe:
            recipe["Labware"] = {}
            
        for logical_name, hw_info in resolved.items():
            if hw_info.get("type") == "labware":
                recipe["Labware"][logical_name] = {
                    "LoadName": hw_info.get("load_name") or hw_info.get("LoadName"),
                    "Slot": hw_info.get("slot") or hw_info.get("Slot"),
                    "DisplayName": hw_info.get("display_name") or hw_info.get("DisplayName", logical_name)
                }
                
        # Build modules section  
        if "Modules" not in recipe:
            recipe["Modules"] = {}
            
        for logical_name, hw_info in resolved.items():
            if hw_info.get("type") == "module":
                recipe["Modules"][logical_name] = {
                    "Type": hw_info.get("module_type") or hw_info.get("Type"),
                    "Slot": hw_info.get("slot") or hw_info.get("Slot")
                }
                
        # Build trash section
        if "Trash" not in recipe:
            recipe["Trash"] = {}
            
        for logical_name, hw_info in resolved.items():
            if hw_info.get("type") == "trash":
                recipe["Trash"][logical_name] = {
                    "Type": hw_info.get("trash_type") or hw_info.get("Type"),
                    "Slot": hw_info.get("slot") or hw_info.get("Slot")
                }
                
        # Set pipettes from config
        if self.config.pipettes:
            recipe["Pipettes"] = self.config.pipettes
            
        # Remove Requirements section (now resolved)
        recipe.pop("Requirements", None)
        
        return recipe
        
    def _merge_hardware_config(self, recipe: Dict) -> Dict:
        """Merge hardware config into recipe without Requirements."""
        if not self.config:
            return recipe
            
        recipe = copy.deepcopy(recipe)
        
        # Merge labware
        if self.config.labware:
            if "Labware" not in recipe:
                recipe["Labware"] = {}
            recipe["Labware"].update(self.config.labware)
            
        # Merge modules
        if self.config.modules:
            if "Modules" not in recipe:
                recipe["Modules"] = {}
            recipe["Modules"].update(self.config.modules)
            
        # Set pipettes
        if self.config.pipettes:
            recipe["Pipettes"] = self.config.pipettes
            
        # Set trash
        if self.config.trash:
            if "Trash" not in recipe:
                recipe["Trash"] = {}
            recipe["Trash"].update(self.config.trash)
            
        return recipe


# ═══════════════════════════════════════════════════════════════════════════
#                           OPTIMIZED CLIENT
# ═══════════════════════════════════════════════════════════════════════════

class OpentronsSiLA2Client:
    def validate_recipe(self, recipe: dict) -> tuple[bool, list]:
        """Valida che tutti i requirements della ricetta siano mappati nell'hardware config attivo."""
        if not self.hw_config:
            return False, ["Nessuna configurazione hardware caricata."]
        missing = []
        reqs = recipe.get("Requirements", {})
        # Labware
        for logical, hw in reqs.items():
            found = False
            if hw in self.hw_config.labware:
                found = True
            if hw in self.hw_config.modules:
                found = True
            if hw in self.hw_config.trash:
                found = True
            if not found:
                missing.append(f"Requirement '{logical}' → '{hw}' non trovato nella configurazione hardware attiva.")
        # Pipette
        pipettes_needed = recipe.get("Pipettes", {})
        for mount, pip in pipettes_needed.items():
            if mount not in self.hw_config.pipettes or self.hw_config.pipettes[mount] != pip:
                missing.append(f"Pipetta richiesta '{pip}' su '{mount}' non trovata nella configurazione hardware attiva.")
        return (len(missing) == 0), missing
    """
    Client ottimizzato per Opentrons SiLA2 Server.
    
    Features:
    - HAL integration per ricette con Requirements
    - Connection pooling con retry
    - Async batch execution
    - Progress monitoring
    - Comprehensive error handling
    """
    
    def __init__(self, config_path: str = None):
        """Initialize client."""
        if config_path is None:
            # Usa sempre config.yaml nella root di OpentronsSiLA2Server, anche se lanciato da scripts/
            config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "config.yaml"))
        else:
            config_path = os.path.abspath(config_path)
        self.config_path = config_path
        self.config = self._load_config(config_path)

        # Robot connection
        self.host = self.config.get("robot", {}).get("ip", "169.254.161.83")
        self.port = self.config.get("robot", {}).get("port", 31950)
        self.local_address = self.config.get("robot", {}).get("local_address")
        self.base_url = f"http://{self.host}:{self.port}"

        # HTTP client
        self.client: Optional[httpx.AsyncClient] = None

        # State
        self.robot_info: Optional[RobotInfo] = None
        self.current_run_id: Optional[str] = None
        self.connected: bool = False

        # HAL
        self.hal = HardwareAbstractionLayer()
        self.hw_config: Optional[HardwareConfig] = None

        # Tip Tracker
        self._tip_tracker = None
        self.tip_state_file = Path(self.config.get("tip_tracking", {}).get("state_file", "./tip_state.json"))

        # Directories
        self.dir_input = Path(self.config.get("directories", {}).get("input_queue", "./input"))
        self.dir_processed = Path(self.config.get("directories", {}).get("processed", "./processed"))
        self.dir_errors = Path(self.config.get("directories", {}).get("errors", "./errors"))
        self.dir_output = Path(self.config.get("directories", {}).get("output", "./output"))
        self.hw_config_folder = Path(
            self.config.get("hardware", {}).get("config_folder", "../HardwareConfig")
        ).resolve()

        # Protocol generator (lazy loaded)
        self._generator = None

        # Logger
        self.logger = logging.getLogger("OpentronsSiLA2Client")
        
    def _load_config(self, path: str) -> Dict:
        """Load YAML configuration."""
        try:
            with open(path, 'r') as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            print_warning(f"Config not found: {path}")
            return {}
            
    @property
    def generator(self):
        """Lazy load protocol generator."""
        if self._generator is None:
            from src.protocol_generator import ProtocolGenerator
            self._generator = ProtocolGenerator(temp_dir="./temp")
        return self._generator
        
    @property
    def tip_tracker(self):
        """Lazy load tip tracker."""
        if self._tip_tracker is None:
            from src.tip_tracker import TipTracker
            self._tip_tracker = TipTracker(str(self.tip_state_file))
        return self._tip_tracker
        
    # ─────────────────────────────────────────────────────────────────────
    #                         CONNECTION
    # ─────────────────────────────────────────────────────────────────────
    
    async def connect(self, retries: int = 3, timeout: float = 30.0) -> bool:
        """
        Connect to robot with retry logic.
        
        Args:
            retries: Number of connection attempts
            timeout: Request timeout in seconds
            
        Returns:
            True if connected successfully
        """
        for attempt in range(retries):
            try:
                transport = httpx.AsyncHTTPTransport(
                    local_address=self.local_address
                ) if self.local_address else None
                
                self.client = httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=timeout,
                    headers={"Opentrons-Version": "*"},
                    transport=transport
                )
                
                response = await self.client.get("/health")
                
                if response.status_code == 200:
                    data = response.json()
                    self.robot_info = RobotInfo(
                        name=data.get("name", "Unknown"),
                        model=data.get("robot_model", "Unknown"),
                        serial=data.get("robot_serial", "Unknown"),
                        api_version=data.get("api_version", "Unknown"),
                        firmware=data.get("fw_version", "Unknown")
                    )
                    self.connected = True
                    print_success(f"Connected to {self.robot_info.name}")
                    return True
                    
            except Exception as e:
                if attempt < retries - 1:
                    print_warning(f"Connection attempt {attempt + 1} failed, retrying...")
                    await asyncio.sleep(1)
                else:
                    print_error(f"Connection failed: {e}")
                    
        return False
        
    async def disconnect(self):
        """Disconnect from robot."""
        if self.client:
            await self.client.aclose()
            self.client = None
        self.connected = False
        
    async def ensure_connected(self) -> bool:
        """Ensure connection is active."""
        if not self.connected or not self.client:
            return await self.connect()
        return True
        
    # ─────────────────────────────────────────────────────────────────────
    #                      HARDWARE CONFIG
    # ─────────────────────────────────────────────────────────────────────
    
    def list_hw_configs(self) -> List[str]:
        """List available hardware configurations."""
        if not self.hw_config_folder.exists():
            return []
        return sorted([f.name for f in self.hw_config_folder.glob("*.json")])
        
    def load_hw_config(self, name: str) -> bool:
        """
        Load hardware configuration.
        
        Args:
            name: Config filename (e.g., "Standard_Flex_Setup.json")
            
        Returns:
            True if loaded successfully
        """
        filepath = self.hw_config_folder / name
        if not filepath.exists():
            print_error(f"Config not found: {name}")
            return False
            
        try:
            self.hw_config = HardwareConfig.from_file(str(filepath))
            self.hal.load_config(self.hw_config)
            print_success(f"Loaded hardware config: {name}")
            return True
        except Exception as e:
            print_error(f"Failed to load config: {e}")
            return False
            
    def show_hw_config(self):
        """Display current hardware configuration."""
        if not self.hw_config:
            print_warning("No hardware config loaded")
            return
            
        print_section(f"Hardware Config: {self.hw_config.name}")
        
        if self.hw_config.labware:
            print(f"{Colors.BOLD}Labware:{Colors.ENDC}")
            for name, details in self.hw_config.labware.items():
                slot = details.get("Slot", "?")
                load_name = details.get("LoadName", "?")
                print(f"  {Colors.CYAN}{name}{Colors.ENDC}: {load_name} @ {slot}")
                
        if self.hw_config.modules:
            print(f"\n{Colors.BOLD}Modules:{Colors.ENDC}")
            for name, details in self.hw_config.modules.items():
                slot = details.get("Slot", "?")
                mod_type = details.get("Type", "?")
                print(f"  {Colors.CYAN}{name}{Colors.ENDC}: {mod_type} @ {slot}")
                
        if self.hw_config.pipettes:
            print(f"\n{Colors.BOLD}Pipettes:{Colors.ENDC}")
            for mount, name in self.hw_config.pipettes.items():
                print(f"  {Colors.CYAN}{mount.upper()}{Colors.ENDC}: {name}")
                
        if self.hw_config.trash:
            print(f"\n{Colors.BOLD}Trash:{Colors.ENDC}")
            for name, details in self.hw_config.trash.items():
                slot = details.get("Slot", "?")
                print(f"  {Colors.CYAN}{name}{Colors.ENDC}: {slot}")
                
    # ─────────────────────────────────────────────────────────────────────
    #                      PROTOCOL EXECUTION
    # ─────────────────────────────────────────────────────────────────────
    
    def list_protocols(self) -> List[Path]:
        """List available protocol/recipe files from input and recipes folders."""
        protocols = []
        # Cerca nella cartella input
        for ext in ['*.json', '*.py']:
            protocols.extend(self.dir_input.glob(ext))
        # Cerca anche nella cartella delle ricette, se esiste
        recipes_dir = self.config.get("directories", {}).get("recipes")
        if recipes_dir:
            recipes_path = Path(recipes_dir).resolve()
            if recipes_path.exists():
                for ext in ['*.json', '*.py']:
                    protocols.extend(recipes_path.glob(ext))
        return sorted(protocols)
        
    async def execute_recipe(
        self, 
        recipe: Dict, 
        apply_hal: bool = True,
        monitor: bool = True
    ) -> Tuple[bool, Optional[str]]:
        """
        Execute a recipe on the robot.
        
        Args:
            recipe: Recipe dictionary
            apply_hal: Apply HAL transformation (resolve Requirements)
            monitor: Monitor run until completion
            
        Returns:
            (success, run_id)
        """
        if not await self.ensure_connected():
            return False, None
            
        try:
            # Apply HAL transformation
            if apply_hal and self.hw_config:
                recipe = self.hal.transform_recipe(recipe)
            
            # Inject TipUsageMap from tracker
            tip_usage = self.tip_tracker.get_all_usage()
            if tip_usage:
                recipe["TipUsageMap"] = tip_usage
                print_info(f"TipUsageMap: {tip_usage}")
                
            # Show recipe info
            protocol_name = recipe.get("ProtocolName", "Unknown")
            steps = recipe.get("Steps", [])
            print_info(f"Protocol: {protocol_name}")
            print_info(f"Steps: {len(steps)}")
            
            # Generate Python code
            protocol_code = self.generator.generate_content(json.dumps(recipe))
            
            # Upload to robot
            files = {"files": ("protocol.py", protocol_code.encode('utf-8'), "application/x-python")}
            response = await self.client.post("/protocols", files=files)
            
            if response.status_code not in [200, 201]:
                print_error(f"Upload failed: {response.text}")
                return False, None
                
            protocol_id = response.json().get("data", {}).get("id")
            print_success(f"Protocol uploaded: {protocol_id[:12]}...")
            
            # Create run
            response = await self.client.post("/runs", json={"data": {"protocolId": protocol_id}})
            
            if response.status_code == 409:
                print_warning("Zombie run detected, cleaning...")
                await self._cleanup_runs()
                response = await self.client.post("/runs", json={"data": {"protocolId": protocol_id}})
                
            if response.status_code not in [200, 201]:
                print_error(f"Run creation failed: {response.text}")
                return False, None
                
            run_id = response.json().get("data", {}).get("id")
            self.current_run_id = run_id
            print_success(f"Run created: {run_id[:12]}...")
            
            # Start run
            response = await self.client.post(
                f"/runs/{run_id}/actions",
                json={"data": {"actionType": "play"}}
            )
            
            if response.status_code not in [200, 201]:
                print_error(f"Start failed: {response.text}")
                return False, run_id
                
            print_success("Run started!")
            
            # Calculate tips used for tracking (before monitoring)
            from src.tip_tracker import calculate_tips_from_recipe
            tips_used = calculate_tips_from_recipe(recipe)
            
            # Monitor if requested
            if monitor:
                success = await self.monitor_run(run_id)
                
                # Update tip tracker on success
                if success and tips_used:
                    for rack_type, count in tips_used.items():
                        self.tip_tracker.add_usage(rack_type, count)
                    print_info(f"Tips consumed: {tips_used}")
                    
                return success, run_id
                
            return True, run_id
            
        except Exception as e:
            print_error(f"Execution error: {e}")
            self.logger.exception("Recipe execution failed")
            return False, None
            
    async def execute_file(
        self, 
        filepath: str, 
        apply_hal: bool = True,
        confirm: bool = True
    ) -> Tuple[bool, Optional[str]]:
        """
        Execute a protocol file.
        
        Args:
            filepath: Path to .json or .py file
            apply_hal: Apply HAL transformation for JSON
            confirm: Ask for confirmation before execution
            
        Returns:
            (success, run_id)
        """
        path = Path(filepath)
        
        if not path.exists():
            print_error(f"File not found: {filepath}")
            return False, None
            
        print_info(f"Loading: {path.name}")
        
        if path.suffix.lower() == '.json':
            with open(path, 'r') as f:
                recipe = json.load(f)
                
            # Show info
            print_section("Recipe Info")
            print(f"  Name: {recipe.get('ProtocolName', 'Unknown')}")
            print(f"  Description: {recipe.get('Description', 'N/A')}")
            
            # Show requirements if present
            requirements = recipe.get("Requirements", {})
            if requirements:
                print(f"\n{Colors.BOLD}Requirements (HAL):{Colors.ENDC}")
                for logical, hw in requirements.items():
                    print(f"  {logical} → {hw}")
                    
            steps = recipe.get("Steps", [])
            print(f"\n{Colors.BOLD}Steps ({len(steps)}):{Colors.ENDC}")
            for i, step in enumerate(steps[:10], 1):  # Show first 10
                cmd = step.get("Command", "?")
                print(f"  {i}. {cmd}")
            if len(steps) > 10:
                print(f"  ... +{len(steps) - 10} more steps")
                
            if confirm:
                ans = input(f"\n{Colors.YELLOW}Execute? (y/n): {Colors.ENDC}").strip().lower()
                if ans != 'y':
                    print_info("Cancelled")
                    return False, None
                    
            return await self.execute_recipe(recipe, apply_hal=apply_hal)
            
        elif path.suffix.lower() == '.py':
            # Upload Python directly
            with open(path, 'rb') as f:
                files = {"files": (path.name, f, "application/x-python")}
                response = await self.client.post("/protocols", files=files)
                
            if response.status_code not in [200, 201]:
                print_error(f"Upload failed: {response.text}")
                return False, None
                
            protocol_id = response.json().get("data", {}).get("id")
            print_success(f"Uploaded: {protocol_id[:12]}...")
            
            if confirm:
                ans = input(f"\n{Colors.YELLOW}Run? (y/n): {Colors.ENDC}").strip().lower()
                if ans != 'y':
                    return False, None
                    
            # Create and run
            response = await self.client.post("/runs", json={"data": {"protocolId": protocol_id}})
            if response.status_code not in [200, 201]:
                return False, None
                
            run_id = response.json().get("data", {}).get("id")
            self.current_run_id = run_id
            
            response = await self.client.post(
                f"/runs/{run_id}/actions",
                json={"data": {"actionType": "play"}}
            )
            
            if response.status_code in [200, 201]:
                print_success("Run started!")
                await self.monitor_run(run_id)
                return True, run_id
                
        else:
            print_error(f"Unsupported file type: {path.suffix}")
            
        return False, None
        
    async def monitor_run(self, run_id: str, poll_interval: float = 1.0) -> bool:
        """
        Monitor run until completion.
        
        Args:
            run_id: Run ID to monitor
            poll_interval: Polling interval in seconds
            
        Returns:
            True if run succeeded
        """
        terminal_states = {"succeeded", "failed", "stopped"}
        last_status = ""
        
        print_info("Monitoring run...")
        
        try:
            while True:
                response = await self.client.get(f"/runs/{run_id}")
                if response.status_code != 200:
                    break
                    
                data = response.json().get("data", {})
                status = data.get("status", "unknown")
                
                if status != last_status:
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    
                    if status == "running":
                        print(f"  {Colors.GREEN}[{timestamp}] {status}{Colors.ENDC}")
                    elif status == "succeeded":
                        print(f"  {Colors.GREEN}[{timestamp}] {status} ✓{Colors.ENDC}")
                        return True
                    elif status in ["failed", "stopped"]:
                        print(f"  {Colors.RED}[{timestamp}] {status}{Colors.ENDC}")
                        return False
                    else:
                        print(f"  {Colors.YELLOW}[{timestamp}] {status}{Colors.ENDC}")
                        
                    last_status = status
                    
                if status in terminal_states:
                    return status == "succeeded"
                    
                await asyncio.sleep(poll_interval)
                
        except KeyboardInterrupt:
            print_info("\nMonitoring stopped")
            
        return False
        
    async def _cleanup_runs(self):
        """Cleanup zombie runs."""
        try:
            response = await self.client.get("/runs?pageLength=5")
            if response.status_code == 200:
                for run in response.json().get("data", []):
                    if run.get("current"):
                        await self.client.patch(
                            f"/runs/{run['id']}", 
                            json={"data": {"current": False}}
                        )
        except:
            pass
            
    # ─────────────────────────────────────────────────────────────────────
    #                        ROBOT STATUS
    # ─────────────────────────────────────────────────────────────────────
    
    async def get_status(self) -> Dict[str, Any]:
        """Get comprehensive robot status."""
        if not await self.ensure_connected():
            return {}
            
        status = {
            "robot": None,
            "instruments": [],
            "modules": [],
            "active_run": None
        }
        
        # Robot info
        response = await self.client.get("/health")
        if response.status_code == 200:
            status["robot"] = response.json()
            
        # Instruments
        response = await self.client.get("/instruments")
        if response.status_code == 200:
            status["instruments"] = response.json().get("data", [])
            
        # Modules
        response = await self.client.get("/modules")
        if response.status_code == 200:
            status["modules"] = response.json().get("data", [])
            
        # Active run
        response = await self.client.get("/runs?pageLength=1")
        if response.status_code == 200:
            runs = response.json().get("data", [])
            for run in runs:
                if run.get("current"):
                    status["active_run"] = run
                    
        return status
        
    async def show_status(self):
        """Display robot status."""
        status = await self.get_status()
        
        print_section("Robot Status")
        
        if status.get("robot"):
            r = status["robot"]
            print(f"  Name:     {r.get('name', 'N/A')}")
            print(f"  Model:    {r.get('robot_model', 'N/A')}")
            print(f"  Serial:   {r.get('robot_serial', 'N/A')}")
            print(f"  API:      {r.get('api_version', 'N/A')}")
            print(f"  Firmware: {r.get('fw_version', 'N/A')}")
            
        if status.get("instruments"):
            print(f"\n{Colors.BOLD}Instruments:{Colors.ENDC}")
            for inst in status["instruments"]:
                if inst.get('instrumentType') == 'pipette':
                    print(f"  {inst.get('mount', '?').upper()}: {inst.get('instrumentName', 'N/A')}")
                elif inst.get('instrumentType') == 'gripper':
                    print(f"  GRIPPER: {inst.get('instrumentModel', 'N/A')}")
                    
        if status.get("modules"):
            print(f"\n{Colors.BOLD}Modules:{Colors.ENDC}")
            for m in status["modules"]:
                mod_type = m.get('moduleType', 'Unknown')
                mod_data = m.get('moduleData', {})
                info = []
                if 'currentTemperature' in mod_data:
                    info.append(f"T={mod_data['currentTemperature']}°C")
                if 'labwareLatchStatus' in mod_data:
                    info.append(f"Latch={mod_data['labwareLatchStatus']}")
                info_str = f" ({', '.join(info)})" if info else ""
                print(f"  {mod_type}: {m.get('serialNumber', 'N/A')}{info_str}")
                
        if status.get("active_run"):
            run = status["active_run"]
            print(f"\n{Colors.BOLD}Active Run:{Colors.ENDC}")
            print(f"  ID:     {run.get('id', 'N/A')[:12]}...")
            print(f"  Status: {run.get('status', 'N/A')}")
        else:
            print(f"\n{Colors.GREEN}Robot idle{Colors.ENDC}")
            
        if self.hw_config:
            print(f"\n{Colors.BOLD}Hardware Config:{Colors.ENDC} {self.hw_config.name}")
            
    # ─────────────────────────────────────────────────────────────────────
    #                      CONTROL COMMANDS
    # ─────────────────────────────────────────────────────────────────────
    
    async def home(self):
        """Home robot."""
        print_info("Homing robot...")
        response = await self.client.post("/robot/home", json={"target": "robot"})
        if response.status_code == 200:
            print_success("Robot homed")
        else:
            print_error(f"Home failed: {response.text}")
            
    async def pause(self):
        """Pause current run."""
        if not self.current_run_id:
            await self._find_active_run()
        if not self.current_run_id:
            print_warning("No active run")
            return
            
        response = await self.client.post(
            f"/runs/{self.current_run_id}/actions",
            json={"data": {"actionType": "pause"}}
        )
        if response.status_code in [200, 201]:
            print_success("Run paused")
        else:
            print_error(f"Pause failed")
            
    async def resume(self):
        """Resume paused run."""
        if not self.current_run_id:
            print_warning("No run to resume")
            return
            
        response = await self.client.post(
            f"/runs/{self.current_run_id}/actions",
            json={"data": {"actionType": "play"}}
        )
        if response.status_code in [200, 201]:
            print_success("Run resumed")
            
    async def stop(self):
        """Stop current run."""
        if not self.current_run_id:
            await self._find_active_run()
        if not self.current_run_id:
            print_warning("No active run")
            return
            
        response = await self.client.post(
            f"/runs/{self.current_run_id}/actions",
            json={"data": {"actionType": "stop"}}
        )
        if response.status_code in [200, 201]:
            print_success("Run stopped")
            
    async def _find_active_run(self):
        """Find active run."""
        response = await self.client.get("/runs?pageLength=5")
        if response.status_code == 200:
            for run in response.json().get("data", []):
                if run.get("current"):
                    self.current_run_id = run["id"]
                    return
                    
    async def lights(self, on: bool):
        """Control robot lights."""
        await self.client.post("/robot/lights", json={"on": on})
        print_success(f"Lights {'ON' if on else 'OFF'}")
        
    async def emergency_stop(self):
        """Emergency stop - stop run, drop tips, home."""
        print_section("EMERGENCY STOP")
        print_warning("Stopping all operations...")
        
        # Stop active run
        await self._find_active_run()
        if self.current_run_id:
            await self.stop()
            await asyncio.sleep(1)
            await self._cleanup_runs()
            
        # Home
        await self.home()
        
        print_success("Emergency stop complete")


# ═══════════════════════════════════════════════════════════════════════════
#                           INTERACTIVE SHELL
# ═══════════════════════════════════════════════════════════════════════════

async def interactive_shell():
    """Run interactive shell."""
    print_header("OPENTRONS SiLA2 CLIENT")
    
    client = OpentronsSiLA2Client()
    
    print_info(f"Robot: {client.host}:{client.port}")
    
    if not await client.connect():
        print_error("Connection failed")
        return
        
    # Load default config
    configs = client.list_hw_configs()
    if configs:
        client.load_hw_config(configs[0])
        
    print_info("Type 'help' for commands")
    
    commands = {
        # Status
        "status": ("Show robot status", client.show_status),
        "hw": ("Show hardware config", client.show_hw_config),
        
        # Config
        "hwlist": ("List hardware configs", lambda: print("\n".join(f"  {c}" for c in client.list_hw_configs()))),
        "hwload": ("Load hardware config", None),  # Special handling
        
        # Execution
        "list": ("List protocols", lambda: print("\n".join(f"  {p.name}" for p in client.list_protocols()))),
        "run": ("Run protocol", None),  # Special handling
        
        # Control
        "home": ("Home robot", client.home),
        "pause": ("Pause run", client.pause),
        "resume": ("Resume run", client.resume),
        "stop": ("Stop run", client.stop),
        "estop": ("Emergency stop", client.emergency_stop),
        
        # Tips
        "tips": ("Show tip status", None),  # Special handling
        "tipreset": ("Reset tip counter (refill)", None),  # Special handling
        
        # Lights
        "lon": ("Lights on", lambda: client.lights(True)),
        "loff": ("Lights off", lambda: client.lights(False)),
        
        # Help
        "help": ("Show help", None),
        "exit": ("Exit", None),
    }
    
    while True:
        try:
            cmd = input(f"\n{Colors.GREEN}Opentrons❯{Colors.ENDC} ").strip()
            if not cmd:
                continue
            parts = cmd.split(maxsplit=1)
            cmd_name = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""

            if cmd_name not in commands:
                print_warning(f"Unknown command: {cmd_name}")
                continue
            if cmd_name == "exit":
                print_info("Exiting...")
                break
            if cmd_name == "help":
                print("\nCommands:")
                for k, v in commands.items():
                    print(f"  {k:10} {v[0]}")
                continue
            if cmd_name == "hwload":
                configs = client.list_hw_configs()
                for i, c in enumerate(configs, 1):
                    print(f"  [{i}] {c}")
                sel = input("Select: ")
                try:
                    idx = int(sel) - 1
                    if 0 <= idx < len(configs):
                        client.load_hw_config(configs[idx])
                        print_success(f"Loaded hardware config: {configs[idx]}")
                    else:
                        print_warning("Invalid selection")
                except Exception:
                    print_warning("Invalid input")
                continue
            if cmd_name == "run":
                protocols = client.list_protocols()
                for i, p in enumerate(protocols, 1):
                    print(f"  [{i}] {p.name}")
                sel = input("Select: ")
                try:
                    idx = int(sel) - 1
                    if 0 <= idx < len(protocols):
                        proto_path = protocols[idx]
                        print_info(f"Loading: {proto_path.name}")
                        with open(proto_path, "r", encoding="utf-8") as f:
                            recipe = json.load(f)
                        # Mostra info ricetta
                        print_section("Recipe Info")
                        print(f"\n  Name: {recipe.get('Name', proto_path.name)}")
                        print(f"  Description: {recipe.get('Description', '')}\n")
                        if "Requirements" in recipe:
                            print("Requirements (HAL):")
                            for k, v in recipe["Requirements"].items():
                                print(f"  {k} → {v}")
                        if "Steps" in recipe:
                            print(f"\nSteps ({len(recipe['Steps'])}):")
                            for i, s in enumerate(recipe["Steps"], 1):
                                print(f"  {i}. {s.get('Type', 'Unknown')}")
                        # --- VALIDAZIONE ---
                        valid, errors = client.validate_recipe(recipe)
                        if not valid:
                            print_error("\nVALIDAZIONE FALLITA: la ricetta non è compatibile con l'hardware attivo!")
                            for err in errors:
                                print_error(f"- {err}")
                            print_info("Carica una hardware config compatibile o modifica la ricetta.")
                            continue
                        yn = input("\nExecute? (y/n): ").strip().lower()
                        if yn == "y":
                            await client.execute_recipe(recipe)
                    else:
                        print_warning("Invalid selection")
                except Exception as e:
                    print_error(f"Error: {e}")
                continue
            _, func = commands[cmd_name]
            if func:
                result = func()
                if asyncio.iscoroutine(result):
                    await result
        except KeyboardInterrupt:
            print()
            break
        except Exception as e:
            print_error(f"Error: {e}")
    await client.disconnect()
    print_info("Goodbye!")


# ═══════════════════════════════════════════════════════════════════════════
#                              ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        asyncio.run(interactive_shell())
    except KeyboardInterrupt:
        print("\nInterrotto.")
