"""A resource held *across* steps, and the deadlock that makes it delicate.

The case: a plate sits in the reader between `PlateIn` and `PlateOut`. The tray
is occupied for the duration of three steps and no single step holds it, so
step-scoped claims do not cover it — the robot could place a second plate on the
tray between the read and the eject and nothing would stop it.

This is why holds were a later phase than claims. A step-scoped claim set cannot
deadlock, because a step never holds one resource while waiting for another. A
held resource breaks that property, so the two ways it can go wrong are rejected
at validation, before anything physical moves:

* nothing ever runs the releasing command, so the tray stays held for the rest
  of the run;
* a step needs the tray but is not ordered after whatever releases it, so it
  waits for something nothing will free for it.

Both are decidable from the workflow alone, which is the only reason holds are
safe to have at all.
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
from src.resources import ResourceLedger  # noqa: E402
from src.workflow import (  # noqa: E402
    PnPWorkflowExecutor,
    Workflow,
    WorkflowContext,
    WorkflowStep,
)

_ISOLATED = Path(__file__).resolve().parent / "_no_such_config_dir"

TECAN = {"resources": {
    "provides": ["instrument:tecan", "tray:tecan"],
    "commands": {
        # PlateIn keeps the tray after it finishes: the plate is in there now.
        "PlateIn": {"claims": ["instrument:tecan", "tray:tecan"],
                    "holds": {"tray:tecan": "PlateOut"}},
        "RunMeasurement": {"claims": ["instrument:tecan"]},
        "PlateOut": {"claims": ["instrument:tecan", "tray:tecan"]},
    },
}}
MOBILE = {"resources": {
    "provides": ["instrument:mobile"],
    "commands": {"ExecuteTask": {
        "claims": ["instrument:mobile"],
        "by_parameter": {"TaskName": {"place_on_TC": ["tray:tecan"]}}}},
}}


def _step(number, instrument, action, parameters=None, depends_on=None):
    return WorkflowStep(
        step_number=number, instrument=instrument, action=action,
        parameters=dict(parameters or {}), depends_on=list(depends_on or []),
        retry_count=0)


def _executor(catalogs=None, hold=0.02):
    servers = {
        name: SimpleNamespace(name=name, catalog=parse_catalog(raw))
        for name, raw in (catalogs or {"tecan": TECAN, "mobile": MOBILE}).items()
    }
    registry = MagicMock()
    registry.servers = {}
    registry.list_servers = lambda: list(servers.values())
    registry.get_server_by_name = lambda name: servers.get(name)

    live, witnessed = [], []

    async def _execute(instrument, action, parameters, timeout=None):
        live.append(f"{instrument}.{action}")
        witnessed.append(tuple(sorted(live)))
        await asyncio.sleep(hold)
        live.remove(f"{instrument}.{action}")
        return CommandResult(success=True, data={})

    registry.execute = AsyncMock(side_effect=_execute)
    executor = PnPWorkflowExecutor(registry, base_dir=_ISOLATED)
    executor._error_config = executor._error_config.__class__(
        retry_strategy="none", base_delay_seconds=0.0, max_delay_seconds=0.0,
        max_retries=0, enable_human_intervention=False,
    )
    return executor, witnessed


async def _run(executor, steps):
    workflow = Workflow(name="W", steps=steps)
    workflow.start_time = datetime.datetime.now()
    return await executor._execute_waves(workflow, WorkflowContext())


# The real sequence: in, read, out, with the robot placing a plate afterwards.
def _reader_sequence():
    return [
        _step(1, "tecan", "PlateIn"),
        _step(2, "tecan", "RunMeasurement", depends_on=[1]),
        _step(3, "tecan", "PlateOut", depends_on=[2]),
        _step(4, "mobile", "ExecuteTask", {"TaskName": "place_on_TC"}, depends_on=[3]),
    ]


class TestTheTrayStaysOccupied:

    @pytest.mark.asyncio
    async def test_the_tray_is_still_held_after_platein_finishes(self):
        executor, _ = _executor()
        seen = {}

        async def _watch(instrument, action, parameters, timeout=None):
            if action == "RunMeasurement":
                seen.update(executor._ledger.held)
            return CommandResult(success=True, data={})
        executor.registry.execute = AsyncMock(side_effect=_watch)

        await _run(executor, _reader_sequence())
        assert seen.get("tray:tecan", "").startswith("hold-1"), (
            "the tray was free while a plate was sitting in the reader"
        )

    @pytest.mark.asyncio
    async def test_the_whole_sequence_completes(self):
        executor, _ = _executor()
        completed, failed, _, _, errors = await _run(executor, _reader_sequence())
        assert (completed, failed) == (4, 0)
        assert errors == []

    @pytest.mark.asyncio
    async def test_plateout_does_not_wait_for_itself(self):
        # PlateOut needs the tray that PlateIn is holding, and it is the command
        # that releases it. Without releasing first it would block forever.
        executor, _ = _executor()
        done = await asyncio.wait_for(_run(executor, _reader_sequence()), timeout=5)
        assert done[0] == 4

    @pytest.mark.asyncio
    async def test_nothing_is_left_held_after_a_clean_run(self):
        executor, _ = _executor()
        await _run(executor, _reader_sequence())
        assert executor._ledger is None


class TestValidationRejectsADeadlock:

    @pytest.mark.asyncio
    async def test_a_hold_nothing_releases_is_refused(self):
        executor, _ = _executor()
        workflow = Workflow(name="W", steps=[
            _step(1, "tecan", "PlateIn"),
            _step(2, "tecan", "RunMeasurement", depends_on=[1]),
        ])
        errors = await executor.validate_workflow(workflow)
        holds = [e for e in errors if e.field == "holds"]
        assert holds, "a tray held for the rest of the run must be refused"
        assert "PlateOut" in holds[0].message

    @pytest.mark.asyncio
    async def test_a_releaser_that_does_not_follow_the_holder_is_refused(self):
        # A PlateOut that is not ordered after the PlateIn does not release it.
        executor, _ = _executor()
        workflow = Workflow(name="W", steps=[
            _step(1, "tecan", "PlateOut"),
            _step(2, "tecan", "PlateIn", depends_on=[1]),
        ])
        errors = await executor.validate_workflow(workflow)
        assert [e for e in errors if e.field == "holds"]

    @pytest.mark.asyncio
    async def test_a_step_needing_the_tray_out_of_order_is_refused(self):
        # The robot places a plate on the tray without depending on the
        # PlateOut that empties it: at runtime it would wait forever.
        executor, _ = _executor()
        workflow = Workflow(name="W", steps=[
            _step(1, "tecan", "PlateIn"),
            _step(2, "tecan", "PlateOut", depends_on=[1]),
            _step(3, "mobile", "ExecuteTask", {"TaskName": "place_on_TC"},
                  depends_on=[1]),   # should depend on 2
        ])
        errors = await executor.validate_workflow(workflow)
        holds = [e for e in errors if e.field == "holds"]
        assert holds, "a step waiting on a held resource forever must be refused"
        assert any("DependsOn" in e.message for e in holds)

    @pytest.mark.asyncio
    async def test_the_correct_ordering_validates(self):
        executor, _ = _executor()
        workflow = Workflow(name="W", steps=_reader_sequence())
        errors = await executor.validate_workflow(workflow)
        assert [e for e in errors if e.field == "holds"] == []

    @pytest.mark.asyncio
    async def test_nothing_runs_when_a_hold_is_unreleasable(self):
        executor, witnessed = _executor()
        workflow = Workflow(name="W", steps=[_step(1, "tecan", "PlateIn")])
        result = await executor.execute(workflow, validate=True)
        assert result.success is False
        assert witnessed == [], "an instrument was driven despite validation failing"


class TestTheLedgerTransfer:

    @pytest.mark.asyncio
    async def test_a_transfer_never_lets_the_resource_go(self):
        # Release-then-acquire would open a window where a waiting step takes
        # the tray with a plate still in the reader.
        ledger = ResourceLedger()
        await ledger.acquire("step-1", {"instrument:tecan", "tray:tecan"})
        waiter = asyncio.create_task(ledger.acquire("intruder", {"tray:tecan"}))
        await ledger.transfer("step-1", "hold-1", {"tray:tecan"})
        await ledger.release("step-1")
        await asyncio.sleep(0.05)
        assert not waiter.done(), "the tray became free during the hand-over"
        assert ledger.holder_of("tray:tecan") == "hold-1"
        assert ledger.holder_of("instrument:tecan") is None, (
            "the step's other claims should have been released with it"
        )
        await ledger.release("hold-1")
        await asyncio.wait_for(waiter, timeout=1)

    @pytest.mark.asyncio
    async def test_transferring_something_not_held_takes_it_outright(self):
        ledger = ResourceLedger()
        await ledger.transfer("nobody", "hold-9", {"tray:tecan"})
        assert ledger.holder_of("tray:tecan") == "hold-9"

    @pytest.mark.asyncio
    async def test_an_empty_transfer_changes_nothing(self):
        ledger = ResourceLedger()
        await ledger.acquire("step-1", {"tray:tecan"})
        await ledger.transfer("step-1", "hold-1", set())
        assert ledger.holder_of("tray:tecan") == "step-1"


class TestDeclaringHolds:

    def test_a_command_cannot_release_itself(self):
        from src.resources import parse_declaration
        declaration = parse_declaration({
            "provides": ["tray:t"],
            "commands": {"PlateIn": {"holds": {"tray:t": "PlateIn"}}},
        })
        assert declaration.commands["PlateIn"].holds == {}
        assert any("released by itself" in p for p in declaration.problems)

    def test_control_and_holds_together_are_contradictory(self):
        from src.resources import parse_declaration
        declaration = parse_declaration({
            "commands": {"Stop": {"control": True, "holds": {"tray:t": "Go"}}},
        })
        assert declaration.commands["Stop"].holds == {}
        assert any("occupies nothing" in p for p in declaration.problems)

    def test_a_held_resource_counts_as_claimed_for_the_cross_check(self):
        # Otherwise a hold on a resource nobody provides would slip past
        # discovery, which is the silent-no-exclusion failure again.
        from src.resources import build_topology, parse_declaration
        topology = build_topology({"tecan": parse_declaration({
            "provides": ["instrument:tecan"],
            "commands": {"PlateIn": {"holds": {"tray:ghost": "PlateOut"}}},
        })})
        assert [u.resource for u in topology.unresolved] == ["tray:ghost"]


class TestAStepBeforeTheHoldIsNotAtRisk:
    """Only a step that could still be waiting once the hold exists can deadlock.

    The first version of this validation asked only "does it depend on the
    releaser?", which rejected any earlier use of the same resource — including
    the workflow this lab actually runs, where the reader tray is used, emptied
    and used again. A step the holder itself depends on runs strictly earlier
    and is never at risk.
    """

    @pytest.mark.asyncio
    async def test_the_tray_may_be_used_before_the_hold_starts(self):
        executor, _ = _executor()
        workflow = Workflow(name="W", steps=[
            _step(1, "tecan", "PlateOut"),                      # uses the tray
            _step(2, "mobile", "ExecuteTask",
                  {"TaskName": "place_on_TC"}, depends_on=[1]), # uses the tray
            _step(3, "tecan", "PlateIn", depends_on=[2]),       # now holds it
            _step(4, "tecan", "PlateOut", depends_on=[3]),      # releases it
        ])
        errors = await executor.validate_workflow(workflow)
        assert [e for e in errors if e.field == "holds"] == [], (
            "using the tray before the hold began was reported as a deadlock"
        )

    @pytest.mark.asyncio
    async def test_the_same_tray_can_be_used_twice_in_a_row(self):
        executor, _ = _executor()
        workflow = Workflow(name="W", steps=[
            _step(1, "tecan", "PlateIn"),
            _step(2, "tecan", "PlateOut", depends_on=[1]),
            _step(3, "tecan", "PlateIn", depends_on=[2]),
            _step(4, "tecan", "PlateOut", depends_on=[3]),
        ])
        errors = await executor.validate_workflow(workflow)
        assert [e for e in errors if e.field == "holds"] == []

    @pytest.mark.asyncio
    async def test_a_concurrent_step_is_still_caught(self):
        # Narrowing the rule must not have let the real deadlock through: this
        # step neither precedes the holder nor follows the releaser.
        executor, _ = _executor()
        workflow = Workflow(name="W", steps=[
            _step(1, "tecan", "PlateIn"),
            _step(2, "tecan", "PlateOut", depends_on=[1]),
            _step(3, "mobile", "ExecuteTask", {"TaskName": "place_on_TC"},
                  depends_on=[1]),
        ])
        errors = await executor.validate_workflow(workflow)
        assert [e for e in errors if e.field == "holds"]
