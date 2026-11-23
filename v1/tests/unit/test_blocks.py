"""Blocks: named step sequences the Workflow Builder inserts in one go.

See src/blocks.py. These cover the format, the shipped blocks against the
robot's last reported task list, and the label lookup that keeps a block
working when a task is re-taught with a new id.
"""
import json
from pathlib import Path

import pytest

from src.blocks import expand, load_blocks, parse_block

V1 = Path(__file__).resolve().parents[2]
BLOCKS, PROBLEMS = load_blocks(V1 / "Library" / "Blocks")
ROBOT_TASKS = {t["TaskName"]: t["TaskId"] for t in json.loads(
    (V1 / "Library/MobileTasks/_options.json").read_text(encoding="utf-8-sig"))}
DECLARED = set(json.loads((V1 / "SiLA2/MobileSiLA2Server/library_catalog.json")
                          .read_text(encoding="utf-8"))
               ["resources"]["commands"]["ExecuteTask"]["by_parameter"]["TaskName"])


def _by_task_name(instrument, action, parameter, label):
    if label in ROBOT_TASKS:
        return ROBOT_TASKS[label], None
    return None, f"no task {label}"


class TestTheFormat:

    def test_steps_wait_for_the_one_before_unless_told_otherwise(self):
        block = parse_block("b", {"Steps": [
            {"Instrument": "x", "Action": "A"},
            {"Instrument": "x", "Action": "B"},
            {"Instrument": "y", "Action": "C", "DependsOn": [1]},
        ]}, [])
        steps, errors = expand(block, _by_task_name)
        assert errors == []
        assert [s["DependsOn"] for s in steps] == [[], [1], [1]]

    @pytest.mark.parametrize("raw,needle", [
        ({}, "Steps"),
        ({"Steps": [{"Instrument": "x"}]}, "Action"),
        ({"Steps": [{"Instrument": "x", "Action": "A", "DependsOn": [1]}]}, "DependsOn"),
        ({"Steps": [{"Instrument": "x", "Action": "A", "Ask": "Recipe"}]}, "Ask"),
    ])
    def test_a_malformed_block_is_reported_not_raised(self, raw, needle):
        problems = []
        assert parse_block("b", raw, problems) is None
        assert any(needle in p for p in problems)

    def test_a_label_becomes_the_current_value(self):
        block = parse_block("b", {"Steps": [{"Instrument": "mobile", "Action": "ExecuteTask",
            "Parameters": {"TaskId": {"label": "go_to_tecan"}, "ExecutionMode": "Normal"}}]}, [])
        steps, errors = expand(block, _by_task_name)
        assert steps[0]["Parameters"] == {"TaskId": ROBOT_TASKS["go_to_tecan"],
                                          "ExecutionMode": "Normal"}

    def test_a_label_nobody_knows_is_named_and_left_empty(self):
        block = parse_block("b", {"Steps": [{"Instrument": "mobile", "Action": "ExecuteTask",
            "Parameters": {"TaskId": {"label": "fly_to_mars"}}}]}, [])
        steps, errors = expand(block, _by_task_name)
        assert steps[0]["Parameters"]["TaskId"] == ""
        assert errors and "fly_to_mars" in errors[0]


class TestTheShippedBlocks:

    def test_every_block_file_is_valid(self):
        assert PROBLEMS == []
        assert len(BLOCKS) >= 8

    @pytest.mark.parametrize("block", BLOCKS, ids=lambda b: b.id)
    def test_every_robot_task_exists_and_is_declared(self, block):
        for step in block.steps:
            label = (step.parameters.get("TaskId") or {}).get("label") \
                if isinstance(step.parameters.get("TaskId"), dict) else None
            if label:
                assert label in ROBOT_TASKS, f"{block.id}: the robot has no task {label}"
                assert label in DECLARED, f"{block.id}: {label} is not declared"

    @pytest.mark.parametrize("block", BLOCKS, ids=lambda b: b.id)
    def test_the_tray_is_open_whenever_the_robot_reaches_into_it(self, block):
        tray_open = False
        for step in block.steps:
            if step.instrument == "tecan" and step.action in ("PlateOut", "PlateIn"):
                tray_open = step.action == "PlateOut"
            label = step.parameters.get("TaskId")
            label = label.get("label") if isinstance(label, dict) else ""
            if label in ("place_on_Tecan", "pick_from_tecan"):
                assert tray_open, f"{block.id}: {label} with the tray closed"
        assert not tray_open, f"{block.id}: leaves the reader tray open"

    @pytest.mark.parametrize("block", BLOCKS, ids=lambda b: b.id)
    def test_a_choice_the_person_must_make_is_asked(self, block):
        for step in block.steps:
            if step.action == "ExecuteRecipe":
                assert "RecipeName" in step.ask
            if step.action == "RunMeasurement":
                assert {"ProtocolFile", "PlateType"} <= set(step.ask)
