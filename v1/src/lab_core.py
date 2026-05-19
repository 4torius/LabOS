#!/usr/bin/env python3
"""
================================================================================
                            LabCore - Shared Core Module
================================================================================

This is the SINGLE SOURCE OF TRUTH for lab automation logic.
Both CLI and WebApp use this module for all operations.

Architecture:
    ┌─────────────┐   ┌─────────────┐
    │    CLI      │   │   WebApp    │
    └──────┬──────┘   └──────┬──────┘
           │                 │
           └────────┬────────┘
                    │
             ┌──────▼──────┐
             │  LabCore    │  ← You are here
             │  - discovery │
             │  - commands  │
             │  - files     │
             │  - workflows │
             └─────────────┘

Usage:
    from src.lab_core import LabCore
    
    core = LabCore()
    await core.discover()
    
    # List available instruments
    instruments = core.list_instruments()
    
    # Execute a command
    result = await core.execute_command("opentrons_flex", "ExecuteRecipe", {"RecipeName": "test.json"})
    
    # Get available files
    recipes = core.list_files("recipes")
    analyses = core.list_files("analyses")
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Awaitable

# Local imports
try:
    from src.discovery import PnPDiscovery, PnPServer, PnPCommand
    from src.client import PnPClient, PnPRegistry, CommandResult as PnPResult
    from src.workflow import (
        PnPWorkflowExecutor,
        InterventionRequest,
        InterventionAction,
        InterventionCallback,
        WorkflowStep,
        WorkflowProgress,
        Workflow,
        StepStatus
    )
except ImportError:
    # Running from different directory
    from discovery import PnPDiscovery, PnPServer, PnPCommand
    from client import PnPClient, PnPRegistry, CommandResult as PnPResult
    from workflow import (
        PnPWorkflowExecutor,
        InterventionRequest,
        InterventionAction,
        InterventionCallback,
        WorkflowStep,
        WorkflowProgress,
        Workflow,
        StepStatus
    )

logger = logging.getLogger(__name__)


#                           DATA CLASSES

@dataclass
class CommandParameter:
    """Parameter for a command with UI hints."""
    name: str
    display_name: str = ""
    description: str = ""
    data_type: str = "String"
    required: bool = True
    default_value: str = ""
    ui_hint: str = ""           # "recipe", "analysis", "hal_config", "liquid_class", "location", "labware"
    options: List[str] = field(default_factory=list)  # Predefined options if any
    
    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "displayName": self.display_name or self.name,
            "description": self.description,
            "type": self.data_type,
            "required": self.required,
            "default": self.default_value,
            "ui_hint": self.ui_hint,
            "options": self.options
        }


@dataclass
class Command:
    """A command that can be executed on an instrument."""
    id: str
    name: str
    description: str = ""
    feature: str = ""
    important: bool = False
    parameters: List[CommandParameter] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "feature": self.feature,
            "important": self.important,
            "parameters": [p.to_dict() for p in self.parameters]
        }


@dataclass
class Instrument:
    """A discovered instrument with its capabilities."""
    id: str
    name: str
    type: str = "instrument"
    status: str = "offline"
    host: str = "localhost"
    port: int = 0
    commands: List[Command] = field(default_factory=list)
    
    # Internal reference to PnP server
    _server: Optional[PnPServer] = field(default=None, repr=False)
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "status": self.status,
            "address": f"{self.host}:{self.port}",
            "commands": [c.to_dict() for c in self.commands]
        }


@dataclass
class ExecutionResult:
    """Result of a command execution."""
    success: bool
    message: str = ""
    data: Any = None
    error: str = ""
    
    def to_dict(self) -> Dict:
        return {
            "success": self.success,
            "message": self.message,
            "data": self.data,
            "error": self.error
        }


#                           LAB CORE

class LabCore:
    """
    Core module for lab automation.
    
    This is the shared logic layer that both CLI and WebApp use.
    """
    
    def __init__(self, base_dir: Optional[Path] = None):
        """
        Initialize LabCore.
        
        Args:
            base_dir: Base directory of the project (defaults to parent of src/)
        """
        if base_dir is None:
            base_dir = Path(__file__).parent.parent
        
        self.base_dir = Path(base_dir)
        self.library_dir = self.base_dir / "Library"
        
        # State
        self._discovery: Optional[PnPDiscovery] = None
        self._instruments: Dict[str, Instrument] = {}
        # important_commands removed (Week 3): all commands come from GetFeatures
        self._client: Optional[PnPClient] = None  # For command execution
        self._workflow_executor: Optional[PnPWorkflowExecutor] = None
        self._intervention_callback: Optional[InterventionCallback] = None
        
        # Callbacks for UI updates
        self._on_discovery_update: Optional[Callable] = None
        self._on_command_start: Optional[Callable] = None
        self._on_command_complete: Optional[Callable] = None
        self._on_log: Optional[Callable] = None
        self._on_workflow_step: Optional[Callable] = None  # Step progress callback
        
        # Cache for dynamic dropdown options (e.g., mobile_task from SiLA2 servers)
        self._dynamic_options_cache: Dict[str, List[str]] = {}

        # Discovery cache: avoid re-running gRPC discovery on every API call
        self._last_discovery_time: float = 0.0
        self._discovery_cache_ttl: float = 30.0  # overwritten by _load_config()

        # Load config
        self._load_config()
    
    #                           CONFIGURATION
    
    def _load_config(self):
        """Load configuration from lab_config.yaml."""
        from src.config_schema import load_lab_config
        config_path = self.base_dir / "lab_config.yaml"
        self._ui_dropdowns = {}
        try:
            config, _ = load_lab_config(config_path, apply_defaults=False, strict=False)
            self._ui_dropdowns = config.get("ui_dropdowns", {})
            self._discovery_cache_ttl = float(
                config.get("discovery", {}).get("scan_interval", 30)
            )
        except Exception as e:
            logger.warning(f"Error loading config: {e}")
    
    def _get_options_for_hint(self, ui_hint: str) -> List[str]:
        """
        Get dynamic options based on UI hint from lab_config.yaml.
        
        The ui_dropdowns section in lab_config.yaml defines:
        - folder: Read files from this folder
        - file_extension: Filter by file extension
        - options: Static list of options
        - dynamic: If true, options are fetched from SiLA2 server at runtime
        
        Args:
            ui_hint: Type of parameter ("recipe", "analysis", "hal_config", etc.)
            
        Returns:
            List of available options
        """
        # Catalog-backed hints — handled natively before generic folder scan
        import json as _json
        if ui_hint == "plate":
            plates_dir = self.base_dir / "Library" / "Labware" / "Plates"
            try:
                return sorted(
                    _json.loads(f.read_text(encoding="utf-8"))["id"]
                    for f in plates_dir.glob("*.plate.json")
                )
            except Exception:
                return []
        if ui_hint == "tiprack":
            idx = self.base_dir / "Library" / "Labware" / "TipRacks" / "index.json"
            if idx.exists():
                return _json.loads(idx.read_text(encoding="utf-8")).get("load_names", [])
            return []
        if ui_hint == "pipette":
            idx = self.base_dir / "Library" / "Labware" / "Pipettes" / "index.json"
            if idx.exists():
                data = _json.loads(idx.read_text(encoding="utf-8"))
                seen: set = set()
                result = []
                for d in data.get("definitions", []):
                    key = f"{d['channel']}/{d['model']}"
                    if key not in seen:
                        seen.add(key)
                        result.append(key)
                return result
            return []

        # Check if ui_hint is configured in lab_config.yaml
        dropdown_config = self._ui_dropdowns.get(ui_hint)

        if dropdown_config:
            # Dynamic options from SiLA2 server (e.g., mobile_task)
            # Note: These are populated asynchronously via API endpoint
            if dropdown_config.get("dynamic"):
                # Return cached options if available
                cached = self._dynamic_options_cache.get(ui_hint, [])
                if cached:
                    return cached
                # Return empty - actual options fetched via /api/instruments/dynamic_options
                return []
            
            # Static options
            if "options" in dropdown_config:
                return dropdown_config["options"]
            
            # Read from folder
            if "folder" in dropdown_config:
                folder_path = self.base_dir / dropdown_config["folder"]
                extension = dropdown_config.get("file_extension", "")
                
                if folder_path.exists():
                    options = []
                    for item in folder_path.iterdir():
                        if item.is_file():
                            if not extension or item.suffix == extension:
                                options.append(item.stem)
                    return sorted(options)
        
        # Fallback: Try to infer from file type (backward compatibility)
        hint_to_folder = {
            "recipe": ("Library/Recipes", ".json"),
            "analysis": ("Library/Analysis", ".mdfx"),
            "hal_config": ("Library/HardwareConfig", ".json"),
            "liquid_class": ("Library/LiquidClasses", ".json"),
        }
        
        if ui_hint in hint_to_folder:
            folder, ext = hint_to_folder[ui_hint]
            folder_path = self.base_dir / folder
            if folder_path.exists():
                return sorted([f.stem for f in folder_path.iterdir() if f.suffix == ext])
        
        # Location: discovered instruments
        if ui_hint == "location":
            return self._get_available_locations()

        return []
    
    def get_dropdown_config(self) -> Dict[str, Any]:
        """
        Get the full dropdown configuration for frontend use.
        
        Returns:
            Dict mapping ui_hint to dropdown configuration
        """
        return self._ui_dropdowns.copy()
    
    def _get_available_locations(self) -> List[str]:
        """Return discovered instrument IDs as available locations."""
        return sorted(self._instruments.keys())
    
    async def fetch_dynamic_options(self, ui_hint: str) -> List[Dict[str, str]]:
        """
        Fetch dynamic options from SiLA2 servers for dropdown population.
        
        Used for options that need to be fetched from external servers,
        like mobile robot tasks.
        
        Args:
            ui_hint: The UI hint type (e.g., "mobile_task")
            
        Returns:
            List of {id, name} dicts for dropdown options
        """
        dropdown_config = self._ui_dropdowns.get(ui_hint)
        
        if not dropdown_config or not dropdown_config.get("dynamic"):
            return []
        
        # Handle mobile_task specifically
        if ui_hint == "mobile_task":
            return await self._fetch_mobile_tasks()
        
        return []
    
    async def _fetch_mobile_tasks(self) -> List[Dict[str, Any]]:
        """
        Fetch available tasks from Library/MobileTasks/ folder.
        
        Tasks are populated by calling RefreshTasks command.
        
        Returns:
            List of {id, name, subtask_count} dicts
        """
        tasks_dir = self.library_dir / "MobileTasks"
        
        if not tasks_dir.exists():
            logger.warning(f"MobileTasks directory not found: {tasks_dir}")
            return []
        
        options = []
        for task_file in tasks_dir.glob("*.json"):
            try:
                with open(task_file, 'r', encoding='utf-8') as f:
                    task = json.load(f)
                    options.append({
                        "id": task.get("id", task_file.stem),
                        "name": task.get("name", task_file.stem),
                        "subtask_count": task.get("subtask_count", 0)
                    })
                    logger.debug(f"Loaded task from {task_file.name}: {task.get('name')}")
            except Exception as e:
                logger.warning(f"Failed to load task file {task_file}: {e}")
        
        logger.info(f"Loaded {len(options)} mobile tasks from Library/MobileTasks/")
        return options
    
    async def _save_mobile_tasks_to_library(self, tasks: List[Dict[str, Any]]) -> int:
        """
        Save mobile tasks to Library/MobileTasks/ folder.
        
        Clears the folder first, then saves each task as JSON.
        
        Args:
            tasks: List of task dicts with id, name, subtask_count
            
        Returns:
            Number of tasks saved
        """
        tasks_dir = self.library_dir / "MobileTasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        
        # Clear existing tasks
        for old_file in tasks_dir.glob("*.json"):
            try:
                old_file.unlink()
            except Exception as e:
                logger.warning(f"Failed to delete {old_file}: {e}")
        
        # Save new tasks
        saved = 0
        for task in tasks:
            task_id = task.get("id", "")
            if not task_id:
                continue
            
            task_file = tasks_dir / f"{task_id}.json"
            try:
                with open(task_file, 'w', encoding='utf-8') as f:
                    json.dump(task, f, indent=2)
                saved += 1
                logger.debug(f"Saved task: {task_id}")
            except Exception as e:
                logger.warning(f"Failed to save task {task_id}: {e}")
        
        logger.info(f"Saved {saved} mobile tasks to Library/MobileTasks/")
        return saved
    
    async def refresh_mobile_tasks(self) -> int:
        """
        Refresh mobile tasks from ROS and save to Library.
        
        Calls Get_AvailableTasks on the MobileSiLA2Server,
        then saves results to Library/MobileTasks/.
        
        Returns:
            Number of tasks found
        """
        try:
            # Get mobile instrument
            mobile = None
            for mobile_id in ["mobile", "mobile_robot", "gofa", "gofago"]:
                mobile = self.get_instrument(mobile_id)
                if mobile:
                    break
            
            if not mobile:
                for instr in self._instruments.values():
                    if "mobile" in instr.name.lower() or "gofa" in instr.name.lower():
                        mobile = instr
                        break
            
            if not mobile or mobile.status != "online":
                logger.warning("Mobile robot not available for refresh")
                return 0
            
            # Use PnPClient to call Get_AvailableTasks
            if self._client is None:
                self._client = PnPClient()
            
            server = mobile._server
            if not server:
                return 0
            
            await self._client.connect(server)
            
            result = await self._client.execute(
                server,
                "Get_AvailableTasks",
                {},
                feature="TaskManagement"
            )
            
            if not result.success:
                logger.warning(f"Get_AvailableTasks failed: {result.error}")
                return 0
            
            # Parse response
            tasks_data = result.data
            logger.info(f"Get_AvailableTasks response: {tasks_data}")
            
            tasks = []
            if isinstance(tasks_data, dict):
                tasks_raw = tasks_data.get("tasks", [])
                if isinstance(tasks_raw, str):
                    try:
                        tasks = json.loads(tasks_raw)
                    except:
                        tasks = []
                else:
                    tasks = tasks_raw
            elif isinstance(tasks_data, list):
                tasks = tasks_data
            
            # Normalize task format
            normalized_tasks = []
            for task in tasks:
                if isinstance(task, dict):
                    normalized_tasks.append({
                        "id": task.get("task_id", task.get("id", "")),
                        "name": task.get("task_name", task.get("name", "")),
                        "subtask_count": task.get("subtask_count", 0)
                    })
            
            # Save to library
            saved = await self._save_mobile_tasks_to_library(normalized_tasks)
            return saved
            
        except Exception as e:
            logger.error(f"Error refreshing mobile tasks: {e}")
            return 0
    
    #                           DISCOVERY

    def _build_port_to_key(self) -> Dict[int, str]:
        """Build port → lab_config key map for stable instrument IDs."""
        result: Dict[int, str] = {}
        try:
            import yaml as _yaml
            raw = _yaml.safe_load((self.base_dir / "lab_config.yaml").read_text())
            for k, v in (raw or {}).get("servers", {}).items():
                p = int((v or {}).get("port", 0))
                if p:
                    result[p] = k
        except Exception:
            pass
        return result

    def _server_to_instrument(self, server: PnPServer, port_to_key: Dict[int, str]) -> Instrument:
        """Convert a PnPServer (with populated features) to an Instrument."""
        inst_id = port_to_key.get(server.port) or server.name.lower().replace(" ", "_")
        commands = []
        for feature_id, cmd_id, cmd in server.get_all_commands():
            params = []
            for p in cmd.parameters:
                ui_hint = p.infer_ui_hint()
                options = p.constraints if p.constraints else []
                if not options and ui_hint:
                    options = self._get_options_for_hint(ui_hint)
                params.append(CommandParameter(
                    name=p.identifier,
                    display_name=p.display_name,
                    description=p.description,
                    data_type=p.data_type,
                    required=p.required,
                    ui_hint=ui_hint,
                    options=options,
                ))
            commands.append(Command(
                id=cmd_id,
                name=cmd.display_name or cmd_id,
                description=cmd.description,
                feature=feature_id,
                important=True,
                parameters=params,
            ))
        return Instrument(
            id=inst_id,
            name=server.name,
            type=server.server_type or "instrument",
            status="online" if server.server_online else "offline",
            host=server.host,
            port=server.port,
            commands=commands,
            _server=server,
        )

    def _on_mdns_server_discovered(self, server: PnPServer):
        """Sync callback fired by the mDNS listener when a new server appears."""
        try:
            asyncio.get_event_loop().create_task(self._handle_new_server_async(server))
        except RuntimeError:
            pass  # No running loop (shouldn't happen during server operation)

    async def _handle_new_server_async(self, server: PnPServer):
        """Query metadata for a freshly-announced mDNS server and register it."""
        if self._discovery is None:
            return
        loop = asyncio.get_running_loop()
        # Fetch SiLAService metadata + feature definitions in thread
        await loop.run_in_executor(
            None, self._discovery._try_sila2_client_query, server.host, server.port, server
        )
        port_to_key = self._build_port_to_key()
        instrument = self._server_to_instrument(server, port_to_key)
        self._instruments[instrument.id] = instrument
        logger.info(f"mDNS push: registered new instrument '{instrument.name}'")
        if self._on_discovery_update:
            self._on_discovery_update(list(self._instruments.values()))

    async def discover(self, timeout: float = 2.0) -> List[Instrument]:
        """
        Discover all available SiLA2 instruments.

        On the first call (or after the cache TTL expires) this runs a full
        bootstrap + mDNS scan and then starts a continuous mDNS listener so
        that new instruments are registered immediately when they come online
        (push, not polling).

        Returns:
            List of discovered instruments
        """
        now = time.monotonic()
        if self._instruments and (now - self._last_discovery_time) < self._discovery_cache_ttl:
            return list(self._instruments.values())

        # Stop old continuous listener before resetting the discovery instance
        if self._discovery:
            await self._discovery.stop_continuous_discovery()

        self._discovery = PnPDiscovery(self.base_dir)
        self._discovery.set_discovery_callback(self._on_mdns_server_discovered)
        await self._discovery.discover_all(timeout=timeout)

        port_to_key = self._build_port_to_key()
        self._instruments = {
            inst.id: inst
            for inst in (
                self._server_to_instrument(s, port_to_key)
                for s in self._discovery.list_servers()
            )
        }

        self._last_discovery_time = time.monotonic()

        # Start continuous mDNS listener — new servers now arrive via push callback
        await self._discovery.start_continuous_discovery()

        if self._on_discovery_update:
            self._on_discovery_update(list(self._instruments.values()))

        return list(self._instruments.values())
    
    def list_instruments(self) -> List[Instrument]:
        """Get list of discovered instruments."""
        return list(self._instruments.values())
    
    def get_instrument(self, instrument_id: str) -> Optional[Instrument]:
        """Get instrument by ID."""
        # Normalize ID
        normalized = instrument_id.lower().replace("-", "_")
        
        # Try exact match first
        if normalized in self._instruments:
            return self._instruments[normalized]
        if instrument_id in self._instruments:
            return self._instruments[instrument_id]
        
        # Try without underscores (handles OpentronsFlex vs opentrons_flex)
        no_underscore = normalized.replace("_", "")
        for inst_id, inst in self._instruments.items():
            if inst_id.replace("_", "") == no_underscore:
                return inst
        
        return None
    
    #                           COMMAND EXECUTION
    
    async def execute_command(
        self, 
        instrument_id: str, 
        command_id: str, 
        parameters: Dict[str, Any]
    ) -> ExecutionResult:
        """
        Execute a command on an instrument.
        
        Args:
            instrument_id: ID of the instrument
            command_id: ID of the command to execute
            parameters: Command parameters
            
        Returns:
            ExecutionResult with success/failure info
        """
        # Get instrument
        instrument = self.get_instrument(instrument_id)
        if not instrument:
            return ExecutionResult(
                success=False, 
                error=f"Instrument '{instrument_id}' not found"
            )
        
        # Check if online
        if instrument.status != "online":
            return ExecutionResult(
                success=False,
                error=f"Instrument '{instrument.name}' is offline"
            )
        
        # Notify start
        if self._on_command_start:
            self._on_command_start(instrument_id, command_id, parameters)
        
        self._log("info", f"Executing {command_id} on {instrument.name}...")
        
        try:
            # Use PnPClient for command execution (Strategy 0: SilaClient, Strategy 1: SiLA2Common fallback)
            if self._client is None:
                self._client = PnPClient()
            
            # Get the underlying PnPServer for execution
            server = instrument._server
            if not server:
                return ExecutionResult(
                    success=False,
                    error=f"No server reference for {instrument.name}"
                )
            
            # Connect if needed
            await self._client.connect(server)
            
            # Find the command to get feature info
            cmd = None
            for c in instrument.commands:
                if c.id == command_id:
                    cmd = c
                    break
            
            if not cmd:
                return ExecutionResult(
                    success=False,
                    error=f"Command '{command_id}' not found on {instrument.name}"
                )
            
            # Execute via PnPClient (uses SiLA2Common.ExecuteCommand)
            result = await self._client.execute(
                server, 
                command_id, 
                parameters,
                feature=cmd.feature
            )
            
            # Check result
            if not result.success:
                self._log("error", f"{command_id} failed: {result.error}")
                return ExecutionResult(
                    success=False,
                    error=result.error or "Unknown error"
                )
            
            # Special handling: after RefreshTasks, save tasks from response to Library
            if command_id == "RefreshTasks" and result.data:
                tasks_json = result.data.get("tasks", "[]")
                try:
                    if isinstance(tasks_json, str):
                        tasks = json.loads(tasks_json)
                    else:
                        tasks = tasks_json
                    saved = await self._save_mobile_tasks_to_library(tasks)
                    self._log("info", f"Saved {saved} tasks to Library/MobileTasks/")
                except Exception as e:
                    self._log("warning", f"Failed to save tasks: {e}")
            
            # Notify complete
            if self._on_command_complete:
                self._on_command_complete(instrument_id, command_id, True, result.data)
            
            self._log("success", f"{command_id} completed successfully")
            
            return ExecutionResult(
                success=True,
                message=f"{command_id} completed",
                data=result.data
            )
            
        except Exception as e:
            error_msg = str(e)
            
            # Notify complete (with error)
            if self._on_command_complete:
                self._on_command_complete(instrument_id, command_id, False, error_msg)
            
            self._log("error", f"{command_id} failed: {error_msg}")
            
            return ExecutionResult(
                success=False,
                error=error_msg
            )
    
    #                           FILE MANAGEMENT
    
    def list_files(self, file_type: str) -> List[str]:
        """
        List available files of a given type.
        
        Args:
            file_type: One of "recipes", "analyses", "hal", "liquidclasses", "workflows", "protocols"
            
        Returns:
            List of filenames
        """
        type_map = {
            "recipes": ("Recipes", "*.json"),
            "analyses": ("Analysis", "*.mdfx"),
            "protocols": ("Analysis", "*.mdfx"),  # Alias
            "hal": ("HardwareConfig", "*.json"),
            "hal_config": ("HardwareConfig", "*.json"),  # Alias
            "liquidclasses": ("LiquidClasses", "*.json"),
            "liquid_class": ("LiquidClasses", "*.json"),  # Alias
            "workflows": ("Workflows", "*.workflow.json"),
        }
        
        if file_type not in type_map:
            logger.warning(f"Unknown file type: {file_type}")
            return []
        
        folder, pattern = type_map[file_type]
        target_dir = self.library_dir / folder
        
        if not target_dir.exists():
            return []
        
        return [f.name for f in target_dir.glob(pattern)]
    
    def get_file_path(self, file_type: str, filename: str) -> Optional[Path]:
        """Get full path to a file."""
        type_map = {
            "recipes": "Recipes",
            "analyses": "Analysis",
            "protocols": "Analysis",
            "hal": "HardwareConfig",
            "hal_config": "HardwareConfig",
            "liquidclasses": "LiquidClasses",
            "liquid_class": "LiquidClasses",
            "workflows": "Workflows",
        }
        
        if file_type not in type_map:
            return None
        
        folder = type_map[file_type]
        path = self.library_dir / folder / filename
        
        return path if path.exists() else None
    
    def load_file(self, file_type: str, filename: str) -> Optional[Dict]:
        """Load and parse a JSON file."""
        path = self.get_file_path(file_type, filename)
        
        if not path:
            return None
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading {filename}: {e}")
            return None
    
    #                           WORKFLOW EXECUTION
    
    async def execute_workflow(
        self,
        workflow_name: str,
        on_step_start: Optional[Callable] = None,
        on_step_complete: Optional[Callable] = None,
        on_intervention_needed: Optional[Callable] = None,
        parallel: Optional[bool] = None,
    ) -> ExecutionResult:
        """
        Execute a workflow using PnPWorkflowExecutor with retry and intervention support.
        
        Args:
            workflow_name: Name of the workflow file
            on_step_start: Callback when a step starts
            on_step_complete: Callback when a step completes
            on_intervention_needed: Callback when human intervention is needed
            
        Returns:
            ExecutionResult
        """
        # Load workflow
        workflow = self.load_file("workflows", workflow_name)
        
        if not workflow:
            return ExecutionResult(
                success=False,
                error=f"Workflow '{workflow_name}' not found"
            )
        
        steps = workflow.get("Steps", [])
        total_steps = len(steps)
        
        self._log("info", f"Starting workflow: {workflow.get('WorkflowName', workflow_name)}")
        
        # Initialize workflow executor if not already done
        if self._workflow_executor is None:
            registry = PnPRegistry()
            for inst_id, inst in self._instruments.items():
                if inst._server:
                    registry.register(inst_id, inst._server)
            self._workflow_executor = PnPWorkflowExecutor(registry)

        # Set intervention callback if available
        if self._intervention_callback:
            self._workflow_executor.set_intervention_callback(self._intervention_callback)

        # Build Workflow object directly from the loaded file dict
        workflow_obj = Workflow.from_dict(workflow)

        # Progress callback bridges WorkflowProgress to the caller's step callbacks
        def on_step_progress(progress: WorkflowProgress):
            if on_step_start and progress.step_status == StepStatus.RUNNING:
                on_step_start(progress.current_step, total_steps, {
                    "instrument": progress.step_instrument,
                    "action": progress.step_action
                })
            if on_step_complete and progress.step_status in (StepStatus.SUCCESS, StepStatus.FAILED, StepStatus.SKIPPED):
                on_step_complete(progress.current_step, total_steps, {
                    "success": progress.step_status == StepStatus.SUCCESS,
                    "skipped": progress.step_status == StepStatus.SKIPPED,
                    "error": progress.message if progress.step_status == StepStatus.FAILED else None
                })
            if self._on_workflow_step:
                self._on_workflow_step(
                    progress.current_step, total_steps,
                    progress.step_status.value,
                    progress.step_instrument,
                    progress.step_action
                )

        self._workflow_executor.add_progress_callback(on_step_progress)

        # Determine parallel flag: caller overrides config; config overrides default False
        if parallel is None:
            try:
                from src.config_schema import load_lab_config
                _cfg, _ = load_lab_config(self.base_dir / "lab_config.yaml", apply_defaults=False, strict=False)
                parallel = bool(_cfg.get("workflow", {}).get("parallel_execution", False))
            except Exception:
                parallel = False

        try:
            result = await self._workflow_executor.execute(workflow_obj, validate=False, parallel=parallel)

            self._workflow_executor.remove_progress_callback(on_step_progress)

            if result.success:
                self._log("success", f"Workflow completed: {result.steps_completed}/{total_steps} steps")
                return ExecutionResult(
                    success=True,
                    message=f"Workflow completed ({result.steps_completed} steps in {result.duration_seconds:.1f}s)"
                )
            else:
                return ExecutionResult(
                    success=False,
                    error="; ".join(result.errors) if result.errors else "Workflow failed"
                )
        except Exception as e:
            self._workflow_executor.remove_progress_callback(on_step_progress)
            self._log("error", f"Workflow execution error: {e}")
            return ExecutionResult(success=False, error=str(e))
    
    #                           CALLBACKS & LOGGING
    
    def set_callbacks(
        self,
        on_discovery_update: Optional[Callable] = None,
        on_command_start: Optional[Callable] = None,
        on_command_complete: Optional[Callable] = None,
        on_log: Optional[Callable] = None,
        on_workflow_step: Optional[Callable] = None
    ):
        """Set UI callbacks for status updates."""
        self._on_discovery_update = on_discovery_update
        self._on_command_start = on_command_start
        self._on_command_complete = on_command_complete
        self._on_log = on_log
        self._on_workflow_step = on_workflow_step
    
    def set_intervention_callback(self, callback: Optional[InterventionCallback]):
        """
        Set the human intervention callback for workflow execution.
        
        The callback is invoked when a workflow step fails after all retries.
        It should return an InterventionAction (RETRY, SKIP, or ABORT).
        
        For CLI: Implement interactive prompt
        For WebApp: Implement WebSocket-based UI notification
        
        Args:
            callback: Async function taking InterventionRequest, returning InterventionAction
        """
        self._intervention_callback = callback
    
    def _log(self, level: str, message: str):
        """Log a message and notify callback."""
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        elif level == "success":
            logger.info(f"✓ {message}")
        else:
            logger.info(message)
        
        if self._on_log:
            self._on_log(level, message)
    
    #                           UTILITY METHODS
    
    def get_all_commands_dict(self) -> Dict[str, Any]:
        """
        Get all instruments and commands in API format.
        
        This is the same format used by /api/instruments/commands.
        """
        instruments = {}
        
        for inst_id, instrument in self._instruments.items():
            instruments[inst_id] = instrument.to_dict()
        
        return {
            "source": "lab_core",
            "instruments": instruments
        }


#                           SINGLETON INSTANCE

_core_instance: Optional[LabCore] = None

def get_lab_core(base_dir: Optional[Path] = None) -> LabCore:
    """
    Get the singleton LabCore instance.
    
    This ensures CLI and WebApp share the same state.
    """
    global _core_instance
    
    if _core_instance is None:
        _core_instance = LabCore(base_dir)
    
    return _core_instance


#                           TEST / DEMO

async def _demo():
    """Demo usage of LabCore."""
    core = LabCore()
    instruments = await core.discover()
    logger.info(f"Found {len(instruments)} instruments:")
    for inst in instruments:
        logger.info(f"  {inst.name} ({inst.status}) — {len(inst.commands)} commands")
    logger.info(f"Recipes: {core.list_files('recipes')}")
    logger.info(f"Analyses: {core.list_files('analyses')}")
    logger.info(f"HAL configs: {core.list_files('hal')}")


if __name__ == "__main__":
    asyncio.run(_demo())
