"""Workflow runs as supervised background jobs.

`POST /api/workflows/execute` used to hold the executor lock *and the HTTP
connection* for the entire run. A workflow that pipettes a plate, incubates it
and reads it can legitimately take hours; the run id was generated at the start
but only returned at the very end, so a closed browser tab or a proxy read
timeout lost the reply entirely and the only remaining evidence a run existed
was whatever WebSocket messages had already been sent.

A run is now a background task. Starting one returns its id immediately, its
state is queryable over REST at any point, and every step transition is written
to `run_steps` as it happens — so a page opened halfway through a run, or after
a crash, can reconstruct exactly where things got to.

Concurrency: one run at a time, deliberately. The instruments are physical and
share a deck; a queue of pending runs is a product decision, not a technical
gap, and pretending otherwise by allowing two would be worse than refusing the
second. A refusal names the run that holds the slot.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Terminal states. Anything else means the run still owns the instruments.
FINISHED_STATES = frozenset({"completed", "failed", "aborted", "error"})


def make_run_id(workflow_name: str, *, now: Optional[datetime] = None) -> str:
    """`20260910_142530_My_Workflow` — sorts chronologically, readable in a path."""
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w\-]", "_", workflow_name or "workflow")
    return f"{stamp}_{safe}"


@dataclass
class RunState:
    """Everything a UI needs to render a run without replaying its events."""

    run_id: str
    workflow_name: str
    status: str = "queued"
    started_at: str = ""
    ended_at: Optional[str] = None
    total_steps: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    skipped_steps: int = 0
    current_step: Optional[int] = None
    current_instrument: Optional[str] = None
    current_action: Optional[str] = None
    message: str = ""
    paused: bool = False
    errors: List[str] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None

    @property
    def active(self) -> bool:
        return self.status not in FINISHED_STATES

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["active"] = self.active
        percent = 0
        if self.total_steps:
            done = self.completed_steps + self.failed_steps + self.skipped_steps
            percent = round(done / self.total_steps * 100)
        data["percent"] = percent
        return data


class RunBusy(RuntimeError):
    """Raised when a run is requested while another still owns the instruments."""

    def __init__(self, active: RunState):
        self.active = active
        super().__init__(
            f"'{active.workflow_name}' is already running (run {active.run_id}, "
            f"step {active.current_step or 0} of {active.total_steps}). "
            f"Wait for it to finish, or abort it first."
        )


class RunManager:
    """Starts, tracks and controls background workflow runs."""

    def __init__(self, db=None, *, history_limit: int = 50):
        self._db = db
        self._runs: Dict[str, RunState] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._executors: Dict[str, Any] = {}
        self._active_id: Optional[str] = None
        self._lock = asyncio.Lock()
        self._history_limit = history_limit

    # ── Query ───────────────────────────────────────────────────────────────

    def get(self, run_id: str) -> Optional[RunState]:
        return self._runs.get(run_id)

    def active(self) -> Optional[RunState]:
        """The run currently holding the instruments, if any."""
        if self._active_id is None:
            return None
        state = self._runs.get(self._active_id)
        if state is None or not state.active:
            self._active_id = None
            return None
        return state

    def list_runs(self) -> List[RunState]:
        """In-memory runs, newest first. The durable record is the database."""
        return sorted(self._runs.values(), key=lambda r: r.started_at, reverse=True)

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def start(
        self,
        workflow_name: str,
        body: Callable[[str], Awaitable[Dict[str, Any]]],
        *,
        total_steps: int = 0,
        executor: Any = None,
        run_id: Optional[str] = None,
    ) -> RunState:
        """Begin a run in the background and return its state immediately.

        `body` is an async callable taking the run id and returning the run's
        result dict. It is executed as a task; exceptions are captured into the
        run's state rather than surfacing as an unretrieved-exception warning
        on a task nobody awaited.

        Raises RunBusy if another run is still active.
        """
        async with self._lock:
            current = self.active()
            if current is not None:
                raise RunBusy(current)

            run_id = run_id or make_run_id(workflow_name)
            state = RunState(
                run_id=run_id,
                workflow_name=workflow_name,
                status="running",
                started_at=datetime.now().isoformat(),
                total_steps=total_steps,
            )
            self._runs[run_id] = state
            self._active_id = run_id
            if executor is not None:
                self._executors[run_id] = executor

            task = asyncio.create_task(self._supervise(run_id, body), name=f"run:{run_id}")
            self._tasks[run_id] = task

        self._prune()
        return state

    async def _supervise(self, run_id: str, body: Callable[[str], Awaitable[Dict[str, Any]]]):
        state = self._runs[run_id]
        try:
            result = await body(run_id)
            state.result = result if isinstance(result, dict) else {"result": result}
            status = str(state.result.get("status", "completed"))
            state.status = status if status in FINISHED_STATES else "completed"
        except asyncio.CancelledError:
            state.status = "aborted"
            state.message = "Aborted"
            raise
        except Exception as exc:
            logger.exception("Run %s failed", run_id)
            state.status = "error"
            state.message = str(exc)
            state.errors.append(str(exc))
            state.result = {"status": "error", "error": str(exc)}
        finally:
            state.ended_at = datetime.now().isoformat()
            state.paused = False
            if self._active_id == run_id:
                self._active_id = None
            self._tasks.pop(run_id, None)
            self._executors.pop(run_id, None)
            self._persist_status(state)

    # ── Control ─────────────────────────────────────────────────────────────

    def pause(self, run_id: Optional[str] = None) -> Optional[RunState]:
        state, executor = self._target(run_id)
        if state is None:
            return None
        if executor is not None:
            executor.request_pause()
        state.paused = True
        state.status = "paused"
        return state

    def resume(self, run_id: Optional[str] = None) -> Optional[RunState]:
        state, executor = self._target(run_id)
        if state is None:
            return None
        if executor is not None:
            executor.request_resume()
        state.paused = False
        state.status = "running"
        return state

    async def abort(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        """Stop a run and tell its instruments to stop too.

        Returns the executor's abort report so the caller can put an
        instrument that did *not* confirm the stop in front of the operator.
        """
        state, executor = self._target(run_id)
        if state is None:
            return {"status": "idle", "report": [], "requires_attention": False}

        report: List[str] = []
        if executor is not None:
            executor.request_abort()
            if hasattr(executor, "_perform_abort"):
                report = await executor._perform_abort()

        state.status = "aborted"
        state.paused = False
        state.message = "Aborted by operator"
        unresolved = [line for line in report
                      if "FAILED" in line or "no abort command" in line]
        return {
            "status": "aborted",
            "run_id": state.run_id,
            "report": report,
            "requires_attention": bool(unresolved),
        }

    def release(self, run_id: str, status: str = "completed") -> None:
        """Free the run slot for a caller that drives its own loop.

        The batch runner reserves the slot and then executes samples itself, so
        nothing supervises it; this is how it hands the slot back.
        """
        state = self._runs.get(run_id)
        if state is not None and state.active:
            state.status = status
            state.ended_at = datetime.now().isoformat()
            state.paused = False
            self._persist_status(state)
        if self._active_id == run_id:
            self._active_id = None
        self._executors.pop(run_id, None)

    def attach_executor(self, run_id: str, executor: Any) -> None:
        """Give the manager the executor driving a run.

        Built inside the run body (it needs a registry populated from live
        discovery), so it cannot be passed to start(). Without it, pause and
        abort have nothing to act on.
        """
        self._executors[run_id] = executor

    def _target(self, run_id: Optional[str]):
        state = self._runs.get(run_id) if run_id else self.active()
        if state is None:
            return None, None
        return state, self._executors.get(state.run_id)

    # ── Progress ────────────────────────────────────────────────────────────

    def record_progress(self, run_id: str, progress: Dict[str, Any]) -> None:
        """Fold one progress event into the run's state.

        Called from the executor's progress callback. Keeps the snapshot in
        step with what was broadcast, so a client that connects late and asks
        REST for the state cannot disagree with the last WebSocket message.
        """
        state = self._runs.get(run_id)
        if state is None:
            return
        status = str(progress.get("status") or "")
        state.current_step = progress.get("step", state.current_step)
        state.current_instrument = progress.get("instrument") or state.current_instrument
        state.current_action = progress.get("action") or state.current_action
        state.message = progress.get("message", "") or state.message
        if progress.get("total"):
            state.total_steps = progress["total"]
        if status == "success":
            state.completed_steps += 1
        elif status == "failed":
            state.failed_steps += 1
        elif status == "skipped":
            state.skipped_steps += 1
        elif status == "waiting_intervention":
            state.paused = True

    def record_step(self, run_id: str, step: Dict[str, Any]) -> None:
        """Persist one step transition to the database."""
        if self._db is None:
            return
        try:
            self._db.save_run_step({**step, "run_id": run_id})
        except Exception as exc:  # never let bookkeeping abort a live run
            logger.warning("Could not persist step for run %s: %s", run_id, exc)

    def _persist_status(self, state: RunState) -> None:
        if self._db is None:
            return
        try:
            self._db.update_run_status(
                state.run_id, state.status,
                completed_at=state.ended_at,
                steps_completed=state.completed_steps,
                steps_failed=state.failed_steps,
                steps_skipped=state.skipped_steps,
            )
        except Exception as exc:
            logger.warning("Could not persist final status for run %s: %s", state.run_id, exc)

    # ── Housekeeping ────────────────────────────────────────────────────────

    def _prune(self) -> None:
        """Bound the in-memory history; the database keeps the real record."""
        finished = [r for r in self._runs.values() if not r.active]
        if len(finished) <= self._history_limit:
            return
        finished.sort(key=lambda r: r.started_at)
        for state in finished[: len(finished) - self._history_limit]:
            self._runs.pop(state.run_id, None)


__all__ = ["FINISHED_STATES", "RunBusy", "RunManager", "RunState", "make_run_id"]
