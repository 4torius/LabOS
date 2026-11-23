"""The builder's plate check against the instruments' real declarations.

src/plates.py reads acquires / releases / needs from the topology the
simulated servers serve - the same catalogs the bench uses - so this is where
a wrong declaration would show up as a false warning (or a missing one).
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
from src.plates import report_workflow  # noqa: E402
from src.workflow import is_locally_handled  # noqa: E402

pytestmark = pytest.mark.simlab

EXAMPLE = json.loads((V1_DIR / "Library/Workflows/Esempio_Due_Piastre.workflow.json")
                     .read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def check():
    with SimLab() as lab:
        _core, _registry, executor = asyncio.run(lab.orchestrator())
        topology = executor._topology()

        def key_of(instrument, action):
            if is_locally_handled(instrument, action):
                return ""
            server = executor.registry.get_server_by_name(instrument)
            return executor._key_of(server) if server is not None else ""

        yield lambda steps: report_workflow(steps, key_of, topology)


def _warnings(report):
    return [(int(n), w) for n, e in report.items() for w in e["warnings"]]


def test_the_two_plate_example_is_clean(check):
    report = check(EXAMPLE["Steps"])
    assert _warnings(report) == []
    assert report["13"]["after"] == {"A": "in tecan"}           # reading A
    assert report["18"]["after"] == {"A": "in tecan", "B": "in opentrons"}


def test_forgetting_to_collect_plate_a_is_caught(check):
    steps = [s for s in copy.deepcopy(EXAMPLE["Steps"]) if s["StepNumber"] != 23]
    found = _warnings(check(steps))
    assert found and "carrying plates A, B" in found[0][1]


def test_reading_b_before_it_is_in_the_reader_is_caught(check):
    steps = copy.deepcopy(EXAMPLE["Steps"])
    for step in steps:
        if step["StepNumber"] == 13:      # the first read, which is A's
            step["Plate"] = "B"
    found = _warnings(check(steps))
    assert any(n == 13 and "works on plate B in tecan" in w for n, w in found)
