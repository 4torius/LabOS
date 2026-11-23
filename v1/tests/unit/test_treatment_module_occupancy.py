"""Recipe page treatments: one plate on a module at a time.

Two reservoirs kept warm in different phases: the second "Keep warm" used to
move its reservoir onto a Heater-Shaker the first one still occupied, and the
robot refused it ("LocationIsOccupiedError", confirmed in opentrons_simulate).
Now the one already there goes back to its own slot first. The cleanup "Keep
warm" adds at the end only switches the module off when its reservoir has
already gone home, instead of moving it back on to move it off again.
"""
import sys
from pathlib import Path

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))

from src.excel_recipe_parser import _emit_treatment  # noqa: E402

HOME = {"Source_1": "A2", "Source_2": "B2", "TargetPlate": "D2"}
WARM = {"Temperature": 30, "WaitForTemp": True}


def _run(treatments):
    steps, req, mods, where = [], {}, {}, {}
    for t in treatments:
        _emit_treatment(t, steps, req, mods, where, {}, HOME)
    return steps, where


def _moves(steps):
    return [(s["LabwareID"], s["NewLocation"]) for s in steps if s["Command"] == "MoveLabware"]


def test_the_reservoir_on_the_module_goes_home_before_the_next_one_goes_on():
    steps, where = _run([
        {"kind": "HeaterShaker", "target": "Source_1", "params": WARM, "return_after": False},
        {"kind": "HeaterShaker", "target": "Source_2", "params": WARM, "return_after": False},
    ])
    assert _moves(steps) == [("Source_1", "HeaterShaker"), ("Source_1", "A2"), ("Source_2", "HeaterShaker")]
    assert where == {"Source_1": "A2", "Source_2": "HeaterShaker"}


def test_the_same_reservoir_twice_is_not_moved_again():
    steps, _ = _run([
        {"kind": "HeaterShaker", "target": "Source_1", "params": WARM, "return_after": False},
        {"kind": "HeaterShaker", "target": "Source_1", "params": {"RPM": 300, "Duration": 5}, "return_after": False},
    ])
    assert _moves(steps) == [("Source_1", "HeaterShaker")]


def test_a_cleanup_whose_reservoir_already_went_home_only_switches_the_module_off():
    steps, _ = _run([
        {"kind": "HeaterShaker", "target": "Source_1", "params": WARM, "return_after": False},
        {"kind": "HeaterShaker", "target": "Source_2", "params": WARM, "return_after": False},
        {"kind": "HeaterShaker", "target": "Source_1", "params": {"DeactivateHeater": True},
         "return_after": True, "linked_to": "a"},
        {"kind": "HeaterShaker", "target": "Source_2", "params": {"DeactivateHeater": True},
         "return_after": True, "linked_to": "b"},
    ])
    assert _moves(steps)[-1] == ("Source_2", "B2")
    assert ("Source_1", "HeaterShaker") not in _moves(steps)[1:], "not put back on to be taken off"
    off = [s for s in steps if s["Command"] == "HeaterShaker" and s.get("DeactivateHeater")]
    assert len(off) == 2 and "OpenLatch" not in off[0]


def test_a_cleanup_whose_reservoir_is_still_on_the_module_returns_it():
    steps, _ = _run([
        {"kind": "HeaterShaker", "target": "Source_1", "params": WARM, "return_after": False},
        {"kind": "HeaterShaker", "target": "Source_1", "params": {"DeactivateHeater": True},
         "return_after": True, "linked_to": "a"},
    ])
    assert _moves(steps) == [("Source_1", "HeaterShaker"), ("Source_1", "A2")]
