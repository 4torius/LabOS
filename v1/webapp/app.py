# -*- coding: utf-8 -*-
"""LabOS Integrated Web Application — app shell and HTML pages."""

import asyncio
import json
import logging
import sys
import uuid
import yaml
from datetime import datetime
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, FileResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    print("FastAPI not installed. Run: pip install fastapi uvicorn jinja2 python-multipart")

try:
    from src.lab_core import get_lab_core, InterventionRequest, InterventionAction
    LAB_CORE_AVAILABLE = True
except (ImportError, NameError) as _err:
    logging.warning(f"LabCore not available at startup: {_err}")
    get_lab_core = None
    LAB_CORE_AVAILABLE = False

from webapp.models import DeviceState, AppState
from src.config_schema import load_lab_config, validate_lab_config

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

PLATE_TRACKING_FILE = BASE_DIR / "Results" / "plate_tracking.json"
_plate_tracking_lock = asyncio.Lock()


def load_webapp_config() -> dict:
    defaults = {
        "intervention_timeout": 3600,
        "manual_action_timeout": 300,
        "tip_refill_timeout": 600,
        "discovery_timeout": 1.0,
        "nodered_url": "http://localhost:1880",
    }
    try:
        config_path = BASE_DIR / "lab_config.yaml"
        config, validation = load_lab_config(config_path, apply_defaults=True, strict=False)
        for warning in validation.warnings:
            logger.warning(f"Config warning: {warning}")
        if not validation.ok:
            logger.error(f"Configuration errors: {'; '.join(validation.errors)}")
        webapp_cfg = config.get("webapp", {})
        timeouts = webapp_cfg.get("timeouts", {})
        return {**defaults, **timeouts, "nodered_url": webapp_cfg.get("nodered_url", defaults["nodered_url"])}
    except Exception as e:
        logger.warning(f"Failed to load webapp config: {e}")
    return defaults


def load_plate_tracking() -> dict:
    try:
        if PLATE_TRACKING_FILE.exists():
            return json.loads(PLATE_TRACKING_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        logger.warning(f"Failed to load plate tracking: {e}")
    return {}


async def save_plate_tracking(tracking: dict):
    async with _plate_tracking_lock:
        try:
            PLATE_TRACKING_FILE.parent.mkdir(parents=True, exist_ok=True)
            PLATE_TRACKING_FILE.write_text(json.dumps(tracking, indent=2), encoding='utf-8')
        except Exception as e:
            logger.error(f"Failed to save plate tracking: {e}")


WEBAPP_CONFIG = load_webapp_config()

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
        for conn in list(self.active_connections):
            try:
                await conn.send_json(message)
            except Exception:
                pass

# ==============================================================================
# Application factory
# ==============================================================================

def create_app() -> "FastAPI":
    if not FASTAPI_AVAILABLE:
        raise ImportError("FastAPI not installed")

    app = FastAPI(title="LabOS WebApp", description="Integrated Lab Automation Platform", version="2.0.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    state = AppState()
    ws_manager = ConnectionManager()

    # Shared mutable state passed to routers via closure/factory
    pending_operator_actions: list = []
    pending_operator_actions_lock = asyncio.Lock()
    pending_interventions: Dict[str, asyncio.Future] = {}

    lab_core = None
    if LAB_CORE_AVAILABLE and get_lab_core:
        lab_core = get_lab_core(BASE_DIR)

        async def webapp_intervention_handler(request: InterventionRequest) -> InterventionAction:
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
                return await asyncio.wait_for(future, timeout=float(WEBAPP_CONFIG["intervention_timeout"]))
            except asyncio.TimeoutError:
                state.add_log("error", "Intervention timed out, aborting", "workflow")
                return InterventionAction.ABORT
            finally:
                pending_interventions.pop(intervention_id, None)

        lab_core.set_intervention_callback(webapp_intervention_handler)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # --------------------------------------------------------------------------
    # HTML pages
    # --------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return templates.TemplateResponse("index.html", {
            "request": request, "title": "LabOS", "devices": list(state.devices.values())
        })

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return templates.TemplateResponse("dashboard.html", {
            "request": request, "title": "Dashboard", "devices": list(state.devices.values())
        })

    @app.get("/workflows", response_class=HTMLResponse)
    async def workflows_page(request: Request):
        return templates.TemplateResponse("workflows.html", {
            "request": request, "title": "Workflows",
            "nodered_url": WEBAPP_CONFIG.get("nodered_url", "http://localhost:1880")
        })

    @app.get("/results", response_class=HTMLResponse)
    async def results_page(request: Request):
        results = []
        for fmt in ["CSV", "Excel", "AnIML", "XML"]:
            fmt_dir = RESULTS_DIR / fmt
            if fmt_dir.exists():
                for f in fmt_dir.glob("*"):
                    if f.is_file():
                        results.append({
                            "name": f.name, "format": fmt,
                            "size": f.stat().st_size,
                            "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat()
                        })
        return templates.TemplateResponse("results.html", {"request": request, "title": "Results", "results": results})

    @app.get("/api/results/{fmt}/{filename}")
    async def download_result(fmt: str, filename: str):
        if fmt not in ["CSV", "Excel", "AnIML", "XML"]:
            raise HTTPException(400, "Invalid format")
        file_path = RESULTS_DIR / fmt / filename
        if not file_path.exists():
            raise HTTPException(404, "File not found")
        try:
            file_path.resolve().relative_to(RESULTS_DIR.resolve())
        except ValueError:
            raise HTTPException(403, "Access denied")
        return FileResponse(path=str(file_path), filename=filename, media_type="application/octet-stream")

    @app.get("/api/results/{fmt}/{filename}/view")
    async def view_result(fmt: str, filename: str):
        if fmt not in ["CSV", "Excel", "AnIML", "XML"]:
            raise HTTPException(400, "Invalid format")
        file_path = RESULTS_DIR / fmt / filename
        if not file_path.exists():
            raise HTTPException(404, "File not found")
        try:
            file_path.resolve().relative_to(RESULTS_DIR.resolve())
        except ValueError:
            raise HTTPException(403, "Access denied")
        try:
            return {"filename": filename, "format": fmt, "content": file_path.read_text(encoding="utf-8"), "size": file_path.stat().st_size}
        except UnicodeDecodeError:
            return {"filename": filename, "format": fmt, "content": "(Binary file - download to view)", "size": file_path.stat().st_size, "binary": True}

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

    @app.get("/hardware", response_class=HTMLResponse)
    async def hardware_page(request: Request):
        return templates.TemplateResponse("hardware.html", {"request": request, "title": "Hardware"})

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        return templates.TemplateResponse("settings.html", {"request": request, "title": "Settings"})

    @app.get("/operator", response_class=HTMLResponse)
    async def operator_page(request: Request):
        return templates.TemplateResponse("operator.html", {"request": request, "title": "Operator"})

    @app.get("/batch", response_class=HTMLResponse)
    @app.get("/recipes", response_class=HTMLResponse)
    async def recipe_generator_page(request: Request):
        return templates.TemplateResponse("recipe_generator.html", {"request": request, "title": "Recipe Generator"})

    @app.get("/plates", response_class=HTMLResponse)
    async def plates_page(request: Request):
        return templates.TemplateResponse("plates.html", {"request": request, "title": "Plates Tracking"})

    @app.get("/history", response_class=HTMLResponse)
    async def history_page(request: Request):
        return templates.TemplateResponse("history.html", {"request": request, "title": "History"})

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
                    if section in existing and isinstance(existing[section], dict):
                        existing[section].update(new_config[section])
                    else:
                        existing[section] = new_config[section]

            validation = validate_lab_config(existing)
            if not validation.ok:
                raise HTTPException(400, {
                    "message": "Invalid configuration",
                    "errors": validation.errors,
                    "warnings": validation.warnings,
                })

            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(existing, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
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

    # --------------------------------------------------------------------------
    # Logs and history
    # --------------------------------------------------------------------------

    @app.get("/api/logs")
    async def get_logs(limit: int = 100):
        return state.logs[-limit:]

    @app.get("/api/history")
    async def get_command_history(limit: int = 100):
        return state.command_history[-limit:][::-1]

    @app.delete("/api/history")
    async def clear_history():
        state.command_history = []
        return {"status": "ok"}

    @app.delete("/api/logs")
    async def clear_logs():
        state.logs = []
        return {"status": "ok"}

    # --------------------------------------------------------------------------
    # Routers (extracted modules)
    # --------------------------------------------------------------------------

    plate_tracking = load_plate_tracking()

    from webapp.routes.batch import create_batch_router
    from webapp.routes.plates import create_plates_router
    from webapp.routes.instruments import create_instruments_router
    from webapp.routes.workflows import create_workflows_router
    from webapp.routes.operator import create_operator_router
    from webapp.routes.hardware import create_hardware_router

    app.include_router(create_batch_router(state, LIBRARY_DIR, plate_tracking, save_plate_tracking, _plate_tracking_lock))
    app.include_router(create_plates_router(state, plate_tracking, save_plate_tracking, _plate_tracking_lock))
    app.include_router(create_instruments_router(state, ws_manager, lab_core, plate_tracking, save_plate_tracking, _plate_tracking_lock, BASE_DIR, LIBRARY_DIR))
    app.include_router(create_workflows_router(
        state, ws_manager, lab_core, plate_tracking, save_plate_tracking, _plate_tracking_lock,
        pending_operator_actions, pending_operator_actions_lock, pending_interventions,
        BASE_DIR, LIBRARY_DIR, WEBAPP_CONFIG
    ))
    app.include_router(create_operator_router(state, ws_manager, lab_core, pending_operator_actions, pending_operator_actions_lock, BASE_DIR, WEBAPP_CONFIG))
    app.include_router(create_hardware_router(BASE_DIR, LIBRARY_DIR, lab_core))

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

    # --------------------------------------------------------------------------
    # Startup
    # --------------------------------------------------------------------------

    @app.on_event("startup")
    async def startup():
        state.add_log("info", "LabOS WebApp started")
        try:
            _, validation = load_lab_config(BASE_DIR / "lab_config.yaml", apply_defaults=True, strict=True)
            for warning in validation.warnings:
                state.add_log("warning", f"Config warning: {warning}", "system")
        except Exception as e:
            state.add_log("error", f"Invalid configuration: {e}", "system")
            raise

        if lab_core:
            try:
                instruments = await lab_core.discover()
                for inst in instruments:
                    state.devices[inst.id] = DeviceState(
                        id=inst.id, name=inst.name, type=inst.type,
                        status=inst.status, host=inst.host, port=inst.port
                    )
                state.add_log("info", f"PnP Discovery: found {len(instruments)} instruments")
            except Exception as e:
                state.add_log("warning", f"Startup discovery failed: {e}")

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
