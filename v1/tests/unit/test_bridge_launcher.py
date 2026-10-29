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


class TestOrphanedBridge:
    """A bridge left over from a previous run must be replaced, never adopted.

    Adopting it — which is what start() used to do on finding the port already
    listening — broke three things silently. `self._proc` stayed None, so the
    health monitor skipped it forever; its stdout was never pumped, so its logs
    went nowhere; and restarting the Tecan server no longer restarted the
    bridge, which made the documented remedy for every bridge-level fault a
    no-op.

    Observed on the bench: the Python server was restarted at 17:18 and the
    bridge it "started" was still the process from 12:48, carrying the failed
    USB enumeration it picked up while iControl held the instrument. Connect
    kept failing with the reader switched on, cabled, and reachable from
    iControl.
    """

    def test_our_own_live_bridge_is_reused(self):
        launcher = BridgeLauncher()
        launcher._proc = _fake_process(alive=True)
        with patch.object(launcher, "_probe", return_value=True),              patch.object(launcher, "_terminate_orphan") as terminate:
            assert launcher.start() is True
            terminate.assert_not_called()

    def test_a_bridge_we_do_not_own_is_terminated_and_replaced(self):
        launcher = BridgeLauncher()
        launcher._proc = None                       # nothing of ours is running
        probes = iter([True, False])                # occupied, then free

        with patch.object(launcher, "_probe", side_effect=lambda: next(probes, False)),              patch.object(launcher, "_terminate_orphan", return_value=True) as terminate,              patch("subprocess.Popen", side_effect=OSError("stop here")):
            launcher.start()
            terminate.assert_called_once()

    def test_start_refuses_rather_than_adopting_when_the_orphan_survives(self):
        """Adopting it would leave the bridge unmonitored and unrestartable —
        worse than reporting that startup failed."""
        launcher = BridgeLauncher()
        launcher._proc = None
        with patch.object(launcher, "_probe", return_value=True),              patch.object(launcher, "_terminate_orphan", return_value=False),              patch("subprocess.Popen") as popen:
            assert launcher.start() is False
            popen.assert_not_called()

    def test_a_dead_process_of_ours_does_not_count_as_running(self):
        launcher = BridgeLauncher()
        launcher._proc = _fake_process(alive=False)
        assert launcher.running is False

    def test_terminate_orphan_reports_success_when_the_port_frees(self):
        launcher = BridgeLauncher()
        probes = iter([False])                      # already gone after the kill
        with patch.object(launcher, "_pid_on_port", return_value=9999),              patch.object(launcher, "_probe", side_effect=lambda: next(probes, False)),              patch("subprocess.run") as run,              patch("os.kill") as kill:
            assert launcher._terminate_orphan(timeout=1) is True
            assert run.called or kill.called

    def test_terminate_orphan_reports_failure_when_the_port_stays_held(self):
        launcher = BridgeLauncher()
        with patch.object(launcher, "_pid_on_port", return_value=9999),              patch.object(launcher, "_probe", return_value=True),              patch("subprocess.run"),              patch("os.kill"),              patch("time.sleep"):
            assert launcher._terminate_orphan(timeout=0.5) is False

    def test_no_owner_and_a_free_port_is_success(self):
        """The probe can race a shutdown that was already under way."""
        launcher = BridgeLauncher()
        with patch.object(launcher, "_pid_on_port", return_value=None),              patch.object(launcher, "_probe", return_value=False):
            assert launcher._terminate_orphan() is True

    def test_pid_lookup_never_raises(self):
        launcher = BridgeLauncher()
        with patch("subprocess.run", side_effect=OSError("no netstat here")):
            assert launcher._pid_on_port() is None
