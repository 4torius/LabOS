"""Operator notification and emergency control routes."""
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

#: Per-instrument ceiling for an emergency stop call, in seconds. Deliberately
#: not `workflow.default_timeout`: that one is sized for a protocol that may run
#: for hours, which is the wrong order of magnitude for a stop. Calls go out
#: concurrently, so this is the total latency of the endpoint, not a per-device
#: cost. Short enough that an unreachable instrument cannot delay the others,
#: long enough to cross a LAN to the mobile robot.
EMERGENCY_STOP_TIMEOUT_S = 3.0


def create_operator_router(
    state,
    ws_manager,
    lab_core,
    pending_operator_actions: list,
    pending_operator_actions_lock: asyncio.Lock,
    BASE_DIR: Path,
) -> APIRouter:
    router = APIRouter(tags=["operator"])

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
        extra_data = data.get("extra_data", {})

        # Ids are compared as text. Notifications carry numeric ids and
        # interventions carry string ones; int() on every queued id raised
        # ValueError as soon as an intervention was waiting in the queue, so
        # confirming a manual task while a step awaited a decision failed.
        wanted = str(action_id)
        action = None
        async with pending_operator_actions_lock:
            action = next((a for a in pending_operator_actions if str(a.get("id")) == wanted), None)
            pending_operator_actions[:] = [a for a in pending_operator_actions if str(a.get("id")) != wanted]

        if not action:
            state.add_log("info", f"Acknowledge: action {action_id} not found (may be local-only)", "operator")
            return {"status": "ok", "message": "Action acknowledged (not found on server)"}

        state.add_log("info", f"Operator acknowledged action: {action_id}", "operator")
        await ws_manager.broadcast({"type": "action_acknowledged", "action_id": action_id, "title": action.get("title", "")})

        # Release the waiting task on whichever discovered server raised it.
        #
        # This used to hand-roll a gRPC call against ManualStationService stubs
        # in src/pnp_stubs/ — stubs that do not exist in this tree, at a port
        # (50360) that is not the one the server listens on (50500). Every
        # confirmation therefore threw ImportError and was swallowed by the
        # except below, so a RequestOperatorTask blocked until its own timeout
        # no matter how many times the operator clicked Confirm.
        #
        # Now it goes through the same SiLA2 path as every other command, and
        # finds the target by asking which discovered instrument exposes
        # ConfirmTaskComplete rather than by hardcoding a server name.
        task_id = action.get("params", {}).get("task_id") if action else None
        if task_id and action.get("action") == "operator_task":
            if not lab_core:
                state.add_log("warning", "Cannot confirm task: LabCore unavailable", "operator")
            else:
                try:
                    target = next(iter(lab_core.instruments_with_command("ConfirmTaskComplete")), None)
                    if target is None:
                        state.add_log(
                            "warning",
                            f"Task {task_id} acknowledged locally, but no online instrument "
                            f"exposes ConfirmTaskComplete to release it",
                            "operator",
                        )
                    else:
                        result = await lab_core.execute_command(
                            target.id, "ConfirmTaskComplete",
                            {"TaskId": task_id,
                             "Notes": extra_data.get("notes", "Confirmed via WebApp")},
                        )
                        state.add_log(
                            "info" if result.success else "warning",
                            f"ManualStation task {task_id}: "
                            f"{result.message or result.error or 'confirmed'}",
                            "operator",
                        )
                except Exception as e:
                    state.add_log("warning", f"Could not confirm ManualStation task: {e}", "operator")

        # Update tip_state.json on tip refill acknowledgment
        if action and (action.get("action") == "RefillTipRack" or "refill" in str(action.get("title", "")).lower()):
            await _update_tip_state(BASE_DIR, action.get("params", {}).get("rack_type", "all"), state, ws_manager, lab_core)

        return {"success": True, "remaining": len(pending_operator_actions)}

    @router.post("/api/operator/manual-step")
    async def record_manual_step(request: Request):
        # This is a free-text session-log entry only - it is not a tracked
        # workflow step or pending action (see operator.html's "Add Log Note").
        data = await request.json()
        step_id = int(datetime.now().timestamp() * 1000)
        note_type = data.get("type", "note")
        description = (data.get("description") or "").strip()
        value = (data.get("value") or "").strip()
        message = f"[{note_type}] {description}" + (f" = {value}" if value else "")
        state.add_log("info", message, "operator")
        return {"success": True, "step_id": step_id}

    # Emergency / control

    @router.post("/api/emergency/stop")
    async def emergency_stop():
        """Send every discovered instrument its stop command, at once.

        Three properties this endpoint has to have, none of which are free:

        *It does not wait for discovery.* The registry is populated at startup
        and kept current by mDNS; re-running discovery here would add seconds
        to the one request that cannot afford them, and an instrument that only
        a fresh scan would find is one this stop could not have reached anyway.

        *It does not use the configured command timeout.* That value (600 s by
        default) is sized for a protocol. Applied per instrument, in sequence,
        it meant a robot arm could receive its stop ten minutes after the
        operator pressed the button. Each call gets `EMERGENCY_STOP_TIMEOUT_S`
        and they all go out concurrently, so total latency is one timeout, not
        one per instrument.

        *It reports what actually happened.* `status` used to be the constant
        "stopped" — returned even when nothing was attempted or every call
        failed — and the UI showed "System Stopped" on the strength of it. An
        emergency stop that only claims to have worked is worse than one that
        admits it did not: the operator needs to know to go and hit the
        hardware stop.
        """
        state.add_log("warning", "EMERGENCY STOP activated", "system")
        await ws_manager.broadcast({"type": "emergency_stop", "message": "Emergency stop activated by operator"})

        if not lab_core:
            # No registry means no instrument was contacted. Say so plainly.
            return {
                "status": "failed", "attempted": 0, "successful": 0, "skipped": 0,
                "failures": ["LabCore unavailable — no instrument was contacted"],
                "message": "Emergency stop could NOT be delivered. Stop the instruments manually.",
            }

        candidate_commands = ("EmergencyStop", "Stop", "Abort", "Cancel", "Halt")

        targets = []
        skipped_names = []
        for instrument in lab_core.list_instruments():
            command_ids = {cmd.id for cmd in instrument.commands}
            cmd_to_run = next((cmd for cmd in candidate_commands if cmd in command_ids), None)
            if cmd_to_run:
                targets.append((instrument, cmd_to_run))
            else:
                skipped_names.append(instrument.name)
                state.add_log("info", f"No emergency command exposed by {instrument.name}", "system")

        async def _stop(instrument, command_id):
            try:
                result = await asyncio.wait_for(
                    lab_core.execute_command(instrument.id, command_id, {}),
                    timeout=EMERGENCY_STOP_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                return instrument.name, command_id, False, f"no answer within {EMERGENCY_STOP_TIMEOUT_S:g}s"
            except Exception as exc:
                return instrument.name, command_id, False, str(exc)
            if getattr(result, "success", False):
                return instrument.name, command_id, True, None
            return instrument.name, command_id, False, getattr(result, "error", None) or "unknown error"

        outcomes = await asyncio.gather(*(_stop(i, c) for i, c in targets))

        successful = 0
        failures = []
        for name, command_id, ok, error in outcomes:
            if ok:
                successful += 1
                state.add_log("warning", f"Emergency command {command_id} acknowledged by {name}", "system")
            else:
                failures.append(f"{name}: {error}")
                state.add_log("error", f"Emergency command failed on {name}: {error}", "system")

        attempted = len(targets)
        if attempted and successful == attempted:
            status = "stopped"
            message = f"All {attempted} instrument(s) acknowledged the stop."
        elif successful:
            status = "partial"
            message = (f"{successful} of {attempted} instrument(s) acknowledged the stop. "
                       "VERIFY THE OTHERS MANUALLY.")
        elif attempted:
            status = "failed"
            message = "No instrument acknowledged the stop. Use the hardware emergency stop."
        else:
            # Instruments may be present but expose no stop command at all.
            status = "failed"
            message = ("No discovered instrument exposes a stop command — nothing was stopped. "
                       "Use the hardware emergency stop.")

        return {
            "status": status,
            "attempted": attempted,
            "successful": successful,
            "skipped": len(skipped_names),
            "skipped_instruments": skipped_names,
            "failures": failures,
            "message": message,
        }

    return router


async def _update_tip_state(BASE_DIR: Path, rack_type: str, state, ws_manager, lab_core):
    """Reset tip counters after operator acknowledges refill, via the SiLA2
    RefillTipRack command (same rationale as instruments.py's /api/opentrons/refill-tips:
    tip_state.json is owned by the SiLA2 server's TipTracker; writing it directly
    here duplicated that logic and raced with it across processes)."""
    core = lab_core
    if not core:
        from src.lab_core import get_lab_core
        core = get_lab_core(BASE_DIR)

    try:
        await core.discover()
        # Ask which instrument can actually refill tips rather than matching
        # the substring "opentrons" — a second liquid handler, or a rename,
        # would silently break the name match.
        target = next(iter(core.instruments_with_command("RefillTipRack")), None)
        if target is None:
            state.add_log(
                "error",
                "Error during tip refill: no online instrument exposes RefillTipRack",
                "operator",
            )
            return

        result = await core.execute_command(target.id, "RefillTipRack", {"RackSlot": rack_type})
        if not result.success:
            state.add_log("error", f"Error during tip refill: {result.error}", "operator")
            return

        message = "All tip racks refilled" if rack_type.lower() == "all" else f"Tip rack {rack_type} refilled"
        state.add_log("info", f"Tip rack refilled: {rack_type}", "operator")
        asyncio.create_task(ws_manager.broadcast({"type": "tip_refill_complete", "rack_type": rack_type, "message": message}))
    except Exception as e:
        state.add_log("error", f"Error during tip refill: {e}", "operator")
