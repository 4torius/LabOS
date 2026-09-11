"""Dispatching a one-off protocol, and reading back which run it was.

Two methods on the Opentrons server look interchangeable and are not.
cmd_execute_recipe_by_name returns a (run_id, result) tuple; cmd_run_protocol
returns a single formatted string. The inline-protocol helper was written by
copying the recipe call site, so it unpacked that string into two names and
raised "too many values to unpack (expected 2)". Nothing caught it, because
every command that reaches the robot through that helper — MoveLabware,
SetModuleTemperature, ShakeModule, SetModuleLatch — needs a robot to exercise.
On the bench all four failed identically, and the failure came back through
SiLA2 as a ValueError with no hint of where it came from.
"""
import re
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))
sys.path.insert(0, str(V1_DIR / "tests" / "unit"))

from _pkg_loader import load_opentrons_src  # noqa: E402

_ot = load_opentrons_src()
from _ot_src.server import parse_run_id  # noqa: E402

IMPL = (V1_DIR / "SiLA2" / "OpentronsSiLA2Server" / "src"
        / "workflow_api_impl.py").read_text(encoding="utf-8")
SERVER = (V1_DIR / "SiLA2" / "OpentronsSiLA2Server" / "src"
          / "server.py").read_text(encoding="utf-8")


def _method_body(signature: str) -> str:
    """One method's source, up to whatever def comes next."""
    start = IMPL.index(signature)
    following = re.compile(r"^    (?:async )?def ", re.M).search(IMPL, start + 1)
    return IMPL[start:following.start() if following else len(IMPL)]


class TestParseRunId:

    def test_it_reads_back_what_cmd_run_protocol_writes(self):
        run_id = "3f2a91c4-0e5b-4d17-9a22-6b8e5c1d7f00"
        assert parse_run_id(f"Run {run_id}: succeeded") == run_id

    def test_a_status_containing_a_colon_does_not_eat_the_id(self):
        run_id = "abc-123"
        assert parse_run_id(f"Run {run_id}: failed: pick up tip") == run_id

    @pytest.mark.parametrize("result", [
        "", None, "Refilled: opentrons_flex_96_tiprack_1000ul", "succeeded",
    ])
    def test_anything_not_in_that_shape_yields_no_id(self, result):
        assert parse_run_id(result) == ""

    def test_the_recipe_path_and_the_inline_path_parse_it_the_same_way(self):
        """One implementation, not two that can drift apart."""
        assert SERVER.count("def parse_run_id(") == 1
        assert "parse_run_id(result)" in SERVER
        assert "parse_run_id(result)" in IMPL


class TestInlineProtocolDispatch:
    """Read statically: importing the implementation registers its .proto in
    protobuf's process-global pool, which collides with a real Opentrons server
    running on the same bench (see test_opentrons_feature_surface)."""

    def _run_inline_body(self):
        return _method_body("    def _run_inline(")

    def test_it_does_not_unpack_a_string_into_two_names(self):
        body = self._run_inline_body()
        assert not re.search(r"\w+\s*,\s*\w+\s*=\s*self\._run_async", body), (
            "cmd_run_protocol returns one string, not (run_id, result) — "
            "unpacking it is the bug that broke every module and gripper command"
        )

    def test_it_still_derives_a_run_id(self):
        """The id is what an operator uses to find the run's log on the robot,
        and a failed run reports it."""
        assert "parse_run_id" in self._run_inline_body()

    def test_it_still_refuses_a_run_that_did_not_succeed(self):
        assert "_raise_unless_succeeded" in self._run_inline_body()

    def test_the_recipe_path_keeps_its_tuple(self):
        """cmd_execute_recipe_by_name really does return two values; this test
        is here so a later 'consistency' cleanup does not break that instead."""
        assert "async def cmd_execute_recipe_by_name(self, recipe_name: str, on_progress=None) -> tuple:" in SERVER
        assert "return parse_run_id(result), result" in SERVER


class TestCameraConnectivityCheck:
    """RobotClient.is_connected is a property. The server object has a method
    of the same name. Calling the property raised "'bool' object is not
    callable", which the capture reported as "HTTP capture failed" — so every
    CaptureImage fell through to the protocol path, which homes the robot, and
    with AllowRobotMotion false refused. The camera looked unreachable when it
    had never been asked."""

    def test_robot_client_exposes_it_as_a_property(self):
        client = (V1_DIR / "SiLA2" / "OpentronsSiLA2Server" / "src"
                  / "robot_client.py").read_text(encoding="utf-8")
        assert re.search(r"@property\s+def is_connected", client)

    def test_the_capture_path_reads_it_without_calling_it(self):
        body = _method_body("    async def _capture_over_http(")
        assert "ot.robot.is_connected" in body
        assert "ot.robot.is_connected()" not in body
