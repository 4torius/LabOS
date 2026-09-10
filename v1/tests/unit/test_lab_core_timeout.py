"""Tests for LabCore.execute_command: bounded timeout wiring and offline rejection.

Without a timeout, a hung robot command would block the calling coroutine (and
any awaiting HTTP handler) forever — see src/lab_core.py execute_command().
"""
import asyncio

import pytest
import yaml
from unittest.mock import AsyncMock, MagicMock

from src.lab_core import Command, Instrument, LabCore
from src.client import CommandResult


def _make_core_with_instrument(tmp_path, *, status="online", default_timeout=None):
    if default_timeout is not None:
        (tmp_path / "lab_config.yaml").write_text(
            yaml.dump({"workflow": {"default_timeout": default_timeout}})
        )
    core = LabCore(base_dir=tmp_path)
    cmd = Command(id="DoThing", name="DoThing", feature="SomeFeature")
    inst = Instrument(id="instr1", name="Instr1", status=status, commands=[cmd], _server=MagicMock())
    core._instruments = {"instr1": inst}
    return core


class TestExecuteCommandTimeout:
    @pytest.mark.asyncio
    async def test_configured_default_timeout_is_passed_to_client_execute(self, tmp_path):
        core = _make_core_with_instrument(tmp_path, default_timeout=42)
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.execute = AsyncMock(return_value=CommandResult(success=True, data={}))
        core._client = mock_client

        result = await core.execute_command("instr1", "DoThing", {})

        assert result.success is True
        _, kwargs = mock_client.execute.call_args
        assert kwargs["timeout"] == 42.0

    @pytest.mark.asyncio
    async def test_missing_config_falls_back_to_a_finite_default(self, tmp_path):
        core = _make_core_with_instrument(tmp_path)  # no lab_config.yaml at all
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.execute = AsyncMock(return_value=CommandResult(success=True, data={}))
        core._client = mock_client

        await core.execute_command("instr1", "DoThing", {})

        _, kwargs = mock_client.execute.call_args
        assert isinstance(kwargs["timeout"], float)
        assert kwargs["timeout"] > 0

    @pytest.mark.asyncio
    async def test_zero_default_timeout_means_unlimited_and_is_passed_through(self, tmp_path):
        """default_timeout: 0 means unlimited and must reach client.execute() as
        0 (not silently replaced by a finite fallback) — client.execute() is
        responsible for normalizing 0 to None, not _command_timeout()."""
        core = _make_core_with_instrument(tmp_path, default_timeout=0)
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.execute = AsyncMock(return_value=CommandResult(success=True, data={}))
        core._client = mock_client

        await core.execute_command("instr1", "DoThing", {})

        _, kwargs = mock_client.execute.call_args
        assert kwargs["timeout"] == 0.0

    @pytest.mark.asyncio
    async def test_null_default_timeout_means_unlimited(self, tmp_path):
        """default_timeout: null must mean unlimited, same as 0 — matching
        _command_timeout()'s own documented contract ("0 or null means
        unlimited"). Previously this fell back to a hardcoded 300.0 instead,
        so no config setting could ever actually produce an unlimited
        timeout: a real bug (confirmed on real hardware — a still-running
        ExecuteRecipe got reported "failed" at the 600s default, which then
        skipped the plate-tracking DB write gated on a success result)."""
        (tmp_path / "lab_config.yaml").write_text(
            yaml.dump({"workflow": {"default_timeout": None}})
        )
        core = LabCore(base_dir=tmp_path)
        cmd = Command(id="DoThing", name="DoThing", feature="SomeFeature")
        inst = Instrument(id="instr1", name="Instr1", status="online", commands=[cmd], _server=MagicMock())
        core._instruments = {"instr1": inst}
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.execute = AsyncMock(return_value=CommandResult(success=True, data={}))
        core._client = mock_client

        result = await core.execute_command("instr1", "DoThing", {})

        assert result.success is True
        _, kwargs = mock_client.execute.call_args
        assert kwargs["timeout"] is None


class TestExecuteCommandObservableBypassesTimeout:
    """Observable SiLA2 commands (ExecuteRecipe, RunMeasurement, ...) are
    genuinely long-running by design and already have their own server-side
    real-status tracking - a client-side cutoff on these doesn't catch a
    real hang, it just abandons a command that's still working and reports
    a false failure. Confirmed on real hardware: an ExecuteRecipe still
    genuinely running past the 600s default got reported "failed" here."""

    @pytest.mark.asyncio
    async def test_observable_command_gets_no_timeout_even_with_finite_default(self, tmp_path):
        core = _make_core_with_instrument(tmp_path, default_timeout=42)
        cmd = Command(id="ExecuteRecipe", name="ExecuteRecipe", feature="Opentrons", observable=True)
        core._instruments["instr1"].commands = [cmd]
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.execute = AsyncMock(return_value=CommandResult(success=True, data={}))
        core._client = mock_client

        await core.execute_command("instr1", "ExecuteRecipe", {})

        _, kwargs = mock_client.execute.call_args
        assert kwargs["timeout"] is None

    @pytest.mark.asyncio
    async def test_non_observable_command_still_gets_the_bounded_default(self, tmp_path):
        core = _make_core_with_instrument(tmp_path, default_timeout=42)
        cmd = Command(id="GetStatus", name="GetStatus", feature="Opentrons", observable=False)
        core._instruments["instr1"].commands = [cmd]
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.execute = AsyncMock(return_value=CommandResult(success=True, data={}))
        core._client = mock_client

        await core.execute_command("instr1", "GetStatus", {})

        _, kwargs = mock_client.execute.call_args
        assert kwargs["timeout"] == 42.0

    @pytest.mark.asyncio
    async def test_client_timeout_is_reported_as_a_failed_result_not_raised(self, tmp_path):
        """A hung command must surface as ExecutionResult(success=False), not
        propagate an unhandled exception out of execute_command."""
        core = _make_core_with_instrument(tmp_path, default_timeout=1)
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.execute = AsyncMock(side_effect=asyncio.TimeoutError("simulated hang"))
        core._client = mock_client

        result = await core.execute_command("instr1", "DoThing", {})

        assert result.success is False
        assert "hang" in result.error


class TestExecuteCommandOfflineRejection:
    @pytest.mark.asyncio
    async def test_offline_instrument_is_rejected_before_touching_the_client(self, tmp_path):
        core = _make_core_with_instrument(tmp_path, status="offline")
        mock_client = MagicMock()
        mock_client.execute = AsyncMock()
        core._client = mock_client

        result = await core.execute_command("instr1", "DoThing", {})

        assert result.success is False
        assert "offline" in result.error.lower()
        mock_client.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_instrument_returns_error(self, tmp_path):
        core = _make_core_with_instrument(tmp_path)
        result = await core.execute_command("does_not_exist", "DoThing", {})
        assert result.success is False
        assert "not found" in result.error.lower()
