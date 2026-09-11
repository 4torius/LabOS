"""Refusing a module command before the robot homes.

Every module command reaches the robot the same way: generate a one-step
protocol, upload it, create a run, play it. Playing a run homes the Flex. So a
module name that the deck cannot satisfy used to home the robot and only then
fail on a loadModule — the operator saw the arm move, then an error, which
reads like the command half worked.

Two separate things have to hold, and conflating them is what hid the problem
on this bench: the active hardware config declared a HeaterShaker at A1 while
the robot reported no modules attached at all. The config is a description of
the deck written by a person; it can be out of date, and the only authority on
what is plugged in is the robot.
"""
import re
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))

SERVER_DIR = V1_DIR / "SiLA2" / "OpentronsSiLA2Server"
IMPL = (SERVER_DIR / "src" / "workflow_api_impl.py").read_text(encoding="utf-8")
FDL = (SERVER_DIR / "features" / "WorkflowAPI.sila.xml").read_text(encoding="utf-8")

MODULE_COMMANDS = ("SetModuleTemperature", "ShakeModule", "SetModuleLatch")


def _method_body(signature: str) -> str:
    start = IMPL.index(signature)
    following = re.compile(r"^    (?:async )?def ", re.M).search(IMPL, start + 1)
    return IMPL[start:following.start() if following else len(IMPL)]


class TestTheCheckRunsBeforeAnythingIsGenerated:

    @pytest.mark.parametrize("command", MODULE_COMMANDS)
    def test_every_module_command_resolves_its_module_first(self, command):
        body = _method_body(f"    def {command}(")
        assert "_require_module" in body, (
            f"{command} hands its module name straight to the robot; a wrong "
            f"name homes the Flex before failing"
        )

    @pytest.mark.parametrize("command", MODULE_COMMANDS)
    def test_no_module_command_generates_a_protocol(self, command):
        """Module control does not belong in a protocol run.

        A run homes the robot before it starts, takes twenty to thirty seconds
        to upload, analyse and play, and the engine deactivates the modules
        when it ends - so SetModuleTemperature reported success and left the
        heater off, the target dropped along with the run that set it.
        POST /commands runs the same command outside any run: nothing homes, it
        returns in milliseconds, and the module keeps what it was given.
        """
        body = _method_body(f"    def {command}(")
        assert "_run_inline" not in body, (
            f"{command} is back to generating a protocol; that homes the robot "
            f"and the module is deactivated when the run ends"
        )
        assert "_module_command" in body

    @pytest.mark.parametrize("command", MODULE_COMMANDS)
    def test_the_check_comes_before_the_robot_is_touched(self, command):
        body = _method_body(f"    def {command}(")
        assert body.index("_require_module") < body.index("_module_command")


class TestWhatTheCheckRequires:

    def _body(self):
        return _method_body("    def _require_module(")

    def test_it_consults_the_active_hardware_config(self):
        assert "get_modules()" in self._body()

    def test_it_also_asks_the_robot_what_is_attached(self):
        """The config is a description; the robot is the authority."""
        assert "ot.robot.get_modules()" in self._body()

    def test_an_unreachable_robot_does_not_block_the_command(self):
        """Not reachable is not the same as not attached."""
        body = self._body()
        assert "except Exception" in body
        assert "return match" in body

    def test_the_refusal_says_nothing_moved(self):
        assert "Nothing was moved" in self._body()

    def test_the_refusal_lists_what_is_available(self):
        body = self._body()
        assert "Modules it declares" in body
        assert "no modules attached" in body


class TestTheFeatureDefinitionSaysSo:

    @pytest.mark.parametrize("command", MODULE_COMMANDS)
    def test_the_precondition_is_documented_on_the_command(self, command):
        block = re.search(
            rf"<Command>\s*<Identifier>{command}</Identifier>(.*?)</Command>",
            FDL, re.S).group(1)
        assert "reported attached by the robot" in block
        assert "nothing actuates" in block

    @pytest.mark.parametrize("command", MODULE_COMMANDS)
    def test_it_says_the_robot_arm_does_not_move(self, command):
        """MOVES HARDWARE on these means the module actuates, not the gantry."""
        block = re.search(
            rf"<Command>\s*<Identifier>{command}</Identifier>(.*?)</Command>",
            FDL, re.S).group(1)
        assert "does not home" in block

    @pytest.mark.parametrize("command", MODULE_COMMANDS)
    def test_they_still_announce_that_they_move_hardware(self, command):
        block = re.search(
            rf"<Command>\s*<Identifier>{command}</Identifier>(.*?)</Command>",
            FDL, re.S).group(1)
        assert "MOVES HARDWARE" in block


class TestTheDirectCommandChannel:
    """POST /commands, which the robot itself points at: /modules/{serial}
    answers 410 "This endpoint has been removed. Use POST /commands instead."
    """

    def _body(self):
        return _method_body("    async def _module_command(")

    def test_it_waits_for_the_command_to_complete(self):
        assert '"waitUntilComplete": True' in self._body()

    def test_a_failed_command_is_not_reported_as_success(self):
        """The endpoint answers 201 for a command that then fails on the
        module; the status inside the response is the real outcome."""
        body = self._body()
        assert 'data.get("status") == "failed"' in body
        assert 'data.get("error")' in body

    def test_a_refusal_carries_the_robots_own_words(self):
        assert "response.text[:300]" in self._body()

    def test_shaking_closes_the_latch_itself(self):
        body = _method_body("    def ShakeModule(")
        assert "closeLabwareLatch" in body
        assert body.index("closeLabwareLatch") < body.index("setAndWaitForShakeSpeed")

    def test_a_zero_duration_stops_the_shaker(self):
        body = _method_body("    def ShakeModule(")
        assert "deactivateShaker" in body

    def test_zero_degrees_deactivates_the_heater(self):
        body = _method_body("    def SetModuleTemperature(")
        assert "deactivateHeater" in body

    def test_waiting_for_temperature_is_the_robots_job(self):
        """Not a client-side polling loop."""
        body = _method_body("    def SetModuleTemperature(")
        assert "heaterShaker/waitForTemperature" in body
