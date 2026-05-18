"""Workflow CRUD, execution, intervention, experiment archive, templates, and batch routes."""
import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

try:
    from src import database as _db
    _DB_AVAILABLE = True
except ImportError:
    _db = None
    _DB_AVAILABLE = False

logger = logging.getLogger(__name__)

# Regex for ${variable_name} patterns in workflow parameters
_VAR_RE = re.compile(r'\$\{([^}]+)\}')


def _extract_variables(data) -> List[str]:
    """Recursively find all ${var} references in a workflow dict."""
    found = set()
    if isinstance(data, str):
        found.update(_VAR_RE.findall(data))
    elif isinstance(data, dict):
        for v in data.values():
            found.update(_extract_variables(v))
    elif isinstance(data, list):
        for item in data:
            found.update(_extract_variables(item))
    return sorted(found)


def _resolve_variables(data, variables: dict):
    """Replace ${var} with values from the variables dict (recursive)."""
    if isinstance(data, str):
        def replacer(m):
            key = m.group(1)
            return str(variables.get(key, m.group(0)))
        return _VAR_RE.sub(replacer, data)
    elif isinstance(data, dict):
        return {k: _resolve_variables(v, variables) for k, v in data.items()}
    elif isinstance(data, list):
        return [_resolve_variables(item, variables) for item in data]
    return data


def _save_run_archive(BASE_DIR: Path, workflow_name: str, result, step_results: list, run_id: Optional[str] = None):
    """Persist experiment run metadata to Results/runs/."""
    try:
        runs_dir = BASE_DIR / "Results" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        if run_id is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = re.sub(r'[^\w\-]', '_', workflow_name)
            run_id = f"{timestamp}_{safe_name}"
        run_data = {
            "run_id": run_id,
            "workflow_name": workflow_name,
            "started_at": datetime.now().isoformat(),
            "status": "completed" if result.success else "failed",
            "steps_completed": result.steps_completed,
            "steps_failed": result.steps_failed,
            "steps_skipped": result.steps_skipped,
            "duration_seconds": result.duration_seconds,
            "errors": result.errors,
            "step_results": step_results,
        }
        (runs_dir / f"{run_id}.json").write_text(
            json.dumps(run_data, indent=2, default=str), encoding='utf-8'
        )
        if _DB_AVAILABLE:
            _db.save_run(run_data)
        logger.info(f"Run archived: {run_id}")
        return run_id
    except Exception as e:
        logger.error(f"Failed to archive run: {e}")
        return None


def create_workflows_router(
    state,
    ws_manager,
    lab_core,
    plate_tracking: dict,
    save_plate_tracking: Callable,
    plate_tracking_lock: asyncio.Lock,
    pending_operator_actions: list,
    pending_operator_actions_lock: asyncio.Lock,
    pending_interventions: dict,
    BASE_DIR: Path,
    LIBRARY_DIR: Path,
    WEBAPP_CONFIG: dict,
) -> APIRouter:
    router = APIRouter(tags=["workflows"])
    active_executor = None
    # Prevents concurrent workflow execution (parallel_execution:false is the default).
    _executor_lock = asyncio.Lock()

    # CRUD

    @router.get("/api/workflows")
    async def get_workflows():
        workflows = []
        wf_dir = LIBRARY_DIR / "Workflows"
        if wf_dir.exists():
            for f in wf_dir.glob("*.workflow.json"):
                try:
                    wf = json.loads(f.read_text(encoding='utf-8'))
                    workflows.append({
                        "id": f.stem,
                        "name": wf.get("WorkflowName", f.stem),
                        "description": wf.get("Description", ""),
                        "steps": len(wf.get("Steps", []))
                    })
                except Exception:
                    pass
        return workflows

    @router.get("/api/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str):
        wf_dir = LIBRARY_DIR / "Workflows"
        for pattern in [f"{workflow_id}.workflow.json", f"{workflow_id}.json", workflow_id]:
            wf_path = wf_dir / pattern
            if wf_path.exists():
                return json.loads(wf_path.read_text(encoding='utf-8'))
        raise HTTPException(404, f"Workflow {workflow_id} not found")

    @router.post("/api/workflows/save")
    async def save_workflow(request: Request):
        data = await request.json()
        workflow_name = data.get("WorkflowName", f"Workflow_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        safe_name = re.sub(r'[^\w\-. ]', '', workflow_name).strip().replace(" ", "_")
        wf_dir = LIBRARY_DIR / "Workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        wf_path = wf_dir / f"{safe_name}.workflow.json"
        data["CreatedAt"] = datetime.now().isoformat()
        wf_path.write_text(json.dumps(data, indent=2), encoding='utf-8')
        state.add_log("info", f"Workflow saved: {wf_path.name}", "workflow")
        return {"success": True, "filename": wf_path.name, "path": str(wf_path)}

    # Variable scanning (Settimana 8: dialog variabili)

    @router.post("/api/workflows/scan-variables")
    async def scan_variables(request: Request):
        """Return the list of ${var} names found in a workflow definition."""
        data = await request.json()
        variables = _extract_variables(data)
        # Exclude workflow.* (runtime context vars, not user inputs)
        user_vars = [v for v in variables if not v.startswith("workflow.")]
        return {"variables": user_vars, "count": len(user_vars)}

    # Intervention API

    @router.post("/api/intervention/respond")
    async def respond_to_intervention(request: Request):
        from src.lab_core import InterventionAction
        data = await request.json()
        intervention_id = data.get("intervention_id")
        action_str = data.get("action", "").lower()

        if not intervention_id:
            raise HTTPException(400, "Missing intervention_id")
        if intervention_id not in pending_interventions:
            raise HTTPException(404, "Intervention not found or already resolved")

        action_map = {
            "retry": InterventionAction.RETRY,
            "skip": InterventionAction.SKIP,
            "abort": InterventionAction.ABORT,
        }
        if action_str not in action_map:
            raise HTTPException(400, f"Invalid action: {action_str}. Must be retry, skip, or abort")

        future = pending_interventions[intervention_id]
        if not future.done():
            future.set_result(action_map[action_str])

        state.add_log("info", f"Intervention {intervention_id} resolved: {action_str}", "workflow")
        await ws_manager.broadcast({
            "type": "intervention_resolved",
            "intervention_id": intervention_id,
            "action": action_str
        })
        return {"success": True, "action": action_str}

    @router.get("/api/intervention/pending")
    async def get_pending_interventions():
        return {"pending": list(pending_interventions.keys())}

    # Workflow control

    @router.post("/api/workflows/pause")
    async def pause_workflow():
        if active_executor:
            active_executor.request_pause()
            state.add_log("info", "Workflow pause requested", "workflow")
            await ws_manager.broadcast({"type": "workflow_paused", "message": "Workflow paused by operator"})
            return {"status": "paused", "message": "Pause requested"}
        return {"status": "idle", "message": "No active workflow"}

    @router.post("/api/workflows/resume")
    async def resume_workflow():
        if active_executor:
            active_executor.request_resume()
            state.add_log("info", "Workflow resumed by operator", "workflow")
            await ws_manager.broadcast({"type": "workflow_resumed", "message": "Workflow resumed by operator"})
            return {"status": "resumed", "message": "Workflow resumed"}
        return {"status": "idle", "message": "No active workflow"}

    @router.post("/api/workflows/abort")
    async def abort_workflow():
        if active_executor:
            active_executor.request_abort()
            state.add_log("warning", "Workflow aborted by operator", "workflow")
            await ws_manager.broadcast({"type": "workflow_failed", "workflow_name": "active", "error": "Aborted by operator"})
            return {"status": "aborted", "message": "Abort requested"}
        return {"status": "idle", "message": "No active workflow"}

    # Execute workflow

    @router.post("/api/workflows/execute")
    async def execute_workflow(request: Request):
        """Execute a workflow via PnPWorkflowExecutor (dependency graph + retry + intervention)."""
        nonlocal active_executor
        if _executor_lock.locked():
            return {"status": "busy", "error": "Another workflow is already running"}
        await _executor_lock.acquire()
        try:
            return await _run_workflow(request)
        finally:
            _executor_lock.release()

    async def _run_workflow(request: Request):
        nonlocal active_executor
        data = await request.json()
        workflow_name = data.get("WorkflowName", "Unknown")
        # Optional variables dict provided by frontend (Settimana 8: dialog variabili)
        user_variables = data.get("variables", {})

        # Resolve ${var} in step parameters before building the Workflow object
        if user_variables:
            steps = data.get("Steps", [])
            resolved_steps = _resolve_variables(steps, user_variables)
            data = {**data, "Steps": resolved_steps}

        state.add_log("info", f"Starting workflow: {workflow_name}", "workflow")
        await ws_manager.broadcast({
            "type": "workflow_start",
            "workflow_name": workflow_name,
            "total_steps": len(data.get("Steps", []))
        })

        try:
            from src.workflow import Workflow, PnPWorkflowExecutor, WorkflowProgress
            from src.client import PnPRegistry, CommandResult
            workflow_obj = Workflow.from_dict(data)
        except Exception as e:
            return {"status": "error", "error": f"Invalid workflow format: {e}"}

        _exec_core = lab_core
        if not _exec_core:
            from src.lab_core import get_lab_core
            _exec_core = get_lab_core(BASE_DIR)
        if not _exec_core:
            return {"status": "error", "error": "LabCore not initialized"}

        # Only run discovery when no instruments are cached yet (startup path).
        # Once instruments are cached, repeated calls return the same data and
        # the discover() overhead (mDNS wait + port scan) is wasted on every run.
        if not _exec_core.list_instruments():
            await _exec_core.discover()

        registry = PnPRegistry(BASE_DIR)
        for instr in _exec_core.list_instruments():
            if hasattr(instr, '_server') and instr._server:
                registry.register(instr.id, instr._server)

        # Pre-flight: validate all instrument names before starting
        _live_names = [i.name.lower() for i in _exec_core.list_instruments()]
        _missing = []
        for _step in data.get("Steps", []):
            _instr = (_step.get("Instrument") or "").strip()
            _instr_lower = _instr.lower()
            if not _instr_lower or _instr_lower in ("manual", "delay", "refill"):
                continue
            if not any(_instr_lower in n or n in _instr_lower for n in _live_names):
                _missing.append(_instr)
        if _missing:
            _available = [i.name for i in _exec_core.list_instruments()]
            return {
                "status": "error",
                "error": f"Instrument(s) not found: {', '.join(set(_missing))}. "
                         f"Available: {', '.join(_available) or 'none'}",
            }

        executor = PnPWorkflowExecutor(registry)
        _wf_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        _wf_safe = re.sub(r'[^\w\-]', '_', workflow_name)
        _run_id = f"{_wf_ts}_{_wf_safe}"
        shared_wf_ctx: dict = {
            "plate_id": None, "current_plate_id": None,
            "step_results": [], "workflow_name": workflow_name,
            "run_id": _run_id,
        }

        async def web_execute_step(step, wf_context) -> "CommandResult":
            if wf_context and wf_context.plate_id and not shared_wf_ctx["plate_id"]:
                shared_wf_ctx["plate_id"] = wf_context.plate_id

            result_dict = await _execute_step(
                step.instrument, step.action, step.parameters,
                state, ws_manager, _exec_core, shared_wf_ctx,
                plate_tracking, save_plate_tracking, plate_tracking_lock,
                pending_operator_actions, pending_operator_actions_lock, WEBAPP_CONFIG,
                base_dir=BASE_DIR,
            )

            if wf_context and shared_wf_ctx.get("plate_id") and not wf_context.plate_id:
                wf_context.plate_id = shared_wf_ctx["plate_id"]

            if isinstance(result_dict, dict):
                r_status = str(result_dict.get("status", "completed") or "completed").lower()
                if r_status in ("error", "failed"):
                    success = False
                    error = (
                        result_dict.get("error")
                        or result_dict.get("reason")
                        or result_dict.get("message")
                        or "Step execution failed"
                    )
                elif r_status == "skipped":
                    # Skip is a valid control-flow outcome and should not abort workflow execution.
                    success = True
                    error = None
                    result_dict.setdefault("skipped", True)
                else:
                    success = True
                    error = None
            else:
                r_status = "completed"
                success = True
                error = None

            step_record = {
                "step": getattr(step, 'step_number', None),
                "instrument": step.instrument,
                "action": step.action,
                "status": r_status if r_status else ("completed" if success else "error"),
                "error": error,
                "timestamp": datetime.now().isoformat(),
            }
            shared_wf_ctx["step_results"].append(step_record)

            return CommandResult(success=success, data=result_dict, error=error, status=r_status)

        executor.set_step_executor(web_execute_step)
        active_executor = executor

        def on_progress(p: "WorkflowProgress"):
            asyncio.create_task(ws_manager.broadcast({
                "type": "workflow_progress",
                "workflow_name": p.workflow_name,
                "step": p.current_step,
                "total": p.total_steps,
                "instrument": p.step_instrument,
                "action": p.step_action,
                "status": p.step_status.value,
                "message": p.message,
                "percent": round((p.current_step / p.total_steps * 100) if p.total_steps else 0),
            }))

        executor.add_progress_callback(on_progress)

        async def on_intervention(req) -> "InterventionAction":
            from src.lab_core import InterventionAction
            iid = f"step_{req.step_number}_{int(datetime.now().timestamp())}"
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            pending_interventions[iid] = future
            total_steps = len(workflow_obj.steps)
            await ws_manager.broadcast({
                "type": "workflow_progress",
                "workflow_name": workflow_name,
                "step": req.step_number,
                "total": total_steps,
                "instrument": req.instrument,
                "action": req.action,
                "status": "waiting_intervention",
                "message": f"Step failed: {req.error.message}",
                "percent": round((req.step_number / total_steps * 100) if total_steps else 0),
            })
            await ws_manager.broadcast({
                "type": "intervention_required",
                "intervention_id": iid,
                "step": req.step_number,
                "instrument": req.instrument,
                "action": req.action,
                "error": req.error.message,
                "category": req.error.category.value,
                "workflow_name": workflow_name
            })
            try:
                timeout = float(WEBAPP_CONFIG.get("intervention_timeout", 3600))
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                from src.lab_core import InterventionAction
                pending_interventions.pop(iid, None)
                return InterventionAction.SKIP

        executor.set_intervention_callback(on_intervention)

        # Read parallel_execution from lab_config
        _parallel = False
        try:
            from src.config_schema import load_lab_config
            _cfg, _ = load_lab_config(BASE_DIR / "lab_config.yaml", apply_defaults=False, strict=False)
            _parallel = bool(_cfg.get("workflow", {}).get("parallel_execution", False))
        except Exception:
            pass

        try:
            # Validate upfront so dependency/instrument/command issues are explicit
            # instead of being silently skipped at runtime.
            result = await executor.execute(workflow_obj, validate=True, parallel=_parallel)
        except Exception as e:
            state.add_log("error", f"Workflow error: {e}", "workflow")
            await ws_manager.broadcast({"type": "workflow_failed", "workflow_name": workflow_name, "error": str(e)})
            return {"status": "error", "error": str(e)}
        finally:
            active_executor = None

        # Archive run (Settimana 8: archivio esperimenti)
        run_id = _save_run_archive(BASE_DIR, workflow_name, result, shared_wf_ctx["step_results"], run_id=_run_id)

        if result.success:
            state.add_log(
                "info",
                f"Workflow completed: {workflow_name} ({result.steps_completed} steps in {result.duration_seconds:.1f}s)",
                "workflow"
            )
            await ws_manager.broadcast({
                "type": "workflow_complete",
                "workflow_name": workflow_name,
                "run_id": run_id,
                "message": f"Workflow '{workflow_name}' completato con successo",
                "steps_completed": result.steps_completed,
                "duration_seconds": result.duration_seconds,
            })
            return {
                "status": "completed",
                "workflow": workflow_name,
                "run_id": run_id,
                "steps_completed": result.steps_completed,
                "steps_failed": result.steps_failed,
                "steps_skipped": result.steps_skipped,
                "duration_seconds": result.duration_seconds,
            }
        else:
            state.add_log("error", f"Workflow failed: {result.errors}", "workflow")
            await ws_manager.broadcast({
                "type": "workflow_failed",
                "workflow_name": workflow_name,
                "run_id": run_id,
                "errors": result.errors,
            })
            return {
                "status": "failed",
                "workflow": workflow_name,
                "run_id": run_id,
                "errors": result.errors,
                "steps_completed": result.steps_completed,
                "steps_failed": result.steps_failed,
            }

    # Experiment archive (Settimana 8: archivio runs)

    @router.get("/api/runs")
    async def list_runs():
        runs_dir = BASE_DIR / "Results" / "runs"
        if not runs_dir.exists():
            return {"runs": []}
        runs = []
        for f in sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(f.read_text(encoding='utf-8'))
                runs.append({
                    "run_id": data.get("run_id", f.stem),
                    "workflow_name": data.get("workflow_name"),
                    "started_at": data.get("started_at"),
                    "status": data.get("status"),
                    "steps_completed": data.get("steps_completed", 0),
                    "steps_failed": data.get("steps_failed", 0),
                    "duration_seconds": data.get("duration_seconds"),
                })
            except Exception:
                pass
        return {"runs": runs, "count": len(runs)}

    @router.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        runs_dir = BASE_DIR / "Results" / "runs"
        run_file = runs_dir / f"{run_id}.json"
        if not run_file.exists():
            raise HTTPException(404, f"Run {run_id} not found")
        return json.loads(run_file.read_text(encoding='utf-8'))


    # Experiment Templates (Settimana 9)

    TEMPLATES_DIR = LIBRARY_DIR / "Templates"

    @router.get("/api/templates")
    async def list_templates():
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        templates_list = []
        for f in sorted(TEMPLATES_DIR.glob("*.template.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                t = json.loads(f.read_text(encoding='utf-8'))
                templates_list.append({
                    "id": f.stem,
                    "name": t.get("TemplateName", f.stem),
                    "description": t.get("Description", ""),
                    "tags": t.get("Tags", []),
                    "steps": len(t.get("Steps", [])),
                    "version": t.get("Version", "1.0"),
                })
            except Exception:
                pass
        return {"templates": templates_list}

    @router.get("/api/templates/{template_id}")
    async def get_template(template_id: str):
        for pattern in [f"{template_id}.template.json", f"{template_id}.json", template_id]:
            t_path = TEMPLATES_DIR / pattern
            if t_path.exists():
                return json.loads(t_path.read_text(encoding='utf-8'))
        raise HTTPException(404, f"Template {template_id} not found")

    @router.post("/api/templates/save")
    async def save_template(request: Request):
        data = await request.json()
        tname = data.get("TemplateName", f"Template_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        safe = re.sub(r'[^\w\-. ]', '', tname).strip().replace(" ", "_")
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        t_path = TEMPLATES_DIR / f"{safe}.template.json"
        data["CreatedAt"] = datetime.now().isoformat()
        data.setdefault("Version", "1.0")
        t_path.write_text(json.dumps(data, indent=2), encoding='utf-8')
        state.add_log("info", f"Template saved: {t_path.name}", "templates")
        return {"success": True, "filename": t_path.name, "id": t_path.stem}

    @router.post("/api/templates/{template_id}/instantiate")
    async def instantiate_template(template_id: str, request: Request):
        """Generate a workflow JSON from a template + user-provided variable overrides."""
        override = await request.json()
        t_path = None
        for pattern in [f"{template_id}.template.json", f"{template_id}.json", template_id]:
            p = TEMPLATES_DIR / pattern
            if p.exists():
                t_path = p
                break
        if not t_path:
            raise HTTPException(404, f"Template {template_id} not found")
        tmpl = json.loads(t_path.read_text(encoding='utf-8'))
        wf = {
            "WorkflowName": override.get("WorkflowName", tmpl.get("TemplateName", template_id)),
            "Steps": tmpl.get("Steps", []),
            "CreatedAt": datetime.now().isoformat(),
            "_from_template": template_id,
        }
        if override.get("variables"):
            wf["Steps"] = _resolve_variables(wf["Steps"], override["variables"])
        return wf

    @router.delete("/api/templates/{template_id}")
    async def delete_template(template_id: str):
        for pattern in [f"{template_id}.template.json", f"{template_id}.json", template_id]:
            t_path = TEMPLATES_DIR / pattern
            if t_path.exists():
                t_path.unlink()
                return {"success": True}
        raise HTTPException(404, f"Template {template_id} not found")

    # Export / Import with versioning (Settimana 9)

    @router.get("/api/workflows/{workflow_id}/export")
    async def export_workflow(workflow_id: str):
        from fastapi.responses import Response
        wf_dir = LIBRARY_DIR / "Workflows"
        wf_path = None
        for pattern in [f"{workflow_id}.workflow.json", f"{workflow_id}.json", workflow_id]:
            p = wf_dir / pattern
            if p.exists():
                wf_path = p
                break
        if not wf_path:
            raise HTTPException(404, f"Workflow {workflow_id} not found")
        content = wf_path.read_text(encoding='utf-8')
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        download_name = f"{workflow_id}_{ts}.workflow.json"
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
        )

    @router.post("/api/workflows/import")
    async def import_workflow(file: UploadFile = File(...)):
        content = await file.read()
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"Invalid JSON: {e}")
        if "WorkflowName" not in data and "Steps" not in data:
            raise HTTPException(400, "Not a valid workflow file (missing WorkflowName or Steps)")
        workflow_name = data.get("WorkflowName", file.filename or "Imported")
        safe = re.sub(r'[^\w\-. ]', '', workflow_name).strip().replace(" ", "_")
        wf_dir = LIBRARY_DIR / "Workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        data["ImportedAt"] = datetime.now().isoformat()
        wf_path = wf_dir / f"{safe}.workflow.json"
        wf_path.write_text(json.dumps(data, indent=2), encoding='utf-8')
        _create_version_snapshot(LIBRARY_DIR, safe, data, "imported")
        state.add_log("info", f"Workflow imported: {wf_path.name}", "workflow")
        return {"success": True, "filename": wf_path.name, "workflow_name": workflow_name}

    @router.get("/api/workflows/{workflow_id}/versions")
    async def list_versions(workflow_id: str):
        versions_dir = LIBRARY_DIR / "Workflows" / ".versions" / workflow_id
        if not versions_dir.exists():
            return {"versions": []}
        versions = []
        for f in sorted(versions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                meta = json.loads(f.read_text(encoding='utf-8'))
                versions.append({
                    "version_id": f.stem,
                    "workflow_name": meta.get("WorkflowName", workflow_id),
                    "saved_at": meta.get("_saved_at", ""),
                    "note": meta.get("_version_note", ""),
                    "steps": len(meta.get("Steps", [])),
                })
            except Exception:
                pass
        return {"versions": versions, "count": len(versions)}

    @router.post("/api/workflows/{workflow_id}/versions/snapshot")
    async def create_snapshot(workflow_id: str, request: Request):
        """Save current state of a workflow as a named version snapshot."""
        body = await request.json()
        note = body.get("note", "manual snapshot")
        wf_dir = LIBRARY_DIR / "Workflows"
        wf_path = None
        for pattern in [f"{workflow_id}.workflow.json", f"{workflow_id}.json", workflow_id]:
            p = wf_dir / pattern
            if p.exists():
                wf_path = p
                break
        if not wf_path:
            raise HTTPException(404, f"Workflow {workflow_id} not found")
        data = json.loads(wf_path.read_text(encoding='utf-8'))
        vid = _create_version_snapshot(LIBRARY_DIR, workflow_id, data, note)
        return {"success": True, "version_id": vid}

    @router.get("/api/workflows/{workflow_id}/versions/{version_id}")
    async def get_version(workflow_id: str, version_id: str):
        v_path = LIBRARY_DIR / "Workflows" / ".versions" / workflow_id / f"{version_id}.json"
        if not v_path.exists():
            raise HTTPException(404, "Version not found")
        return json.loads(v_path.read_text(encoding='utf-8'))

    @router.post("/api/workflows/{workflow_id}/versions/{version_id}/restore")
    async def restore_version(workflow_id: str, version_id: str):
        v_path = LIBRARY_DIR / "Workflows" / ".versions" / workflow_id / f"{version_id}.json"
        if not v_path.exists():
            raise HTTPException(404, "Version not found")
        data = json.loads(v_path.read_text(encoding='utf-8'))
        # Snapshot current before overwriting
        wf_dir = LIBRARY_DIR / "Workflows"
        safe = re.sub(r'[^\w\-. ]', '', workflow_id).strip().replace(" ", "_")
        current_path = wf_dir / f"{safe}.workflow.json"
        if current_path.exists():
            current = json.loads(current_path.read_text(encoding='utf-8'))
            _create_version_snapshot(LIBRARY_DIR, workflow_id, current, "pre-restore backup")
        data.pop("_saved_at", None)
        data.pop("_version_note", None)
        data["RestoredAt"] = datetime.now().isoformat()
        current_path.write_text(json.dumps(data, indent=2), encoding='utf-8')
        state.add_log("info", f"Workflow {workflow_id} restored to version {version_id}", "workflow")
        return {"success": True, "workflow_id": workflow_id, "restored_version": version_id}

    # Batch multi-experiment (Settimana 9)

    @router.post("/api/workflows/batch-execute")
    async def batch_execute(request: Request):
        """Run the same workflow N times with different variable sets (one per sample)."""
        nonlocal active_executor
        data = await request.json()
        workflow_def = data.get("workflow")
        samples = data.get("samples", [])  # [{sample_id, variables: {var: value, ...}}, ...]

        if not workflow_def:
            raise HTTPException(400, "Missing 'workflow' field")
        if not samples:
            raise HTTPException(400, "Missing 'samples' list")

        if _executor_lock.locked():
            raise HTTPException(409, "A workflow is already running")

        batch_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        workflow_name = workflow_def.get("WorkflowName", "Batch")

        # Read parallel_execution from lab_config (same logic as regular run)
        _parallel = False
        try:
            from src.config_schema import load_lab_config
            _cfg, _ = load_lab_config(BASE_DIR / "lab_config.yaml", apply_defaults=False, strict=False)
            _parallel = bool(_cfg.get("workflow", {}).get("parallel_execution", False))
        except Exception:
            pass

        state.add_log("info", f"Batch started: {batch_id} ({len(samples)} samples)", "workflow")
        await ws_manager.broadcast({
            "type": "batch_start",
            "batch_id": batch_id,
            "workflow_name": workflow_name,
            "total_samples": len(samples),
        })

        run_ids = []
        errors = []

        await _executor_lock.acquire()
        try:
            for idx, sample in enumerate(samples):
                sample_id = sample.get("sample_id", f"sample_{idx + 1}")
                variables = sample.get("variables", {})
                variables["sample_id"] = sample_id
                variables["sample_index"] = str(idx + 1)

                steps = _resolve_variables(workflow_def.get("Steps", []), variables)
                sample_wf = {**workflow_def, "Steps": steps, "WorkflowName": f"{workflow_name}_{sample_id}"}

                await ws_manager.broadcast({
                    "type": "batch_sample_start",
                    "batch_id": batch_id,
                    "sample_id": sample_id,
                    "sample_index": idx + 1,
                    "total_samples": len(samples),
                })

                try:
                    from src.workflow import Workflow, PnPWorkflowExecutor, WorkflowProgress
                    from src.client import PnPRegistry
                    wf_obj = Workflow.from_dict(sample_wf)
                    _exec_core = lab_core
                    if not _exec_core:
                        from src.lab_core import get_lab_core
                        _exec_core = get_lab_core(BASE_DIR)
                    if not _exec_core:
                        raise Exception("LabCore not initialized")

                    if not _exec_core.list_instruments():
                        await _exec_core.discover()
                    registry = PnPRegistry(BASE_DIR)
                    for instr in _exec_core.list_instruments():
                        if hasattr(instr, '_server') and instr._server:
                            registry.register(instr.id, instr._server)

                    executor = PnPWorkflowExecutor(registry)
                    active_executor = executor
                    shared_ctx: dict = {"plate_id": None, "step_results": [], "workflow_name": sample_wf["WorkflowName"]}

                    async def _sample_step(step, wf_context, _sc=shared_ctx):
                        r = await _execute_step(
                            step.instrument, step.action, step.parameters,
                            state, ws_manager, _exec_core, _sc,
                            plate_tracking, save_plate_tracking, plate_tracking_lock,
                            pending_operator_actions, pending_operator_actions_lock, WEBAPP_CONFIG,
                            base_dir=BASE_DIR,
                        )
                        if isinstance(r, dict):
                            success = r.get("status", "completed") not in ("error", "skipped")
                            error = r.get("error") if not success else None
                        else:
                            success, error = True, None
                        from src.client import CommandResult
                        _sc["step_results"].append({
                            "step": getattr(step, 'step_number', None),
                            "instrument": step.instrument, "action": step.action,
                            "status": "completed" if success else "error", "error": error,
                            "timestamp": datetime.now().isoformat(),
                        })
                        return CommandResult(success=success, data=r, error=error)

                    def on_batch_progress(p: "WorkflowProgress", _sid=sample_id):
                        asyncio.create_task(ws_manager.broadcast({
                            "type": "workflow_progress",
                            "workflow_name": p.workflow_name,
                            "step": p.current_step,
                            "total": p.total_steps,
                            "instrument": p.step_instrument,
                            "action": p.step_action,
                            "status": p.step_status.value,
                            "message": p.message,
                            "percent": round((p.current_step / p.total_steps * 100) if p.total_steps else 0),
                            "sample_id": _sid,
                            "batch_id": batch_id,
                        }))

                    async def on_batch_intervention(req) -> "InterventionAction":
                        from src.lab_core import InterventionAction
                        iid = f"batch_{batch_id}_step_{req.step_number}_{int(datetime.now().timestamp())}"
                        future: asyncio.Future = asyncio.get_event_loop().create_future()
                        pending_interventions[iid] = future
                        total_steps = len(wf_obj.steps)
                        await ws_manager.broadcast({
                            "type": "intervention_required",
                            "intervention_id": iid,
                            "step": req.step_number,
                            "instrument": req.instrument,
                            "action": req.action,
                            "error": req.error.message,
                            "category": req.error.category.value,
                            "workflow_name": req.workflow_name,
                            "sample_id": sample_id,
                            "batch_id": batch_id,
                        })
                        try:
                            timeout = float(WEBAPP_CONFIG.get("intervention_timeout", 3600))
                            return await asyncio.wait_for(future, timeout=timeout)
                        except asyncio.TimeoutError:
                            pending_interventions.pop(iid, None)
                            return InterventionAction.SKIP

                    executor.set_step_executor(_sample_step)
                    executor.add_progress_callback(on_batch_progress)
                    executor.set_intervention_callback(on_batch_intervention)
                    result = await executor.execute(wf_obj, validate=False, parallel=_parallel)
                    run_id = _save_run_archive(BASE_DIR, sample_wf["WorkflowName"], result, shared_ctx["step_results"])
                    if run_id:
                        # Tag run with batch metadata
                        runs_dir = BASE_DIR / "Results" / "runs"
                        run_file = runs_dir / f"{run_id}.json"
                        if run_file.exists():
                            run_data = json.loads(run_file.read_text(encoding='utf-8'))
                            run_data["batch_id"] = batch_id
                            run_data["sample_id"] = sample_id
                            run_file.write_text(json.dumps(run_data, indent=2, default=str), encoding='utf-8')
                        run_ids.append(run_id)

                    await ws_manager.broadcast({
                        "type": "batch_sample_complete",
                        "batch_id": batch_id,
                        "sample_id": sample_id,
                        "sample_index": idx + 1,
                        "run_id": run_id,
                        "success": result.success,
                    })

                except Exception as e:
                    errors.append({"sample_id": sample_id, "error": str(e)})
                    state.add_log("error", f"Batch sample {sample_id} failed: {e}", "workflow")
                    await ws_manager.broadcast({
                        "type": "batch_sample_failed",
                        "batch_id": batch_id,
                        "sample_id": sample_id,
                        "error": str(e),
                    })
                finally:
                    active_executor = None

            await ws_manager.broadcast({
                "type": "batch_complete",
                "batch_id": batch_id,
                "run_ids": run_ids,
                "errors_count": len(errors),
                "success_count": len(run_ids),
            })
            state.add_log("info", f"Batch complete: {batch_id} — {len(run_ids)} OK, {len(errors)} failed", "workflow")

            return {
                "batch_id": batch_id,
                "run_ids": run_ids,
                "success_count": len(run_ids),
                "errors_count": len(errors),
                "errors": errors,
            }
        finally:
            _executor_lock.release()

    return router


def _create_version_snapshot(LIBRARY_DIR: Path, workflow_id: str, data: dict, note: str) -> str:
    """Save a version snapshot of a workflow. Returns version_id."""
    try:
        versions_dir = LIBRARY_DIR / "Workflows" / ".versions" / workflow_id
        versions_dir.mkdir(parents=True, exist_ok=True)
        vid = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        snapshot = {**data, "_saved_at": datetime.now().isoformat(), "_version_note": note}
        (versions_dir / f"{vid}.json").write_text(json.dumps(snapshot, indent=2, default=str), encoding='utf-8')
        return vid
    except Exception as e:
        logger.error(f"Version snapshot failed: {e}")
        return ""


# execute_step: shared step execution logic

async def _execute_step(
    instrument: str,
    action: str,
    params: dict,
    state,
    ws_manager,
    core,
    workflow_context: dict,
    plate_tracking: dict,
    save_plate_tracking: Callable,
    plate_tracking_lock: asyncio.Lock,
    pending_operator_actions: list,
    pending_operator_actions_lock: asyncio.Lock,
    WEBAPP_CONFIG: dict,
    base_dir: Optional[Path] = None,
) -> dict:
    """Execute one workflow step: delay, manual, refill, or instrument command."""

    instrument_lower = instrument.lower()

    # Delay
    if "delay" in instrument_lower or action.lower() == "wait":
        seconds = params.get("seconds", 30)
        state.add_log("info", f"Waiting {seconds} seconds...", "workflow")
        await asyncio.sleep(seconds)
        return {"action": "delay", "seconds": seconds, "status": "completed"}

    # Manual station (requires operator acknowledgment)
    if "manual" in instrument_lower:
        return await _wait_operator(
            action, params, state, ws_manager, pending_operator_actions,
            pending_operator_actions_lock, WEBAPP_CONFIG["manual_action_timeout"]
        )

    # Explicit RefillTipRack step
    if action.lower() in ("refilltiprack", "refill") or "refill" in action.lower():
        rack_type = params.get("rack_type", "tiprack")
        return await _wait_refill(
            rack_type, state, ws_manager, pending_operator_actions,
            pending_operator_actions_lock, WEBAPP_CONFIG["tip_refill_timeout"]
        )

    # Generic instrument via LabCore
    matched_instr = None
    if core:
        for instr in core.list_instruments():
            name_l = instr.name.lower()
            id_l = instr.id.lower()
            if (instrument_lower == id_l or instrument_lower == name_l
                    or instrument_lower in name_l or name_l in instrument_lower
                    or instrument_lower in id_l):
                matched_instr = instr
                break

    if not matched_instr:
        available = [f"{i.name} ({i.id})" for i in (core.list_instruments() if core else [])]
        msg = f"Instrument '{instrument}' not found. Running: {available}"
        logger.error(f"[WORKFLOW] {msg}")
        state.add_log("error", msg, "workflow")
        return {"status": "error", "error": msg}

    device_id = matched_instr.id
    if device_id in state.devices:
        state.devices[device_id].status = "busy"
        await ws_manager.broadcast({"type": "device_update", "device_id": device_id, "status": "busy"})

    # Auto-inject PlateID/SampleSetID/PlateType for RunMeasurement from workflow context
    if action.lower() == "runmeasurement" and not params.get("PlateID"):
        params = dict(params)
        params["PlateID"] = workflow_context.get("current_plate_id", "")
        params.setdefault("SampleSetID", "")
        params.setdefault("PlateType", "")

    try:
        result = await core.execute_command(device_id, action, params)
        logger.info(f"[WORKFLOW] {device_id}/{action} → success={result.success}, error={result.error}")

        if result.success:
            state.add_log("info", f"{action} completed on {device_id}", "workflow")

            # Plate tracking (save_plate_tracking handles its own lock)
            _wf_track_tecan(device_id, action, params, result, workflow_context, plate_tracking, state, base_dir=base_dir)
            _wf_track_opentrons(device_id, action, params, result, workflow_context, plate_tracking, state, base_dir=base_dir)
            await save_plate_tracking(plate_tracking)

            # Tip refill notification on warning message
            result_msg = result.message or (result.data.get("message") if isinstance(result.data, dict) else str(result.data or ""))
            if result_msg and isinstance(result_msg, str):
                if "insufficient tips" in result_msg.lower() or "refill" in result_msg.lower():
                    await ws_manager.broadcast({
                        "type": "operator_notification",
                        "id": int(datetime.now().timestamp() * 1000),
                        "title": "Refill Tips Richiesto",
                        "message": result_msg,
                        "priority": "urgent",
                        "requires_action": True,
                        "timestamp": datetime.now().isoformat()
                    })

            if device_id in state.devices:
                state.devices[device_id].status = "online"
                state.devices[device_id].last_command = action
                state.devices[device_id].last_result = "OK"
                await ws_manager.broadcast({"type": "device_update", "device_id": device_id, "status": "online"})

            state.add_command(device=device_id, command=action, params=params,
                              result={"data": result.data, "message": result.message, "workflow": True}, success=True)

            step_result = {"device": device_id, "action": action, "params": params, "status": "completed", "result": result_msg}
            if workflow_context and workflow_context.get("plate_id"):
                step_result["plate_id"] = workflow_context["plate_id"]
            return step_result

        else:
            error_msg = result.error or "Unknown error"
            # Auto tip refill on insufficient tips
            if "insufficient tips" in error_msg.lower() or "refill cancelled" in error_msg.lower():
                await _wait_refill(
                    "all", state, ws_manager, pending_operator_actions,
                    pending_operator_actions_lock, WEBAPP_CONFIG["tip_refill_timeout"],
                    context_msg=error_msg
                )
                retry = await core.execute_command(device_id, action, params)
                if retry.success:
                    state.add_log("info", f"{action} completed on retry", "workflow")
                    return {"device": device_id, "action": action, "params": params,
                            "status": "completed", "result": retry.message or "Completed after tip refill"}
                raise Exception(retry.error or "Command failed after tip refill")
            raise Exception(error_msg)

    except ImportError as e:
        state.add_log("error", f"LabCore not available: {e}", "workflow")
        raise Exception(f"LabCore module not available: {e}")
    except Exception as e:
        state.add_log("error", f"LabCore error: {e}", "workflow")
        state.add_command(device=instrument, command=action, params=params,
                          result={"error": str(e), "workflow": True}, success=False)
        raise


async def _wait_operator(action, params, state, ws_manager, pending_operator_actions, lock, timeout):
    operator_message = (
        params.get("TaskDescription")
        or params.get("task_description")
        or params.get("description")
        or params.get("message")
        or f"Esegui: {action}"
    )

    notification_id = int(datetime.now().timestamp() * 1000)
    notification = {
        "id": notification_id, "type": "operator_notification",
        "title": f"Azione Manuale: {action}",
        "message": operator_message,
        "priority": "warning", "requires_action": True,
        "timestamp": datetime.now().isoformat(), "action": action, "params": params
    }
    async with lock:
        pending_operator_actions.append(notification)
    await ws_manager.broadcast(notification)
    state.add_log("warning", f"Waiting for operator: {action}", "workflow")

    timeout_val = (
        params.get("TimeoutSeconds")
        or params.get("timeout_seconds")
        or params.get("timeout")
        or timeout
    )
    start = datetime.now()
    while True:
        async with lock:
            if notification_id not in [int(a.get("id", 0)) for a in pending_operator_actions]:
                break
        await asyncio.sleep(1)
        if (datetime.now() - start).total_seconds() > timeout_val:
            async with lock:
                pending_operator_actions[:] = [a for a in pending_operator_actions if int(a.get("id", 0)) != notification_id]
            raise TimeoutError(f"Operator did not confirm '{action}' within {timeout_val}s")

    state.add_log("info", f"Operator completed: {action}", "workflow")
    return {"status": "completed", "action": action, "acknowledged": True}


async def _wait_refill(rack_type, state, ws_manager, pending_operator_actions, lock, timeout, context_msg=""):
    notification_id = int(datetime.now().timestamp() * 1000)
    notification = {
        "id": notification_id, "type": "operator_notification",
        "title": "Refill Tip Rack",
        "message": f"Richiesto refill: {rack_type}. {context_msg}".strip(),
        "priority": "urgent", "requires_action": True,
        "timestamp": datetime.now().isoformat(), "action": "RefillTipRack",
        "params": {"rack_type": rack_type}
    }
    async with lock:
        pending_operator_actions.append(notification)
    await ws_manager.broadcast(notification)
    state.add_log("warning", f"Waiting for tip rack refill: {rack_type}", "workflow")

    start = datetime.now()
    while True:
        async with lock:
            if notification_id not in [int(a.get("id", 0)) for a in pending_operator_actions]:
                break
        await asyncio.sleep(1)
        if (datetime.now() - start).total_seconds() > timeout:
            async with lock:
                pending_operator_actions[:] = [a for a in pending_operator_actions if int(a.get("id", 0)) != notification_id]
            raise TimeoutError(f"Operator did not refill tips within {timeout}s")

    state.add_log("info", f"Tip rack refilled: {rack_type}", "workflow")
    return {"status": "completed", "action": "RefillTipRack", "rack_type": rack_type}


def _wf_track_tecan(device_id, action, params, result, wf_ctx, plate_tracking, state, base_dir: Optional[Path] = None):
    if "tecan" not in device_id.lower() or action not in ["RunMeasurement", "RunAnalysis"]:
        return
    plate_id = (params.get("PlateID") or params.get("plate_id") or params.get("plateId")
                or (wf_ctx.get("current_plate_id") if wf_ctx else None))
    result_data = result.data if isinstance(result.data, dict) else {}
    animl_path = result_data.get("animl_file_path", "") or result_data.get("AnIMLFilePath", "")

    if plate_id and plate_id not in plate_tracking:
        plate_tracking[plate_id] = {"created": datetime.now().isoformat(), "status": "analyzed", "analysis_results": []}
    if plate_id:
        plate_tracking[plate_id]["analysis_results"] = plate_tracking[plate_id].get("analysis_results", [])
        plate_tracking[plate_id]["analysis_results"].append({
            "timestamp": datetime.now().isoformat(),
            "measurement_type": params.get("measurement_type", "spectroscopy"),
            "protocol": params.get("ProtocolFile") or params.get("protocol", ""),
            "result_file": animl_path or result_data.get("excel_file_path", ""),
            "command": action, "instrument": device_id, "workflow_context": True, "raw_result": result_data,
        })
        plate_tracking[plate_id]["status"] = "analyzed"
        state.add_log("info", f"Analysis result linked to plate {plate_id}", "plates")

    # DB: parse AnIML per-well and save with full measurement metadata
    if _DB_AVAILABLE and animl_path and plate_id:
        try:
            from src.animl_parser import parse_animl
            animl_result = parse_animl(Path(animl_path))
            run_id = wf_ctx.get("run_id") if wf_ctx else None
            measurement_id = _db.get_last_measurement_id(run_id) if run_id else None

            protocol_name = params.get("ProtocolFile") or params.get("protocol", "")
            protocol_id = None
            if protocol_name:
                # Build protocol metadata from the first experiment step that has it
                proto_data: dict = {}
                for exp_step in animl_result.get("experiment_steps", []):
                    proto_data = {
                        "measurement_type": exp_step.get("measurement_type"),
                        "wavelength_nm": exp_step.get("wavelength_nm"),
                        "excitation_nm": exp_step.get("excitation_nm"),
                        "emission_nm": exp_step.get("emission_nm"),
                        "parameters": exp_step.get("parameters"),
                    }
                    if any(v is not None for v in proto_data.values()):
                        break
                protocol_id = _db.get_or_create_protocol(protocol_name, proto_data)

            well_values = []
            for exp_step in animl_result.get("experiment_steps", []):
                meas_type = exp_step.get("measurement_type")
                wl_nm = exp_step.get("wavelength_nm")
                ex_nm = exp_step.get("excitation_nm")
                em_nm = exp_step.get("emission_nm")
                for m in exp_step.get("measurements", []):
                    well_values.append({
                        "well": m.get("well"),
                        "value": m.get("value"),
                        "unit": m.get("unit"),
                        "measurement_type": meas_type,
                        "wavelength_nm": wl_nm,
                        "excitation_nm": ex_nm,
                        "emission_nm": em_nm,
                        "cycle": 1,
                        "timestamp": datetime.utcnow().isoformat(),
                    })
            if well_values:
                _db.save_well_measurements(plate_id, measurement_id, well_values, protocol_id=protocol_id)
                _db.update_plate_status(plate_id, "measured")
        except Exception as exc:
            logger.warning(f"AnIML DB integration failed: {exc}")


def _resolve_plate_metadata(recipe_name: str, base_dir: Path) -> dict:
    """Read active HAL config, find the primary well-plate labware, return plate metadata."""
    try:
        active_file = base_dir / "Library" / "HardwareConfig" / ".active"
        hal_name = active_file.read_text(encoding="utf-8").strip() if active_file.exists() else ""
        if not hal_name:
            return {}
        hal_path = base_dir / "Library" / "HardwareConfig" / hal_name
        if not hal_path.exists():
            hal_path = base_dir / "Library" / "HardwareConfig" / f"{hal_name}.json"
        if not hal_path.exists():
            return {}
        hal = json.loads(hal_path.read_text(encoding="utf-8"))
        for logical, lw in hal.get("Labware", {}).items():
            load_name = lw.get("LoadName", "")
            if "wellplate" in load_name.lower():
                slot = lw.get("Slot", "")
                plate_json = base_dir / "Library" / "Labware" / "Plates" / f"{load_name}.plate.json"
                if plate_json.exists():
                    meta = json.loads(plate_json.read_text(encoding="utf-8"))
                    return {
                        "plate_type": load_name,
                        "display_name": meta.get("display_name"),
                        "rows": meta.get("rows"),
                        "columns": meta.get("columns"),
                        "total_wells": meta.get("total_wells"),
                        "max_volume_ul": meta.get("max_volume_ul"),
                        "working_volume_ul": meta.get("working_volume_ul"),
                        "well_shape": meta.get("well_shape"),
                        "well_depth_mm": meta.get("well_depth_mm"),
                        "bottom_type": meta.get("bottom_type"),
                        "brand": meta.get("brand"),
                        "tecan_compatible": meta.get("tecan_compatible", True),
                        "labware_slot": slot,
                        "hal_config": hal_name.replace(".json", ""),
                    }
                return {"plate_type": load_name, "labware_slot": slot,
                        "hal_config": hal_name.replace(".json", "")}
    except Exception as exc:
        logger.warning(f"HAL metadata resolution failed: {exc}")
    return {}


def _extract_wells_from_recipe(recipe_name: str, plate_id: str, base_dir: Path) -> list:
    """
    Parse recipe JSON to build per-well records with reagent, volume, liquid_class,
    phase_name (from Comment steps used as group markers), and pipette_mount.

    Supports: Transfer, TransferWithLiquidClass, Distribute, Aspirate→Dispense pairs.
    """
    try:
        recipe_path = base_dir / "Library" / "Recipes" / f"{recipe_name}.json"
        if not recipe_path.exists():
            return []
        recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
        steps = recipe.get("Steps", [])
        now = datetime.utcnow().isoformat()

        # Build a well → cumulative record dict (last write wins per well+reagent combo)
        well_records: dict = {}
        current_phase = "default"
        current_source = ""
        current_volume = 0.0
        current_liquid_class = ""
        current_pipette = recipe.get("Pipettes", {}).get("left") and "left" or "left"

        _transfer_cmds = {
            "Transfer", "TransferWithLiquidClass", "Distribute",
            "Aspirate", "Dispense",
        }

        for step in steps:
            cmd = step.get("Command", "")

            # Comment steps used as phase/group markers by the recipe generator
            if cmd == "Comment":
                msg = step.get("Message", step.get("Text", "")).strip()
                if msg:
                    current_phase = msg
                continue

            # Delay steps mark phase boundaries — reset phase label
            if cmd == "Delay":
                current_phase = "default"
                continue

            if cmd not in _transfer_cmds:
                continue

            pipette_mount = step.get("PipetteMount", current_pipette)
            liquid_class = step.get("LiquidClass", current_liquid_class) or ""
            source = step.get("Source", current_source)
            volume = float(step.get("Volume", current_volume) or 0)

            if cmd == "Aspirate":
                # Remember source for the subsequent Dispense
                current_source = source
                current_volume = volume
                current_liquid_class = liquid_class
                continue

            if cmd == "Dispense":
                source = source or current_source
                volume = volume or current_volume
                liquid_class = liquid_class or current_liquid_class

            destinations = step.get("Destinations") or step.get("Destination") or step.get("Dest") or []
            if isinstance(destinations, str):
                destinations = [destinations]

            reagent_name = source.split(":")[0] if ":" in source else source

            for dest in destinations:
                if not dest or ":" not in dest:
                    continue
                labware, well_id = dest.split(":", 1)
                well_id = well_id.strip()
                if not well_id:
                    continue
                row_label = well_id[0].upper()
                col_str = well_id[1:]
                col_num = int(col_str) if col_str.isdigit() else 0

                key = well_id
                if key in well_records:
                    # Accumulate volume if same reagent, otherwise keep last write
                    if well_records[key]["reagent_name"] == reagent_name:
                        well_records[key]["volume_ul"] = (
                            well_records[key]["volume_ul"] + volume
                        )
                    else:
                        well_records[key] = dict(well_records[key])
                        well_records[key]["volume_ul"] = volume
                        well_records[key]["reagent_name"] = reagent_name
                else:
                    well_records[key] = {
                        "plate_id": plate_id,
                        "well_id": well_id,
                        "row_label": row_label,
                        "col_number": col_num,
                        "reagent_name": reagent_name,
                        "volume_ul": volume,
                        "liquid_class": liquid_class,
                        "phase_name": current_phase,
                        "source_well": source,
                        "pipette_mount": pipette_mount,
                        "pipetted_at": now,
                    }

        return list(well_records.values())
    except Exception as exc:
        logger.warning(f"Recipe well extraction failed: {exc}")
        return []


def _wf_track_opentrons(device_id, action, params, result, wf_ctx, plate_tracking, state, base_dir: Optional[Path] = None):
    if "opentrons" not in device_id.lower() or action not in ["ExecuteRecipe", "RunRecipe", "run_recipe"]:
        return
    recipe_name = params.get("RecipeName") or params.get("recipe_name") or params.get("recipe", "")
    recipe_slug = re.sub(r'[^a-z0-9]', '', recipe_name.lower())[:12] if recipe_name else "unknown"
    plate_id = f"PLT-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{recipe_slug}"

    if wf_ctx is not None:
        wf_ctx["plate_id"] = plate_id
        wf_ctx["current_plate_id"] = plate_id
        state.add_log("info", f"Plate ID: {plate_id}", "workflow")

    plate_tracking[plate_id] = {
        "created": datetime.now().isoformat(), "status": "pipetted", "analysis_results": [],
        "pipetting_info": {"timestamp": datetime.now().isoformat(), "recipe": recipe_name,
                           "instrument": device_id, "command": action, "workflow_context": True},
    }

    if _DB_AVAILABLE:
        try:
            plate_meta = _resolve_plate_metadata(recipe_name, base_dir) if base_dir else {}
            wells = _extract_wells_from_recipe(recipe_name, plate_id, base_dir) if base_dir else []
            run_id = wf_ctx.get("run_id") if wf_ctx else None
            _db.save_plate({
                "plate_id": plate_id,
                "run_id": run_id,
                "recipe_name": recipe_name,
                "prepared_at": datetime.utcnow().isoformat(),
                "status": "prepared",
                **plate_meta,
            })
            if wells:
                _db.save_wells(wells)
        except Exception as exc:
            logger.warning(f"Plate DB save failed: {exc}")

    state.add_log("info", f"Pipetting recorded for plate {plate_id}", "plates")
