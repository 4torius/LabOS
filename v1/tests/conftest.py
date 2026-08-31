"""
Shared pytest fixtures for LabOS test suite.
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Ensure v1/ is on sys.path
V1_DIR = Path(__file__).parent.parent
if str(V1_DIR) not in sys.path:
    sys.path.insert(0, str(V1_DIR))


@pytest.fixture(scope="session")
def base_dir() -> Path:
    return V1_DIR


@pytest.fixture
def lab_config(base_dir):
    import yaml
    config_path = base_dir / "lab_config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def sila2_dir(base_dir):
    return base_dir / "SiLA2"


# Skip markers

def pytest_configure(config):
    config.addinivalue_line("markers", "integration: requires running SiLA2 servers")
    config.addinivalue_line("markers", "tecan: requires Tecan server on port 50051")
    config.addinivalue_line("markers", "opentrons: requires Opentrons server on port 50302")
    config.addinivalue_line("markers", "manual_station: requires ManualStation server on port 50360")
    config.addinivalue_line("markers", "mobile: requires the remote MobileSiLA2Server reachable on the network")


def is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError):
        return False


@pytest.fixture(scope="session")
def opentrons_available():
    return is_port_open("localhost", 50302)


@pytest.fixture(scope="session")
def tecan_available():
    return is_port_open("localhost", 50051)


@pytest.fixture(scope="session")
def manual_station_available():
    return is_port_open("localhost", 50360)


@pytest.fixture(scope="session")
def mobile_available(lab_config):
    """Reachability of the remote MobileSiLA2Server (runs on a separate Ubuntu
    PC, not on this machine — see servers.mobile in lab_config.yaml)."""
    mobile_cfg = lab_config.get("servers", {}).get("mobile", {})
    host = mobile_cfg.get("host")
    port = mobile_cfg.get("port")
    if not host or not port:
        return False
    return is_port_open(host, int(port), timeout=2.0)


@pytest.fixture(scope="session")
def any_server_available(opentrons_available, tecan_available, manual_station_available):
    return opentrons_available or tecan_available or manual_station_available
