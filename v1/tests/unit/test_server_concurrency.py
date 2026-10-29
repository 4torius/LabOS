"""Concurrency tests for OpentronsSiLA2Server.

cmd_abort_run / cmd_pause_run / cmd_resume_run / cmd_emergency_stop deliberately
do NOT use the same lock as cmd_run_protocol (that lock is held for an entire
run's duration — reusing it here would prevent abort from ever interrupting a
running protocol). Instead they share a separate _control_lock so they can
still interrupt a run, while not racing against each other and corrupting
_current_run_id/_is_running/_status. This test proves that mutual exclusion.
"""
import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from _pkg_loader import load_opentrons_src

_ot = load_opentrons_src()


def _make_server(tmp_path):
    config = _ot.ServerConfig(config_path=str(tmp_path / "nonexistent.yaml"), use_mdns=False)
    config.auto_drop_tip_on_abort = False  # keep the test focused on the lock, not _run_emergency_drop
    srv = _ot.OpentronsSiLA2Server(config)
    srv.robot = MagicMock()
    srv.generator = MagicMock()
    return srv


class TestControlLockSerializesConcurrentCalls:
    @pytest.mark.asyncio
    async def test_abort_and_pause_never_run_concurrently(self, tmp_path):
        srv = _make_server(tmp_path)
        srv._current_run_id = "run-1"

        concurrent = 0
        max_concurrent = 0

        async def _tracked(*_a, **_kw):
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.05)
            concurrent -= 1

        srv.robot.stop_run = AsyncMock(side_effect=_tracked)
        srv.robot.pause_run = AsyncMock(side_effect=_tracked)

        await asyncio.gather(srv.cmd_abort_run(), srv.cmd_pause_run())

        assert max_concurrent == 1

    @pytest.mark.asyncio
    async def test_emergency_stop_and_resume_never_run_concurrently(self, tmp_path):
        srv = _make_server(tmp_path)
        srv._current_run_id = "run-1"

        concurrent = 0
        max_concurrent = 0

        async def _tracked(*_a, **_kw):
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.05)
            concurrent -= 1

        srv.robot.stop_run = AsyncMock(side_effect=_tracked)
        srv.robot.get_run_status = AsyncMock(return_value="stopped")
        srv.robot.play_run = AsyncMock(side_effect=_tracked)

        await asyncio.gather(srv.cmd_emergency_stop(), srv.cmd_resume_run())

        assert max_concurrent == 1

    @pytest.mark.asyncio
    async def test_final_state_consistent_after_concurrent_abort_and_emergency_stop(self, tmp_path):
        srv = _make_server(tmp_path)
        srv._current_run_id = "run-1"
        srv._is_running = True
        srv._status = "running"

        srv.robot.stop_run = AsyncMock()
        srv.robot.get_run_status = AsyncMock(return_value="stopped")

        await asyncio.gather(srv.cmd_abort_run(), srv.cmd_emergency_stop())

        # Whichever ran last, the state must be a valid post-stop state, not a
        # torn mix (e.g. _is_running True with _current_run_id already None).
        assert srv._current_run_id is None
        assert srv._is_running is False
        assert srv._status == "idle"


class TestRunProtocolBusyCheckFastPath:
    """cmd_run_protocol's busy-check used to run only after acquiring the lock
    that's held for a run's entire (now potentially unbounded) duration — so a
    second concurrent call never actually saw _is_running=True, it just queued
    silently behind the first run instead of failing fast with "Robot is busy"."""

    @pytest.mark.asyncio
    async def test_second_call_fails_fast_instead_of_queueing(self, tmp_path):
        srv = _make_server(tmp_path)

        first_call_started = asyncio.Event()

        async def _slow_execute(code, recipe=None):
            first_call_started.set()
            await asyncio.sleep(0.2)
            return "run-1"

        srv._execute_protocol = _slow_execute
        srv.robot.wait_for_run_completion = AsyncMock(return_value="succeeded")
        srv.robot.dismiss_run = AsyncMock()
        srv.robot.extract_images_from_log = AsyncMock()

        first_task = asyncio.create_task(srv.cmd_run_protocol("print('x')", protocol_type="python"))
        await first_call_started.wait()

        with pytest.raises(RuntimeError, match="busy"):
            await srv.cmd_run_protocol("print('y')", protocol_type="python")

        await first_task  # let the first call finish cleanly before teardown


class TestNoRunToControl:
    @pytest.mark.asyncio
    async def test_pause_with_no_active_run_is_a_no_op(self, tmp_path):
        srv = _make_server(tmp_path)
        srv._current_run_id = None
        srv.robot.pause_run = AsyncMock()

        result = await srv.cmd_pause_run()

        assert "No run" in result
        srv.robot.pause_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_abort_with_no_active_run_is_a_no_op(self, tmp_path):
        srv = _make_server(tmp_path)
        srv._current_run_id = None
        srv.robot.stop_run = AsyncMock()

        result = await srv.cmd_abort_run()

        assert "No run" in result
        srv.robot.stop_run.assert_not_called()
