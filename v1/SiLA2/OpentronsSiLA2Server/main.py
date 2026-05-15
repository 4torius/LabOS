#!/usr/bin/env python3
"""
OpentronsSiLA2Server - Main Entry Point (sila2 standard library)
================================================================

Starts the SiLA2-compliant server for the Opentrons Flex liquid handler.
Uses the official sila2 Python library (v0.14+) instead of raw gRPC.

Usage:
    python main.py                       # default config.yaml, insecure
    python main.py --config other.yaml
    python main.py --port 50058          # override port
    python main.py --secure              # enable TLS (self-signed cert generated)
"""

import argparse
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path
from uuid import UUID, uuid4

# Add server root to sys.path so that absolute imports work for both
# "generated.*" and "src.*" packages.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sila2.server import SilaServer

from generated.workflowapi import WorkflowAPIFeature


def _get_persistent_uuid(server_dir: str) -> UUID:
    uuid_file = Path(server_dir) / ".server_uuid"
    if uuid_file.exists():
        return UUID(uuid_file.read_text().strip())
    new_uuid = uuid4()
    uuid_file.write_text(str(new_uuid))
    return new_uuid
from src.config import ServerConfig
from src.workflow_api_impl import WorkflowAPIImpl


class _SuppressSubscriptionManagerLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("SubscriptionManagerThread")


def _print_banner(config: ServerConfig) -> None:
    print()
    print("=" * 60)
    print("  OPENTRONS SiLA2 SERVER  -  sila2 standard library")
    print("=" * 60)
    print(f"  Server : {config.host}:{config.port}")
    print(f"  Robot  : {config.robot_ip}:{config.robot_port}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="OpentronsSiLA2Server — SiLA2 standard library server"
    )
    parser.add_argument("--config", "-c", default="config.yaml")
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

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    for handler in logging.root.handlers:
        handler.addFilter(_SuppressSubscriptionManagerLogs())

    config = ServerConfig(args.config)
    is_valid, error = config.validate()
    if not is_valid:
        print(f"Configuration error: {error}")
        return 1

    if args.port:
        config.port = args.port

    _print_banner(config)

    # ── Build the SiLA2 standard server ──────────────────────────────────────
    sila_server = SilaServer(
        server_name="OpentronsFlex",
        server_type="LiquidHandler",
        server_description="Opentrons Flex Liquid Handler — BicoccaLab",
        server_version="1.0",
        server_vendor_url="https://bicocca.lab",
        server_uuid=_get_persistent_uuid(os.path.dirname(os.path.abspath(__file__))),
    )

    impl = WorkflowAPIImpl(sila_server, config)
    sila_server.set_feature_implementation(WorkflowAPIFeature, impl)

    # ── Start ─────────────────────────────────────────────────────────────────
    enable_discovery = not args.no_discovery

    if args.secure:
        sila_server.start(config.host, config.port, enable_discovery=enable_discovery)
        if sila_server.generated_ca:
            ca_path = os.path.join(os.path.dirname(args.config), "generated_ca.pem")
            with open(ca_path, "wb") as fp:
                fp.write(sila_server.generated_ca)
            print(f"  CA cert: {ca_path}")
    else:
        sila_server.start_insecure(config.host, config.port, enable_discovery=enable_discovery)

    print(f"  Mode   : {'TLS' if args.secure else 'insecure (lab network)'}")
    print(f"  mDNS   : {'enabled' if enable_discovery else 'disabled'}")
    print(f"  Status : Running - waiting for connections...")
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
