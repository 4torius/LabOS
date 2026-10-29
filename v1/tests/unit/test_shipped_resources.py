"""The resource declarations this repository actually ships.

`test_resources.py` tests the model with invented fixtures. This file loads the
real `library_catalog.json` of every server in `SiLA2/` plus the real
`resources:` section of `lab_config.yaml`, and asserts the topology they add up
to is coherent — because a typo in one of those files is exactly the failure
the model is built to make loud, and nothing else would catch it.

It also pins the one case the whole design exists for: the mobile robot's
transport tasks and the instrument they reach into must exclude each other,
across two servers that know nothing about one another.
"""
import json
import sys
from pathlib import Path

import pytest
import yaml

V1_DIR = Path(__file__).resolve().parents[2]
if str(V1_DIR) not in sys.path:
    sys.path.insert(0, str(V1_DIR))

from src.catalog import parse_catalog  # noqa: E402
from src.resources import build_topology, parse_config  # noqa: E402

#: catalog file -> the instrument key its server is registered under.
SHIPPED = {
    "opentrons": "SiLA2/OpentronsSiLA2Server/library_catalog.json",
    "tecan": "SiLA2/TecanM200SiLA2Server/library_catalog.json",
    "manual_station": "SiLA2/ManualStationSiLA2Server/library_catalog.json",
}


@pytest.fixture(scope="module")
def topology():
    declarations = {}
    for key, relative in SHIPPED.items():
        raw = (V1_DIR / relative).read_text(encoding="utf-8-sig")
        declarations[key] = parse_catalog(raw, source=key).resources
    # The mobile robot's server lives on the remote host and its catalog cannot
    # be edited from here, so it is declared in lab_config.yaml. Registering it
    # with an empty declaration is what the executor does for a server that
    # said nothing, which is what makes the config layer apply.
    from src.resources import ResourceDeclaration
    declarations["mobile"] = ResourceDeclaration()

    config = yaml.safe_load((V1_DIR / "lab_config.yaml").read_text(encoding="utf-8"))
    return build_topology(declarations, parse_config(config.get("resources")))


class TestTheShippedDeclarationsAreCoherent:

    def test_every_catalog_is_valid_json_with_no_parse_problems(self):
        for key, relative in SHIPPED.items():
            raw = (V1_DIR / relative).read_text(encoding="utf-8-sig")
            json.loads(raw)
            assert parse_catalog(raw, source=key).problems == [], key

    def test_no_claim_is_left_unresolved(self, topology):
        assert topology.unresolved == [], (
            "a shipped declaration claims a resource nobody provides: "
            + "; ".join(str(u) for u in topology.unresolved)
        )

    def test_the_topology_has_no_problems(self, topology):
        assert topology.problems == []

    def test_every_instrument_declares_something(self, topology):
        assert topology.undeclared == (), (
            f"{topology.undeclared} declare no resources, so their steps "
            "serialise against each other"
        )

    def test_the_resources_are_the_ones_we_expect(self, topology):
        assert topology.providers == {
            "instrument:opentrons": "opentrons",
            "deck:opentrons": "opentrons",
            "instrument:tecan": "tecan",
            "tray:tecan": "tecan",
            "instrument:manual_station": "manual_station",
            "operator:bench": "manual_station",
            "instrument:mobile": "mobile",
        }


class TestTheHazardTheModelExistsFor:

    @pytest.mark.parametrize("task,instrument,command", [
        ("place_on_OT", "opentrons", "ExecuteRecipe"),
        ("pick_from_opentrons", "opentrons", "ExecuteRecipe"),
        ("place_on_TC", "tecan", "RunMeasurement"),
        ("pick_from_tecan", "tecan", "PlateIn"),
    ])
    def test_a_transport_task_excludes_the_instrument_it_reaches_into(
            self, topology, task, instrument, command):
        transport = topology.claims_for("mobile", "ExecuteTask", {"TaskName": task})
        work = topology.claims_for(instrument, command)
        assert topology.conflicts(transport, work), (
            f"{task} and {instrument}.{command} were allowed to run together"
        )

    @pytest.mark.parametrize("task", [
        "go_to_Opentrons", "go_to_Tecan", "Docking", "WorkingPose", "pick_from_dima",
    ])
    def test_moving_without_entering_anything_blocks_nobody(self, topology, task):
        moving = topology.claims_for("mobile", "ExecuteTask", {"TaskName": task})
        assert moving == {"instrument:mobile"}, (
            f"{task} occupies more than the robot, which would serialise it "
            "against work it does not touch"
        )

    def test_the_two_instruments_do_not_exclude_each_other(self, topology):
        assert not topology.conflicts(
            topology.claims_for("opentrons", "ExecuteRecipe"),
            topology.claims_for("tecan", "RunMeasurement"),
        )

    def test_two_steps_needing_the_operator_take_turns(self, topology):
        # The scarce thing at a manual station is the person, so two operator
        # tasks must not be issued at once.
        task = topology.claims_for("manual_station", "RequestOperatorTask")
        assert "operator:bench" in task
        assert topology.conflicts(task, task)


class TestControlCommandsStayInterrupting:

    @pytest.mark.parametrize("instrument,control,busy", [
        ("opentrons", "EmergencyStop", "ExecuteRecipe"),
        ("opentrons", "AbortRun", "ExecuteRecipe"),
        ("opentrons", "PauseRun", "ExecuteRecipe"),
        ("tecan", "ListProtocols", "RunMeasurement"),
        ("manual_station", "SendNotification", "RequestOperatorTask"),
        ("mobile", "CancelTask", "ExecuteTask"),
    ])
    def test_a_control_command_never_waits_for_the_work_it_interrupts(
            self, topology, instrument, control, busy):
        assert topology.claims_for(instrument, control) == frozenset(), (
            f"{instrument}.{control} occupies something, so it would queue "
            f"behind {busy} — a stop that waits is not a stop"
        )
