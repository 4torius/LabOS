"""The robot's own camera, and why it looked broken.

The Flex firmware has a native camera API: a still from POST /camera/picture,
and an HLS/RTMP live stream. All three enablement flags ship off, and with them
off every capture comes back 422 "Cannot take photo, camera is disabled".

Two separate faults hid that for two sessions. RobotClient.is_connected is a
property while the server object has a method of the same name, so the capture
called a bool and reported "HTTP capture failed: 'bool' object is not callable"
— the endpoint was never actually contacted. Once it was, the 422 was swallowed
by raise_for_status into a generic HTTP error, so the robot's own one-line
explanation never reached the operator.
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


def _method_body(signature: str) -> str:
    start = IMPL.index(signature)
    following = re.compile(r"^    (?:async )?def ", re.M).search(IMPL, start + 1)
    return IMPL[start:following.start() if following else len(IMPL)]


class TestTheRobotsRefusalReachesTheOperator:

    def test_a_422_is_not_flattened_into_a_generic_http_error(self):
        body = _method_body("    async def _capture_over_http(")
        assert "response.status_code == 422" in body
        assert body.index("status_code == 422") < body.index("raise_for_status")

    def test_the_robots_own_message_is_quoted_back(self):
        body = _method_body("    async def _capture_over_http(")
        assert '"message"' in body

    def test_the_message_says_where_to_fix_it(self):
        body = _method_body("    async def _capture_over_http(")
        assert "SetCameraEnabled" in body
        assert "CameraStatus" in body

    def test_a_missing_endpoint_is_still_distinguished_from_a_refusal(self):
        """404 means this robot has no camera API at all; 422 means it has one
        and is switched off. They call for different actions."""
        body = _method_body("    async def _capture_over_http(")
        assert "response.status_code == 404" in body


class TestCameraStatus:

    def test_it_is_a_property_not_a_command(self):
        properties = re.findall(r"<Property>\s*<Identifier>(\w+)</Identifier>", FDL)
        commands = re.findall(r"<Command>\s*<Identifier>(\w+)</Identifier>", FDL)
        assert "CameraStatus" in properties
        assert "CameraStatus" not in commands

    def test_it_reports_all_three_enablement_flags(self):
        body = _method_body("    def get_CameraStatus(")
        for flag in ("cameraEnabled", "liveStreamEnabled", "errorRecoveryCameraEnabled"):
            assert flag in body

    def test_it_gives_the_stream_urls(self):
        body = _method_body("    async def _camera_state(")
        assert "/camera/stream" in body
        assert "hls" in body and "rtmp" in body

    def test_the_hls_url_is_absolute(self):
        """The robot returns a path; a path is not something an operator can
        paste into a player."""
        body = _method_body("    async def _camera_state(")
        assert 'f"{base}{data.get(' in body

    def test_a_disconnected_robot_does_not_raise(self):
        body = _method_body("    def get_CameraStatus(")
        assert "not ot.robot.is_connected" in body
        assert "not connected to the robot" in body


class TestSetCameraEnabled:

    def test_it_is_declared_in_the_feature_definition(self):
        commands = re.findall(r"<Command>\s*<Identifier>(\w+)</Identifier>", FDL)
        assert "SetCameraEnabled" in commands

    def test_it_does_not_claim_to_move_hardware(self):
        block = re.search(
            r"<Command>\s*<Identifier>SetCameraEnabled</Identifier>(.*?)</Command>",
            FDL, re.S).group(1)
        assert "MOVES HARDWARE" not in block
        assert "Nothing moves" in block

    def test_the_error_recovery_camera_is_left_alone(self):
        """Whether the robot films its own error recovery is a separate
        decision from whether a photograph can be taken."""
        body = _method_body("    async def _set_camera(")
        assert '"errorRecoveryCameraEnabled": None' in body

    def test_the_stream_is_a_separate_switch_from_the_stills(self):
        """Enabling the live stream broadcasts the lab over the network; it
        must not ride along with permission to take one photograph."""
        block = re.search(
            r"<Command>\s*<Identifier>SetCameraEnabled</Identifier>(.*?)</Command>",
            FDL, re.S).group(1)
        parameters = re.findall(r"<Parameter>\s*<Identifier>(\w+)</Identifier>", block)
        assert parameters == ["Camera", "LiveStream"]

    def test_it_says_the_setting_lives_on_the_robot(self):
        block = re.search(
            r"<Command>\s*<Identifier>SetCameraEnabled</Identifier>(.*?)</Command>",
            FDL, re.S).group(1)
        assert "on the robot" in block
        assert "persists" in block

    @pytest.mark.parametrize("payload_key", [
        "cameraEnabled", "liveStreamEnabled",
    ])
    def test_the_request_uses_the_robots_own_field_names(self, payload_key):
        assert payload_key in _method_body("    async def _set_camera(")


class TestThereIsNoVideoFromThisRobot:
    """Asked for, and looked into properly before being dropped.

    The Flex advertises an HLS and an RTMP live stream. Enabling it restarts
    the live-stream service cleanly, port 1935 listens, the robot's own logs
    show no error - and no playlist or segment is ever published, checked with
    the camera completely idle. Single frames come back at about one every two
    seconds, which is not a frame rate.

    A timelapse command existed for a while as the nearest thing. It was
    removed: nobody wanted a sped-up slideshow, they wanted video.
    """

    def test_the_capture_surface_is_one_photograph(self):
        commands = re.findall(r"<Command>\s*<Identifier>(\w+)</Identifier>", FDL)
        assert "CaptureImage" in commands
        assert "CaptureTimelapse" not in commands

    def test_nothing_is_left_behind_in_the_implementation(self):
        for leftover in ("CaptureTimelapse", "_animate", "MAX_TIMELAPSE"):
            assert leftover not in IMPL, f"{leftover} survived the removal"

    def test_the_stream_urls_are_still_reported(self):
        """Reporting what the robot claims costs nothing and is how anyone
        would notice if a firmware update ever made it work."""
        body = _method_body("    async def _camera_state(")
        assert "/camera/stream" in body


class TestTheCameraIsBorrowedNotLeftOn:
    """A capture switches the camera on and puts it back how it was found.

    The Flex ships with it disabled, so asking for a frame gets 422 and looks
    broken; turning it on is part of taking a picture. Leaving it on afterwards
    is a decision about the lab, not a side effect a capture gets to make.
    """

    def test_capturing_a_still_borrows_the_camera(self):
        body = _method_body("    def CaptureImage(")
        assert "_borrow_camera()" in body
        assert "_return_camera(" in body

    def test_it_is_put_back_even_when_the_capture_fails(self):
        body = _method_body("    def CaptureImage(")
        assert "finally:" in body
        assert body.index("finally:") < body.index("_return_camera(")

    def test_a_camera_already_on_is_left_on(self):
        """Nothing to restore means nothing is switched off underneath someone."""
        body = _method_body("    def _borrow_camera(")
        assert 'before.get("cameraEnabled")' in body
        assert "return None" in body

    def test_putting_it_back_never_raises(self):
        body = _method_body("    def _return_camera(")
        assert "except Exception" in body

    def test_the_live_stream_flag_is_carried_through_untouched(self):
        """Borrowing the camera for a photograph must not start broadcasting."""
        body = _method_body("    def _borrow_camera(")
        assert 'bool(before.get("liveStreamEnabled"))' in body
