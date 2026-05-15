"""
MobileRobot SiLA2 Feature Implementation
=========================================

Implements the MobileRobotBase interface generated from MobileRobot.sila.xml.
Wraps RosBridge for all hardware interactions; degrades gracefully when ROS
is unavailable (stub mode).

Observable properties (RobotStatus, CurrentPosition, BatteryLevel) are
updated every 2 seconds by a background polling thread, and also on each
client subscription via *_on_subscription() hooks.

Observable commands (Navigate, MoveArm, MoveLabware) block the sila2
worker thread while waiting for the hardware, sending intermediate responses
at each step.
"""

import json
import logging
import threading
from datetime import timedelta
from typing import Optional

from sila2.server import MetadataDict, ObservableCommandInstanceWithIntermediateResponses

from generated.mobilerobot import (
    MobileRobotBase,
    MobileRobotFeature,
)
from generated.mobilerobot.mobilerobot_errors import (
    ArmMovementFailed,
    GripperFailed,
    NavigationFailed,
    PositionNotFound,
    RobotBusy,
    StationNotFound,
)
from generated.mobilerobot.mobilerobot_types import (
    ControlGripper_Responses,
    EmergencyStop_Responses,
    GetStations_Responses,
    GetStatus_Responses,
    MoveArm_IntermediateResponses,
    MoveArm_Responses,
    MoveLabware_IntermediateResponses,
    MoveLabware_Responses,
    Navigate_IntermediateResponses,
    Navigate_Responses,
)
from .ros_bridge import RosBridge

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 2.0


class MobileRobotImpl(MobileRobotBase):
    """
    SiLA2 standard library implementation of the MobileRobot feature.

    Uses RosBridge for all hardware calls (ROS1 services + topic subscriptions).
    Falls back to stub mode when rospy is not available.
    """

    def __init__(self, parent_server, config: dict) -> None:
        super().__init__(parent_server)
        self._config = config
        self._stations = {s["name"]: s for s in config.get("stations", [])}
        self._arm_positions = config.get("arm", {}).get("named_positions", {})
        self._gripper_profiles = config.get("gripper", {}).get("labware_profiles", {})
        self._ros = RosBridge(config)
        self._busy_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="mobile-prop-poll"
        )

        self.Navigate_default_lifetime_of_execution = timedelta(minutes=10)
        self.MoveArm_default_lifetime_of_execution = timedelta(minutes=5)
        self.MoveLabware_default_lifetime_of_execution = timedelta(minutes=30)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        super().start()
        status = "disconnected" if not self._ros.ros_available else "idle"
        self.update_RobotStatus(status)
        self.update_CurrentPosition(json.dumps(self._ros.current_position))
        self.update_BatteryLevel(self._ros.battery_level)
        self._poll_thread.start()
        logger.info(
            "MobileRobotImpl started — ROS: %s, stations: %s",
            "connected" if self._ros.ros_available else "stub",
            list(self._stations.keys()),
        )

    def stop(self) -> None:
        super().stop()
        self._stop_event.set()

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(timeout=_POLL_INTERVAL):
            try:
                status = self._ros.robot_status
                if not self._ros.ros_available:
                    status = "disconnected"
                self.update_RobotStatus(status)
                self.update_CurrentPosition(json.dumps(self._ros.current_position))
                self.update_BatteryLevel(self._ros.battery_level)
            except Exception as exc:
                logger.debug("Property poll error: %s", exc)

    # ── Observable property subscription hooks ────────────────────────────────

    def RobotStatus_on_subscription(self, *, metadata: MetadataDict):
        status = self._ros.robot_status if self._ros.ros_available else "disconnected"
        self.update_RobotStatus(status)

    def CurrentPosition_on_subscription(self, *, metadata: MetadataDict):
        self.update_CurrentPosition(json.dumps(self._ros.current_position))

    def BatteryLevel_on_subscription(self, *, metadata: MetadataDict):
        self.update_BatteryLevel(self._ros.battery_level)

    # ── Busy guard ────────────────────────────────────────────────────────────

    def _acquire_busy(self) -> None:
        if not self._busy_lock.acquire(blocking=False):
            raise RobotBusy()

    def _release_busy(self) -> None:
        try:
            self._busy_lock.release()
        except RuntimeError:
            pass

    # ── Commands ──────────────────────────────────────────────────────────────

    def Navigate(
        self,
        StationName: str,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[Navigate_IntermediateResponses],
    ) -> Navigate_Responses:
        station = self._stations.get(StationName)
        if station is None:
            raise StationNotFound(f"Unknown station: {StationName!r}")

        self._acquire_busy()
        try:
            instance.begin_execution()
            instance.send_intermediate_response(
                Navigate_IntermediateResponses(NavigationStatus="navigating")
            )
            ok, msg = self._ros.navigate_to(station)
        finally:
            self._release_busy()

        if not ok:
            instance.send_intermediate_response(
                Navigate_IntermediateResponses(NavigationStatus="failed")
            )
            raise NavigationFailed(msg)

        instance.send_intermediate_response(
            Navigate_IntermediateResponses(NavigationStatus="arrived")
        )
        return Navigate_Responses(NavigationResult=f"Arrived at {StationName}: {msg}")

    def MoveArm(
        self,
        PositionName: str,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[MoveArm_IntermediateResponses],
    ) -> MoveArm_Responses:
        if PositionName not in self._arm_positions:
            raise PositionNotFound(f"Unknown arm position: {PositionName!r}")

        self._acquire_busy()
        try:
            instance.begin_execution()
            instance.send_intermediate_response(
                MoveArm_IntermediateResponses(ArmStatus="moving")
            )
            ok, msg = self._ros.arm_move_to(PositionName)
        finally:
            self._release_busy()

        if not ok:
            instance.send_intermediate_response(
                MoveArm_IntermediateResponses(ArmStatus="failed")
            )
            raise ArmMovementFailed(msg)

        instance.send_intermediate_response(
            MoveArm_IntermediateResponses(ArmStatus="reached")
        )
        return MoveArm_Responses(ArmResult=f"Arm at {PositionName}: {msg}")

    def ControlGripper(
        self, Action: str, LabwareType: str, *, metadata: MetadataDict
    ) -> ControlGripper_Responses:
        profile = self._gripper_profiles.get(LabwareType) if LabwareType else None
        ok, msg = self._ros.gripper_control(Action, profile)
        if not ok:
            raise GripperFailed(msg)
        return ControlGripper_Responses(GripperResult=msg)

    def MoveLabware(
        self,
        SourceStation: str,
        DestinationStation: str,
        LabwareType: str,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[MoveLabware_IntermediateResponses],
    ) -> MoveLabware_Responses:
        if SourceStation not in self._stations:
            raise StationNotFound(f"Source station not found: {SourceStation!r}")
        if DestinationStation not in self._stations:
            raise StationNotFound(f"Destination station not found: {DestinationStation!r}")

        profile = self._gripper_profiles.get(LabwareType)

        def _step(status: str) -> None:
            instance.send_intermediate_response(
                MoveLabware_IntermediateResponses(TransportStatus=status)
            )

        self._acquire_busy()
        try:
            instance.begin_execution()

            _step("navigating_to_source")
            ok, msg = self._ros.navigate_to(self._stations[SourceStation])
            if not ok:
                raise NavigationFailed(f"Cannot reach {SourceStation}: {msg}")

            _step("picking_up")
            ok, msg = self._ros.arm_move_to("Pickup")
            if not ok:
                raise ArmMovementFailed(f"Arm pickup failed: {msg}")
            ok, msg = self._ros.gripper_control("close", profile)
            if not ok:
                raise GripperFailed(f"Gripper close failed: {msg}")
            ok, msg = self._ros.arm_move_to("Travel")
            if not ok:
                raise ArmMovementFailed(f"Arm travel failed: {msg}")

            _step("navigating_to_destination")
            ok, msg = self._ros.navigate_to(self._stations[DestinationStation])
            if not ok:
                raise NavigationFailed(f"Cannot reach {DestinationStation}: {msg}")

            _step("placing")
            ok, msg = self._ros.arm_move_to("Place")
            if not ok:
                raise ArmMovementFailed(f"Arm place failed: {msg}")
            ok, msg = self._ros.gripper_control("open", None)
            if not ok:
                raise GripperFailed(f"Gripper open failed: {msg}")
            ok, msg = self._ros.arm_move_to("Travel")
            if not ok:
                raise ArmMovementFailed(f"Arm retract failed: {msg}")

        finally:
            self._release_busy()

        _step("completed")
        return MoveLabware_Responses(
            TransportResult=f"Moved {LabwareType} from {SourceStation} to {DestinationStation}"
        )

    def EmergencyStop(self, *, metadata: MetadataDict) -> EmergencyStop_Responses:
        ok, msg = self._ros.emergency_stop()
        return EmergencyStop_Responses(StopResult=msg)

    def GetStatus(self, *, metadata: MetadataDict) -> GetStatus_Responses:
        payload = {
            "state": self._ros.robot_status if self._ros.ros_available else "disconnected",
            "position": self._ros.current_position,
            "battery_level": self._ros.battery_level,
            "ros_connected": self._ros.ros_available,
            "stations": list(self._stations.keys()),
        }
        return GetStatus_Responses(StatusJson=json.dumps(payload))

    def GetStations(self, *, metadata: MetadataDict) -> GetStations_Responses:
        stations = [
            {
                "name": s["name"],
                "type": s.get("type", ""),
                "x": s.get("x", 0.0),
                "y": s.get("y", 0.0),
                "height": s.get("height", 0.0),
                "description": s.get("description", ""),
            }
            for s in self._config.get("stations", [])
        ]
        return GetStations_Responses(StationsJson=json.dumps(stations))
