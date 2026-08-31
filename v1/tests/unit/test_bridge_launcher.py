"""Tests for TecanM200SiLA2Server's BridgeLauncher — subprocess lifecycle and
the post-startup health monitor added to catch a mid-session bridge crash
(previously only checked readiness once, at startup)."""
import importlib

import pytest
from unittest.mock import MagicMock, patch

from _pkg_loader import load_tecan_src

load_tecan_src()  # registers the _tc_src package in sys.modules
BridgeLauncher = importlib.import_module("_tc_src.bridge_launcher").BridgeLauncher


def _fake_process(alive=True, returncode=None):
    proc = MagicMock()
    proc.poll.return_value = None if alive else (returncode if returncode is not None else 1)
    proc.pid = 4242
    proc.stdout = None
    proc.stderr = None
    return proc


class TestStop:
    def test_stop_terminates_running_process(self):
        launcher = BridgeLauncher()
        proc = _fake_process(alive=True)
        proc.wait.return_value = None
        launcher._proc = proc

        launcher.stop()

        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()

    def test_stop_kills_process_if_terminate_times_out(self):
        import subprocess as subprocess_module
        launcher = BridgeLauncher()
        proc = _fake_process(alive=True)
        proc.wait.side_effect = subprocess_module.TimeoutExpired(cmd="x", timeout=5)
        launcher._proc = proc

        launcher.stop()

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_stop_on_already_dead_process_does_nothing(self):
        launcher = BridgeLauncher()
        proc = _fake_process(alive=False)
        launcher._proc = proc

        launcher.stop()  # must not raise

        proc.terminate.assert_not_called()


def _fast_wait_after(n_calls):
    """A drop-in replacement for threading.Event.wait() that never really
    blocks and reports "stopped" after `n_calls` invocations — lets
    _monitor_loop's internal waits (poll interval + backoff) be driven
    synchronously and deterministically instead of sleeping in real time."""
    state = {"n": 0}

    def _wait(timeout=None):
        state["n"] += 1
        return state["n"] > n_calls

    return _wait


class TestHealthMonitor:
    def test_monitor_restarts_bridge_after_crash(self):
        launcher = BridgeLauncher()
        launcher._proc = _fake_process(alive=False, returncode=1)
        launcher._monitor_stop.wait = _fast_wait_after(1)

        with patch.object(launcher, "start", return_value=True) as mock_start:
            launcher._monitor_loop(interval=0.01, max_restart_attempts=3)

        mock_start.assert_called_once()

    def test_monitor_marks_degraded_after_exhausting_restart_attempts(self):
        launcher = BridgeLauncher()
        launcher._proc = _fake_process(alive=False, returncode=1)
        # 1 outer poll tick + 2 backoff waits (one per failed attempt) = 3 calls
        # before the for-else sets degraded; stop right after.
        launcher._monitor_stop.wait = _fast_wait_after(3)

        with patch.object(launcher, "start", return_value=False):
            launcher._monitor_loop(interval=0.01, max_restart_attempts=2)

        assert launcher.degraded is True

    def test_monitor_does_nothing_while_process_alive_and_responsive(self):
        launcher = BridgeLauncher()
        launcher._proc = _fake_process(alive=True)
        launcher._monitor_stop.wait = _fast_wait_after(2)

        with patch.object(launcher, "start") as mock_start, \
             patch.object(launcher, "_probe", return_value=True):
            launcher._monitor_loop(interval=0.01, max_restart_attempts=3)

        mock_start.assert_not_called()

    def test_monitor_restarts_bridge_when_alive_but_unresponsive(self):
        """A hung-but-alive process (e.g. a deadlocked gRPC server thread) is
        not caught by proc.poll() alone — the monitor must also probe the
        port and restart if it doesn't answer."""
        launcher = BridgeLauncher()
        proc = _fake_process(alive=True)
        proc.wait.return_value = None
        launcher._proc = proc
        launcher._monitor_stop.wait = _fast_wait_after(1)

        with patch.object(launcher, "start", return_value=True) as mock_start, \
             patch.object(launcher, "_probe", return_value=False):
            launcher._monitor_loop(interval=0.01, max_restart_attempts=3)

        proc.terminate.assert_called_once()  # stop() called on the hung process
        mock_start.assert_called_once()

    def test_stop_health_monitor_is_idempotent(self):
        launcher = BridgeLauncher()
        launcher.stop_health_monitor()  # never started — must not raise
        launcher.stop_health_monitor()  # calling twice — must not raise


class TestProbe:
    def test_probe_false_when_channel_never_ready(self):
        launcher = BridgeLauncher(host="127.0.0.1", port=1)
        # No bridge listening on port 1 — probe must return False, not raise.
        assert launcher._probe() is False
