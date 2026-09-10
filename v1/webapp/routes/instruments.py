"""Instrument, device, and file API routes."""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.config_schema import load_lab_config as _load_lab_config_schema
from src import plate_tracking


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

    @router.get("/api/devices/{device_id}/property/{feature}/{property_name}")
    async def get_device_property(device_id: str, feature: str, property_name: str):
        core = lab_core
        if not core:
            from src.lab_core import get_lab_core
            core = get_lab_core(BASE_DIR)

        await core.discover()
        result = await core.get_property(device_id, feature, property_name)
        if not result.success:
            return {"status": "error", "error": result.error}
        return {"status": "completed", "result": result.data}

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

                if command == "RefreshTasks":
                    await ws_manager.broadcast({"type": "mobile_tasks_updated"})

                # Plate ID assignment + DB population (Database page: Plates tab).
                # Same shared logic the workflow-execution path uses, so a
                # manually-triggered Tecan measurement or Opentrons recipe
                # (no workflow running) is tracked identically instead of
                # only reaching the ephemeral command history.
                try:
                    plate_tracking.track_tecan_measurement(
                        actual_id, command, params, result,
                        run_id=None, current_plate_id=None, base_dir=BASE_DIR, state=state,
                    )
                    plate_tracking.track_opentrons_recipe(
                        actual_id, command, params, result,
                        run_id=None, base_dir=BASE_DIR, state=state,
                    )
                except Exception as exc:
                    logger.warning(f"Plate tracking failed for manual command {command}: {exc}")

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
                    data = json.loads(f.read_text(encoding='utf-8-sig'))
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
                data = json.loads(f.read_text(encoding='utf-8-sig'))
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
            data = json.loads(index_file.read_text(encoding='utf-8-sig'))
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
            data = json.loads(index_file.read_text(encoding='utf-8-sig'))
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
                    data = json.loads(f.read_text(encoding='utf-8-sig'))
                    name = data.get('name', f.stem)
                    emoji = {'Aqueous': '💧', 'Viscous': '🍯', 'HighlyViscous': '🍯🍯',
                             'Volatile': '💨', 'Foaming': '🫧'}.get(name, '🔬')
                    classes[name] = {'displayName': f"{emoji} {name}", 'description': data.get('description', '')}
                except Exception:
                    classes[f.stem] = {'displayName': f.stem, 'description': ''}
        return {"files": files, "classes": classes}

    @router.get("/api/tecan/protocols")
    async def get_tecan_protocols():
        protocols = [f.name for f in (LIBRARY_DIR / "Analysis").glob("*.mdfx")] if (LIBRARY_DIR / "Analysis").exists() else []
        return {"protocols": protocols}

    @router.get("/api/tecan/plate-types")
    async def get_tecan_plate_types():
        """Real Tecan-compatible plate catalog for the RunMeasurement `PlateType`
        parameter (Library/Analysis/PlateTypes.json - maps a plate id to the
        Tecan-internal {file, range} pair TecanBridge.RewritePlateType uses to
        override the plate hardcoded in a .mdfx). Passing anything not listed
        here fails on the bridge with a clear error, never a silent fallback -
        this endpoint is the same list, just exposed to the frontend so the
        dropdown can show real options instead of a generic placeholder."""
        path = LIBRARY_DIR / "Analysis" / "PlateTypes.json"
        if not path.exists():
            return {"plate_types": []}
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
        except Exception:
            return {"plate_types": []}
        plate_types = [
            {"id": key, "display_name": key.replace("_", " "), "tecan_file": val.get("file", "")}
            for key, val in data.items()
            if not key.startswith("_") and isinstance(val, dict)
        ]
        return {"plate_types": plate_types}

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
        data = await request.json()
        rack_type = data.get("rack_type", "all")

        # Delegate to the SiLA2 RefillTipRack command instead of writing
        # tip_state.json directly here: that file is also owned/written by the
        # SiLA2 server process's TipTracker (with its own lock + real rack
        # capacities), so a second, unlocked writer here risked a cross-process
        # race and always assumed 96 tips/rack even for 384-tip racks.
        core = lab_core
        if not core:
            from src.lab_core import get_lab_core
            core = get_lab_core(BASE_DIR)

        try:
            await core.discover()
            opentrons = next(
                (i for i in core.list_instruments() if "opentrons" in i.id.lower() or "opentrons" in i.name.lower()),
                None,
            )
            if opentrons is None:
                return {"success": False, "error": "Opentrons instrument not found"}

            result = await core.execute_command(opentrons.id, "RefillTipRack", {"RackSlot": rack_type})
            if not result.success:
                state.add_log("error", f"RefillTipRack error: {result.error}", "operator")
                return {"success": False, "error": result.error}

            message = "All tip racks refilled" if rack_type.lower() == "all" else f"Tip rack {rack_type} refilled"

            # Read-only (never write) tip_state.json just to report an accurate
            # count in the response/broadcast — TipTracker is the sole writer.
            tips_restored = 0
            tip_state_file = BASE_DIR / "SiLA2" / "OpentronsSiLA2Server" / "tip_state.json"
            if tip_state_file.exists():
                tip_state = json.loads(tip_state_file.read_text(encoding='utf-8-sig'))
                if rack_type.lower() == "all":
                    tips_restored = sum(384 if "384" in key else 96 for key in tip_state)
                else:
                    tips_restored = 384 if "384" in rack_type else 96

            state.add_log("info", f"Tip rack refilled: {rack_type}", "operator")
            await ws_manager.broadcast({"type": "tip_refill_complete", "rack_type": rack_type,
                                        "message": message, "tips_restored": tips_restored})

            return {"success": True, "message": message, "tips_restored": tips_restored}
        except Exception as e:
            state.add_log("error", f"RefillTipRack error: {e}", "operator")
            return {"success": False, "error": str(e)}

    @router.get("/api/opentrons/tip-status")
    async def get_tip_status(hal_config: Optional[str] = None):
        try:
            tip_state_file = BASE_DIR / "SiLA2" / "OpentronsSiLA2Server" / "tip_state.json"
            tip_usage = json.loads(tip_state_file.read_text(encoding='utf-8-sig')) if tip_state_file.exists() else {}

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
                hal_data = json.loads(hal_file.read_text(encoding='utf-8-sig'))
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
