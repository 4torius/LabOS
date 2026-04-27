#!/usr/bin/env python3
"""
Manual Station SiLA2 Servicer
=============================

Implements the ManualStationService gRPC interface.
Manages operator tasks, notifications, and workflow synchronization.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Callable, Any
from enum import Enum

import grpc
from . import ManualStationService_pb2 as pb2
from . import ManualStationService_pb2_grpc as pb2_grpc

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class TaskType(Enum):
    MANUAL_OPERATION = "manual_operation"
    TIP_REFILL = "tip_refill"
    QUALITY_CHECK = "quality_check"
    SAMPLE_PREP = "sample_prep"
    CUSTOM = "custom"


class Priority(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class NotificationLevel(Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class OperatorTask:
    """Represents a task for the operator."""
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
    
    # Internal: event to signal completion
    _completion_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    
    def to_dict(self) -> dict:
        """Convert to dictionary (excluding internal fields)."""
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
            "timeout_seconds": self.timeout_seconds
        }


@dataclass
class Notification:
    """Represents a notification."""
    notification_id: str
    message: str
    level: str
    source_instrument: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> dict:
        return asdict(self)


class ManualStationServicer(pb2_grpc.ManualStationServiceServicer):
    """
    gRPC servicer for the Manual Station.
    
    Manages:
    - Operator tasks (request, confirm, cancel)
    - Notifications
    - Status tracking
    """
    
    def __init__(self):
        self._active_tasks: Dict[str, OperatorTask] = {}
        self._task_history: List[OperatorTask] = []
        self._notifications: List[Notification] = []
        self._last_notification: Optional[Notification] = None
        
        # Status subscribers
        self._status_subscribers: List[asyncio.Queue] = []
        self._task_count_subscribers: List[asyncio.Queue] = []
        self._notification_subscribers: List[asyncio.Queue] = []
        
        # Callbacks for external notification (CLI, WebApp)
        self._on_task_created: Optional[Callable[[OperatorTask], None]] = None
        self._on_task_completed: Optional[Callable[[OperatorTask], None]] = None
        self._on_notification: Optional[Callable[[Notification], None]] = None
        
        logger.info("ManualStationServicer initialized")
    
    def get_pending_tasks(self) -> List[OperatorTask]:
        """Get list of pending tasks (for Plug & Play adapter)."""
        return [t for t in self._active_tasks.values() if t.status == TaskStatus.PENDING.value]
    
    #                           PROPERTIES
    
    async def GetStationStatus(self, request, context):
        """Get current station status."""
        status = self._get_status()
        return pb2.GetStationStatusResponse(status=status)
    
    async def SubscribeStationStatus(self, request, context):
        """Subscribe to station status changes."""
        queue = asyncio.Queue()
        self._status_subscribers.append(queue)
        
        try:
            # Send initial status
            yield pb2.GetStationStatusResponse(status=self._get_status())
            
            # Wait for updates
            while True:
                status = await queue.get()
                yield pb2.GetStationStatusResponse(status=status)
        finally:
            self._status_subscribers.remove(queue)
    
    async def GetPendingTaskCount(self, request, context):
        """Get count of pending tasks."""
        count = len([t for t in self._active_tasks.values() 
                    if t.status == TaskStatus.PENDING.value])
        return pb2.GetPendingTaskCountResponse(count=count)
    
    async def SubscribePendingTaskCount(self, request, context):
        """Subscribe to pending task count changes."""
        queue = asyncio.Queue()
        self._task_count_subscribers.append(queue)
        
        try:
            # Send initial count
            count = len([t for t in self._active_tasks.values() 
                        if t.status == TaskStatus.PENDING.value])
            yield pb2.GetPendingTaskCountResponse(count=count)
            
            # Wait for updates
            while True:
                count = await queue.get()
                yield pb2.GetPendingTaskCountResponse(count=count)
        finally:
            self._task_count_subscribers.remove(queue)
    
    async def GetLastNotification(self, request, context):
        """Get the last notification."""
        if self._last_notification:
            return pb2.GetLastNotificationResponse(
                notification_json=json.dumps(self._last_notification.to_dict())
            )
        return pb2.GetLastNotificationResponse(notification_json="{}")
    
    async def SubscribeLastNotification(self, request, context):
        """Subscribe to new notifications."""
        queue = asyncio.Queue()
        self._notification_subscribers.append(queue)
        
        try:
            # Send current notification
            if self._last_notification:
                yield pb2.GetLastNotificationResponse(
                    notification_json=json.dumps(self._last_notification.to_dict())
                )
            
            # Wait for new notifications
            while True:
                notification = await queue.get()
                yield pb2.GetLastNotificationResponse(
                    notification_json=json.dumps(notification.to_dict())
                )
        finally:
            self._notification_subscribers.remove(queue)
    
    #                           COMMANDS
    
    async def RequestOperatorTask(self, request, context):
        """
        Request the operator to perform a task.
        
        This is a streaming response that:
        1. Creates the task and sends initial response
        2. Waits for operator confirmation
        3. Sends final response when completed/cancelled/timeout
        """
        # Create task
        task = OperatorTask(
            task_id=f"task_{uuid.uuid4().hex[:8]}",
            task_type=request.task_type or TaskType.MANUAL_OPERATION.value,
            description=request.task_description,
            priority=request.priority or Priority.NORMAL.value,
            source_instrument=request.source_instrument or "Unknown",
            timeout_seconds=request.timeout_seconds
        )
        
        self._active_tasks[task.task_id] = task
        logger.info(f"Created operator task: {task.task_id} - {task.description}")
        
        # Notify subscribers
        await self._notify_status_change()
        await self._notify_task_count_change()
        
        # External callback
        if self._on_task_created:
            try:
                self._on_task_created(task)
            except Exception as e:
                logger.error(f"Error in on_task_created callback: {e}")
        
        # Send initial response
        yield pb2.RequestOperatorTaskResponse(
            is_intermediate=True,
            task_id=task.task_id,
            status=task.status,
            message=f"Task created. Waiting for operator to complete: {task.description}"
        )
        
        # Wait for completion, cancellation, or timeout
        try:
            if task.timeout_seconds > 0:
                await asyncio.wait_for(
                    task._completion_event.wait(),
                    timeout=task.timeout_seconds
                )
            else:
                await task._completion_event.wait()
            
            # Task completed or cancelled
            final_task = self._active_tasks.get(task.task_id) or task
            yield pb2.RequestOperatorTaskResponse(
                is_intermediate=False,
                task_id=final_task.task_id,
                status=final_task.status,
                message=f"Task {final_task.status}: {final_task.operator_notes or 'No notes'}"
            )
            
        except asyncio.TimeoutError:
            # Timeout
            task.status = TaskStatus.TIMEOUT.value
            self._move_to_history(task.task_id)
            
            yield pb2.RequestOperatorTaskResponse(
                is_intermediate=False,
                task_id=task.task_id,
                status=TaskStatus.TIMEOUT.value,
                message=f"Task timed out after {task.timeout_seconds} seconds"
            )
        
        finally:
            # Cleanup
            if task.task_id in self._active_tasks:
                self._move_to_history(task.task_id)
            await self._notify_status_change()
            await self._notify_task_count_change()
    
    async def ConfirmTaskComplete(self, request, context):
        """Operator confirms task completion."""
        task_id = request.task_id
        
        if task_id not in self._active_tasks:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Task not found: {task_id}")
            return pb2.ConfirmTaskCompleteResponse(
                success=False,
                message=f"Task not found: {task_id}"
            )
        
        task = self._active_tasks[task_id]
        
        if task.status not in (TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value):
            return pb2.ConfirmTaskCompleteResponse(
                success=False,
                message=f"Task already {task.status}"
            )
        
        # Mark as completed
        task.status = TaskStatus.COMPLETED.value
        task.completed_at = datetime.now().isoformat()
        task.operator_notes = request.notes or None
        
        # Signal completion
        task._completion_event.set()
        
        logger.info(f"Task completed by operator: {task_id}")
        
        # External callback
        if self._on_task_completed:
            try:
                self._on_task_completed(task)
            except Exception as e:
                logger.error(f"Error in on_task_completed callback: {e}")
        
        return pb2.ConfirmTaskCompleteResponse(
            success=True,
            message=f"Task {task_id} marked as completed"
        )
    
    async def CancelTask(self, request, context):
        """Cancel a pending task."""
        task_id = request.task_id
        
        if task_id not in self._active_tasks:
            return pb2.CancelTaskResponse(
                success=False,
                message=f"Task not found: {task_id}"
            )
        
        task = self._active_tasks[task_id]
        task.status = TaskStatus.CANCELLED.value
        task.operator_notes = f"Cancelled: {request.reason}"
        
        # Signal completion (with cancelled status)
        task._completion_event.set()
        
        logger.info(f"Task cancelled: {task_id} - {request.reason}")
        
        return pb2.CancelTaskResponse(
            success=True,
            message=f"Task {task_id} cancelled"
        )
    
    async def GetActiveTasks(self, request, context):
        """Get all active tasks."""
        tasks = [t.to_dict() for t in self._active_tasks.values()]
        return pb2.GetActiveTasksResponse(
            tasks_json=json.dumps(tasks),
            count=len(tasks)
        )
    
    async def GetTaskHistory(self, request, context):
        """Get task history."""
        limit = request.limit or 50
        tasks = [t.to_dict() for t in self._task_history[-limit:]]
        return pb2.GetTaskHistoryResponse(
            tasks_json=json.dumps(tasks),
            count=len(tasks)
        )
    
    async def SendNotification(self, request, context):
        """Send a notification (non-blocking)."""
        notification = Notification(
            notification_id=f"notif_{uuid.uuid4().hex[:8]}",
            message=request.message,
            level=request.level or NotificationLevel.INFO.value,
            source_instrument=request.source_instrument or "System"
        )
        
        self._notifications.append(notification)
        self._last_notification = notification
        
        # Notify subscribers
        for queue in self._notification_subscribers:
            try:
                queue.put_nowait(notification)
            except asyncio.QueueFull:
                pass
        
        # External callback
        if self._on_notification:
            try:
                self._on_notification(notification)
            except Exception as e:
                logger.error(f"Error in on_notification callback: {e}")
        
        logger.info(f"Notification sent: [{notification.level}] {notification.message}")
        
        return pb2.SendNotificationResponse(
            notification_id=notification.notification_id,
            success=True
        )
    
    #                           INTERNAL METHODS
    
    def _get_status(self) -> str:
        """Get current station status."""
        pending_count = len([t for t in self._active_tasks.values() 
                           if t.status == TaskStatus.PENDING.value])
        
        if pending_count == 0:
            return "idle"
        elif any(t.priority == Priority.URGENT.value for t in self._active_tasks.values()):
            return "waiting_for_operator"
        else:
            return "waiting_for_operator"
    
    def _move_to_history(self, task_id: str):
        """Move a task from active to history."""
        if task_id in self._active_tasks:
            task = self._active_tasks.pop(task_id)
            self._task_history.append(task)
            
            # Keep history size manageable
            if len(self._task_history) > 1000:
                self._task_history = self._task_history[-500:]
    
    async def _notify_status_change(self):
        """Notify all status subscribers."""
        status = self._get_status()
        for queue in self._status_subscribers:
            try:
                queue.put_nowait(status)
            except asyncio.QueueFull:
                pass
    
    async def _notify_task_count_change(self):
        """Notify all task count subscribers."""
        count = len([t for t in self._active_tasks.values() 
                    if t.status == TaskStatus.PENDING.value])
        for queue in self._task_count_subscribers:
            try:
                queue.put_nowait(count)
            except asyncio.QueueFull:
                pass
    
    #                           CALLBACKS
    
    def set_on_task_created(self, callback: Callable[[OperatorTask], None]):
        """Set callback for when a new task is created."""
        self._on_task_created = callback
    
    def set_on_task_completed(self, callback: Callable[[OperatorTask], None]):
        """Set callback for when a task is completed."""
        self._on_task_completed = callback
    
    def set_on_notification(self, callback: Callable[[Notification], None]):
        """Set callback for new notifications."""
        self._on_notification = callback
    
    #                     DIRECT METHODS (for internal use)
    
    def get_active_tasks_list(self) -> List[dict]:
        """Get active tasks as list of dicts (for CLI/WebApp)."""
        return [t.to_dict() for t in self._active_tasks.values()]
    
    def confirm_task(self, task_id: str, notes: str = "") -> bool:
        """Directly confirm a task (for CLI/WebApp)."""
        if task_id not in self._active_tasks:
            return False
        
        task = self._active_tasks[task_id]
        task.status = TaskStatus.COMPLETED.value
        task.completed_at = datetime.now().isoformat()
        task.operator_notes = notes
        task._completion_event.set()
        return True
    
    def cancel_task_sync(self, task_id: str, reason: str = "") -> bool:
        """Directly cancel a task (for CLI/WebApp)."""
        if task_id not in self._active_tasks:
            return False
        
        task = self._active_tasks[task_id]
        task.status = TaskStatus.CANCELLED.value
        task.operator_notes = f"Cancelled: {reason}"
        task._completion_event.set()
        return True
