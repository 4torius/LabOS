#!/usr/bin/env python3
"""
TecanM200SiLA2Server
====================
SiLA2 server for the Tecan Infinite M200 Pro plate reader.

Architecture:
  SiLA2 clients (webapp / API)
      ↓  gRPC  (port 50051, sila2 library)
  THIS SERVER  [main.py]
      ↓  gRPC  (localhost:50055, internal)
  C# COM bridge  [bridge/TecanSiLA2Server.exe]
      ↓  COM / SDK
  Tecan M200 Pro hardware

The C# bridge lives in bridge/ and is started automatically by this process.
It runs silently (no window). Logs are the only debug interface.

Usage:
    python main.py                    # default: port 50051, config.yaml
    python main.py --port 50051
    python main.py --no-discovery     # disable mDNS
    python main.py --no-bridge        # skip bridge autostart (debug)
"""

import argparse
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path
from uuid import UUID, uuid4

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sila2.server import SilaServer

from generated.platereaderservice.platereaderservice_feature import PlateReaderServiceFeature
from src.plate_reader_impl import PlateReaderImpl
from src.bridge_launcher import BridgeLauncher


class _SuppressSubscriptionManagerLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("SubscriptionManagerThread")


def _load_config(path: str) -> dict:
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            return yaml.safe_load(f) or {}
    return {}


def _get_persistent_uuid(server_dir: str) -> UUID:
    uuid_file = Path(server_dir) / ".server_uuid"
    if uuid_file.exists():
        return UUID(uuid_file.read_text().strip())
    new_uuid = uuid4()
    uuid_file.write_text(str(new_uuid))
    return new_uuid


def main() -> int:
    parser = argparse.ArgumentParser(description="TecanM200SiLA2Server")
    parser.add_argument("--config", "-c", default="config.yaml")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", "-p", type=int, default=None)
    parser.add_argument("--no-discovery", action="store_true", default=False)
    parser.add_argument("--no-bridge", action="store_true", default=False,
                        help="Skip bridge autostart (useful if bridge is already running)")
    args = parser.parse_args()

    config = _load_config(args.config)
    srv_cfg = config.get("server", {})
    host = args.host or srv_cfg.get("host", "0.0.0.0")
    port = args.port or srv_cfg.get("port", 50051)

    log_level = config.get("logging", {}).get("level", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    for handler in logging.root.handlers:
        handler.addFilter(_SuppressSubscriptionManagerLogs())

    bridge_cfg = config.get("bridge", {})
    bridge_host = bridge_cfg.get("host", "127.0.0.1")
    bridge_port = bridge_cfg.get("port", 50055)

    # ── Auto-start C# bridge ───────────────────────────────────────────────
    launcher = BridgeLauncher(bridge_host, bridge_port)
    if not args.no_bridge:
        if not launcher.start():
            logging.error("C# bridge failed to start — commands requiring hardware will fail")
    else:
        logging.info("Bridge autostart skipped (--no-bridge)")

    # ── Build SiLA2 server ─────────────────────────────────────────────────
    sila_server = SilaServer(
        server_name="TecanM200Pro",
        server_type="PlateReader",
        server_description="Tecan Infinite M200 Pro plate reader",
        server_version="1.0",
        server_vendor_url="https://bicocca.lab",
        server_uuid=_get_persistent_uuid(os.path.dirname(os.path.abspath(__file__))),
    )

    impl = PlateReaderImpl(sila_server, config)
    sila_server.set_feature_implementation(PlateReaderServiceFeature, impl)

    # ── Start ──────────────────────────────────────────────────────────────
    enable_discovery = not args.no_discovery
    sila_server.start_insecure(host, port, enable_discovery=enable_discovery)

    print()
    print("=" * 60)
    print("  TECAN M200 PRO — SiLA2 Server")
    print("=" * 60)
    print(f"  SiLA2  : {host}:{port}")
    print(f"  Bridge : {bridge_host}:{bridge_port}  ({'running' if launcher.running else 'NOT running'})")
    print(f"  mDNS   : {'enabled' if enable_discovery else 'disabled'}")
    print()
    print("  Commands: Connect, Disconnect, PlateIn, PlateOut,")
    print("            SetTemperature, TurnOffTemperature,")
    print("            RunMeasurement, GetAnIMLResult, ListProtocols")
    print("=" * 60)
    print()

    signal.signal(signal.SIGTERM, lambda *_: sila_server.grpc_server.stop(0))

    try:
        with contextlib.suppress(KeyboardInterrupt):
            sila_server.grpc_server.wait_for_termination()
    finally:
        impl.stop()
        sila_server.stop()
        launcher.stop()
        print("\nServer stopped.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
