#!/usr/bin/env python3
"""
MobileSiLA2Server - Main Entry Point (sila2 standard library)
=============================================================

SiLA2 server for the RB Kairos + ABB GoFa 15000 mobile manipulator.
Uses the official sila2 Python library (v0.14+) with ROS1 integration.
Degrades gracefully to stub mode when ROS is unavailable.

Usage:
    python main.py                     # default config.yaml, insecure
    python main.py --config other.yaml
    python main.py --port 50201        # override port
    python main.py --secure            # enable TLS (self-signed cert generated)
    python main.py --no-discovery      # disable mDNS/DNS-SD
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

from generated.mobilerobot.mobilerobot_feature import MobileRobotFeature
from src.mobile_robot_impl import MobileRobotImpl


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
    parser = argparse.ArgumentParser(
        description="MobileSiLA2Server — RB Kairos + ABB GoFa 15000"
    )
    parser.add_argument("--config", "-c", default="config.yaml")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", "-p", type=int, default=None)
    parser.add_argument(
        "--secure",
        action="store_true",
        default=False,
        help="Enable TLS (auto-generates a self-signed certificate)",
    )
    parser.add_argument(
        "--no-discovery",
        action="store_true",
        default=False,
        help="Disable mDNS/DNS-SD auto-discovery",
    )
    args = parser.parse_args()

    config = _load_config(args.config)
    srv_cfg = config.get("server", {})
    host = args.host or srv_cfg.get("host", "0.0.0.0")
    port = args.port or srv_cfg.get("port", 50201)

    log_level = config.get("logging", {}).get("level", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    for handler in logging.root.handlers:
        handler.addFilter(_SuppressSubscriptionManagerLogs())
    logging.getLogger("SiLAService").setLevel(logging.WARNING)
    logging.getLogger("sila2").setLevel(logging.WARNING)
    logging.getLogger("grpc").setLevel(logging.WARNING)

    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"), exist_ok=True)

    # ── Build the SiLA2 server ────────────────────────────────────────────────
    sila_server = SilaServer(
        server_name="GoFaGoMobileRobot",
        server_type="MobileRobot",
        server_description="RB Kairos + ABB GoFa 15000 mobile manipulator — BicoccaLab",
        server_version="1.0",
        server_vendor_url="https://bicocca.lab",
        server_uuid=_get_persistent_uuid(os.path.dirname(os.path.abspath(__file__))),
    )

    impl = MobileRobotImpl(sila_server, config)
    sila_server.set_feature_implementation(MobileRobotFeature, impl)

    # ── Start ─────────────────────────────────────────────────────────────────
    enable_discovery = not args.no_discovery

    if args.secure:
        sila_server.start(host, port, enable_discovery=enable_discovery)
        if sila_server.generated_ca:
            ca_path = "generated_ca.pem"
            with open(ca_path, "wb") as fp:
                fp.write(sila_server.generated_ca)
            print(f"  CA cert: {ca_path}")
    else:
        sila_server.start_insecure(host, port, enable_discovery=enable_discovery)

    print()
    print("=" * 60)
    print("  MOBILE ROBOT - SiLA2 Server (standard library)")
    print("=" * 60)
    print(f"  gRPC   : {host}:{port}")
    print(f"  Mode   : {'TLS' if args.secure else 'insecure'}")
    print(f"  mDNS   : {'enabled' if enable_discovery else 'disabled'}")
    ros_mode = "connected" if impl._ros.ros_available else "stub (ROS unavailable)"
    print(f"  ROS    : {ros_mode}")
    stations = list(impl._stations.keys())
    print(f"  Stations: {', '.join(stations)}")
    print()
    print("  Handles: navigation, arm control, gripper, labware transport")
    print("=" * 60)
    print()

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    signal.signal(signal.SIGTERM, lambda *_: sila_server.grpc_server.stop(0))

    try:
        with contextlib.suppress(KeyboardInterrupt):
            sila_server.grpc_server.wait_for_termination()
    finally:
        sila_server.stop()
        print("\nServer stopped.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
