"""
OpentronsServicer - gRPC Service Implementation (Simplified Workflow API)
=========================================================================

Implements the simplified OpentronsService gRPC interface.
Focuses on workflow-level operations:
- Robot Control (Initialize, Home, EmergencyStop, GetStatus)
- HAL Configuration (Load, List hardware configs)
- Recipe Execution (List, Execute recipes)
- Tip Management (List, Refill tip racks)
- Monitoring (Status, Info, Modules)
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import AsyncIterator, TYPE_CHECKING

import grpc
from google.protobuf import empty_pb2

from . import OpentronsService_pb2 as pb2
from . import OpentronsService_pb2_grpc as pb2_grpc

if TYPE_CHECKING:
    from .server import OpentronsSiLA2Server

logger = logging.getLogger(__name__)


class OpentronsServicer(pb2_grpc.OpentronsServiceServicer):
    """
    gRPC Servicer for Opentrons Flex - Simplified Workflow API.
    
    Provides high-level workflow commands. Low-level liquid handling
    is handled internally by recipes via UNIVERSAL_TEMPLATE.
    """
    
    def __init__(self, server: 'OpentronsSiLA2Server'):
        """Initialize servicer with server reference."""
        self.server = server
        self.logger = logging.getLogger("OpentronsServicer")
    
    # ═══════════════════════════════════════════════════════════════════════
    #                         ROBOT CONTROL
    # ═══════════════════════════════════════════════════════════════════════
    
    async def Initialize(self, request: pb2.InitializeRequest, context) -> pb2.InitializeResponse:
        """Initialize connection to robot."""
        try:
            # Use robot_ip from request if provided
            result = await self.server.cmd_initialize()
            robot_name = ""
            if self.server.robot:
                health = await self.server.robot.get_health()
                robot_name = health.get("name", "") if health else ""
            return pb2.InitializeResponse(
                success=True,
                message=result,
                robot_name=robot_name
            )
        except Exception as e:
            return pb2.InitializeResponse(
                success=False,
                message=str(e),
                robot_name=""
            )
    
    async def Home(self, request: pb2.HomeRequest, context) -> AsyncIterator[pb2.HomeResponse]:
        """Home all robot axes with optional tip drop."""
        try:
            yield pb2.HomeResponse(is_complete=False, status="Homing started...")
            
            # Drop tip first if requested
            if request.drop_tip:
                try:
                    await self.server.cmd_drop_tip("left", force=True)
                except:
                    pass  # Ignore if no tip
            
            result = await self.server.cmd_home()
            yield pb2.HomeResponse(is_complete=True, status=result)
        except Exception as e:
            yield pb2.HomeResponse(is_complete=True, status=f"Error: {e}")
    
    async def EmergencyStop(self, request: empty_pb2.Empty, context) -> pb2.EmergencyStopResponse:
        """Emergency stop."""
        try:
            result = await self.server.cmd_emergency_stop()
            return pb2.EmergencyStopResponse(success=True, message=result)
        except Exception as e:
            return pb2.EmergencyStopResponse(success=False, message=str(e))
    
    async def GetStatus(self, request: empty_pb2.Empty, context) -> pb2.StatusResponse:
        """Get comprehensive robot status."""
        try:
            state = self.server.get_robot_status() or "unknown"
            run_id = self.server.get_current_run_id() or ""
            connected = self.server.is_connected()
            config_name = self.server.get_current_hardware_config() or ""
            
            # Try to get current recipe name (from run metadata if available)
            current_recipe = ""
            progress = 0
            
            return pb2.StatusResponse(
                state=state,
                current_run_id=run_id,
                current_recipe=current_recipe,
                progress=progress,
                is_connected=connected,
                config_loaded=config_name
            )
        except Exception as e:
            logger.error(f"GetStatus error: {e}")
            return pb2.StatusResponse(state="error", is_connected=False)
    
    # ═══════════════════════════════════════════════════════════════════════
    #                         HAL CONFIGURATION
    # ═══════════════════════════════════════════════════════════════════════
    
    async def ListHardwareConfigs(self, request: empty_pb2.Empty, context) -> pb2.ListHardwareConfigsResponse:
        """List available hardware configurations with details."""
        try:
            config_names = self.server.list_hardware_configs()
            configs = []
            
            for name in config_names:
                config_info = pb2.HardwareConfigInfo(
                    filename=name,
                    description="",
                    pipettes=[]
                )
                # Try to load config details
                try:
                    config_path = self.server.hardware_config_dir / name
                    if config_path.exists():
                        with open(config_path, 'r') as f:
                            cfg = json.load(f)
                        config_info.description = cfg.get("description", "")
                        pipettes = []
                        for pip in cfg.get("pipettes", []):
                            pipettes.append(f"{pip.get('mount', 'unknown')}: {pip.get('type', 'unknown')}")
                        config_info.pipettes.extend(pipettes)
                except:
                    pass
                configs.append(config_info)
            
            return pb2.ListHardwareConfigsResponse(configs=configs)
        except Exception as e:
            logger.error(f"ListHardwareConfigs error: {e}")
            return pb2.ListHardwareConfigsResponse(configs=[])
    
    async def LoadHardwareConfig(self, request: pb2.LoadHardwareConfigRequest, context) -> pb2.LoadHardwareConfigResponse:
        """Load a hardware configuration (HAL)."""
        try:
            result = self.server.load_hardware_config(request.config_file)
            success = "loaded" in result.lower() or "success" in result.lower()
            return pb2.LoadHardwareConfigResponse(
                success=success,
                message=result,
                loaded_config=request.config_file if success else ""
            )
        except Exception as e:
            return pb2.LoadHardwareConfigResponse(
                success=False,
                message=str(e),
                loaded_config=""
            )
    
    async def GetCurrentConfig(self, request: empty_pb2.Empty, context) -> pb2.GetCurrentConfigResponse:
        """Get currently loaded hardware configuration."""
        try:
            config_name = self.server.get_current_hardware_config() or ""
            config_json = ""
            if config_name and self.server.config:
                config_json = json.dumps(self.server.config, indent=2)
            return pb2.GetCurrentConfigResponse(
                config_name=config_name,
                config_json=config_json
            )
        except Exception as e:
            return pb2.GetCurrentConfigResponse(config_name="", config_json="")
    
    # ═══════════════════════════════════════════════════════════════════════
    #                         RECIPE EXECUTION
    # ═══════════════════════════════════════════════════════════════════════
    
    async def ListRecipes(self, request: empty_pb2.Empty, context) -> pb2.ListRecipesResponse:
        """List available recipes with metadata."""
        try:
            recipe_dir = self.server.base_dir / "Library" / "Recipes"
            recipes = []
            
            if recipe_dir.exists():
                for f in recipe_dir.glob("*.json"):
                    recipe_info = pb2.RecipeInfo(
                        filename=f.name,
                        name=f.stem,
                        description="",
                        step_count=0
                    )
                    # Try to load recipe details
                    try:
                        with open(f, 'r') as rf:
                            recipe_data = json.load(rf)
                        recipe_info.name = recipe_data.get("name", f.stem)
                        recipe_info.description = recipe_data.get("description", "")
                        recipe_info.step_count = len(recipe_data.get("commands", []))
                    except:
                        pass
                    recipes.append(recipe_info)
            
            return pb2.ListRecipesResponse(recipes=recipes)
        except Exception as e:
            logger.error(f"ListRecipes error: {e}")
            return pb2.ListRecipesResponse(recipes=[])
    
    async def ExecuteRecipe(self, request: pb2.ExecuteRecipeRequest, context) -> AsyncIterator[pb2.ExecuteRecipeResponse]:
        """Execute a recipe from Library/Recipes folder."""
        try:
            recipe_name = request.recipe_name
            
            yield pb2.ExecuteRecipeResponse(
                is_intermediate=True,
                status="loading",
                progress=0,
                message=f"Loading recipe: {recipe_name}",
                current_step=0,
                total_steps=0
            )
            
            # Execute the recipe
            run_id, result = await self.server.cmd_execute_recipe_by_name(recipe_name)
            
            # Stream status updates
            last_status = None
            final_status = "unknown"
            while True:
                status = self.server.get_robot_status()
                
                if status != last_status:
                    last_status = status
                    progress = 50 if status == "running" else 100
                    yield pb2.ExecuteRecipeResponse(
                        is_intermediate=status in ("running", "paused"),
                        run_id=run_id,
                        status=status,
                        progress=progress,
                        message=f"Status: {status}",
                        current_step=0,
                        total_steps=0
                    )
                
                if status in ("succeeded", "failed", "stopped", "idle"):
                    final_status = status
                    break
                
                await asyncio.sleep(0.5)
            
            # Final response
            if final_status in ("failed", "stopped"):
                yield pb2.ExecuteRecipeResponse(
                    is_intermediate=False,
                    run_id=run_id,
                    status="error",
                    progress=100,
                    message=f"Recipe execution failed: {result or final_status}",
                    current_step=0,
                    total_steps=0
                )
            else:
                yield pb2.ExecuteRecipeResponse(
                    is_intermediate=False,
                    run_id=run_id,
                    status="complete",
                    progress=100,
                    message=result,
                    current_step=0,
                    total_steps=0
                )
            
        except FileNotFoundError as e:
            yield pb2.ExecuteRecipeResponse(
                is_intermediate=False,
                status="error",
                progress=0,
                message=f"Recipe not found: {e}"
            )
        except Exception as e:
            logger.error(f"ExecuteRecipe error: {e}")
            yield pb2.ExecuteRecipeResponse(
                is_intermediate=False,
                status="error",
                progress=0,
                message=str(e)
            )
    
    async def PauseRun(self, request: empty_pb2.Empty, context) -> pb2.PauseRunResponse:
        """Pause current run."""
        try:
            result = await self.server.cmd_pause_run()
            return pb2.PauseRunResponse(success=True, message=result)
        except Exception as e:
            return pb2.PauseRunResponse(success=False, message=str(e))
    
    async def ResumeRun(self, request: empty_pb2.Empty, context) -> pb2.ResumeRunResponse:
        """Resume paused run."""
        try:
            result = await self.server.cmd_resume_run()
            return pb2.ResumeRunResponse(success=True, message=result)
        except Exception as e:
            return pb2.ResumeRunResponse(success=False, message=str(e))
    
    async def AbortRun(self, request: empty_pb2.Empty, context) -> pb2.AbortRunResponse:
        """Abort current run."""
        try:
            result = await self.server.cmd_abort_run()
            return pb2.AbortRunResponse(success=True, message=result)
        except Exception as e:
            return pb2.AbortRunResponse(success=False, message=str(e))
    
    # ═══════════════════════════════════════════════════════════════════════
    #                         TIP MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════════
    
    async def ListTipRacks(self, request: empty_pb2.Empty, context) -> pb2.ListTipRacksResponse:
        """List tip racks with usage statistics."""
        try:
            tip_racks = []
            
            # Get tip status from server (returns JSON string)
            tip_status_json = self.server.get_tip_status()
            # tip_data format: {"opentrons_flex_96_tiprack_1000ul": 12, ...} (used count)
            tip_data = json.loads(tip_status_json) if tip_status_json else {}
            
            # Get tip rack info from hardware config
            if self.server.hardware:
                hal_tips = self.server.hardware.get_configured_tip_racks()
                # hal_tips format: {"MyTips": "opentrons_flex_96_tiprack_1000ul", ...}
                
                for logical_name, load_name in hal_tips.items():
                    # Determine capacity from load name
                    if "384" in load_name:
                        total_tips = 384
                    else:
                        total_tips = 96
                    
                    # Get usage from tip tracking (keyed by load_name)
                    used = tip_data.get(load_name, 0)
                    if isinstance(used, dict):  # handle legacy format
                        used = used.get("used", 0)
                    remaining = total_tips - used
                    
                    # Get slot from HAL
                    labware = self.server.hardware.get_labware()
                    location = labware.get(logical_name, {}).get("Slot", "?")
                    
                    tip_racks.append(pb2.TipRackInfo(
                        rack_id=load_name,  # Use load_name for refill
                        rack_type=load_name,
                        location=str(location),
                        total_tips=total_tips,
                        remaining_tips=remaining,
                        used_tips=used,
                        percent_remaining=round(remaining / total_tips * 100, 1) if total_tips > 0 else 0
                    ))
            
            # Also include any tip racks in tip_state that aren't in HAL
            for load_name, used in tip_data.items():
                if isinstance(used, dict):
                    used = used.get("used", 0)
                # Skip if already added from HAL
                if any(r.rack_id == load_name for r in tip_racks):
                    continue
                if "tip" not in load_name.lower():
                    continue
                    
                total_tips = 384 if "384" in load_name else 96
                remaining = total_tips - used
                tip_racks.append(pb2.TipRackInfo(
                    rack_id=load_name,
                    rack_type=load_name,
                    location="?",
                    total_tips=total_tips,
                    remaining_tips=remaining,
                    used_tips=used,
                    percent_remaining=round(remaining / total_tips * 100, 1) if total_tips > 0 else 0
                ))
            
            return pb2.ListTipRacksResponse(tip_racks=tip_racks)
        except Exception as e:
            logger.error(f"ListTipRacks error: {e}", exc_info=True)
            return pb2.ListTipRacksResponse(tip_racks=[])
    
    async def RefillTipRack(self, request: pb2.RefillTipRackRequest, context) -> pb2.RefillTipRackResponse:
        """Refill a specific tip rack."""
        try:
            result = await self.server.cmd_refill_tip_rack(request.rack_id)
            return pb2.RefillTipRackResponse(
                success=True,
                message=result,
                tips_restored=96  # Default to 96, adjust based on rack type
            )
        except Exception as e:
            return pb2.RefillTipRackResponse(
                success=False,
                message=str(e),
                tips_restored=0
            )
    
    async def ResetTipTracking(self, request: empty_pb2.Empty, context) -> pb2.ResetTipTrackingResponse:
        """Reset all tip tracking (mark all racks as full)."""
        try:
            racks_reset = 0
            if self.server.config:
                labware_list = self.server.config.get("labware", [])
                for lw in labware_list:
                    if "tiprack" in lw.get("type", "").lower():
                        try:
                            await self.server.cmd_refill_tip_rack(lw.get("id", ""))
                            racks_reset += 1
                        except:
                            pass
            
            return pb2.ResetTipTrackingResponse(
                success=True,
                message=f"Reset {racks_reset} tip rack(s)",
                racks_reset=racks_reset
            )
        except Exception as e:
            return pb2.ResetTipTrackingResponse(
                success=False,
                message=str(e),
                racks_reset=0
            )
    
    # ═══════════════════════════════════════════════════════════════════════
    #                         MONITORING & ACCESSORIES
    # ═══════════════════════════════════════════════════════════════════════
    
    async def SetLights(self, request: pb2.SetLightsRequest, context) -> pb2.SetLightsResponse:
        """Set robot deck lights on/off."""
        try:
            if self.server.robot:
                result = await self.server.robot.set_lights(request.on)
                return pb2.SetLightsResponse(success=result)
            return pb2.SetLightsResponse(success=False)
        except Exception as e:
            return pb2.SetLightsResponse(success=False)
    
    async def GetRobotInfo(self, request: empty_pb2.Empty, context) -> pb2.RobotInfoResponse:
        """Get robot information."""
        if self.server.robot:
            health = await self.server.robot.get_health()
            if health:
                return pb2.RobotInfoResponse(
                    name=health.get("name", "Unknown"),
                    model=health.get("robot_model", "Opentrons Flex"),
                    serial_number=health.get("robot_serial", "Unknown"),
                    api_version=health.get("api_version", "Unknown"),
                    firmware_version=health.get("fw_version", "Unknown")
                )
        return pb2.RobotInfoResponse(name="Disconnected")
    
    async def GetModulesStatus(self, request: empty_pb2.Empty, context) -> pb2.ModulesStatusResponse:
        """Get status of all attached modules."""
        try:
            modules = []
            
            # Get live status from robot
            if self.server.robot:
                modules_data = await self.server.robot.get_modules()
                if modules_data:
                    for mod in modules_data:
                        module_info = pb2.ModuleInfo(
                            module_id=mod.get("id", "unknown"),
                            module_type=mod.get("moduleType", "unknown"),
                            location=mod.get("slot", "unknown"),
                            status=mod.get("status", "unknown"),
                            status_details=json.dumps(mod.get("data", {}))
                        )
                        modules.append(module_info)
            
            return pb2.ModulesStatusResponse(modules=modules)
        except Exception as e:
            logger.error(f"GetModulesStatus error: {e}")
            return pb2.ModulesStatusResponse(modules=[])
    
    async def SubscribeStatus(self, request: empty_pb2.Empty, context) -> AsyncIterator[pb2.StatusResponse]:
        """Subscribe to status updates (streaming)."""
        last_state = None
        while True:
            try:
                status = await self.GetStatus(request, context)
                if status.state != last_state:
                    last_state = status.state
                    yield status
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"SubscribeStatus error: {e}")
                await asyncio.sleep(1.0)


def add_servicer_to_server(server: 'OpentronsSiLA2Server', grpc_server):
    """
    Add the OpentronsServicer to a gRPC server.
    
    Args:
        server: OpentronsSiLA2Server instance
        grpc_server: gRPC server instance
    """
    servicer = OpentronsServicer(server)
    pb2_grpc.add_OpentronsServiceServicer_to_server(servicer, grpc_server)
    return servicer
