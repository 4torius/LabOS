"""`NotBeforeSeconds` and `DeadlineSeconds`: an incubation, and a late reading.

The motivating case: an incubation of 30 minutes has to be read at 30 minutes,
not at 45 because the reader was busy. Expressing that constraint and checking
it are the executor's job; getting the reading to happen on time would be a
scheduler's, and there isn't one — see docs/RESOURCE_MODEL.md on why that
division is deliberate.

So the contract these tests pin is narrow and worth being precise about:

* `NotBeforeSeconds` is a guarantee. The step waits.
* `DeadlineSeconds` is *not* a guarantee and must not pretend to be. The step
  still runs — the measurement happened and the data is real — but the overrun
  is recorded against the step and reported, because a plate read at 45 minutes
  when the protocol said 30 is a result with a caveat rather than a result.
"""
import asyncio
import datetime
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
if str(V1_DIR) not in sys.path:
    sys.path.insert(0, str(V1_DIR))

from src.catalog import parse_catalog  # noqa: E402
from src.client import CommandResult  # noqa: E402
from src.workflow import (  # noqa: E402
    PnPWorkflowExecutor,
    Workflow,
    WorkflowContext,
    WorkflowStep,
)

_ISOLATED = Path(__file__).resolve().parent / "_no_such_config_dir"

DECLARED = {"resources": {
    "provides": ["instrument:A", "instrument:B"],
    "commands": {"Run": {"claims": ["instrument:A"]}},
}}


def _step(number, instrument="A", depends_on=None, **timing):
    return WorkflowStep(
        step_number=number, instrument=instrument, action="Run",
        parameters={}, depends_on=list(depends_on or []), retry_count=0, **timing)


def _executor():
    servers = {
        name: SimpleNamespace(name=name, catalog=parse_catalog({"resources": {
            "provides": [f"instrument:{name}"],
            "commands": {"Run": {"claims": [f"instrument:{name}"]}},
        }}))
        for name in ("A", "B")
    }
    registry = MagicMock()
    registry.servers = {}
    registry.list_servers = lambda: list(servers.values())
    registry.get_server_by_name = lambda name: servers.get(name)
    registry.execute = AsyncMock(side_effect=lambda *a, **k: CommandResult(success=True, data={}))
    executor = PnPWorkflowExecutor(registry, base_dir=_ISOLATED)
    executor._error_config = executor._error_config.__class__(
        retry_strategy="none", base_delay_seconds=0.0, max_delay_seconds=0.0,
        max_retries=0, enable_human_intervention=False,
    )
    return executor


async def _run(executor, steps):
    workflow = Workflow(name="W", steps=steps)
    workflow.start_time = datetime.datetime.now()
    return await executor._execute_waves(workflow, WorkflowContext())


class TestNotBefore:

    @pytest.mark.asyncio
    async def test_a_step_waits_out_its_window(self):
        executor = _executor()
        started = asyncio.get_running_loop().time()
        await _run(executor, [_step(1), _step(2, depends_on=[1], not_before_seconds=0.6)])
        assert asyncio.get_running_loop().time() - started >= 0.6

    @pytest.mark.asyncio
    async def test_the_window_is_measured_from_the_dependency_finishing(self):
        # Not from the start of the run: an incubation starts when the step
        # that set it up completed.
        executor = _executor()

        async def _slow(*a, **k):
            await asyncio.sleep(0.3)
            return CommandResult(success=True, data={})
        executor.registry.execute = AsyncMock(side_effect=_slow)

        started = asyncio.get_running_loop().time()
        await _run(executor, [_step(1), _step(2, depends_on=[1], not_before_seconds=0.4)])
        # 0.3 for step 1, then 0.4 from when it finished, then 0.3 for step 2.
        assert asyncio.get_running_loop().time() - started >= 0.95

    @pytest.mark.asyncio
    async def test_a_step_with_no_window_is_not_delayed(self):
        executor = _executor()
        started = asyncio.get_running_loop().time()
        await _run(executor, [_step(1), _step(2, depends_on=[1])])
        assert asyncio.get_running_loop().time() - started < 0.3

    @pytest.mark.asyncio
    async def test_waiting_holds_no_resource(self):
        # A run waiting out an incubation must not sit on the instrument it is
        # about to use, or the wait blocks everything else for its duration.
        executor = _executor()
        held_during_wait = {}

        async def _watch(instrument, action, parameters, timeout=None):
            # Sample only while the step on the *other* instrument is running,
            # which is the moment step 2 is still waiting out its window.
            # Accumulating over every step would also catch step 2 once it
            # finally starts, which proves nothing.
            if instrument == "B":
                ledger = executor._ledger
                held_during_wait.update(ledger.held if ledger else {})
            return CommandResult(success=True, data={})
        executor.registry.execute = AsyncMock(side_effect=_watch)

        # Step 2 waits; step 3 on another instrument runs meanwhile.
        await _run(executor, [
            _step(1),
            _step(2, depends_on=[1], not_before_seconds=0.5),
            _step(3, instrument="B", depends_on=[1]),
        ])
        assert "instrument:A" not in held_during_wait, (
            "a step waiting out its window was holding the instrument it had "
            "not started using"
        )

    @pytest.mark.asyncio
    async def test_an_abort_during_a_long_wait_does_not_wait_it_out(self):
        executor = _executor()
        run = asyncio.create_task(_run(
            executor, [_step(1), _step(2, depends_on=[1], not_before_seconds=30)]))
        await asyncio.sleep(0.3)
        executor.request_abort()
        completed, failed, skipped, _, _ = await asyncio.wait_for(run, timeout=5)
        assert skipped >= 1


class TestDeadline:

    @pytest.mark.asyncio
    async def test_being_late_does_not_fail_the_step(self):
        executor = _executor()

        async def _slow(instrument, action, parameters, timeout=None):
            await asyncio.sleep(0.4)
            return CommandResult(success=True, data={})
        executor.registry.execute = AsyncMock(side_effect=_slow)

        completed, failed, _, _, _ = await _run(executor, [
            _step(1, instrument="B"),
            _step(2, depends_on=[1]),
            _step(3, depends_on=[1], deadline_seconds=0.1),
        ])
        assert (completed, failed) == (3, 0), "a late reading is still a reading"

    @pytest.mark.asyncio
    async def test_the_overrun_is_recorded_against_the_step(self):
        # The motivating scenario, reproduced: the instrument is busy, so the
        # step that should have run at its deadline waits for the claim and
        # starts late. Nothing else in this system would delay it.
        executor = _executor()

        async def _slow(instrument, action, parameters, timeout=None):
            await asyncio.sleep(0.4)
            return CommandResult(success=True, data={})
        executor.registry.execute = AsyncMock(side_effect=_slow)

        _, _, _, results, _ = await _run(executor, [
            _step(1, instrument="B"),
            _step(2, depends_on=[1]),                              # occupies A for 0.4s
            _step(3, depends_on=[1], deadline_seconds=0.1),        # wants A, waits
        ])
        assert "deadline_exceeded_by_seconds" in results[3].data
        assert results[3].data["deadline_exceeded_by_seconds"] > 0
        assert "deadline_exceeded_by_seconds" not in results[2].data, (
            "the step that was on time must not be marked"
        )

    @pytest.mark.asyncio
    async def test_the_overrun_is_reported_not_only_recorded(self):
        executor = _executor()

        async def _slow(instrument, action, parameters, timeout=None):
            await asyncio.sleep(0.4)
            return CommandResult(success=True, data={})
        executor.registry.execute = AsyncMock(side_effect=_slow)

        _, failed, _, _, errors = await _run(executor, [
            _step(1, instrument="B"),
            _step(2, depends_on=[1]),
            _step(3, depends_on=[1], deadline_seconds=0.1),
        ])
        assert failed == 0, "a late reading is still a reading"
        assert any("DeadlineSeconds" in e for e in errors), (
            "the overrun has to reach the run's report, not only the step record"
        )

    @pytest.mark.asyncio
    async def test_in_time_records_nothing(self):
        executor = _executor()
        _, _, _, results, errors = await _run(
            executor, [_step(1), _step(2, depends_on=[1], deadline_seconds=30)])
        assert "deadline_exceeded_by_seconds" not in (results[2].data or {})
        assert errors == []


class TestTheWorkflowFormat:

    def test_both_fields_survive_a_round_trip(self):
        source = {"WorkflowName": "W", "Steps": [
            {"StepNumber": 1, "Instrument": "A", "Action": "Run"},
            {"StepNumber": 2, "Instrument": "A", "Action": "Run", "DependsOn": [1],
             "NotBeforeSeconds": 1800, "DeadlineSeconds": 1920},
        ]}
        workflow = Workflow.from_dict(source)
        assert workflow.steps[1].not_before_seconds == 1800
        assert workflow.steps[1].deadline_seconds == 1920
        again = workflow.to_dict()["Steps"][1]
        assert (again["NotBeforeSeconds"], again["DeadlineSeconds"]) == (1800, 1920)

    def test_a_workflow_without_them_parses_unchanged(self):
        workflow = Workflow.from_dict({"WorkflowName": "W", "Steps": [
            {"StepNumber": 1, "Instrument": "A", "Action": "Run"}]})
        assert workflow.steps[0].not_before_seconds is None
        assert workflow.steps[0].deadline_seconds is None
