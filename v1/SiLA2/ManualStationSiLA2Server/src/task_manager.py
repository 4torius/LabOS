"""
TaskManager — threading-based operator task management.

Ported from manual_station_servicer.py (which used asyncio.Event).
Since sila2 dispatches feature methods via a ThreadPoolExecutor,
threading.Event is used so that RequestOperatorTask can block the
worker thread while waiting for operator confirmation, without needing
a shared asyncio event loop.

State-change callbacks allow the sila2 ManualStationImpl to push
updates to observable properties (StationStatus, PendingTaskCount,
LastNotification) whenever the internal state changes.
"""

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


@dataclass
class OperatorTask:
    task_id: str
    task_type: str
    description: str
    priority: str
    source_instrument: str
    status: str = TaskStatus.PENDING.value
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: Optional[str] = None
    operator_notes: Optional[str] = None
    timeout_seconds: int = 0

    _completion_event: threading.Event = field(
        default_factory=threading.Event, repr=False
    )

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "description": self.description,
            "priority": self.priority,
            "source_instrument": self.source_instrument,
            "status": self.status,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "operator_notes": self.operator_notes,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass
class Notification:
    notification_id: str
    message: str
    level: str
    source_instrument: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "notification_id": self.notification_id,
            "message": self.message,
            "level": self.level,
            "source_instrument": self.source_instrument,
            "timestamp": self.timestamp,
        }


class TaskManager:
    """Thread-safe task and notification manager for the Manual Station server."""

    def __init__(self) -> None:
        self._active_tasks: Dict[str, OperatorTask] = {}
        self._task_history: List[OperatorTask] = []
        self._notifications: List[Notification] = []
        self._last_notification: Optional[Notification] = None
        self._lock = threading.Lock()

        # Called by ManualStationImpl to push sila2 observable property updates
        self.on_status_changed: Optional[Callable[[str], None]] = None
        self.on_pending_count_changed: Optional[Callable[[int], None]] = None
        self.on_notification_changed: Optional[Callable[[str], None]] = None

        # Optional callbacks for console / WebApp integration
        self.on_task_created: Optional[Callable[[OperatorTask], None]] = None
        self.on_task_completed: Optional[Callable[[OperatorTask], None]] = None
        self.on_notification_sent: Optional[Callable[[Notification], None]] = None

    # ─── Status helpers ───────────────────────────────────────────────────────

    def get_status(self) -> str:
        with self._lock:
            pending = [
                t for t in self._active_tasks.values()
                if t.status == TaskStatus.PENDING.value
            ]
            if pending:
                return "waiting_for_operator"
            if self._active_tasks:
                return "busy"
            return "idle"

    def get_pending_count(self) -> int:
        with self._lock:
            return sum(
                1 for t in self._active_tasks.values()
                if t.status == TaskStatus.PENDING.value
            )

    # ─── Task lifecycle ───────────────────────────────────────────────────────

    def create_task(
        self,
        task_type: str,
        description: str,
        priority: str,
        source_instrument: str,
        timeout_seconds: int,
    ) -> OperatorTask:
        task = OperatorTask(
            task_id=f"task_{uuid.uuid4().hex[:8]}",
            task_type=task_type,
            description=description,
            priority=priority,
            source_instrument=source_instrument,
            timeout_seconds=timeout_seconds,
        )
        with self._lock:
            self._active_tasks[task.task_id] = task
        self._fire_change_callbacks()
        if self.on_task_created:
            self.on_task_created(task)
        logger.info("Created operator task: %s — %s", task.task_id, task.description)
        return task

    def wait_for_task(self, task: OperatorTask) -> str:
        """
        Block the calling thread until the task is confirmed, cancelled, or timed out.
        Returns the final TaskStatus value string.
        """
        timeout = task.timeout_seconds if task.timeout_seconds > 0 else None
        completed = task._completion_event.wait(timeout=timeout)

        if not completed:
            with self._lock:
                task.status = TaskStatus.TIMEOUT.value
                self._move_to_history_locked(task.task_id)
            self._fire_change_callbacks()
            return TaskStatus.TIMEOUT.value

        with self._lock:
            self._move_to_history_locked(task.task_id)
        self._fire_change_callbacks()
        return task.status

    def confirm_task(self, task_id: str, notes: str = "") -> bool:
        with self._lock:
            task = self._active_tasks.get(task_id)
            if task is None:
                return False
            task.status = TaskStatus.COMPLETED.value
            task.completed_at = datetime.now().isoformat()
            task.operator_notes = notes or None
            task._completion_event.set()
        if self.on_task_completed:
            self.on_task_completed(task)
        logger.info("Task completed by operator: %s", task_id)
        return True

    def cancel_task(self, task_id: str, reason: str = "") -> bool:
        with self._lock:
            task = self._active_tasks.get(task_id)
            if task is None:
                return False
            task.status = TaskStatus.CANCELLED.value
            task.operator_notes = f"Cancelled: {reason}"
            task._completion_event.set()
        logger.info("Task cancelled: %s — %s", task_id, reason)
        return True

    # ─── Queries ──────────────────────────────────────────────────────────────

    def get_active_tasks(self) -> List[dict]:
        with self._lock:
            return [t.to_dict() for t in self._active_tasks.values()]

    def get_history(self, limit: int = 50) -> List[dict]:
        with self._lock:
            return [t.to_dict() for t in self._task_history[-limit:]]

    # ─── Notifications ────────────────────────────────────────────────────────

    def add_notification(self, message: str, level: str, source: str) -> str:
        notif = Notification(
            notification_id=f"notif_{uuid.uuid4().hex[:8]}",
            message=message,
            level=level,
            source_instrument=source,
        )
        with self._lock:
            self._notifications.append(notif)
            self._last_notification = notif
        if self.on_notification_sent:
            self.on_notification_sent(notif)
        if self.on_notification_changed:
            self.on_notification_changed(json.dumps(notif.to_dict()))
        return notif.notification_id

    def get_last_notification(self) -> Optional[dict]:
        with self._lock:
            return self._last_notification.to_dict() if self._last_notification else None

    # ─── Internal helpers ─────────────────────────────────────────────────────

    def _move_to_history_locked(self, task_id: str) -> None:
        """Move task from active to history. Must be called with self._lock held."""
        task = self._active_tasks.pop(task_id, None)
        if task is not None:
            self._task_history.append(task)

    def _fire_change_callbacks(self) -> None:
        status = self.get_status()
        count = self.get_pending_count()
        if self.on_status_changed:
            self.on_status_changed(status)
        if self.on_pending_count_changed:
            self.on_pending_count_changed(count)
