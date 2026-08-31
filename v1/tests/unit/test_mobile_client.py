"""Tests for LabCore.refresh_mobile_tasks — the client for the remote
MobileSiLA2Server (runs on a separate Ubuntu PC, not on this machine).

Covers: graceful handling when the robot is absent/offline, connection
errors are distinguished from malformed responses, and a bounded timeout
is passed to the remote call so a hung server can't block the orchestrator.
"""
import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.lab_core import Instrument, LabCore
from src.client import CommandResult


def _make_core(tmp_path):
    return LabCore(base_dir=tmp_path)


class TestNoMobileRobot:
    @pytest.mark.asyncio
    async def test_no_mobile_instrument_registered_returns_zero(self, tmp_path):
        core = _make_core(tmp_path)
        core._instruments = {}
        assert await core.refresh_mobile_tasks() == 0

    @pytest.mark.asyncio
    async def test_mobile_instrument_offline_returns_zero(self, tmp_path):
        core = _make_core(tmp_path)
        core._instruments = {
            "mobile": Instrument(id="mobile", name="Mobile Robot", status="offline", _server=MagicMock())
        }
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        core._client = mock_client
        assert await core.refresh_mobile_tasks() == 0
        mock_client.connect.assert_not_called()


class TestConnectionErrors:
    @pytest.mark.asyncio
    async def test_connect_failure_returns_zero_without_raising(self, tmp_path):
        core = _make_core(tmp_path)
        core._instruments = {
            "mobile": Instrument(id="mobile", name="Mobile Robot", status="online", _server=MagicMock())
        }
        mock_client = MagicMock()
        mock_client.connect = AsyncMock(side_effect=ConnectionError("PC unreachable over hotspot"))
        core._client = mock_client

        result = await core.refresh_mobile_tasks()

        assert result == 0  # must not propagate the ConnectionError

    @pytest.mark.asyncio
    async def test_connect_timeout_returns_zero_without_raising(self, tmp_path):
        core = _make_core(tmp_path)
        core._instruments = {
            "mobile": Instrument(id="mobile", name="Mobile Robot", status="online", _server=MagicMock())
        }
        mock_client = MagicMock()
        mock_client.connect = AsyncMock(side_effect=asyncio.TimeoutError())
        core._client = mock_client

        assert await core.refresh_mobile_tasks() == 0


class TestRemoteCallTimeout:
    @pytest.mark.asyncio
    async def test_get_available_tasks_is_called_with_bounded_timeout(self, tmp_path):
        core = _make_core(tmp_path)
        core._instruments = {
            "mobile": Instrument(id="mobile", name="Mobile Robot", status="online", _server=MagicMock())
        }
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.execute = AsyncMock(return_value=CommandResult(success=True, data={"tasks": []}))
        core._client = mock_client

        await core.refresh_mobile_tasks()

        _, kwargs = mock_client.execute.call_args
        assert isinstance(kwargs.get("timeout"), float)
        assert kwargs["timeout"] > 0


class TestMalformedResponse:
    @pytest.mark.asyncio
    async def test_execute_failure_returns_zero(self, tmp_path):
        core = _make_core(tmp_path)
        core._instruments = {
            "mobile": Instrument(id="mobile", name="Mobile Robot", status="online", _server=MagicMock())
        }
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.execute = AsyncMock(return_value=CommandResult(success=False, error="boom"))
        core._client = mock_client

        assert await core.refresh_mobile_tasks() == 0

    @pytest.mark.asyncio
    async def test_malformed_json_tasks_payload_does_not_raise(self, tmp_path):
        core = _make_core(tmp_path)
        core._instruments = {
            "mobile": Instrument(id="mobile", name="Mobile Robot", status="online", _server=MagicMock())
        }
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.execute = AsyncMock(
            return_value=CommandResult(success=True, data={"tasks": "{not valid json"})
        )
        core._client = mock_client

        # Must degrade to "no tasks found" rather than raising json.JSONDecodeError
        assert await core.refresh_mobile_tasks() == 0

    @pytest.mark.asyncio
    async def test_well_formed_tasks_are_saved(self, tmp_path):
        core = _make_core(tmp_path)
        core._instruments = {
            "mobile": Instrument(id="mobile", name="Mobile Robot", status="online", _server=MagicMock())
        }
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.execute = AsyncMock(
            return_value=CommandResult(
                success=True,
                data={"tasks": [{"task_id": "t1", "task_name": "Task One", "subtask_count": 2}]},
            )
        )
        core._client = mock_client

        saved = await core.refresh_mobile_tasks()

        assert saved == 1
        assert (tmp_path / "Library" / "MobileTasks" / "t1.json").exists()
