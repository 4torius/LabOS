#!/usr/bin/env python3
"""
Mobile Robot SiLA2 Server — Main Entry Point (sila2 standard library)
======================================================================

SiLA2-compliant server for the GoFaGo mobile manipulator.
Runs on the Ubuntu robot PC and bridges SiLA2 <-> ROS1.

Uses the official sila2 Python library (v0.14+) instead of raw gRPC.

Usage:
    python3 main.py                    # simulation mode (default)
    python3 main.py --production       # real ROS
    python3 main.py --port 50053       # override port
    python3 main.py --secure           # TLS (self-signed cert)
    python3 main.py --no-discovery     # disable mDNS
"""

import argparse
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path
from uuid import UUID, uuid4

# Add server root to sys.path so that "generated.*" and "src.*" resolve correctly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml
from sila2.server import SilaServer

from generated.taskmanagement.taskmanagement_feature import TaskManagementFeature
from ros_task_bridge import ROSTaskBridge
from generated.laboslibraryprovider import LabOSLibraryProviderFeature
from src.library_provider_impl import LibraryProviderImpl
from src.task_management_impl import TaskManagementImpl


class _SuppressSubscriptionManagerLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("SubscriptionManagerThread")


def _load_config(path: str) -> dict:
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
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
        description="MobileSiLA2Server — SiLA2 standard library server"
    )
    parser.add_argument("--config", "-c", default="config.yaml")
    parser.add_argument("--port", "-p", type=int, default=None)
    parser.add_argument(
        "--simulate",
        action="store_true",
        default=False,
        help="Run in simulation mode (no ROS required)",
    )
    parser.add_argument(
        "--production",
        action="store_true",
        default=False,
        help="Run in production mode (connects to real ROS)",
    )
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

    # Default to simulation unless --production is explicitly passed
    simulate = not args.production

    cfg = _load_config(args.config)

    log_level = cfg.get("logging", {}).get("level", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    for handler in logging.root.handlers:
        handler.addFilter(_SuppressSubscriptionManagerLogs())
    logging.getLogger("SiLAService").setLevel(logging.WARNING)

    server_cfg = cfg.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = args.port or server_cfg.get("port", 50053)
    server_name = server_cfg.get("name", "GoFaGo Mobile Robot")

    ros_cfg = cfg.get("ros", {})
    ros_master_uri = ros_cfg.get("master_uri", "http://localhost:11311")
    ros_namespace = ros_cfg.get("namespace", "/setup1")
    task_action_cfg = ros_cfg.get("task_action", {})
    tasks_path = task_action_cfg.get("tasks_path", "")
    task_timeout = float(task_action_cfg.get("timeout", 300.0))

    # ── Build ROS bridge ──────────────────────────────────────────────────────
    bridge = ROSTaskBridge(
        simulate=simulate,
        ros_master_uri=ros_master_uri,
        namespace=ros_namespace,
        tasks_path=tasks_path,
    )

    # ── Build SiLA2 server ────────────────────────────────────────────────────
    sila_server = SilaServer(
        server_name=server_name,
        server_type="MobileManipulator",
        server_description="GoFaGo Mobile Manipulator — task-based ROS bridge",
        server_version="1.0",
        server_vendor_url="https://bicocca.lab",
        server_uuid=_get_persistent_uuid(os.path.dirname(os.path.abspath(__file__))),
    )

    impl = TaskManagementImpl(sila_server, bridge=bridge, task_timeout=task_timeout)
    sila_server.set_feature_implementation(TaskManagementFeature, impl)
    sila_server.set_feature_implementation(LabOSLibraryProviderFeature, LibraryProviderImpl(sila_server))

    # ── Start ─────────────────────────────────────────────────────────────────
    enable_discovery = not args.no_discovery

    if args.secure:
        sila_server.start(host, port, enable_discovery=enable_discovery)
        if sila_server.generated_ca:
            ca_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_ca.pem")
            with open(ca_path, "wb") as fp:
                fp.write(sila_server.generated_ca)
            print(f"  CA cert : {ca_path}")
    else:
        sila_server.start_insecure(host, port, enable_discovery=enable_discovery)

    print()
    print("=" * 60)
    print("  MOBILE ROBOT - SiLA2 Server (standard library)")
    print("=" * 60)
    print(f"  gRPC    : {host}:{port}")
    print(f"  Mode    : {'SIMULATION' if simulate else 'PRODUCTION'}")
    print(f"  TLS     : {'enabled' if args.secure else 'disabled'}")
    print(f"  mDNS    : {'enabled' if enable_discovery else 'disabled'}")
    print(f"  ROS URI : {ros_master_uri}")
    print(f"  NS      : {ros_namespace}")
    print()
    print("  Waiting for SiLA2 connections...")
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
