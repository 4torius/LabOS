"""
WorkflowAPI SiLA2 Feature Implementation
=========================================

Implements the WorkflowAPIBase interface generated from WorkflowAPI.sila.xml.
Wraps the existing OpentronsSiLA2Server business logic (robot_client, protocol_generator,
hardware_manager, tip_tracker) without changing any of it.

Async bridge: the existing business logic uses asyncio coroutines.
SiLA2 feature methods are synchronous (run in a ThreadPoolExecutor).
A dedicated event loop thread bridges the two worlds via _run_async().
"""

import asyncio
import json
import logging
import threading
import time
from datetime import timedelta
from typing import Optional, TYPE_CHECKING

from sila2.server import MetadataDict, ObservableCommandInstance

from generated.workflowapi import (
    WorkflowAPIBase,
    WorkflowAPIFeature,
    AbortRun_Responses,
    EmergencyStop_Responses,
    ExecuteRecipe_Responses,
    GetCurrentConfig_Responses,
    GetModulesStatus_Responses,
    GetRobotInfo_Responses,
    GetStatus_Responses,
    Home_Responses,
    Initialize_Responses,
    ListHardwareConfigs_Responses,
    ListRecipes_Responses,
    ListTipRacks_Responses,
    LoadHardwareConfig_Responses,
    PauseRun_Responses,
    RefillTipRack_Responses,
    ResetTipTracking_Responses,
    ResumeRun_Responses,
    SetLights_Responses,
)

if TYPE_CHECKING:
    from .server import OpentronsSiLA2Server as OTServer
    from .config import ServerConfig

logger = logging.getLogger(__name__)


class WorkflowAPIImpl(WorkflowAPIBase):
    """
    SiLA2 standard library implementation of the Opentrons WorkflowAPI feature.

    The implementation owns a dedicated asyncio event loop (running in a daemon
    thread) so that the synchronous sila2 dispatch thread can call async
    robot/hardware methods without creating a new loop on every call.
    """

    def __init__(self, parent_server, config: "ServerConfig") -> None:
        super().__init__(parent_server)
        self._config = config
        self._ot: Optional["OTServer"] = None

        # Single asyncio event loop shared across all cmd_* calls
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="ot-async-loop",
        )
        self._loop_thread.start()

        self.Home_default_lifetime_of_execution = timedelta(minutes=5)
        self.ExecuteRecipe_default_lifetime_of_execution = timedelta(hours=2)

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        super().start()
        from .server import OpentronsSiLA2Server as OTServer

        self._ot = OTServer(self._config)
        self._run_async(self._ot.initialize(), timeout=30)

        try:
            connected = self._run_async(
                self._ot.robot.connect_with_retry(
                    max_retries=self._config.connection_retry_count,
                    retry_delay=self._config.connection_retry_delay,
                ),
                timeout=120,
            )
            if connected:
                self._ot._status = "idle"
                logger.info("Robot connected successfully")
            else:
                logger.warning("Robot not reachable — server starts in disconnected mode")
        except Exception as exc:
            logger.warning("Robot connection failed: %s — server starts in disconnected mode", exc)

    def stop(self) -> None:
        super().stop()
        self._loop.call_soon_threadsafe(self._loop.stop)

    # ─── Async bridge ────────────────────────────────────────────────────────

    def _run_async(self, coro, timeout: float = 60):
        """Submit an asyncio coroutine to the dedicated event loop and wait for the result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ─── Robot Control ───────────────────────────────────────────────────────

    def Initialize(self, *, metadata: MetadataDict) -> Initialize_Responses:
        result = self._run_async(self._ot.cmd_initialize())
        return Initialize_Responses(InitializeResult=result or "Initialized")

    def EmergencyStop(self, *, metadata: MetadataDict) -> EmergencyStop_Responses:
        result = self._run_async(self._ot.cmd_emergency_stop())
        return EmergencyStop_Responses(StopResult=result or "Emergency stop executed")

    def GetStatus(self, *, metadata: MetadataDict) -> GetStatus_Responses:
        status = {
            "state": self._ot.get_robot_status() or "unknown",
            "run_id": self._ot.get_current_run_id() or "",
            "connected": self._ot.is_connected(),
            "config": self._ot.get_current_hardware_config() or "",
        }
        return GetStatus_Responses(StatusResult=json.dumps(status))

    def Home(
        self, *, metadata: MetadataDict, instance: ObservableCommandInstance
    ) -> Home_Responses:
        instance.begin_execution()
        instance.progress = 0.05
        result = self._run_async(self._ot.cmd_home(), timeout=120)
        instance.progress = 1.0
        return Home_Responses(HomeResult=result or "Homed successfully")

    # ─── HAL Configuration ───────────────────────────────────────────────────

    def ListHardwareConfigs(self, *, metadata: MetadataDict) -> ListHardwareConfigs_Responses:
        configs = self._ot.list_hardware_configs()
        return ListHardwareConfigs_Responses(ConfigList=json.dumps(configs))

    def LoadHardwareConfig(
        self, ConfigName: str, *, metadata: MetadataDict
    ) -> LoadHardwareConfig_Responses:
        result = self._ot.load_hardware_config(ConfigName)
        return LoadHardwareConfig_Responses(LoadResult=result or f"Loaded: {ConfigName}")

    def GetCurrentConfig(self, *, metadata: MetadataDict) -> GetCurrentConfig_Responses:
        name = self._ot.get_current_hardware_config() or ""
        config_json = ""
        if name and self._ot.config:
            config_json = json.dumps(self._ot.config, indent=2)
        payload = json.dumps({"name": name, "config": config_json})
        return GetCurrentConfig_Responses(CurrentConfig=payload)

    # ─── Recipe Execution ────────────────────────────────────────────────────

    def ListRecipes(self, *, metadata: MetadataDict) -> ListRecipes_Responses:
        recipe_dir = self._ot.base_dir / "Library" / "Recipes"
        recipes = []
        if recipe_dir.exists():
            for f in sorted(recipe_dir.glob("*.json")):
                try:
                    with open(f) as rf:
                        data = json.load(rf)
                    recipes.append(
                        {
                            "filename": f.name,
                            "name": data.get("name", f.stem),
                            "description": data.get("description", ""),
                            "steps": len(data.get("Steps", data.get("commands", []))),
                        }
                    )
                except Exception:
                    recipes.append({"filename": f.name, "name": f.stem})
        return ListRecipes_Responses(RecipeList=json.dumps(recipes))

    def ExecuteRecipe(
        self,
        RecipeName: str,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstance,
    ) -> ExecuteRecipe_Responses:
        instance.begin_execution()
        instance.progress = 0.05

        # cmd_execute_recipe_by_name → cmd_run_protocol waits for full run completion.
        # Timeout must cover the entire execution (up to 2 hours).
        run_id, _ = self._run_async(
            self._ot.cmd_execute_recipe_by_name(RecipeName), timeout=7200
        )

        instance.progress = 1.0
        return ExecuteRecipe_Responses(RunId=run_id or "")

    def PauseRun(self, *, metadata: MetadataDict) -> PauseRun_Responses:
        result = self._run_async(self._ot.cmd_pause_run())
        return PauseRun_Responses(PauseResult=result or "Paused")

    def ResumeRun(self, *, metadata: MetadataDict) -> ResumeRun_Responses:
        result = self._run_async(self._ot.cmd_resume_run())
        return ResumeRun_Responses(ResumeResult=result or "Resumed")

    def AbortRun(self, *, metadata: MetadataDict) -> AbortRun_Responses:
        result = self._run_async(self._ot.cmd_abort_run())
        return AbortRun_Responses(AbortResult=result or "Aborted")

    # ─── Tip Management ──────────────────────────────────────────────────────

    def ListTipRacks(self, *, metadata: MetadataDict) -> ListTipRacks_Responses:
        tip_status_json = self._ot.get_tip_status()
        tip_data = json.loads(tip_status_json) if tip_status_json else {}
        racks = []
        if self._ot.hardware:
            hal_tips = self._ot.hardware.get_configured_tip_racks()
            labware = self._ot.hardware.get_labware()
            for logical_name, load_name in hal_tips.items():
                total = 384 if "384" in load_name else 96
                used = tip_data.get(load_name, 0)
                if isinstance(used, dict):
                    used = used.get("used", 0)
                slot = str(labware.get(logical_name, {}).get("Slot", "?"))
                racks.append(
                    {
                        "rack_id": load_name,
                        "rack_type": load_name,
                        "location": slot,
                        "total": total,
                        "remaining": total - used,
                        "used": used,
                        "percent_remaining": round((total - used) / total * 100, 1) if total else 0,
                    }
                )
        return ListTipRacks_Responses(TipRackList=json.dumps(racks))

    def RefillTipRack(self, RackSlot: str, *, metadata: MetadataDict) -> RefillTipRack_Responses:
        result = self._run_async(self._ot.cmd_refill_tip_rack(RackSlot))
        return RefillTipRack_Responses(RefillResult=result or f"Refilled: {RackSlot}")

    def ResetTipTracking(self, *, metadata: MetadataDict) -> ResetTipTracking_Responses:
        count = 0
        if self._ot.config:
            for lw in self._ot.config.get("labware", []):
                if "tiprack" in lw.get("type", "").lower():
                    try:
                        self._run_async(self._ot.cmd_refill_tip_rack(lw.get("id", "")))
                        count += 1
                    except Exception:
                        pass
        return ResetTipTracking_Responses(ResetResult=f"Reset {count} tip rack(s)")

    # ─── Monitoring ──────────────────────────────────────────────────────────

    def SetLights(self, On: bool, *, metadata: MetadataDict) -> SetLights_Responses:
        if self._ot.robot:
            self._run_async(self._ot.robot.set_lights(On))
        return SetLights_Responses(LightsResult="on" if On else "off")

    def GetRobotInfo(self, *, metadata: MetadataDict) -> GetRobotInfo_Responses:
        info: dict = {"connected": False}
        if self._ot.robot:
            try:
                health = self._run_async(self._ot.robot.get_health())
                if health:
                    info = {
                        "name": health.get("name", "Unknown"),
                        "model": health.get("robot_model", "Opentrons Flex"),
                        "serial": health.get("robot_serial", "Unknown"),
                        "api_version": health.get("api_version", "Unknown"),
                        "connected": True,
                    }
            except Exception:
                pass
        return GetRobotInfo_Responses(RobotInfo=json.dumps(info))

    def GetModulesStatus(self, *, metadata: MetadataDict) -> GetModulesStatus_Responses:
        modules = []
        if self._ot.robot:
            try:
                data = self._run_async(self._ot.robot.get_modules())
                if data:
                    modules = data
            except Exception:
                pass
        return GetModulesStatus_Responses(ModulesStatus=json.dumps(modules))
