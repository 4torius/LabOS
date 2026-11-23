"""Which command stops which instrument, and what a stop may then claim."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
if str(V1_DIR) not in sys.path:
    sys.path.insert(0, str(V1_DIR))

from src.catalog import parse_catalog  # noqa: E402
from src.emergency import plan_stop, stop_command_of, verdict  # noqa: E402
from tests.unit.test_emergency_stop import FakeLabCore, _endpoint  # noqa: E402


def _command(name, required=()):
    return SimpleNamespace(id=name, parameters=[
        SimpleNamespace(name=p, required=True) for p in required])


def _instrument(key, name, commands, catalog=None):
    return SimpleNamespace(id=key, name=name, commands=commands,
                           catalog=parse_catalog(catalog or {}))


ROBOT = _instrument("mobile", "GoFaGo Mobile Robot",
                    [_command("ExecuteTask", ["TaskId"]), _command("CancelTask")],
                    {"emergency_stop": {"command": "CancelTask"}})
HANDLER = _instrument("opentrons", "OpentronsFlex",
                      [_command("ExecuteRecipe", ["RecipeName"]), _command("EmergencyStop")],
                      {"emergency_stop": {"command": "EmergencyStop"}})
READER = _instrument("tecan", "TecanM200Pro",
                     [_command("RunMeasurement"), _command("AbortMeasurement")],
                     {"emergency_stop": {"command": "AbortMeasurement", "timeout_seconds": 30}})
#: An instrument whose server has no stop command at all.
STOPLESS = _instrument("stopless", "Old Reader", [_command("RunMeasurement"), _command("PlateIn")])
BENCH = _instrument("manual_station", "Manual Station",
                    [_command("RequestOperatorTask", ["Description"]),
                     _command("CancelTask", ["TaskId"])],
                    {"emergency_stop": {"not_applicable": "an operator at a bench"}})


class TestWhichCommandStopsAnInstrument:

    def test_the_one_its_catalog_names(self):
        assert stop_command_of(ROBOT) == ("CancelTask", "")

    def test_a_server_that_says_nothing_is_tried_against_the_usual_names(self):
        silent = _instrument("x", "X", [_command("Halt")])
        assert stop_command_of(silent) == ("Halt", "")

    def test_but_never_with_a_command_that_needs_an_argument(self):
        # The bench's CancelTask needs the id of the task to cancel.
        silent = _instrument("x", "X", [_command("CancelTask", ["TaskId"])])
        command, why = stop_command_of(silent)
        assert command is None and "no stop command" in why

    def test_a_catalog_naming_a_command_the_server_lacks_is_not_trusted(self):
        wrong = _instrument("x", "X", [_command("Halt")],
                            {"emergency_stop": {"command": "Stopp"}})
        command, why = stop_command_of(wrong)
        assert command is None and "Stopp" in why

    def test_the_bench_has_nothing_to_stop(self):
        assert stop_command_of(BENCH)[0] == ""

    def test_an_instrument_without_a_stop_command_cannot_be_stopped(self):
        assert stop_command_of(STOPLESS)[0] is None

    def test_the_reader_is_aborted_and_given_longer(self):
        plan = plan_stop([READER, ROBOT])
        assert (READER, "AbortMeasurement") in plan.targets
        assert plan.timeouts == {"tecan": 30.0}


class TestWhatTheStopMayClaim:

    def test_stopped_only_when_everything_stoppable_acknowledged(self):
        plan = plan_stop([ROBOT, HANDLER, BENCH])
        report = verdict(plan, acknowledged=2, failures=[])
        assert report["status"] == "stopped"
        assert report["not_applicable"] == ["Manual Station"]

    def test_an_instrument_with_no_stop_command_makes_it_partial(self):
        # It used to read "all instruments confirmed".
        plan = plan_stop([ROBOT, HANDLER, STOPLESS, BENCH])
        report = verdict(plan, acknowledged=2, failures=[])
        assert report["status"] == "partial"
        assert "Old Reader" in report["message"]
        assert report["skipped_instruments"] == ["Old Reader"]

    def test_the_whole_bicocca_lab_can_now_be_stopped(self):
        plan = plan_stop([ROBOT, HANDLER, READER, BENCH])
        assert verdict(plan, acknowledged=3, failures=[])["status"] == "stopped"

    def test_nothing_acknowledged_reads_failed(self):
        plan = plan_stop([ROBOT, STOPLESS])
        report = verdict(plan, acknowledged=0, failures=["GoFaGo Mobile Robot: timeout"])
        assert report["status"] == "failed"
        assert "hardware" in report["message"].lower()


class TestTheEndpoint:

    @pytest.mark.asyncio
    async def test_the_robot_is_sent_its_cancel(self):
        core = FakeLabCore([ROBOT, HANDLER, READER, BENCH])
        result = await _endpoint(core)()
        assert ("mobile", "CancelTask") in core.calls
        assert ("opentrons", "EmergencyStop") in core.calls
        assert ("tecan", "AbortMeasurement") in core.calls
        assert not any(key == "manual_station" for key, _ in core.calls)
        assert result["status"] == "stopped"

    @pytest.mark.asyncio
    async def test_a_running_workflow_is_reported_as_halted(self, monkeypatch):
        import src.workflow as workflow_module
        halted = SimpleNamespace(cancelled=False)
        halted.cancel_inflight = lambda: setattr(halted, "cancelled", True)
        monkeypatch.setattr(workflow_module, "halt_running_workflows", lambda: [halted])
        result = await _endpoint(FakeLabCore([ROBOT]))()
        assert result["workflow_halted"] is True
        assert halted.cancelled


class TestTheCatalogSection:

    def test_a_command_and_a_reason_together_is_reported(self):
        catalog = parse_catalog({"emergency_stop": {"command": "Stop", "not_applicable": "x"}})
        assert catalog.stop_command == "Stop" and catalog.problems

    def test_an_empty_section_is_reported(self):
        assert parse_catalog({"emergency_stop": {}}).problems

    def test_it_round_trips(self):
        assert parse_catalog({"emergency_stop": {"command": "Stop"}}).to_dict()[
            "emergency_stop"] == {"command": "Stop"}
