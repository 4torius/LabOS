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
import dataclasses
import json
import logging
import os
import threading
import time
from datetime import timedelta
from pathlib import Path
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
        # Only governs how long a *finished* run's result stays fetchable by a
        # client that reconnects late — not the run duration itself (that's
        # config.run_timeout, enforced inside wait_for_run_completion). Generous
        # so the webapp can resync after losing its poll connection during a
        # multi-day protocol.
        self.ExecuteRecipe_default_lifetime_of_execution = timedelta(days=7)

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        super().start()
        from .server import OpentronsSiLA2Server as OTServer

        self._ot = OTServer(self._config)
        self._run_async(self._ot.initialize(), timeout=30)

        if self._ot.robot is None:
            raise RuntimeError("OTServer.initialize() did not set up robot — cannot start")

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
                # connect_with_retry() may have self-healed to a new DHCP
                # address via mDNS — keep config in sync so logs/status and
                # any later RobotClient rebuild use the address actually in use.
                self._config.robot_ip = self._ot.robot.host
                self._config.robot_port = self._ot.robot.port
                logger.info(
                    "Robot connected successfully (%s:%s)",
                    self._ot.robot.host, self._ot.robot.port,
                )
            else:
                logger.warning("Robot not reachable — server starts in disconnected mode")
        except Exception as exc:
            logger.warning("Robot connection failed: %s — server starts in disconnected mode", exc)

    def stop(self) -> None:
        super().stop()
        self._loop.call_soon_threadsafe(self._loop.stop)

    def _require_ot(self) -> "OTServer":
        """Narrow self._ot for pyright and fail loudly if start() hasn't run yet.

        Every cmd_*/SiLA2 feature method below runs only after start() has
        completed (that's the SiLA2 server lifecycle contract), so self._ot
        should never actually be None here — this guard documents that
        invariant and turns a violation into a clear error instead of a
        confusing AttributeError.
        """
        if self._ot is None:
            raise RuntimeError("Server not started — start() must be called first")
        return self._ot

    # ─── Async bridge ────────────────────────────────────────────────────────

    def _run_async(self, coro, timeout: Optional[float] = 60):
        """Submit an asyncio coroutine to the dedicated event loop and wait for the result.

        timeout=None blocks forever (Future.result's native behavior) — used for
        recipe execution, where the real bound is config.run_timeout enforced
        deeper in wait_for_run_completion, not this dispatch wrapper.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ─── Robot Control ───────────────────────────────────────────────────────

    def Initialize(self, *, metadata: MetadataDict) -> Initialize_Responses:
        ot = self._require_ot()
        result = self._run_async(ot.cmd_initialize())
        return Initialize_Responses(InitializeResult=result or "Initialized")

    def EmergencyStop(self, *, metadata: MetadataDict) -> EmergencyStop_Responses:
        ot = self._require_ot()
        result = self._run_async(ot.cmd_emergency_stop())
        return EmergencyStop_Responses(StopResult=result or "Emergency stop executed")

    def GetStatus(self, *, metadata: MetadataDict) -> GetStatus_Responses:
        ot = self._require_ot()
        status = {
            "state": ot.get_robot_status() or "unknown",
            "run_id": ot.get_current_run_id() or "",
            "connected": ot.is_connected(),
            "config": ot.get_current_hardware_config() or "",
        }
        return GetStatus_Responses(StatusResult=json.dumps(status))

    def Home(
        self, *, metadata: MetadataDict, instance: ObservableCommandInstance
    ) -> Home_Responses:
        ot = self._require_ot()
        instance.begin_execution()
        instance.progress = 0.05
        result = self._run_async(ot.cmd_home(), timeout=120)
        instance.progress = 1.0
        return Home_Responses(HomeResult=result or "Homed successfully")

    # ─── HAL Configuration ───────────────────────────────────────────────────

    def ListHardwareConfigs(self, *, metadata: MetadataDict) -> ListHardwareConfigs_Responses:
        ot = self._require_ot()
        configs = ot.list_hardware_configs()
        return ListHardwareConfigs_Responses(ConfigList=json.dumps(configs))

    def LoadHardwareConfig(
        self, ConfigName: str, *, metadata: MetadataDict
    ) -> LoadHardwareConfig_Responses:
        ot = self._require_ot()
        result = ot.load_hardware_config(ConfigName)
        return LoadHardwareConfig_Responses(LoadResult=result or f"Loaded: {ConfigName}")

    def GetCurrentConfig(self, *, metadata: MetadataDict) -> GetCurrentConfig_Responses:
        ot = self._require_ot()
        name = ot.get_current_hardware_config() or ""
        config_json = ""
        if name and ot.config:
            config_json = json.dumps(dataclasses.asdict(ot.config), indent=2)
        payload = json.dumps({"name": name, "config": config_json})
        return GetCurrentConfig_Responses(CurrentConfig=payload)

    # ─── Recipe Execution ────────────────────────────────────────────────────

    def ListRecipes(self, *, metadata: MetadataDict) -> ListRecipes_Responses:
        ot = self._require_ot()
        # dir_recipes (config.yaml -> directories.recipes) is now read into
        # ServerConfig; resolve it the same way the existing legacy fallback
        # in server.py already does (os.path.abspath), and only fall back to
        # the old base_dir-relative guess if it's unset.
        configured = getattr(ot.config, "dir_recipes", None) if ot.config else None
        recipe_dir = Path(os.path.abspath(configured)) if configured else ot.base_dir / "Library" / "Recipes"
        recipes = []
        if recipe_dir.exists():
            for f in sorted(recipe_dir.glob("*.json")):
                try:
                    with open(f, encoding="utf-8-sig") as rf:
                        data = json.load(rf)
                    recipes.append(
                        {
                            "filename": f.name,
                            "name": data.get("name", f.stem),
                            "description": data.get("description", ""),
                            "steps": len(data.get("Steps", data.get("commands", []))),
                        }
                    )
                except Exception as exc:
                    logger.debug("Failed to parse recipe %s: %s", f.name, exc)
                    recipes.append({"filename": f.name, "name": f.stem})
        return ListRecipes_Responses(RecipeList=json.dumps(recipes))

    def ExecuteRecipe(
        self,
        RecipeName: str,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstance,
    ) -> ExecuteRecipe_Responses:
        ot = self._require_ot()
        instance.begin_execution()
        instance.progress = 0.05

        # cmd_execute_recipe_by_name → cmd_run_protocol waits for full run completion.
        # No timeout at this dispatch layer: the one authoritative bound is
        # config.run_timeout, enforced inside wait_for_run_completion. A second
        # hardcoded value here was redundant and easy to desync from that one
        # (e.g. it stayed at 2h even when run_timeout was raised/unlimited).
        run_id, result = self._run_async(
            ot.cmd_execute_recipe_by_name(RecipeName), timeout=None
        )

        # cmd_run_protocol NEVER raises just because the robot's own run
        # ended anything other than "succeeded" (a real pickUpTip/liquid/
        # hardware failure included) - it only formats that outcome into
        # `result` ("Run {run_id}: {final_status}") and returns normally, so
        # a stopped/failed run silently completed this SiLA2 command as a
        # success with nothing but a RunId to show for it (confirmed on
        # hardware: a run that entered unrecovered error-recovery over a
        # missing tip and auto-stopped still reported "ExecuteRecipe
        # completed" here). The run's own final status is the one place
        # that's actually known - surface it as a real command failure
        # instead of letting it evaporate in a discarded return value.
        final_status = result.rsplit(": ", 1)[-1] if result else ""
        if final_status != "succeeded":
            raise RuntimeError(
                f"Recipe execution did not succeed (RunId={run_id}): {result} - "
                f"check the run's own command log on the robot for what happened."
            )

        instance.progress = 1.0
        return ExecuteRecipe_Responses(RunId=run_id or "")

    def PauseRun(self, *, metadata: MetadataDict) -> PauseRun_Responses:
        ot = self._require_ot()
        result = self._run_async(ot.cmd_pause_run())
        return PauseRun_Responses(PauseResult=result or "Paused")

    def ResumeRun(self, *, metadata: MetadataDict) -> ResumeRun_Responses:
        ot = self._require_ot()
        result = self._run_async(ot.cmd_resume_run())
        return ResumeRun_Responses(ResumeResult=result or "Resumed")

    def AbortRun(self, *, metadata: MetadataDict) -> AbortRun_Responses:
        ot = self._require_ot()
        result = self._run_async(ot.cmd_abort_run())
        return AbortRun_Responses(AbortResult=result or "Aborted")

    # ─── Tip Management ──────────────────────────────────────────────────────

    def ListTipRacks(self, *, metadata: MetadataDict) -> ListTipRacks_Responses:
        ot = self._require_ot()
        tip_status_json = ot.get_tip_status()
        tip_data = json.loads(tip_status_json) if tip_status_json else {}
        racks = []
        if ot.hardware:
            hal_tips = ot.hardware.get_configured_tip_racks()
            labware = ot.hardware.get_labware()
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
        ot = self._require_ot()
        result = self._run_async(ot.cmd_refill_tip_rack(RackSlot))
        return RefillTipRack_Responses(RefillResult=result or f"Refilled: {RackSlot}")

    def ResetTipTracking(self, *, metadata: MetadataDict) -> ResetTipTracking_Responses:
        ot = self._require_ot()
        count = 0
        if ot.hardware:
            # Tip racks configured in the currently-loaded HAL (same source as
            # ListTipRacks) — self._ot.config is a ServerConfig dataclass, not
            # a labware list, and never had the "labware" data this used to read.
            hal_tips = ot.hardware.get_configured_tip_racks()
            for logical_name, load_name in hal_tips.items():
                try:
                    self._run_async(ot.cmd_refill_tip_rack(load_name))
                    count += 1
                except Exception as exc:
                    logger.warning("Failed to reset tip tracking for %s: %s", load_name, exc)
        return ResetTipTracking_Responses(ResetResult=f"Reset {count} tip rack(s)")

    # ─── Monitoring ──────────────────────────────────────────────────────────

    def SetLights(self, On: bool, *, metadata: MetadataDict) -> SetLights_Responses:
        ot = self._require_ot()
        if ot.robot:
            self._run_async(ot.robot.set_lights(On))
        return SetLights_Responses(LightsResult="on" if On else "off")

    def GetRobotInfo(self, *, metadata: MetadataDict) -> GetRobotInfo_Responses:
        ot = self._require_ot()
        info: dict = {"connected": False}
        if ot.robot:
            try:
                health = self._run_async(ot.robot.get_health())
                if health:
                    info = {
                        "name": health.get("name", "Unknown"),
                        "model": health.get("robot_model", "Opentrons Flex"),
                        "serial": health.get("robot_serial", "Unknown"),
                        "api_version": health.get("api_version", "Unknown"),
                        "connected": True,
                    }
            except Exception as exc:
                logger.warning("Failed to fetch robot info: %s", exc)
        return GetRobotInfo_Responses(RobotInfo=json.dumps(info))

    def GetModulesStatus(self, *, metadata: MetadataDict) -> GetModulesStatus_Responses:
        ot = self._require_ot()
        modules = []
        if ot.robot:
            try:
                data = self._run_async(ot.robot.get_modules())
                if data:
                    modules = data
            except Exception as exc:
                logger.warning("Failed to fetch module status: %s", exc)
        return GetModulesStatus_Responses(ModulesStatus=json.dumps(modules))
