"""Steps on an ordered instrument run in the order they are written.

Where the mobile robot *is* cannot be a resource: `pick_from_tecan` is only
right after `go_to_tecan`, and nothing in the resource model knows that. A
two-plate workflow made the author chain every robot step by hand, across both
plates, or a robot step could start while the robot was somewhere else. The
robot's catalog now says `"ordered": true` and the executor adds the edges.
"""
import asyncio
import json
from pathlib import Path

import pytest

from src.catalog import parse_catalog
from src.resources import parse_declaration
from src.workflow import Workflow, WorkflowStep, apply_instrument_order
from tests.unit.test_resource_exclusion import _executor, _step

V1 = Path(__file__).resolve().parents[2]

ROBOT = {"resources": {
    "ordered": True,
    "provides": ["instrument:mobile"],
    "commands": {
        "ExecuteTask": {"claims": ["instrument:mobile"]},
        "CancelTask": {"control": True},
    },
}}
READER = {"resources": {
    "provides": ["instrument:tecan"],
    "commands": {"PlateOut": {"claims": ["instrument:tecan"]}},
}}


class TestTheDeclaration:

    def test_ordered_is_read(self):
        assert parse_declaration(ROBOT["resources"]).ordered is True
        assert parse_declaration(READER["resources"]).ordered is False

    def test_anything_but_a_boolean_is_reported_and_ignored(self):
        declaration = parse_declaration({"ordered": "yes"})
        assert declaration.ordered is False
        assert any("ordered" in p for p in declaration.problems)

    def test_the_shipped_robot_is_ordered(self):
        catalog = json.loads((V1 / "SiLA2/MobileSiLA2Server/library_catalog.json")
                             .read_text(encoding="utf-8"))
        assert parse_catalog(catalog).resources.ordered is True


class TestTheEdges:

    def _steps(self):
        return [WorkflowStep(1, "mobile", "ExecuteTask"),
                WorkflowStep(2, "tecan", "PlateOut"),
                WorkflowStep(3, "mobile", "ExecuteTask"),
                WorkflowStep(4, "mobile", "ExecuteTask", depends_on=[3])]

    def test_each_ordered_step_depends_on_the_one_before_it(self):
        steps = self._steps()
        apply_instrument_order(steps, lambda s: s.instrument if s.instrument == "mobile" else None)
        assert [s.depends_on for s in steps] == [[], [], [1], [3]]
        assert steps[2].implied_depends_on == [1]
        assert steps[3].implied_depends_on == []  # already written by the author

    def test_applying_twice_changes_nothing(self):
        steps = self._steps()
        key = lambda s: s.instrument if s.instrument == "mobile" else None  # noqa: E731
        apply_instrument_order(steps, key)
        apply_instrument_order(steps, key)
        assert steps[2].depends_on == [1]
        assert steps[2].implied_depends_on == [1]

    def test_a_saved_workflow_shows_only_what_was_written(self):
        steps = self._steps()
        apply_instrument_order(steps, lambda s: s.instrument if s.instrument == "mobile" else None)
        saved = Workflow(name="W", steps=steps).to_dict()["Steps"]
        assert saved[2]["DependsOn"] == []


class TestTheExecutor:

    @pytest.mark.asyncio
    async def test_robot_steps_without_dependencies_still_run_in_written_order(self):
        executor, witnessed = _executor({"mobile": ROBOT, "tecan": READER})
        order = []
        original = executor.registry.execute.side_effect

        async def recording(instrument, action, parameters, timeout=None):
            if instrument == "mobile":
                order.append(parameters["TaskName"])
            return await original(instrument, action, parameters, timeout)

        executor.registry.execute.side_effect = recording
        steps = [
            _step(1, "mobile", "ExecuteTask", {"TaskName": "go_to_tecan"}),
            _step(2, "mobile", "ExecuteTask", {"TaskName": "pick_from_tecan"}),
            _step(3, "tecan", "PlateOut"),
            _step(4, "mobile", "ExecuteTask", {"TaskName": "go_to_opentrons"}),
        ]
        result = await executor.execute(Workflow(name="W", steps=steps), validate=False,
                                        max_concurrency=0)
        assert result.success
        assert order == ["go_to_tecan", "pick_from_tecan", "go_to_opentrons"]
        # The reader is not ordered with the robot: it may overlap it.
        assert any({"mobile", "tecan"} <= set(m) for m in witnessed)

    @pytest.mark.asyncio
    async def test_a_control_command_is_not_put_in_the_queue(self):
        executor, _ = _executor({"mobile": ROBOT})
        steps = [_step(1, "mobile", "ExecuteTask", {"TaskName": "a"}),
                 _step(2, "mobile", "CancelTask"),
                 _step(3, "mobile", "ExecuteTask", {"TaskName": "b"})]
        executor._apply_instrument_order(Workflow(name="W", steps=steps))
        assert steps[1].depends_on == []
        assert steps[2].depends_on == [1]

    def test_an_order_that_contradicts_the_dependencies_is_named(self):
        executor, _ = _executor({"mobile": ROBOT})
        # Step 1 waits for step 2, but the robot runs 1 before 2: a cycle.
        steps = [_step(1, "mobile", "ExecuteTask", {"TaskName": "a"}, depends_on=[2]),
                 _step(2, "mobile", "ExecuteTask", {"TaskName": "b"})]
        errors = asyncio.run(executor.validate_workflow(Workflow(name="W", steps=steps)))
        cycle = [e for e in errors if "cycle" in e.message.lower()]
        assert cycle and "written order" in cycle[0].message
