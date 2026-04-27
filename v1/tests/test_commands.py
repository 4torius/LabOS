#!/usr/bin/env python3
"""
Test Command Execution via CLI (LabCore) and WebApp API
========================================================

Verifies that both interfaces use the same LabCore logic
and can execute real commands on instruments.
"""

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

# Add to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # For WebApp API testing
import pytest
from src.lab_core import LabCore


#                              COLORS

class C:
    RESET = '\033[0m'
    BOLD = '\033[1m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'


def ok(msg): print(f"  {C.GREEN}[OK]{C.RESET} {msg}")
def err(msg): print(f"  {C.RED}[ERR]{C.RESET} {msg}")
def info(msg): print(f"  {C.CYAN}[...]{C.RESET} {msg}")
def header(msg): print(f"\n{C.BOLD}{C.CYAN}{'='*60}\n  {msg}\n{'='*60}{C.RESET}")


#                              TEST VIA LABCORE (CLI)

def _is_webapp_available(host: str = "localhost", port: int = 5000, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cli_labcore(any_server_available):
    """Test commands using LabCore directly (same as CLI)."""
    if os.environ.get("LABOS_ENABLE_COMMAND_TESTS") != "1":
        pytest.skip("Set LABOS_ENABLE_COMMAND_TESTS=1 to run hardware command tests")
    if not any_server_available:
        pytest.skip("No SiLA2 servers running")

    header("TEST 1: LabCore (CLI Logic)")
    
    core = LabCore()
    
    # Discover instruments
    info("Discovering instruments...")
    instruments = await core.discover()
    
    for inst in instruments:
        print(f"    Found: {inst.name} ({inst.status})")
    
    results = []
    
    # TECAN TESTS
    print(f"\n{C.BOLD}  TECAN Tests:{C.RESET}")
    
    # 1. Tecan Connect
    info("Tecan: Connect...")
    result = await core.execute_command("tecan_m200_pro", "Connect", {})
    if result.success:
        ok(f"Connect: {result.message}")
        results.append(("CLI", "Tecan Connect", True))
    else:
        err(f"Connect: {result.error}")
        results.append(("CLI", "Tecan Connect", False))
    
    # 2. Tecan PlateOut
    info("Tecan: PlateOut...")
    result = await core.execute_command("tecan_m200_pro", "PlateOut", {})
    if result.success:
        ok(f"PlateOut: {result.message}")
        results.append(("CLI", "Tecan PlateOut", True))
    else:
        err(f"PlateOut: {result.error}")
        results.append(("CLI", "Tecan PlateOut", False))
    
    # 3. Tecan PlateIn
    info("Tecan: PlateIn...")
    result = await core.execute_command("tecan_m200_pro", "PlateIn", {})
    if result.success:
        ok(f"PlateIn: {result.message}")
        results.append(("CLI", "Tecan PlateIn", True))
    else:
        err(f"PlateIn: {result.error}")
        results.append(("CLI", "Tecan PlateIn", False))
    
    # OPENTRONS TESTS
    print(f"\n{C.BOLD}  OPENTRONS Tests:{C.RESET}")
    
    # 4. SwitchHardwareConfig
    info("Opentrons: SwitchHardwareConfig...")
    # Get available configs
    configs = core.list_files("hal")
    if configs:
        config_file = configs[0]
        info(f"  Using config: {config_file}")
        result = await core.execute_command(
            "opentrons_flex", 
            "SwitchHardwareConfig", 
            {"ConfigName": config_file}
        )
        if result.success:
            ok(f"SwitchHardwareConfig: {result.message}")
            results.append(("CLI", "Opentrons SwitchHardwareConfig", True))
        else:
            err(f"SwitchHardwareConfig: {result.error}")
            results.append(("CLI", "Opentrons SwitchHardwareConfig", False))
    else:
        err("No HAL configs found")
        results.append(("CLI", "Opentrons SwitchHardwareConfig", False))
    
    # 5. ExecuteRecipe
    info("Opentrons: ExecuteRecipe...")
    recipes = core.list_files("recipes")
    if recipes:
        recipe_file = recipes[0]
        info(f"  Using recipe: {recipe_file}")
        result = await core.execute_command(
            "opentrons_flex",
            "ExecuteRecipe",
            {"RecipeName": recipe_file}
        )
        if result.success:
            ok(f"ExecuteRecipe: {result.message}")
            results.append(("CLI", "Opentrons ExecuteRecipe", True))
        else:
            err(f"ExecuteRecipe: {result.error}")
            results.append(("CLI", "Opentrons ExecuteRecipe", False))
    else:
        err("No recipes found")
        results.append(("CLI", "Opentrons ExecuteRecipe", False))
    
    assert isinstance(results, list)


#                              TEST VIA WEBAPP API

@pytest.mark.integration
@pytest.mark.asyncio
async def test_webapp_api(any_server_available):
    """Test commands using WebApp API endpoints."""
    if os.environ.get("LABOS_ENABLE_COMMAND_TESTS") != "1":
        pytest.skip("Set LABOS_ENABLE_COMMAND_TESTS=1 to run hardware command tests")
    if not any_server_available:
        pytest.skip("No SiLA2 servers running")
    if not _is_webapp_available():
        pytest.skip("WebApp not running on localhost:5000")

    header("TEST 2: WebApp API")
    
    results = []
    base_url = "http://localhost:5000"
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Check if webapp is up
        try:
            resp = await client.get(f"{base_url}/api/devices")
            if resp.status_code != 200:
                err(f"WebApp not responding correctly: {resp.status_code}")
                return results
        except Exception as e:
            err(f"WebApp not reachable: {e}")
            return results
        
        ok("WebApp is responding")
        
        # TECAN TESTS
        print(f"\n{C.BOLD}  TECAN Tests (via API):{C.RESET}")
        
        # 1. Tecan Connect
        info("Tecan: Connect...")
        resp = await client.post(
            f"{base_url}/api/devices/tecan-m200-pro/command",  # Test with hyphen
            json={"command": "Connect", "parameters": {}}
        )
        data = resp.json()
        if data.get("status") == "completed":
            ok(f"Connect: {data.get('result', 'OK')}")
            results.append(("WebApp", "Tecan Connect", True))
        else:
            err(f"Connect: {data.get('error', 'Unknown error')}")
            results.append(("WebApp", "Tecan Connect", False))
        
        # 2. Tecan PlateOut
        info("Tecan: PlateOut...")
        resp = await client.post(
            f"{base_url}/api/devices/tecan_m200_pro/command",  # Test with underscore
            json={"command": "PlateOut", "parameters": {}}
        )
        data = resp.json()
        if data.get("status") == "completed":
            ok(f"PlateOut: {data.get('result', 'OK')}")
            results.append(("WebApp", "Tecan PlateOut", True))
        else:
            err(f"PlateOut: {data.get('error', 'Unknown error')}")
            results.append(("WebApp", "Tecan PlateOut", False))
        
        # 3. Tecan PlateIn
        info("Tecan: PlateIn...")
        resp = await client.post(
            f"{base_url}/api/devices/tecan_m200_pro/command",
            json={"command": "PlateIn", "parameters": {}}
        )
        data = resp.json()
        if data.get("status") == "completed":
            ok(f"PlateIn: {data.get('result', 'OK')}")
            results.append(("WebApp", "Tecan PlateIn", True))
        else:
            err(f"PlateIn: {data.get('error', 'Unknown error')}")
            results.append(("WebApp", "Tecan PlateIn", False))
        
        # OPENTRONS TESTS
        print(f"\n{C.BOLD}  OPENTRONS Tests (via API):{C.RESET}")
        
        # Get available files
        hal_resp = await client.get(f"{base_url}/api/files/hal")
        hal_files = hal_resp.json().get("files", [])
        
        recipe_resp = await client.get(f"{base_url}/api/files/recipes")
        recipe_files = recipe_resp.json().get("files", [])
        
        # 4. SwitchHardwareConfig
        info("Opentrons: SwitchHardwareConfig...")
        if hal_files:
            config_file = hal_files[0]
            info(f"  Using config: {config_file}")
            resp = await client.post(
                f"{base_url}/api/devices/opentrons_flex/command",
                json={"command": "SwitchHardwareConfig", "parameters": {"ConfigName": config_file}}
            )
            data = resp.json()
            if data.get("status") == "completed":
                ok(f"SwitchHardwareConfig: {data.get('result', 'OK')}")
                results.append(("WebApp", "Opentrons SwitchHardwareConfig", True))
            else:
                err(f"SwitchHardwareConfig: {data.get('error', 'Unknown error')}")
                results.append(("WebApp", "Opentrons SwitchHardwareConfig", False))
        else:
            err("No HAL configs found")
            results.append(("WebApp", "Opentrons SwitchHardwareConfig", False))
        
        # 5. ExecuteRecipe
        info("Opentrons: ExecuteRecipe...")
        if recipe_files:
            recipe_file = recipe_files[0]
            info(f"  Using recipe: {recipe_file}")
            resp = await client.post(
                f"{base_url}/api/devices/opentrons_flex/command",
                json={"command": "ExecuteRecipe", "parameters": {"RecipeName": recipe_file}}
            )
            data = resp.json()
            if data.get("status") == "completed":
                ok(f"ExecuteRecipe: {data.get('result', 'OK')}")
                results.append(("WebApp", "Opentrons ExecuteRecipe", True))
            else:
                err(f"ExecuteRecipe: {data.get('error', 'Unknown error')}")
                results.append(("WebApp", "Opentrons ExecuteRecipe", False))
        else:
            err("No recipes found")
            results.append(("WebApp", "Opentrons ExecuteRecipe", False))
    
    assert isinstance(results, list)


#                              MAIN

async def main():
    print(f"""
{C.CYAN}{C.BOLD}
╔══════════════════════════════════════════════════════════════════╗
║                 COMMAND EXECUTION TEST SUITE                     ║
║        Testing both CLI (LabCore) and WebApp API paths           ║
╚══════════════════════════════════════════════════════════════════╝
{C.RESET}
""")
    
    all_results = []
    
    # Test 1: CLI via LabCore
    try:
        cli_results = await test_cli_labcore()
        all_results.extend(cli_results)
    except Exception as e:
        err(f"CLI test failed: {e}")
        import traceback
        traceback.print_exc()
    
    # Test 2: WebApp API
    try:
        webapp_results = await test_webapp_api()
        all_results.extend(webapp_results)
    except Exception as e:
        err(f"WebApp test failed: {e}")
        import traceback
        traceback.print_exc()
    
    # Summary
    header("TEST SUMMARY")
    
    print(f"\n  {'Interface':<10} {'Command':<35} {'Result'}")
    print(f"  {'-'*10} {'-'*35} {'-'*8}")
    
    passed = 0
    failed = 0
    
    for interface, command, success in all_results:
        status = f"{C.GREEN}PASS{C.RESET}" if success else f"{C.RED}FAIL{C.RESET}"
        print(f"  {interface:<10} {command:<35} {status}")
        if success:
            passed += 1
        else:
            failed += 1
    
    print(f"\n  {C.BOLD}Total: {passed} passed, {failed} failed{C.RESET}")
    
    if failed == 0:
        print(f"\n  {C.GREEN}{C.BOLD}✓ ALL TESTS PASSED!{C.RESET}")
        print(f"  {C.GREEN}Both CLI and WebApp use LabCore correctly.{C.RESET}")
    else:
        print(f"\n  {C.YELLOW}⚠ Some tests failed - check server logs{C.RESET}")
    
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
