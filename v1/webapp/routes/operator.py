"""Operator notification and emergency control routes."""
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.config_schema import load_lab_config as _load_lab_config_schema


def _load_server_endpoint_from_config(base_dir: Path, server_key: str, default_host: str, default_port: int) -> tuple[str, int]:
    config_path = base_dir / "lab_config.yaml"
    try:
        config, _ = _load_lab_config_schema(config_path, apply_defaults=False, strict=False)
        server_cfg = (config.get("servers") or {}).get(server_key, {})
        return server_cfg.get("host", default_host), int(server_cfg.get("port", default_port))
    except Exception as exc:
        logger.warning(f"Unable to load endpoint for {server_key}: {exc}")
    return default_host, default_port


def create_operator_router(
    state,
    ws_manager,
    lab_core,
    pending_operator_actions: list,
    pending_operator_actions_lock: asyncio.Lock,
    BASE_DIR: Path,
    WEBAPP_CONFIG: dict,
) -> APIRouter:
    router = APIRouter(tags=["operator"])

    def _load_server_endpoint(server_key: str, default_host: str, default_port: int) -> tuple[str, int]:
        return _load_server_endpoint_from_config(BASE_DIR, server_key, default_host, default_port)

    @router.get("/api/operator/pending")
    async def get_pending_actions():
        async with pending_operator_actions_lock:
            return {"actions": list(pending_operator_actions)}

    @router.post("/api/operator/notify")
    async def send_operator_notification(request: Request):
        data = await request.json()
        notification_id = data.get("id") or int(datetime.now().timestamp() * 1000)
        notification = {
            "id": notification_id, "type": "operator_notification",
            "title": data.get("title", "Notification"),
            "message": data.get("message", ""),
            "priority": data.get("priority", "info"),
            "requires_action": data.get("requires_action", False),
            "action": data.get("action"),
            "params": data.get("params", {}),
            "timestamp": datetime.now().isoformat()
        }
        if data.get("requires_action", False):
            async with pending_operator_actions_lock:
                pending_operator_actions.append(notification)
        await ws_manager.broadcast(notification)
        state.add_log("info", f"Operator notification sent: {notification['title']}", "operator")
        return {"success": True, "notification_id": notification["id"]}

    @router.post("/api/operator/acknowledge")
    async def acknowledge_action(request: Request):
        data = await request.json()
        action_id = data.get("action_id")
        if action_id is None:
            raise HTTPException(400, "Missing action_id")
        try:
            action_id = int(action_id)
        except (ValueError, TypeError):
            pass
        extra_data = data.get("extra_data", {})

        action = None
        async with pending_operator_actions_lock:
            action = next((a for a in pending_operator_actions if int(a.get("id", 0)) == action_id), None)
            pending_operator_actions[:] = [a for a in pending_operator_actions if int(a.get("id", 0)) != action_id]

        if not action:
            state.add_log("info", f"Acknowledge: action {action_id} not found (may be local-only)", "operator")
            return {"status": "ok", "message": "Action acknowledged (not found on server)"}

        state.add_log("info", f"Operator acknowledged action: {action_id}", "operator")
        await ws_manager.broadcast({"type": "action_acknowledged", "action_id": action_id, "title": action.get("title", "")})

        # Confirm ManualStation task via gRPC if applicable
        task_id = action.get("params", {}).get("task_id") if action else None
        if task_id and action.get("action") == "operator_task":
            try:
                import grpc
                from src.pnp_stubs import ManualStationService_pb2 as manual_pb2
                from src.pnp_stubs import ManualStationService_pb2_grpc as manual_pb2_grpc
                host, port = _load_server_endpoint("manual_station", "localhost", 50360)
                async with grpc.aio.insecure_channel(f'{host}:{port}') as channel:
                    stub = manual_pb2_grpc.ManualStationServiceStub(channel)
                    req = manual_pb2.ConfirmTaskCompleteRequest(
                        task_id=task_id,
                        notes=extra_data.get("notes", "Confirmed via WebApp")
                    )
                    response = await stub.ConfirmTaskComplete(req)
                    log_level = "info" if response.success else "warning"
                    state.add_log(log_level, f"ManualStation task {task_id}: {response.message}", "operator")
            except Exception as e:
                state.add_log("warning", f"Could not confirm ManualStation task: {e}", "operator")

        # Update tip_state.json on tip refill acknowledgment
        if action and (action.get("action") == "RefillTipRack" or "refill" in str(action.get("title", "")).lower()):
            _update_tip_state(BASE_DIR, action.get("params", {}).get("rack_type", "all"), state, ws_manager)

        return {"success": True, "remaining": len(pending_operator_actions)}

    @router.post("/api/operator/manual-step")
    async def record_manual_step(request: Request):
        data = await request.json()
        step_id = int(datetime.now().timestamp() * 1000)
        state.add_log("info", f"Manual step recorded: {data.get('type', 'note')}", "operator")
        return {"success": True, "step_id": step_id}

    @router.post("/api/operator/test-notification")
    async def create_test_notification():
        notification_id = int(datetime.now().timestamp() * 1000)
        notification = {
            "id": notification_id, "type": "operator_notification",
            "title": "Test Notification",
            "message": "This is a test notification. Click Confirm to dismiss it.",
            "priority": "warning", "requires_action": True,
            "timestamp": datetime.now().isoformat(), "action": "test", "params": {}
        }
        async with pending_operator_actions_lock:
            pending_operator_actions.append(notification)
        await ws_manager.broadcast(notification)
        state.add_log("info", "Test notification created", "operator")
        return {"status": "ok", "notification_id": notification_id}

    @router.post("/api/operator/test-tip-refill")
    async def create_tip_refill_notification():
        notification_id = int(datetime.now().timestamp() * 1000)
        notification = {
            "id": notification_id, "type": "operator_notification",
            "title": "Refill Tip Rack (TEST)",
            "message": (
                "REFILL TIPRACK RICHIESTO\n\n"
                "Tipo: opentrons_flex_96_tiprack_1000ul\n"
                "Posizione: C3\nTips necessari: 48\nTips disponibili: 12\n\n"
                "Sostituire il tiprack con uno pieno e confermare."
            ),
            "priority": "urgent", "requires_action": True,
            "timestamp": datetime.now().isoformat(), "action": "RefillTipRack",
            "params": {"rack_type": "opentrons_flex_96_tiprack_1000ul", "location": "C3", "needed": 48, "available": 12}
        }
        async with pending_operator_actions_lock:
            pending_operator_actions.append(notification)
        await ws_manager.broadcast(notification)
        state.add_log("info", "Test tip refill notification created", "operator")
        return {"status": "ok", "notification_id": notification_id}

    # Emergency / control

    @router.post("/api/emergency/stop")
    async def emergency_stop():
        state.add_log("warning", "EMERGENCY STOP activated", "system")
        await ws_manager.broadcast({"type": "emergency_stop", "message": "Emergency stop activated by operator"})

        if not lab_core:
            return {"status": "stopped", "message": "Emergency stop broadcasted (LabCore unavailable)"}

        attempted = 0
        successful = 0
        skipped = 0
        failures = []
        candidate_commands = ("EmergencyStop", "Stop", "Abort", "Cancel", "Halt")

        try:
            await lab_core.discover()
            for instrument in lab_core.list_instruments():
                command_ids = {cmd.id for cmd in instrument.commands}
                cmd_to_run = next((cmd for cmd in candidate_commands if cmd in command_ids), None)

                if not cmd_to_run:
                    skipped += 1
                    state.add_log("info", f"No emergency command exposed by {instrument.name}", "system")
                    continue

                attempted += 1
                result = await lab_core.execute_command(instrument.id, cmd_to_run, {})
                if result.success:
                    successful += 1
                    state.add_log("warning", f"Emergency command {cmd_to_run} sent to {instrument.name}", "system")
                else:
                    msg = f"{instrument.name}: {result.error or 'unknown error'}"
                    failures.append(msg)
                    state.add_log("error", f"Emergency command failed on {msg}", "system")
        except Exception as exc:
            failures.append(str(exc))
            state.add_log("error", f"Emergency stop propagation error: {exc}", "system")

        return {
            "status": "stopped",
            "attempted": attempted,
            "successful": successful,
            "skipped": skipped,
            "failures": failures,
            "message": "Emergency stop processed"
        }

    return router


def _update_tip_state(BASE_DIR: Path, rack_type: str, state, ws_manager):
    """Reset tip counters in tip_state.json after operator acknowledges refill."""
    tip_state_file = BASE_DIR / "SiLA2" / "OpentronsSiLA2Server" / "tip_state.json"
    try:
        tip_state = {}
        if tip_state_file.exists():
            tip_state = json.loads(tip_state_file.read_text(encoding='utf-8'))
        if rack_type.lower() == "all":
            for key in tip_state:
                tip_state[key] = 0
            message = "All tip racks refilled"
        else:
            tip_state[rack_type] = 0
            message = f"Tip rack {rack_type} refilled"
        tip_state_file.write_text(json.dumps(tip_state, indent=2), encoding='utf-8')
        state.add_log("info", f"Tip rack refilled: {rack_type}", "operator")
        asyncio.create_task(ws_manager.broadcast({"type": "tip_refill_complete", "rack_type": rack_type, "message": message}))
    except Exception as e:
        state.add_log("error", f"Error during tip refill: {e}", "operator")
