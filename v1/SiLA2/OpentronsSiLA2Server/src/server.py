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
import sys
import time
from typing import Any, Callable, Dict, List, Optional

import httpx

from .config import ServerConfig, setup_logging

# Add parent directory for SiLA2 common modules
_sila2_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _sila2_path not in sys.path:
    sys.path.insert(0, _sila2_path)

from .robot_client import RobotClient
from .protocol_generator import ProtocolGenerator
from .hardware_manager import HardwareManager
from .tip_tracker import TipTracker, MountedTipTracker, calculate_tips_from_recipe, reconstruct_tip_mount_state
from .run_archive import RunArchive

logger = logging.getLogger(__name__)


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
        self.mounted_tip_tracker: Optional[MountedTipTracker] = None
        self.run_archive: Optional[RunArchive] = None

        # State
        self._current_run_id: Optional[str] = None
        self._is_running = False
        self._status = "disconnected"
        self._lock = asyncio.Lock()
        # Serializes pause/resume/abort/emergency-stop against each other so
        # conflicting robot actions (e.g. pause racing abort) can't be sent
        # concurrently. Deliberately separate from self._lock, which is held
        # for an entire run's duration — these control commands must be able
        # to interrupt an in-progress run without waiting for it to finish.
        self._control_lock = asyncio.Lock()

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

        # Initialize per-mount tip-presence tracker (used by the leftover-tip
        # pre-flight guard; independent of tip_tracking_enabled, which is
        # about consumption counts, not mount state)
        if self.config.check_tip_before_run:
            self.mounted_tip_tracker = MountedTipTracker(self.config.tip_mount_state_file)

        # Initialize run archive (Fase 6/7): per-run command log + summary,
        # with rotation - resolved the same way as dir_recipes (relative to
        # cwd, matching how this server is actually started).
        if self.config.run_archive_enabled:
            archive_dir = os.path.abspath(self.config.dir_run_archive)
            self.run_archive = RunArchive(
                archive_dir,
                retention_uncompressed_days=self.config.run_archive_retention_uncompressed_days,
                retention_compressed_days=self.config.run_archive_retention_compressed_days,
            )
            self.logger.info(f"Run archive: {archive_dir}")

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
    #
    # This class is the Opentrons *domain* layer: it owns the robot client,
    # the HAL, tip tracking and the recipe pipeline. It is NOT a server.
    # main.py builds the SiLA2 server (sila2 library) and WorkflowAPIImpl
    # drives this object. A second, hand-rolled grpc.aio server used to live
    # here — with its own mDNS registration and a SiLA2Common adapter — that
    # main.py never called. Removed; there is one server, and the sila2
    # library owns it.

    async def stop(self):
        """Release hardware resources. Called by WorkflowAPIImpl.stop()."""
        self.logger.info("Shutting down Opentrons domain layer...")
        if self.robot:
            await self.robot.disconnect()
        self.logger.info("Stopped")

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
        
        # No fallback: the webapp notification above is the only operator
        # channel. A ManualStation gRPC fallback used to live here, guarded by
        # a MANUAL_STATION_AVAILABLE flag that was always False (its stubs
        # exist nowhere in the tree) and spawning an operator_popup.py that
        # does not exist. It could never run.
        self.logger.error(
            "Tip refill could not be confirmed: the webapp at %s did not "
            "respond. Refill the rack and re-run, or confirm from the "
            "Operator page once the webapp is back.", webapp_url,
        )
        return False

    # ═══════════════════════════════════════════════════════════════════
    #                    OPENTRONS FLEX FEATURE
    # ═══════════════════════════════════════════════════════════════════
    
    async def cmd_initialize(self) -> str:
        """Initialize connection to robot."""
        if self.robot is None:
            raise RuntimeError("Robot client not initialized — call initialize() first")
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
        if self.robot is None:
            raise RuntimeError("Robot client not initialized — call initialize() first")
        await self.robot.home()
        return "Homing complete"

    async def cmd_emergency_stop(self) -> str:
        """Emergency stop - abort current run, drop tips, home."""
        if self.robot is None:
            raise RuntimeError("Robot client not initialized — call initialize() first")
        async with self._control_lock:
            if self._current_run_id:
                await self.robot.stop_run(self._current_run_id)
                # Wait for stop to complete
                for _ in range(10):
                    status = await self.robot.get_run_status(self._current_run_id)
                    if "stopped" in status or "failed" in status or status == "succeeded":
                        break
                    await asyncio.sleep(0.5)

            # Run emergency drop sequence
            drop_ok, drop_error = await self._run_emergency_drop()

            self._status = "idle"
            self._current_run_id = None
            self._is_running = False
            if drop_ok:
                return "Emergency stop executed"
            # Never report full success when the drop/home sequence didn't
            # actually complete — tips may still be attached, axes un-homed.
            return f"Emergency stop executed. WARNING: tip drop/home sequence failed ({drop_error}) - verify manually."

    async def _run_emergency_drop(self) -> tuple[bool, Optional[str]]:
        """Execute the emergency drop and home sequence. Returns (success, error)."""
        if self.robot is None:
            raise RuntimeError("Robot client not initialized — call initialize() first")
        if self.generator is None:
            raise RuntimeError("Protocol generator not initialized — call initialize() first")
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
            if self.mounted_tip_tracker:
                self.mounted_tip_tracker.mark_unconfirmed(list(drop_recipe["Pipettes"].keys()))

            code = self.generator.generate_content(json.dumps(drop_recipe))
            protocol_id = await self.robot.upload_protocol_content(code)
            run_id = await self.robot.create_run(protocol_id)
            await self.robot.play_run(run_id)

            # Wait for completion (max 60 seconds)
            final_status = None
            for _ in range(60):
                final_status = await self.robot.get_run_status(run_id)
                if final_status in ("succeeded", "failed", "stopped"):
                    break
                await asyncio.sleep(1)

            # This path (EmergencyStop/AbortRun) bypasses cmd_run_protocol
            # entirely, so without this the leftover-tip guard would never
            # learn a drop happened here (confirmed on real hardware: an
            # emergency drop that clearly succeeded was still followed by the
            # next run's guard firing again, because nothing told the tracker).
            await self._reconcile_mounted_tip_state(run_id)

            await self.robot.dismiss_run(run_id)

            if final_status != "succeeded":
                self.logger.warning(f"Emergency drop did not succeed (status: {final_status})")
                return False, f"drop sequence ended with status '{final_status}'"

            self.logger.info("Emergency drop completed")
            return True, None

        except Exception as e:
            self.logger.error(f"Emergency drop failed: {e}")
            return False, str(e)

    def _prepend_leftover_tip_guard(self, recipe: Dict) -> Dict:
        """
        Prepend an unconditional Force DropTip per pipette mount that might
        still have a tip attached, to the recipe's own Steps - so a tip left
        mounted from a previous interrupted run is cleared as part of THIS
        run (one Home, one pipette load, one continuous execution) instead of
        a separate preliminary robot run.

        This robot's installed robot-server does not implement
        `detect_tip_presence()` at all (confirmed on hardware: AttributeError,
        not just an import issue - the tip-presence-sensing feature isn't
        present regardless of the declared apiLevel, and isn't part of the
        public Python Protocol API either), so there's no real *on-robot*
        presence check available (`DropTipIfPresent`, kept in
        protocol_generator.py for when/if the robot software is upgraded,
        isn't usable here). Which mounts actually get a guard step is instead
        decided from `self.mounted_tip_tracker` - our own persisted,
        command-log-reconstructed record of the last known state (see
        MountedTipTracker in tip_tracker.py) - so a confidently-clear mount
        doesn't pay for an unnecessary drop on every single run. Falling back
        to "assume present" whenever that record is missing or uncertain;
        the drop itself is the existing `DropTip(Force=True)` path, already
        safe as a no-op when there's nothing mounted.
        """
        mounts = list(recipe.get("Pipettes", {}).keys())
        if not mounts:
            return recipe

        if self.mounted_tip_tracker:
            guard_mounts = [m for m in mounts if self.mounted_tip_tracker.might_have_tip(m)]
        else:
            guard_mounts = mounts

        self.logger.info(f"[TIP-GUARD] mounts={mounts} guarded={guard_mounts}")

        guard_steps = [
            {"Command": "DropTip", "PipetteMount": mount, "Force": True}
            for mount in guard_mounts
        ]
        recipe["Steps"] = guard_steps + recipe.get("Steps", [])
        return recipe

    async def _reconcile_mounted_tip_state(self, run_id: str) -> None:
        """
        After a run (success, failure, or stop), reconstruct each involved
        mount's real has_tip state from what actually executed - authoritative
        even for a run that failed partway through - and persist it for the
        next run's leftover-tip guard. Any failure here just leaves mounts at
        the pessimistic "assume present" state written by mark_unconfirmed()
        at run start - never silently assumes "clear".
        """
        if not self.mounted_tip_tracker:
            return
        if self.robot is None:
            raise RuntimeError("Robot client not initialized — call initialize() first")
        try:
            run_detail = await self.robot.get_run_detail(run_id)
            pipette_mounts = {
                p.get("id"): p.get("mount")
                for p in ((run_detail or {}).get("data", {}).get("pipettes", []) or [])
                if p.get("id") and p.get("mount")
            }
            data = await self.robot.get_run_commands(run_id)
            commands = data.get("data", []) if data else []
        except Exception as e:
            self.logger.warning(f"Could not read run detail/commands for mounted-tip reconciliation: {e}")
            return
        if not commands or not pipette_mounts:
            self.logger.warning(
                f"[TIP-GUARD] Incomplete data for reconciliation (commands={len(commands)}, "
                f"pipette_mounts={pipette_mounts}) - mounts stay 'assume present'"
            )
            return
        reconstructed = reconstruct_tip_mount_state(commands, pipette_mounts)
        self.logger.info(f"[TIP-GUARD] reconciled from run {run_id} commands: {reconstructed}")
        self.mounted_tip_tracker.mark_from_commands(commands, pipette_mounts)

    def _compute_waste_comparison(self, recipe: Optional[Dict], commands: List[Dict[str, Any]]) -> Optional[Dict]:
        """
        Fase 7 part 2: informational estimate-vs-actual liquid waste for this
        run - no impact on robot behavior, reporting only.

        Aggregate, not per-reagent: the run's command log only carries
        labwareId/wellName, not reagent identity, so reconstructing which
        aspirate/dispense belonged to which reagent post-hoc would need a
        well->reagent map threaded through the recipe just for this purpose.
        Instead: summing every succeeded aspirate volume minus every
        succeeded dispense volume across the whole run gives the TOTAL waste
        actually incurred (anything aspirated but never dispensed into a
        destination well was disposed of/blown out) - correct regardless of
        reagent identity - compared against the sum of the recipe's own
        generation-time per-reagent estimates
        (Metadata.RequiredVolumes[*].estimated_waste_ul, from Fase 7 part 1
        in excel_recipe_parser.py).
        """
        if not recipe:
            return None
        required_volumes = recipe.get("Metadata", {}).get("RequiredVolumes", {})
        if not isinstance(required_volumes, dict):
            return None
        estimated_waste_ul = sum(
            (v.get("estimated_waste_ul") or 0)
            for v in required_volumes.values()
            if isinstance(v, dict)
        )
        if not estimated_waste_ul:
            return None

        total_aspirated = 0.0
        total_dispensed = 0.0
        for cmd in commands:
            if cmd.get("status") != "succeeded":
                continue
            volume = cmd.get("params", {}).get("volume")
            if volume is None:
                continue
            command_type = cmd.get("commandType")
            if command_type == "aspirate":
                total_aspirated += volume
            elif command_type == "dispense":
                total_dispensed += volume

        if total_aspirated <= 0:
            return None

        actual_waste_ul = round(total_aspirated - total_dispensed, 1)
        return {
            "estimated_waste_ul": round(estimated_waste_ul, 1),
            "actual_waste_ul": actual_waste_ul,
            "delta_ul": round(actual_waste_ul - estimated_waste_ul, 1),
            "note": (
                "Aggregate across the whole run (total aspirated minus total "
                "dispensed) - not per-reagent, since the run log only has "
                "labwareId/wellName, not reagent identity."
            ),
        }

    async def _archive_run(
        self,
        run_id: str,
        recipe: Optional[Dict],
        final_status: str,
        planned_tips: Dict[str, int],
        duration_seconds: float,
    ) -> None:
        """
        Fase 6: persist this run's command log + a small summary (status,
        duration, tip usage, estimate-vs-actual waste) to the run archive,
        then apply retention. Best-effort: archiving must never break or mask
        the outcome of a real run, so every failure here is caught and logged,
        never raised.
        """
        if not self.run_archive:
            return
        if self.robot is None:
            raise RuntimeError("Robot client not initialized — call initialize() first")
        try:
            data = await self.robot.get_run_commands(run_id)
            commands = data.get("data", []) if data else []
        except Exception as e:
            self.logger.warning(f"Could not fetch commands for run archive: {e}")
            commands = []

        summary = {
            "run_id": run_id,
            "recipe_name": (recipe or {}).get("ProtocolName", ""),
            "status": final_status,
            "duration_seconds": round(duration_seconds, 1),
            "planned_tip_usage": planned_tips,
            "command_count": len(commands),
        }

        waste_comparison = self._compute_waste_comparison(recipe, commands)
        if waste_comparison:
            summary["waste_comparison"] = waste_comparison

        try:
            path = self.run_archive.save(run_id, summary, commands)
            if path:
                self.run_archive.sweep_retention()
        except Exception as e:
            self.logger.warning(f"Run archive save/sweep failed: {e}")

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
        # Fast-path check before acquiring self._lock: the lock is held for a
        # run's entire (potentially unbounded) duration, so a check done only
        # after acquiring it would never actually see _is_running=True — a
        # concurrent call would just block in queue behind the running protocol
        # instead of failing fast with "Robot is busy".
        if self._is_running:
            raise RuntimeError("Robot is busy")

        if self.robot is None:
            raise RuntimeError("Robot client not initialized — call initialize() first")
        if self.generator is None:
            raise RuntimeError("Protocol generator not initialized — call initialize() first")

        async with self._lock:
            # Re-check inside the lock: belt-and-suspenders against two calls
            # arriving almost simultaneously and both passing the check above.
            if self._is_running:
                raise RuntimeError("Robot is busy")

            self._is_running = True
            self._status = "running"
            planned_tips = {}
            run_id = None
            run_start: Optional[float] = None
            recipe: Optional[Dict] = None
            guarded_mounts: List[str] = []

            try:
                if protocol_type == "json":
                    recipe = json.loads(content)
                    if not isinstance(recipe, dict):
                        raise ValueError("Recipe JSON must be a JSON object (dict) at the top level")

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

                    # Pre-flight: catch a tip left mounted from a previous
                    # interrupted run before it can collide with anything
                    # this run tries to move. Integrated into this same run
                    # (see _prepend_leftover_tip_guard), not a separate one.
                    if self.config.check_tip_before_run:
                        recipe = self._prepend_leftover_tip_guard(recipe)
                        guarded_mounts = list(recipe.get("Pipettes", {}).keys())
                        if self.mounted_tip_tracker:
                            # Pessimistic default until this run's own log
                            # confirms otherwise (see _reconcile_mounted_tip_state).
                            self.mounted_tip_tracker.mark_unconfirmed(guarded_mounts)

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
                run_start = time.time()
                run_id = await self._execute_protocol(protocol_code, recipe=recipe)

                # Wait for completion
                final_status = await self.robot.wait_for_run_completion(
                    run_id,
                    on_status_change=on_progress,
                    timeout=self.config.run_timeout
                )

                # Smart tip tracking update
                await self._update_tracker_smart(run_id, planned_tips, final_status)

                # Reconcile per-mount tip-presence state from what this run
                # actually did, for next run's leftover-tip guard.
                if guarded_mounts:
                    await self._reconcile_mounted_tip_state(run_id)

                # Archive the run log + summary (Fase 6/7) - after
                # reconciliation so it doesn't compete for the command-log
                # fetch, before dismiss_run so the run is still queryable.
                await self._archive_run(run_id, recipe, final_status, planned_tips, time.time() - run_start)

                # Extract images if succeeded
                if final_status == "succeeded":
                    await self.robot.extract_images_from_log(run_id, self.config.dir_images)
                        
                await self.robot.dismiss_run(run_id)
                
                self._current_run_id = None
                self._is_running = False
                self._status = "idle"
                
                return f"Run {run_id}: {final_status}"
                
            except Exception as e:
                self._current_run_id = None
                self._is_running = False
                self._status = "error"
                # Best-effort: a run that raised (e.g. timeout) may still have
                # a readable command log worth reconciling. If this itself
                # fails, guarded_mounts stays at the pessimistic "assume
                # present" state already written by mark_unconfirmed().
                if guarded_mounts and run_id:
                    try:
                        await self._reconcile_mounted_tip_state(run_id)
                    except Exception as track_exc:
                        self.logger.warning(f"Could not reconcile mounted-tip state after error: {track_exc}")
                # Same best-effort spirit for the archive: a failed run's log
                # is often the most useful one to have kept.
                if run_id:
                    try:
                        await self._archive_run(
                            run_id, recipe, "error", planned_tips,
                            time.time() - run_start if run_start else 0.0,
                        )
                    except Exception as archive_exc:
                        self.logger.warning(f"Could not archive run after error: {archive_exc}")
                if self.config.auto_home_on_error:
                    try:
                        await self.robot.home()
                    except Exception as home_exc:
                        self.logger.error(f"Auto-home after error also failed: {home_exc}")
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
        if self.robot is None:
            raise RuntimeError("Robot client not initialized — call initialize() first")

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

            # None means "couldn't determine" (robot_client.get_actual_tip_usage's
            # own analysis failed) - fall through to the planned-usage safety
            # net below. An empty dict is a real, determined answer ("zero
            # tips were actually picked up" - e.g. a run that failed on its
            # very first PickUpTip) and must be recorded as exactly that,
            # not treated the same as "unknown" - that was the bug: a failed
            # run with truly zero tip usage always fell through to charging
            # the FULL planned usage instead, every time.
            if actual_usage is not None:
                for rack_type, count in actual_usage.items():
                    self.tip_tracker.add_usage(rack_type, count)
                    self.logger.info(f"  + {count} tips ({rack_type}) [FROM LOG]")
                if not actual_usage:
                    self.logger.info("  Actual usage from log: 0 tips (run failed before any pickup)")
                return

        except Exception as e:
            self.logger.warning(f"Log analysis failed: {e}")
            
        # Fallback - apply full planned usage for safety
        if planned:
            self.logger.warning("Applying full planned usage for safety")
            for rack_type, count in planned.items():
                self.tip_tracker.add_usage(rack_type, count)
                self.logger.info(f"  + {count} tips ({rack_type}) [SAFETY]")
            
    def _collect_custom_labware_definitions(self, recipe: Optional[dict]) -> List[dict]:
        """
        Find every LoadName in recipe["Labware"] that needs to be pushed to
        the robot before the run starts, i.e. genuinely custom labware (not
        real Opentrons catalog labware the robot already knows). Reads
        Library/Labware/Reservoirs/<LoadName>.json - the only local catalog
        with the full Opentrons v2 schema (real Plates/*.json use a compact
        schema not directly usable as a labware_definitions payload).

        Standard-vs-custom is decided by each file's own "namespace" field:
        real Opentrons definitions declare "namespace": "opentrons" (skip -
        the robot already knows these, and pushing a slightly different copy
        risks a version/geometry mismatch); anything else (e.g. this repo's
        "labos_custom") is genuinely custom and gets pushed. A LoadName with
        no local file at all is silently skipped here - if it's actually
        needed and unknown to the robot, the run fails at loadLabware with a
        clear FileNotFoundError, same as today, rather than this method
        guessing/fabricating a definition.
        """
        if not recipe or "Labware" not in recipe:
            return []
        from pathlib import Path
        # Resolved the same way as dir_recipes/hardware_config_folder
        # elsewhere in this file (os.path.abspath against the process cwd,
        # NOT self.base_dir - that resolves to .../SiLA2, one level short of
        # the actual repo root, and is otherwise unused in this file).
        library_dir = Path(os.path.abspath(self.config.dir_library))
        reservoirs_dir = library_dir / "Labware" / "Reservoirs"
        if not reservoirs_dir.exists():
            return []
        definitions = []
        seen_load_names = set()
        for lw_val in recipe["Labware"].values():
            if not isinstance(lw_val, dict):
                continue
            load_name = lw_val.get("LoadName", lw_val.get("type", ""))
            if not load_name or load_name in seen_load_names:
                continue
            seen_load_names.add(load_name)
            def_path = reservoirs_dir / f"{load_name}.json"
            if not def_path.exists():
                continue
            try:
                data = json.loads(def_path.read_text(encoding="utf-8-sig"))
            except Exception as e:
                self.logger.warning(f"Could not read labware definition {def_path}: {e}")
                continue
            if data.get("namespace") == "opentrons":
                continue  # real Opentrons catalog labware, robot already has it
            definitions.append(data)
        return definitions

    async def _execute_protocol(self, code: str, recipe: Optional[dict] = None) -> str:
        """Upload and execute a protocol.

        `recipe` (optional): the resolved, post-HAL recipe dict, used only to
        find custom labware definitions that need registering on the robot
        before the run starts (see _collect_custom_labware_definitions). Pass
        None for raw/uploaded protocol content with no structured recipe.
        """
        if self.robot is None:
            raise RuntimeError("Robot client not initialized — call initialize() first")
        protocol_id = await self.robot.upload_protocol_content(code)
        run_id = await self.robot.create_run(protocol_id)
        self._current_run_id = run_id
        for definition in self._collect_custom_labware_definitions(recipe):
            await self.robot.add_labware_definition(run_id, definition)
        await self.robot.play_run(run_id)
        return run_id

    async def cmd_abort_run(self, run_id: str = "") -> str:
        """Abort a running protocol."""
        if self.robot is None:
            raise RuntimeError("Robot client not initialized — call initialize() first")
        async with self._control_lock:
            target_id = run_id or self._current_run_id
            if target_id:
                await self.robot.stop_run(target_id)
                if self.config.auto_drop_tip_on_abort:
                    drop_ok, drop_error = await self._run_emergency_drop()
                    if not drop_ok:
                        return f"Run {target_id} aborted. WARNING: tip drop/home sequence failed ({drop_error}) - verify manually."
                return f"Run {target_id} aborted"
            return "No run to abort"

    async def cmd_pause_run(self) -> str:
        """Pause current run."""
        if self.robot is None:
            raise RuntimeError("Robot client not initialized — call initialize() first")
        async with self._control_lock:
            if self._current_run_id:
                await self.robot.pause_run(self._current_run_id)
                self._status = "paused"
                return "Run paused"
            return "No run to pause"

    async def cmd_resume_run(self) -> str:
        """Resume paused run."""
        if self.robot is None:
            raise RuntimeError("Robot client not initialized — call initialize() first")
        async with self._control_lock:
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
        """Check if the HTTP client exists (shallow check — robot may have gone offline)."""
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
            recipes_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../Library/Recipes"))
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
            recipes_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../Library/Recipes"))
        
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
            recipes_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../Library/Recipes"))
        
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
        if self.robot is None:
            raise RuntimeError("Robot client not initialized — call initialize() first")

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
        if self.robot is None:
            raise RuntimeError("Robot client not initialized — call initialize() first")
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
