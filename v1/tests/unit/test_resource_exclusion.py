"""Resource exclusion through the real wave runner.

`tests/unit/test_resources.py` covers the model in isolation. This file runs
actual workflows through `PnPWorkflowExecutor._execute_waves` and watches what
overlaps, because that is where the hazard lives.

The case that matters: a step **on the mobile robot** (`place_on_OT`) occupying
**the Opentrons' deck**. Both steps are in wave 0 with no dependency between
them, both are perfectly valid, and the two instruments are different — so the
per-instrument lanes this replaces could not have seen the conflict, and a
recipe would have started with an arm inside the deck.
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

OPENTRONS_CATALOG = {
    "resources": {
        "provides": ["instrument:opentrons", "deck:opentrons"],
        "commands": {
            "ExecuteRecipe": {"claims": ["instrument:opentrons", "deck:opentrons"]},
            "CaptureImage": {"claims": ["instrument:opentrons"]},
        },
    }
}
MOBILE_CATALOG = {
    "resources": {
        "provides": ["instrument:mobile"],
        "commands": {
            "ExecuteTask": {
                "claims": ["instrument:mobile"],
                "by_parameter": {"TaskName": {
                    "place_on_OT": ["deck:opentrons"],
                    "go_to_Opentrons": [],
                }},
            }
        },
    }
}
TECAN_CATALOG = {
    "resources": {
        "provides": ["instrument:tecan", "tray:tecan"],
        "commands": {"RunMeasurement": {"claims": ["instrument:tecan", "tray:tecan"]}},
    }
}


def _step(number, instrument, action, parameters=None, depends_on=None):
    return WorkflowStep(
        step_number=number, instrument=instrument, action=action,
        parameters=dict(parameters or {}), depends_on=list(depends_on or []),
        retry_count=0,
    )


#: These tests build their own catalogs, so the executor must not also pick up
#: the repository's real `lab_config.yaml` — which does declare `mobile`, and
#: would quietly make a test about undeclared instruments pass for the wrong
#: reason. The shipped declarations are verified in test_shipped_resources.py.
_ISOLATED = Path(__file__).resolve().parent / "_no_such_config_dir"


def _executor(catalogs, hold=0.05):
    """An executor whose registry serves the given catalogs, recording overlap."""
    servers = {
        name: SimpleNamespace(name=name, catalog=parse_catalog(catalog))
        for name, catalog in catalogs.items()
    }
    registry = MagicMock()
    registry.servers = {}
    registry.list_servers = lambda: list(servers.values())
    registry.get_server_by_name = lambda name: servers.get(name)

    live = []
    witnessed = []

    async def _execute(instrument, action, parameters, timeout=None):
        live.append(instrument)
        witnessed.append(tuple(sorted(live)))
        await asyncio.sleep(hold)
        live.remove(instrument)
        return CommandResult(success=True, data={})

    registry.execute = AsyncMock(side_effect=_execute)
    executor = PnPWorkflowExecutor(registry, base_dir=_ISOLATED)
    executor._error_config = executor._error_config.__class__(
        retry_strategy="none", base_delay_seconds=0.0, max_delay_seconds=0.0,
        max_retries=0, enable_human_intervention=False,
    )
    return executor, witnessed


async def _run(executor, steps, **kwargs):
    workflow = Workflow(name="W", steps=steps)
    workflow.start_time = datetime.datetime.now()
    return await executor._execute_waves(workflow, WorkflowContext(), **kwargs)


def _ever_together(witnessed, *instruments):
    wanted = set(instruments)
    return any(wanted <= set(moment) for moment in witnessed)


# ── The hazard this exists for ────────────────────────────────────────────────

class TestTheArmAndTheDeck:

    @pytest.mark.asyncio
    async def test_a_recipe_does_not_start_while_the_arm_is_in_the_deck(self):
        executor, witnessed = _executor({
            "opentrons": OPENTRONS_CATALOG, "mobile": MOBILE_CATALOG})
        steps = [
            _step(1, "mobile", "ExecuteTask", {"TaskName": "place_on_OT"}),
            _step(2, "opentrons", "ExecuteRecipe", {"RecipeName": "r.json"}),
        ]
        completed, failed, _, _, _ = await _run(executor, steps)
        assert (completed, failed) == (2, 0)
        assert not _ever_together(witnessed, "mobile", "opentrons"), (
            "the recipe ran while the arm was inside the deck"
        )

    @pytest.mark.asyncio
    async def test_the_robot_elsewhere_runs_alongside_the_recipe(self):
        # The same two instruments, a task that occupies nothing of theirs.
        # Exclusion has to be this precise or it is just a global lock.
        executor, witnessed = _executor({
            "opentrons": OPENTRONS_CATALOG, "mobile": MOBILE_CATALOG})
        steps = [
            _step(1, "mobile", "ExecuteTask", {"TaskName": "go_to_Opentrons"}),
            _step(2, "opentrons", "ExecuteRecipe", {"RecipeName": "r.json"}),
        ]
        await _run(executor, steps)
        assert _ever_together(witnessed, "mobile", "opentrons"), (
            "driving to a station needlessly blocked the deck"
        )

    @pytest.mark.asyncio
    async def test_unrelated_instruments_still_overlap(self):
        executor, witnessed = _executor({
            "opentrons": OPENTRONS_CATALOG, "tecan": TECAN_CATALOG})
        steps = [
            _step(1, "opentrons", "CaptureImage"),
            _step(2, "tecan", "RunMeasurement"),
        ]
        await _run(executor, steps)
        assert _ever_together(witnessed, "opentrons", "tecan")


# ── What the per-instrument lanes already did, still done ─────────────────────

class TestOneInstrumentOneStep:

    @pytest.mark.asyncio
    async def test_two_steps_on_one_instrument_never_overlap(self):
        executor, witnessed = _executor({"opentrons": OPENTRONS_CATALOG})
        steps = [
            _step(1, "opentrons", "CaptureImage"),
            _step(2, "opentrons", "CaptureImage"),
        ]
        completed, _, _, _, _ = await _run(executor, steps)
        assert completed == 2
        assert all(len(moment) == 1 for moment in witnessed)


# ── Adding the model must not have introduced the hazard ──────────────────────

class TestUndeclaredServersSerialise:

    @pytest.mark.asyncio
    async def test_instruments_that_declare_nothing_run_one_at_a_time(self):
        # Today's shipped state: no catalog carries a resources section. If
        # undeclared steps were mutually disjoint, all 19 steps of Test_Bicocca
        # would become eligible to dispatch together.
        executor, witnessed = _executor({
            "opentrons": {}, "tecan": {}, "mobile": {}})
        steps = [
            _step(1, "mobile", "ExecuteTask", {"TaskName": "place_on_OT"}),
            _step(2, "opentrons", "ExecuteRecipe", {"RecipeName": "r.json"}),
            _step(3, "tecan", "RunMeasurement"),
        ]
        completed, failed, _, _, _ = await _run(executor, steps)
        assert (completed, failed) == (3, 0)
        assert all(len(moment) == 1 for moment in witnessed), (
            "undeclared instruments were allowed to overlap"
        )

    @pytest.mark.asyncio
    async def test_a_declared_instrument_is_not_held_back_by_an_undeclared_one(self):
        # An undeclared instrument serialises against other undeclared ones,
        # not against the whole lab — or declaring would never pay off.
        executor, witnessed = _executor({
            "opentrons": OPENTRONS_CATALOG, "tecan": TECAN_CATALOG, "mobile": {}})
        steps = [
            _step(1, "mobile", "ExecuteTask", {"TaskName": "whatever"}),
            _step(2, "tecan", "RunMeasurement"),
        ]
        await _run(executor, steps)
        assert _ever_together(witnessed, "mobile", "tecan")


# ── The cap, and what the retired flag now means ──────────────────────────────

class TestConcurrencyCap:

    @pytest.mark.asyncio
    async def test_max_concurrency_one_serialises_everything(self):
        executor, witnessed = _executor({
            "opentrons": OPENTRONS_CATALOG, "tecan": TECAN_CATALOG})
        steps = [
            _step(1, "opentrons", "CaptureImage"),
            _step(2, "tecan", "RunMeasurement"),
        ]
        await _run(executor, steps, max_concurrency=1)
        assert all(len(moment) == 1 for moment in witnessed)

    @pytest.mark.asyncio
    async def test_the_retired_parallel_false_still_means_one_at_a_time(self):
        # A deployment carrying `parallel_execution: false` meant exactly this,
        # and this is a hardware system: that intent is honoured, not ignored.
        executor, witnessed = _executor({
            "opentrons": OPENTRONS_CATALOG, "tecan": TECAN_CATALOG})
        steps = [
            _step(1, "opentrons", "CaptureImage"),
            _step(2, "tecan", "RunMeasurement"),
        ]
        await _run(executor, steps, parallel=False)
        assert all(len(moment) == 1 for moment in witnessed)


# ── Dependencies still decide order; claims only decide overlap ───────────────

class TestClaimsDoNotReplaceDependencies:

    @pytest.mark.asyncio
    async def test_a_dependency_is_still_what_orders_two_contending_steps(self):
        executor, _ = _executor({
            "opentrons": OPENTRONS_CATALOG, "mobile": MOBILE_CATALOG})
        order = []

        async def _execute(instrument, action, parameters, timeout=None):
            order.append(instrument)
            return CommandResult(success=True, data={})
        executor.registry.execute = AsyncMock(side_effect=_execute)

        steps = [
            _step(1, "opentrons", "ExecuteRecipe", {"RecipeName": "r.json"}),
            _step(2, "mobile", "ExecuteTask", {"TaskName": "place_on_OT"},
                  depends_on=[1]),
        ]
        await _run(executor, steps)
        assert order == ["opentrons", "mobile"], (
            "exclusion must not reorder what a dependency pinned"
        )

    @pytest.mark.asyncio
    async def test_without_a_dependency_the_order_within_a_wave_is_as_written(self):
        # Claims make contending steps take turns; they do not say whose turn
        # comes first. Written order does, which is why Test_Bicocca needs real
        # dependencies rather than relying on this.
        executor, _ = _executor({
            "opentrons": OPENTRONS_CATALOG, "mobile": MOBILE_CATALOG})
        order = []

        async def _execute(instrument, action, parameters, timeout=None):
            order.append(instrument)
            await asyncio.sleep(0.01)
            return CommandResult(success=True, data={})
        executor.registry.execute = AsyncMock(side_effect=_execute)

        steps = [
            _step(1, "mobile", "ExecuteTask", {"TaskName": "place_on_OT"}),
            _step(2, "opentrons", "ExecuteRecipe", {"RecipeName": "r.json"}),
        ]
        await _run(executor, steps)
        assert order == ["mobile", "opentrons"]


# ── Fail closed: an unresolved claim stops the workflow before anything moves ──

class TestAnUnresolvedClaimFailsValidation:

    TYPO = {
        "resources": {
            "provides": ["instrument:mobile"],
            # deck:OT against the Opentrons' deck:opentrons. With claims alone
            # and no owner per resource this produced no error and no
            # exclusion — silently. That is the failure this guards.
            "commands": {"ExecuteTask": {"claims": ["instrument:mobile", "deck:OT"]}},
        }
    }

    @pytest.mark.asyncio
    async def test_a_claim_nobody_provides_is_an_error_not_a_free_pass(self):
        executor, _ = _executor({"opentrons": OPENTRONS_CATALOG, "mobile": self.TYPO})
        workflow = Workflow(name="W", steps=[
            _step(1, "mobile", "ExecuteTask", {"TaskName": "place_on_OT"}),
        ])
        errors = await executor.validate_workflow(workflow)
        resource_errors = [e for e in errors if e.field == "resources"]
        assert resource_errors, "an unresolved claim must fail validation"
        assert "deck:OT" in resource_errors[0].message
        assert "lab_config.yaml" in resource_errors[0].message, (
            "the error should say how to fix it"
        )

    @pytest.mark.asyncio
    async def test_nothing_is_executed_when_validation_rejects_it(self):
        executor, witnessed = _executor({"opentrons": OPENTRONS_CATALOG, "mobile": self.TYPO})
        workflow = Workflow(name="W", steps=[
            _step(1, "mobile", "ExecuteTask", {"TaskName": "place_on_OT"}),
        ])
        result = await executor.execute(workflow, validate=True)
        assert result.success is False
        assert witnessed == [], "an instrument was driven despite validation failing"

    @pytest.mark.asyncio
    async def test_an_unresolved_claim_elsewhere_does_not_block_this_workflow(self):
        # The broken declaration belongs to an instrument this workflow never
        # touches. Refusing to run would make one misconfigured server stop the
        # whole lab; the Settings panel is where that gets noticed.
        executor, _ = _executor({"opentrons": OPENTRONS_CATALOG, "mobile": self.TYPO,
                                 "tecan": TECAN_CATALOG})
        workflow = Workflow(name="W", steps=[_step(1, "tecan", "RunMeasurement")])
        errors = await executor.validate_workflow(workflow)
        assert [e for e in errors if e.field == "resources"] == []
