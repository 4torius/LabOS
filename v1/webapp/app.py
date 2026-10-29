# -*- coding: utf-8 -*-
"""LabOS Integrated Web Application — app shell and HTML pages."""

import asyncio
import json
import logging
import os
import sys
import tempfile
import uuid
import yaml
from datetime import datetime
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent))

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

FASTAPI_AVAILABLE = True

try:
    from src.lab_core import get_lab_core, InterventionRequest, InterventionAction
    LAB_CORE_AVAILABLE = True
except (ImportError, NameError) as _err:
    logging.warning(f"LabCore not available at startup: {_err}")
    get_lab_core = None
    InterventionAction = None
    LAB_CORE_AVAILABLE = False

try:
    from src import database as _db
    _DB_AVAILABLE = True
except ImportError:
    _db = None
    _DB_AVAILABLE = False

from webapp.models import DeviceState, AppState
from src.config_schema import load_lab_config, validate_lab_config, _deep_merge
from src.settings import get_settings, reload_settings

logger = logging.getLogger(__name__)

# ==============================================================================
# Paths and configuration
# ==============================================================================

BASE_DIR = Path(__file__).parent.parent
TEMPLATES_DIR = BASE_DIR / "webapp" / "templates"
STATIC_DIR = BASE_DIR / "webapp" / "static"
LIBRARY_DIR = BASE_DIR / "Library"
RESULTS_DIR = BASE_DIR / "Results"

TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "css").mkdir(exist_ok=True)
(STATIC_DIR / "js").mkdir(exist_ok=True)

# ==============================================================================
# WebSocket manager
# ==============================================================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        dead = []
        for conn in list(self.active_connections):
            try:
                await conn.send_json(message)
            except Exception:
                dead.append(conn)
        for conn in dead:
            self.disconnect(conn)

# ==============================================================================
# Application factory
# ==============================================================================

def _static_version(static_dir: Path) -> str:
    """A token that changes when any static file does.

    Every page links the shared scripts by a bare path, so a browser that has
    seen them once keeps its copy. That is how a template calling a function
    added to labos-params.js an hour earlier met a cached file that did not
    have it: the call threw, renderProperties() never re-ran, and the workflow
    builder went on showing the previous command's parameters while its own
    dropdown said something else. Nothing on the page looked broken.

    The newest modification time under static/, as a compact string: computed
    once at startup, so a changed script is fetched and an unchanged one still
    comes from cache.
    """
    newest = 0.0
    try:
        for path in static_dir.rglob("*"):
            if path.is_file():
                newest = max(newest, path.stat().st_mtime)
    except OSError:
        pass
    return str(int(newest))


def create_app() -> "FastAPI":
    if not FASTAPI_AVAILABLE:
        raise ImportError("FastAPI not installed")

    state = AppState()
    ws_manager = ConnectionManager()

    if _db is not None:
        _db.configure(BASE_DIR / "Results" / "labos.db")
        _db.init_db()

    # Holds a strong reference to the fire-and-forget startup discovery task so
    # asyncio doesn't garbage-collect it mid-execution.
    _background_tasks: set = set()

    async def _run_startup_discovery():
        """Populate state.devices via PnP discovery in the background so a slow
        or unreachable configured server can't delay uvicorn accepting connections."""
        if not lab_core:
            return
        try:
            instruments = await lab_core.discover(
                timeout=get_settings(BASE_DIR).webapp.timeouts.discovery_timeout
            )
            for inst in instruments:
                state.devices[inst.id] = DeviceState(
                    id=inst.id, name=inst.name, type=inst.type,
                    status=inst.status, host=inst.host, port=inst.port
                )
            state.add_log("info", f"PnP Discovery: found {len(instruments)} instruments")
        except Exception as e:
            state.add_log("warning", f"Startup discovery failed: {e}")

    # Property values pushed by the instruments, rather than polled for. The
    # watcher owns the subscriptions; the webapp just forwards what it reports.
    property_watcher = None

    def _on_property_change(entry):
        state.devices  # noqa: B018 - keep the closure's intent obvious
        return ws_manager.broadcast({"type": "property_update", **entry.to_dict()})

    # Tell open pages when the set of instruments changes - a server switched
    # on, switched off, or one nobody had seen before - so a new instrument
    # appears in the dashboard and the workflow builder without a reload.
    # Discovery reports on every cache refresh; only a real change is sent.
    _instrument_signature: dict = {"value": None}

    def _on_discovery_update(instruments):
        signature = sorted((i.id, i.status, len(i.commands)) for i in instruments)
        if signature == _instrument_signature["value"]:
            return
        first = _instrument_signature["value"] is None
        _instrument_signature["value"] = signature
        for inst in instruments:
            state.devices[inst.id] = DeviceState(
                id=inst.id, name=inst.name, type=inst.type,
                status=inst.status, host=inst.host, port=inst.port)
        if first:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(ws_manager.broadcast({
            "type": "instruments_changed",
            "instruments": [{"id": i.id, "name": i.name, "status": i.status} for i in instruments],
        }))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.add_log("info", "LabOS WebApp started")
        try:
            _, validation = load_lab_config(BASE_DIR / "lab_config.yaml", apply_defaults=True, strict=True)
            for warning in validation.warnings:
                state.add_log("warning", f"Config warning: {warning}", "system")
        except Exception as e:
            state.add_log("error", f"Invalid configuration: {e}", "system")
            raise
        discovery_task = asyncio.create_task(_run_startup_discovery())
        _background_tasks.add(discovery_task)
        discovery_task.add_done_callback(_background_tasks.discard)

        nonlocal property_watcher
        if lab_core:
            from src.property_watch import PropertyWatcher
            property_watcher = PropertyWatcher(lab_core, _on_property_change)
            await property_watcher.start()
        try:
            yield
        finally:
            if property_watcher is not None:
                await property_watcher.stop()

    app = FastAPI(title="LabOS WebApp", description="Integrated Lab Automation Platform", version="2.0.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    # Shared mutable state passed to routers via closure/factory
    pending_operator_actions: list = []
    pending_operator_actions_lock = asyncio.Lock()
    pending_interventions: Dict[str, asyncio.Future] = {}

    lab_core = None
    if LAB_CORE_AVAILABLE and get_lab_core:
        lab_core = get_lab_core(BASE_DIR)
        assert InterventionAction is not None

        async def webapp_intervention_handler(request: InterventionRequest):
            intervention_id = str(uuid.uuid4())
            future = asyncio.get_event_loop().create_future()
            pending_interventions[intervention_id] = future
            await ws_manager.broadcast({
                "type": "intervention_required",
                "intervention_id": intervention_id,
                "workflow_name": request.workflow_name,
                "step_number": request.step_number,
                "instrument": request.instrument,
                "action": request.action,
                "error_message": request.error.message,
                "error_category": request.error.category.value,
                "options": ["retry", "skip", "abort"]
            })
            state.add_log("warning", f"Intervention required for step {request.step_number}: {request.error.message}", "workflow")
            try:
                return await asyncio.wait_for(
                    future,
                    timeout=get_settings(BASE_DIR).webapp.timeouts.intervention_timeout,
                )
            except asyncio.TimeoutError:
                state.add_log("error", "Intervention timed out, aborting", "workflow")
                return InterventionAction.ABORT  # type: ignore[union-attr]
            finally:
                pending_interventions.pop(intervention_id, None)

        lab_core.set_intervention_callback(webapp_intervention_handler)
        lab_core.set_callbacks(on_discovery_update=_on_discovery_update)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # Available to every template without each route passing it along.
    templates.env.globals["static_version"] = _static_version(STATIC_DIR)
    # Work around Jinja2 cache key issues when globals include unhashable objects.
    templates.env.cache = None
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # --------------------------------------------------------------------------
    # HTML pages
    # --------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return templates.TemplateResponse(request, "index.html", {
            "request": request, "title": "LabOS", "devices": list(state.devices.values())
        })

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return templates.TemplateResponse(request, "dashboard.html", {
            "request": request, "title": "Dashboard", "devices": list(state.devices.values())
        })

    @app.get("/workflows", response_class=HTMLResponse)
    async def workflows_page(request: Request):
        return templates.TemplateResponse(request, "workflows.html", {
            "request": request, "title": "Workflows",
            "nodered_url": get_settings(BASE_DIR).webapp.nodered_url
        })

    # ── Results: whatever folders the instruments declare ────────────────────
    #
    # This page used to know four folder names - CSV, Excel, AnIML, XML - which
    # are the Tecan bridge's, plus a separate deck-photos route for the
    # Opentrons camera. An instrument writing anywhere else produced data the
    # web interface could not show. Each server now declares its result folders
    # (src/catalog.py); the page lists those, and any other folder that exists.

    _IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}

    def _photos_last(folders: list) -> list:
        """Measurement data first; a folder that only holds pictures goes last."""
        def only_images(entry):
            exts = {e.lower() if e.startswith(".") else "." + e.lower()
                    for e in entry.get("file_extensions") or []}
            return bool(exts) and exts <= _IMAGE_SUFFIXES
        return sorted(folders, key=only_images)

    async def _result_folders() -> list:
        return _photos_last(await _all_result_folders())

    async def _all_result_folders() -> list:
        results_dir = get_settings(BASE_DIR).results_dir
        if lab_core:
            try:
                await lab_core.discover()
                return lab_core.list_result_folders()
            except Exception as exc:
                logger.warning(f"Could not read declared result folders: {exc}")
        # No LabCore: the folders that exist are still worth showing.
        return [{"name": d.name, "display_name": d.name, "description": "",
                 "file_extensions": [], "declared_by": [], "declared": False,
                 "exists": True}
                for d in sorted(results_dir.iterdir())
                if d.is_dir() and d.name not in ("runs", "__pycache__") and not d.name.startswith(".")
                ] if results_dir.is_dir() else []

    def _result_url(folder: str, name: str) -> str:
        from urllib.parse import quote
        return f"/api/results/{quote(folder)}/{quote(name)}"

    def _files_in(folder: str) -> list:
        directory = get_settings(BASE_DIR).results_dir / folder
        if not directory.is_dir():
            return []
        files = []
        for f in directory.iterdir():
            if not f.is_file() or f.name.startswith("."):
                continue
            suffix = f.suffix.lower()
            stat = f.stat()
            files.append({
                "name": f.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "kind": ("image" if suffix in _IMAGE_SUFFIXES
                         else "animl" if suffix == ".animl" else "file"),
                "url": _result_url(folder, f.name),
            })
        return sorted(files, key=lambda item: item["modified"], reverse=True)

    async def _result_file(folder: str, filename: str) -> Path:
        """A file inside a known result folder, or an HTTP error."""
        known = {entry["name"] for entry in await _result_folders()}
        if folder not in known:
            raise HTTPException(404, f"No result folder '{folder}'")
        results_dir = get_settings(BASE_DIR).results_dir.resolve()
        file_path = (results_dir / folder / filename).resolve()
        try:
            file_path.relative_to(results_dir / folder)
        except ValueError:
            raise HTTPException(403, "Access denied")
        if not file_path.is_file():
            raise HTTPException(404, "File not found")
        return file_path

    @app.get("/results", response_class=HTMLResponse)
    async def results_page(request: Request):
        folders = await _result_folders()
        for entry in folders:
            entry["files"] = _files_in(entry["name"])
        return templates.TemplateResponse(request, "results.html", {
            "request": request, "title": "Results", "folders": folders})

    @app.get("/api/results/folders")
    async def list_result_folders():
        """The Results folders, who declared each, and the files in them."""
        folders = await _result_folders()
        for entry in folders:
            entry["files"] = _files_in(entry["name"])
        return {"folders": folders}

    @app.get("/api/results/{folder:path}/{filename}/view")
    async def view_result(folder: str, filename: str):
        file_path = await _result_file(folder, filename)
        try:
            return {"filename": filename, "folder": folder, "format": folder,
                    "content": file_path.read_text(encoding="utf-8-sig"), "size": file_path.stat().st_size}
        except UnicodeDecodeError:
            return {"filename": filename, "folder": folder, "format": folder,
                    "content": "(Binary file - download to view)",
                    "size": file_path.stat().st_size, "binary": True}

    @app.get("/api/results/{folder:path}/{filename}/animl")
    async def parse_animl_in_folder(folder: str, filename: str):
        """Structured measurement data from an AnIML file in any result folder."""
        file_path = await _result_file(folder, filename)
        try:
            from src.animl_parser import parse_animl
            return parse_animl(file_path)
        except Exception as e:
            raise HTTPException(500, f"Parse error: {e}")

    @app.get("/api/properties")
    async def get_property_snapshot(instrument_id: str = ""):
        """Every instrument property value currently known.

        A page loading mid-session reads this instead of waiting for the next
        change, and instead of firing one HTTP request per property.
        """
        if property_watcher is None:
            return {"properties": [], "watching": False}
        return {
            "properties": property_watcher.snapshot(instrument_id or None),
            "watching": True,
        }

    @app.get("/api/images")
    async def list_deck_images():
        """Images captured from the robot's on-board camera.

        CaptureImage writes here. Before, captured frames landed in the
        Opentrons server's own ./images directory, which no route served —
        taken and then invisible.
        """
        images_dir = RESULTS_DIR / "Images"
        if not images_dir.exists():
            return {"images": []}
        files = sorted(
            ({"name": f.name,
              "url": f"/api/images/{f.name}",
              "size": f.stat().st_size,
              "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat()}
             for f in images_dir.iterdir()
             if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png")),
            key=lambda i: i["modified"], reverse=True,
        )
        return {"images": files}

    @app.get("/api/images/{filename}")
    async def get_deck_image(filename: str):
        file_path = RESULTS_DIR / "Images" / filename
        if not file_path.exists():
            raise HTTPException(404, "Image not found")
        try:
            file_path.resolve().relative_to((RESULTS_DIR / "Images").resolve())
        except ValueError:
            raise HTTPException(403, "Access denied")
        media = "image/png" if file_path.suffix.lower() == ".png" else "image/jpeg"
        return FileResponse(path=str(file_path), media_type=media)

    @app.get("/api/files/animl_results")
    async def list_animl_result_files():
        """List available AnIML result files for the plate analysis browser."""
        animl_dir = RESULTS_DIR / "AnIML"
        if not animl_dir.exists():
            return {"files": []}
        files = sorted(
            [{"name": f.name, "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat()}
             for f in animl_dir.glob("*.animl") if f.is_file()],
            key=lambda x: x["modified"], reverse=True
        )
        return {"files": files}

    @app.get("/api/results/animl/{filename}/parse")
    async def parse_animl_result(filename: str):
        """Parse an AnIML file and return structured measurement data for visualization."""
        file_path = RESULTS_DIR / "AnIML" / filename
        if not file_path.exists():
            raise HTTPException(404, "AnIML file not found")
        try:
            file_path.resolve().relative_to(RESULTS_DIR.resolve())
        except ValueError:
            raise HTTPException(403, "Access denied")
        try:
            from src.animl_parser import parse_animl
            return parse_animl(file_path)
        except Exception as e:
            raise HTTPException(500, f"Parse error: {e}")

    # Last of the /api/results routes: `{folder:path}` would otherwise swallow
    # the /view, /animl and /parse routes above it.
    @app.get("/api/results/{folder:path}/{filename}")
    async def download_result(folder: str, filename: str):
        file_path = await _result_file(folder, filename)
        return FileResponse(path=str(file_path), filename=filename, media_type="application/octet-stream")

    @app.get("/hardware", response_class=HTMLResponse)
    async def hardware_page(request: Request):
        return templates.TemplateResponse(request, "hardware.html", {"request": request, "title": "Hardware"})

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        return templates.TemplateResponse(request, "settings.html", {"request": request, "title": "Settings"})

    @app.get("/operator", response_class=HTMLResponse)
    async def operator_page(request: Request):
        return templates.TemplateResponse(request, "operator.html", {"request": request, "title": "Operator"})

    @app.get("/batch", response_class=HTMLResponse)
    @app.get("/recipes", response_class=HTMLResponse)
    async def recipe_generator_page(request: Request):
        return templates.TemplateResponse(request, "recipe_generator.html", {"request": request, "title": "Recipe Generator"})

    # "Database" page: unified Plates (chemical record) + Runs & Log
    # (automation/execution log) view with tabs. /plates and /runs are kept
    # as redirects so any saved links/bookmarks still work.
    @app.get("/database", response_class=HTMLResponse)
    async def database_page(request: Request):
        return templates.TemplateResponse(request, "database.html", {"request": request, "title": "Database"})

    @app.get("/plates")
    async def plates_page_redirect():
        return RedirectResponse(url="/database#plates")

    @app.get("/runs")
    async def runs_page_redirect():
        return RedirectResponse(url="/database#runs")

    # --------------------------------------------------------------------------
    # Settings API
    # --------------------------------------------------------------------------

    @app.get("/api/config")
    async def get_config():
        config_path = BASE_DIR / "lab_config.yaml"
        if not config_path.exists():
            raise HTTPException(404, "Config file not found")
        config, validation = load_lab_config(config_path, apply_defaults=False, strict=False)
        if not validation.ok:
            logger.warning(f"Invalid config loaded via API: {'; '.join(validation.errors)}")
        for warning in validation.warnings:
            logger.warning(f"Config warning via API: {warning}")
        return config

    @app.post("/api/config")
    async def save_config(request: Request):
        try:
            new_config = await request.json()
            config_path = BASE_DIR / "lab_config.yaml"
            existing, _ = load_lab_config(config_path, apply_defaults=False, strict=False)
            for section in ['servers', 'system', 'discovery', 'workflow', 'error_handling', 'webapp', 'paths', 'ui_dropdowns']:
                if section in new_config:
                    # Deep merge (not .update()): a partial nested payload — e.g. only
                    # {"mobile": {"host": "..."}} inside "servers" — must not silently
                    # drop sibling keys that .update() would leave untouched at the
                    # top level but that a shallow merge loses one level down.
                    if (
                        section in existing
                        and isinstance(existing[section], dict)
                        and isinstance(new_config[section], dict)
                    ):
                        existing[section] = _deep_merge(existing[section], new_config[section])
                    else:
                        existing[section] = new_config[section]

            validation = validate_lab_config(existing)
            if not validation.ok:
                raise HTTPException(400, {
                    "message": "Invalid configuration",
                    "errors": validation.errors,
                    "warnings": validation.warnings,
                })

            # Atomic write: lab_config.yaml is the single source of truth PnP
            # discovery bootstraps from — a crash mid-write must not corrupt it.
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(config_path.parent), prefix=".lab_config_", suffix=".yaml.tmp"
            )
            try:
                with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                    yaml.dump(existing, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
                os.replace(tmp_path, config_path)
                # Invalidate the cached snapshot so the new values take effect
                # without a restart.
                reload_settings(BASE_DIR)
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
            return {
                "status": "ok",
                "message": "Configuration saved",
                "warnings": validation.warnings,
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"Failed to save config: {e}")

    @app.post("/api/network/test")
    async def test_network_connection(request: Request):
        import socket
        import time
        data = await request.json()
        host = data.get("host", "")
        port = data.get("port", 50053)
        timeout = data.get("timeout", 5.0)
        if not host:
            return {"reachable": False, "error": "No host specified"}
        result = {"host": host, "port": port, "reachable": False, "latency_ms": None, "error": None, "server_name": None}
        start = time.time()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.close()
            result["latency_ms"] = round((time.time() - start) * 1000, 1)
            result["reachable"] = True
        except socket.timeout:
            result["error"] = f"Connection timed out after {timeout}s"
            return result
        except Exception as e:
            result["error"] = str(e)
            return result
        try:
            import grpc
            grpc.channel_ready_future(grpc.insecure_channel(f"{host}:{port}")).result(timeout=2.0)
            result["server_name"] = f"SiLA2 Server at {host}:{port}"
        except Exception:
            pass
        return result

    # Note: the dedicated History page/API (/history, /api/logs, /api/history)
    # was removed - it was a pure viewer over ephemeral in-memory AppState
    # logs/command_history with no other consumer. state.add_log()/
    # add_command() themselves are kept (used ~79 places for their side
    # effect: in-memory ring buffer + WebSocket broadcast for live dashboard
    # updates), only the browsing UI/API surface is gone.

    # --------------------------------------------------------------------------
    # Routers (extracted modules)
    # --------------------------------------------------------------------------

    from webapp.routes.batch import create_batch_router
    from webapp.routes.instruments import create_instruments_router
    from webapp.routes.workflows import create_workflows_router
    from webapp.routes.operator import create_operator_router
    from webapp.routes.hardware import create_hardware_router
    from webapp.routes.db import create_db_router

    app.include_router(create_batch_router(state, LIBRARY_DIR))
    app.include_router(create_instruments_router(state, ws_manager, lab_core, BASE_DIR, LIBRARY_DIR))
    app.include_router(create_workflows_router(
        state, ws_manager, lab_core,
        pending_operator_actions, pending_operator_actions_lock, pending_interventions,
        BASE_DIR, LIBRARY_DIR
    ))
    app.include_router(create_operator_router(state, ws_manager, lab_core, pending_operator_actions, pending_operator_actions_lock, BASE_DIR))
    app.include_router(create_hardware_router(BASE_DIR, LIBRARY_DIR, lab_core))
    if _DB_AVAILABLE:
        app.include_router(create_db_router(_db, RESULTS_DIR, state))

    # --------------------------------------------------------------------------
    # WebSocket
    # --------------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await ws_manager.connect(websocket)
        try:
            while True:
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_text("pong")
        except WebSocketDisconnect:
            ws_manager.disconnect(websocket)

    return app


# ==============================================================================
# HTML Templates (fallback generator — only if templates/ are missing)
# ==============================================================================

def create_templates():
    """Write fallback HTML templates when templates/ directory is empty."""
    base_html = '''<!DOCTYPE html>
<html lang="en" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }} - LabOS</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.1/font/bootstrap-icons.css" rel="stylesheet">
    <style>
        :root { --labos-primary: #667eea; --labos-secondary: #764ba2; }
        .navbar-brand { font-weight: bold; background: linear-gradient(135deg, var(--labos-primary), var(--labos-secondary)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .sidebar { position: fixed; top: 56px; bottom: 0; left: 0; width: 250px; padding: 20px; background: var(--bs-body-bg); border-right: 1px solid var(--bs-border-color); }
        .main-content { margin-left: 250px; padding: 20px; min-height: calc(100vh - 56px); }
        .device-card { transition: transform 0.2s, box-shadow 0.2s; }
        .device-card:hover { transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
        .status-online { color: #28a745; } .status-offline { color: #dc3545; } .status-busy { color: #ffc107; }
        .nav-link.active { background: linear-gradient(135deg, var(--labos-primary), var(--labos-secondary)); border-radius: 8px; }
        .nodered-frame { width: 100%; height: calc(100vh - 150px); border: none; border-radius: 8px; }
    </style>
    {% block extra_css %}{% endblock %}
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark bg-dark fixed-top">
        <div class="container-fluid">
            <a class="navbar-brand" href="/"><i class="bi bi-cpu"></i> LabOS</a>
            <div class="navbar-nav ms-auto">
                <span class="nav-item nav-link text-muted"><i class="bi bi-circle-fill text-success"></i> System Online</span>
            </div>
        </div>
    </nav>
    <div class="sidebar">
        <nav class="nav flex-column">
            <a class="nav-link {% if title == \'Dashboard\' %}active{% endif %}" href="/dashboard"><i class="bi bi-speedometer2"></i> Dashboard</a>
            <a class="nav-link {% if title == \'Workflows\' %}active{% endif %}" href="/workflows"><i class="bi bi-diagram-3"></i> Workflows</a>
            <a class="nav-link {% if title == \'Results\' %}active{% endif %}" href="/results"><i class="bi bi-file-earmark-bar-graph"></i> Results</a>
            <a class="nav-link {% if title == \'Settings\' %}active{% endif %}" href="/settings"><i class="bi bi-gear"></i> Settings</a>
            <hr>
            <a class="nav-link text-muted" href="http://localhost:1880" target="_blank"><i class="bi bi-box-arrow-up-right"></i> Node-RED</a>
            <a class="nav-link text-muted" href="/docs" target="_blank"><i class="bi bi-book"></i> API Docs</a>
        </nav>
    </div>
    <main class="main-content">{% block content %}{% endblock %}</main>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        const ws = new WebSocket(`ws://${window.location.host}/ws`);
        ws.onmessage = e => { const d = JSON.parse(e.data); if (d.type === \'device_update\') { const c = document.querySelector(`[data-device-id="${d.device_id}"]`); if (c) { const s = c.querySelector(\'.device-status\'); if (s) { s.className = `device-status status-${d.status}`; s.innerHTML = `<i class="bi bi-circle-fill"></i> ${d.status}`; } } } };
        setInterval(() => ws.readyState === 1 && ws.send(\'ping\'), 30000);
    </script>
    {% block extra_js %}{% endblock %}
</body>
</html>'''

    index_html = '''{% extends "base.html" %}
{% block content %}
<div class="container-fluid">
    <h2 class="mb-4">Welcome to LabOS</h2>
    <div class="row g-4">
        <div class="col-md-6 col-lg-3"><div class="card bg-primary text-white"><div class="card-body"><h5><i class="bi bi-hdd-stack"></i> Devices</h5><h2>{{ devices|length }}</h2><small>Connected instruments</small></div></div></div>
        <div class="col-md-6 col-lg-3"><div class="card bg-success text-white"><div class="card-body"><h5><i class="bi bi-check-circle"></i> Status</h5><h2>Online</h2><small>System operational</small></div></div></div>
    </div>
    <div class="row mt-5"><div class="col-12"><div class="btn-group">
        <a href="/dashboard" class="btn btn-outline-primary btn-lg"><i class="bi bi-speedometer2"></i> Dashboard</a>
        <a href="/workflows" class="btn btn-outline-success btn-lg"><i class="bi bi-diagram-3"></i> Workflows</a>
    </div></div></div>
</div>
{% endblock %}'''

    dashboard_html = '''{% extends "base.html" %}
{% block content %}
<div class="container-fluid">
    <div class="d-flex justify-content-between align-items-center mb-4">
        <h2><i class="bi bi-speedometer2"></i> Device Dashboard</h2>
        <button class="btn btn-outline-primary" onclick="refreshDevices()"><i class="bi bi-arrow-clockwise"></i> Refresh</button>
    </div>
    <div class="row g-4" id="devices-container"></div>
</div>
{% endblock %}
{% block extra_js %}
<script>
async function refreshDevices() {
    const devices = await fetch('/api/devices').then(r => r.json());
    document.getElementById('devices-container').innerHTML = devices.map(d => `
        <div class="col-md-6 col-lg-4"><div class="card device-card" data-device-id="${d.id}">
            <div class="card-header d-flex justify-content-between">
                <span><i class="bi bi-cpu"></i> ${d.name}</span>
                <span class="device-status status-${d.status}"><i class="bi bi-circle-fill"></i> ${d.status}</span>
            </div>
            <div class="card-body"><p class="text-muted"><small>${d.host}:${d.port}</small></p></div>
        </div></div>`).join('');
}
refreshDevices();
</script>
{% endblock %}'''

    workflows_html = '''{% extends "base.html" %}
{% block content %}
<div class="container-fluid">
    <div class="d-flex justify-content-between align-items-center mb-4">
        <h2><i class="bi bi-diagram-3"></i> Visual Workflow Editor</h2>
        <a href="{{ nodered_url }}" target="_blank" class="btn btn-outline-secondary"><i class="bi bi-box-arrow-up-right"></i> Open in New Tab</a>
    </div>
    <div class="card"><div class="card-body p-0">
        <iframe src="{{ nodered_url }}" class="nodered-frame"></iframe>
    </div></div>
</div>
{% endblock %}'''

    results_html = '''{% extends "base.html" %}
{% block content %}
<div class="container-fluid">
    <h2 class="mb-4"><i class="bi bi-file-earmark-bar-graph"></i> Results</h2>
    <div class="table-responsive"><table class="table table-hover"><thead><tr><th>File</th><th>Format</th><th>Size</th><th>Modified</th></tr></thead>
    <tbody>{% for r in results %}<tr><td>{{ r.name }}</td><td>{{ r.format }}</td><td>{{ (r.size/1024)|round(1) }} KB</td><td>{{ r.modified[:19] }}</td></tr>{% endfor %}</tbody>
    </table></div>
</div>
{% endblock %}'''

    settings_html = '''{% extends "base.html" %}
{% block content %}
<div class="container-fluid">
    <h2 class="mb-4"><i class="bi bi-gear"></i> Settings</h2>
    <div class="card"><div class="card-body"><p><strong>Version:</strong> 2.0.0</p><p><strong>Architecture:</strong> Plug & Play SiLA2</p></div></div>
</div>
{% endblock %}'''

    templates = {
        "base.html": base_html, "index.html": index_html, "dashboard.html": dashboard_html,
        "workflows.html": workflows_html, "results.html": results_html, "settings.html": settings_html
    }
    for name, content in templates.items():
        path = TEMPLATES_DIR / name
        if not path.exists():
            path.write_text(content, encoding='utf-8')
    print(f"Created {len(templates)} fallback templates in {TEMPLATES_DIR}")


# ==============================================================================
# Entry point
# ==============================================================================

app = create_app() if FASTAPI_AVAILABLE else None


def main():
    import argparse

    # When stdout/stderr aren't a real Windows console (redirected to a file,
    # piped, run as a service), Python falls back to the system ANSI codepage
    # (cp1252) instead of UTF-8 — the banner's box-drawing characters below
    # then raise UnicodeEncodeError and crash the process before uvicorn even
    # starts. reconfigure() makes startup output resilient regardless of how
    # the process is launched.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    parser = argparse.ArgumentParser(description="LabOS WebApp")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--create-templates", action="store_true")
    args = parser.parse_args()

    if args.create_templates or not (TEMPLATES_DIR / "base.html").exists():
        create_templates()

    print(f"""
╔══════════════════════════════════════════════╗
║         LabOS WebApp v2.0                    ║
╠══════════════════════════════════════════════╣
║  WebApp:    http://localhost:{args.port}          ║
║  API Docs:  http://localhost:{args.port}/docs     ║
╚══════════════════════════════════════════════╝""")

    if app is None:
        print("ERROR: FastAPI not available.")
        return
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
