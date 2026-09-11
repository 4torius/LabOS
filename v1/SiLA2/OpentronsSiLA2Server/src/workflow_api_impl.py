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

* A command is one deliberate action: move this labware, shake this module,
  open this latch. Each validates its own arguments against the loaded deck
  before the robot moves. A whole procedure is a recipe, run by ExecuteRecipe.

Async bridge: the business logic is asyncio, SiLA2 feature methods are
synchronous (called on a thread-pool). A dedicated event-loop thread bridges
the two via _run_async().
"""

import asyncio
import base64
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
    EmergencyStop_Responses,
    ExecuteRecipe_Responses,
    Home_Responses,
    LoadHardwareConfig_Responses,
    MoveLabware_Responses,
    PauseRun_Responses,
    RefillTipRack_Responses,
    ResetTipTracking_Responses,
    ResumeRun_Responses,
    SetCameraEnabled_Responses,
    SetLights_Responses,
    SetModuleLatch_Responses,
    SetModuleTemperature_Responses,
    ShakeModule_Responses,
    ValidateRecipe_Responses,
)

from .server import parse_run_id

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
        """The hardware configuration - the deck - not this server's settings.

        It used to serialise ot.config, which is the ServerConfig dataclass
        read from the server's own config.yaml: host, port, robot IP, timeouts.
        So a client asking "what is on the deck?" was handed the server's
        network settings, under a property whose description says "this is what
        ExecuteRecipe runs against". Two objects, both called a config.
        """
        ot = self._require_ot()
        name = ot.get_current_hardware_config() or ""
        config_json = ""
        if ot.hardware:
            config_json = json.dumps(ot.hardware.get_config(), indent=2)
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

    def get_CameraStatus(self, *, metadata: MetadataDict) -> str:
        """Whether the robot's camera is on, and where to watch it.

        The Flex has a native camera API - a still from /camera/picture and an
        HLS/RTMP live stream - and all three enablement flags ship off. With
        them off every capture comes back 422 "Cannot take photo, camera is
        disabled", which reads like a broken endpoint rather than a setting.
        This says so plainly, and gives the stream URLs when it is on.
        """
        ot = self._require_ot()
        status: Dict[str, Any] = {
            "cameraEnabled": None, "liveStreamEnabled": None,
            "errorRecoveryCameraEnabled": None, "hls": "", "rtmp": "",
        }
        if ot.robot is None or not ot.robot.is_connected:
            status["detail"] = "not connected to the robot"
            return json.dumps(status)
        try:
            status.update(self._run_async(self._camera_state(), timeout=20))
        except Exception as exc:
            status["detail"] = f"could not read the camera state: {exc}"
        return json.dumps(status)

    async def _camera_state(self) -> Dict[str, Any]:
        """The robot's camera enablement plus its live-stream URLs."""
        import httpx

        ot = self._require_ot()
        base = f"http://{ot.robot.host}:{ot.robot.port}"
        headers = {"Opentrons-Version": "*"}
        out: Dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            enablement = await client.get(f"{base}/camera")
            if enablement.is_success:
                out.update(enablement.json())
            stream = await client.get(f"{base}/camera/stream")
            if stream.is_success:
                data = stream.json()
                # The robot returns paths; make them usable as they stand.
                out["liveStreamEnabled"] = data.get("enabled", out.get("liveStreamEnabled"))
                out["hls"] = f"{base}{data.get('hls', '')}" if data.get("hls") else ""
                out["rtmp"] = data.get("rtmp", "")
        return out

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

    def _run_inline(
        self,
        steps: List[Dict[str, Any]],
        description: str,
        requirements: Optional[Dict[str, str]] = None,
    ) -> str:
        """Execute steps as a one-off protocol and return the run id.

        `requirements` names the deck items the steps address. It is not
        optional in practice: the hardware config reaches a generated protocol
        only through Requirements, so a recipe that declares none gets a
        protocol with no load_module and no load_labware beyond the tip racks
        that are auto-injected. The step then refers to a name the protocol
        never defined, and the robot fails with a bare KeyError on that name -
        after homing, because the run has already started by then. Confirmed on
        hardware: SetModuleLatch on a heater-shaker that was correctly declared
        AND physically attached died on KeyError: 'HeaterShaker'.
        """
        ot = self._require_ot()
        recipe = {
            "ProtocolName": description,
            "Description": description,
            # logical name -> physical id in the hardware config; for a
            # single-action command the two are the same name.
            "Requirements": dict(requirements or {}),
            "Steps": steps,
        }
        # cmd_run_protocol returns one string, not the (run_id, result) tuple
        # cmd_execute_recipe_by_name returns - unpacking it into two names
        # raised "too many values to unpack", which is what every command
        # routed through here did instead of moving: MoveLabware,
        # SetModuleTemperature, ShakeModule, SetModuleLatch.
        result = self._run_async(
            ot.cmd_run_protocol(json.dumps(recipe), "json"), timeout=None
        )
        run_id = parse_run_id(result)
        self._raise_unless_succeeded(run_id, result)
        self._publish_state()
        return run_id or ""

    def _move_requirements(self, labware_name: str, new_location: str) -> Dict[str, str]:
        """The deck items a move addresses, so the protocol actually loads them.

        The labware being moved, always. The destination too when it is a
        module rather than a bare slot - a move onto a heater-shaker needs that
        module loaded in the protocol to have somewhere to go.
        """
        requirements = {labware_name: labware_name}
        ot = self._require_ot()
        if ot.hardware and new_location in (ot.hardware.get_modules() or {}):
            requirements[new_location] = new_location
        return requirements

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
                free = sorted(set(_FLEX_SLOTS) - set(occupied))
                # A destination that is neither a real slot nor a module on the
                # deck reached the robot and failed there with "'Z9' is not a
                # valid deck slot" - which cost a home first, because the run
                # was already playing by then.
                if NewLocation not in _FLEX_SLOTS:
                    raise ValueError(
                        f"'{NewLocation}' is not a deck slot or a module on the deck. "
                        f"Free slots: {', '.join(free) or 'none'}"
                        + (f". Modules: {', '.join(sorted(modules))}" if modules else "")
                    )
                if NewLocation in occupied:
                    raise ValueError(
                        f"Slot {NewLocation} already holds '{occupied[NewLocation]}'. "
                        f"Free slots: {', '.join(free) or 'none'}"
                    )

        self._run_inline(
            [{
                "Command": "MoveLabware",
                "LabwareID": LabwareName,
                "NewLocation": NewLocation,
                "UseGripper": UseGripper,
            }],
            f"MoveLabware {LabwareName} -> {NewLocation}",
            requirements=self._move_requirements(LabwareName, NewLocation),
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

    def _require_module(self, module_name: str) -> "tuple[str, str]":
        """The module a command names, as (config name, the robot's module id).

        The id is what POST /commands addresses the module by, so resolving the
        name and finding the attached module are the same lookup.

        Two things have to be true, and they are different things:
          * the active hardware config declares the module - otherwise the
            generated protocol has nothing to address;
          * the robot reports it attached - the config is a description of the
            deck, written by a person, and it can be out of date. Ours was:
            01_SPAAC declared a HeaterShaker at A1 while the robot reported no
            modules at all.
        """
        ot = self._require_ot()
        declared: Dict[str, Any] = {}
        if ot.hardware:
            declared = ot.hardware.get_modules() or {}

        if not module_name:
            raise ValueError(
                "No module named. Modules in the active hardware config: "
                f"{', '.join(sorted(declared)) or 'none'}"
            )

        match = module_name if module_name in declared else None
        if match is None:
            # A module is also addressable by its type, which is what the
            # recipe format calls a ModuleID.
            for name, spec in declared.items():
                if isinstance(spec, dict) and spec.get("Type") == module_name:
                    match = name
                    break
        if match is None:
            raise ValueError(
                f"'{module_name}' is not in the active hardware config "
                f"({ot.hardware.get_current_config_name() if ot.hardware else 'none'}). "
                f"Modules it declares: {', '.join(sorted(declared)) or 'none'}"
            )

        if ot.robot is None:
            return match
        attached = []
        try:
            attached = self._run_async(ot.robot.get_modules(), timeout=15) or []
        except Exception as exc:
            # Not reachable is not the same as not attached; do not block on it.
            logger.warning("Could not ask the robot which modules are attached: %s", exc)
            return match, ""

        spec = declared.get(match) or {}
        wanted = (spec.get("Type") or "").lower()
        for module in attached:
            model = str(module.get("moduleModel", "")).lower()
            if wanted and (wanted in model or model in wanted):
                return match, str(module.get("id") or "")

        present = {str(m.get("moduleModel", "")).lower() for m in attached}
        raise ValueError(
            f"The hardware config declares '{match}' ({spec.get('Type')}) at slot "
            f"{spec.get('Slot', '?')}, but the robot reports "
            f"{'no modules attached' if not attached else 'only: ' + ', '.join(sorted(present))}. "
            f"Nothing was moved. Either plug the module in, or load a hardware "
            f"config that matches the deck."
        )

    async def _module_command(self, command_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Send one protocol-engine command straight to the robot.

        Module control does not belong in a protocol run. A run homes the robot
        before it starts, takes twenty to thirty seconds to upload, analyse and
        play, and - the part that actually broke things - the engine deactivates
        the modules when the run ends. So SetModuleTemperature reported success
        and left the heater off: the target was set and then dropped along with
        the run that set it. Confirmed on hardware.

        POST /commands runs the same command outside any run. Nothing homes, it
        returns in milliseconds, and the module keeps the state it was given.
        The robot itself says so: /modules/{serial} answers 410 "This endpoint
        has been removed. Use POST /commands instead."
        """
        import httpx

        ot = self._require_ot()
        if ot.robot is None or not ot.robot.is_connected:
            raise RuntimeError("Not connected to the robot")
        url = f"http://{ot.robot.host}:{ot.robot.port}/commands"
        async with httpx.AsyncClient(timeout=120.0,
                                     headers={"Opentrons-Version": "*"}) as client:
            response = await client.post(
                url, params={"waitUntilComplete": True},
                json={"data": {"commandType": command_type, "params": params}},
            )
            if not response.is_success:
                raise RuntimeError(
                    f"{command_type} refused by the robot ({response.status_code}): "
                    f"{response.text[:300]}"
                )
            data = response.json().get("data", {})
        error = data.get("error")
        if error or data.get("status") == "failed":
            detail = (error or {}).get("detail") or (error or {}).get("errorType") or "failed"
            raise RuntimeError(f"{command_type} failed on the robot: {detail}")
        return data

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
        ModuleName, module_id = self._require_module(ModuleName)
        instance.progress = 0.2

        if Celsius <= 0:
            self._run_async(
                self._module_command("heaterShaker/deactivateHeater",
                                     {"moduleId": module_id}), timeout=60)
            instance.progress = 1.0
            return SetModuleTemperature_Responses(
                TemperatureResult=f"{ModuleName}: heating deactivated")

        self._run_async(
            self._module_command("heaterShaker/setTargetTemperature",
                                 {"moduleId": module_id, "celsius": Celsius}), timeout=60)
        instance.progress = 0.5
        if WaitForTemperature:
            # The robot blocks until it is there; no client-side polling loop.
            self._run_async(
                self._module_command("heaterShaker/waitForTemperature",
                                     {"moduleId": module_id, "celsius": Celsius}),
                timeout=None)
            instance.progress = 1.0
            return SetModuleTemperature_Responses(
                TemperatureResult=f"{ModuleName}: reached {Celsius} C")

        instance.progress = 1.0
        return SetModuleTemperature_Responses(
            TemperatureResult=f"{ModuleName}: heating to {Celsius} C")

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
        ModuleName, module_id = self._require_module(ModuleName)
        instance.progress = 0.2

        if DurationSeconds <= 0:
            self._run_async(
                self._module_command("heaterShaker/deactivateShaker",
                                     {"moduleId": module_id}), timeout=60)
            instance.progress = 1.0
            return ShakeModule_Responses(ShakeResult=f"{ModuleName}: shaking stopped")

        # A closed latch is a precondition of shaking, not a side effect: the
        # robot raises CannotPerformModuleAction otherwise. Closing it clamps
        # whatever is on the module, which is what you want before spinning it.
        self._run_async(
            self._module_command("heaterShaker/closeLabwareLatch",
                                 {"moduleId": module_id}), timeout=60)
        instance.progress = 0.35
        self._run_async(
            self._module_command("heaterShaker/setAndWaitForShakeSpeed",
                                 {"moduleId": module_id, "rpm": RPM}), timeout=120)
        instance.progress = 0.5

        self._run_async(asyncio.sleep(DurationSeconds), timeout=DurationSeconds + 30)
        instance.progress = 0.9

        self._run_async(
            self._module_command("heaterShaker/deactivateShaker",
                                 {"moduleId": module_id}), timeout=60)
        instance.progress = 1.0
        return ShakeModule_Responses(
            ShakeResult=f"{ModuleName}: shook at {RPM} rpm for {DurationSeconds}s")

    def SetModuleLatch(
        self, ModuleName: str, Open: bool, *, metadata: MetadataDict
    ) -> SetModuleLatch_Responses:
        ModuleName, module_id = self._require_module(ModuleName)
        self._run_async(
            self._module_command(
                "heaterShaker/openLabwareLatch" if Open
                else "heaterShaker/closeLabwareLatch",
                {"moduleId": module_id}), timeout=60)
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

    def _borrow_camera(self) -> Optional[Dict[str, Any]]:
        """Switch the camera on for the duration of a capture.

        The Flex ships with its camera disabled, so a capture command that
        merely asks for a frame gets 422 "Cannot take photo, camera is
        disabled" and looks broken. Turning it on is part of taking a picture.

        Returns the state to restore, or None if nothing needed changing.
        Always paired with _return_camera in a finally.
        """
        try:
            before = self._run_async(self._camera_state(), timeout=20)
        except Exception as exc:
            logger.warning("Could not read the camera state: %s", exc)
            return None
        if before.get("cameraEnabled"):
            return None
        self._run_async(
            self._set_camera(True, bool(before.get("liveStreamEnabled"))), timeout=30)
        return before

    def _return_camera(self, before: Optional[Dict[str, Any]]) -> None:
        """Put the camera back how it was found. Never raises."""
        if not before:
            return
        try:
            self._run_async(
                self._set_camera(bool(before.get("cameraEnabled")),
                                 bool(before.get("liveStreamEnabled"))), timeout=30)
        except Exception as exc:
            logger.warning("Could not switch the camera back off: %s", exc)

    def SetCameraEnabled(
        self, Camera: bool, LiveStream: bool, *, metadata: MetadataDict
    ) -> SetCameraEnabled_Responses:
        """Turn the robot's camera and live stream on or off.

        Nothing moves. This is a setting on the robot, not on this server: it
        persists across restarts of both, and it is the reason CaptureImage
        used to fail with a bare 422.
        """
        ot = self._require_ot()
        if ot.robot is None or not ot.robot.is_connected:
            raise RuntimeError("Not connected to the robot")
        result = self._run_async(self._set_camera(Camera, LiveStream), timeout=30)
        return SetCameraEnabled_Responses(CameraResult=json.dumps(result))

    async def _set_camera(self, camera: bool, live_stream: bool) -> Dict[str, Any]:
        import httpx

        ot = self._require_ot()
        base = f"http://{ot.robot.host}:{ot.robot.port}"
        payload = {"data": {
            "cameraEnabled": camera,
            "liveStreamEnabled": live_stream,
            # Left alone deliberately: whether the robot films its own error
            # recovery is a separate decision from whether we can take a photo.
            "errorRecoveryCameraEnabled": None,
        }}
        async with httpx.AsyncClient(timeout=30.0,
                                     headers={"Opentrons-Version": "*"}) as client:
            response = await client.post(f"{base}/camera", json=payload)
            if not response.is_success:
                raise RuntimeError(
                    f"the robot refused ({response.status_code}): {response.text[:300]}"
                )
        return await self._camera_state()

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
            restore = self._borrow_camera()
            try:
                data = self._run_async(self._capture_over_http(), timeout=30)
                if data:
                    target.write_bytes(data)
                    instance.progress = 1.0
                    return CaptureImage_Responses(CaptureResult=json.dumps({
                        "saved": True, "path": str(target), "url": f"/api/images/{filename}",
                        "method": "http",
                        "detail": ("Captured via the robot's camera endpoint; nothing moved"
                                   + (". The camera was switched on for this and switched back off"
                                      if restore else "")),
                    }))
                http_detail = "robot has no HTTP camera endpoint"
            except Exception as exc:
                http_detail = f"HTTP capture failed: {exc}"
            finally:
                self._return_camera(restore)
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
        # RobotClient.is_connected is a property; the server object's
        # is_connected of the same name is a method. Calling this one
        # raised "'bool' object is not callable", which the capture
        # reported as "HTTP capture failed" - so every CaptureImage fell
        # through to the protocol path, which homes the robot, and with
        # AllowRobotMotion false simply refused. The camera looked
        # unreachable when it had never been asked.
        if ot.robot is None or not ot.robot.is_connected:
            return None
        import httpx

        url = f"http://{ot.robot.host}:{ot.robot.port}/camera/picture"
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, headers={"Opentrons-Version": "*"})
            if response.status_code == 404:
                return None
            if response.status_code == 422:
                # The endpoint exists and the request was fine; the robot is
                # refusing. Its own message is the useful part, and the usual
                # one is "Cannot take photo, camera is disabled." - a per-robot
                # setting that ships off, and which no amount of retrying here
                # will change.
                try:
                    message = response.json().get("message", response.text)
                except Exception:
                    message = response.text
                raise RuntimeError(
                    f"the robot refused: {message} "
                    f"(see the CameraStatus property; SetCameraEnabled turns it on)"
                )
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
