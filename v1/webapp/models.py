"""Shared data models for the LabOS webapp."""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from datetime import datetime


@dataclass
class DeviceState:
    id: str
    name: str
    type: str
    status: str  # online, offline, busy, error
    host: str
    port: int
    last_command: Optional[str] = None
    last_result: Optional[str] = None
    temperature: Optional[float] = None
    progress: Optional[int] = None


class AppState:
    def __init__(self):
        self.devices: Dict[str, DeviceState] = {}
        self.active_workflow: Optional[str] = None
        self.workflow_progress: int = 0
        self.logs: List[Dict[str, Any]] = []
        self.command_history: List[Dict[str, Any]] = []
        self.pnp_discovery = None

    def add_log(self, level: str, message: str, source: str = "system"):
        self.logs.append({
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "message": message,
            "source": source
        })
        if len(self.logs) > 1000:
            self.logs = self.logs[-1000:]

    def add_command(self, device: str, command: str, params: dict, result: dict, success: bool, duration_ms: float = 0):
        self.command_history.append({
            "id": len(self.command_history) + 1,
            "timestamp": datetime.now().isoformat(),
            "device": device,
            "command": command,
            "parameters": params,
            "result": result,
            "success": success,
            "duration_ms": duration_ms
        })
        if len(self.command_history) > 500:
            self.command_history = self.command_history[-500:]
