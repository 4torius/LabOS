"""Dependency ordering and strict validation in src/workflow.py.

Regression cover for the executor's central defect: sequential execution used
to walk the file top to bottom and mark a step SKIPPED when its dependency
happened to be written further down. Both execution modes now share one
topological ordering.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.client import CommandResult  # noqa: E402
from src.workflow import (  # noqa: E402
    PnPWorkflowExecutor,
    StepStatus,
    Workflow,
    WorkflowContext,
    WorkflowStep,
    find_dependency_cycles,
    is_locally_handled,
    topological_waves,
)


def _step(number, depends_on=None, **kw):
    return WorkflowStep(
        step_number=number,
        instrument=kw.pop("instrument", "test_instr"),
        action=kw.pop("action", "test_action"),
        parameters=kw.pop("parameters", {}),
        depends_on=depends_on or [],
        retry_count=kw.pop("retry_count", 0),
        **kw,
    )


def _wave_numbers(steps):
    return [[s.step_number for s in wave] for wave in topological_waves(steps)]


class TestTopologicalWaves:

    def test_independent_steps_form_one_wave_in_file_order(self):
        assert _wave_numbers([_step(3), _step(1), _step(2)]) == [[3, 1, 2]]

    def test_a_chain_becomes_one_step_per_wave(self):
        steps = [_step(1), _step(2, [1]), _step(3, [2])]
        assert _wave_numbers(steps) == [[1], [2], [3]]

    def test_dependency_written_later_in_the_file_is_reordered(self):
        """The core bug: step 1 depends on step 2, which appears after it.

        The old sequential executor reached step 1 first, saw an unsatisfied
        dependency, and marked it SKIPPED — a valid workflow silently lost a
        step because of the order it was typed in.
        """
        steps = [_step(1, depends_on=[2]), _step(2)]
        assert _wave_numbers(steps) == [[2], [1]]

    def test_diamond(self):
        steps = [_step(1), _step(2, [1]), _step(3, [1]), _step(4, [2, 3])]
        assert _wave_numbers(steps) == [[1], [2, 3], [4]]

    def test_unknown_dependency_is_ignored_here(self):
        """validate_workflow() reports it; ordering must not deadlock on it."""
        assert _wave_numbers([_step(1, depends_on=[99])]) == [[1]]

    def test_a_cycle_still_yields_every_step(self):
        steps = [_step(1, [2]), _step(2, [1])]
        assert sorted(n for wave in _wave_numbers(steps) for n in wave) == [1, 2]

    def test_empty(self):
        assert topological_waves([]) == []


class TestCycleDetection:

    def test_two_step_cycle(self):
        cycles = find_dependency_cycles([_step(1, [2]), _step(2, [1])])
        assert len(cycles) == 1
        assert set(cycles[0]) == {1, 2}

    def test_self_dependency(self):
        assert find_dependency_cycles([_step(1, [1])])

    def test_longer_cycle(self):
        cycles = find_dependency_cycles([_step(1, [3]), _step(2, [1]), _step(3, [2])])
        assert len(cycles) == 1
        assert set(cycles[0]) == {1, 2, 3}

    def test_a_valid_graph_has_none(self):
        assert find_dependency_cycles([_step(1), _step(2, [1]), _step(3, [1, 2])]) == []

    def test_each_loop_is_reported_once(self):
        steps = [_step(1, [2]), _step(2, [1]), _step(3, [4]), _step(4, [3])]
        assert len(find_dependency_cycles(steps)) == 2


class TestIsLocallyHandled:
    """Only the Delay block is the orchestrator's own. People are a station."""

    @pytest.mark.parametrize("instrument,action", [
        ("delay", "Wait"),
        ("Delay", "anything"),
        ("anything", "Wait"),
    ])
    def test_local(self, instrument, action):
        assert is_locally_handled(instrument, action)

    @pytest.mark.parametrize("instrument,action", [
        ("opentrons", "ExecuteRecipe"),
        ("tecan", "RunMeasurement"),
        ("mobile", "ExecuteTask"),
        ("manual_station", "RequestOperatorTask"),
        ("Manual Station", "SendNotification"),
        ("opentrons", "RefillTipRack"),
        ("Manual Pipettor", "Aspirate"),
        ("Delay Line Controller", "SetDelay"),
    ])
    def test_dispatched_to_a_server(self, instrument, action):
        assert not is_locally_handled(instrument, action)


# ── Execution ────────────────────────────────────────────────────────────────

def _executor(results_by_step=None, default_result=None):
    registry = MagicMock()
    registry.servers = {}

    async def _execute(instrument, action, parameters, timeout=None):
        number = parameters.get("_n")
        if results_by_step and number in results_by_step:
            return results_by_step[number]
        return default_result or CommandResult(success=True, data={})

    registry.execute = AsyncMock(side_effect=_execute)
    ex = PnPWorkflowExecutor(registry)
    ex._error_config = ex._error_config.__class__(
        retry_strategy="none", base_delay_seconds=0.0, max_delay_seconds=0.0,
        max_retries=0, enable_human_intervention=False,
    )
    return ex


async def _run(executor, steps, parallel=False):
    import datetime
    wf = Workflow(name="W", steps=steps)
    wf.start_time = datetime.datetime.now()
    for s in steps:
        s.parameters = {**s.parameters, "_n": s.step_number}
    return await executor._execute_waves(wf, WorkflowContext(), parallel=parallel)


class TestExecutionOrdering:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("parallel", [False, True])
    async def test_out_of_order_dependency_runs_instead_of_being_skipped(self, parallel):
        ex = _executor()
        steps = [_step(1, depends_on=[2]), _step(2)]
        completed, failed, skipped, _, errors = await _run(ex, steps, parallel)
        assert (completed, failed, skipped) == (2, 0, 0)
        assert errors == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("parallel", [False, True])
    async def test_steps_run_in_dependency_order(self, parallel):
        ex = _executor()
        steps = [_step(3, [2]), _step(1), _step(2, [1])]
        await _run(ex, steps, parallel)
        dispatched = [c.args[2]["_n"] for c in ex.registry.execute.call_args_list]
        assert dispatched == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_a_failure_with_stop_halts_and_says_why(self):
        ex = _executor(results_by_step={1: CommandResult(success=False, error="boom")})
        steps = [_step(1, on_failure="stop"), _step(2, [1])]
        completed, failed, skipped, _, errors = await _run(ex, steps)
        assert (completed, failed, skipped) == (0, 1, 1)
        assert any("boom" in e for e in errors)
        # the step that never ran explains itself
        assert any("halted after step 1" in e for e in errors)
        assert steps[1].status is StepStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_on_failure_continue_lets_dependents_run_anyway(self):
        ex = _executor(results_by_step={1: CommandResult(success=False, error="boom")})
        steps = [_step(1, on_failure="continue"), _step(2, [1], on_failure="continue")]
        completed, failed, _, _, _ = await _run(ex, steps)
        assert completed == 1 and failed == 1

    @pytest.mark.asyncio
    async def test_on_failure_skip_does_not_halt_the_rest(self):
        ex = _executor(results_by_step={1: CommandResult(success=False, error="boom")})
        steps = [_step(1, on_failure="skip"), _step(2)]
        completed, failed, skipped, _, _ = await _run(ex, steps)
        assert completed == 1 and failed == 0 and skipped == 1

    @pytest.mark.asyncio
    async def test_abort_marks_the_remainder_and_names_the_reason(self):
        ex = _executor()
        steps = [_step(1), _step(2, [1]), _step(3, [2])]
        ex.request_abort()
        completed, _, skipped, _, errors = await _run(ex, steps)
        assert completed == 0 and skipped == 3
        assert all("aborted" in e for e in errors)

    @pytest.mark.asyncio
    async def test_independent_steps_are_dispatched_together_in_parallel_mode(self):
        ex = _executor()
        steps = [_step(1), _step(2), _step(3, [1, 2])]
        completed, _, _, _, _ = await _run(ex, steps, parallel=True)
        assert completed == 3

    @pytest.mark.asyncio
    async def test_context_plate_id_propagates_in_both_modes(self):
        """Plate tracking used to work only in sequential mode, so the same
        workflow recorded plates or not depending on a config flag.

        And only for instruments named "opentrons" and "tecan": the roles are
        now declared by the servers, so these deliberately have other names."""
        from src.catalog import parse_catalog
        servers = {
            "Pipettor": SimpleNamespace(catalog=parse_catalog({"traceability": {
                "Dispense": {"role": "prepares_plate", "recipe_parameter": "RecipeName"}}})),
            "Reader": SimpleNamespace(catalog=parse_catalog({"traceability": {
                "Read": {"role": "measures_plate", "plate_parameter": "PlateID"}}})),
        }
        for parallel in (False, True):
            ex = _executor()
            ex.registry.get_server_by_name = lambda name: servers.get(name)
            sent = {}

            async def _execute(instrument, action, parameters, timeout=None):
                sent[action] = dict(parameters)
                return CommandResult(success=True, data={})
            ex.registry.execute = AsyncMock(side_effect=_execute)
            steps = [
                _step(1, instrument="Pipettor", action="Dispense",
                      parameters={"RecipeName": "r.json"}),
                _step(2, [1], instrument="Reader", action="Read"),
            ]
            import datetime
            wf = Workflow(name="W", steps=steps)
            wf.start_time = datetime.datetime.now()
            ctx = WorkflowContext()
            await ex._execute_waves(wf, ctx, parallel=parallel)
            assert ctx.plate_id, f"no plate id generated (parallel={parallel})"
            assert sent["Read"]["PlateID"] == ctx.plate_id, "the reader was told which plate"

    @pytest.mark.asyncio
    async def test_no_declared_role_means_no_plate_id(self):
        ex = _executor()
        ex.registry.get_server_by_name = lambda name: None
        steps = [_step(1, instrument="opentrons", action="ExecuteRecipe",
                       parameters={"RecipeName": "r.json"})]
        import datetime
        wf = Workflow(name="W", steps=steps)
        wf.start_time = datetime.datetime.now()
        ctx = WorkflowContext()
        await ex._execute_waves(wf, ctx, parallel=False)
        assert ctx.plate_id is None, "a name alone no longer makes a step a pipetting run"


class TestParallelModeRespectsInstruments:
    """Independent steps run together, but never two at once on one instrument."""

    @staticmethod
    def _executor_tracking_overlap():
        ex = _executor()
        servers = {"A": SimpleNamespace(name="A"), "B": SimpleNamespace(name="B")}
        ex.registry.get_server_by_name = lambda name: servers.get(name)
        running = {"A": 0, "B": 0}
        peak = {"A": 0, "B": 0, "total": 0}

        async def _execute(instrument, action, parameters, timeout=None):
            import asyncio
            running[instrument] += 1
            peak[instrument] = max(peak[instrument], running[instrument])
            peak["total"] = max(peak["total"], sum(running.values()))
            await asyncio.sleep(0.05)
            running[instrument] -= 1
            return CommandResult(success=True, data={})

        ex.registry.execute = AsyncMock(side_effect=_execute)
        return ex, peak

    @pytest.mark.asyncio
    async def test_two_instruments_overlap_one_instrument_does_not(self):
        ex, peak = self._executor_tracking_overlap()
        steps = [_step(1, instrument="A"), _step(2, instrument="A"), _step(3, instrument="B")]
        completed, failed, _, _, _ = await _run(ex, steps, parallel=True)
        assert (completed, failed) == (3, 0)
        assert peak["A"] == 1, "a second command was sent to A while it was busy"
        assert peak["total"] == 2, "A and B should have worked at the same time"

    @pytest.mark.asyncio
    async def test_sequential_mode_never_overlaps(self):
        ex, peak = self._executor_tracking_overlap()
        steps = [_step(1, instrument="A"), _step(2, instrument="B")]
        await _run(ex, steps, parallel=False)
        assert peak["total"] == 1
