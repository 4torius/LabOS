#!/usr/bin/env python3
"""
Test SiLA2 Connection to Remote MobileSiLA2Server
=================================================

Tests connectivity from this PC to the MobileSiLA2Server
running on the Ubuntu/ROS machine.

Usage:
    python test_remote_mobile.py                    # Default: 10.16.0.114:50053
    python test_remote_mobile.py --host 192.168.1.100  # Custom host
"""

import argparse
import asyncio
import sys
import os

# Add paths for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grpc


def print_header(text: str):
    print("\n" + "=" * 60)
    print(f" {text}")
    print("=" * 60)


def print_section(text: str):
    print(f"\n--- {text} ---")


async def test_grpc_connection(host: str, port: int) -> bool:
    """Test basic gRPC connectivity."""
    print_section("gRPC Connection Test")
    
    address = f"{host}:{port}"
    print(f"Connecting to: {address}")
    
    try:
        # Create insecure channel (SiLA2 typically uses insecure for local networks)
        channel = grpc.aio.insecure_channel(address)
        
        # Try to get channel state
        # Wait for connection with timeout
        try:
            await asyncio.wait_for(
                channel.channel_ready(),
                timeout=10.0
            )
            print(f"✓ gRPC channel connected to {address}")
            await channel.close()
            return True
        except asyncio.TimeoutError:
            print(f"✗ Connection timeout - server may not be running")
            await channel.close()
            return False
            
    except Exception as e:
        print(f"✗ Connection error: {e}")
        return False


async def test_sila2_discovery(host: str, port: int):
    """Test SiLA2 service discovery on the server."""
    print_section("SiLA2 Service Discovery")
    
    try:
        # Try to import generated stubs (relative path)
        import sys
        import os
        script_dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, script_dir)
        
        from features import TaskManagement_pb2, TaskManagement_pb2_grpc
        
        address = f"{host}:{port}"
        channel = grpc.aio.insecure_channel(address)
        
        # Try TaskManagement service
        stub = TaskManagement_pb2_grpc.TaskManagementStub(channel)
        
        print("Calling Get_AvailableTasks...")
        try:
            request = TaskManagement_pb2.Get_AvailableTasks_Request()
            response = await asyncio.wait_for(
                stub.Get_AvailableTasks(request),
                timeout=10.0
            )
            
            print(f"✓ Response received!")
            print(f"  Tasks found: {len(response.tasks)}")
            for task in response.tasks:
                print(f"    - {task.task_name} (ID: {task.task_id}, subtasks: {task.subtask_count})")
                
        except asyncio.TimeoutError:
            print("✗ Request timeout")
        except grpc.RpcError as e:
            print(f"✗ gRPC error: {e.code()} - {e.details()}")
            
        await channel.close()
        
    except ImportError as e:
        print(f"Stubs not found: {e}")
        print("Run: python -m grpc_tools.protoc ... in features/ directory")


async def test_sila2_server_info(host: str, port: int):
    """Try to get basic server info via reflection or standard methods."""
    print_section("SiLA2 Server Info")
    
    # SiLA2 servers implement SiLAService feature
    address = f"{host}:{port}"
    
    try:
        channel = grpc.aio.insecure_channel(address)
        
        # Try reflection if available
        try:
            from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc
            
            stub = reflection_pb2_grpc.ServerReflectionStub(channel)
            
            # List services
            request = reflection_pb2.ServerReflectionRequest(
                list_services=""
            )
            
            async for response in stub.ServerReflectionInfo(iter([request])):
                if response.HasField('list_services_response'):
                    print("Available gRPC services:")
                    for service in response.list_services_response.service:
                        print(f"  - {service.name}")
                        
        except ImportError:
            print("gRPC reflection not available")
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.UNIMPLEMENTED:
                print("Server doesn't support reflection")
            else:
                print(f"Reflection error: {e}")
                
        await channel.close()
        
    except Exception as e:
        print(f"Error: {e}")


def test_network(host: str, port: int):
    """Quick network connectivity test."""
    import socket
    
    print_section("Network Connectivity")
    
    # Ping
    print(f"\n[1] Testing ICMP ping to {host}...")
    import subprocess
    try:
        if sys.platform == 'win32':
            result = subprocess.run(
                ['ping', '-n', '2', '-w', '1000', host],
                capture_output=True,
                text=True,
                timeout=10
            )
        else:
            result = subprocess.run(
                ['ping', '-c', '2', '-W', '1', host],
                capture_output=True,
                text=True,
                timeout=10
            )
        
        if result.returncode == 0:
            print(f"    ✓ Host reachable")
        else:
            print(f"    ✗ Host not responding to ping")
    except Exception as e:
        print(f"    ✗ Ping error: {e}")
    
    # TCP port
    print(f"\n[2] Testing TCP port {port}...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    
    try:
        result = sock.connect_ex((host, port))
        if result == 0:
            print(f"    ✓ Port {port} is OPEN")
            return True
        else:
            print(f"    ✗ Port {port} is CLOSED (error: {result})")
            print("    Make sure MobileSiLA2Server is running on the Ubuntu PC")
            return False
    except socket.timeout:
        print(f"    ✗ Connection timeout")
        return False
    except Exception as e:
        print(f"    ✗ Error: {e}")
        return False
    finally:
        sock.close()


async def main():
    parser = argparse.ArgumentParser(
        description="Test connection to remote MobileSiLA2Server"
    )
    parser.add_argument(
        '--host',
        default='10.16.0.114',
        help='Server host (default: 10.16.0.114)'
    )
    parser.add_argument(
        '--port',
        type=int,
        default=50053,
        help='Server port (default: 50053)'
    )
    parser.add_argument(
        '--network-only',
        action='store_true',
        help='Only test network connectivity'
    )
    
    args = parser.parse_args()
    
    print_header(f"MobileSiLA2Server Remote Connection Test")
    print(f"Target: {args.host}:{args.port}")
    
    # Network test
    network_ok = test_network(args.host, args.port)
    
    if args.network_only:
        return
    
    if not network_ok:
        print("\n" + "!" * 60)
        print(" Cannot reach server. Make sure:")
        print(" 1. MobileSiLA2Server is running on the Ubuntu PC")
        print(" 2. Run: ./run_server.sh on the Ubuntu PC")
        print("!" * 60)
        return
    
    # gRPC test
    grpc_ok = await test_grpc_connection(args.host, args.port)
    
    if grpc_ok:
        # Try to discover services
        await test_sila2_server_info(args.host, args.port)
        await test_sila2_discovery(args.host, args.port)
    
    print("\n" + "=" * 60)
    if grpc_ok:
        print(" ✓ Server is reachable and responding!")
        print("\n Next steps:")
        print(" 1. Start the orchestrator on this PC")
        print(" 2. The mobile robot should appear in PnP Console")
        print(" 3. Try executing a task from the orchestrator")
    else:
        print(" ✗ Server not responding")
        print("\n On the Ubuntu PC, run:")
        print("   cd /path/to/MobileSiLA2Server")
        print("   ./run_server.sh")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
