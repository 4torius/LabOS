"""_update_tracker_smart() (server.py): for a non-succeeded run, must record
whatever robot.get_actual_tip_usage() determined - including a genuine zero
- and fall back to the full planned usage ONLY when that determination
itself failed (returned None), never merely because it came back empty.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from _pkg_loader import load_opentrons_src

_ot = load_opentrons_src()


def _make_server(tmp_path):
    config = _ot.ServerConfig(config_path=str(tmp_path / "nonexistent.yaml"), use_mdns=False)
    srv = _ot.OpentronsSiLA2Server(config)
    srv.robot = MagicMock()
    srv.tip_tracker = _ot.TipTracker(state_file=str(tmp_path / "tip_state.json"))
    return srv


class TestUpdateTrackerSmart:
    @pytest.mark.asyncio
    async def test_succeeded_run_applies_planned_usage(self, tmp_path):
        srv = _make_server(tmp_path)
        await srv._update_tracker_smart("run-1", {"MyTips_50": 4}, "succeeded")
        assert srv.tip_tracker.get_usage("MyTips_50") == 4

    @pytest.mark.asyncio
    async def test_failed_run_with_zero_actual_usage_charges_zero_not_planned(self, tmp_path):
        """The exact bug this fixes: a run that failed before picking up any
        tip (get_actual_tip_usage correctly determines {}) must NOT fall
        back to the full planned usage - that was the real-world incident
        (planned 4+1, actually 0 picked up, but 4+1 got charged anyway)."""
        srv = _make_server(tmp_path)
        srv.robot.get_actual_tip_usage = AsyncMock(return_value={})

        await srv._update_tracker_smart("run-1", {"MyTips_50": 4, "MyTips_1000": 1}, "stopped-error-recovery: No Tip Detected")

        assert srv.tip_tracker.get_usage("MyTips_50") == 0
        assert srv.tip_tracker.get_usage("MyTips_1000") == 0

    @pytest.mark.asyncio
    async def test_failed_run_with_partial_actual_usage_charges_only_that(self, tmp_path):
        srv = _make_server(tmp_path)
        srv.robot.get_actual_tip_usage = AsyncMock(return_value={"MyTips_50": 2})

        await srv._update_tracker_smart("run-1", {"MyTips_50": 4}, "stopped")

        assert srv.tip_tracker.get_usage("MyTips_50") == 2

    @pytest.mark.asyncio
    async def test_undetermined_usage_falls_back_to_full_planned(self, tmp_path):
        """get_actual_tip_usage returning None (analysis itself failed, e.g.
        robot unreachable) is the ONLY case that should fall back to the
        planned usage as a safety net."""
        srv = _make_server(tmp_path)
        srv.robot.get_actual_tip_usage = AsyncMock(return_value=None)

        await srv._update_tracker_smart("run-1", {"MyTips_50": 4}, "failed")

        assert srv.tip_tracker.get_usage("MyTips_50") == 4

    @pytest.mark.asyncio
    async def test_analysis_raising_falls_back_to_full_planned(self, tmp_path):
        srv = _make_server(tmp_path)
        srv.robot.get_actual_tip_usage = AsyncMock(side_effect=RuntimeError("connection reset"))

        await srv._update_tracker_smart("run-1", {"MyTips_50": 4}, "failed")

        assert srv.tip_tracker.get_usage("MyTips_50") == 4
