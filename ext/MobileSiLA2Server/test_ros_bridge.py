#!/usr/bin/env python3
"""
Test Script for ROS Task Bridge
================================

Interactive test to verify communication with the ROS robot PC.

Usage:
    python test_ros_bridge.py                    # Simulation mode
    python test_ros_bridge.py --ros-uri http://192.168.1.100:11311  # Real ROS
    python test_ros_bridge.py --ping-only        # Only test network connectivity
"""

import argparse
import asyncio
import logging
import socket
import subprocess
import sys
import os
from urllib.parse import urlparse

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s │ %(levelname)-8s │ %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def print_header(text: str):
    """Print a formatted header."""
    print("\n" + "=" * 60)
    print(f" {text}")
    print("=" * 60)


def print_section(text: str):
    """Print a section header."""
    print(f"\n--- {text} ---")


def test_network_connectivity(host: str, port: int) -> dict:
    """
    Test network connectivity to the ROS master.
    
    Returns:
        dict with 'reachable', 'latency_ms', 'error' keys
    """
    result = {
        'reachable': False,
        'latency_ms': None,
        'error': None,
        'ping_ok': False,
        'port_open': False
    }
    
    # Test ICMP ping
    print(f"\n[1/3] Ping test to {host}...")
    try:
        if sys.platform == 'win32':
            ping_cmd = ['ping', '-n', '3', '-w', '1000', host]
        else:
            ping_cmd = ['ping', '-c', '3', '-W', '1', host]
        
        ping_result = subprocess.run(
            ping_cmd, 
            capture_output=True, 
            text=True,
            timeout=10
        )
        
        if ping_result.returncode == 0:
            result['ping_ok'] = True
            # Extract latency from output
            output = ping_result.stdout
            if 'time=' in output or 'time<' in output:
                import re
                # Windows: time=XXms or time<1ms
                # Linux: time=XX.X ms
                match = re.search(r'time[=<](\d+\.?\d*)', output)
                if match:
                    result['latency_ms'] = float(match.group(1))
            print(f"      ✓ Host reachable (latency: {result['latency_ms']}ms)")
        else:
            print(f"      ✗ Host not reachable via ping")
            print(f"        (This is OK if ICMP is blocked)")
    except subprocess.TimeoutExpired:
        print(f"      ✗ Ping timeout")
    except Exception as e:
        print(f"      ✗ Ping error: {e}")
    
    # Test TCP connection to ROS master port
    print(f"\n[2/3] TCP port test to {host}:{port}...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        start_time = asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else 0
        
        connect_result = sock.connect_ex((host, port))
        
        if connect_result == 0:
            result['port_open'] = True
            result['reachable'] = True
            print(f"      ✓ ROS Master port {port} is OPEN")
        else:
            print(f"      ✗ ROS Master port {port} is CLOSED")
            print(f"        Error code: {connect_result}")
            if connect_result == 10061:
                print(f"        Connection refused - ROS Master may not be running")
            elif connect_result == 10060:
                print(f"        Connection timeout - check firewall")
        sock.close()
    except socket.timeout:
        print(f"      ✗ Connection timeout to port {port}")
        result['error'] = "Connection timeout"
    except Exception as e:
        print(f"      ✗ TCP test error: {e}")
        result['error'] = str(e)
    
    # DNS resolution test
    print(f"\n[3/3] DNS resolution test for {host}...")
    try:
        ip = socket.gethostbyname(host)
        print(f"      ✓ Resolved to: {ip}")
    except socket.gaierror as e:
        print(f"      ✗ DNS resolution failed: {e}")
        if not result['ping_ok'] and not result['port_open']:
            result['error'] = f"Cannot resolve hostname: {host}"
    
    return result


async def test_ros_bridge_simulation():
    """Test the ROS bridge in simulation mode."""
    print_header("ROS Task Bridge - SIMULATION Test")
    
    from ros_task_bridge import ROSTaskBridge, TaskFeedback
    
    bridge = ROSTaskBridge(simulate=True)
    
    # Start
    print_section("Starting Bridge")
    success = await bridge.start()
    print(f"Started: {success}")
    print(f"Connected: {bridge.is_connected}")
    
    # Get tasks
    print_section("Available Tasks")
    tasks = await bridge.get_available_tasks()
    
    if not tasks:
        print("No tasks available!")
    else:
        for i, task in enumerate(tasks, 1):
            print(f"\n{i}. [{task.root_id}] {task.root_name}")
            for st in task.subtasks:
                print(f"      └─ {st.subtask_name} ({st.estimated_duration}s)")
    
    # Execute a task
    print_section("Task Execution Test")
    
    def progress_cb(feedback: TaskFeedback):
        bar_len = 30
        filled = int(bar_len * feedback.progress / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\r  [{bar}] {feedback.progress:5.1f}% | {feedback.current_subtask}: {feedback.status}    ", end="", flush=True)
    
    if tasks:
        task_id = tasks[0].root_id
        print(f"Executing: {task_id}")
        
        result = await bridge.execute_task(
            task_id=task_id,
            progress_callback=progress_cb
        )
        
        print()  # Newline after progress bar
        print(f"\nResult: {'✓ SUCCESS' if result.success else '✗ FAILED'}")
        print(f"Message: {result.message}")
        print(f"Duration: {result.execution_time:.1f}s")
    
    # Stop
    await bridge.stop()
    print("\n✓ Bridge stopped successfully")
    
    return True


async def test_ros_bridge_real(ros_master_uri: str, execute_task: bool = False):
    """
    Test the ROS bridge with real ROS connection.
    
    Args:
        ros_master_uri: URI of the ROS master (e.g., http://192.168.1.100:11311)
        execute_task: If True, execute a task after listing them
    """
    print_header(f"ROS Task Bridge - REAL Connection Test")
    print(f"ROS Master URI: {ros_master_uri}")
    
    # Parse URI
    parsed = urlparse(ros_master_uri)
    host = parsed.hostname or 'localhost'
    port = parsed.port or 11311
    
    # Network connectivity test
    print_section("Network Connectivity Test")
    net_result = test_network_connectivity(host, port)
    
    if not net_result['reachable']:
        print("\n" + "!" * 60)
        print(" NETWORK ERROR - Cannot reach ROS Master")
        print("!" * 60)
        print("\nPossible causes:")
        print("  1. ROS Master is not running on the robot PC")
        print("  2. Firewall blocking port 11311")
        print("  3. Wrong IP address")
        print("  4. Network routing issue (dual-homing not configured)")
        print("\nOn the Ubuntu/ROS PC, run:")
        print(f"  roscore")
        print(f"  # Then verify with: rostopic list")
        print("\nAlso ensure:")
        print(f"  export ROS_MASTER_URI={ros_master_uri}")
        print(f"  export ROS_IP={host}")
        return False
    
    print("\n✓ Network connectivity OK")
    
    # Try ROS bridge connection
    print_section("ROS Bridge Connection")
    
    try:
        from ros_task_bridge import ROSTaskBridge, TaskFeedback
        
        bridge = ROSTaskBridge(
            simulate=False,
            ros_master_uri=ros_master_uri
        )
        
        success = await bridge.start()
        
        if not success:
            print("✗ Failed to start ROS bridge")
            print("\nPossible causes:")
            print("  - rospy not installed on this machine")
            print("  - ROS environment not sourced")
            print("  - rpwc_msgs package not available")
            return False
        
        print("✓ ROS bridge started")
        print(f"  Connected: {bridge.is_connected}")
        
        # Get available tasks
        print_section("Fetching Available Tasks")
        tasks = await bridge.get_available_tasks()
        
        if not tasks:
            print("No tasks found!")
            print("\nOn the robot PC, verify the service is running:")
            print("  rosservice list | grep getSubtasksInfo")
            print("  rosservice call /setup1/getSubtasksInfo")
        else:
            print(f"Found {len(tasks)} tasks:\n")
            for i, task in enumerate(tasks, 1):
                print(f"  {i}. [{task.root_id}] {task.root_name}")
                for st in task.subtasks:
                    print(f"        └─ {st.subtask_name}")
        
        # Optionally execute a task
        if execute_task and tasks:
            print_section("Task Execution")
            print("\nAvailable tasks:")
            for i, task in enumerate(tasks, 1):
                print(f"  {i}. {task.root_name} (ID: {task.root_id})")
            
            try:
                choice = input("\nEnter task number to execute (or 'skip'): ").strip()
                if choice.lower() != 'skip' and choice.isdigit():
                    idx = int(choice) - 1
                    if 0 <= idx < len(tasks):
                        task = tasks[idx]
                        print(f"\nExecuting: {task.root_name}")
                        
                        def progress_cb(feedback: TaskFeedback):
                            print(f"  → {feedback.current_subtask}: {feedback.status}")
                        
                        result = await bridge.execute_task(
                            task_id=task.root_id,
                            progress_callback=progress_cb
                        )
                        
                        print(f"\nResult: {'✓ SUCCESS' if result.success else '✗ FAILED'}")
                        print(f"Message: {result.message}")
            except (EOFError, KeyboardInterrupt):
                print("\nSkipped task execution")
        
        await bridge.stop()
        print("\n✓ Test completed successfully")
        return True
        
    except ImportError as e:
        print(f"\n✗ Import error: {e}")
        print("\nThis Windows PC doesn't have ROS installed.")
        print("The actual ROS communication happens ON the robot PC.")
        print("\nTo test the full bridge, you need to:")
        print("  1. Start the MobileSiLA2Server on this PC (uses SiLA2)")
        print("  2. The server will try to connect to ROS on the robot PC")
        print("\nAlternatively, test network connectivity only with --ping-only")
        return False


async def interactive_menu():
    """Interactive test menu."""
    print_header("ROS Task Bridge - Interactive Test")
    
    while True:
        print("\nOptions:")
        print("  1. Test in SIMULATION mode (no ROS needed)")
        print("  2. Test NETWORK connectivity to robot PC")
        print("  3. Test REAL ROS connection")
        print("  4. Test REAL ROS + execute task")
        print("  5. Show network configuration help")
        print("  0. Exit")
        
        try:
            choice = input("\nSelect option (0-5): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting...")
            break
        
        if choice == "0":
            break
        elif choice == "1":
            await test_ros_bridge_simulation()
        elif choice == "2":
            uri = input("Enter ROS Master URI [http://192.168.1.100:11311]: ").strip()
            if not uri:
                uri = "http://192.168.1.100:11311"
            parsed = urlparse(uri)
            test_network_connectivity(parsed.hostname, parsed.port or 11311)
        elif choice == "3":
            uri = input("Enter ROS Master URI [http://192.168.1.100:11311]: ").strip()
            if not uri:
                uri = "http://192.168.1.100:11311"
            await test_ros_bridge_real(uri, execute_task=False)
        elif choice == "4":
            uri = input("Enter ROS Master URI [http://192.168.1.100:11311]: ").strip()
            if not uri:
                uri = "http://192.168.1.100:11311"
            await test_ros_bridge_real(uri, execute_task=True)
        elif choice == "5":
            show_network_help()
        else:
            print("Invalid option")


def show_network_help():
    """Show network configuration help."""
    print_header("Network Configuration for Dual-Homing")
    
    print("""
The ROS PC needs TWO network interfaces:

┌─────────────────────────────────────────────────────────────────┐
│  PC Windows (This Machine - Orchestrator)                       │
│  WiFi IP: 192.168.1.X                                          │
│  └── MobileSiLA2Server (port 50053)                            │
└──────────────────┬──────────────────────────────────────────────┘
                   │ WiFi Network (192.168.1.0/24)
                   │
┌──────────────────▼──────────────────────────────────────────────┐
│  PC Ubuntu (ROS)                                                │
│                                                                 │
│  Interface 1 - WiFi (for orchestrator communication):           │
│    wlan0: 192.168.1.100                                        │
│    → Connected to lab WiFi router                              │
│                                                                 │
│  Interface 2 - Ethernet (for robot control):                   │
│    eth0: 10.0.0.X (or whatever the robot network uses)         │
│    → Connected to robot internal network                       │
│                                                                 │
│  ROS Configuration (~/.bashrc or before running roscore):      │
│    export ROS_MASTER_URI=http://192.168.1.100:11311            │
│    export ROS_IP=192.168.1.100                                 │
│                                                                 │
│  IMPORTANT: ROS_IP must be the WiFi IP so this PC can reach it │
└─────────────────────────────────────────────────────────────────┘

SETUP STEPS ON UBUNTU PC:

1. Connect Ethernet to robot network
   # Probably already configured for robot control

2. Connect WiFi to lab router
   nmcli device wifi connect "LabNetwork" password "password"
   
3. Get the WiFi IP address
   ip addr show wlan0 | grep inet
   # Note this IP (e.g., 192.168.1.100)

4. Configure ROS to use WiFi IP (add to ~/.bashrc)
   export ROS_MASTER_URI=http://192.168.1.100:11311
   export ROS_IP=192.168.1.100
   
5. Start ROS master
   source ~/.bashrc
   roscore
   
6. In another terminal, start the action server
   # (your robot-specific launch file)
   roslaunch rpwc_msgs start_task_server.launch
   
7. Verify from robot PC
   rostopic list
   rosservice list

VERIFY FROM THIS WINDOWS PC:

1. Update config.yaml with correct ROS Master URI
   ros:
     master_uri: "http://192.168.1.100:11311"

2. Run this test script
   python test_ros_bridge.py --ping-only
   python test_ros_bridge.py --ros-uri http://192.168.1.100:11311

FIREWALL RULES (on Ubuntu):

sudo ufw allow 11311/tcp  # ROS Master
sudo ufw allow 11312:11500/tcp  # ROS topics range
""")


def main():
    parser = argparse.ArgumentParser(
        description="Test ROS Task Bridge connectivity",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test_ros_bridge.py                                    # Interactive mode
  python test_ros_bridge.py --simulate                         # Simulation only
  python test_ros_bridge.py --ping-only                        # Network test only
  python test_ros_bridge.py --ros-uri http://192.168.1.100:11311  # Real ROS test
"""
    )
    
    parser.add_argument(
        '--ros-uri',
        type=str,
        default='',
        help='ROS Master URI (e.g., http://192.168.1.100:11311)'
    )
    
    parser.add_argument(
        '--simulate',
        action='store_true',
        help='Run in simulation mode (no ROS connection)'
    )
    
    parser.add_argument(
        '--ping-only',
        action='store_true',
        help='Only test network connectivity, no ROS'
    )
    
    parser.add_argument(
        '--execute',
        action='store_true',
        help='Also execute a task (requires --ros-uri)'
    )
    
    parser.add_argument(
        '--help-network',
        action='store_true',
        help='Show network configuration help'
    )
    
    args = parser.parse_args()
    
    if args.help_network:
        show_network_help()
        return
    
    if args.simulate:
        asyncio.run(test_ros_bridge_simulation())
    elif args.ping_only:
        uri = args.ros_uri or "http://192.168.1.100:11311"
        parsed = urlparse(uri)
        test_network_connectivity(parsed.hostname, parsed.port or 11311)
    elif args.ros_uri:
        asyncio.run(test_ros_bridge_real(args.ros_uri, execute_task=args.execute))
    else:
        asyncio.run(interactive_menu())


if __name__ == "__main__":
    main()
