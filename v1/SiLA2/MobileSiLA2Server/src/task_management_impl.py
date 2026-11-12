"""
TaskManagement SiLA2 Feature Implementation
============================================

Implements TaskManagementBase (generated from TaskManagement.sila.xml).
Bridges SiLA2 commands to ROSTaskBridge (ROS1 action/service calls).

Async bridge: ROSTaskBridge uses asyncio; sila2 dispatches synchronously.
A dedicated event loop thread bridges the two worlds via _run_async().
"""

import asyncio
import json
import logging
import os
import queue
import signal
import threading
import time
from datetime import timedelta
from typing import List, Optional

from sila2.server import MetadataDict, ObservableCommandInstanceWithIntermediateResponses

from generated.taskmanagement import (
    ActivateAllHardware_Responses,
    CancelTask_Responses,
    ConnectToRobot_Responses,
    DeactivateAllHardware_Responses,
    ExecuteTask_IntermediateResponses,
    ExecuteTask_Responses,
    GetTaskDetails_Responses,
    RefreshTasks_Responses,
    SetHardwareActivation_Responses,
    SetHardwareActivationByRole_Responses,
    TaskInfo,
    TaskManagementBase,
)
from generated.taskmanagement import HardwareInfo as SilaHardwareInfo
from generated.taskmanagement.taskmanagement_errors import (
    HardwareNotFound,
    RobotNotConnected,
    TaskExecutionFailed,
    TaskNotFound,
)

# Avoid name clash with ros_task_bridge.TaskInfo/HardwareInfo (dataclasses)
from ros_task_bridge import MobileCommand, ROSTaskBridge, TaskFeedback

logger = logging.getLogger(__name__)

# Maps a HardwareRole (SiLA2 Constrained/Set value) to the "Brand" field
# reported by getSetupInfo for this robot's fixed hardware configuration.
# Resolved live rather than hardcoding hardware IDs, so a hardware ID
# regenerated on reconfiguration doesn't require an FDL/deployment update.
_ROLE_TO_BRAND = {
    "Arm": "ABB",
    "Gripper": "OnRobot",
    "Camera": "Intel",
    "MobileBase": "Robotnik",
}


class TaskManagementImpl(TaskManagementBase):
    """
    SiLA2 standard library implementation of the TaskManagement feature.

    Uses a dedicated asyncio event loop (running in a daemon thread) to call
    the async ROSTaskBridge methods from the synchronous sila2 dispatch thread.
    Observable properties are pushed via update_*() helpers whenever state changes.
    """

    # How often the reconnect loop checks for ROS (seconds)
    _RECONNECT_INTERVAL = 15.0

    def __init__(
        self,
        parent_server,
        bridge: ROSTaskBridge,
        task_timeout: float = 300.0,
        reconnect_interval: float = _RECONNECT_INTERVAL,
    ) -> None:
        super().__init__(parent_server)
        self._bridge = bridge
        self._task_timeout = task_timeout
        self._reconnect_interval = reconnect_interval

        # Dedicated asyncio loop for bridge coroutines
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="mobile-async-loop",
        )
        self._loop_thread.start()

        # Reconnect loop control
        self._reconnect_stop = threading.Event()
        self._reconnect_thread: Optional[threading.Thread] = None

        self.ExecuteTask_default_lifetime_of_execution = timedelta(
            seconds=task_timeout + 60
        )

    # ── Async bridge ─────────────────────────────────────────────────────────

    def _run_async(self, coro, timeout: float = 60.0):
        """Submit an asyncio coroutine to the dedicated loop and block for the result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ── Reconnect loop ────────────────────────────────────────────────────────

    def _reconnect_loop(self) -> None:
        """Background thread: retry ROS connection every _reconnect_interval seconds."""
        logger.info(
            f"Reconnect loop started (interval={self._reconnect_interval}s) — "
            "waiting for ROS master..."
        )
        while not self._reconnect_stop.wait(timeout=self._reconnect_interval):
            if self._bridge.is_connected:
                if not self._bridge.master_restarted():
                    # Still the same roscore -- nothing to do.
                    continue
                # roscore was restarted underneath us. rospy.init_node() can't be
                # called a second time in the same process, so an in-process
                # reconnect isn't possible -- exit and let Docker's `restart: always`
                # bring the server back up with a clean rospy session against the
                # current roscore.
                logger.error(
                    "Reconnect loop: ROS master was restarted -- rospy session is "
                    "stale and cannot be recovered in-process; restarting server"
                )
                os.kill(os.getpid(), signal.SIGTERM)
                return
            logger.info("Reconnect loop: attempting ROS connection...")
            self.update_RobotConnectionStatus("Connecting")
            try:
                connected = self._run_async(self._bridge.start(), timeout=35.0)
                if connected:
                    self.update_RobotConnectionStatus("Connected")
                    logger.info("Reconnect loop: ROS bridge connected successfully")
                    try:
                        tasks = self._run_async(
                            self._bridge.get_available_tasks(refresh=True), timeout=20.0
                        )
                        logger.info(
                            f"Reconnect loop: loaded {len(tasks)} tasks from ROS"
                        )
                    except Exception as exc:
                        logger.warning(f"Reconnect loop: task discovery failed: {exc}")
                else:
                    self.update_RobotConnectionStatus("Disconnected")
                    logger.info(
                        "Reconnect loop: ROS not ready yet — "
                        f"retrying in {self._reconnect_interval}s"
                    )
            except Exception as exc:
                self.update_RobotConnectionStatus("Disconnected")
                logger.debug(f"Reconnect loop: attempt failed ({exc})")
        logger.info("Reconnect loop stopped.")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        super().start()

        # Initialise all observable properties so subscribers always get a value
        self.update_CurrentTaskId("")
        self.update_CurrentSubtask("")
        self.update_TaskStatus("Idle")
        self.update_RobotConnectionStatus("Connecting")

        connected = False
        try:
            connected = self._run_async(self._bridge.start(), timeout=30.0)
            if connected:
                self.update_RobotConnectionStatus("Connected")
                logger.info("ROS bridge connected at startup")
                try:
                    tasks = self._run_async(
                        self._bridge.get_available_tasks(refresh=True), timeout=20.0
                    )
                    logger.info(f"Loaded {len(tasks)} tasks from ROS")
                    for t in tasks:
                        logger.info(f"  [{t.root_id}] {t.root_name}")
                except Exception as exc:
                    logger.warning(f"Task discovery failed at startup: {exc}")
            else:
                self.update_RobotConnectionStatus("Disconnected")
                logger.warning("ROS bridge not connected at startup — will retry automatically")
        except Exception as exc:
            self.update_RobotConnectionStatus("Disconnected")
            logger.warning(f"ROS bridge start error: {exc} — will retry automatically")

        # Always start the reconnect loop; it will keep retrying until ROS is up,
        # and will also recover from future ROS master restarts.
        self._reconnect_stop.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop,
            daemon=True,
            name="mobile-reconnect",
        )
        self._reconnect_thread.start()

    def stop(self) -> None:
        # Stop the reconnect loop first so it doesn't race with bridge.stop()
        self._reconnect_stop.set()
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=self._reconnect_interval + 2)

        super().stop()
        try:
            self._run_async(self._bridge.stop(), timeout=10.0)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    # ── Observable property subscription callbacks ────────────────────────────

    def CurrentTaskId_on_subscription(self, *, metadata: MetadataDict):
        try:
            self.update_CurrentTaskId(self.current_CurrentTaskId)
        except AttributeError:
            self.update_CurrentTaskId("")

    def CurrentSubtask_on_subscription(self, *, metadata: MetadataDict):
        try:
            self.update_CurrentSubtask(self.current_CurrentSubtask)
        except AttributeError:
            self.update_CurrentSubtask("")

    def TaskStatus_on_subscription(self, *, metadata: MetadataDict):
        try:
            self.update_TaskStatus(self.current_TaskStatus)
        except AttributeError:
            self.update_TaskStatus("Idle")

    def RobotConnectionStatus_on_subscription(self, *, metadata: MetadataDict):
        status = "Connected" if self._bridge.is_connected else "Disconnected"
        self.update_RobotConnectionStatus(status)

    # ── Non-observable property ───────────────────────────────────────────────

    def get_AvailableTasks(self, *, metadata: MetadataDict) -> List[TaskInfo]:
        return self.AvailableTasks(metadata=metadata)

    def AvailableTasks(self, *, metadata: MetadataDict) -> List[TaskInfo]:
        tasks = self._run_async(
            self._bridge.get_available_tasks(refresh=False), timeout=10.0
        )
        return [
            TaskInfo(
                TaskId=t.root_id,
                TaskName=t.root_name,
                SubtaskCount=len(t.subtasks),
            )
            for t in tasks
        ]

    def get_AvailableHardware(self, *, metadata: MetadataDict) -> List[SilaHardwareInfo]:
        return self.AvailableHardware(metadata=metadata)

    def AvailableHardware(self, *, metadata: MetadataDict) -> List[SilaHardwareInfo]:
        hardware = self._run_async(self._bridge.get_hardware_list(), timeout=20.0)
        return [
            SilaHardwareInfo(
                HardwareId=hw.hardware_id,
                HardwareName=hw.name,
                Brand=hw.brand,
                Model=hw.model,
                Activated=hw.activated,
            )
            for hw in hardware
        ]

    # ── Commands ──────────────────────────────────────────────────────────────

    def RefreshTasks(self, *, metadata: MetadataDict) -> RefreshTasks_Responses:
        if not self._bridge.is_connected:
            raise RobotNotConnected()
        tasks = self._run_async(
            self._bridge.get_available_tasks(refresh=True), timeout=30.0
        )
        return RefreshTasks_Responses(TaskCount=len(tasks))

    def ExecuteTask(
        self,
        TaskId: str,
        ExecutionMode: str,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[
            ExecuteTask_IntermediateResponses
        ],
    ) -> ExecuteTask_Responses:
        instance.begin_execution()

        task = self._bridge.get_task_by_id(TaskId)
        if not task:
            raise TaskNotFound(f"Task '{TaskId}' not found")
        if not self._bridge.is_connected:
            raise RobotNotConnected()

        command_map = {
            "Normal": MobileCommand.PLAY,
            "StepByStep": MobileCommand.STEP,
            "DryRun": MobileCommand.DRY_RUN,
        }
        ros_command = int(command_map.get(ExecutionMode, MobileCommand.PLAY))

        self.update_CurrentTaskId(TaskId)
        self.update_TaskStatus("Executing")

        # Thread-safe queue for feedback from the async loop
        fb_queue: queue.Queue = queue.Queue()
        done_event = threading.Event()
        exec_result: list = [None]
        exec_error: list = [None]

        def on_progress(fb: TaskFeedback) -> None:
            fb_queue.put_nowait(fb)

        async def _run() -> None:
            try:
                result = await self._bridge.execute_task(
                    task_id=TaskId,
                    command=ros_command,
                    timeout=self._task_timeout,
                    progress_callback=on_progress,
                )
                exec_result[0] = result
            except Exception as exc:
                exec_error[0] = exc
            finally:
                done_event.set()

        asyncio.run_coroutine_threadsafe(_run(), self._loop)

        # Poll for feedback until execution completes
        deadline = time.monotonic() + self._task_timeout + 30.0
        while not done_event.is_set() and time.monotonic() < deadline:
            # Drain feedback queue
            while True:
                try:
                    fb = fb_queue.get_nowait()
                    self.update_CurrentSubtask(fb.current_subtask)
                    instance.send_intermediate_response(
                        ExecuteTask_IntermediateResponses(
                            CurrentSubtask=fb.current_subtask,
                            Progress=float(fb.progress) / 100.0,
                            Status=fb.status,
                        )
                    )
                except queue.Empty:
                    break
            time.sleep(0.2)

        # Drain any remaining feedback
        while True:
            try:
                fb = fb_queue.get_nowait()
                self.update_CurrentSubtask(fb.current_subtask)
                instance.send_intermediate_response(
                    ExecuteTask_IntermediateResponses(
                        CurrentSubtask=fb.current_subtask,
                        Progress=float(fb.progress) / 100.0,
                        Status=fb.status,
                    )
                )
            except queue.Empty:
                break

        # Reset transient state
        self.update_CurrentTaskId("")
        self.update_CurrentSubtask("")

        if exec_error[0]:
            self.update_TaskStatus("Failed")
            raise TaskExecutionFailed(str(exec_error[0]))

        result = exec_result[0]
        if result is None:
            self.update_TaskStatus("Failed")
            raise TaskExecutionFailed("Execution timed out")

        self.update_TaskStatus("Completed" if result.success else "Failed")

        if not result.success:
            raise TaskExecutionFailed(result.message)

        return ExecuteTask_Responses(
            Success=result.success,
            Message=result.message,
            ExecutionTime=float(result.execution_time),
        )

    def CancelTask(self, *, metadata: MetadataDict) -> CancelTask_Responses:
        cancelled = self._run_async(self._bridge.cancel_task(), timeout=15.0)
        if cancelled:
            self.update_TaskStatus("Cancelled")
            self.update_CurrentTaskId("")
            self.update_CurrentSubtask("")
        return CancelTask_Responses(Cancelled=cancelled)

    def GetTaskDetails(self, TaskId: str, *, metadata: MetadataDict) -> GetTaskDetails_Responses:
        task = self._bridge.get_task_by_id(TaskId)
        if not task:
            raise TaskNotFound(f"Task '{TaskId}' not found")

        subtasks_json = json.dumps(
            [
                {
                    "id": st.subtask_id,
                    "name": st.subtask_name,
                    "description": st.description,
                }
                for st in task.subtasks
            ]
        )
        return GetTaskDetails_Responses(
            TaskId=task.root_id,
            TaskName=task.root_name,
            Subtasks=subtasks_json,
        )

    def ConnectToRobot(self, RosMasterUri: str, *, metadata: MetadataDict) -> ConnectToRobot_Responses:
        uri = RosMasterUri.strip() or self._bridge.ros_master_uri

        # Pause the reconnect loop while we do a manual connect to avoid races.
        self._reconnect_stop.set()
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=self._reconnect_interval + 2)

        self.update_RobotConnectionStatus("Connecting")
        connected = False
        try:
            if self._bridge.is_connected:
                self._run_async(self._bridge.stop(), timeout=10.0)
            self._bridge.ros_master_uri = uri
            connected = self._run_async(self._bridge.start(), timeout=30.0)
            if connected:
                self.update_RobotConnectionStatus("Connected")
                self._run_async(
                    self._bridge.get_available_tasks(refresh=True), timeout=20.0
                )
            else:
                self.update_RobotConnectionStatus("Disconnected")
        except Exception as exc:
            self.update_RobotConnectionStatus("Error")
            # Restart reconnect loop so it keeps retrying after manual failure
            self._reconnect_stop.clear()
            self._reconnect_thread = threading.Thread(
                target=self._reconnect_loop, daemon=True, name="mobile-reconnect"
            )
            self._reconnect_thread.start()
            return ConnectToRobot_Responses(Connected=False, Message=str(exc))

        # Restart reconnect loop regardless — it will detect disconnections later.
        self._reconnect_stop.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True, name="mobile-reconnect"
        )
        self._reconnect_thread.start()

        return ConnectToRobot_Responses(
            Connected=connected,
            Message="Connected successfully" if connected else "Connection failed",
        )

    def SetHardwareActivation(
        self, HardwareId: str, Activate: bool, *, metadata: MetadataDict
    ) -> SetHardwareActivation_Responses:
        if not self._bridge.is_connected:
            raise RobotNotConnected()

        success, activation_state, message = self._run_async(
            self._bridge.set_hardware_activation(HardwareId, Activate), timeout=20.0
        )

        if not success and "not found" in message.lower():
            raise HardwareNotFound(f"Hardware '{HardwareId}' not found: {message}")

        return SetHardwareActivation_Responses(
            Success=success,
            ActivationState=activation_state,
            Message=message,
        )

    def SetHardwareActivationByRole(
        self, Hardware: str, Activate: bool, *, metadata: MetadataDict
    ) -> SetHardwareActivationByRole_Responses:
        if not self._bridge.is_connected:
            raise RobotNotConnected()

        brand = _ROLE_TO_BRAND.get(Hardware)
        hardware_list = self._run_async(self._bridge.get_hardware_list(), timeout=20.0)
        match = next((hw for hw in hardware_list if hw.brand == brand), None)
        if match is None:
            raise HardwareNotFound(f"No hardware found for role '{Hardware}'")

        success, activation_state, message = self._run_async(
            self._bridge.set_hardware_activation(match.hardware_id, Activate), timeout=20.0
        )

        return SetHardwareActivationByRole_Responses(
            Success=success,
            ActivationState=activation_state,
            Message=message,
        )

    def ActivateAllHardware(self, *, metadata: MetadataDict) -> ActivateAllHardware_Responses:
        if not self._bridge.is_connected:
            raise RobotNotConnected()

        hardware_list = self._run_async(self._bridge.get_hardware_list(), timeout=20.0)
        activated = 0
        lines = []

        # The mobile base must be up before anything else is activated — the
        # arm/gripper/camera are mounted on it and their activation depends on
        # the base already being live. Activate it first and confirm it actually
        # reports active before touching the rest of the hardware.
        base_brand = _ROLE_TO_BRAND["MobileBase"]
        base_hw = next((hw for hw in hardware_list if hw.brand == base_brand), None)
        rest_hw = [hw for hw in hardware_list if hw.brand != base_brand]

        if base_hw is not None:
            if base_hw.activated:
                # set_hardware_activation's response is unreliable (both result
                # and activationState come back False) when asked to activate
                # hardware that's already active — the hardware_list snapshot
                # above is accurate, so trust that instead of making the call.
                activated += 1
                lines.append(f"{base_hw.name}: OK (already active)")
            else:
                success, _, message = self._run_async(
                    self._bridge.set_hardware_activation(base_hw.hardware_id, True), timeout=20.0
                )
                if success:
                    activated += 1
                lines.append(f"{base_hw.name}: {'OK' if success else 'FAILED'} ({message})")

                if not success:
                    lines.append("Mobile base did not activate — aborting activation of remaining hardware")
                    return ActivateAllHardware_Responses(
                        ActivatedCount=activated,
                        Message="; ".join(lines),
                    )

        for hw in rest_hw:
            if hw.activated:
                activated += 1
                lines.append(f"{hw.name}: OK (already active)")
                continue
            success, _, message = self._run_async(
                self._bridge.set_hardware_activation(hw.hardware_id, True), timeout=20.0
            )
            if success:
                activated += 1
            lines.append(f"{hw.name}: {'OK' if success else 'FAILED'} ({message})")

        return ActivateAllHardware_Responses(
            ActivatedCount=activated,
            Message="; ".join(lines),
        )

    def DeactivateAllHardware(self, *, metadata: MetadataDict) -> DeactivateAllHardware_Responses:
        if not self._bridge.is_connected:
            raise RobotNotConnected()

        hardware_list = self._run_async(self._bridge.get_hardware_list(), timeout=20.0)
        deactivated = 0
        lines = []
        for hw in hardware_list:
            success, _, message = self._run_async(
                self._bridge.set_hardware_activation(hw.hardware_id, False), timeout=20.0
            )
            if success:
                deactivated += 1
            lines.append(f"{hw.name}: {'OK' if success else 'FAILED'} ({message})")

        return DeactivateAllHardware_Responses(
            DeactivatedCount=deactivated,
            Message="; ".join(lines),
        )
