"""Every robot task a saved workflow uses exists and is declared.

The robot's tasks are renamed and re-taught on the robot itself (place_on_OT
became place_on_Opentrons on 2026-11-20). The robot's catalog is exhaustive,
so a task it does not list is refused at validation - correct, but found at
the bench is too late. This compares the saved workflows against the last task
list the robot reported (Library/MobileTasks/_options.json, rewritten whenever
LabOS reads it) and the names the robot's catalog declares.

When this fails after a RefreshTasks: re-pick the task in the workflow if its
id is gone, and declare the name in
SiLA2/MobileSiLA2Server/library_catalog.json with what it physically occupies.
"""
import json
import re
from pathlib import Path

import pytest

V1 = Path(__file__).resolve().parents[2]


def _load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


ROBOT_TASKS = {t["TaskId"]: t["TaskName"]
               for t in _load(V1 / "Library/MobileTasks/_options.json")}
DECLARED = set(_load(V1 / "SiLA2/MobileSiLA2Server/library_catalog.json")
               ["resources"]["commands"]["ExecuteTask"]["by_parameter"]["TaskName"])
USES = sorted({(path.name, task_id)
               for path in (V1 / "Library/Workflows").glob("*.json")
               for task_id in re.findall(r'"TaskId"\s*:\s*"(\w+)"',
                                         path.read_text(encoding="utf-8-sig"))})


@pytest.mark.parametrize("workflow,task_id", USES)
def test_the_task_is_still_on_the_robot(workflow, task_id):
    assert task_id in ROBOT_TASKS, (
        f"{workflow} uses TaskId {task_id}, which the robot no longer reports")


@pytest.mark.parametrize("workflow,task_id", USES)
def test_the_task_is_declared(workflow, task_id):
    name = ROBOT_TASKS.get(task_id)
    if name is None:
        pytest.skip("not on the robot; reported by the test above")
    assert name in DECLARED, (
        f"{workflow} uses {name}, which the robot's catalog does not declare")


WORKFLOWS = sorted((V1 / "Library/Workflows").glob("*.json"))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_a_saved_workflow_says_what_waits_for_what(path):
    """A workflow of several steps with no DependsOn anywhere has no order:
    with max_concurrency above 1 every step would start at once (Video did,
    until 2026-11-23). One step that genuinely needs nothing is fine; all of
    them is a workflow nobody ordered."""
    steps = _load(path).get("Steps") or []
    if len(steps) < 2:
        pytest.skip("one step needs no order")
    assert any(s.get("DependsOn") for s in steps), (
        f"{path.name}: no step depends on any other, so nothing keeps them in order")
