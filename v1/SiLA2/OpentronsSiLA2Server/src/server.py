"""
OpentronsSiLA2Server - Main SiLA2 Server Implementation
=======================================================

A robust SiLA2 server for Opentrons Flex robot control.

Features:
    - Full SiLA2 gRPC interface
    - Hardware Abstraction Layer (HAL)
    - JSON recipe validation and execution
    - Tip tracking with crash recovery
    - Image extraction from run logs
    - Directory-based file processing
    - Comprehensive logging
"""

import asyncio
import glob
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from concurrent import futures
from typing import Any, Callable, Dict, List, Optional

import grpc
import httpx

from .config import ServerConfig, setup_logging

# Add parent directory for SiLA2 common modules
_sila2_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _sila2_path not in sys.path:
    sys.path.insert(0, _sila2_path)

# mDNS registration
try:
    from sila2_mdns_registry import SiLA2ServerRegistry
    MDNS_AVAILABLE = True
except ImportError:
    MDNS_AVAILABLE = False
from .robot_client import RobotClient
from .protocol_generator import ProtocolGenerator
from .hardware_manager import HardwareManager
from .tip_tracker import TipTracker, calculate_tips_from_recipe

# ManualStation gRPC client for operator interactions
try:
    import importlib

    # Ensure repository root is importable so "src.pnp_stubs" can be resolved.
    _repo_root = os.path.dirname(_sila2_path)
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

    # Preferred path: shared stubs package used by webapp and clients.
    manual_pb2 = importlib.import_module("src.pnp_stubs.ManualStationService_pb2")
    manual_pb2_grpc = importlib.import_module("src.pnp_stubs.ManualStationService_pb2_grpc")
    MANUAL_STATION_AVAILABLE = True
except ImportError:
    try:
        import importlib

        # Fallback path: stubs generated inside ManualStationSiLA2Server/src.
        _manual_src = os.path.join(_sila2_path, "ManualStationSiLA2Server", "src")
        if _manual_src not in sys.path:
            sys.path.insert(0, _manual_src)

        manual_pb2 = importlib.import_module("ManualStationService_pb2")
        manual_pb2_grpc = importlib.import_module("ManualStationService_pb2_grpc")
        MANUAL_STATION_AVAILABLE = True
    except ImportError:
        manual_pb2 = None
        manual_pb2_grpc = None
        MANUAL_STATION_AVAILABLE = False

# Import gRPC servicer
try:
    from .opentrons_servicer import add_servicer_to_server
    SERVICER_AVAILABLE = True
except ImportError:
    SERVICER_AVAILABLE = False

# Import SiLA2Common for plug-and-play support
try:
    import importlib.util
    # Try to load from sibling SiLA2 directory
    common_pb2_path = os.path.join(_sila2_path, "SiLA2Common_pb2.py")
    if os.path.exists(common_pb2_path):
        spec = importlib.util.spec_from_file_location("SiLA2Common_pb2", common_pb2_path)
        if spec and spec.loader:
            SiLA2Common_pb2 = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(SiLA2Common_pb2)
            
            grpc_path = os.path.join(_sila2_path, "SiLA2Common_pb2_grpc.py")
            spec_grpc = importlib.util.spec_from_file_location("SiLA2Common_pb2_grpc", grpc_path)
            if spec_grpc and spec_grpc.loader:
                SiLA2Common_pb2_grpc = importlib.util.module_from_spec(spec_grpc)
                spec_grpc.loader.exec_module(SiLA2Common_pb2_grpc)
            else:
                SiLA2Common_pb2_grpc = None
        else:
            SiLA2Common_pb2 = None
            SiLA2Common_pb2_grpc = None
    else:
        SiLA2Common_pb2 = None
        SiLA2Common_pb2_grpc = None
    SILA2_COMMON_AVAILABLE = SiLA2Common_pb2_grpc is not None
except Exception as e:
    SiLA2Common_pb2 = None
    SiLA2Common_pb2_grpc = None
    SILA2_COMMON_AVAILABLE = False

logger = logging.getLogger(__name__)


#                    SILA2 COMMON ADAPTER (Plug & Play)

class SiLA2CommonAdapter:
    """
    Adapter to provide SiLA2Common interface for plug-and-play discovery.
    
    This allows generic clients to:
    1. Discover server capabilities via GetFeatures()
    2. Execute any command via ExecuteCommand(feature, command, params)
    """
    
    def __init__(self, server: 'OpentronsSiLA2Server'):
        self._server = server
        self._servicer = None  # Set after server starts
        self._start_time = time.time()
        
        # Map of command_id -> handler method
        self._command_handlers = {}
        
    def set_servicer(self, servicer):
        """Set the OpentronsServicer reference."""
        self._servicer = servicer

    @staticmethod
    def _to_string_map(data: dict) -> dict:
        """Convert any result payload to map<string, string> for SiLA2Common."""
        if not isinstance(data, dict):
            return {"value": str(data)}

        converted = {}
        for key, value in data.items():
            if value is None:
                converted[str(key)] = ""
            elif isinstance(value, str):
                converted[str(key)] = value
            elif isinstance(value, (dict, list, tuple)):
                converted[str(key)] = json.dumps(value, ensure_ascii=False)
            else:
                converted[str(key)] = str(value)
        return converted
        
    async def GetServerInfo(self, request, context):
        """Return server metadata for discovery."""
        uptime = int(time.time() - self._start_time)
        robot_info = {}

        if self._server.robot and self._server.is_connected():
            try:
                health = await self._server.robot.get_health()
                if health:
                    robot_info = {
                        "robot_name": health.get("name", ""),
                        "robot_model": health.get("robot_model", "Opentrons Flex"),
                        "serial": health.get("robot_serial", ""),
                    }
            except Exception:
                pass

        # Read description from .sila.xml
        _features_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "features"
        )
        description = "SiLA2 Server for Opentrons Flex liquid handler"
        try:
            from sila2_xml_parser import features_from_xml_dir
            xml_features = features_from_xml_dir(_features_dir)
            if xml_features:
                description = xml_features[0].get('description', description)
        except Exception:
            pass

        return SiLA2Common_pb2.ServerInfoResponse(
            server_name="OpentronsFlex",
            server_type="liquid_handler",
            vendor="Opentrons / BicoccaLab",
            model=robot_info.get("robot_model", "Opentrons Flex"),
            serial_number=robot_info.get("serial", ""),
            server_version=self._server.config.version,
            sila_version="2.0",
            description=description,
            host=self._server.config.host,
            uptime_seconds=uptime,
            capabilities=["liquid_handling", "module_control", "recipe_execution", "tip_tracking"]
        )
    
    async def GetFeatures(self, request, context):
        """Return available features — read from features/*.sila.xml."""
        _features_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "features"
        )
        try:
            from sila2_xml_parser import features_from_xml_dir, build_proto_features
            xml_features = features_from_xml_dir(_features_dir)
            proto_features = build_proto_features(xml_features, SiLA2Common_pb2)
        except Exception as e:
            logger.warning(f"Could not load features from XML: {e}")
            proto_features = []
        return SiLA2Common_pb2.FeaturesResponse(features=proto_features)
    
    async def GetStatus(self, request, context):
        """Return current server status."""
        connected = self._server.is_connected() if self._server else False
        robot_status = self._server._status if self._server else "unknown"
        is_busy = self._server._is_running if self._server else False

        return SiLA2Common_pb2.StatusResponse(
            status="running",
            server_online=True,
            hardware_online=connected,
            hardware_status="connected" if connected else "disconnected",
            details={
                "robot_status": robot_status,
                "current_run_id": self._server._current_run_id or "",
                "is_busy": str(is_busy),
            }
        )
    
    async def ExecuteCommand(self, request, context):
        """
        Execute any command generically.
        
        This is the core of plug-and-play - allows execution without
        knowing the specific protobuf types.
        """
        feature = request.feature
        command = request.command
        params = dict(request.parameters) if request.parameters else {}
        
        logger.info(f"[PnP] ExecuteCommand: {feature}.{command} params={list(params.keys())}")
        
        try:
            # Route to appropriate handler based on command
            result = await self._execute_command_internal(feature, command, params)
            result_map = self._to_string_map(result)
            
            yield SiLA2Common_pb2.ExecuteCommandResponse(
                success=True,
                result=result_map,
                is_intermediate=False,
                progress=100
            )
            
        except Exception as e:
            logger.error(f"[PnP] ExecuteCommand failed: {e}")
            yield SiLA2Common_pb2.ExecuteCommandResponse(
                success=False,
                error=str(e),
                is_intermediate=False,
                progress=0
            )
    
    async def _execute_command_internal(self, feature: str, command: str, params: dict) -> dict:
        """Internal command routing."""
        
        # Robot Control commands
        if command == "Initialize":
            result = await self._server.cmd_initialize()
            return {"message": result}
            
        elif command == "Home":
            result = await self._server.cmd_home()
            return {"message": result}
            
        elif command == "EmergencyStop":
            result = await self._server.cmd_emergency_stop()
            return {"message": result}
            
        elif command == "RunProtocol":
            content = params.get("protocol_content", "")
            ptype = params.get("protocol_type", "python")
            result = await self._server.cmd_run_protocol(content, ptype)
            return {"run_id": result}
            
        elif command == "ExecuteRecipeByName" or command == "ExecuteRecipe":
            # Accept RecipeName (from XML), recipe_name, Recipe, or recipe as parameter names
            name = params.get("RecipeName", params.get("recipe_name", params.get("Recipe", params.get("recipe", ""))))
            run_id, result = await self._server.cmd_execute_recipe_by_name(name)
            return {"run_id": run_id, "result": result}
            
        elif command == "AbortRun":
            result = await self._server.cmd_abort_run()
            return {"message": result}
            
        elif command == "PauseRun":
            result = await self._server.cmd_pause_run()
            return {"message": result}
            
        elif command == "ResumeRun":
            result = await self._server.cmd_resume_run()
            return {"message": result}
            
        # Tip management
        elif command == "RefillTipRack":
            rack_name = params.get("rack_name", params.get("rack_type", ""))
            result = await self._server.cmd_refill_tip_rack(rack_name)
            return {"message": result}
            
        # Heater-Shaker commands
        elif command == "HeaterShakerSetTemp":
            temp = float(params.get("temperature", 25))
            module_id = params.get("module_id", params.get("serial", ""))
            result = await self._server.cmd_heater_shaker_set_temp(module_id, temp)
            return {"message": result}
            
        elif command == "HeaterShakerShake":
            rpm = int(params.get("rpm", 500))
            module_id = params.get("module_id", params.get("serial", ""))
            result = await self._server.cmd_heater_shaker_shake(module_id, rpm)
            return {"message": result}
            
        elif command == "HeaterShakerStop":
            module_id = params.get("module_id", params.get("serial", ""))
            # Use deactivate with heater off but latch open (stops shaking)
            result = await self._server.cmd_heater_shaker_deactivate(module_id, deactivate_heater=False, open_latch=False)
            return {"message": result}
            
        elif command == "HeaterShakerOpenLatch":
            module_id = params.get("module_id", params.get("serial", ""))
            result = await self._server.cmd_heater_shaker_open_latch(module_id)
            return {"message": result}
            
        elif command == "HeaterShakerCloseLatch":
            module_id = params.get("module_id", params.get("serial", ""))
            result = await self._server.cmd_heater_shaker_close_latch(module_id)
            return {"message": result}
        
        # Status queries
        elif command == "GetStatus" or command == "GetRobotInfo":
            if self._server.robot and self._server.is_connected():
                health = await self._server.robot.get_health()
                return health or {"status": "connected"}
            return {"status": "disconnected"}
            
        elif command == "GetTipStatus":
            status = self._server.get_tip_status()
            return json.loads(status) if isinstance(status, str) else status
            
        elif command == "ListHardwareConfigs":
            configs = self._server.list_hardware_configs()
            return {"configs": configs}
            
        elif command == "LoadHardwareConfig" or command == "SwitchHardwareConfig":
            # Accept ConfigName (from XML), config_name, or config as parameter names
            config_name = params.get("ConfigName", params.get("config_name", params.get("config", "")))
            result = self._server.switch_hardware_config(config_name)
            return {"message": result}
        
        elif command == "GetCurrentConfig":
            config = self._server.get_current_hardware_config()
            return {"config": config}
        
        elif command == "ListRecipes":
            recipes = self._server.list_recipes()
            return {"recipes": recipes}
        
        elif command == "ListTipRacks":
            tip_status = self._server.get_tip_status()
            return json.loads(tip_status) if isinstance(tip_status, str) else tip_status
        
        elif command == "ResetTipTracking":
            result = self._server.reset_tip_tracking()
            return {"message": result}
        
        elif command == "SetLights":
            # Accept On (from XML), on, or lights as parameter names
            on = params.get("On", params.get("on", params.get("lights", True)))
            if isinstance(on, str):
                on = on.lower() in ("true", "1", "yes", "on")
            result = await self._server.cmd_set_lights(on)
            return {"message": result, "lights_on": on}
        
        elif command == "GetModulesStatus":
            modules = await self._server.cmd_get_modules_status()
            return {"modules": modules}
        
        else:
            raise ValueError(f"Unknown command: {feature}.{command}")
    
    async def GetProperty(self, request, context):
        """Get a property value."""
        prop_name = request.property_name
        
        if prop_name == "is_connected":
            return SiLA2Common_pb2.PropertyResponse(
                property_name=prop_name,
                value=str(self._server.is_connected())
            )
        elif prop_name == "status":
            return SiLA2Common_pb2.PropertyResponse(
                property_name=prop_name,
                value=self._server._status
            )
        elif prop_name == "current_run_id":
            return SiLA2Common_pb2.PropertyResponse(
                property_name=prop_name,
                value=self._server._current_run_id or ""
            )
        elif prop_name == "hardware_config":
            return SiLA2Common_pb2.PropertyResponse(
                property_name=prop_name,
                value=self._server.get_current_hardware_config()
            )
        else:
            return SiLA2Common_pb2.PropertyResponse(
                property_name=prop_name,
                value="",
                error=f"Unknown property: {prop_name}"
            )


class OpentronsSiLA2Server:
    """
    Main SiLA2 Server for Opentrons Flex.
    
    Implements OpentronsFlex, LiquidHandling, and ModuleControl features.
    Complete port of C# OpentronsBridge functionality with improvements.
    """
    
    def __init__(self, config: ServerConfig):
        """
        Initialize the server.
        
        Args:
            config: Server configuration
        """
        self.config = config
        self.logger = setup_logging(config)
        
        # Base directory (parent of src/)
        from pathlib import Path
        self.base_dir = Path(__file__).parent.parent.parent
        self.hardware_config_dir = Path(config.hardware_config_folder)
        
        # Components (initialized in initialize())
        self.robot: Optional[RobotClient] = None
        self.generator: Optional[ProtocolGenerator] = None
        self.hardware: Optional[HardwareManager] = None
        self.tip_tracker: Optional[TipTracker] = None
        
        # State
        self._current_run_id: Optional[str] = None
        self._is_running = False
        self._status = "disconnected"
        self._lock = asyncio.Lock()
        
        # gRPC Server
        self._grpc_server = None
        
        # mDNS Registry for automatic discovery
        self._mdns_registry: Optional[SiLA2ServerRegistry] = None
        if MDNS_AVAILABLE:
            # Derive feature IDs from .sila.xml so TXT records stay in sync
            _features_dir = str(Path(__file__).parent.parent / "features")
            try:
                from sila2_xml_parser import features_from_xml_dir
                _feature_ids = [f['identifier'] for f in features_from_xml_dir(_features_dir)]
            except Exception:
                _feature_ids = ["WorkflowAPI"]
            _feature_ids.append("SiLA2Common")

            self._mdns_registry = SiLA2ServerRegistry(
                name="OpentronsFlex",
                port=config.port,
                features=_feature_ids,
                vendor="BicoccaLab",
                version=config.version,
                server_type="Real"
            )
        
    # ═══════════════════════════════════════════════════════════════════
    #                      INITIALIZATION
    # ═══════════════════════════════════════════════════════════════════
        
    def _initialize_directories(self):
        """Create all required directories."""
        dirs = [
            self.config.dir_input,
            self.config.dir_processed,
            self.config.dir_errors,
            self.config.dir_output,
            self.config.dir_images,
            self.config.dir_logs,
            self.config.dir_temp,
        ]
        for d in dirs:
            os.makedirs(d, exist_ok=True)
        self.logger.info("Directories initialized")
        
    async def initialize(self):
        """Initialize all components."""
        self.logger.info("Initializing OpentronsSiLA2Server...")
        
        # Create directories
        self._initialize_directories()
        
        # Initialize robot client
        self.robot = RobotClient(
            host=self.config.robot_ip,
            port=self.config.robot_port,
            timeout=self.config.robot_timeout,
            local_address=self.config.robot_local_address
        )
        
        # Initialize protocol generator
        self.generator = ProtocolGenerator(temp_dir=self.config.dir_temp)
        
        # Initialize hardware manager
        hw_folder = os.path.abspath(self.config.hardware_config_folder)
        if os.path.exists(hw_folder):
            hw_file = os.path.join(hw_folder, self.config.hardware_default_config)
            if os.path.exists(hw_file):
                self.hardware = HardwareManager(config_path=hw_file, config_folder=hw_folder)
            else:
                self.hardware = HardwareManager(config_folder=hw_folder)
            self.logger.info(f"Hardware config folder: {hw_folder}")
            self.logger.info(f"Available configs: {self.hardware.list_available_configs()}")
        else:
            self.logger.warning(f"Hardware config folder not found: {hw_folder}")
            
        # Initialize tip tracker
        if self.config.tip_tracking_enabled:
            self.tip_tracker = TipTracker(self.config.tip_state_file)
            
        # Initialize tip racks from HAL config
        self._initialize_tip_racks_from_hal()
            
        # Create emergency reset file
        self._ensure_emergency_file()
            
        self.logger.info("Components initialized")
        
    def _ensure_emergency_file(self):
        """Create emergency reset protocol if it doesn't exist."""
        emergency_path = os.path.abspath(self.config.emergency_protocol)
        if not os.path.exists(emergency_path):
            emergency_recipe = {
                "ProtocolName": "EMERGENCY_RESET",
                "SafetyCleanup": True,
                "Labware": {},
                "Trash": {"Bin": {"Type": "TrashBin", "Slot": "A3"}},
                "Pipettes": {"left": "flex_1channel_1000"},
                "Steps": [
                    {"Command": "Comment", "Text": "SAFETY RESET"},
                    {"Command": "DropTip", "PipetteMount": "left", "TrashLocation": "Bin", "Force": True},
                    {"Command": "Home"}
                ]
            }
            os.makedirs(os.path.dirname(emergency_path), exist_ok=True)
            with open(emergency_path, 'w', encoding='utf-8') as f:
                json.dump(emergency_recipe, f, indent=2)
            self.logger.info(f"Created emergency reset file: {emergency_path}")
        
    # ═══════════════════════════════════════════════════════════════════
    #                      SERVER LIFECYCLE
    # ═══════════════════════════════════════════════════════════════════
        
    async def start(self):
        """Start the SiLA2 server."""
        await self.initialize()
        
        # Connect to robot with retry
        async def on_retry(attempt, max_attempts):
            self.logger.info(f"Retry {attempt}/{max_attempts}...")
            
        connected = await self.robot.connect_with_retry(
            max_retries=self.config.connection_retry_count,
            retry_delay=self.config.connection_retry_delay,
            on_retry=on_retry
        )
        
        if connected:
            health = await self.robot.get_health()
            self.logger.info(f"Robot connected: {health.get('name', 'Unknown')}")
            self._status = "idle"
        else:
            self.logger.error("Robot not found after 3 retries - server cannot start")
            raise ConnectionError(f"Opentrons robot not found at {self.config.robot_ip}:{self.config.robot_port}. Ensure the robot is powered on and connected to the network.")
            
        # Create gRPC server
        self._grpc_server = grpc.aio.server(
            futures.ThreadPoolExecutor(max_workers=10)
        )
        
        # Register gRPC servicer if available
        if SERVICER_AVAILABLE:
            servicer = add_servicer_to_server(self, self._grpc_server)
            self.logger.info("OpentronsServicer registered")
        else:
            servicer = None
            self.logger.warning("gRPC servicer not available - server will have no RPC methods")
        
        # Register SiLA2Common adapter for plug-and-play
        if SILA2_COMMON_AVAILABLE:
            self._common_adapter = SiLA2CommonAdapter(self)
            if servicer:
                self._common_adapter.set_servicer(servicer)
            SiLA2Common_pb2_grpc.add_SiLA2ServerInfoServicer_to_server(
                self._common_adapter, self._grpc_server
            )
            self.logger.info("SiLA2Common adapter registered (Plug & Play enabled)")
        else:
            self.logger.warning("SiLA2Common not available - Plug & Play disabled")
        
        # Bind to port
        address = f"{self.config.host}:{self.config.port}"
        self._grpc_server.add_insecure_port(address)
        
        # Start server
        self.logger.info(f"Starting SiLA2 server on {address}...")
        await self._grpc_server.start()
        
        # Register on mDNS for automatic discovery
        if self._mdns_registry:
            await self._mdns_registry.register()
        
        self._print_banner()
        
        # Keep server running
        await self._grpc_server.wait_for_termination()
        
    def _print_banner(self):
        """Print server startup banner."""
        self.logger.info("=" * 60)
        self.logger.info(f"  OpentronsSiLA2Server v{self.config.version}")
        self.logger.info(f"  gRPC: {self.config.host}:{self.config.port}")
        self.logger.info(f"  Robot: {self.config.robot_ip}:{self.config.robot_port}")
        if self.hardware:
            self.logger.info(f"  Hardware: {self.hardware.get_current_config_name()}")
        self.logger.info("=" * 60)
        
    async def stop(self):
        """Stop the server gracefully."""
        self.logger.info("Shutting down...")
        
        # Unregister from mDNS
        if self._mdns_registry:
            await self._mdns_registry.unregister()
        
        if self.robot:
            await self.robot.disconnect()
            
        if self._grpc_server:
            await self._grpc_server.stop(grace=5)
            
        self.logger.info("Server stopped")
        
    # ═══════════════════════════════════════════════════════════════════
    #                 HARDWARE CONFIG MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════
    
    def list_hardware_configs(self) -> List[str]:
        """List available hardware configurations."""
        if self.hardware:
            return self.hardware.list_available_configs()
        return []
        
    def switch_hardware_config(self, config_name: str) -> str:
        """Switch to a different hardware configuration."""
        if not self.hardware:
            return "Hardware manager not initialized"
            
        if self.hardware.switch_config(config_name):
            # Initialize tip racks from the new HAL config
            self._initialize_tip_racks_from_hal()
            return f"Switched to: {self.hardware.get_current_config_name()}"
        return f"Failed to switch to: {config_name}"
    
    # Alias for simplified API compatibility
    load_hardware_config = switch_hardware_config
    
    def _initialize_tip_racks_from_hal(self):
        """Synchronize tip_state with tip racks defined in current HAL config."""
        if not self.hardware or not self.tip_tracker:
            return

        hal_tips = self.hardware.get_configured_tip_racks()
        allowed_load_names = set(hal_tips.values())

        summary = self.tip_tracker.sync_with_allowed_types(allowed_load_names)

        self.logger.info(
            "Tip state synchronized with HAL | "
            f"kept={summary['kept']} added={summary['added']} removed={summary['removed']}"
        )
        
    def get_current_hardware_config(self) -> str:
        """Get current hardware configuration name."""
        if self.hardware:
            return self.hardware.get_current_config_name()
        return "None"
        
    # ═══════════════════════════════════════════════════════════════════
    #                    JSON VALIDATION
    # ═══════════════════════════════════════════════════════════════════
    
    def validate_json_recipe(self, recipe: Dict[str, Any]) -> tuple:
        """
        Validate a JSON recipe structure.
        
        Args:
            recipe: Recipe dictionary
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if not recipe:
            return False, "Recipe is empty or invalid"
            
        # Check for required fields
        if "ProtocolName" not in recipe and "Command" not in recipe:
            return False, "Missing 'ProtocolName' or 'Command'"
            
        # Special commands (Refill) don't need more validation
        if recipe.get("Command") == "Refill":
            return True, ""
            
        # After HAL processing, Labware should exist
        if "Labware" not in recipe and "Requirements" not in recipe:
            return False, "Missing 'Labware' section (use 'Requirements' for HAL mapping)"
            
        # Check Steps
        steps = recipe.get("Steps", [])
        if not steps:
            return False, "Missing or empty 'Steps' list"
            
        # Validate each step
        for i, step in enumerate(steps):
            # Skip comment-only steps (legacy format)
            if "Comment" in step and "Command" not in step:
                continue
            if "Command" not in step:
                return False, f"Step {i} missing 'Command'"
                
        return True, ""
    
    def validate_recipe_against_hal(self, recipe: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate a recipe's requirements against the currently loaded HAL config.
        
        Args:
            recipe: Recipe dictionary
            
        Returns:
            Dict with 'valid', 'errors', and 'hal_config' keys
        """
        result = {
            "valid": True,
            "errors": [],
            "hal_config": self.hardware.get_current_config_name() if self.hardware else "None"
        }
        
        if not self.hardware:
            result["valid"] = False
            result["errors"].append("No hardware config loaded. Load a HAL config first.")
            return result
        
        is_valid, errors = self.hardware.validate_recipe_requirements(recipe)
        result["valid"] = is_valid
        result["errors"] = errors
        
        return result
    
    # ═══════════════════════════════════════════════════════════════════
    #                    MANUAL STATION INTEGRATION
    # ═══════════════════════════════════════════════════════════════════
    
    async def _request_tip_refill(self, rack_type: str, location: str, needed: int, available: int) -> bool:
        """
        Request operator to refill tip rack via webapp or ManualStation.
        
        First tries webapp notification (preferred), then falls back to ManualStation.
        
        Args:
            rack_type: Tiprack load name
            location: Deck location (e.g., "C3")
            needed: Tips needed for recipe
            available: Tips currently available
            
        Returns:
            True if operator confirmed refill, False if cancelled/timeout
        """
        # Strategy 1: Try webapp notification (preferred)
        webapp_url = os.environ.get("WEBAPP_URL", "http://127.0.0.1:5000")
        
        self.logger.info(f"[TIP REFILL] Requesting refill for {rack_type} via webapp at {webapp_url}")
        
        try:
            notification_id = int(time.time() * 1000)
            notification = {
                "id": notification_id,
                "type": "operator_notification",
                "title": "🔄 Refill Tip Rack",
                "message": (
                    f"REFILL TIPRACK RICHIESTO\n\n"
                    f"Tipo: {rack_type}\n"
                    f"Posizione: {location}\n"
                    f"Tips necessari: {needed}\n"
                    f"Tips disponibili: {available}\n\n"
                    f"Sostituire il tiprack con uno pieno e confermare."
                ),
                "priority": "urgent",
                "requires_action": True,
                "action": "RefillTipRack",
                "params": {
                    "rack_type": rack_type,
                    "location": location,
                    "needed": needed,
                    "available": available
                }
            }
            
            self.logger.info(f"[TIP REFILL] Sending notification ID {notification_id} to {webapp_url}/api/operator/notify")
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Send notification to webapp
                response = await client.post(
                    f"{webapp_url}/api/operator/notify",
                    json=notification
                )
                
                if response.status_code == 200:
                    self.logger.info(f"Sent tip refill notification to webapp: {notification_id}")
                    
                    # Poll for acknowledgment (timeout after 1 hour)
                    timeout_seconds = 3600
                    poll_interval = 2.0
                    elapsed = 0.0
                    
                    while elapsed < timeout_seconds:
                        await asyncio.sleep(poll_interval)
                        elapsed += poll_interval
                        
                        try:
                            pending_resp = await client.get(f"{webapp_url}/api/operator/pending")
                            if pending_resp.status_code == 200:
                                pending = pending_resp.json()
                                # Convert IDs to int for consistent comparison
                                pending_ids = [int(a.get("id", 0)) for a in pending.get("actions", [])]
                                
                                # If our notification is no longer pending, operator acknowledged
                                if notification_id not in pending_ids:
                                    self.logger.info(f"Operator confirmed tip refill for {rack_type}")
                                    return True
                        except Exception as poll_e:
                            self.logger.warning(f"Error polling webapp: {poll_e}")
                    
                    self.logger.warning(f"Tip refill request timed out for {rack_type}")
                    return False
                else:
                    self.logger.warning(f"Webapp notify failed: {response.status_code}")
                    
        except Exception as e:
            self.logger.warning(f"Webapp notification failed: {e}, trying ManualStation...")
        
        # Strategy 2: Fallback to ManualStation gRPC
        if not MANUAL_STATION_AVAILABLE:
            self.logger.warning("ManualStation not available - cannot request refill")
            return False
            
        try:
            # Connect to ManualStation
            channel = grpc.aio.insecure_channel("localhost:50360")
            stub = manual_pb2_grpc.ManualStationServiceStub(channel)
            
            description = (
                f"REFILL TIPRACK RICHIESTO\n\n"
                f"Tipo: {rack_type}\n"
                f"Posizione: {location}\n"
                f"Tips necessari: {needed}\n"
                f"Tips disponibili: {available}\n\n"
                f"Sostituire il tiprack con uno pieno e confermare."
            )
            
            request = manual_pb2.RequestOperatorTaskRequest(
                task_type="tip_refill",
                task_description=description,
                source_instrument="Opentrons",
                priority="high",
                timeout_seconds=3600
            )
            
            # Spawn popup
            workspace_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            popup_script = os.path.join(workspace_root, "operator_popup.py")
            
            task_id = None
            popup_opened = False
            
            async for response in stub.RequestOperatorTask(request):
                task_id = response.task_id
                
                # Open popup on first response
                if not popup_opened and response.status in ["pending", "created"]:
                    popup_opened = True
                    safe_description = description.replace('"', '\\"').replace('\n', ' ')
                    subprocess.Popen(
                        f'start "Refill Tips - {location}" python "{popup_script}" "{task_id}" "tip_refill" "{safe_description}" "Opentrons" "high"',
                        shell=True,
                        cwd=workspace_root
                    )
                    self.logger.info(f"Opened refill popup for task {task_id}")
                
                if response.status == "completed":
                    self.logger.info(f"Operator confirmed tip refill for {rack_type}")
                    await channel.close()
                    return True
                elif response.status == "cancelled":
                    self.logger.warning(f"Operator cancelled tip refill for {rack_type}")
                    await channel.close()
                    return False
                elif response.status == "timeout":
                    self.logger.warning(f"Tip refill request timed out for {rack_type}")
                    await channel.close()
                    return False
                    
            await channel.close()
            return False
            
        except Exception as e:
            self.logger.error(f"Failed to request tip refill: {e}")
            return False
        
    # ═══════════════════════════════════════════════════════════════════
    #                    OPENTRONS FLEX FEATURE
    # ═══════════════════════════════════════════════════════════════════
    
    async def cmd_initialize(self) -> str:
        """Initialize connection to robot."""
        if not self.robot.is_connected:
            connected = await self.robot.connect_with_retry(
                max_retries=self.config.connection_retry_count,
                retry_delay=self.config.connection_retry_delay
            )
            if not connected:
                return "Connection failed after retries"
            
        health = await self.robot.get_health()
        if "error" in health:
            return f"Connection failed: {health.get('error')}"
            
        self._status = "idle"
        return f"Connected to {health.get('name', 'Opentrons Flex')}"
        
    async def cmd_home(self) -> str:
        """Home all robot axes."""
        await self.robot.home()
        return "Homing complete"
        
    async def cmd_emergency_stop(self) -> str:
        """Emergency stop - abort current run, drop tips, home."""
        if self._current_run_id:
            await self.robot.stop_run(self._current_run_id)
            # Wait for stop to complete
            for _ in range(10):
                status = await self.robot.get_run_status(self._current_run_id)
                if "stopped" in status or "failed" in status or status == "succeeded":
                    break
                await asyncio.sleep(0.5)
            
        # Run emergency drop sequence
        await self._run_emergency_drop()
            
        self._status = "idle"
        self._current_run_id = None
        self._is_running = False
        return "Emergency stop executed"
        
    async def _run_emergency_drop(self):
        """Execute the emergency drop and home sequence."""
        self.logger.info("Executing emergency drop sequence...")
        
        drop_recipe = {
            "ProtocolName": "EMERGENCY_DROP",
            "Labware": {},
            "Trash": {"Bin": {"Type": "TrashBin", "Slot": "A3"}},
            "Pipettes": {"left": "flex_1channel_1000"},
            "Steps": [
                {"Command": "DropTip", "PipetteMount": "left", "TrashLocation": "Bin", "Force": True},
                {"Command": "Home"}
            ]
        }
        
        try:
            code = self.generator.generate_content(json.dumps(drop_recipe))
            protocol_id = await self.robot.upload_protocol_content(code)
            run_id = await self.robot.create_run(protocol_id)
            await self.robot.play_run(run_id)
            
            # Wait for completion (max 60 seconds)
            for _ in range(60):
                status = await self.robot.get_run_status(run_id)
                if status in ("succeeded", "failed", "stopped"):
                    break
                await asyncio.sleep(1)
                
            await self.robot.dismiss_run(run_id)
            self.logger.info("Emergency drop completed")
            
        except Exception as e:
            self.logger.error(f"Emergency drop failed: {e}")
        
    async def cmd_run_protocol(
        self, 
        content: str, 
        protocol_type: str = "json",
        on_progress: Optional[Callable] = None
    ) -> str:
        """
        Execute a protocol.
        
        Args:
            content: Protocol content (JSON or Python)
            protocol_type: "json" or "python"
            on_progress: Optional progress callback
            
        Returns:
            Result string with run ID and status
        """
        async with self._lock:
            if self._is_running:
                raise RuntimeError("Robot is busy")
                
            self._is_running = True
            self._status = "running"
            planned_tips = {}
            
            try:
                if protocol_type == "json":
                    recipe = json.loads(content)
                    
                    # Handle Refill command
                    if recipe.get("Command") == "Refill":
                        rack_type = recipe.get("RackType", "")
                        if rack_type and self.tip_tracker:
                            self.tip_tracker.reset(rack_type)
                            self._is_running = False
                            self._status = "idle"
                            return f"Refilled: {rack_type}"
                    
                    # Apply hardware config (HAL)
                    if self.hardware:
                        self.logger.info("Applying Hardware Config (HAL)...")
                        recipe = self.hardware.apply_hardware_to_recipe(recipe)
                        
                    # Validate
                    is_valid, error = self.validate_json_recipe(recipe)
                    if not is_valid:
                        raise ValueError(f"Validation error: {error}")
                        
                    # Calculate tip usage
                    if self.tip_tracker:
                        planned_tips = calculate_tips_from_recipe(recipe)
                        
                        # Check availability and request refill if needed
                        for rack_type, needed in planned_tips.items():
                            used = self.tip_tracker.get_usage(rack_type)
                            capacity = self._count_racks(recipe, rack_type) * 96
                            if capacity == 0 and needed > 0:
                                capacity = 96  # Fallback
                            
                            available = capacity - used
                            self.logger.info(f"[PLAN] {rack_type}: need {needed}, available {available}")
                            
                            if used + needed > capacity:
                                # Find tiprack location - check both Labware and Requirements.Labware
                                location = "deck"
                                
                                # Check Labware (post-HAL)
                                if "Labware" in recipe:
                                    for lw_key, lw_val in recipe["Labware"].items():
                                        if isinstance(lw_val, dict):
                                            load_name = lw_val.get("LoadName", lw_val.get("type", ""))
                                            if load_name == rack_type:
                                                location = lw_val.get("Location", lw_val.get("location", "deck"))
                                                break
                                
                                # Check Requirements.Labware (pre-HAL)
                                if location == "deck" and "Requirements" in recipe:
                                    req = recipe.get("Requirements", {})
                                    if isinstance(req, dict) and "Labware" in req:
                                        for lw_key, lw_val in req["Labware"].items():
                                            if isinstance(lw_val, dict):
                                                load_name = lw_val.get("type", lw_val.get("LoadName", ""))
                                                if load_name == rack_type:
                                                    location = lw_val.get("location", lw_val.get("Location", "deck"))
                                                    break
                                
                                self.logger.warning(f"Insufficient tips: {rack_type} - requesting refill...")
                                
                                # Request operator refill via ManualStation
                                refill_confirmed = await self._request_tip_refill(
                                    rack_type=rack_type,
                                    location=location,
                                    needed=needed,
                                    available=available
                                )
                                
                                if refill_confirmed:
                                    # Reset tip tracker - rack has been refilled
                                    self.tip_tracker.reset(rack_type)
                                    self.logger.info(f"Tip tracker reset for {rack_type} after refill")
                                else:
                                    raise RuntimeError(
                                        f"Insufficient tips: {rack_type} (used: {used}, need: {needed}) - refill cancelled"
                                    )
                                
                        # Inject tip usage map for starting_tip
                        recipe["TipUsageMap"] = {
                            k: self.tip_tracker.get_usage(k) 
                            for k in planned_tips.keys()
                        }
                    
                    # Generate protocol
                    protocol_code = self.generator.generate_content(json.dumps(recipe))
                    
                else:
                    protocol_code = content
                    
                # Execute
                run_id = await self._execute_protocol(protocol_code)
                
                # Wait for completion
                final_status = await self.robot.wait_for_run_completion(
                    run_id,
                    on_status_change=on_progress
                )
                
                # Smart tip tracking update
                await self._update_tracker_smart(run_id, planned_tips, final_status)
                
                # Extract images if succeeded
                if final_status == "succeeded":
                    await self.robot.extract_images_from_log(run_id, self.config.dir_images)
                        
                await self.robot.dismiss_run(run_id)
                
                self._current_run_id = None
                self._is_running = False
                self._status = "idle"
                
                return f"Run {run_id}: {final_status}"
                
            except Exception as e:
                self._is_running = False
                self._status = "error"
                if self.config.auto_home_on_error:
                    try:
                        await self.robot.home()
                    except Exception:
                        pass
                raise
            
    def _count_racks(self, recipe: Dict, rack_type: str) -> int:
        """Count number of tip racks of a given type."""
        count = 0
        
        # Check Labware (post-HAL format)
        if "Labware" in recipe:
            for value in recipe["Labware"].values():
                if isinstance(value, dict):
                    load_name = value.get("LoadName", value.get("type", ""))
                    if load_name == rack_type:
                        count += 1
                        
        # Check Requirements.Labware (pre-HAL format)
        if count == 0 and "Requirements" in recipe:
            req = recipe.get("Requirements", {})
            if isinstance(req, dict) and "Labware" in req:
                for value in req["Labware"].values():
                    if isinstance(value, dict):
                        load_name = value.get("type", value.get("LoadName", ""))
                        if load_name == rack_type:
                            count += 1
                            
        return count
            
    async def _update_tracker_smart(
        self, 
        run_id: str, 
        planned: Dict[str, int], 
        status: str
    ):
        """Smart tip tracker update - uses actual data for partial runs."""
        if not self.tip_tracker:
            return
            
        self.logger.info(f"Updating tip tracker (status: {status})...")
        
        if status == "succeeded":
            # Success - apply planned consumption
            if planned:
                for rack_type, count in planned.items():
                    self.tip_tracker.add_usage(rack_type, count)
                    self.logger.info(f"  + {count} tips ({rack_type})")
            return
            
        # Partial/failed run - try to read actual usage from log
        self.logger.warning("Run incomplete - reading actual usage from log...")
        
        try:
            actual_usage = await self.robot.get_actual_tip_usage(run_id)
            
            if actual_usage:
                for rack_type, count in actual_usage.items():
                    self.tip_tracker.add_usage(rack_type, count)
                    self.logger.info(f"  + {count} tips ({rack_type}) [FROM LOG]")
                return
                
        except Exception as e:
            self.logger.warning(f"Log analysis failed: {e}")
            
        # Fallback - apply full planned usage for safety
        if planned:
            self.logger.warning("Applying full planned usage for safety")
            for rack_type, count in planned.items():
                self.tip_tracker.add_usage(rack_type, count)
                self.logger.info(f"  + {count} tips ({rack_type}) [SAFETY]")
            
    async def _execute_protocol(self, code: str) -> str:
        """Upload and execute a protocol."""
        protocol_id = await self.robot.upload_protocol_content(code)
        run_id = await self.robot.create_run(protocol_id)
        self._current_run_id = run_id
        await self.robot.play_run(run_id)
        return run_id
        
    async def cmd_abort_run(self, run_id: str = "") -> str:
        """Abort a running protocol."""
        target_id = run_id or self._current_run_id
        if target_id:
            await self.robot.stop_run(target_id)
            if self.config.auto_drop_tip_on_abort:
                await self._run_emergency_drop()
            return f"Run {target_id} aborted"
        return "No run to abort"
        
    async def cmd_pause_run(self) -> str:
        """Pause current run."""
        if self._current_run_id:
            await self.robot.pause_run(self._current_run_id)
            self._status = "paused"
            return "Run paused"
        return "No run to pause"
        
    async def cmd_resume_run(self) -> str:
        """Resume paused run."""
        if self._current_run_id:
            await self.robot.play_run(self._current_run_id)
            self._status = "running"
            return "Run resumed"
        return "No run to resume"
    
    async def cmd_set_lights(self, on: bool) -> str:
        """Set robot deck lights on or off."""
        if self.robot:
            await self.robot.set_lights(on)
            return f"Lights {'on' if on else 'off'}"
        return "Not connected to robot"
    
    async def cmd_get_modules_status(self) -> list:
        """Get status of all attached modules."""
        if self.robot:
            modules = await self.robot.get_modules()
            return modules if modules else []
        return []
    
    def reset_tip_tracking(self) -> str:
        """Reset all tip tracking counters to full."""
        if self.tip_tracker:
            self.tip_tracker.reset_all()
            return "Tip tracking reset to full"
        return "Tip tracker not initialized"
        
    def get_robot_status(self) -> str:
        """Get current robot status."""
        return self._status
        
    def get_current_run_id(self) -> str:
        """Get current run ID."""
        return self._current_run_id or ""
        
    def is_connected(self) -> bool:
        """Check if connected to robot."""
        return self.robot.is_connected if self.robot else False
        
    # ═══════════════════════════════════════════════════════════════════
    #                    LIQUID HANDLING FEATURE
    # ═══════════════════════════════════════════════════════════════════
    
    async def cmd_transfer(
        self,
        volume: float,
        source: str,
        destination: str,
        pipette_mount: str = "left",
        new_tip: str = "once"
    ) -> str:
        """Execute a transfer operation."""
        recipe = self._build_base_recipe("Transfer")
        recipe["Steps"].append({
            "Command": "Transfer",
            "Volume": volume,
            "Source": source,
            "Dest": destination,
            "PipetteMount": pipette_mount,
            "NewTip": new_tip
        })
        return await self.cmd_run_protocol(json.dumps(recipe), "json")
        
    async def cmd_distribute(
        self,
        volume: float,
        source: str,
        destinations: str,
        pipette_mount: str = "left"
    ) -> str:
        """Execute a distribute operation."""
        dest_list = [d.strip() for d in destinations.split(",")]
        recipe = self._build_base_recipe("Distribute")
        recipe["Steps"].append({
            "Command": "Distribute",
            "Volume": volume,
            "Source": source,
            "Destinations": dest_list,
            "PipetteMount": pipette_mount
        })
        return await self.cmd_run_protocol(json.dumps(recipe), "json")
        
    async def cmd_consolidate(
        self,
        volume: float,
        sources: str,
        destination: str,
        pipette_mount: str = "left"
    ) -> str:
        """Execute a consolidate operation."""
        source_list = [s.strip() for s in sources.split(",")]
        recipe = self._build_base_recipe("Consolidate")
        recipe["Steps"].append({
            "Command": "Consolidate",
            "Volume": volume,
            "Sources": source_list,
            "Dest": destination,
            "PipetteMount": pipette_mount
        })
        return await self.cmd_run_protocol(json.dumps(recipe), "json")
        
    async def cmd_mix(
        self,
        volume: float,
        location: str,
        repetitions: int,
        pipette_mount: str = "left"
    ) -> str:
        """Execute a mix operation."""
        recipe = self._build_base_recipe("Mix")
        recipe["Steps"].append({
            "Command": "Transfer",
            "Volume": volume,
            "Source": location,
            "Dest": location,
            "PipetteMount": pipette_mount,
            "MixBefore": {"Volume": volume, "Repetitions": repetitions}
        })
        return await self.cmd_run_protocol(json.dumps(recipe), "json")
        
    async def cmd_pick_up_tip(self, pipette_mount: str = "left") -> str:
        """Pick up a tip."""
        recipe = self._build_base_recipe("PickUpTip")
        recipe["Steps"].append({
            "Command": "PickUpTip",
            "PipetteMount": pipette_mount
        })
        return await self.cmd_run_protocol(json.dumps(recipe), "json")
        
    async def cmd_drop_tip(self, pipette_mount: str = "left", force: bool = False) -> str:
        """Drop the current tip."""
        recipe = self._build_base_recipe("DropTip")
        recipe["Steps"].append({
            "Command": "DropTip",
            "PipetteMount": pipette_mount,
            "Force": force
        })
        return await self.cmd_run_protocol(json.dumps(recipe), "json")
        
    async def cmd_refill_tip_rack(self, rack_type: str) -> str:
        """
        Mark a tip rack as refilled.
        
        Args:
            rack_type: Either logical name (e.g., "tiprack_1000ul") or 
                      load name (e.g., "opentrons_flex_96_filtertiprack_1000ul")
                      or "all" to refill all racks
        """
        if self.tip_tracker:
            if rack_type.lower() == "all":
                # Reset all tracked tip racks
                self.tip_tracker.reset_all()
                return "All tip racks refilled"
            
            # Try to resolve logical name to load name
            actual_rack_type = rack_type
            if self.hardware:
                hal_tips = self.hardware.get_configured_tip_racks()
                # Check if rack_type is a logical name
                if rack_type in hal_tips:
                    actual_rack_type = hal_tips[rack_type]
                    self.logger.info(f"Resolved logical name '{rack_type}' to load name '{actual_rack_type}'")
                # Also check reverse (if they passed a load_name that matches a HAL tip)
                elif rack_type not in hal_tips.values():
                    # Not found in HAL - try partial matching
                    for logical, load in hal_tips.items():
                        if rack_type.lower() in load.lower() or load.lower() in rack_type.lower():
                            actual_rack_type = load
                            self.logger.info(f"Partial match: '{rack_type}' -> '{actual_rack_type}'")
                            break
            
            self.tip_tracker.reset(actual_rack_type)
            return f"Tip rack {actual_rack_type} refilled"
        return "Tip tracking disabled"
        
    def get_tip_status(self) -> str:
        """Get tip usage status as JSON."""
        if self.tip_tracker:
            return self.tip_tracker.to_json()
        return "{}"
        
    def get_loaded_labware(self) -> str:
        """Get loaded labware from hardware config."""
        if self.hardware:
            return json.dumps(self.hardware.get_labware(), indent=2)
        return "{}"
    
    # ═══════════════════════════════════════════════════════════════════
    #                    SIMPLIFIED COMMANDS
    # ═══════════════════════════════════════════════════════════════════
    
    def list_recipes(self) -> List[str]:
        """List available recipe files."""
        recipes_dir = os.path.abspath(self.config.dir_recipes if hasattr(self.config, 'dir_recipes') else 
                                       os.path.join(os.path.dirname(__file__), "../../../Library/Recipes"))
        if not os.path.exists(recipes_dir):
            recipes_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../Library/Recipes"))
        if not os.path.exists(recipes_dir):
            self.logger.warning(f"Recipes directory not found: {recipes_dir}")
            return []
        
        recipes = []
        for f in os.listdir(recipes_dir):
            if f.endswith('.json') and not f.startswith('.'):
                recipes.append(f)
        return sorted(recipes)
    
    async def cmd_execute_recipe_by_name(self, recipe_name: str, on_progress=None) -> tuple:
        """
        Execute a recipe by filename.
        
        Args:
            recipe_name: Name of the recipe file (e.g., "modifiedtest1.json" or "modifiedtest1")
            on_progress: Optional callback for progress updates
            
        Returns:
            Tuple of (run_id, final_status)
        """
        # Find the recipe file
        recipes_dir = os.path.abspath(self.config.dir_recipes if hasattr(self.config, 'dir_recipes') else 
                                       os.path.join(os.path.dirname(__file__), "../../../Library/Recipes"))
        if not os.path.exists(recipes_dir):
            recipes_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../Library/Recipes"))
        
        # Add .json extension if missing
        if not recipe_name.endswith('.json'):
            recipe_name = recipe_name + '.json'
        
        recipe_path = os.path.join(recipes_dir, recipe_name)
        if not os.path.exists(recipe_path):
            raise FileNotFoundError(f"Recipe not found: {recipe_name}")
        
        # Load and execute
        # utf-8-sig transparently handles UTF-8 BOM produced by some editors/tools.
        with open(recipe_path, 'r', encoding='utf-8-sig') as f:
            content = f.read()
        
        result = await self.cmd_run_protocol(content, "json", on_progress)
        
        # Parse run_id from result string (format: "Run {run_id}: {status}")
        # _current_run_id is None at this point because cmd_run_protocol clears it
        run_id = ""
        if result and result.startswith("Run "):
            parts = result.split(":")
            if parts:
                run_id = parts[0].replace("Run ", "").strip()
        
        return run_id, result
    
    def list_tip_racks(self) -> str:
        """List tip racks from hardware config with their current usage status."""
        racks_info = []
        
        # Get tip racks from HAL config (if available)
        if self.hardware:
            hal_tips = self.hardware.get_configured_tip_racks()
            for logical_name, load_name in hal_tips.items():
                used = self.tip_tracker.get_usage(load_name) if self.tip_tracker else 0
                racks_info.append({
                    "id": logical_name,
                    "type": load_name,
                    "capacity": 96,
                    "used": used,
                    "available": 96 - used
                })
        
        # If no HAL config or no tip racks, use defaults
        if not racks_info:
            default_racks = [
                "No HAL Config"
            ]
            for rack_type in default_racks:
                used = self.tip_tracker.get_usage(rack_type) if self.tip_tracker else 0
                racks_info.append({
                    "id": rack_type,
                    "type": rack_type,
                    "capacity": 96,
                    "used": used,
                    "available": 96 - used
                })
        
        # Add "all" option at the end to refill all racks
        racks_info.append({
            "id": "all",
            "type": "all",
            "capacity": 0,
            "used": 0,
            "available": 0,
            "description": "Refill ALL tip racks"
        })
        
        return json.dumps({"enabled": bool(self.tip_tracker), "racks": racks_info}, indent=2)
    
    async def cmd_validate_recipe_hal(self, recipe_name: str) -> str:
        """
        Validate a recipe's requirements against the loaded HAL config.
        
        Args:
            recipe_name: Name of the recipe file (e.g., "modifiedtest1.json" or "modifiedtest1")
            
        Returns:
            JSON string with validation result
        """
        # Find and load the recipe
        recipes_dir = os.path.abspath(self.config.dir_recipes if hasattr(self.config, 'dir_recipes') else 
                                       os.path.join(os.path.dirname(__file__), "../../../Library/Recipes"))
        if not os.path.exists(recipes_dir):
            recipes_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../Library/Recipes"))
        
        # Add .json extension if missing
        if not recipe_name.endswith('.json'):
            recipe_name = recipe_name + '.json'
        
        recipe_path = os.path.join(recipes_dir, recipe_name)
        if not os.path.exists(recipe_path):
            return json.dumps({"valid": False, "errors": [f"Recipe not found: {recipe_name}"]})
        
        try:
            with open(recipe_path, 'r', encoding='utf-8-sig') as f:
                recipe = json.load(f)
        except json.JSONDecodeError as e:
            return json.dumps({"valid": False, "errors": [f"Invalid JSON: {str(e)}"]})
        
        # Validate against HAL
        result = self.validate_recipe_against_hal(recipe)
        return json.dumps(result, indent=2)
    
    async def cmd_home_and_reset(self, drop_tip: bool = True) -> str:
        """
        Home robot with optional tip drop.
        
        Args:
            drop_tip: If True, force drop any tip before homing
            
        Returns:
            Result message
        """
        if drop_tip:
            # Run emergency drop first
            try:
                await self._run_emergency_drop()
            except Exception as e:
                self.logger.warning(f"Drop tip failed (may not have tip): {e}")
        
        # Then home
        await self.robot.home()
        self._status = "idle"
        return "Home and reset complete"
    
    def get_status_info(self) -> str:
        """Get complete status information as JSON."""
        status = {
            "connected": self.robot.is_connected if self.robot else False,
            "status": self._status,
            "current_run_id": self._current_run_id or "",
            "hardware_config": self.get_current_hardware_config(),
            "tip_tracking": json.loads(self.get_tip_status()) if self.tip_tracker else None,
        }
        return json.dumps(status, indent=2)
        
    # ═══════════════════════════════════════════════════════════════════
    #                    MODULE CONTROL FEATURE
    # ═══════════════════════════════════════════════════════════════════
    
    async def cmd_heater_shaker_set_temp(
        self,
        module_id: str,
        temperature: float,
        wait_for_temp: bool = False
    ) -> str:
        """Set Heater-Shaker temperature."""
        recipe = self._build_base_recipe("HSTemp")
        recipe["Steps"].append({
            "Command": "HeaterShaker",
            "ModuleID": module_id,
            "Temperature": temperature,
            "WaitForTemp": wait_for_temp
        })
        return await self.cmd_run_protocol(json.dumps(recipe), "json")
        
    async def cmd_heater_shaker_shake(
        self,
        module_id: str,
        rpm: int,
        duration: int = 0
    ) -> str:
        """Start Heater-Shaker shaking."""
        recipe = self._build_base_recipe("HSShake")
        recipe["Steps"].append({
            "Command": "HeaterShaker",
            "ModuleID": module_id,
            "CloseLatch": True,
            "RPM": rpm,
            "Duration": duration if duration > 0 else None,
            "OpenLatch": duration > 0
        })
        return await self.cmd_run_protocol(json.dumps(recipe), "json")
        
    async def cmd_heater_shaker_open_latch(self, module_id: str) -> str:
        """Open Heater-Shaker latch."""
        recipe = self._build_base_recipe("HSOpen")
        recipe["Steps"].append({
            "Command": "HeaterShaker",
            "ModuleID": module_id,
            "OpenLatch": True
        })
        return await self.cmd_run_protocol(json.dumps(recipe), "json")
        
    async def cmd_heater_shaker_close_latch(self, module_id: str) -> str:
        """Close Heater-Shaker latch."""
        recipe = self._build_base_recipe("HSClose")
        recipe["Steps"].append({
            "Command": "HeaterShaker",
            "ModuleID": module_id,
            "CloseLatch": True
        })
        return await self.cmd_run_protocol(json.dumps(recipe), "json")
        
    async def cmd_heater_shaker_deactivate(
        self,
        module_id: str,
        deactivate_heater: bool = True,
        open_latch: bool = True
    ) -> str:
        """Deactivate Heater-Shaker."""
        recipe = self._build_base_recipe("HSDeactivate")
        recipe["Steps"].append({
            "Command": "HeaterShaker",
            "ModuleID": module_id,
            "DeactivateHeater": deactivate_heater,
            "OpenLatch": open_latch
        })
        return await self.cmd_run_protocol(json.dumps(recipe), "json")
        
    async def cmd_move_labware(
        self,
        labware_id: str,
        new_location: str,
        use_gripper: bool = True
    ) -> str:
        """Move labware using gripper."""
        recipe = self._build_base_recipe("MoveLabware")
        recipe["Steps"].append({
            "Command": "MoveLabware",
            "LabwareID": labware_id,
            "NewLocation": new_location,
            "UseGripper": use_gripper
        })
        return await self.cmd_run_protocol(json.dumps(recipe), "json")
        
    async def get_modules_status(self) -> str:
        """Get status of attached modules."""
        modules = await self.robot.get_modules()
        return json.dumps(modules, indent=2)
        
    def get_loaded_modules(self) -> str:
        """Get loaded modules from hardware config."""
        if self.hardware:
            return json.dumps(self.hardware.get_modules(), indent=2)
        return "{}"
        
    # ═══════════════════════════════════════════════════════════════════
    #                    FILE-BASED PROCESSING
    # ═══════════════════════════════════════════════════════════════════
    
    async def process_input_file(self, file_path: str) -> str:
        """
        Process a protocol file from the input queue.
        
        Args:
            file_path: Path to protocol file
            
        Returns:
            Result string
        """
        filename = os.path.basename(file_path)
        self.logger.info(f"Processing: {filename}")
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                
            # Determine type
            if file_path.endswith('.json'):
                result = await self.cmd_run_protocol(content, "json")
            elif file_path.endswith('.py'):
                result = await self.cmd_run_protocol(content, "python")
            else:
                raise ValueError(f"Unsupported file type: {file_path}")
                
            # Move to processed
            self._move_file(file_path, self.config.dir_processed)
            return result
            
        except Exception as e:
            self.logger.error(f"Processing failed: {e}")
            self._move_file(file_path, self.config.dir_errors)
            raise
            
    def _move_file(self, src: str, dest_dir: str):
        """Move a file to destination directory."""
        try:
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(src))
            if os.path.exists(dest):
                os.remove(dest)
            shutil.move(src, dest)
        except Exception as e:
            self.logger.warning(f"Failed to move file: {e}")
            
    async def scan_input_queue(self) -> List[str]:
        """Scan input directory for protocol files."""
        files = []
        for ext in ['*.json', '*.py']:
            files.extend(glob.glob(os.path.join(self.config.dir_input, ext)))
        return sorted(files)
        
    # ═══════════════════════════════════════════════════════════════════
    #                         HELPERS
    # ═══════════════════════════════════════════════════════════════════
    
    def _build_base_recipe(self, name: str) -> Dict[str, Any]:
        """Build a base recipe structure."""
        return {
            "ProtocolName": name,
            "Labware": {},
            "Trash": {"Bin": {"Type": "TrashBin", "Slot": "A3"}},
            "Steps": []
        }


# ═══════════════════════════════════════════════════════════════════════════
#                              MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    """Main entry point."""
    print()
    print("=" * 60)
    print("  OPENTRONS SiLA2 SERVER")
    print("  Python Implementation")
    print("=" * 60)
    print()
    
    # Load config
    config = ServerConfig("config.yaml")
    
    # Validate
    is_valid, error = config.validate()
    if not is_valid:
        print(f"Configuration error: {error}")
        return
    
    # Create and start server
    server = OpentronsSiLA2Server(config)
    
    try:
        await server.start()
        
    except KeyboardInterrupt:
        print("\nShutdown requested...")
    except Exception as e:
        print(f"Server error: {e}")
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
