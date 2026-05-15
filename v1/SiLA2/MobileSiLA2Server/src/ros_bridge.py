"""
ROS1 bridge for the RB Kairos + ABB GoFa 15000 mobile manipulator.

Wraps rospy service calls and topic subscriptions. Falls back to stub mode
when ROS is unavailable (Windows dev machines, CI, no rosmaster running).

In stub mode all operations succeed immediately with simulated time delays
so the SiLA2 server can be tested without physical hardware.

ROS service types used (from gofa_go package):
- navigate_to / arm_move_to / gripper_control / emergency_stop use std_srvs/Trigger
  as a stand-in; swap to the correct custom message types when integrating.
"""

import json
import logging
import os
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class RosBridge:
    """Thread-safe ROS1 interface for the mobile manipulator."""

    def __init__(self, config: dict) -> None:
        self._config = config
        self._ros_cfg = config.get("ros", {})
        self._ns = self._ros_cfg.get("namespace", "")
        self._services = self._ros_cfg.get("services", {})
        self._topics = self._ros_cfg.get("topics", {})

        self._lock = threading.Lock()
        self._status: str = "idle"
        self._position: Dict[str, float] = {"x": 0.0, "y": 0.0, "theta": 0.0}
        self._battery: float = 100.0
        self._ros_available: bool = False
        self._rospy = None

        self._try_connect_ros()

    # ── Startup ──────────────────────────────────────────────────────────────

    def _try_connect_ros(self) -> None:
        master_uri = self._ros_cfg.get("master_uri", "http://localhost:11311")
        os.environ.setdefault("ROS_MASTER_URI", master_uri)
        try:
            import rospy  # type: ignore[import]
            rospy.init_node("sila2_mobile_server", anonymous=True, disable_signals=True)
            self._rospy = rospy
            self._ros_available = True
            self._setup_subscriptions()
            logger.info("ROS connected — namespace: %s, master: %s", self._ns or "/", master_uri)
        except Exception as exc:
            logger.warning("ROS unavailable (%s) — stub mode active", exc)

    def _setup_subscriptions(self) -> None:
        rospy = self._rospy
        ns = self._ns
        topics = self._topics

        try:
            from sensor_msgs.msg import BatteryState  # type: ignore[import]
            rospy.Subscriber(
                ns + topics.get("battery_state", "/battery_state"),
                BatteryState,
                self._cb_battery,
            )
        except Exception as exc:
            logger.debug("Battery subscription skipped: %s", exc)

        try:
            from geometry_msgs.msg import Pose2D  # type: ignore[import]
            rospy.Subscriber(
                ns + topics.get("base_pose", "/base_pose"),
                Pose2D,
                self._cb_pose,
            )
        except Exception as exc:
            logger.debug("Pose subscription skipped: %s", exc)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _cb_battery(self, msg) -> None:
        pct = float(msg.percentage * 100) if hasattr(msg, "percentage") else 100.0
        with self._lock:
            self._battery = pct

    def _cb_pose(self, msg) -> None:
        with self._lock:
            self._position = {
                "x": float(getattr(msg, "x", 0.0)),
                "y": float(getattr(msg, "y", 0.0)),
                "theta": float(getattr(msg, "theta", 0.0)),
            }

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def ros_available(self) -> bool:
        return self._ros_available

    @property
    def battery_level(self) -> float:
        with self._lock:
            return self._battery

    @property
    def current_position(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._position)

    @property
    def robot_status(self) -> str:
        with self._lock:
            return self._status

    def _set_status(self, status: str) -> None:
        with self._lock:
            self._status = status

    # ── ROS service helper ────────────────────────────────────────────────────

    def _call_trigger(self, service_key: str, default_path: str, timeout: float = 10.0) -> Tuple[bool, str]:
        """Call a std_srvs/Trigger ROS service. Returns (success, message)."""
        svc_name = self._ns + self._services.get(service_key, default_path)
        try:
            from std_srvs.srv import Trigger, TriggerRequest  # type: ignore[import]
            self._rospy.wait_for_service(svc_name, timeout=timeout)
            svc = self._rospy.ServiceProxy(svc_name, Trigger)
            resp = svc(TriggerRequest())
            return bool(getattr(resp, "success", True)), str(getattr(resp, "message", "OK"))
        except Exception as exc:
            return False, str(exc)

    # ── Navigation ────────────────────────────────────────────────────────────

    def navigate_to(self, station: dict) -> Tuple[bool, str]:
        """Navigate to a station config dict. Returns (success, message)."""
        name = station["name"]
        self._set_status("navigating")

        if not self._ros_available:
            time.sleep(1.5)
            with self._lock:
                self._position = {"x": float(station.get("x", 0)), "y": float(station.get("y", 0)), "theta": 0.0}
            self._set_status("idle")
            logger.info("[stub] Navigated to %s", name)
            return True, f"Arrived at {name}"

        ok, msg = self._call_trigger("navigate_to", "/navigate_to", timeout=30.0)
        if ok:
            with self._lock:
                self._position = {"x": float(station.get("x", 0)), "y": float(station.get("y", 0)), "theta": 0.0}
        self._set_status("idle" if ok else "error")
        return ok, msg

    # ── Arm ──────────────────────────────────────────────────────────────────

    def arm_move_to(self, position_name: str) -> Tuple[bool, str]:
        """Move arm to a named position. Returns (success, message)."""
        self._set_status("moving_arm")

        if not self._ros_available:
            time.sleep(0.8)
            self._set_status("idle")
            logger.info("[stub] Arm moved to %s", position_name)
            return True, f"Arm at {position_name}"

        ok, msg = self._call_trigger("arm_move_to", "/arm/move_to", timeout=30.0)
        self._set_status("idle" if ok else "error")
        return ok, msg

    # ── Gripper ──────────────────────────────────────────────────────────────

    def gripper_control(
        self, action: str, profile: Optional[dict] = None
    ) -> Tuple[bool, str]:
        """Open or close the gripper. Returns (success, message)."""
        if not self._ros_available:
            time.sleep(0.4)
            logger.info("[stub] Gripper %s", action)
            return True, f"Gripper {action}ed"

        ok, msg = self._call_trigger("gripper_control", "/gripper/control", timeout=10.0)
        return ok, msg

    # ── Emergency Stop ────────────────────────────────────────────────────────

    def emergency_stop(self) -> Tuple[bool, str]:
        """Trigger immediate emergency stop. Returns (success, message)."""
        self._set_status("error")

        if not self._ros_available:
            logger.warning("[stub] EMERGENCY STOP triggered")
            return True, "Emergency stop executed (stub mode)"

        ok, msg = self._call_trigger("emergency_stop", "/emergency_stop", timeout=5.0)
        return ok, msg
