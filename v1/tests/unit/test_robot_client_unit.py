"""Unit tests for OpentronsSiLA2Server's RobotClient — mocked HTTP, no real robot.

Covers the two robot_client.py hardening fixes:
  - wait_for_run_completion() raises TimeoutError instead of polling forever
    when a run never reaches a terminal state (this coroutine runs inside
    server.py's cmd_run_protocol, which holds self._lock for its duration —
    an unbounded wait would make the whole server unresponsive).
  - Methods that talk to the robot now call _require_connected() and raise
    a clear ConnectionError instead of silently returning {}/[] when the
    HTTP client was never initialized.
"""
import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from _pkg_loader import load_opentrons_src

_ot = load_opentrons_src()
RobotClient = _ot.RobotClient


def _disconnected_client():
    client = RobotClient(host="127.0.0.1", port=1)
    assert client._client is None
    return client


class TestRequireConnected:
    @pytest.mark.asyncio
    async def test_get_robot_info_raises_when_disconnected(self):
        client = _disconnected_client()
        with pytest.raises(ConnectionError):
            await client.get_robot_info()

    @pytest.mark.asyncio
    async def test_get_run_commands_raises_when_disconnected(self):
        client = _disconnected_client()
        with pytest.raises(ConnectionError):
            await client.get_run_commands("run-1")

    @pytest.mark.asyncio
    async def test_get_lights_raises_when_disconnected(self):
        client = _disconnected_client()
        with pytest.raises(ConnectionError):
            await client.get_lights()

    @pytest.mark.asyncio
    async def test_get_modules_raises_when_disconnected(self):
        client = _disconnected_client()
        with pytest.raises(ConnectionError):
            await client.get_modules()

    @pytest.mark.asyncio
    async def test_extract_images_raises_when_disconnected(self, tmp_path):
        client = _disconnected_client()
        with pytest.raises(ConnectionError):
            await client.extract_images_from_log("run-1", str(tmp_path))

    @pytest.mark.asyncio
    async def test_get_actual_tip_usage_raises_when_disconnected(self):
        client = _disconnected_client()
        with pytest.raises(ConnectionError):
            await client.get_actual_tip_usage("run-1")


class TestSendActionRaisesOnFailure:
    """_send_action (used by play_run/pause_run/stop_run) must raise when the
    robot API rejects the action — previously it only logged a warning and
    returned normally, so cmd_pause_run/cmd_resume_run/cmd_abort_run/
    cmd_emergency_stop would all report success even when the robot refused."""

    @pytest.mark.asyncio
    async def test_send_action_raises_when_robot_rejects(self):
        client = RobotClient(host="127.0.0.1", port=1)
        mock_response = MagicMock(is_success=False, status_code=409, text="run already terminal")
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(RuntimeError):
            await client._send_action("run-1", "stop")

    @pytest.mark.asyncio
    async def test_send_action_succeeds_silently_when_robot_accepts(self):
        client = RobotClient(host="127.0.0.1", port=1)
        mock_response = MagicMock(is_success=True)
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        await client._send_action("run-1", "play")  # must not raise

    @pytest.mark.asyncio
    async def test_stop_run_propagates_the_failure(self):
        """stop_run (called by cmd_abort_run/cmd_emergency_stop) must not
        swallow the failure and log a false 'Run stopped' success."""
        client = RobotClient(host="127.0.0.1", port=1)
        mock_response = MagicMock(is_success=False, status_code=500, text="internal error")
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(RuntimeError):
            await client.stop_run("run-1")


class TestWaitForRunCompletionTimeout:
    @pytest.mark.asyncio
    async def test_raises_timeout_error_instead_of_polling_forever(self, monkeypatch):
        client = RobotClient(host="127.0.0.1", port=1)
        # Run never reaches a terminal state.
        client.get_run_status = AsyncMock(return_value="running")

        async def _fast_sleep(_seconds):
            pass  # collapse polling delay so the test doesn't take real wall time

        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

        with pytest.raises(TimeoutError):
            await client.wait_for_run_completion("run-1", poll_interval=0.01, timeout=0.05)

    @pytest.mark.asyncio
    async def test_returns_promptly_on_terminal_state(self, monkeypatch):
        client = RobotClient(host="127.0.0.1", port=1)
        client.get_run_status = AsyncMock(return_value="succeeded")

        result = await client.wait_for_run_completion("run-1", poll_interval=0.01, timeout=5.0)

        assert result == "succeeded"

    @pytest.mark.asyncio
    async def test_none_timeout_disables_the_bound(self, monkeypatch):
        """Explicit opt-out still works for callers that want to wait forever."""
        client = RobotClient(host="127.0.0.1", port=1)
        statuses = iter(["running", "running", "succeeded"])
        client.get_run_status = AsyncMock(side_effect=lambda *_: next(statuses))

        async def _fast_sleep(_seconds):
            pass

        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

        result = await client.wait_for_run_completion("run-1", poll_interval=0.01, timeout=None)
        assert result == "succeeded"
