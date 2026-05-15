"""
ManualStation SiLA2 Feature Implementation
==========================================

Implements the ManualStationBase interface generated from ManualStation.sila.xml.
Wraps the TaskManager (threading-based task synchronisation) without changing
any of the operator-task business logic.

Observable properties (StationStatus, PendingTaskCount, LastNotification) are
kept up-to-date via TaskManager callbacks that call the sila2 update_*() methods.

RequestOperatorTask is an Observable command that blocks the sila2 worker thread
using threading.Event.wait() until the operator confirms, cancels, or the task
times out.
"""

import json
import logging
import os
import threading
from datetime import timedelta

import httpx

from sila2.server import MetadataDict, ObservableCommandInstanceWithIntermediateResponses

from generated.manualstation import (
    ManualStationBase,
    ManualStationFeature,
)
from generated.manualstation.manualstation_errors import (
    TaskAlreadyCompleted,
    TaskCancelled,
    TaskNotFound,
    TaskTimeout,
)
from generated.manualstation.manualstation_types import (
    CancelTask_Responses,
    ConfirmTaskComplete_Responses,
    GetActiveTasks_Responses,
    GetTaskHistory_Responses,
    RequestOperatorTask_IntermediateResponses,
    RequestOperatorTask_Responses,
    SendNotification_Responses,
)
from .task_manager import TaskManager, OperatorTask, Notification

logger = logging.getLogger(__name__)

WEBAPP_URL = os.environ.get("WEBAPP_URL", "http://127.0.0.1:5000")


class ManualStationImpl(ManualStationBase):
    """
    SiLA2 standard library implementation of the ManualStation feature.

    Operator tasks are managed by a thread-safe TaskManager.
    Observable properties are pushed via the generated update_*() helpers
    whenever internal state changes.
    """

    def __init__(self, parent_server, webapp_url: str = WEBAPP_URL) -> None:
        super().__init__(parent_server)
        self._webapp_url = webapp_url
        self._manager = TaskManager()

        # Wire TaskManager callbacks → sila2 observable property updates
        self._manager.on_status_changed = self.update_StationStatus
        self._manager.on_pending_count_changed = self.update_PendingTaskCount
        self._manager.on_notification_changed = self.update_LastNotification

        # Wire external-notification callbacks (console + WebApp)
        self._manager.on_task_created = self._on_task_created
        self._manager.on_notification_sent = self._on_notification_sent

        self.RequestOperatorTask_default_lifetime_of_execution = timedelta(hours=8)

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        super().start()
        # Initialise observable properties so subscribers always get an initial value
        self.update_StationStatus("idle")
        self.update_PendingTaskCount(0)
        self.update_LastNotification("{}")

    # ─── Observable property subscription callbacks ──────────────────────────

    def StationStatus_on_subscription(self, *, metadata: MetadataDict):
        self.update_StationStatus(self._manager.get_status())

    def PendingTaskCount_on_subscription(self, *, metadata: MetadataDict):
        self.update_PendingTaskCount(self._manager.get_pending_count())

    def LastNotification_on_subscription(self, *, metadata: MetadataDict):
        last = self._manager.get_last_notification()
        self.update_LastNotification(json.dumps(last) if last else "{}")

    # ─── Commands ────────────────────────────────────────────────────────────

    def RequestOperatorTask(
        self,
        TaskDescription: str,
        TaskType: str,
        Priority: str,
        SourceInstrument: str,
        TimeoutSeconds: int,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[
            RequestOperatorTask_IntermediateResponses
        ],
    ) -> RequestOperatorTask_Responses:
        instance.begin_execution()

        task = self._manager.create_task(
            task_type=TaskType,
            description=TaskDescription,
            priority=Priority,
            source_instrument=SourceInstrument,
            timeout_seconds=TimeoutSeconds,
        )

        instance.send_intermediate_response(
            RequestOperatorTask_IntermediateResponses(TaskStatus="pending")
        )

        # Block worker thread until the operator acts (or timeout elapses)
        final_status = self._manager.wait_for_task(task)

        if final_status == "timeout":
            raise TaskTimeout(
                f"Operator did not respond within {TimeoutSeconds}s"
            )
        if final_status == "cancelled":
            raise TaskCancelled(f"Task {task.task_id} was cancelled")

        return RequestOperatorTask_Responses(TaskId=task.task_id)

    def ConfirmTaskComplete(
        self, TaskId: str, Notes: str, *, metadata: MetadataDict
    ) -> ConfirmTaskComplete_Responses:
        if not self._manager.confirm_task(TaskId, Notes):
            raise TaskNotFound(f"Task not found: {TaskId}")
        return ConfirmTaskComplete_Responses(
            ConfirmationResult=f"Task {TaskId} confirmed"
        )

    def CancelTask(
        self, TaskId: str, Reason: str, *, metadata: MetadataDict
    ) -> CancelTask_Responses:
        if not self._manager.cancel_task(TaskId, Reason):
            raise TaskNotFound(f"Task not found: {TaskId}")
        return CancelTask_Responses(CancelResult=f"Task {TaskId} cancelled")

    def GetActiveTasks(self, *, metadata: MetadataDict) -> GetActiveTasks_Responses:
        return GetActiveTasks_Responses(
            ActiveTasks=json.dumps(self._manager.get_active_tasks())
        )

    def GetTaskHistory(
        self, Limit: int, *, metadata: MetadataDict
    ) -> GetTaskHistory_Responses:
        return GetTaskHistory_Responses(
            TaskHistory=json.dumps(self._manager.get_history(limit=Limit))
        )

    def SendNotification(
        self, Message: str, Level: str, SourceInstrument: str, *, metadata: MetadataDict
    ) -> SendNotification_Responses:
        notif_id = self._manager.add_notification(Message, Level, SourceInstrument)
        return SendNotification_Responses(NotificationId=notif_id)

    # ─── External callbacks ──────────────────────────────────────────────────

    def _on_task_created(self, task: OperatorTask) -> None:
        """Print to console and notify the WebApp operator dashboard."""
        print(f"\n{'='*60}")
        print(f"  OPERATOR TASK REQUIRED")
        print(f"{'='*60}")
        print(f"  Task ID:     {task.task_id}")
        print(f"  Type:        {task.task_type}")
        print(f"  Priority:    {task.priority.upper()}")
        print(f"  From:        {task.source_instrument}")
        print(f"  Description: {task.description}")
        print(f"{'='*60}")
        print(f"  Confirm via: ConfirmTaskComplete task_id={task.task_id!r}")
        print(f"{'='*60}\n")
        threading.Thread(
            target=self._notify_webapp, args=(task,), daemon=True
        ).start()

    def _on_notification_sent(self, notif: Notification) -> None:
        icon = {"info": "ℹ", "warning": "⚠", "error": "✗"}.get(notif.level, "►")
        print(f"\n  {icon} [{notif.source_instrument}] {notif.message}\n")

    def _notify_webapp(self, task: OperatorTask) -> None:
        priority_map = {
            "urgent": "urgent", "high": "warning",
            "normal": "warning", "low": "info",
        }
        payload = {
            "id": hash(task.task_id) & 0x7FFFFFFF,
            "title": f"{task.task_type.replace('_', ' ').title()}: {task.source_instrument}",
            "message": task.description,
            "priority": priority_map.get(task.priority.lower(), "warning"),
            "requires_action": True,
            "action": "operator_task",
            "params": {
                "task_id": task.task_id,
                "task_type": task.task_type,
                "source_instrument": task.source_instrument,
            },
        }
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(
                    f"{self._webapp_url}/api/operator/notify", json=payload
                )
                if resp.status_code == 200:
                    logger.info("Task %s sent to WebApp", task.task_id)
        except Exception:
            pass  # WebApp offline is not fatal
