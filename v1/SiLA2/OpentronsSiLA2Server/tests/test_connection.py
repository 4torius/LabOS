"""
Quick Connection Test - Test robot connectivity
==============================================

Quick test to verify connection to Opentrons Flex robot.
"""

import asyncio
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import httpx


# ═══════════════════════════════════════════════════════════════════════════
#                              COLORS
# ═══════════════════════════════════════════════════════════════════════════

class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    CYAN = '\033[96m'
    YELLOW = '\033[93m'
    BOLD = '\033[1m'
    END = '\033[0m'


def ok(msg: str):
    print(f"{Colors.GREEN}✓ {msg}{Colors.END}")


def fail(msg: str):
    print(f"{Colors.RED}✗ {msg}{Colors.END}")


def info(msg: str):
    print(f"{Colors.CYAN}ℹ {msg}{Colors.END}")


def warn(msg: str):
    print(f"{Colors.YELLOW}⚠ {msg}{Colors.END}")


# ═══════════════════════════════════════════════════════════════════════════
#                              TESTS
# ═══════════════════════════════════════════════════════════════════════════

async def test_robot_connection(
    host: str = "169.254.161.83",
    port: int = 31950,
    local_address: str = "169.254.161.1"
):
    """Test connection to Opentrons Flex robot."""
    
    print(f"\n{Colors.BOLD}{'='*55}{Colors.END}")
    print(f"{Colors.BOLD}  OPENTRONS FLEX - CONNECTION TEST{Colors.END}")
    print(f"{Colors.BOLD}{'='*55}{Colors.END}\n")
    
    info(f"Target: http://{host}:{port}")
    info(f"Local Address: {local_address}")
    print()
    
    # Test 1: HTTP Connection
    print(f"{Colors.BOLD}[1] HTTP Connection{Colors.END}")
    try:
        transport = httpx.AsyncHTTPTransport(local_address=local_address)
        async with httpx.AsyncClient(
            base_url=f"http://{host}:{port}",
            timeout=10.0,
            headers={"Opentrons-Version": "*"},
            transport=transport
        ) as client:
            response = await client.get("/health")
            
            if response.status_code == 200:
                ok("Connection successful!")
                health = response.json()
                print(f"    Robot Name:  {health.get('name', 'N/A')}")
                print(f"    Model:       {health.get('robot_model', 'N/A')}")
                print(f"    Serial:      {health.get('robot_serial', 'N/A')}")
                print(f"    API Version: {health.get('api_version', 'N/A')}")
                print(f"    FW Version:  {health.get('fw_version', 'N/A')}")
            else:
                fail(f"HTTP {response.status_code}: {response.text}")
                return False
                
            # Test 2: Pipettes
            print(f"\n{Colors.BOLD}[2] Instruments{Colors.END}")
            response = await client.get("/instruments")
            if response.status_code == 200:
                instruments = response.json().get("data", [])
                pipettes = [i for i in instruments if i.get("instrumentType") == "pipette"]
                grippers = [i for i in instruments if i.get("instrumentType") == "gripper"]
                
                if pipettes:
                    ok(f"Found {len(pipettes)} pipette(s)")
                    for p in pipettes:
                        mount = p.get("mount", "?").upper()
                        name = p.get("instrumentName", "Unknown")
                        print(f"    {mount}: {name}")
                else:
                    warn("No pipettes installed")
                    
                if grippers:
                    ok(f"Found {len(grippers)} gripper(s)")
                else:
                    info("No gripper installed")
            else:
                warn(f"Could not get instruments: {response.status_code}")
                
            # Test 3: Modules
            print(f"\n{Colors.BOLD}[3] Modules{Colors.END}")
            response = await client.get("/modules")
            if response.status_code == 200:
                modules = response.json().get("data", [])
                if modules:
                    ok(f"Found {len(modules)} module(s)")
                    for m in modules:
                        name = m.get("moduleModel", "Unknown")
                        slot = m.get("usbPort", {}).get("hub", "?")
                        print(f"    {name} (port {slot})")
                else:
                    info("No modules attached")
            else:
                warn(f"Could not get modules: {response.status_code}")
                
            # Test 4: Runs
            print(f"\n{Colors.BOLD}[4] Active Runs{Colors.END}")
            response = await client.get("/runs?pageLength=5")
            if response.status_code == 200:
                runs = response.json().get("data", [])
                active = [r for r in runs if r.get("current")]
                if active:
                    warn(f"Found {len(active)} active run(s)")
                    for r in active:
                        print(f"    {r.get('id', '?')}: {r.get('status', '?')}")
                else:
                    ok("No active runs")
            else:
                warn(f"Could not get runs: {response.status_code}")
                
            print(f"\n{Colors.BOLD}{'='*55}{Colors.END}")
            ok("All tests passed!")
            return True
            
    except httpx.ConnectError as e:
        fail(f"Connection error: {e}")
        print()
        warn("Possible causes:")
        print("    1. Robot is not powered on")
        print("    2. USB cable not connected")
        print("    3. Wrong IP address")
        print(f"    4. Local address binding issue (try without {local_address})")
        return False
        
    except Exception as e:
        fail(f"Error: {e}")
        return False


async def test_with_robot_client():
    """Test using the RobotClient class."""
    from src.robot_client import RobotClient
    from src.config import ServerConfig
    
    print(f"\n{Colors.BOLD}{'='*55}{Colors.END}")
    print(f"{Colors.BOLD}  ROBOT CLIENT TEST{Colors.END}")
    print(f"{Colors.BOLD}{'='*55}{Colors.END}\n")
    
    # Load config
    config = ServerConfig("config.yaml")
    
    # Create client
    client = RobotClient(
        host=config.robot_ip,
        port=config.robot_port,
        timeout=config.robot_timeout,
        local_address=config.robot_local_address
    )
    
    # Test connection with retry
    connected = await client.connect_with_retry(
        max_retries=3,
        retry_delay=2.0
    )
    
    if connected:
        ok("RobotClient connected successfully")
        
        # Get health
        health = await client.get_health()
        print(f"    Robot: {health.get('name', 'Unknown')}")
        
        await client.disconnect()
        return True
    else:
        fail("RobotClient failed to connect")
        return False


# ═══════════════════════════════════════════════════════════════════════════
#                              MAIN
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    """Run all connection tests."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Test Opentrons connection")
    parser.add_argument("--host", default="169.254.161.83", help="Robot IP")
    parser.add_argument("--port", type=int, default=31950, help="Robot port")
    parser.add_argument("--local", default="169.254.161.1", help="Local address")
    parser.add_argument("--use-client", action="store_true", help="Test with RobotClient")
    args = parser.parse_args()
    
    if args.use_client:
        await test_with_robot_client()
    else:
        await test_robot_connection(args.host, args.port, args.local)


if __name__ == "__main__":
    asyncio.run(main())
