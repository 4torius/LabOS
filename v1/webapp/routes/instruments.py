"""Instrument, device, and file API routes."""
import glob as glob_module
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.config_schema import load_lab_config as _load_lab_config_schema


def _load_lab_config(base_dir: Path) -> dict:
    config_path = base_dir / "lab_config.yaml"
    try:
        config, _ = _load_lab_config_schema(config_path, apply_defaults=False, strict=False)
        return config
    except Exception as exc:
        logger.warning(f"Unable to load lab_config.yaml: {exc}")
    return {}


def _resolve_library_path(base_dir: Path, config_key: str, default_relative: str) -> Path:
    config = _load_lab_config(base_dir)
    configured = ((config.get("paths") or {}).get(config_key) or default_relative)
    path = Path(configured)
    if path.is_absolute():
        return path
    return base_dir / path


def _load_server_endpoint(base_dir: Path, server_key: str, default_host: str, default_port: int) -> tuple[str, int]:
    try:
        config = _load_lab_config(base_dir)
        server_cfg = (config.get("servers") or {}).get(server_key, {})
        host = server_cfg.get("host", default_host)
        port = int(server_cfg.get("port", default_port))
        return host, port
    except Exception as exc:
        logger.warning(f"Unable to load endpoint for {server_key}: {exc}")
    return default_host, default_port


def create_instruments_router(
    state,
    ws_manager,
    lab_core,
    plate_tracking: dict,
    save_plate_tracking: Callable,
    plate_tracking_lock,
    BASE_DIR: Path,
    LIBRARY_DIR: Path,
) -> APIRouter:
    router = APIRouter(tags=["instruments"])

    # Devices

    @router.get("/api/devices")
    async def get_devices():
        try:
            if lab_core:
                await lab_core.discover()
                state.devices = {}
                for inst in lab_core.list_instruments():
                    from webapp.models import DeviceState
                    state.devices[inst.id] = DeviceState(
                        id=inst.id, name=inst.name, type=inst.type,
                        status=inst.status, host=inst.host, port=inst.port
                    )
                if state.devices:
                    state.add_log("info", f"Discovered {len(state.devices)} devices via LabCore", "discovery")
                    return [asdict(d) for d in state.devices.values()]
        except Exception as e:
            state.add_log("warning", f"LabCore discovery failed: {e}", "discovery")

        try:
            config = _load_lab_config(BASE_DIR)
            servers = config.get("servers", {})
            state.devices = {}
            for key, srv in servers.items():
                if srv.get("enabled", True):
                    from webapp.models import DeviceState
                    device_id = key.lower().replace("-", "_")
                    state.devices[device_id] = DeviceState(
                        id=device_id, name=srv.get("name", key),
                        type=srv.get("type", "instrument"), status="unknown",
                        host=srv.get("host", "localhost"), port=srv.get("port", 50051)
                    )
            state.add_log("info", f"Loaded {len(state.devices)} devices from config", "config")
        except Exception as e:
            state.add_log("error", f"Failed to load devices: {e}", "config")

        return [asdict(d) for d in state.devices.values()]

    @router.get("/api/devices/{device_id}")
    async def get_device(device_id: str):
        if device_id not in state.devices:
            raise HTTPException(404, "Device not found")
        return asdict(state.devices[device_id])

    # Instrument commands (Plug & Play discovery)

    @router.get("/api/instruments/commands")
    async def get_instrument_commands():
        try:
            if lab_core:
                await lab_core.discover()
                return lab_core.get_all_commands_dict()
            else:
                from src.lab_core import get_lab_core
                core = get_lab_core(BASE_DIR)
                await core.discover()
                return core.get_all_commands_dict()
        except Exception as e:
            state.add_log("warning", f"LabCore discovery failed: {e}", "system")

        try:
            from src.discovery import PnPDiscovery
            discovery = PnPDiscovery(BASE_DIR)
            await discovery.discover_all()
            instruments = {}
            for server in discovery.list_servers():
                key = server.name.lower().replace(" ", "_")
                commands = []
                for feature_id, cmd_id, cmd in server.get_all_commands():
                    commands.append({
                        "id": cmd_id,
                        "name": cmd.display_name or cmd_id,
                        "description": cmd.description or "",
                        "feature": feature_id,
                        "important": True,
                        "parameters": [
                            {
                                "name": p.identifier,
                                "displayName": p.display_name or p.identifier,
                                "type": p.data_type,
                                "required": p.required,
                                "description": p.description or "",
                                "ui_hint": p.infer_ui_hint(),
                                "options": p.constraints if p.constraints else []
                            }
                            for p in cmd.parameters
                        ]
                    })
                instruments[key] = {
                    "name": server.name, "type": server.server_type or "instrument",
                    "status": "online" if server.server_online else "offline",
                    "address": server.address, "commands": commands
                }
            if instruments:
                return {"source": "pnp_discovery_fallback", "instruments": instruments}
        except Exception as e:
            state.add_log("warning", f"PnP discovery failed: {e}", "system")

        config_path = BASE_DIR / "webapp" / "static" / "config" / "instruments.json"
        try:
            with open(config_path) as f:
                config = json.load(f)
            for key, inst in config.get("instruments", {}).items():
                for cmd in inst.get("commands", []):
                    cmd["important"] = True
            return {"source": "config_file", "instruments": config.get("instruments", {})}
        except Exception as e:
            return {"source": "error", "error": str(e), "instruments": {}}

    # Execute command on device

    @router.post("/api/devices/{device_id}/command")
    async def execute_device_command(device_id: str, request: Request):
        data = await request.json()
        command = data.get("command")
        params = data.get("parameters", {})
        normalized_id = device_id.lower().replace("-", "_")

        try:
            core = lab_core
            if not core:
                from src.lab_core import get_lab_core
                core = get_lab_core(BASE_DIR)

            await core.discover()
            instrument = core.get_instrument(normalized_id) or core.get_instrument(device_id)
            if not instrument:
                return {"status": "error", "error": f"Device '{device_id}' not found"}

            actual_id = instrument.id
            if actual_id in state.devices:
                state.devices[actual_id].status = "busy"
                state.devices[actual_id].last_command = command

            await ws_manager.broadcast({"type": "device_update", "device_id": actual_id, "status": "busy", "command": command})

            result = await core.execute_command(actual_id, command, params)

            if result.success:
                if actual_id in state.devices:
                    state.devices[actual_id].status = "online"
                    state.devices[actual_id].last_result = result.message or "OK"

                await ws_manager.broadcast({"type": "device_update", "device_id": actual_id, "status": "online", "result": "completed"})
                state.add_log("info", f"Executed {command} on {instrument.name}", actual_id)
                await ws_manager.broadcast({
                    "type": "command_executed",
                    "device_id": actual_id,
                    "device_name": instrument.name,
                    "command": command,
                    "message": result.message or "completed",
                    "timestamp": datetime.now().isoformat(),
                })

                # Plate tracking (save_plate_tracking handles its own lock)
                _auto_track_tecan(actual_id, command, params, result, plate_tracking, state)
                _auto_track_opentrons(actual_id, command, params, result, plate_tracking, state)
                await save_plate_tracking(plate_tracking)

                if command == "RefreshTasks":
                    await ws_manager.broadcast({"type": "mobile_tasks_updated"})

                state.add_command(
                    device=actual_id, command=command, params=params,
                    result={"data": result.data, "message": result.message}, success=True
                )
                return {"status": "completed", "result": result.data or "OK"}
            else:
                if actual_id in state.devices:
                    state.devices[actual_id].status = "error"
                    state.devices[actual_id].last_result = result.error
                await ws_manager.broadcast({"type": "device_update", "device_id": actual_id, "status": "error", "error": result.error})
                state.add_log("error", f"Command {command} failed: {result.error}", actual_id)
                await ws_manager.broadcast({
                    "type": "command_failed",
                    "device_id": actual_id,
                    "device_name": instrument.name,
                    "command": command,
                    "error": result.error or "unknown error",
                    "timestamp": datetime.now().isoformat(),
                })
                state.add_command(device=actual_id, command=command, params=params, result={"error": result.error}, success=False)
                return {"status": "error", "error": result.error}

        except Exception as e:
            state.add_log("error", f"Execution error: {e}", device_id)
            state.add_command(device=device_id, command=command, params=params,
                              result={"error": str(e), "exception": type(e).__name__}, success=False)
            return {"status": "error", "error": str(e)}

    # File listings

    @router.get("/api/analyses")
    async def get_analyses():
        analyses = []
        analysis_dir = LIBRARY_DIR / "Analysis"
        if analysis_dir.exists():
            for f in analysis_dir.glob("*.mdfx"):
                analyses.append({"id": f.stem, "name": f.name, "path": str(f)})
        return analyses

    # Plate catalog (Settimana 9+)

    @router.get("/api/library/plates")
    async def get_plates(category: str = ""):
        """Return all plate definitions from Library/Labware/Plates/."""
        plates_dir = _resolve_library_path(BASE_DIR, "plates", "Library/Labware/Plates")
        plates = []
        if plates_dir.exists():
            for f in sorted(plates_dir.glob("*.plate.json")):
                try:
                    data = json.loads(f.read_text(encoding='utf-8'))
                    if category and data.get("category", "") != category:
                        continue
                    plates.append(data)
                except Exception:
                    pass
        # Sort: 96-well first, then 384, then others
        def _sort_key(p):
            fmt = p.get("format", "")
            if fmt == "96Standard": return 0
            if fmt == "384Standard": return 1
            return 2
        plates.sort(key=_sort_key)
        return {"plates": plates, "count": len(plates)}

    @router.get("/api/library/plates/{plate_id}")
    async def get_plate(plate_id: str):
        """Return a single plate definition by id."""
        plates_dir = _resolve_library_path(BASE_DIR, "plates", "Library/Labware/Plates")
        for f in plates_dir.glob("*.plate.json"):
            try:
                data = json.loads(f.read_text(encoding='utf-8'))
                if data.get("id") == plate_id or f.stem.replace(".plate", "") == plate_id:
                    return data
            except Exception:
                pass
        raise HTTPException(404, f"Plate {plate_id!r} not found in Library/Labware/Plates/")

    @router.get("/api/tipracks")
    async def get_tipracks():
        """Return Opentrons tip rack definitions from Library/Labware/TipRacks/."""
        tipracks_dir = _resolve_library_path(BASE_DIR, "tipracks", "Library/Labware/TipRacks")
        index_file = tipracks_dir / "index.json"
        if index_file.exists():
            data = json.loads(index_file.read_text(encoding='utf-8'))
            data["path"] = str(tipracks_dir)
            return data

        definitions = []
        for load_name_dir in sorted([p for p in tipracks_dir.glob("*") if p.is_dir()]):
            for def_file in sorted(load_name_dir.glob("*.json")):
                definitions.append({
                    "load_name": load_name_dir.name,
                    "version": def_file.stem,
                    "path": str(def_file),
                })
        return {
            "source": "local-folder-scan",
            "path": str(tipracks_dir),
            "total_files": len(definitions),
            "definitions": definitions,
        }

    @router.get("/api/pipettes")
    async def get_pipettes():
        """Return Opentrons pipette definitions from Library/Labware/Pipettes/."""
        pipettes_dir = _resolve_library_path(BASE_DIR, "pipettes", "Library/Labware/Pipettes")
        index_file = pipettes_dir / "index.json"
        if index_file.exists():
            data = json.loads(index_file.read_text(encoding='utf-8'))
            data["path"] = str(pipettes_dir)
            return data

        definitions = []
        for channel_dir in sorted([p for p in pipettes_dir.glob("*") if p.is_dir()]):
            for model_dir in sorted([p for p in channel_dir.glob("*") if p.is_dir()]):
                for def_file in sorted(model_dir.glob("*.json")):
                    definitions.append({
                        "channel": channel_dir.name,
                        "model": model_dir.name,
                        "version": def_file.stem,
                        "path": str(def_file),
                    })
        return {
            "source": "local-folder-scan",
            "path": str(pipettes_dir),
            "total_files": len(definitions),
            "definitions": definitions,
        }

    @router.get("/api/files/hal")
    async def get_hal_files():
        files = [f.name for f in (LIBRARY_DIR / "HardwareConfig").glob("*.json")] if (LIBRARY_DIR / "HardwareConfig").exists() else []
        return {"files": files}

    @router.get("/api/files/recipes")
    async def get_recipe_files():
        files = [f.name for f in (LIBRARY_DIR / "Recipes").glob("*.json")] if (LIBRARY_DIR / "Recipes").exists() else []
        return {"files": files}

    @router.get("/api/files/protocols")
    async def get_protocol_files():
        files = [f.name for f in (LIBRARY_DIR / "Analysis").glob("*.mdfx")] if (LIBRARY_DIR / "Analysis").exists() else []
        return {"files": files}

    @router.get("/api/files/analyses")
    async def get_analysis_files():
        return await get_protocol_files()

    @router.get("/api/files/workflows")
    async def get_workflow_files():
        files = [f.name for f in (LIBRARY_DIR / "Workflows").glob("*.workflow.json")] if (LIBRARY_DIR / "Workflows").exists() else []
        return {"files": files}

    @router.get("/api/files/liquidclasses")
    async def get_liquid_class_files():
        files = []
        classes = {}
        lc_dir = LIBRARY_DIR / "LiquidClasses"
        if lc_dir.exists():
            for f in lc_dir.glob("*.json"):
                if f.name == 'liquidclass.schema.json':
                    continue
                files.append(f.stem)
                try:
                    data = json.loads(f.read_text(encoding='utf-8'))
                    name = data.get('name', f.stem)
                    emoji = {'Aqueous': '💧', 'Viscous': '🍯', 'HighlyViscous': '🍯🍯',
                             'Volatile': '💨', 'Foaming': '🫧'}.get(name, '🔬')
                    classes[name] = {'displayName': f"{emoji} {name}", 'description': data.get('description', '')}
                except Exception:
                    classes[f.stem] = {'displayName': f.stem, 'description': ''}
        return {"files": files, "classes": classes}

    @router.get("/api/files/pipetting-plans")
    async def get_pipetting_plans():
        files = []
        plans_dir = LIBRARY_DIR / "PipettingPlans"
        if plans_dir.exists():
            for ext in ["*.xlsx", "*.xls", "*.csv"]:
                for f in plans_dir.glob(ext):
                    files.append({
                        "name": f.name, "stem": f.stem,
                        "size": f.stat().st_size,
                        "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat()
                    })
        return {"files": sorted(files, key=lambda x: x["name"])}

    @router.get("/api/files/pipetting-plans/{filename}")
    async def get_pipetting_plan_file(filename: str):
        file_path = LIBRARY_DIR / "PipettingPlans" / filename
        if not file_path.exists():
            raise HTTPException(404, f"File not found: {filename}")
        return FileResponse(file_path, filename=filename)

    @router.get("/api/tecan/protocols")
    async def get_tecan_protocols():
        protocols = [f.name for f in (LIBRARY_DIR / "Analysis").glob("*.mdfx")] if (LIBRARY_DIR / "Analysis").exists() else []
        return {"protocols": protocols}

    # Mobile + dynamic options

    @router.get("/api/mobile/tasks")
    async def get_mobile_tasks():
        try:
            if lab_core:
                tasks = await lab_core.fetch_dynamic_options("mobile_task")
                return {"tasks": tasks, "count": len(tasks)}
            return {"tasks": [], "count": 0, "error": "LabCore not initialized"}
        except Exception as e:
            logger.error(f"Failed to get mobile tasks: {e}")
            return {"tasks": [], "count": 0, "error": str(e)}

    @router.get("/api/dynamic-options/{ui_hint}")
    async def get_dynamic_options(ui_hint: str):
        try:
            if lab_core:
                options = await lab_core.fetch_dynamic_options(ui_hint)
                return {"options": options, "ui_hint": ui_hint}
            return {"options": [], "ui_hint": ui_hint, "error": "LabCore not initialized"}
        except Exception as e:
            logger.error(f"Failed to get dynamic options for {ui_hint}: {e}")
            return {"options": [], "ui_hint": ui_hint, "error": str(e)}

    # Opentrons tip management

    @router.post("/api/opentrons/refill-tips")
    async def refill_tip_rack(request: Request):
        import asyncio
        data = await request.json()
        rack_type = data.get("rack_type", "all")
        tip_state_file = BASE_DIR / "SiLA2" / "OpentronsSiLA2Server" / "tip_state.json"

        try:
            tip_state = {}
            if tip_state_file.exists():
                tip_state = json.loads(tip_state_file.read_text(encoding='utf-8'))

            if rack_type.lower() == "all":
                for key in tip_state:
                    tip_state[key] = 0
                message = "All tip racks refilled"
                tips_restored = sum(96 for _ in tip_state)
            else:
                tip_state[rack_type] = 0
                message = f"Tip rack {rack_type} refilled"
                tips_restored = 96

            tip_state_file.write_text(json.dumps(tip_state, indent=2), encoding='utf-8')
            state.add_log("info", f"Tip rack refilled: {rack_type}", "operator")

            await ws_manager.broadcast({"type": "tip_refill_complete", "rack_type": rack_type,
                                        "message": message, "tips_restored": tips_restored})

            try:
                import grpc
                from src.pnp_stubs import OpentronsService_pb2 as pb2
                from src.pnp_stubs import OpentronsService_pb2_grpc as pb2_grpc
                host, port = _load_server_endpoint(BASE_DIR, "opentrons", "localhost", 50302)
                async with grpc.aio.insecure_channel(f'{host}:{port}') as channel:
                    stub = pb2_grpc.OpentronsServiceStub(channel)
                    req = pb2.RefillTipRackRequest(rack_id=rack_type)
                    await asyncio.wait_for(stub.RefillTipRack(req), timeout=2.0)
            except Exception:
                pass

            return {"success": True, "message": message, "tips_restored": tips_restored}
        except Exception as e:
            state.add_log("error", f"RefillTipRack error: {e}", "operator")
            return {"success": False, "error": str(e)}

    @router.get("/api/opentrons/tip-status")
    async def get_tip_status(hal_config: Optional[str] = None):
        try:
            tip_state_file = BASE_DIR / "SiLA2" / "OpentronsSiLA2Server" / "tip_state.json"
            tip_usage = json.loads(tip_state_file.read_text(encoding='utf-8')) if tip_state_file.exists() else {}

            hal_dir = BASE_DIR / "Library" / "HardwareConfig"
            hal_file = None

            if hal_config:
                candidate = hal_dir / f"{hal_config}.json"
                hal_file = candidate if candidate.exists() else hal_dir / hal_config

            if not hal_file or not hal_file.exists():
                hal_files = sorted(hal_dir.glob("Generic_HAL_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                if not hal_files:
                    hal_files = sorted(hal_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                hal_file = hal_files[0] if hal_files else None

            tip_racks = {}
            if hal_file and hal_file.exists():
                hal_data = json.loads(hal_file.read_text(encoding='utf-8'))
                for logical_name, lw_config in hal_data.get("Labware", {}).items():
                    load_name = lw_config.get("LoadName", "")
                    if "tiprack" not in load_name.lower() and "tip" not in load_name.lower():
                        continue
                    used = tip_usage.get(load_name, 0)
                    max_tips = 384 if "384" in load_name else 96
                    available = max_tips - used
                    tip_racks[logical_name] = {
                        "logical_name": logical_name, "load_name": load_name,
                        "slot": lw_config.get("Slot", "?"),
                        "display_name": lw_config.get("DisplayName", logical_name),
                        "used": used, "available": available, "max": max_tips,
                        "percent_available": round((available / max_tips) * 100, 1)
                    }

            return {"success": True, "status": tip_racks,
                    "hal_file": hal_file.name if hal_file else None, "raw": tip_usage}
        except Exception as e:
            return {"success": False, "error": str(e), "status": {}}

    return router


# Plate-tracking helpers (shared with workflows router)

def _auto_track_tecan(actual_id, command, params, result, plate_tracking, state):
    if "tecan" not in actual_id.lower() or command not in ["RunMeasurement", "RunAnalysis"]:
        return
    plate_id = params.get("plate_id") or params.get("plateId") or params.get("PlateId")
    if not plate_id:
        return
    if plate_id not in plate_tracking:
        plate_tracking[plate_id] = {"created": datetime.now().isoformat(), "status": "analyzed", "analysis_results": []}
    result_data = result.data if isinstance(result.data, dict) else {}
    plate_tracking[plate_id]["analysis_results"].append({
        "timestamp": datetime.now().isoformat(),
        "measurement_type": params.get("measurement_type", "spectroscopy"),
        "protocol": params.get("protocol") or params.get("protocol_file", ""),
        "result_file": result_data.get("animl_file_path", "") or result_data.get("excel_file_path", ""),
        "command": command, "instrument": actual_id, "raw_result": result_data
    })
    plate_tracking[plate_id]["status"] = "analyzed"
    state.add_log("info", f"Analysis result linked to plate {plate_id}", "plates")


def _auto_track_opentrons(actual_id, command, params, result, plate_tracking, state):
    if "opentrons" not in actual_id.lower() or command not in ["ExecuteRecipe", "RunRecipe", "run_recipe"]:
        return
    plate_id = params.get("plate_id") or params.get("PlateId")
    recipe_name = params.get("recipe") or params.get("recipe_name") or params.get("RecipeName", "")
    if not plate_id and recipe_name:
        plate_id = f"PLATE-{datetime.now().strftime('%Y%m%d')}-{recipe_name.replace('.json', '')}"
    if not plate_id:
        return
    if plate_id not in plate_tracking:
        plate_tracking[plate_id] = {
            "created": datetime.now().isoformat(), "status": "pipetted", "analysis_results": [],
            "pipetting_info": {"timestamp": datetime.now().isoformat(), "recipe": recipe_name,
                               "instrument": actual_id, "command": command}
        }
    else:
        plate_tracking[plate_id]["status"] = "pipetted"
        plate_tracking[plate_id]["pipetting_info"] = {
            "timestamp": datetime.now().isoformat(), "recipe": recipe_name,
            "instrument": actual_id, "command": command
        }
    state.add_log("info", f"Pipetting recorded for plate {plate_id}", "plates")
