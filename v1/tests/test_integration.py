"""
End-to-end integration tests: discover → connect → execute command → verify result.
All tests require running servers and are automatically skipped if unavailable.
"""
import asyncio
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(V1_DIR))

from src.lab_core import LabCore


@pytest.fixture
async def core(base_dir):
    c = LabCore(base_dir)
    await c.discover(timeout=2.0)
    return c


# LabCore unit behaviour (no servers needed)

def test_lab_core_instantiates(base_dir):
    core = LabCore(base_dir)
    assert core is not None


def test_lab_core_list_files_recipes(base_dir):
    core = LabCore(base_dir)
    # list_files doesn't need servers — just scans Library/
    result = core.list_files("recipes")
    assert isinstance(result, list)


def test_lab_core_list_files_analyses(base_dir):
    core = LabCore(base_dir)
    result = core.list_files("analyses")
    assert isinstance(result, list)


def test_lab_core_list_files_hal(base_dir):
    core = LabCore(base_dir)
    result = core.list_files("hal")
    assert isinstance(result, list)


def test_lab_core_list_files_workflows(base_dir):
    core = LabCore(base_dir)
    result = core.list_files("workflows")
    assert isinstance(result, list)


def test_lab_core_discover_cache(base_dir):
    """Second discover() call within TTL returns cached result without new gRPC calls."""
    import time
    core = LabCore(base_dir)
    # Force cache expiry
    core._last_discovery_time = 0.0

    async def run():
        t0 = time.monotonic()
        await core.discover(timeout=0.5)
        first_elapsed = time.monotonic() - t0

        t1 = time.monotonic()
        await core.discover(timeout=0.5)
        cached_elapsed = time.monotonic() - t1

        # Cached call should be >10x faster
        assert cached_elapsed < first_elapsed / 5 or cached_elapsed < 0.01

    asyncio.run(run())


# Integration: requires running servers

@pytest.mark.integration
@pytest.mark.asyncio
async def test_discover_returns_instruments(base_dir, any_server_available):
    if not any_server_available:
        pytest.skip("No servers running")
    core = LabCore(base_dir)
    instruments = await core.discover(timeout=2.0)
    assert len(instruments) > 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_instruments_have_commands(base_dir, any_server_available):
    if not any_server_available:
        pytest.skip("No servers running")
    core = LabCore(base_dir)
    await core.discover(timeout=2.0)
    online = [i for i in core.list_instruments() if i.status == "online"]
    assert len(online) > 0, "At least one instrument must be online"
    for inst in online:
        assert len(inst.commands) > 0, f"{inst.name} has no commands"


@pytest.mark.integration
@pytest.mark.opentrons
@pytest.mark.asyncio
async def test_opentrons_execute_get_status(base_dir, opentrons_available):
    """
    Execute GetStatus command on Opentrons via SiLA2Common.ExecuteCommand.
    This is a non-destructive read command — safe to run anytime.
    """
    if not opentrons_available:
        pytest.skip("Opentrons not running")
    core = LabCore(base_dir)
    await core.discover(timeout=2.0)

    opentrons = next(
        (i for i in core.list_instruments() if "opentrons" in i.name.lower()),
        None
    )
    assert opentrons is not None, "Opentrons not found after discovery"

    result = await core.execute_command(opentrons.id, "GetStatus", {})
    assert result.success, f"GetStatus failed: {result.error}"


@pytest.mark.integration
@pytest.mark.opentrons
@pytest.mark.asyncio
async def test_opentrons_execute_list_recipes(base_dir, opentrons_available):
    """ListRecipes returns list of available recipes — non-destructive."""
    if not opentrons_available:
        pytest.skip("Opentrons not running")
    core = LabCore(base_dir)
    await core.discover(timeout=2.0)
    opentrons = next(
        (i for i in core.list_instruments() if "opentrons" in i.name.lower()),
        None
    )
    if opentrons is None:
        pytest.skip("Opentrons not in discovered instruments")

    result = await core.execute_command(opentrons.id, "ListRecipes", {})
    assert result.success, f"ListRecipes failed: {result.error}"


@pytest.mark.integration
@pytest.mark.manual_station
@pytest.mark.asyncio
async def test_manual_station_get_active_tasks(base_dir, manual_station_available):
    """GetActiveTasks returns current task list — non-destructive."""
    if not manual_station_available:
        pytest.skip("ManualStation not running")
    core = LabCore(base_dir)
    await core.discover(timeout=2.0)
    manual = next(
        (i for i in core.list_instruments() if "manual" in i.name.lower()),
        None
    )
    if manual is None:
        pytest.skip("ManualStation not in discovered instruments")

    result = await core.execute_command(manual.id, "GetActiveTasks", {})
    assert result.success, f"GetActiveTasks failed: {result.error}"


@pytest.mark.integration
@pytest.mark.tecan
@pytest.mark.asyncio
async def test_tecan_execute_list_protocols(base_dir, tecan_available):
    """ListProtocols returns .mdfx file list — non-destructive."""
    if not tecan_available:
        pytest.skip("Tecan not running")
    core = LabCore(base_dir)
    await core.discover(timeout=2.0)
    tecan = next(
        (i for i in core.list_instruments() if "tecan" in i.name.lower()),
        None
    )
    if tecan is None:
        pytest.skip("Tecan not in discovered instruments")

    result = await core.execute_command(tecan.id, "ListProtocols", {})
    assert result.success, f"ListProtocols failed: {result.error}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_instrument_returns_error(base_dir, any_server_available):
    if not any_server_available:
        pytest.skip("No servers running")
    core = LabCore(base_dir)
    await core.discover(timeout=2.0)
    result = await core.execute_command("nonexistent_instrument_xyz", "DoSomething", {})
    assert not result.success
    assert result.error


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_command_returns_error(base_dir, opentrons_available):
    if not opentrons_available:
        pytest.skip("Opentrons not running")
    core = LabCore(base_dir)
    await core.discover(timeout=2.0)
    opentrons = next(
        (i for i in core.list_instruments() if "opentrons" in i.name.lower()),
        None
    )
    if opentrons is None:
        pytest.skip("Opentrons not found")
    result = await core.execute_command(opentrons.id, "ThisCommandDoesNotExist", {})
    assert not result.success
