"""Tests for src/run_manager.py — workflow runs as background jobs.

The behaviour under test is what POST /api/workflows/execute could not do
before: return a run id immediately, let the run continue past the request
that started it, and answer questions about it at any point.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.run_manager import RunBusy, RunManager, RunState, make_run_id  # noqa: E402


class TestMakeRunId:

    def test_sorts_chronologically_and_carries_the_name(self):
        import datetime
        run_id = make_run_id("My Workflow", now=datetime.datetime(2026, 9, 10, 14, 25, 30))
        assert run_id == "20260910_142530_My_Workflow"

    def test_unsafe_characters_are_replaced(self):
        assert "/" not in make_run_id("a/b:c*d")
        assert "*" not in make_run_id("a/b:c*d")

    def test_empty_name(self):
        assert make_run_id("").endswith("_workflow")


class TestStartingRuns:

    @pytest.mark.asyncio
    async def test_start_returns_immediately_while_the_run_continues(self):
        """The point of the whole module: a long run must not hold its caller."""
        manager = RunManager()
        release = asyncio.Event()

        async def body(run_id):
            await release.wait()
            return {"status": "completed"}

        state = await manager.start("Slow", body, total_steps=3)
        assert state.status == "running"
        assert state.active
        # the run is still going, and we already have its id
        assert manager.active().run_id == state.run_id

        release.set()
        await asyncio.sleep(0.05)
        assert manager.get(state.run_id).status == "completed"
        assert not manager.get(state.run_id).active

    @pytest.mark.asyncio
    async def test_a_second_run_is_refused_and_told_which_one_is_busy(self):
        manager = RunManager()
        release = asyncio.Event()

        async def body(run_id):
            await release.wait()
            return {"status": "completed"}

        first = await manager.start("First", body, total_steps=2)
        with pytest.raises(RunBusy) as excinfo:
            await manager.start("Second", body)

        message = str(excinfo.value)
        assert first.run_id in message
        assert "First" in message
        assert excinfo.value.active.run_id == first.run_id
        release.set()
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_the_slot_is_free_once_the_run_finishes(self):
        manager = RunManager()

        async def body(run_id):
            return {"status": "completed"}

        await manager.start("First", body)
        await asyncio.sleep(0.05)
        assert manager.active() is None
        await manager.start("Second", body)   # no RunBusy

    @pytest.mark.asyncio
    async def test_a_crashing_run_is_captured_not_lost(self):
        """An unretrieved exception on a task nobody awaits is invisible; the
        failure has to land in the run's state."""
        manager = RunManager()

        async def body(run_id):
            raise RuntimeError("instrument exploded")

        state = await manager.start("Doomed", body)
        await asyncio.sleep(0.05)

        finished = manager.get(state.run_id)
        assert finished.status == "error"
        assert "instrument exploded" in finished.message
        assert manager.active() is None, "a crash must still free the slot"

    @pytest.mark.asyncio
    async def test_a_failed_run_keeps_its_reported_status(self):
        manager = RunManager()

        async def body(run_id):
            return {"status": "failed", "errors": ["step 2"]}

        state = await manager.start("Failing", body)
        await asyncio.sleep(0.05)
        assert manager.get(state.run_id).status == "failed"


class TestProgress:

    @pytest.mark.asyncio
    async def test_progress_events_fold_into_the_snapshot(self):
        manager = RunManager()
        release = asyncio.Event()

        async def body(run_id):
            await release.wait()
            return {"status": "completed"}

        state = await manager.start("W", body, total_steps=3)
        manager.record_progress(state.run_id, {
            "step": 1, "total": 3, "instrument": "opentrons",
            "action": "ExecuteRecipe", "status": "success", "message": "done",
        })
        manager.record_progress(state.run_id, {"step": 2, "status": "failed"})
        manager.record_progress(state.run_id, {"step": 3, "status": "skipped"})

        snapshot = manager.get(state.run_id).to_dict()
        assert snapshot["completed_steps"] == 1
        assert snapshot["failed_steps"] == 1
        assert snapshot["skipped_steps"] == 1
        assert snapshot["current_step"] == 3
        assert snapshot["percent"] == 100
        release.set()
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_waiting_for_intervention_shows_as_paused(self):
        manager = RunManager()
        release = asyncio.Event()

        async def body(run_id):
            await release.wait()
            return {"status": "completed"}

        state = await manager.start("W", body, total_steps=1)
        manager.record_progress(state.run_id, {"step": 1, "status": "waiting_intervention"})
        assert manager.get(state.run_id).paused is True
        release.set()
        await asyncio.sleep(0.05)

    def test_progress_for_an_unknown_run_is_ignored(self):
        RunManager().record_progress("nope", {"step": 1})   # must not raise

    @pytest.mark.asyncio
    async def test_steps_are_persisted_through_the_database(self):
        db = MagicMock()
        manager = RunManager(db=db)
        manager.record_step("RUN-1", {"step_number": 1, "status": "running"})
        db.save_run_step.assert_called_once()
        assert db.save_run_step.call_args[0][0]["run_id"] == "RUN-1"

    def test_a_database_failure_never_breaks_a_live_run(self):
        db = MagicMock()
        db.save_run_step.side_effect = RuntimeError("disk full")
        RunManager(db=db).record_step("RUN-1", {"step_number": 1})  # must not raise


class TestControl:

    @pytest.mark.asyncio
    async def _running_manager(self):
        manager = RunManager()
        release = asyncio.Event()
        executor = MagicMock()
        executor._perform_abort = AsyncMock(return_value=["opentrons: AbortRun sent"])

        async def body(run_id):
            await release.wait()
            return {"status": "completed"}

        state = await manager.start("W", body, total_steps=2)
        manager.attach_executor(state.run_id, executor)
        return manager, state, executor, release

    @pytest.mark.asyncio
    async def test_pause_and_resume_reach_the_executor(self):
        manager, state, executor, release = await self._running_manager()
        manager.pause()
        assert manager.get(state.run_id).paused is True
        assert manager.get(state.run_id).status == "paused"
        executor.request_pause.assert_called_once()

        manager.resume()
        assert manager.get(state.run_id).paused is False
        assert manager.get(state.run_id).status == "running"
        executor.request_resume.assert_called_once()
        release.set()
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_abort_returns_the_instrument_report(self):
        manager, state, executor, release = await self._running_manager()
        result = await manager.abort()
        assert result["status"] == "aborted"
        assert result["report"] == ["opentrons: AbortRun sent"]
        assert result["requires_attention"] is False
        executor.request_abort.assert_called_once()
        release.set()
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_an_unstoppable_instrument_raises_requires_attention(self):
        manager, state, executor, release = await self._running_manager()
        executor._perform_abort = AsyncMock(
            return_value=["tecan: AbortRun FAILED (offline) — verify manually"]
        )
        result = await manager.abort()
        assert result["requires_attention"] is True
        release.set()
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_control_of_nothing_is_reported_not_crashed(self):
        manager = RunManager()
        assert manager.pause() is None
        assert manager.resume() is None
        assert (await manager.abort())["status"] == "idle"

    @pytest.mark.asyncio
    async def test_release_frees_a_slot_a_caller_drives_itself(self):
        """How the batch runner hands the slot back."""
        manager = RunManager()

        async def body(run_id):
            return {"status": "running"}

        state = await manager.start("Batch", body, run_id="batch_1")
        await asyncio.sleep(0.05)
        manager.release("batch_1")
        assert manager.active() is None


class TestHousekeeping:

    @pytest.mark.asyncio
    async def test_history_is_bounded(self):
        manager = RunManager(history_limit=3)

        async def body(run_id):
            return {"status": "completed"}

        for i in range(6):
            await manager.start(f"W{i}", body)
            await asyncio.sleep(0.02)
        assert len(manager.list_runs()) <= 4

    @pytest.mark.asyncio
    async def test_list_is_newest_first(self):
        manager = RunManager()

        async def body(run_id):
            return {"status": "completed"}

        first = await manager.start("First", body)
        await asyncio.sleep(0.02)
        second = await manager.start("Second", body)
        await asyncio.sleep(0.02)
        assert manager.list_runs()[0].run_id == second.run_id


class TestRunState:

    def test_percent_counts_every_resolved_step(self):
        state = RunState(run_id="r", workflow_name="w", total_steps=4,
                         completed_steps=1, failed_steps=1, skipped_steps=1)
        assert state.to_dict()["percent"] == 75

    def test_percent_with_no_steps_does_not_divide_by_zero(self):
        assert RunState(run_id="r", workflow_name="w").to_dict()["percent"] == 0

    def test_active_reflects_terminal_states(self):
        assert RunState(run_id="r", workflow_name="w", status="running").active
        for status in ("completed", "failed", "aborted", "error"):
            assert not RunState(run_id="r", workflow_name="w", status=status).active
