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
import time
from pathlib import Path

import grpc

logger = logging.getLogger(__name__)

_BRIDGE_DIR = Path(__file__).parent.parent / "bridge"
_BRIDGE_EXE = _BRIDGE_DIR / "bin" / "Debug" / "net48" / "TecanSiLA2Server.exe"


class BridgeLauncher:
    """Manages the lifecycle of the C# COM bridge subprocess."""

    def __init__(self, host: str = "127.0.0.1", port: int = 50055) -> None:
        self._host = host
        self._port = port
        self._proc: subprocess.Popen | None = None

    # ── public API ─────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Build (if needed) and start the bridge. Returns True when ready."""
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
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
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
                return True
            time.sleep(0.5)

        logger.error("Bridge did not become ready within 15 s")
        return False

    def stop(self) -> None:
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
        logger.info("Building bridge with 'dotnet build' in %s ...", _BRIDGE_DIR)
        result = subprocess.run(
            ["dotnet", "build", "-c", "Debug"],
            cwd=str(_BRIDGE_DIR),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error("dotnet build failed:\n%s", result.stderr)
            return False
        logger.info("Bridge built OK")
        return True
