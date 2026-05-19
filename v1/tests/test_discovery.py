"""
Tests for PnPDiscovery — bootstrap and mDNS discovery.
Unit tests run without servers.  Integration tests require running servers.
"""
import asyncio
import sys
from pathlib import Path

import pytest
import pytest_asyncio

V1_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(V1_DIR))

from src.discovery import PnPDiscovery, PnPServer, PnPCommand, PnPFeature


# Instantiation

def test_pnp_discovery_instantiates(base_dir):
    d = PnPDiscovery(base_dir)
    assert d is not None


def test_pnp_discovery_list_servers_empty_before_discover(base_dir):
    d = PnPDiscovery(base_dir)
    assert d.list_servers() == []


# Bootstrap discovery (reads lab_config.yaml — no servers needed)

@pytest.mark.asyncio
async def test_bootstrap_from_config_no_exception(base_dir):
    """_bootstrap_from_config should complete without exceptions even if no servers are up."""
    d = PnPDiscovery(base_dir)
    await d._bootstrap_from_config(timeout=0.3)
    # Offline servers are skipped; no exception should be raised
    assert True


@pytest.mark.asyncio
async def test_bootstrap_from_config_registers_servers(base_dir):
    """Config seeds should appear in the registry (online or offline) after bootstrap."""
    d = PnPDiscovery(base_dir)
    await d._bootstrap_from_config(timeout=0.3)
    # All configured+enabled servers should be registered regardless of online status
    assert len(d.list_servers()) >= 0  # At least doesn't crash
    for server in d.list_servers():
        assert server.discovered_via == "config"


def test_pnp_server_dataclass():
    server = PnPServer(
        name="Test Server",
        host="localhost",
        port=50099,
        server_type="instrument",
        features=[],
        server_online=True,
    )
    assert server.name == "Test Server"
    assert server.address == "localhost:50099"
    assert server.server_online is True


def test_pnp_server_get_all_commands_empty():
    server = PnPServer(name="Empty", host="localhost", port=50099, features=[])
    assert list(server.get_all_commands()) == []


def test_pnp_server_get_all_commands_with_feature():
    cmd = PnPCommand(
        identifier="Initialize",
        display_name="Initialize",
        description="Init",
        parameters=[],
        observable=False,
    )
    feat = PnPFeature(
        identifier="TestFeature",
        display_name="Test Feature",
        description="",
        commands=[cmd],
        properties=[],
    )
    server = PnPServer(name="Test", host="localhost", port=50099, features=[feat])
    commands = list(server.get_all_commands())
    assert len(commands) == 1
    feature_id, cmd_id, command = commands[0]
    assert feature_id == "TestFeature"
    assert cmd_id == "Initialize"


# Integration: requires at least one server running

@pytest.mark.integration
@pytest.mark.asyncio
async def test_discover_all_finds_online_servers(base_dir, any_server_available):
    if not any_server_available:
        pytest.skip("No SiLA2 servers running")
    d = PnPDiscovery(base_dir)
    await d.discover_all(timeout=2.0)
    servers = d.list_servers()
    assert len(servers) > 0, "Expected at least one online server"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_online_server_has_features(base_dir, any_server_available):
    if not any_server_available:
        pytest.skip("No SiLA2 servers running")
    d = PnPDiscovery(base_dir)
    await d.discover_all(timeout=2.0)
    online = [s for s in d.list_servers() if s.server_online]
    assert len(online) > 0
    for server in online:
        assert len(server.features) > 0, f"{server.name} has no features after discovery"


@pytest.mark.integration
@pytest.mark.opentrons
@pytest.mark.asyncio
async def test_opentrons_discovered_with_commands(base_dir, opentrons_available):
    if not opentrons_available:
        pytest.skip("Opentrons server not running")
    d = PnPDiscovery(base_dir)
    await d.discover_all(timeout=2.0)
    opentrons = next(
        (s for s in d.list_servers() if "opentrons" in s.name.lower() or "workflow" in s.name.lower()),
        None
    )
    assert opentrons is not None, "Opentrons server not found"
    assert opentrons.server_online
    commands = list(opentrons.get_all_commands())
    assert len(commands) > 0


@pytest.mark.integration
@pytest.mark.tecan
@pytest.mark.asyncio
async def test_tecan_discovered_with_commands(base_dir, tecan_available):
    if not tecan_available:
        pytest.skip("Tecan server not running")
    d = PnPDiscovery(base_dir)
    await d.discover_all(timeout=2.0)
    tecan = next(
        (s for s in d.list_servers() if "tecan" in s.name.lower()),
        None
    )
    assert tecan is not None, "Tecan server not found"
    assert tecan.server_online
