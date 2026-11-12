#!/usr/bin/env python3
"""
Test MobileSiLA2Server Client
=============================

Run this from the Windows orchestrator PC to test connection
to the MobileSiLA2Server running on Ubuntu.

Usage:
    python test_mobile_client.py --host 10.16.0.114 --port 50053
"""

import argparse
import asyncio
import sys
import os

# Add SiLA2 paths
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'features'))

import grpc

# Import generated protobuf
try:
    from features import TaskManagement_pb2, TaskManagement_pb2_grpc
except ImportError:
    print("ERROR: Cannot import TaskManagement_pb2")
    print("Make sure you're running from the MobileSiLA2Server directory")
    sys.exit(1)


async def test_connection(host: str, port: int):
    """Test connection to MobileSiLA2Server."""
    
    address = f"{host}:{port}"
    print(f"\nConnecting to {address}...")
    
    try:
        channel = grpc.aio.insecure_channel(address)
        stub = TaskManagement_pb2_grpc.TaskManagementStub(channel)
        
        # Test connection status
        print("\n1. Getting connection status...")
        response = await stub.Get_RobotConnectionStatus(
            TaskManagement_pb2.Get_RobotConnectionStatus_Request()
        )
        print(f"   Robot status: {response.connection_status}")
        
        # Get available tasks (for dropdown)
        print("\n2. Getting available tasks (for dropdown)...")
        response = await stub.Get_AvailableTasks(
            TaskManagement_pb2.Get_AvailableTasks_Request()
        )
        
        tasks = list(response.tasks)
        print(f"   Found {len(tasks)} tasks:")
        
        for task in tasks:
            print(f"   ┌─ ID: {task.task_id}")
            print(f"   │  Name: {task.task_name}")
            print(f"   │  Subtasks: {task.subtask_count}")
            print(f"   └─")
        
        # Get task status
        print("\n3. Getting task status...")
        response = await stub.Get_TaskStatus(
            TaskManagement_pb2.Get_TaskStatus_Request()
        )
        print(f"   Status: {response.task_status}")
        
        await channel.close()
        return tasks
        
    except grpc.aio.AioRpcError as e:
        print(f"\n✗ Connection failed: {e.code()}")
        print(f"  Details: {e.details()}")
        return None
    except Exception as e:
        print(f"\n✗ Error: {e}")
        return None


async def execute_task(host: str, port: int, task_id: str, mode: str = "Normal"):
    """Execute a task on the mobile robot."""
    
    address = f"{host}:{port}"
    print(f"\nExecuting task '{task_id}' on {address}...")
    
    try:
        channel = grpc.aio.insecure_channel(address)
        stub = TaskManagement_pb2_grpc.TaskManagementStub(channel)
        
        # Execute task with streaming
        request = TaskManagement_pb2.ExecuteTask_Request(
            task_id=task_id,
            execution_mode=mode
        )
        
        print(f"\nTask: {task_id}")
        print(f"Mode: {mode}")
        print("-" * 40)
        
        async for response in stub.ExecuteTask(request):
            # Check if it's an intermediate response (progress)
            if hasattr(response, 'current_subtask') and response.current_subtask:
                print(f"  → {response.current_subtask}: {response.status} ({response.progress:.0f}%)")
            
            # Check if it's the final response
            if hasattr(response, 'success'):
                print("-" * 40)
                print(f"Result: {'SUCCESS' if response.success else 'FAILED'}")
                print(f"Message: {response.message}")
                print(f"Duration: {response.execution_time:.1f}s")
        
        await channel.close()
        
    except grpc.aio.AioRpcError as e:
        print(f"\n✗ Execution failed: {e.code()}")
        print(f"  Details: {e.details()}")
    except Exception as e:
        print(f"\n✗ Error: {e}")


async def get_task_details(host: str, port: int, task_id: str):
    """Get details about a specific task."""
    
    address = f"{host}:{port}"
    
    try:
        channel = grpc.aio.insecure_channel(address)
        stub = TaskManagement_pb2_grpc.TaskManagementStub(channel)
        
        response = await stub.GetTaskDetails(
            TaskManagement_pb2.GetTaskDetails_Request(task_id=task_id)
        )
        
        print(f"\nTask Details: {response.task_name}")
        print(f"ID: {response.task_id}")
        print(f"Subtasks: {response.subtasks_json}")
        
        await channel.close()
        
    except Exception as e:
        print(f"\n✗ Error: {e}")


def main():
    parser = argparse.ArgumentParser(description="Test MobileSiLA2Server from Windows")
    parser.add_argument("--host", default="10.16.0.114", help="Server IP address")
    parser.add_argument("--port", type=int, default=50053, help="Server port")
    parser.add_argument("--execute", type=str, help="Execute task by ID")
    parser.add_argument("--details", type=str, help="Get task details by ID")
    parser.add_argument("--mode", default="Normal", choices=["Normal", "StepByStep", "DryRun"])
    args = parser.parse_args()
    
    print("=" * 60)
    print(" MobileSiLA2Server - Client Test")
    print("=" * 60)
    print(f" Target: {args.host}:{args.port}")
    
    if args.execute:
        asyncio.run(execute_task(args.host, args.port, args.execute, args.mode))
    elif args.details:
        asyncio.run(get_task_details(args.host, args.port, args.details))
    else:
        tasks = asyncio.run(test_connection(args.host, args.port))
        
        if tasks:
            print("\n" + "=" * 60)
            print(" Connection successful!")
            print("=" * 60)
            print("\n To execute a task:")
            print(f"   python test_mobile_client.py --host {args.host} --execute {tasks[0].task_id}")
        else:
            print("\n" + "=" * 60)
            print(" Connection failed!")
            print("=" * 60)
            print("\n Make sure:")
            print("   1. MobileSiLA2Server is running on Ubuntu")
            print("   2. Firewall allows port 50053")
            print("   3. Network is reachable")


if __name__ == "__main__":
    main()
