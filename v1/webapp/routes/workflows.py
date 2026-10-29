"""Workflow CRUD, execution, intervention, experiment archive, templates, and batch routes."""
import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

if TYPE_CHECKING:
    # Only for type hints (e.g. `-> "InterventionAction"` below) — the real
    # import stays lazy/inline at each call site to avoid pulling in
    # src.lab_core (and its SiLA2/discovery deps) at module-import time.
    from src.lab_core import InterventionAction

try:
    from src import database as _db
except ImportError:
    _db = None

from src import plate_tracking
from src.run_manager import RunBusy, RunManager
from src.settings import get_settings, normalize_timeout
from src.workflow import is_locally_handled

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
    """Persist experiment run metadata to the SQLite run archive (run_results).

    Previously also wrote a redundant flat-file backup to Results/runs/*.json —
    removed: confirmed write-only dead weight (nothing in the repo ever read
    those files back; the SQLite-backed /api/db/runs is the real, UI-facing
    archive used by Run History).
    """
    try:
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
        if _db is not None:
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
    pending_operator_actions: list,
    pending_operator_actions_lock: asyncio.Lock,
    pending_interventions: dict,
    BASE_DIR: Path,
    LIBRARY_DIR: Path,
) -> APIRouter:
    router = APIRouter(tags=["workflows"])
    active_executor = None
    # Owns background runs and the single-run slot. The instruments are
    # physical and share a deck, so one run at a time is deliberate; a refusal
    # names the run holding the slot instead of a bare "busy".
    runs = RunManager(db=_db)

    # Last known workflow status, kept in sync with every workflow_* broadcast
    # below (see _track_workflow_status). Workflow progress is otherwise a
    # pure push model (websocket only) - a client that opens/refreshes a page
    # AFTER a workflow already started never receives its workflow_start
    # event and has no way to know one is running until the NEXT broadcast
    # happens to update whatever local "is a workflow active" flag that page
    # keeps (confirmed on the Operator page: its own top status card stayed
    # on "No active workflow" while the base.html bottom bar - which has no
    # such flag, just renders whatever the last message said - showed the
    # real live step). GET /api/workflows/live-status below lets a freshly
    # loaded page ask for the current snapshot instead of only waiting for
    # the next push.
    _current_workflow_status: dict = {"active": False}

    def _track_workflow_status(msg: dict) -> None:
        mtype = msg.get("type", "")
        if mtype == "workflow_start":
            _current_workflow_status.clear()
            _current_workflow_status.update({
                "active": True, "workflow_name": msg.get("workflow_name"),
                "step": 0, "total": msg.get("total_steps", 0),
                "instrument": None, "action": "Starting...", "status": None,
                "paused": False, "started_at": datetime.now().isoformat(),
            })
        elif mtype == "workflow_progress":
            _current_workflow_status.update({
                "active": True, "workflow_name": msg.get("workflow_name"),
                "step": msg.get("step", 0), "total": msg.get("total", 0),
                "instrument": msg.get("instrument"), "action": msg.get("action"),
                "status": msg.get("status"), "message": msg.get("message"),
                "paused": msg.get("status") == "waiting_intervention",
            })
        elif mtype == "workflow_paused":
            _current_workflow_status["paused"] = True
            _current_workflow_status["message"] = msg.get("message")
        elif mtype == "workflow_resumed":
            _current_workflow_status["paused"] = False
        elif mtype in ("workflow_complete", "workflow_failed", "batch_complete"):
            _current_workflow_status.clear()
            _current_workflow_status["active"] = False

    async def _broadcast_workflow(msg: dict) -> None:
        """ws_manager.broadcast() plus keeping _current_workflow_status in
        sync - use this (not ws_manager.broadcast directly) for every
        workflow_*/batch_complete message so a late-joining client's
        GET /api/workflows/live-status can never disagree with what was
        actually last broadcast."""
        _track_workflow_status(msg)
        await ws_manager.broadcast(msg)

    @router.get("/api/workflows/live-status")
    async def get_live_workflow_status():
        """Current workflow snapshot, for a page to sync on load instead of
        only ever learning about an already-running workflow from the next
        websocket push (see _current_workflow_status above)."""
        return dict(_current_workflow_status)

    # CRUD

    @router.get("/api/workflows")
    async def get_workflows():
        workflows = []
        wf_dir = LIBRARY_DIR / "Workflows"
        if wf_dir.exists():
            for f in wf_dir.glob("*.workflow.json"):
                try:
                    wf = json.loads(f.read_text(encoding='utf-8-sig'))
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
                return json.loads(wf_path.read_text(encoding='utf-8-sig'))
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

        # Take it out of the operator queue now rather than waiting for the
        # waiter's own cleanup, so the buttons disappear on the next refresh
        # instead of inviting a second click.
        async with pending_operator_actions_lock:
            pending_operator_actions[:] = [
                a for a in pending_operator_actions
                if a.get("id") != intervention_id
            ]

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
        """Pause the active run. Kept as an alias of /api/runs/{id}/pause."""
        result = runs.pause()
        if result is None:
            return {"status": "idle", "message": "No active workflow"}
        state.add_log("info", "Workflow pause requested", "workflow")
        await _broadcast_workflow({"type": "workflow_paused", "run_id": result.run_id,
                                   "message": "Workflow paused by operator"})
        return {"status": "paused", "message": "Pause requested", "run_id": result.run_id}

    @router.post("/api/workflows/resume")
    async def resume_workflow():
        result = runs.resume()
        if result is None:
            return {"status": "idle", "message": "No active workflow"}
        state.add_log("info", "Workflow resumed by operator", "workflow")
        await _broadcast_workflow({"type": "workflow_resumed", "run_id": result.run_id,
                                   "message": "Workflow resumed by operator"})
        return {"status": "resumed", "message": "Workflow resumed", "run_id": result.run_id}

    @router.post("/api/workflows/abort")
    async def abort_workflow():
        """Abort the active run and report what each instrument actually did.

        The response says what happened, not merely that a stop was requested:
        an instrument that did not confirm the stop is the single most
        important thing to put in front of the operator immediately.
        """
        active = runs.active()
        if active is None:
            return {"status": "idle", "message": "No active workflow"}

        result = await runs.abort(active.run_id)
        report = result.get("report", [])
        unresolved = result.get("requires_attention", False)

        state.add_log("warning", f"Workflow aborted by operator: {'; '.join(report)}", "workflow")
        await _broadcast_workflow({
            "type": "workflow_failed",
            "workflow_name": active.workflow_name,
            "run_id": active.run_id,
            "error": "Aborted by operator",
            "abort_report": report,
            "requires_attention": unresolved,
        })
        return {
            "status": "aborted",
            "message": "Abort requested",
            "run_id": active.run_id,
            "report": report,
            "requires_attention": unresolved,
            "warning": ("Some instruments did not confirm the stop — verify them physically"
                        if unresolved else None),
        }

    # Execute workflow

    @router.post("/api/workflows/execute")
    async def execute_workflow(request: Request):
        """Start a workflow and return its run id immediately.

        This used to hold the executor lock *and this HTTP connection* for the
        whole run — hours, for a workflow that incubates a plate — and only
        return the run id at the very end, so a closed tab or a proxy read
        timeout lost the reply entirely. The run is now a background job:
        poll GET /api/runs/{run_id} or listen on the WebSocket.
        """
        data = await request.json()
        workflow_name = data.get("WorkflowName", "Unknown")
        try:
            run_state = await runs.start(
                workflow_name,
                lambda run_id: _run_workflow(data, run_id),
                total_steps=len(data.get("Steps", [])),
            )
        except RunBusy as busy:
            return {
                "status": "busy",
                "error": str(busy),
                "active_run": busy.active.to_dict(),
            }
        # `status` carries the run's own state ("running"), the same field and
        # the same vocabulary GET /api/runs/{id} uses, so a caller does not have
        # to learn a second one. `started` says the request was accepted; the
        # thing to hold on to is `run_id`.
        return {"started": True, **run_state.to_dict()}

    @router.post("/api/runs")
    async def start_run(request: Request):
        """Alias of /api/workflows/execute under the resource-shaped path."""
        return await execute_workflow(request)

    @router.get("/api/runs")
    async def list_runs_in_memory():
        return {"runs": [r.to_dict() for r in runs.list_runs()],
                "active": (runs.active().run_id if runs.active() else None)}

    @router.get("/api/runs/{run_id}")
    async def get_run_state(run_id: str):
        state_obj = runs.get(run_id)
        if state_obj is not None:
            return state_obj.to_dict()
        # Not in memory (an older run, or after a restart): the database is the
        # durable record.
        if _db is not None:
            record = _db.get_run(run_id)
            if record:
                return {"run_id": run_id, "status": record.get("status"),
                        "workflow_name": record.get("workflow_name"),
                        "active": False, "from_database": True, **record}
        raise HTTPException(404, f"Run {run_id} not found")

    @router.get("/api/runs/{run_id}/steps")
    async def get_run_step_timeline(run_id: str):
        """Every recorded step attempt, in order — the run's real timeline.

        Available while the run is still going: steps are written as they
        start and finish, not replayed at the end.
        """
        if _db is None:
            return {"run_id": run_id, "steps": []}
        return {"run_id": run_id, "steps": _db.get_run_steps(run_id)}

    @router.post("/api/runs/{run_id}/pause")
    async def pause_run(run_id: str):
        result = runs.pause(run_id)
        if result is None:
            return {"status": "idle", "message": "No such active run"}
        await _broadcast_workflow({"type": "workflow_paused", "run_id": run_id,
                                   "message": "Workflow paused by operator"})
        return result.to_dict()

    @router.post("/api/runs/{run_id}/resume")
    async def resume_run(run_id: str):
        result = runs.resume(run_id)
        if result is None:
            return {"status": "idle", "message": "No such active run"}
        await _broadcast_workflow({"type": "workflow_resumed", "run_id": run_id,
                                   "message": "Workflow resumed by operator"})
        return result.to_dict()

    @router.post("/api/runs/{run_id}/abort")
    async def abort_run(run_id: str):
        result = await runs.abort(run_id)
        state.add_log("warning", f"Run {run_id} aborted: {'; '.join(result.get('report', []))}",
                      "workflow")
        await _broadcast_workflow({
            "type": "workflow_failed", "run_id": run_id,
            "workflow_name": "active", "error": "Aborted by operator",
            "abort_report": result.get("report", []),
            "requires_attention": result.get("requires_attention", False),
        })
        return result

    async def _run_workflow(data: dict, _run_id: str):
        nonlocal active_executor
        workflow_name = data.get("WorkflowName", "Unknown")
        # Optional variables dict provided by frontend (Settimana 8: dialog variabili)
        user_variables = data.get("variables", {})

        # Resolve ${var} in step parameters before building the Workflow object
        if user_variables:
            steps = data.get("Steps", [])
            resolved_steps = _resolve_variables(steps, user_variables)
            data = {**data, "Steps": resolved_steps}

        state.add_log("info", f"Starting workflow: {workflow_name}", "workflow")
        await _broadcast_workflow({
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

        # Always run discover() — the 30 s TTL cache in lab_core makes this a
        # no-op when instruments are fresh, but it ensures the sila2 SilaClient
        # is alive for every workflow run (mirrors what the dashboard does).
        await _exec_core.discover()

        registry = PnPRegistry(BASE_DIR)
        for instr in _exec_core.list_instruments():
            if hasattr(instr, '_server') and instr._server:
                registry.register(instr.id, instr._server)

        # Pre-flight: every instrument name must resolve to exactly one live
        # instrument before anything physical starts.
        _problems = []
        for _step in data.get("Steps", []):
            _instr = (_step.get("Instrument") or "").strip()
            if not _instr or is_locally_handled(_instr, _step.get("Action", "")):
                continue
            _match = _exec_core.resolve_instrument(_instr)
            if not _match and _match.error:
                _problems.append(_match.error)
        if _problems:
            return {"status": "error", "error": " | ".join(dict.fromkeys(_problems))}

        executor = PnPWorkflowExecutor(registry)
        runs.attach_executor(_run_id, executor)
        shared_wf_ctx: dict = {
            "plate_id": None, "current_plate_id": None,
            "step_results": [], "workflow_name": workflow_name,
            "run_id": _run_id,
        }

        if _db is not None:
            # Create the run_results row up front, before any step executes.
            # plates.run_id and instrument_measurements.run_id both carry a
            # FOREIGN KEY REFERENCES run_results(run_id) - without this,
            # every plate/measurement saved mid-run (i.e. always, since
            # _save_run_archive() only runs at the very end) would silently
            # fail the FK check and never actually persist. Also means a run
            # that crashes before reaching _save_run_archive() still leaves a
            # "running" record instead of vanishing from Run History.
            try:
                _db.save_run({
                    "run_id": _run_id, "workflow_name": workflow_name,
                    "started_at": datetime.now().isoformat(), "status": "running",
                    "steps_completed": 0, "steps_failed": 0, "steps_skipped": 0,
                    "step_results": [],
                })
            except Exception as exc:
                logger.warning(f"Failed to pre-create run_results row for {_run_id}: {exc}")

        # Attempt counters and start times, so a retry is a distinct row in the
        # timeline instead of overwriting the attempt that failed.
        _attempts: dict = {}
        _step_started: dict = {}

        async def web_execute_step(step, wf_context) -> "CommandResult":
            if wf_context and wf_context.plate_id and not shared_wf_ctx["plate_id"]:
                shared_wf_ctx["plate_id"] = wf_context.plate_id

            _n = getattr(step, "step_number", None)
            _attempts[_n] = _attempts.get(_n, 0) + 1
            _step_started[_n] = datetime.now()
            runs.record_step(_run_id, {
                "step_number": _n,
                "attempt": _attempts[_n],
                "instrument": step.instrument,
                "action": step.action,
                "parameters": step.parameters,
                "status": "running",
                "started_at": _step_started[_n].isoformat(),
            })

            try:
                result_dict = await _execute_step(
                    step.instrument, step.action, step.parameters,
                    state, ws_manager, _exec_core, shared_wf_ctx,
                    pending_operator_actions, pending_operator_actions_lock,
                    base_dir=BASE_DIR,
                )
            except (Exception, asyncio.CancelledError) as exc:
                # CancelledError too, and it is not an Exception subclass: a
                # step cut off by its own TimeoutSeconds is cancelled from
                # outside, so without this its row stayed "running" exactly
                # like an uncaught failure used to.
                #
                # A failing step raises, so the completion write below never
                # ran and the row stayed "running" for ever - four retries of
                # one step showed up in the timeline as four steps still in
                # progress, with no reason recorded against any of them. Close
                # the row here, then let the failure carry on to the retry and
                # intervention machinery.
                _failed_at = datetime.now()
                _began_at = _step_started.pop(_n, None)
                runs.record_step(_run_id, {
                    "step_number": _n,
                    "attempt": _attempts.get(_n, 1),
                    "instrument": step.instrument,
                    "action": step.action,
                    "parameters": step.parameters,
                    "status": "timeout" if isinstance(exc, asyncio.CancelledError) else "failed",
                    "ended_at": _failed_at.isoformat(),
                    "duration_seconds": ((_failed_at - _began_at).total_seconds()
                                         if _began_at else None),
                    "error": (str(exc) or "interrotto: TimeoutSeconds dello step scaduto"
                              if isinstance(exc, asyncio.CancelledError) else str(exc)),
                })
                raise

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

            final_status = r_status if r_status else ("completed" if success else "error")
            step_record = {
                "step": getattr(step, 'step_number', None),
                "instrument": step.instrument,
                "action": step.action,
                "status": final_status,
                "error": error,
                "timestamp": datetime.now().isoformat(),
            }
            shared_wf_ctx["step_results"].append(step_record)

            # Persist the outcome now, not in an end-of-run replay: a run that
            # crashes mid-way must still leave a diagnosable trail.
            _ended = datetime.now()
            _n = getattr(step, "step_number", None)
            _began = _step_started.pop(_n, None)
            runs.record_step(_run_id, {
                "step_number": _n,
                "attempt": _attempts.get(_n, 1),
                "instrument": step.instrument,
                "action": step.action,
                "parameters": step.parameters,
                "status": "success" if success else final_status,
                "ended_at": _ended.isoformat(),
                "duration_seconds": (_ended - _began).total_seconds() if _began else None,
                "error": error,
                "result": result_dict if isinstance(result_dict, dict) else None,
                "plate_id": shared_wf_ctx.get("current_plate_id"),
            })

            return CommandResult(success=success, data=result_dict, error=error, status=r_status)

        executor.set_step_executor(web_execute_step)
        active_executor = executor

        def on_progress(p: "WorkflowProgress"):
            _msg = {
                "type": "workflow_progress",
                "workflow_name": p.workflow_name,
                "step": p.current_step,
                "total": p.total_steps,
                "instrument": p.step_instrument,
                "action": p.step_action,
                "status": p.step_status.value,
                "message": p.message,
                "percent": round((p.current_step / p.total_steps * 100) if p.total_steps else 0),
            }
            _track_workflow_status(_msg)
            runs.record_progress(_run_id, _msg)
            asyncio.create_task(ws_manager.broadcast(_msg))

        executor.add_progress_callback(on_progress)

        async def on_intervention(req) -> "InterventionAction":
            iid = f"step_{req.step_number}_{int(datetime.now().timestamp())}"
            total_steps = len(workflow_obj.steps)
            await _broadcast_workflow({
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
            return await _ask_operator(
                req,
                intervention_id=iid,
                pending_interventions=pending_interventions,
                pending_operator_actions=pending_operator_actions,
                pending_operator_actions_lock=pending_operator_actions_lock,
                ws_manager=ws_manager,
                state=state,
                base_dir=BASE_DIR,
                extra_broadcast={"workflow_name": workflow_name},
            )

        executor.set_intervention_callback(on_intervention)

        _parallel = get_settings(BASE_DIR).workflow.parallel_execution

        try:
            # Validate upfront so dependency/instrument/command issues are explicit
            # instead of being silently skipped at runtime.
            result = await executor.execute(workflow_obj, validate=True, parallel=_parallel)
        except Exception as e:
            state.add_log("error", f"Workflow error: {e}", "workflow")
            await _broadcast_workflow({"type": "workflow_failed", "workflow_name": workflow_name, "error": str(e)})
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
            await _broadcast_workflow({
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
                # A run can finish successfully and still have something worth
                # reading: a step the operator chose to skip says so here.
                # Without this the summary showed "1 skipped" and no hint of
                # which one, or why.
                "notes": result.errors,
            }
        else:
            state.add_log("error", f"Workflow failed: {result.errors}", "workflow")
            await _broadcast_workflow({
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

    # Note: the persistent, UI-facing run archive is the SQLite-backed
    # /api/db/runs (routes/db.py), used by the Run History page. The
    # Results/runs/*.json files written by _save_run_archive() are kept
    # purely as a redundant flat-file backup — no route reads them back
    # (a duplicate read-only API here was removed as dead/confusing).

    # Experiment Templates (Settimana 9)

    TEMPLATES_DIR = LIBRARY_DIR / "Templates"

    @router.get("/api/templates")
    async def list_templates():
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        templates_list = []
        for f in sorted(TEMPLATES_DIR.glob("*.template.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                t = json.loads(f.read_text(encoding='utf-8-sig'))
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
                return json.loads(t_path.read_text(encoding='utf-8-sig'))
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
        tmpl = json.loads(t_path.read_text(encoding='utf-8-sig'))
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
        content = wf_path.read_text(encoding='utf-8-sig')
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
                meta = json.loads(f.read_text(encoding='utf-8-sig'))
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
        data = json.loads(wf_path.read_text(encoding='utf-8-sig'))
        vid = _create_version_snapshot(LIBRARY_DIR, workflow_id, data, note)
        return {"success": True, "version_id": vid}

    @router.get("/api/workflows/{workflow_id}/versions/{version_id}")
    async def get_version(workflow_id: str, version_id: str):
        v_path = LIBRARY_DIR / "Workflows" / ".versions" / workflow_id / f"{version_id}.json"
        if not v_path.exists():
            raise HTTPException(404, "Version not found")
        return json.loads(v_path.read_text(encoding='utf-8-sig'))

    @router.post("/api/workflows/{workflow_id}/versions/{version_id}/restore")
    async def restore_version(workflow_id: str, version_id: str):
        v_path = LIBRARY_DIR / "Workflows" / ".versions" / workflow_id / f"{version_id}.json"
        if not v_path.exists():
            raise HTTPException(404, "Version not found")
        data = json.loads(v_path.read_text(encoding='utf-8-sig'))
        # Snapshot current before overwriting
        wf_dir = LIBRARY_DIR / "Workflows"
        safe = re.sub(r'[^\w\-. ]', '', workflow_id).strip().replace(" ", "_")
        current_path = wf_dir / f"{safe}.workflow.json"
        if current_path.exists():
            current = json.loads(current_path.read_text(encoding='utf-8-sig'))
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

        _active = runs.active()
        if _active is not None:
            raise HTTPException(409, str(RunBusy(_active)))

        batch_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        workflow_name = workflow_def.get("WorkflowName", "Batch")

        _parallel = get_settings(BASE_DIR).workflow.parallel_execution

        state.add_log("info", f"Batch started: {batch_id} ({len(samples)} samples)", "workflow")
        await ws_manager.broadcast({
            "type": "batch_start",
            "batch_id": batch_id,
            "workflow_name": workflow_name,
            "total_samples": len(samples),
        })

        run_ids = []
        errors = []

        # A batch holds the single run slot for its whole duration: it drives
        # the same physical instruments, one sample after another.
        batch_state = await runs.start(
            f"{workflow_name} (batch of {len(samples)})",
            lambda _rid: _noop_body(),
            total_steps=len(samples),
            run_id=batch_id,
        )
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
                            pending_operator_actions, pending_operator_actions_lock,
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
                        _msg = {
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
                        }
                        _track_workflow_status(_msg)
                        asyncio.create_task(ws_manager.broadcast(_msg))

                    async def on_batch_intervention(req) -> "InterventionAction":
                        iid = f"batch_{batch_id}_step_{req.step_number}_{int(datetime.now().timestamp())}"
                        return await _ask_operator(
                            req,
                            intervention_id=iid,
                            pending_interventions=pending_interventions,
                            pending_operator_actions=pending_operator_actions,
                            pending_operator_actions_lock=pending_operator_actions_lock,
                            ws_manager=ws_manager,
                            state=state,
                            base_dir=BASE_DIR,
                            extra_broadcast={"workflow_name": req.workflow_name,
                                             "sample_id": sample_id,
                                             "batch_id": batch_id},
                        )

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
                            run_data = json.loads(run_file.read_text(encoding='utf-8-sig'))
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

            await _broadcast_workflow({
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
            runs.release(batch_id)

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

async def _noop_body() -> dict:
    """Body for a slot a caller drives itself (the batch runner).

    RunManager.start() wants something to supervise; a batch already owns its
    own loop over samples, so it just needs the slot reserved and released.
    """
    return {"status": "running"}


def _timeouts(base_dir: Optional[Path]):
    """Operator-facing timeouts, read at call time so a Settings edit applies
    to the next step without restarting the webapp."""
    return get_settings(base_dir).webapp.timeouts


async def _execute_step(
    instrument: str,
    action: str,
    params: dict,
    state,
    ws_manager,
    core,
    workflow_context: dict,
    pending_operator_actions: list,
    pending_operator_actions_lock: asyncio.Lock,
    base_dir: Optional[Path] = None,
) -> dict:
    """Execute one workflow step: a delay, or a command on an instrument.

    Every step that is not the builder's Delay block goes to a SiLA2 server,
    and that includes the people. A manual step is a command on the Manual
    Station server, which puts the task on the Operator page and returns when
    the operator confirms it - the same path as any other instrument.

    This used to intercept any instrument with "manual" in its name, and any
    action with "refill" in its name, and handle both here instead. The Manual
    Station server was then never called by a workflow; a workflow that asked
    the operator to refill tips and then recorded the refill asked the
    operator twice; and SendNotification became a confirmation that blocked
    the run until someone clicked.
    """

    # Delay
    if is_locally_handled(instrument, action):
        seconds = params.get("seconds", 30)
        state.add_log("info", f"Waiting {seconds} seconds...", "workflow")
        await asyncio.sleep(seconds)
        return {"action": "delay", "seconds": seconds, "status": "completed"}

    # Generic instrument via LabCore, through the shared resolver: an ambiguous
    # name is reported instead of being silently decided by dict ordering.
    if not core:
        msg = f"Cannot execute '{action}': LabCore is not available"
        logger.error(f"[WORKFLOW] {msg}")
        state.add_log("error", msg, "workflow")
        return {"status": "error", "error": msg}

    match = core.resolve_instrument(instrument)
    if not match:
        msg = match.error or f"Instrument '{instrument}' not found"
        logger.error(f"[WORKFLOW] {msg}")
        state.add_log("error", msg, "workflow")
        return {"status": "error", "error": msg}

    matched_instr = match.target
    device_id = matched_instr.id
    if device_id in state.devices:
        state.devices[device_id].status = "busy"
        await ws_manager.broadcast({"type": "device_update", "device_id": device_id, "status": "busy"})

    # A command that measures a plate gets the plate prepared earlier in this
    # run, when the step did not name one. Which commands measure plates, and
    # which parameter carries the plate, is what the server declared; the
    # parameters it may leave out are filled from its declared defaults at
    # dispatch (src/client.py).
    role = core.role_of(device_id, action)
    if (role is not None and role.role == "measures_plate" and role.plate_parameter
            and not params.get(role.plate_parameter) and workflow_context):
        params = dict(params)
        params[role.plate_parameter] = workflow_context.get("current_plate_id") or ""

    try:
        result = await core.execute_command(device_id, action, params)
        logger.info(f"[WORKFLOW] {device_id}/{action} → success={result.success}, error={result.error}")

        # A validation that comes back negative is a successful *command* - it
        # ran, and the verdict is in its result - but inside a workflow it is a
        # failed *step*. The robot cannot execute that recipe, so every step
        # after it is going to fail anyway, on a deck that has already been
        # touched. Better to stop here and ask, while nothing has moved.
        #
        # Only inside a workflow: run from the Dashboard, ValidateRecipe stays
        # what it is, a check that reports and changes nothing.
        invalid = _validation_failure(action, result)
        if invalid:
            raise Exception(invalid)

        if result.success:
            state.add_log("info", f"{action} completed on {device_id}", "workflow")

            # Plate ID assignment + DB population (Database page: Plates tab)
            new_plate_id = plate_tracking.track_plate_measurement(
                device_id, action, params, result, role=role,
                run_id=workflow_context.get("run_id") if workflow_context else None,
                current_plate_id=workflow_context.get("current_plate_id") if workflow_context else None,
                base_dir=base_dir, state=state,
            )
            if new_plate_id and workflow_context is not None:
                workflow_context.setdefault("plate_id", new_plate_id)
                workflow_context["current_plate_id"] = new_plate_id

            new_plate_id = plate_tracking.track_plate_preparation(
                device_id, action, params, result, role=role,
                run_id=workflow_context.get("run_id") if workflow_context else None,
                base_dir=base_dir, state=state,
            )
            if new_plate_id and workflow_context is not None:
                workflow_context["plate_id"] = new_plate_id
                workflow_context["current_plate_id"] = new_plate_id

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
                    pending_operator_actions_lock, _timeouts(base_dir).tip_refill_timeout,
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


def _validation_failure(action: str, result) -> Optional[str]:
    """The complaint a negative validation should stop the workflow with.

    None when the step is not a validation, or the recipe checks out.
    """
    if action.lower() != "validaterecipe" or not result.success:
        return None
    payload = result.data
    if isinstance(payload, dict):
        payload = next((v for v in payload.values() if isinstance(v, str)), "")
    if not isinstance(payload, str) or not payload.strip():
        return None
    try:
        verdict = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(verdict, dict) or verdict.get("valid") is not False:
        return None
    problems = verdict.get("errors") or ["no reason given"]
    config = verdict.get("hal_config")
    return ("La ricetta non è eseguibile con la configurazione hardware attiva"
            + (f" ({config})" if config else "") + ": "
            + "; ".join(str(e) for e in problems))


async def _ask_operator(
    req,
    *,
    intervention_id: str,
    pending_interventions: dict,
    pending_operator_actions: list,
    pending_operator_actions_lock,
    ws_manager,
    state,
    base_dir,
    extra_broadcast: Optional[dict] = None,
):
    """Ask the operator to retry, skip or abort a step that failed.

    The request goes into `pending_operator_actions` - the same queue manual
    steps and tip refills use - and not only onto the websocket.

    It used to be websocket-only, and that never worked, in any version of this
    file. The Operator page pushes an incoming `intervention_required` into a
    local array, and every three seconds `refreshPendingActions()` replaces that
    array wholesale with `GET /api/operator/pending`, which interventions were
    never part of. So the Retry/Skip/Abort buttons appeared and were wiped
    within three seconds, before anyone could click them - and a page opened
    after the failure, or reloaded, never saw the request at all. The workflow
    then sat for the full hour and gave up on its own.

    Being in the queue means it survives the refresh, survives a reload, and is
    there for whoever walks up to the machine afterwards.
    """
    from src.lab_core import InterventionAction

    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    pending_interventions[intervention_id] = future

    notification = {
        # The Operator page renders any entry carrying `is_intervention` with
        # Retry/Skip/Abort instead of a Confirm button; `id` is what its
        # acknowledge bookkeeping keys on.
        "id": intervention_id,
        "is_intervention": True,
        "intervention_id": intervention_id,
        "type": "intervention_required",
        "title": f"Step {req.step_number} fallito — serve una decisione",
        "message": (f"{req.instrument} → {req.action}\n"
                    f"{req.error.details or req.error.message}"),
        "priority": "urgent",
        "requires_action": True,
        "timestamp": datetime.now().isoformat(),
        "step": req.step_number,
        # The modal in base.html, which pops on whatever page the operator
        # happens to be looking at, reads step_number/error_message/attempt/
        # max_attempts. Nothing has ever sent those, so it has always rendered
        # "Step ? - / Unknown error / Attempt ? / ?" - a dialog demanding a
        # decision while telling the reader nothing. Both spellings go out.
        "step_number": req.step_number,
        "attempt": req.attempt,
        "max_attempts": req.max_attempts,
        "instrument": req.instrument,
        "action": req.action,
        # CategorizedError.message is a category label ("Hardware error:
        # ValidateRecipe on Opentrons Flex"); what tells an operator what
        # actually went wrong is .details, the raw error text. Asking for a
        # decision without saying what it is about is not a decision.
        "error": req.error.details or req.error.message,
        "error_message": req.error.details or req.error.message,
        "category": req.error.category.value,
        "category_label": req.error.message,
    }
    async with pending_operator_actions_lock:
        pending_operator_actions.append(notification)

    broadcast = dict(notification)
    if extra_broadcast:
        broadcast.update(extra_broadcast)
    await ws_manager.broadcast(broadcast)
    state.add_log("warning",
                  f"Step {req.step_number} failed on {req.instrument}: "
                  f"{req.error.details or req.error.message} "
                  f"— waiting for a decision on the Operator page", "workflow")

    timeout = get_settings(base_dir).webapp.timeouts.intervention_timeout
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        # Abort, not skip. Nobody answered, so nobody knows whether the rest of
        # the workflow still makes sense without this step - and carrying on
        # without it is the outcome that quietly ruins a plate. Skipping stays
        # available, as a decision someone takes.
        state.add_log("error",
                      f"No decision on step {req.step_number} within {timeout}s "
                      f"— aborting the workflow", "workflow")
        await ws_manager.broadcast({
            "type": "intervention_timeout",
            "intervention_id": intervention_id,
            "step": req.step_number,
            "message": f"Nessuna risposta entro {timeout}s: workflow interrotto",
        })
        return InterventionAction.ABORT
    finally:
        pending_interventions.pop(intervention_id, None)
        async with pending_operator_actions_lock:
            pending_operator_actions[:] = [
                a for a in pending_operator_actions
                if a.get("id") != intervention_id
            ]
        await ws_manager.broadcast({
            "type": "intervention_resolved",
            "intervention_id": intervention_id,
        })


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
            if str(notification_id) not in [str(a.get("id")) for a in pending_operator_actions]:
                break
        await asyncio.sleep(1)
        if timeout is not None and (datetime.now() - start).total_seconds() > timeout:
            async with lock:
                pending_operator_actions[:] = [a for a in pending_operator_actions if str(a.get("id")) != str(notification_id)]
            raise TimeoutError(f"Operator did not refill tips within {timeout}s")

    state.add_log("info", f"Tip rack refilled: {rack_type}", "workflow")
    return {"status": "completed", "action": "RefillTipRack", "rack_type": rack_type}

# Plate/measurement DB tracking (moved to src/plate_tracking.py, imported as
# plate_tracking) - shared with webapp/routes/instruments.py's manual command
# execution path.
