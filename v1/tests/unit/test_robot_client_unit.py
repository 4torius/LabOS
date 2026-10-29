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


def _connected_client():
    client = RobotClient(host="127.0.0.1", port=1)
    client._client = MagicMock()  # satisfies _require_connected() without a real HTTP client
    return client


class TestGetActualTipUsage:
    """Regression: this used to build its labwareId -> loadName map from
    each loadLabware command's own "result" field on the /runs/{id}/commands
    *list* endpoint - which never populates "result" at all (same gap as
    reconstruct_tip_mount_state's pipette_mounts, tip_tracker.py), so the
    map was ALWAYS empty and this always returned {} no matter what actually
    happened. Since _update_tracker_smart() only falls back to full planned
    usage when this comes back None (not merely empty) after the matching
    fix there, a run that failed on its very first PickUpTip (confirmed on
    hardware: tipPhysicallyMissing on well A1) used to get charged the FULL
    planned tip count anyway. Now sources the map from get_run_detail()'s
    data.labware, like pipette_mounts already does."""

    @pytest.mark.asyncio
    async def test_counts_succeeded_pickups_against_the_right_rack(self):
        client = _connected_client()
        client.get_run_detail = AsyncMock(return_value={"data": {"labware": [
            {"id": "lw-50", "loadName": "opentrons_flex_96_tiprack_50ul"},
            {"id": "lw-1000", "loadName": "opentrons_flex_96_tiprack_1000ul"},
            {"id": "lw-plate", "loadName": "corning_96_wellplate_360ul_flat"},
        ]}})
        client.get_run_commands = AsyncMock(return_value={"data": [
            {"commandType": "pickUpTip", "status": "succeeded", "params": {"labwareId": "lw-50"}},
            {"commandType": "pickUpTip", "status": "succeeded", "params": {"labwareId": "lw-50"}},
            {"commandType": "pickUpTip", "status": "succeeded", "params": {"labwareId": "lw-1000"}},
        ]})

        usage = await client.get_actual_tip_usage("run-1")

        assert usage == {"opentrons_flex_96_tiprack_50ul": 2, "opentrons_flex_96_tiprack_1000ul": 1}

    @pytest.mark.asyncio
    async def test_a_run_that_failed_on_its_first_pickup_reports_zero_not_none(self):
        """The exact real-world scenario this bug was found from: a
        pickUpTip that failed (tipPhysicallyMissing) before any tip was
        ever actually picked up must report {} (a determined zero), never
        None (which the caller would read as "couldn't tell, use planned")."""
        client = _connected_client()
        client.get_run_detail = AsyncMock(return_value={"data": {"labware": [
            {"id": "lw-50", "loadName": "opentrons_flex_96_tiprack_50ul"},
        ]}})
        client.get_run_commands = AsyncMock(return_value={"data": [
            {"commandType": "pickUpTip", "status": "failed", "params": {"labwareId": "lw-50"},
             "error": {"errorType": "tipPhysicallyMissing", "detail": "No Tip Detected"}},
        ]})

        usage = await client.get_actual_tip_usage("run-1")

        assert usage == {}
        assert usage is not None

    @pytest.mark.asyncio
    async def test_returns_none_when_the_analysis_itself_fails(self):
        client = _connected_client()
        client.get_run_detail = AsyncMock(side_effect=RuntimeError("connection reset"))

        usage = await client.get_actual_tip_usage("run-1")

        assert usage is None


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


class TestAddLabwareDefinition:
    """add_labware_definition() - registers a custom labware definition
    against a run via POST /runs/{run_id}/labware_definitions, needed before
    ctx.load_labware() can succeed for labware the robot doesn't already
    know (confirmed on real hardware: a custom plate referenced only by
    LoadName, never registered, fails the run at the first loadLabware)."""

    @pytest.mark.asyncio
    async def test_posts_definition_wrapped_in_data(self):
        client = RobotClient(host="127.0.0.1", port=1)
        mock_response = MagicMock(is_success=True)
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)
        definition = {"parameters": {"loadName": "my_custom_plate"}, "wells": {}}

        await client.add_labware_definition("run-1", definition)

        client._client.post.assert_awaited_once_with(
            "/runs/run-1/labware_definitions", json={"data": definition}
        )

    @pytest.mark.asyncio
    async def test_raises_when_robot_rejects_definition(self):
        client = RobotClient(host="127.0.0.1", port=1)
        mock_response = MagicMock(is_success=False, status_code=400, text="invalid definition")
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)
        definition = {"parameters": {"loadName": "broken_plate"}}

        with pytest.raises(Exception, match="broken_plate"):
            await client.add_labware_definition("run-1", definition)

    @pytest.mark.asyncio
    async def test_raises_connection_error_when_disconnected(self):
        client = _disconnected_client()
        with pytest.raises(ConnectionError):
            await client.add_labware_definition("run-1", {"parameters": {"loadName": "x"}})


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


class TestWaitForRunCompletionAwaitingRecovery:
    """awaiting-recovery is the Flex firmware state entered when
    liquid_presence_detection (VerifyLiquid on Transfer/Distribute/
    Consolidate) probes a well and finds no liquid. It's neither in
    terminal_states nor contains "failed"/"stopped", so without explicit
    handling the run would sit here polling until `timeout` (default 7200s)
    waiting for a human to resolve it via the touchscreen — which LabOS has
    no UI for. It must be stopped immediately instead."""

    @pytest.mark.asyncio
    async def test_stops_the_run_and_returns_promptly(self, monkeypatch):
        client = RobotClient(host="127.0.0.1", port=1)
        client.get_run_status = AsyncMock(return_value="awaiting-recovery")
        client.get_run_detail = AsyncMock(return_value=None)
        client.get_run_commands = AsyncMock(return_value=None)
        client.stop_run = AsyncMock()

        async def _fast_sleep(_seconds):
            pass

        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

        result = await client.wait_for_run_completion("run-1", poll_interval=0.01, timeout=5.0)

        client.stop_run.assert_awaited_once_with("run-1")
        assert result.startswith("stopped-error-recovery")

    @pytest.mark.asyncio
    async def test_enriches_message_from_run_detail_errors(self):
        client = RobotClient(host="127.0.0.1", port=1)
        client.get_run_status = AsyncMock(return_value="awaiting-recovery")
        client.get_run_detail = AsyncMock(return_value={
            "data": {"errors": [{"detail": "Liquid not found in well A1 of Source_1"}]}
        })
        client.stop_run = AsyncMock()

        result = await client.wait_for_run_completion("run-1", poll_interval=0.01, timeout=5.0)

        assert "Liquid not found in well A1 of Source_1" in result

    @pytest.mark.asyncio
    async def test_falls_back_to_the_failed_commands_own_error_when_run_detail_errors_is_empty(self):
        """Regression: a command-level failure (e.g. pickUpTip hitting
        tipPhysicallyMissing — confirmed on hardware) leaves the run
        detail's data.errors empty even though the failing command itself
        carries a full error object in the run's own command log. Without
        this fallback, the reason silently defaulted to a stale, wrong
        "liquid not detected" message regardless of the real cause."""
        client = RobotClient(host="127.0.0.1", port=1)
        client.get_run_status = AsyncMock(return_value="awaiting-recovery")
        client.get_run_detail = AsyncMock(return_value={"data": {"errors": []}})
        client.get_run_commands = AsyncMock(return_value={"data": [
            {"commandType": "home", "status": "succeeded"},
            {"commandType": "pickUpTip", "status": "failed",
             "error": {"errorType": "tipPhysicallyMissing", "detail": "No Tip Detected"}},
        ]})
        client.stop_run = AsyncMock()

        result = await client.wait_for_run_completion("run-1", poll_interval=0.01, timeout=5.0)

        assert "No Tip Detected" in result
        assert "liquid" not in result.lower()

    @pytest.mark.asyncio
    async def test_returns_promptly_even_if_stop_run_itself_fails(self):
        """A failed stop attempt must not make this hang or propagate — the
        caller still needs a status back, not an unrelated exception."""
        client = RobotClient(host="127.0.0.1", port=1)
        client.get_run_status = AsyncMock(return_value="awaiting-recovery")
        client.get_run_detail = AsyncMock(return_value=None)
        client.get_run_commands = AsyncMock(return_value=None)
        client.stop_run = AsyncMock(side_effect=RuntimeError("robot rejected stop"))

        result = await client.wait_for_run_completion("run-1", poll_interval=0.01, timeout=5.0)

        assert result.startswith("stopped-error-recovery")

    @pytest.mark.asyncio
    async def test_returns_promptly_even_if_get_run_detail_fails(self):
        """The best-effort error-message enrichment must not block the stop
        itself if the robot API call for run detail fails."""
        client = RobotClient(host="127.0.0.1", port=1)
        client.get_run_status = AsyncMock(return_value="awaiting-recovery")
        client.get_run_detail = AsyncMock(side_effect=RuntimeError("connection reset"))
        client.stop_run = AsyncMock()

        result = await client.wait_for_run_completion("run-1", poll_interval=0.01, timeout=5.0)

        client.stop_run.assert_awaited_once_with("run-1")
        assert result.startswith("stopped-error-recovery")
