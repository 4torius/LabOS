#!/usr/bin/env python3
"""
Test ROS Bridge - Quick test for ROS connectivity
=================================================

Run this script on the Ubuntu robot PC to verify:
1. ROS service /setup1/getSubtasksInfo works
2. ROS action /setup1/state_exec is available
3. Tasks can be discovered and executed

Usage:
    source ~/exsensia/tmp_ws/devel/setup.bash
    python3 test_ros_connection.py
    
    # Or test execution:
    python3 test_ros_connection.py --execute <task_id>
"""

import argparse
import asyncio
import sys
import os

# Add current dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def test_ros_imports():
    """Test that ROS modules can be imported."""
    print("\n1. Testing ROS imports...")
    
    try:
        import rospy
        print("   ✓ rospy")
    except ImportError as e:
        print(f"   ✗ rospy: {e}")
        return False
    
    try:
        import actionlib
        print("   ✓ actionlib")
    except ImportError as e:
        print(f"   ✗ actionlib: {e}")
        return False
    
    try:
        from rpwc_msgs.msg import executionCommandsAction, executionCommandsGoal
        print("   ✓ rpwc_msgs.msg (executionCommandsAction)")
    except ImportError as e:
        print(f"   ✗ rpwc_msgs.msg: {e}")
        print("     → Make sure to source your catkin workspace!")
        return False
    
    try:
        from rpwc_msgs.srv import getSubtasksInfo
        print("   ✓ rpwc_msgs.srv (getSubtasksInfo)")
    except ImportError as e:
        print(f"   ✗ rpwc_msgs.srv: {e}")
        return False
    
    return True


def test_ros_master():
    """Test ROS master connectivity."""
    print("\n2. Testing ROS Master...")
    
    import rospy
    
    master_uri = os.environ.get('ROS_MASTER_URI', 'http://localhost:11311')
    print(f"   ROS_MASTER_URI: {master_uri}")
    
    try:
        # Try to get published topics
        import rostopic
        topics = rostopic.get_topic_list()
        print(f"   ✓ Connected to ROS Master ({len(topics[0])} topics)")
        return True
    except Exception as e:
        print(f"   ✗ Cannot connect: {e}")
        return False


def test_service():
    """Test the getSubtasksInfo service."""
    print("\n3. Testing /setup1/getSubtasksInfo service...")
    
    import rospy
    from rpwc_msgs.srv import getSubtasksInfo
    
    service_name = "/setup1/getSubtasksInfo"
    
    try:
        rospy.wait_for_service(service_name, timeout=5.0)
        print(f"   ✓ Service available: {service_name}")
    except rospy.ROSException:
        print(f"   ✗ Service not available: {service_name}")
        return None
    
    try:
        srv = rospy.ServiceProxy(service_name, getSubtasksInfo)
        response = srv()
        
        if response.result.data:
            print(f"   ✓ Service call successful: {response.info.data}")
            print(f"\n   Available Tasks ({len(response.tasksInfo)}):")
            
            tasks = []
            for task in response.tasksInfo:
                root_id = task.rootId.data
                root_name = task.rootName.data
                subtask_count = len(task.subtasksInfo)
                
                print(f"   ┌─ [{root_id[:12]}...]")
                print(f"   │  Name: {root_name}")
                print(f"   │  Subtasks: {subtask_count}")
                
                # Show first few subtasks
                for i, st in enumerate(task.subtasksInfo[:5]):
                    starter = "★" if st.starter.data else " "
                    print(f"   │  {starter} {st.name.data} ({st.id.data[:8]}...)")
                if subtask_count > 5:
                    print(f"   │    ... and {subtask_count - 5} more")
                print(f"   └─")
                
                tasks.append({
                    'id': root_id,
                    'name': root_name,
                    'subtasks': subtask_count
                })
            
            return tasks
        else:
            print(f"   ✗ Service returned failure: {response.info.data}")
            return None
            
    except Exception as e:
        print(f"   ✗ Service call failed: {e}")
        return None


def test_action_server():
    """Test the state_exec action server."""
    print("\n4. Testing /setup1/state_exec action server...")
    
    import rospy
    import actionlib
    from rpwc_msgs.msg import executionCommandsAction
    
    action_name = "/setup1/state_exec"
    
    client = actionlib.SimpleActionClient(action_name, executionCommandsAction)
    
    print(f"   Waiting for action server: {action_name}")
    if client.wait_for_server(rospy.Duration(5.0)):
        print(f"   ✓ Action server available")
        return True
    else:
        print(f"   ✗ Action server not available (timeout)")
        return False


async def test_bridge():
    """Test the full ROS bridge."""
    print("\n5. Testing ROSTaskBridge...")
    
    from ros_task_bridge import ROSTaskBridge
    
    bridge = ROSTaskBridge(
        simulate=False,
        ros_master_uri=os.environ.get('ROS_MASTER_URI', 'http://localhost:11311'),
        namespace="/setup1"
    )
    
    print("   Starting bridge...")
    success = await bridge.start()
    
    if success:
        print("   ✓ Bridge started")
        
        print("   Fetching tasks...")
        tasks = await bridge.get_available_tasks(refresh=True)
        print(f"   ✓ Found {len(tasks)} tasks")
        
        for t in tasks:
            print(f"      - {t.root_name} ({t.root_id[:12]}...)")
        
        await bridge.stop()
        return tasks
    else:
        print("   ✗ Bridge failed to start")
        return None


async def execute_task(task_id: str):
    """Execute a task by ID."""
    print(f"\n6. Executing task: {task_id}")
    
    from ros_task_bridge import ROSTaskBridge, TaskFeedback, TaskState, TaskMode
    
    bridge = ROSTaskBridge(
        simulate=False,
        ros_master_uri=os.environ.get('ROS_MASTER_URI', 'http://localhost:11311'),
        namespace="/setup1"
    )
    
    await bridge.start()
    
    def on_feedback(fb: TaskFeedback):
        print(f"   → {fb.current_subtask}: {fb.status}")
    
    print(f"   Sending goal: startTaskID={task_id}")
    result = await bridge.execute_task(
        task_id=task_id,
        tasks_path="",
        state=TaskState.RUNNING,
        mode=TaskMode.NORMAL,
        timeout=60.0,
        progress_callback=on_feedback
    )
    
    print(f"\n   Result: {'SUCCESS' if result.success else 'FAILED'}")
    print(f"   Message: {result.message}")
    print(f"   Duration: {result.execution_time:.1f}s")
    
    await bridge.stop()
    return result


def main():
    parser = argparse.ArgumentParser(description="Test ROS connection for MobileSiLA2Server")
    parser.add_argument("--execute", type=str, help="Execute task by ID")
    parser.add_argument("--skip-imports", action="store_true", help="Skip import tests")
    args = parser.parse_args()
    
    print("=" * 60)
    print(" MobileSiLA2Server - ROS Connection Test")
    print("=" * 60)
    
    # Test imports
    if not args.skip_imports:
        if not test_ros_imports():
            print("\n✗ ROS imports failed. Make sure to source your workspace:")
            print("  source ~/exsensia/tmp_ws/devel/setup.bash")
            sys.exit(1)
    
    # Initialize ROS node
    import rospy
    rospy.init_node('sila2_ros_test', anonymous=True, disable_signals=True)
    
    # Test ROS master
    if not test_ros_master():
        print("\n✗ Cannot connect to ROS Master")
        sys.exit(1)
    
    # Test service
    tasks = test_service()
    
    # Test action server
    test_action_server()
    
    # Test full bridge
    asyncio.run(test_bridge())
    
    # Execute if requested
    if args.execute:
        asyncio.run(execute_task(args.execute))
    
    print("\n" + "=" * 60)
    print(" Test Complete")
    print("=" * 60)
    
    if tasks:
        print("\n To execute a task, run:")
        print(f"   python3 test_ros_connection.py --execute {tasks[0]['id']}")


if __name__ == "__main__":
    main()
