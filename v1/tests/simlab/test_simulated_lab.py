"""The whole orchestrator against the simulated lab.

Everything between a workflow file and the wire is real here: discovery over
SiLA2, the servers' own feature definitions and catalogs, validation, the
scheduler, the resource ledger. Only the instruments are missing - each is a
process that records when a command started and when it ended.

That record is the point. The unit tests ask the executor what it did; these
read it off the instruments. The bug that left the robot's claims out of the
executor passed every unit test, because the test doubles used one string for
an instrument's key and for the name it announces. Here the names are the real
ones.
"""
import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
if str(V1_DIR) not in sys.path:
    sys.path.insert(0, str(V1_DIR))

from sim_lab.lab import SimLab  # noqa: E402
from src.workflow import Workflow, halt_running_workflows  # noqa: E402

pytestmark = pytest.mark.simlab

BICOCCA = json.loads(
    (V1_DIR / "Library" / "Workflows" / "Test_Bicocca.workflow.json").read_text(encoding="utf-8-sig"))
STEP = {step["StepNumber"]: step for step in BICOCCA["Steps"]}

# The steps of the real workflow, by what they are.
GO_TO_OT, PICK_JIG, PLACE_OT, RECIPE, PICK_OT = 4, 5, 6, 7, 8
TRAY_OUT, PLACE_TC, TRAY_IN, READ, PICK_TC = 10, 12, 13, 14, 16


def workflow(*steps):
    """A workflow from (number, real step number, depends_on) triples."""
    out = []
    for number, source, depends_on in steps:
        step = copy.deepcopy(STEP[source])
        step["StepNumber"] = number
        step["DependsOn"] = list(depends_on)
        out.append(step)
    return {"WorkflowName": "simulated", "SchemaVersion": "1.0", "Steps": out}


@pytest.fixture(scope="module")
def lab():
    with SimLab() as running:
        yield running


@pytest.fixture
def fresh(lab):
    lab.reset()
    return lab


def run(lab, definition, **kwargs):
    return asyncio.run(lab.run_workflow(definition, **kwargs))


class TestTheLabItStandsInFor:

    def test_every_instrument_is_found_under_its_key_and_its_real_name(self, fresh):
        async def _instruments():
            core, _registry, _executor = await fresh.orchestrator()
            return {i.id: i.name for i in core.list_instruments()}
        assert asyncio.run(_instruments()) == {
            "tecan": "TecanM200Pro", "opentrons": "OpentronsFlex",
            "mobile": "GoFaGo Mobile Robot", "manual_station": "ManualStation"}

    def test_the_executor_sees_every_declaration(self, fresh):
        async def _topology():
            _core, _registry, executor = await fresh.orchestrator()
            return executor._topology()
        topology = asyncio.run(_topology())
        assert topology.problems == []
        assert topology.undeclared == ()
        assert topology.unresolved == []
        # The robot's tasks reach the executor by the id a workflow sends.
        assert "deck:opentrons" in topology.claims_for(
            "mobile", "ExecuteTask", STEP[PLACE_OT]["Parameters"])

    def test_nothing_real_can_be_reached(self, fresh):
        from src.settings import get_settings
        settings = get_settings(V1_DIR)
        assert settings.discovery.enabled is False
        assert {spec.host for spec in settings.servers.values() if spec.enabled} == {"127.0.0.1"}


class TestTheWorkflowThisLabRuns:

    def test_test_bicocca_runs_start_to_finish(self, fresh):
        result = run(fresh, "Test_Bicocca")
        assert result.success, result.errors
        assert (result.steps_completed, result.steps_failed, result.steps_skipped) == (19, 0, 0)
        assert result.errors == []
        assert len(fresh.commands()) == 19
        assert fresh.any_overlap() == []


class TestTheArmAndTheDeck:
    """The hazard the resource model exists for, measured at the instruments."""

    def test_a_recipe_never_runs_with_the_arm_in_the_deck(self, fresh):
        # No DependsOn between them at all: only the claims keep them apart.
        result = run(fresh, workflow((1, PLACE_OT, []), (2, RECIPE, []), (3, PICK_OT, [1])),
                     validate=False)
        assert result.steps_completed == 3, result.errors
        assert fresh.overlapping("deck:opentrons") == []
        assert fresh.any_overlap() == []

    def test_what_does_not_touch_does_overlap(self, fresh):
        # Otherwise "never overlapped" would be true of a scheduler that
        # simply ran everything one at a time.
        result = run(fresh, workflow((1, RECIPE, []), (2, READ, [])), validate=False)
        assert result.steps_completed == 2, result.errors
        together = {(str(a).split("(")[0], str(b).split("(")[0]) for a, b in fresh.ran_together()}
        assert together & {("opentrons.ExecuteRecipe", "tecan.RunMeasurement"),
                           ("tecan.RunMeasurement", "opentrons.ExecuteRecipe")}

    def test_under_a_limit_of_one_nothing_overlaps(self, fresh):
        result = run(fresh, workflow((1, RECIPE, []), (2, READ, [])),
                     validate=False, max_concurrency=1)
        assert result.steps_completed == 2
        assert fresh.ran_together() == []


class TestTwoPlates:

    def test_a_chain_does_not_wait_for_the_other_chains_slow_step(self, fresh):
        scenario = json.loads(fresh.scenario_path.read_text(encoding="utf-8"))
        scenario["opentrons"]["ExecuteRecipe"]["duration"] = 2.5
        fresh.reset(scenario)
        # Chain 1: recipe (slow).  Chain 2: tray out, then tray in.
        result = run(fresh, workflow((1, RECIPE, []), (2, TRAY_OUT, []), (3, TRAY_IN, [2])),
                     validate=False)
        assert result.steps_completed == 3, result.errors
        intervals = {str(i).split("(")[0]: i for i in fresh.intervals()}
        assert intervals["tecan.PlateIn"].start < intervals["opentrons.ExecuteRecipe"].end, (
            "the reader's second step waited for the liquid handler's recipe")

    def test_a_second_plate_waits_for_the_reader_to_be_emptied(self, fresh):
        # Two plates want the reader. Nothing orders the second place after
        # the first pick; the position being occupied does.
        definition = workflow(
            (1, PLACE_TC, []), (2, READ, [1]), (3, PICK_TC, [2]),
            (4, PLACE_TC, []), (5, PICK_TC, [4]))
        result = run(fresh, definition)
        assert result.success, result.errors
        tasks = [i for i in fresh.intervals() if i.server == "mobile"]
        place, pick = STEP[PLACE_TC]["Parameters"]["TaskId"], STEP[PICK_TC]["Parameters"]["TaskId"]
        order = [("place" if t.parameters["TaskId"] == place else "pick") for t in tasks]
        assert order == ["place", "pick", "place", "pick"], (
            "a plate was placed in a reader that still had one in it")

    def test_a_plate_nobody_takes_out_is_reported(self, fresh):
        result = run(fresh, workflow((1, PLACE_TC, []), (2, READ, [1])))
        assert result.success
        assert any("slot:tecan was still occupied" in e for e in result.errors)

    def test_a_second_plate_with_nothing_to_empty_the_reader_is_refused(self, fresh):
        result = run(fresh, workflow((1, PLACE_TC, []), (2, PLACE_TC, [1])))
        assert not result.success
        assert any("slot:tecan" in e and "leaves occupied" in e for e in result.errors)
        assert fresh.commands() == [], "a refused workflow moved something"


class TestATaskNobodyDeclared:

    def test_it_is_refused_before_anything_moves(self, fresh):
        definition = workflow((1, PLACE_OT, []))
        definition["Steps"][0]["Parameters"]["TaskId"] = "taught-yesterday-declared-nowhere"
        result = run(fresh, definition)
        assert not result.success
        assert any("is not in that declaration" in e for e in result.errors)
        assert fresh.commands() == []


class TestWhenSomethingFails:

    def _failing_recipe(self, lab, **spec):
        scenario = json.loads(lab.scenario_path.read_text(encoding="utf-8"))
        scenario["opentrons"]["ExecuteRecipe"] = dict(duration=0.2, **spec)
        lab.reset(scenario)

    def test_a_failed_recipe_is_not_sent_again(self, fresh):
        # It would succeed the second time - which is exactly why it must not
        # get one: on the real deck that is a recipe restarted from its first
        # dispense into wells already filled.
        self._failing_recipe(fresh, fail="pipette error", fail_times=1)
        result = run(fresh, workflow((1, RECIPE, []), (2, PICK_OT, [1])))
        assert not result.success
        assert fresh.commands("opentrons") == ["opentrons.ExecuteRecipe"]
        assert fresh.commands("mobile") == [], "the run carried on past the failure"

    def test_with_retries_switched_on_it_is(self, lab):
        with_retry = SimLab(servers=[], retry_on_failure=True)
        # Same instruments, another configuration: reuse the running servers.
        with_retry.servers = lab.servers
        with_retry.timeline_path = lab.timeline_path
        with_retry.scenario_path = lab.scenario_path
        lab.reset()
        self._failing_recipe(lab, fail="pipette error", fail_times=1)
        import os
        from src.settings import reload_settings
        previous = os.environ["LABOS_CONFIG"]
        os.environ["LABOS_CONFIG"] = str(with_retry.write_config())
        reload_settings()
        try:
            result = asyncio.run(with_retry.run_workflow(workflow((1, RECIPE, []))))
        finally:
            os.environ["LABOS_CONFIG"] = previous
            reload_settings()
            with_retry.stop()
        assert result.success, result.errors
        assert lab.commands("opentrons") == ["opentrons.ExecuteRecipe"] * 2


class TestAnEmergencyStop:

    def test_it_stops_the_instrument_and_the_run_and_sends_nothing_more(self, fresh):
        scenario = json.loads(fresh.scenario_path.read_text(encoding="utf-8"))
        scenario["opentrons"]["ExecuteRecipe"] = {"hang": True}
        fresh.reset(scenario)

        async def _scenario():
            from src.emergency import plan_stop, verdict
            core, registry, executor = await fresh.orchestrator()
            running = asyncio.ensure_future(executor.execute(
                Workflow.from_dict(workflow((1, RECIPE, []), (2, PICK_OT, [1]))), validate=True))
            for _ in range(100):                      # until the recipe is in flight
                if fresh.commands("opentrons"):
                    break
                await asyncio.sleep(0.05)

            halted = halt_running_workflows()
            plan = plan_stop(core.list_instruments())
            outcomes = await asyncio.gather(*(
                core.execute_command(instrument.id, command, {})
                for instrument, command in plan.targets))
            for each in halted:
                each.cancel_inflight()
            report = verdict(plan, sum(1 for o in outcomes if o.success), [])
            return report, plan, await asyncio.wait_for(running, timeout=10)

        report, plan, result = asyncio.run(_scenario())

        sent = {(instrument.id, command) for instrument, command in plan.targets}
        assert sent == {("opentrons", "EmergencyStop"), ("mobile", "CancelTask"),
                        ("tecan", "AbortMeasurement")}
        assert report["status"] == "stopped"
        assert report["skipped_instruments"] == []
        assert report["not_applicable"] == ["ManualStation"]

        assert not result.success
        recipe = [i for i in fresh.intervals() if i.command == "ExecuteRecipe"]
        assert [i.status for i in recipe] == ["stopped"], "the recipe was sent again"
        assert fresh.commands("mobile") == [], "the next step started after the stop"


class TestAbortingAMeasurement:
    """The reader is stopped the way i-control's Stop button stops it."""

    def test_a_measurement_in_progress_is_aborted_and_the_run_ends(self, fresh):
        scenario = json.loads(fresh.scenario_path.read_text(encoding="utf-8"))
        scenario["tecan"]["RunMeasurement"] = {"hang": True}
        fresh.reset(scenario)

        async def _scenario():
            core, _registry, executor = await fresh.orchestrator()
            running = asyncio.ensure_future(executor.execute(
                Workflow.from_dict(workflow((1, READ, []), (2, PICK_TC, [1]))), validate=False))
            for _ in range(100):
                if fresh.commands("tecan"):
                    break
                await asyncio.sleep(0.05)
            # Sent while the measurement holds the reader: it must not queue
            # behind it.
            for each in halt_running_workflows():
                outcome = await core.execute_command("tecan", "AbortMeasurement", {})
                each.cancel_inflight()
            return outcome, await asyncio.wait_for(running, timeout=10)

        outcome, result = asyncio.run(_scenario())
        assert outcome.success
        assert not result.success
        reads = [i for i in fresh.intervals() if i.command == "RunMeasurement"]
        assert [i.status for i in reads] == ["stopped"]
        assert fresh.commands("mobile") == []
