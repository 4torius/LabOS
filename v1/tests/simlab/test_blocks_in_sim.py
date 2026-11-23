"""A workflow made only of blocks runs, end to end, in the simulated lab.

Video - pipette a plate on the Opentrons, read it on the Tecan, robot back to
its working pose - is three blocks. Expanded the way the builder does it
(each block's first step waits for the step above it), with the choices the
blocks ask for filled in, it must run with nothing overlapping on a shared
resource, even with no cap on concurrency.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
if str(V1_DIR) not in sys.path:
    sys.path.insert(0, str(V1_DIR))

from sim_lab.lab import SimLab  # noqa: E402
from src.blocks import expand, load_blocks  # noqa: E402

pytestmark = pytest.mark.simlab

TASKS = {t["TaskName"]: t["TaskId"] for t in json.loads(
    (V1_DIR / "Library/MobileTasks/_options.json").read_text(encoding="utf-8-sig"))}
CHOICES = {"RecipeName": "test_6.json", "ProtocolFile": "Script1",
           "PlateType": "greiner_96_wellplate_382ul"}


def _compose(*block_ids):
    blocks = {b.id: b for b in load_blocks(V1_DIR / "Library" / "Blocks")[0]}
    steps = []
    for block_id in block_ids:
        expanded, errors = expand(blocks[block_id], lambda i, a, p, label: (TASKS.get(label), "missing"))
        assert errors == []
        offset = len(steps)
        for k, step in enumerate(expanded):
            deps = [d + offset for d in step["DependsOn"]] or ([offset] if offset else [])
            for name in step.pop("Ask"):
                step["Parameters"][name] = CHOICES[name]
            step.pop("Description")
            steps.append({**step, "StepNumber": offset + k + 1, "DependsOn": deps})
    return {"WorkflowName": "from_blocks", "SchemaVersion": "1.0", "Steps": steps}


def test_video_made_of_three_blocks_runs_without_a_clash():
    workflow = _compose("pipette_on_opentrons", "read_on_tecan", "robot_working_pose")
    assert len(workflow["Steps"]) == 5 + 9 + 1  # the same 15 robot, OT and Tecan steps as Video
    with SimLab(max_concurrency=0) as lab:
        result = asyncio.run(lab.run_workflow(workflow))
        assert result.success, result.errors
        assert result.steps_completed == len(workflow["Steps"])
        assert lab.any_overlap() == []
