"""
bridge_launcher.py
==================
Starts and stops the C# COM bridge subprocess.
The bridge exe lives in ../bridge/bin/Debug/net48/TecanSiLA2Server.exe
and is started silently (no window) by the Python SiLA2 server.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

import grpc

logger = logging.getLogger(__name__)

_BRIDGE_DIR = Path(__file__).parent.parent / "bridge"
_BRIDGE_EXE_RELEASE = _BRIDGE_DIR / "bin" / "Release" / "net48" / "TecanSiLA2Server.exe"
_BRIDGE_EXE_DEBUG   = _BRIDGE_DIR / "bin" / "Debug"   / "net48" / "TecanSiLA2Server.exe"
_BRIDGE_EXE = _BRIDGE_EXE_RELEASE if _BRIDGE_EXE_RELEASE.exists() else _BRIDGE_EXE_DEBUG


class BridgeLauncher:
    """Manages the lifecycle of the C# COM bridge subprocess."""

    def __init__(self, host: str = "127.0.0.1", port: int = 50055) -> None:
        self._host = host
        self._port = port
        self._proc: subprocess.Popen | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        # Set when the bridge crashed mid-session and could not be restarted
        # after retrying — main.py checks this to refuse hardware commands
        # instead of failing cryptically against a dead gRPC endpoint.
        self.degraded = False

    # ── public API ─────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Build (if needed) and start the bridge. Returns True when ready."""
        if self._probe():
            logger.warning("Bridge already running on %s:%d; skipping start", self._host, self._port)
            return True
        if not _BRIDGE_EXE.exists():
            logger.info("Bridge exe not found — building with dotnet...")
            if not self._build():
                return False

        logger.info("Starting C# bridge: %s", _BRIDGE_EXE)
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            self._proc = subprocess.Popen(
                [str(_BRIDGE_EXE)],
                cwd=str(_BRIDGE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                creationflags=flags,
            )
            self._start_log_pumps()
        except Exception as exc:
            logger.error("Failed to start bridge: %s", exc)
            return False

        # Wait up to 15 s for the gRPC port to open
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                logger.error("Bridge exited immediately (code %d)", self._proc.returncode)
                return False
            if self._probe():
                logger.info("Bridge ready on %s:%d (PID %d)", self._host, self._port, self._proc.pid)
                self.degraded = False
                return True
            time.sleep(0.5)

        logger.error("Bridge did not become ready within 15 s")
        self.stop()
        return False

    def stop(self) -> None:
        self.stop_health_monitor()
        self._terminate_process()

    def _terminate_process(self) -> None:
        if self._proc and self._proc.poll() is None:
            logger.info("Stopping bridge (PID %d)", self._proc.pid)
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ── health monitoring ─────────────────────────────────────────────────

    def start_health_monitor(self, interval: float = 5.0, max_restart_attempts: int = 3) -> None:
        """Watch the bridge process after startup and restart it on crash.

        `start()` only guarantees the bridge was alive *at startup*; without
        this, a mid-session crash leaves PlateReaderImpl silently calling a
        dead gRPC endpoint until the next command fails.
        """
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            args=(interval, max_restart_attempts),
            name="tecan-bridge-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def stop_health_monitor(self) -> None:
        self._monitor_stop.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2)

    def _monitor_loop(self, interval: float, max_restart_attempts: int) -> None:
        while not self._monitor_stop.wait(interval):
            if self._proc is None:
                continue  # never started — nothing to do

            if self._proc.poll() is None:
                # Process is alive, but that alone doesn't mean it's responsive
                # (e.g. a hung-but-alive gRPC server) — also check the port.
                if self._probe():
                    continue
                logger.error("Bridge process alive but not responding on %s:%d — restarting", self._host, self._port)
                # Not self.stop(): that also calls stop_health_monitor(), which
                # would set self._monitor_stop and make the restart loop below
                # (running in this same monitor thread/iteration) immediately
                # bail out via its own is_set() check before ever restarting.
                self._terminate_process()
            else:
                logger.error("Bridge process died unexpectedly (exit code %s)", self._proc.returncode)
            for attempt in range(1, max_restart_attempts + 1):
                if self._monitor_stop.is_set():
                    return
                logger.info("Attempting bridge restart %d/%d...", attempt, max_restart_attempts)
                if self.start():
                    logger.info("Bridge restarted successfully")
                    break
                backoff = min(2 ** attempt, 30)
                logger.warning("Bridge restart attempt %d failed, retrying in %ds", attempt, backoff)
                if self._monitor_stop.wait(backoff):
                    return
            else:
                logger.error(
                    "Bridge failed to restart after %d attempts — marking degraded",
                    max_restart_attempts,
                )
                self.degraded = True

    # ── logging helpers ─────────────────────────────────────────────────

    def _start_log_pumps(self) -> None:
        if not self._proc:
            return
        if self._proc.stdout:
            self._stdout_thread = threading.Thread(
                target=self._pump_stream,
                args=(self._proc.stdout, logger.info),
                name="tecan-bridge-stdout",
                daemon=True,
            )
            self._stdout_thread.start()
        if self._proc.stderr:
            self._stderr_thread = threading.Thread(
                target=self._pump_stream,
                args=(self._proc.stderr, logger.error),
                name="tecan-bridge-stderr",
                daemon=True,
            )
            self._stderr_thread.start()

    @staticmethod
    def _pump_stream(stream, log_func) -> None:
        for line in iter(stream.readline, ""):
            msg = line.strip()
            if msg:
                log_func("[bridge] %s", msg)

    # ── internals ──────────────────────────────────────────────────────────

    def _probe(self) -> bool:
        """Return True if the bridge gRPC port is accepting connections."""
        try:
            ch = grpc.insecure_channel(f"{self._host}:{self._port}")
            grpc.channel_ready_future(ch).result(timeout=0.5)
            ch.close()
            return True
        except Exception:
            return False

    def _build(self) -> bool:
        config = "Release" if _BRIDGE_EXE == _BRIDGE_EXE_RELEASE else "Debug"
        logger.info("Building bridge with 'dotnet build -c %s' in %s ...", config, _BRIDGE_DIR)
        result = subprocess.run(
            ["dotnet", "build", "-c", config],
            cwd=str(_BRIDGE_DIR),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error("dotnet build failed:\n%s", result.stderr)
            return False
        logger.info("Bridge built OK")
        return True
