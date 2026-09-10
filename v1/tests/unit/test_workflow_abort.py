"""Abort must reach the instrument, not just set a flag.

Before this, request_abort() set a boolean that the executor read *between*
steps. During a two-hour ExecuteRecipe it therefore did nothing at all, and
the robot was never told anything: the operator pressed Abort, the UI said
"aborted", and the run carried on.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.client import CommandResult  # noqa: E402
from src.workflow import PnPWorkflowExecutor, Workflow, WorkflowContext, WorkflowStep  # noqa: E402


def _server_with(commands):
    server = MagicMock()
    server.get_all_commands.return_value = [("Feature", c, MagicMock()) for c in commands]
    return server


def _executor(server_commands=("AbortRun", "ExecuteRecipe"), block: asyncio.Event = None):
    registry = MagicMock()
    server = _server_with(server_commands)
    registry.get_server_by_name.return_value = server
    registry.servers = {"opentrons": server}

    calls = []

    async def _execute(instrument, action, parameters, timeout=None):
        calls.append(action)
        if action in ("AbortRun", "CancelTask", "Stop", "EmergencyStop"):
            return CommandResult(success=True, data={})
        if block is not None:
            await block.wait()          # a long-running instrument command
        return CommandResult(success=True, data={})

    registry.execute = AsyncMock(side_effect=_execute)
    ex = PnPWorkflowExecutor(registry)
    ex._error_config = ex._error_config.__class__(
        retry_strategy="none", base_delay_seconds=0.0, max_delay_seconds=0.0,
        max_retries=0, enable_human_intervention=False,
    )
    return ex, calls


def _step(number, depends_on=None):
    return WorkflowStep(step_number=number, instrument="opentrons", action="ExecuteRecipe",
                        parameters={}, depends_on=depends_on or [], retry_count=0)


class TestAbortReachesTheInstrument:

    @pytest.mark.asyncio
    async def test_abort_mid_step_sends_the_instruments_own_stop_command(self):
        block = asyncio.Event()
        ex, calls = _executor(block=block)
        workflow = Workflow(name="W", steps=[_step(1), _step(2, [1])])
        import datetime
        workflow.start_time = datetime.datetime.now()

        run = asyncio.create_task(ex._execute_waves(workflow, WorkflowContext(), parallel=False))
        await asyncio.sleep(0.05)          # let step 1 start and block

        ex.request_abort()
        completed, failed, skipped, _, _ = await asyncio.wait_for(run, timeout=5)

        assert "AbortRun" in calls, "the robot was never told to stop"
        assert completed == 0
        assert skipped >= 1
        assert any("AbortRun sent" in line for line in ex.last_abort_report)

    @pytest.mark.asyncio
    async def test_abort_stops_waiting_on_the_step(self):
        """The blocking command never returns; abort must still finish the run."""
        block = asyncio.Event()          # deliberately never set
        ex, _ = _executor(block=block)
        workflow = Workflow(name="W", steps=[_step(1)])
        import datetime
        workflow.start_time = datetime.datetime.now()

        run = asyncio.create_task(ex._execute_waves(workflow, WorkflowContext(), parallel=False))
        await asyncio.sleep(0.05)
        ex.request_abort()

        # Without cancellation this would hang until the test timeout.
        await asyncio.wait_for(run, timeout=5)

    @pytest.mark.asyncio
    async def test_the_stop_command_is_discovered_from_the_fdl_not_hardcoded(self):
        """An instrument exposing only CancelTask (the mobile robot) is aborted
        with CancelTask."""
        block = asyncio.Event()
        ex, calls = _executor(server_commands=("CancelTask", "ExecuteRecipe"), block=block)
        workflow = Workflow(name="W", steps=[_step(1)])
        import datetime
        workflow.start_time = datetime.datetime.now()

        run = asyncio.create_task(ex._execute_waves(workflow, WorkflowContext(), parallel=False))
        await asyncio.sleep(0.05)
        ex.request_abort()
        await asyncio.wait_for(run, timeout=5)

        assert "CancelTask" in calls

    @pytest.mark.asyncio
    async def test_an_instrument_with_no_stop_command_says_so(self):
        block = asyncio.Event()
        ex, calls = _executor(server_commands=("ExecuteRecipe",), block=block)
        workflow = Workflow(name="W", steps=[_step(1)])
        import datetime
        workflow.start_time = datetime.datetime.now()

        run = asyncio.create_task(ex._execute_waves(workflow, WorkflowContext(), parallel=False))
        await asyncio.sleep(0.05)
        ex.request_abort()
        await asyncio.wait_for(run, timeout=5)

        assert any("no abort command" in line for line in ex.last_abort_report)
        assert any("stop it at the instrument" in line for line in ex.last_abort_report)

    @pytest.mark.asyncio
    async def test_a_failed_stop_is_reported_not_swallowed(self):
        block = asyncio.Event()
        ex, _ = _executor(block=block)

        async def _execute(instrument, action, parameters, timeout=None):
            if action == "AbortRun":
                return CommandResult(success=False, error="robot unreachable")
            await block.wait()
            return CommandResult(success=True, data={})

        ex.registry.execute = AsyncMock(side_effect=_execute)
        workflow = Workflow(name="W", steps=[_step(1)])
        import datetime
        workflow.start_time = datetime.datetime.now()

        run = asyncio.create_task(ex._execute_waves(workflow, WorkflowContext(), parallel=False))
        await asyncio.sleep(0.05)
        ex.request_abort()
        await asyncio.wait_for(run, timeout=5)

        assert any("FAILED" in line and "verify manually" in line
                   for line in ex.last_abort_report)

    @pytest.mark.asyncio
    async def test_abort_with_nothing_running_is_reported_honestly(self):
        ex, _ = _executor()
        await ex._perform_abort()
        assert ex.last_abort_report == ["No step was executing"]

    def test_request_abort_outside_a_loop_still_sets_the_flag(self):
        """A synchronous caller gets the old between-steps behaviour rather
        than an exception."""
        ex, _ = _executor()
        ex.request_abort()
        assert ex._abort_requested is True
