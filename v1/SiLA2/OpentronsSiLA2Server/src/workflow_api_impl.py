"""
WorkflowAPI SiLA2 Feature Implementation
=========================================

Implements WorkflowAPIBase, generated from features/WorkflowAPI.sila.xml, on
top of the existing OpentronsSiLA2Server business logic (robot_client,
protocol_generator, hardware_manager, tip_tracker).

Two shaping rules, mirrored from the FDL:

* Commands do something to the robot; anything that only *reads* state is a
  Property. GetStatus/GetRobotInfo/GetModulesStatus/GetCurrentConfig were
  commands, so a client asking "what config is loaded?" appeared in the UI as
  an operation to run. They are Status/RobotInfo/ModulesStatus/ActiveConfig now.

* Rather than one SiLA2 command per Opentrons primitive, RunSteps takes recipe
  steps inline. The server already knew how to execute every step the recipe
  format defines — Transfer, Mix, MoveLabware, heater-shaker, Delay — but the
  only way to reach any of them was to author a recipe file first.

Async bridge: the business logic is asyncio, SiLA2 feature methods are
synchronous (called on a thread-pool). A dedicated event-loop thread bridges
the two via _run_async().
"""

import asyncio
import base64
import dataclasses
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from sila2.server import MetadataDict, ObservableCommandInstance

from generated.workflowapi import (
    WorkflowAPIBase,
    AbortRun_Responses,
    CaptureImage_Responses,
    CaptureTimelapse_Responses,
    EmergencyStop_Responses,
    ExecuteRecipe_Responses,
    Home_Responses,
    LoadHardwareConfig_Responses,
    MoveLabware_Responses,
    PauseRun_Responses,
    RefillTipRack_Responses,
    ResetTipTracking_Responses,
    ResumeRun_Responses,
    RunSteps_Responses,
    SetLights_Responses,
    SetModuleLatch_Responses,
    SetModuleTemperature_Responses,
    ShakeModule_Responses,
    ValidateRecipe_Responses,
)

if TYPE_CHECKING:
    from .server import OpentronsSiLA2Server as OTServer
    from .config import ServerConfig

logger = logging.getLogger(__name__)

#: Addressable Flex deck slots, plus the column-4 staging area the gripper can
#: reach. Used only to say which slots are free when a move is refused.
_FLEX_SLOTS = ("A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3",
               "D1", "D2", "D3", "A4", "B4", "C4", "D4")


class WorkflowAPIImpl(WorkflowAPIBase):
    """SiLA2 implementation of the Opentrons WorkflowAPI feature."""

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
        self.RunSteps_default_lifetime_of_execution = timedelta(days=1)

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

        self._publish_state()

    def stop(self) -> None:
        super().stop()
        # Release the robot's HTTP session before tearing the loop down —
        # OTServer.stop() was previously unreachable (nothing called it), so
        # every shutdown leaked the connection.
        if self._ot is not None:
            try:
                self._run_async(self._ot.stop(), timeout=10)
            except Exception as exc:
                logger.warning("Robot disconnect on shutdown failed: %s", exc)
        self._loop.call_soon_threadsafe(self._loop.stop)

    def _require_ot(self) -> "OTServer":
        """Narrow self._ot for pyright and fail loudly if start() hasn't run yet.

        Every feature method below runs only after start() has completed (the
        SiLA2 server lifecycle contract), so this should never fire — it turns
        a violated invariant into a clear error instead of an AttributeError.
        """
        if self._ot is None:
            raise RuntimeError("Server not started — start() must be called first")
        return self._ot

    # ─── Async bridge ────────────────────────────────────────────────────────

    def _run_async(self, coro, timeout: Optional[float] = 60):
        """Submit a coroutine to the dedicated event loop and wait for it.

        timeout=None blocks forever (Future.result's native behavior) — used for
        recipe execution, where the real bound is config.run_timeout enforced
        deeper in wait_for_run_completion, not this dispatch wrapper.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ═════════════════════════════════════════════════════════════════════════
    #  PROPERTIES
    # ═════════════════════════════════════════════════════════════════════════

    def _publish_state(self) -> None:
        """Push the observable properties to any subscribed client.

        Called after anything that changes robot state, so a dashboard shows
        the change instead of waiting for its next poll.
        """
        try:
            ot = self._ot
            if ot is None:
                return
            self.update_Status(json.dumps({
                "state": ot.get_robot_status() or "unknown",
                "run_id": ot.get_current_run_id() or "",
                "connected": ot.is_connected(),
                "config": ot.get_current_hardware_config() or "",
            }))
            self.update_CurrentRunId(ot.get_current_run_id() or "")
            self.update_ActiveConfig(self._active_config_json())
            self.update_TipsAvailable(self._tips_json())
        except Exception as exc:
            logger.debug("Could not publish state: %s", exc)

    def _active_config_json(self) -> str:
        ot = self._require_ot()
        name = ot.get_current_hardware_config() or ""
        config_json = ""
        if name and ot.config:
            config_json = json.dumps(dataclasses.asdict(ot.config), indent=2)
        return json.dumps({"name": name, "config": config_json})

    def _tips_json(self) -> str:
        """Per-rack tip usage, joined with the racks the active HAL declares."""
        ot = self._require_ot()
        tip_status_json = ot.get_tip_status()
        tip_data = json.loads(tip_status_json) if tip_status_json else {}
        racks: List[Dict[str, Any]] = []
        if ot.hardware:
            hal_tips = ot.hardware.get_configured_tip_racks()
            labware = ot.hardware.get_labware()
            for logical_name, load_name in hal_tips.items():
                total = 384 if "384" in load_name else 96
                used = tip_data.get(load_name, 0)
                if isinstance(used, dict):
                    used = used.get("used", 0)
                racks.append({
                    "rack_id": load_name,
                    "logical_name": logical_name,
                    "location": str(labware.get(logical_name, {}).get("Slot", "?")),
                    "total": total,
                    "remaining": total - used,
                    "used": used,
                    "percent_remaining": round((total - used) / total * 100, 1) if total else 0,
                })
        return json.dumps(racks)

    def Status_on_subscription(self, *, metadata: MetadataDict):
        self._publish_state()
        return None

    def CurrentRunId_on_subscription(self, *, metadata: MetadataDict):
        self._publish_state()
        return None

    def ActiveConfig_on_subscription(self, *, metadata: MetadataDict):
        self._publish_state()
        return None

    def TipsAvailable_on_subscription(self, *, metadata: MetadataDict):
        self._publish_state()
        return None

    def get_RobotInfo(self, *, metadata: MetadataDict) -> str:
        ot = self._require_ot()
        info: Dict[str, Any] = {"connected": False}
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
        return json.dumps(info)

    def get_ModulesStatus(self, *, metadata: MetadataDict) -> str:
        ot = self._require_ot()
        modules: List[Any] = []
        if ot.robot:
            try:
                data = self._run_async(ot.robot.get_modules())
                if data:
                    modules = data
            except Exception as exc:
                logger.warning("Failed to fetch module status: %s", exc)
        return json.dumps(modules)

    # ═════════════════════════════════════════════════════════════════════════
    #  ROBOT CONTROL
    # ═════════════════════════════════════════════════════════════════════════

    def Home(
        self, *, metadata: MetadataDict, instance: ObservableCommandInstance
    ) -> Home_Responses:
        ot = self._require_ot()
        instance.begin_execution()
        instance.progress = 0.05
        result = self._run_async(ot.cmd_home(), timeout=120)
        instance.progress = 1.0
        self._publish_state()
        return Home_Responses(HomeResult=result or "Homed successfully")

    def EmergencyStop(self, *, metadata: MetadataDict) -> EmergencyStop_Responses:
        ot = self._require_ot()
        result = self._run_async(ot.cmd_emergency_stop())
        self._publish_state()
        return EmergencyStop_Responses(StopResult=result or "Emergency stop executed")

    def SetLights(self, On: bool, *, metadata: MetadataDict) -> SetLights_Responses:
        ot = self._require_ot()
        if ot.robot:
            self._run_async(ot.robot.set_lights(On))
        return SetLights_Responses(LightsResult="on" if On else "off")

    # ═════════════════════════════════════════════════════════════════════════
    #  HARDWARE CONFIGURATION
    # ═════════════════════════════════════════════════════════════════════════

    def LoadHardwareConfig(
        self, ConfigName: str, *, metadata: MetadataDict
    ) -> LoadHardwareConfig_Responses:
        ot = self._require_ot()
        result = ot.load_hardware_config(ConfigName)
        self._publish_state()
        return LoadHardwareConfig_Responses(LoadResult=result or f"Loaded: {ConfigName}")

    # ═════════════════════════════════════════════════════════════════════════
    #  RECIPE EXECUTION
    # ═════════════════════════════════════════════════════════════════════════

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
        self._publish_state()

        # cmd_execute_recipe_by_name → cmd_run_protocol waits for full run
        # completion. No timeout at this dispatch layer: the one authoritative
        # bound is config.run_timeout, enforced inside wait_for_run_completion.
        run_id, result = self._run_async(
            ot.cmd_execute_recipe_by_name(RecipeName), timeout=None
        )
        self._raise_unless_succeeded(run_id, result)
        instance.progress = 1.0
        self._publish_state()
        return ExecuteRecipe_Responses(RunId=run_id or "")

    def RunSteps(
        self,
        StepsJson: str,
        Description: str,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstance,
    ) -> RunSteps_Responses:
        """Run recipe steps supplied inline, against the active HAL config."""
        self._require_ot()
        instance.begin_execution()
        instance.progress = 0.05

        try:
            steps = json.loads(StepsJson) if StepsJson else []
        except json.JSONDecodeError as exc:
            raise ValueError(f"StepsJson is not valid JSON: {exc}") from exc
        if isinstance(steps, dict):
            # A single step object is an easy and harmless thing to send.
            steps = [steps]
        if not isinstance(steps, list) or not steps:
            raise ValueError("StepsJson must be a non-empty JSON array of recipe steps")
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or not step.get("Command"):
                raise ValueError(f"Step {index + 1} has no \"Command\" field")

        instance.progress = 0.1
        run_id = self._run_inline(
            steps, Description or "Steps supplied inline via RunSteps"
        )
        instance.progress = 1.0
        return RunSteps_Responses(RunId=run_id)

    def ValidateRecipe(
        self, RecipeName: str, *, metadata: MetadataDict
    ) -> ValidateRecipe_Responses:
        ot = self._require_ot()
        result = self._run_async(ot.cmd_validate_recipe_hal(RecipeName), timeout=120)
        return ValidateRecipe_Responses(ValidationResult=result or "{}")

    def PauseRun(self, *, metadata: MetadataDict) -> PauseRun_Responses:
        ot = self._require_ot()
        result = self._run_async(ot.cmd_pause_run())
        self._publish_state()
        return PauseRun_Responses(PauseResult=result or "Paused")

    def ResumeRun(self, *, metadata: MetadataDict) -> ResumeRun_Responses:
        ot = self._require_ot()
        result = self._run_async(ot.cmd_resume_run())
        self._publish_state()
        return ResumeRun_Responses(ResumeResult=result or "Resumed")

    def AbortRun(self, *, metadata: MetadataDict) -> AbortRun_Responses:
        ot = self._require_ot()
        result = self._run_async(ot.cmd_abort_run())
        self._publish_state()
        return AbortRun_Responses(AbortResult=result or "Aborted")

    @staticmethod
    def _raise_unless_succeeded(run_id: Optional[str], result: Optional[str]) -> None:
        """Turn a non-successful robot run into a failed SiLA2 command.

        cmd_run_protocol never raises just because the robot's own run ended in
        anything other than "succeeded" (a real pick-up-tip or liquid failure
        included) — it formats the outcome into `result` and returns normally.
        A stopped run therefore completed this command as a success with
        nothing but a RunId to show for it (confirmed on hardware: a run that
        entered unrecovered error-recovery over a missing tip and auto-stopped
        still reported "completed"). The run's own final status is the one
        thing actually known, so surface it.
        """
        final_status = result.rsplit(": ", 1)[-1] if result else ""
        if final_status != "succeeded":
            raise RuntimeError(
                f"Run did not succeed (RunId={run_id}): {result} — "
                f"check the run's own command log on the robot for what happened."
            )

    # ═════════════════════════════════════════════════════════════════════════
    #  DECK AND MODULES
    # ═════════════════════════════════════════════════════════════════════════

    def _run_inline(self, steps: List[Dict[str, Any]], description: str) -> str:
        """Execute steps as a one-off protocol and return the run id."""
        ot = self._require_ot()
        recipe = {
            "ProtocolName": description,
            "Description": description,
            "Requirements": {},
            "Steps": steps,
        }
        run_id, result = self._run_async(
            ot.cmd_run_protocol(json.dumps(recipe), "json"), timeout=None
        )
        self._raise_unless_succeeded(run_id, result)
        self._publish_state()
        return run_id or ""

    def MoveLabware(
        self,
        LabwareName: str,
        NewLocation: str,
        UseGripper: bool,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstance,
    ) -> MoveLabware_Responses:
        ot = self._require_ot()
        instance.begin_execution()
        instance.progress = 0.05

        # Check against the deck *before* the gripper moves. Both of these used
        # to fail deep inside the generated protocol, after the run had already
        # started and the robot had homed.
        hardware = ot.hardware
        if hardware is not None:
            known_labware = hardware.get_labware() or {}
            if LabwareName not in known_labware:
                raise ValueError(
                    f"'{LabwareName}' is not labware in the active hardware config "
                    f"'{hardware.get_current_config_name()}'. On the deck: "
                    f"{', '.join(sorted(known_labware)) or 'nothing'}"
                )
            modules = hardware.get_modules() or {}
            if NewLocation not in modules:
                occupied = {
                    slot: name for name, slot in hardware._slot_occupants().items()
                    if name != LabwareName
                }
                if NewLocation in occupied:
                    raise ValueError(
                        f"Slot {NewLocation} already holds '{occupied[NewLocation]}'. "
                        f"Free slots: {', '.join(sorted(set(_FLEX_SLOTS) - set(occupied))) or 'none'}"
                    )

        self._run_inline(
            [{
                "Command": "MoveLabware",
                "LabwareID": LabwareName,
                "NewLocation": NewLocation,
                "UseGripper": UseGripper,
            }],
            f"MoveLabware {LabwareName} -> {NewLocation}",
        )
        instance.progress = 0.9

        # The plate has physically moved, so the config that describes the deck
        # has to say so. Without this the next recipe loads it in the slot it
        # started in, and the deck the operator sees stops matching the deck the
        # software believes in.
        hal_note = ""
        if hardware is not None:
            try:
                hal_note = f"; hardware config updated ({hardware.record_labware_move(LabwareName, NewLocation)})"
            except Exception as exc:
                # The move succeeded; only the bookkeeping failed. Say so loudly
                # rather than reporting a clean success over a config that is now
                # wrong.
                logger.error("Labware moved but the hardware config was not updated: %s", exc)
                hal_note = (f"; WARNING the hardware config could NOT be updated ({exc}) — "
                            f"it still shows the old position, fix it on the Hardware page")

        instance.progress = 1.0
        self._publish_state()
        return MoveLabware_Responses(
            MoveResult=f"Moved {LabwareName} to {NewLocation}"
                       f"{' with the gripper' if UseGripper else ' (manual move)'}{hal_note}"
        )

    def SetModuleTemperature(
        self,
        ModuleName: str,
        Celsius: float,
        WaitForTemperature: bool,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstance,
    ) -> SetModuleTemperature_Responses:
        instance.begin_execution()
        instance.progress = 0.05
        if Celsius <= 0:
            steps = [{"Command": "HeaterShakerDeactivateHeater", "ModuleID": ModuleName}]
            message = f"{ModuleName}: heating deactivated"
        else:
            steps = [{
                "Command": "HeaterShakerSetTemperature",
                "ModuleID": ModuleName,
                "Temperature": Celsius,
            }]
            if WaitForTemperature:
                steps.append({
                    "Command": "HeaterShakerWaitForTemperature",
                    "ModuleID": ModuleName,
                    "Temperature": Celsius,
                })
            message = f"{ModuleName}: target {Celsius} C"
        self._run_inline(steps, f"SetModuleTemperature {ModuleName}")
        instance.progress = 1.0
        return SetModuleTemperature_Responses(TemperatureResult=message)

    def ShakeModule(
        self,
        ModuleName: str,
        RPM: int,
        DurationSeconds: int,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstance,
    ) -> ShakeModule_Responses:
        instance.begin_execution()
        instance.progress = 0.05
        if DurationSeconds <= 0:
            steps = [{"Command": "HeaterShakerDeactivateShaker", "ModuleID": ModuleName}]
            message = f"{ModuleName}: shaking stopped"
        else:
            steps = [
                {"Command": "HeaterShakerSetShake", "ModuleID": ModuleName, "RPM": RPM},
                {"Command": "Delay", "Seconds": DurationSeconds},
                {"Command": "HeaterShakerDeactivateShaker", "ModuleID": ModuleName},
            ]
            message = f"{ModuleName}: shook at {RPM} rpm for {DurationSeconds}s"
        self._run_inline(steps, f"ShakeModule {ModuleName}")
        instance.progress = 1.0
        return ShakeModule_Responses(ShakeResult=message)

    def SetModuleLatch(
        self, ModuleName: str, Open: bool, *, metadata: MetadataDict
    ) -> SetModuleLatch_Responses:
        command = "HeaterShakerOpenLatch" if Open else "HeaterShakerCloseLatch"
        self._run_inline(
            [{"Command": command, "ModuleID": ModuleName}],
            f"SetModuleLatch {ModuleName}",
        )
        return SetModuleLatch_Responses(
            LatchResult=f"{ModuleName}: latch {'opened' if Open else 'closed'}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    #  TIPS
    # ═════════════════════════════════════════════════════════════════════════

    def RefillTipRack(self, RackSlot: str, *, metadata: MetadataDict) -> RefillTipRack_Responses:
        ot = self._require_ot()
        result = self._run_async(ot.cmd_refill_tip_rack(RackSlot))
        self._publish_state()
        return RefillTipRack_Responses(RefillResult=result or f"Refilled: {RackSlot}")

    def ResetTipTracking(self, *, metadata: MetadataDict) -> ResetTipTracking_Responses:
        ot = self._require_ot()
        count = 0
        if ot.hardware:
            # Tip racks configured in the currently-loaded HAL — ot.config is a
            # ServerConfig dataclass, not a labware list.
            for _logical_name, load_name in ot.hardware.get_configured_tip_racks().items():
                try:
                    self._run_async(ot.cmd_refill_tip_rack(load_name))
                    count += 1
                except Exception as exc:
                    logger.warning("Failed to reset tip tracking for %s: %s", load_name, exc)
        self._publish_state()
        return ResetTipTracking_Responses(ResetResult=f"Reset {count} tip rack(s)")

    # ═════════════════════════════════════════════════════════════════════════
    #  CAMERA
    # ═════════════════════════════════════════════════════════════════════════

    def CaptureImage(
        self,
        Label: str,
        AllowRobotMotion: bool,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstance,
    ) -> CaptureImage_Responses:
        """Photograph the deck and save the image where the webapp serves it.

        The capture machinery already existed end to end — a TakeSnapshot recipe
        step that grabs a frame with gst-launch on the robot and base64-encodes
        it into the run log, and extract_images_from_log() to pull it back out —
        but nothing exposed it, and the images landed in a server-local folder
        no route served. This is the missing front door.
        """
        ot = self._require_ot()
        instance.begin_execution()
        instance.progress = 0.05

        if not getattr(self._config, "camera_enabled", True):
            return CaptureImage_Responses(CaptureResult=json.dumps({
                "saved": False, "method": "none",
                "detail": "Camera disabled in config.yaml (camera.enabled: false)",
            }))

        label = "".join(c if c.isalnum() or c in "-_" else "_" for c in (Label or "deck"))
        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{label}.jpg"
        target_dir = self._images_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename

        # ── Path 1: the robot's HTTP camera endpoint. Nothing moves. ─────────
        instance.progress = 0.3
        http_detail = "not attempted"
        if getattr(self._config, "camera_http_probe", True):
            try:
                data = self._run_async(self._capture_over_http(), timeout=30)
                if data:
                    target.write_bytes(data)
                    instance.progress = 1.0
                    return CaptureImage_Responses(CaptureResult=json.dumps({
                        "saved": True, "path": str(target), "url": f"/api/images/{filename}",
                        "method": "http", "detail": "Captured via the robot's camera endpoint; nothing moved",
                    }))
                http_detail = "robot has no HTTP camera endpoint"
            except Exception as exc:
                http_detail = f"HTTP capture failed: {exc}"
        else:
            http_detail = "disabled in config (camera.http_probe: false)"

        # ── Path 2: a one-step protocol. This HOMES THE ROBOT. ───────────────
        if not AllowRobotMotion:
            instance.progress = 1.0
            return CaptureImage_Responses(CaptureResult=json.dumps({
                "saved": False, "method": "none", "detail": (
                    f"{http_detail}. The remaining option runs a one-step protocol, "
                    f"which homes the robot; refused because AllowRobotMotion is false. "
                    f"Set it true only with someone watching the deck."
                ),
            }))

        instance.progress = 0.5
        device = getattr(self._config, "camera_device", "/dev/video2")
        run_id = self._run_inline(
            [{"Command": "TakeSnapshot", "Filename": filename, "Device": device}],
            f"CaptureImage {label}",
        )
        saved = self._run_async(
            ot.robot.extract_images_from_log(run_id, str(target_dir)), timeout=60
        ) if ot.robot else 0
        instance.progress = 1.0
        return CaptureImage_Responses(CaptureResult=json.dumps({
            "saved": bool(saved), "path": str(target_dir), "url": "/api/images/",
            "method": "protocol", "run_id": run_id,
            "detail": (f"{http_detail}. Captured by running a one-step protocol "
                       f"(the robot was homed). {saved} image(s) extracted from the run log."),
        }))

    #: Bound on a timelapse. Each frame is a full-resolution JPEG carried
    #: base64-encoded through the run log; a few hundred is already a very
    #: large log, and a run that never ends is worse than a refused one.
    MAX_TIMELAPSE_FRAMES = 240

    def CaptureTimelapse(
        self,
        Count: int,
        IntervalSeconds: int,
        Label: str,
        AllowRobotMotion: bool,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstance,
    ) -> CaptureTimelapse_Responses:
        """Photograph the deck repeatedly, in a single run.

        The robot cannot hand back a video. A protocol executes *on* the robot,
        and the only channel to this process is the run log, where each frame
        travels base64-encoded — fine for one photograph, impractical for the
        megabytes of a clip. A timelapse is the form this transport supports,
        and for watching a gel set or a plate fill it is the more useful one:
        frames at chosen moments, each a full-resolution still.

        All frames come from one protocol run, so the robot homes once at the
        start rather than once per frame.
        """
        ot = self._require_ot()
        instance.begin_execution()
        instance.progress = 0.02

        if not getattr(self._config, "camera_enabled", True):
            return CaptureTimelapse_Responses(TimelapseResult=json.dumps({
                "saved": False, "frames": 0,
                "detail": "Camera disabled in config.yaml (camera.enabled: false)",
            }))

        if Count < 1 or Count > self.MAX_TIMELAPSE_FRAMES:
            raise ValueError(
                f"Count must be between 1 and {self.MAX_TIMELAPSE_FRAMES}; got {Count}. "
                f"Every frame is carried base64-encoded through the run log."
            )
        interval = max(0, int(IntervalSeconds))

        # Running a protocol homes the robot, exactly as for CaptureImage —
        # and here it is the only available path, since the HTTP camera
        # endpoint takes one picture and cannot be scheduled.
        if not AllowRobotMotion:
            return CaptureTimelapse_Responses(TimelapseResult=json.dumps({
                "saved": False, "frames": 0, "detail": (
                    "A timelapse runs a protocol on the robot, which homes it. "
                    "Refused because AllowRobotMotion is false. Set it true only "
                    "with someone watching the deck."
                ),
            }))

        label = "".join(c if c.isalnum() or c in "-_" else "_" for c in (Label or "timelapse"))
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        device = getattr(self._config, "camera_device", "/dev/video2")
        target_dir = self._images_dir()
        target_dir.mkdir(parents=True, exist_ok=True)

        steps: List[Dict[str, Any]] = []
        for frame in range(Count):
            if frame and interval:
                steps.append({"Command": "Delay", "Seconds": interval})
            steps.append({
                "Command": "TakeSnapshot",
                "Filename": f"{stamp}_{label}_{frame + 1:03d}.jpg",
                "Device": device,
            })

        instance.progress = 0.1
        run_id = self._run_inline(steps, f"CaptureTimelapse {label} x{Count}")

        saved = self._run_async(
            ot.robot.extract_images_from_log(run_id, str(target_dir)), timeout=300
        ) if ot.robot else 0
        instance.progress = 1.0

        return CaptureTimelapse_Responses(TimelapseResult=json.dumps({
            "saved": bool(saved), "frames": saved, "requested": Count,
            "path": str(target_dir), "url": "/api/images", "run_id": run_id,
            "detail": (
                f"{saved} of {Count} frame(s) captured at {interval}s intervals "
                f"and extracted from the run log. The robot was homed once at "
                f"the start of the run."
            ),
        }))

    def _images_dir(self) -> Path:
        """Where captured images go: Results/Images, which the webapp serves.

        They used to land in the Opentrons server's own ./images directory,
        which no route ever exposed — captured and then invisible.
        """
        configured = getattr(self._config, "dir_results", None)
        if configured:
            return Path(os.path.abspath(configured)) / "Images"
        return Path(__file__).resolve().parents[3] / "Results" / "Images"

    async def _capture_over_http(self) -> Optional[bytes]:
        """Try the robot's own camera endpoint. None if it does not have one."""
        ot = self._require_ot()
        if ot.robot is None or not ot.robot.is_connected():
            return None
        import httpx

        url = f"http://{ot.robot.host}:{ot.robot.port}/camera/picture"
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, headers={"Opentrons-Version": "*"})
            if response.status_code == 404:
                return None
            response.raise_for_status()
            content = response.content
            # Some firmware answers with base64 in a JSON envelope.
            if content[:1] == b"{":
                payload = response.json()
                encoded = payload.get("data") or payload.get("image")
                if not encoded:
                    return None
                return base64.b64decode(encoded)
            return content or None
