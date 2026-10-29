#!/usr/bin/env python3
"""
New Instrument SiLA2 Server — Template (sila2 standard library)
================================================================

Do not copy this folder by hand. From v1/:

    python labos.py new-server YourInstrument --results YourData

scaffolds, renames, generates the bindings and registers the server. Then:
1. Add your commands to the feature definition in features/ and run
   `python labos.py codegen YourInstrumentSiLA2Server`.
2. Implement every abstract method in the src/ implementation module.
3. Say what else the orchestrator should know in library_catalog.json:
   files it reads, Results/ folders it writes (created when this server
   starts), parameters a workflow may omit, how it looks in the interface.
4. Try it: `python labos.py instruments`, then `python labos.py call ...`.

The orchestrator discovers this server automatically via mDNS, and nothing in
the orchestrator or the web interface has to change.

Usage:
    python main.py                    # default config.yaml, insecure
    python main.py --port 50061
    python main.py --no-discovery     # disable mDNS (useful for debugging)
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

# Add this server's root to sys.path so "generated.*" and "src.*" are importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sila2.server import SilaServer

# ── Feature imports ────────────────────────────────────────────────────────────
# TODO: Update these after renaming your feature
from generated.newinstrument.newinstrument_feature import NewInstrumentFeature
from src.servicer import NewInstrumentImpl

# LabOSLibraryProvider — declares library file types to the orchestrator
from generated.laboslibraryprovider import LabOSLibraryProviderFeature
from src.library_provider_impl import LibraryProviderImpl


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
    # A redirected stdout on Windows is cp1252. A print of anything outside it
    # raises inside the command that printed, and the command fails - the
    # Manual Station's SendNotification did exactly that.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    parser = argparse.ArgumentParser(description="New Instrument SiLA2 Server")
    parser.add_argument("--config", "-c", default="config.yaml")
    parser.add_argument("--port", "-p", type=int, default=None)
    parser.add_argument("--no-discovery", action="store_true", default=False)
    args = parser.parse_args()

    config = _load_config(args.config)
    srv_cfg = config.get("server", {})
    host = srv_cfg.get("host", "0.0.0.0")
    port = args.port or srv_cfg.get("port", 50061)
    sila2_cfg = config.get("sila2", {})

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

    # ── Build SiLA2 server ─────────────────────────────────────────────────────
    sila_server = SilaServer(
        server_name=sila2_cfg.get("server_name", "New Instrument"),   # TODO: rename
        # SiLA2 requires CamelCase here ("BarcodeScanner"); "instrument" made
        # every server scaffolded from this template refuse to start.
        server_type=sila2_cfg.get("server_type", "Instrument"),
        server_description=sila2_cfg.get("description", ""),
        server_version="1.0",
        server_vendor_url=sila2_cfg.get("vendor_url", "https://bicocca.lab"),
        server_uuid=_get_persistent_uuid(os.path.dirname(os.path.abspath(__file__))),
    )

    # ── Feature implementations ────────────────────────────────────────────────
    # TODO: pass any config/dependencies your implementation needs
    impl = NewInstrumentImpl(sila_server, config)
    sila_server.set_feature_implementation(NewInstrumentFeature, impl)

    # LabOSLibraryProvider: reads library_catalog.json, no other configuration needed
    sila_server.set_feature_implementation(
        LabOSLibraryProviderFeature, LibraryProviderImpl(sila_server)
    )

    # ── Start ──────────────────────────────────────────────────────────────────
    enable_discovery = not args.no_discovery
    sila_server.start_insecure(host, port, enable_discovery=enable_discovery)

    print()
    print("=" * 60)
    print(f"  {sila2_cfg.get('server_name', 'New Instrument')} — SiLA2 Server")
    print("=" * 60)
    print(f"  SiLA2  : {host}:{port}")
    print(f"  mDNS   : {'enabled' if enable_discovery else 'disabled'}")
    print()

    signal.signal(signal.SIGTERM, lambda *_: sila_server.grpc_server.stop(0))

    try:
        with contextlib.suppress(KeyboardInterrupt):
            sila_server.grpc_server.wait_for_termination()
    finally:
        sila_server.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
