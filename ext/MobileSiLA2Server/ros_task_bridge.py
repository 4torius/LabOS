#!/usr/bin/env python3
"""
ROS Task Bridge for Mobile Robot SiLA2 Server
==============================================

Connects the SiLA2 server to a ROS-based task execution system.

This bridge communicates with:
- Action Server: /setup1/state_exec (executionCommand.action)
- Service: /setup1/getSubtasksInfo (getSubtaskInfo.srv)

Action Definition (NEW - rpwc_msgs):
    # Request
    int32 command    # 0=play/run (default); TODO: verify other values with robot team
    string tasksPath
    string startTaskID
    string endTaskID
    ---
    # Response
    bool success
    string msg
    ---
    # Feedback
    string current_subtask
    string status

Service Definition (getSubtaskInfo.srv):
    ---
    infoTasks[] tasksInfo
    std_msgs/Bool result
    std_msgs/String info

Message Definition (infoTasks.msg):
    std_msgs/String rootId
    std_msgs/String rootName
    rpwc_msgs/infoSubtask[] subtasksInfo
"""

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Optional, Callable, Dict, Any, List

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#                              DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════

class MobileCommand(IntEnum):
    """
    Command values for the new ROS action /setup1/state_exec (int32 command field).

    TODO: verify exact values with the robot team before final deployment.
    Default command=0 has been confirmed as normal play/run.
    """
    PLAY = 0        # Normal execution (start/run)
    STEP = 1        # Step-by-step execution
    DRY_RUN = 2     # Dry run (no physical motion)


# Legacy enums kept for SiLA2-layer mapping only — not sent to ROS anymore.
class TaskState(IntEnum):
    IDLE = 0
    RUNNING = 1
    PAUSED = 2
    STOPPED = 3
    ERROR = 4


class TaskMode(IntEnum):
    NORMAL = 0
    STEP_BY_STEP = 1
    DRY_RUN = 2


@dataclass
class SubtaskInfo:
    """Information about a single subtask."""
    subtask_id: str
    subtask_name: str
    description: str = ""
    estimated_duration: float = 0.0  # seconds


@dataclass
class TaskInfo:
    """Information about a root task with its subtasks."""
    root_id: str
    root_name: str
    subtasks: List[SubtaskInfo] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "id": self.root_id,
            "name": self.root_name,
            "subtasks": [
                {
                    "id": st.subtask_id,
                    "name": st.subtask_name,
                    "description": st.description,
                    "estimated_duration": st.estimated_duration
                }
                for st in self.subtasks
            ]
        }


@dataclass
class TaskExecutionResult:
    """Result of a task execution."""
    success: bool
    message: str
    execution_time: float = 0.0
    final_subtask: str = ""


@dataclass
class TaskFeedback:
    """Feedback during task execution."""
    current_subtask: str
    status: str
    progress: float = 0.0  # 0-100


# ═══════════════════════════════════════════════════════════════════════════
#                              ROS TASK BRIDGE
# ═══════════════════════════════════════════════════════════════════════════

class ROSTaskBridge:
    """
    Bridge to ROS for task-based robot control.
    
    Connects to a ROS system on another PC that exposes:
    - Task discovery service
    - Task execution action server
    
    Attributes:
        simulate: If True, simulates ROS communication
        ros_master_uri: URI of the ROS master (e.g., http://192.168.1.100:11311)
        namespace: ROS namespace for topics/services (default: /setup1)
    """
    
    def __init__(
        self,
        simulate: bool = False,
        ros_master_uri: str = "",
        namespace: str = "/setup1",
        tasks_path: str = ""
    ):
        self.simulate = simulate
        self.ros_master_uri = ros_master_uri
        self.namespace = namespace.rstrip('/')
        self.tasks_path = tasks_path  # Path to tasks folder for JSON fallback
        
        # ROS handles
        self._rospy = None
        self._actionlib = None
        self._action_client = None
        self._service_proxy = None
        
        # State
        self._connected = False
        self._available_tasks: List[TaskInfo] = []
        self._current_task: Optional[str] = None
        self._current_feedback: Optional[TaskFeedback] = None
        
        # Thread for ROS spin
        self._ros_thread: Optional[threading.Thread] = None
        self._shutdown_flag = False
        
        # Simulated tasks for testing
        self._simulated_tasks = [
            TaskInfo(
                root_id="transport_plate",
                root_name="Transport Plate",
                subtasks=[
                    SubtaskInfo("move_to_source", "Move to Source", "Navigate to pickup location", 10.0),
                    SubtaskInfo("pickup", "Pickup Plate", "Grasp the plate", 5.0),
                    SubtaskInfo("move_to_dest", "Move to Destination", "Navigate to dropoff location", 10.0),
                    SubtaskInfo("place", "Place Plate", "Release the plate", 5.0),
                ]
            ),
            TaskInfo(
                root_id="pick_from_opentrons",
                root_name="Pick from Opentrons",
                subtasks=[
                    SubtaskInfo("approach", "Approach Opentrons", "Navigate near the instrument", 8.0),
                    SubtaskInfo("arm_extend", "Extend Arm", "Move arm to pickup position", 3.0),
                    SubtaskInfo("grasp", "Grasp Plate", "Close gripper on plate", 2.0),
                    SubtaskInfo("arm_retract", "Retract Arm", "Move arm to travel position", 3.0),
                ]
            ),
            TaskInfo(
                root_id="place_to_tecan",
                root_name="Place to Tecan",
                subtasks=[
                    SubtaskInfo("approach", "Approach Tecan", "Navigate near the instrument", 8.0),
                    SubtaskInfo("arm_extend", "Extend Arm", "Move arm to place position", 3.0),
                    SubtaskInfo("release", "Release Plate", "Open gripper", 2.0),
                    SubtaskInfo("arm_retract", "Retract Arm", "Move arm to travel position", 3.0),
                ]
            ),
            TaskInfo(
                root_id="home_robot",
                root_name="Home Robot",
                subtasks=[
                    SubtaskInfo("arm_home", "Home Arm", "Move arm to home position", 5.0),
                    SubtaskInfo("base_home", "Home Base", "Navigate to home location", 15.0),
                ]
            ),
        ]
        
        logger.info(f"ROSTaskBridge created (simulate={simulate}, namespace={namespace})")
    
    # ─────────────────────────────────────────────────────────────────
    # Connection Management
    # ─────────────────────────────────────────────────────────────────
    
    def _init_ros(self) -> bool:
        """Initialize ROS connection."""
        try:
            # Set ROS_MASTER_URI if provided
            if self.ros_master_uri:
                os.environ['ROS_MASTER_URI'] = self.ros_master_uri
                logger.info(f"ROS_MASTER_URI set to: {self.ros_master_uri}")
            
            import rospy  # type: ignore[import]
            import actionlib  # type: ignore[import]
            
            self._rospy = rospy
            self._actionlib = actionlib
            
            logger.info("ROS1 modules imported successfully")
            return True
            
        except ImportError as e:
            logger.error(f"Failed to import ROS1 modules: {e}")
            logger.error("Make sure rospy and actionlib are installed")
            return False
    
    async def start(self) -> bool:
        """
        Start the ROS task bridge.
        
        Initializes ROS node and connects to:
        - Action server for task execution
        - Service for task discovery
        
        Returns:
            True if connection successful
        """
        if self.simulate:
            logger.info("ROSTaskBridge started in SIMULATION mode")
            self._connected = True
            return True
        
        if not self._init_ros():
            return False
        
        try:
            # Initialize ROS node
            self._rospy.init_node(  # type: ignore[union-attr]
                'sila2_task_bridge',
                anonymous=True,
                disable_signals=True
            )
            logger.info("ROS node initialized: sila2_task_bridge")
            
            # Import action/service message types
            # These are generated from the .action and .srv files
            msg_loaded = False
            try:
                from rpwc_msgs.msg import executionCommandsAction, executionCommandsGoal  # type: ignore[import]
                from rpwc_msgs.srv import getSubtaskInfo  # type: ignore[import]
                
                self._ExecutionCommandAction = executionCommandsAction
                self._ExecutionCommandGoal = executionCommandsGoal
                self._GetSubtaskInfo = getSubtaskInfo
                msg_loaded = True
                logger.info("rpwc_msgs loaded successfully (executionCommandsAction)")
                
            except ImportError as e:
                logger.warning(f"Could not import rpwc_msgs directly: {e}")
                logger.info("Attempting dynamic message loading...")
                
                # Try dynamic message loading using rostopic
                try:
                    import rostopic
                    import roslib.message
                    import genpy
                    
                    # Get action type from topic
                    goal_topic = f"{self.namespace}/state_exec/goal"
                    goal_topic_type, _, _ = rostopic.get_topic_type(goal_topic, blocking=True)
                    
                    if goal_topic_type:
                        # Extract base action name (remove 'ActionGoal' suffix to get 'Action')
                        # e.g., rpwc_msgs/executionCommandActionGoal -> rpwc_msgs/executionCommandAction
                        action_name = goal_topic_type.replace('ActionGoal', 'Action')
                        goal_name = goal_topic_type.replace('ActionGoal', 'Goal')
                        
                        # Load message classes dynamically
                        self._ExecutionCommandAction = roslib.message.get_message_class(action_name)
                        self._ExecutionCommandGoal = roslib.message.get_message_class(goal_name)
                        
                        if self._ExecutionCommandAction and self._ExecutionCommandGoal:
                            logger.info(f"Dynamically loaded: {action_name}")
                            msg_loaded = True
                        else:
                            logger.error(f"Failed to load message classes for {action_name}")
                    else:
                        logger.warning(f"Could not get topic type for {goal_topic}")
                        
                except Exception as dyn_e:
                    logger.warning(f"Dynamic message loading failed: {dyn_e}")
            
            if not msg_loaded:
                logger.error("Could not load rpwc_msgs - task execution will not work")
                logger.error("Make sure to source your catkin workspace with rpwc_msgs before running")
                return False
            
            # Connect to action server
            action_name = f"{self.namespace}/state_exec"
            logger.info(f"Waiting for action server: {action_name}")
            
            self._action_client = self._actionlib.SimpleActionClient(  # type: ignore[union-attr]
                action_name,
                self._ExecutionCommandAction
            )
            
            if not self._action_client.wait_for_server(self._rospy.Duration(10.0)):  # type: ignore[union-attr]
                logger.warning(f"Action server {action_name} not available")
                # Don't fail - might become available later
            else:
                logger.info(f"Connected to action server: {action_name}")
            
            # Connect to discovery service
            service_name = f"{self.namespace}/getSubtasksInfo"
            logger.info(f"Waiting for service: {service_name}")
            
            try:
                self._rospy.wait_for_service(service_name, timeout=5.0)  # type: ignore[union-attr]
                
                # Try to get service type dynamically if not loaded
                if not hasattr(self, '_GetSubtaskInfo') or self._GetSubtaskInfo is None:
                    try:
                        import rosservice
                        srv_type = rosservice.get_service_type(service_name)
                        if srv_type:
                            import roslib.message
                            self._GetSubtaskInfo = roslib.message.get_service_class(srv_type)
                            logger.info(f"Dynamically loaded service: {srv_type}")
                    except Exception as srv_e:
                        logger.warning(f"Could not dynamically load service type: {srv_e}")
                
                if hasattr(self, '_GetSubtaskInfo') and self._GetSubtaskInfo:
                    self._service_proxy = self._rospy.ServiceProxy(  # type: ignore[union-attr]
                        service_name,
                        self._GetSubtaskInfo
                    )
                    logger.info(f"Connected to service: {service_name}")
                else:
                    logger.warning(f"Service type not available - will use JSON fallback for task discovery")
                    
            except self._rospy.ROSException:  # type: ignore[union-attr]
                logger.warning(f"Service {service_name} not available - will use JSON fallback")
            
            # Start ROS spin thread
            self._shutdown_flag = False
            self._ros_thread = threading.Thread(target=self._ros_spin, daemon=True)
            self._ros_thread.start()
            
            self._connected = True
            logger.info("ROSTaskBridge started successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start ROSTaskBridge: {e}")
            return False
    
    def _ros_spin(self):
        """Background thread for ROS communication."""
        rate = self._rospy.Rate(10)  # type: ignore[union-attr]  # 10 Hz
        while not self._shutdown_flag and not self._rospy.is_shutdown():  # type: ignore[union-attr]
            try:
                rate.sleep()
            except:
                break
    
    async def stop(self):
        """Stop the ROS task bridge."""
        if self.simulate:
            self._connected = False
            return
        
        try:
            self._shutdown_flag = True
            
            # Cancel any active action
            if self._action_client:
                self._action_client.cancel_all_goals()
            
            # Signal shutdown
            if self._rospy:
                self._rospy.signal_shutdown("SiLA2 bridge stopping")
            
            # Wait for thread
            if self._ros_thread and self._ros_thread.is_alive():
                self._ros_thread.join(timeout=2.0)
            
            self._connected = False
            logger.info("ROSTaskBridge stopped")
            
        except Exception as e:
            logger.warning(f"Error stopping ROSTaskBridge: {e}")
    
    @property
    def is_connected(self) -> bool:
        """Check if connected to ROS."""
        return self._connected
    
    # ─────────────────────────────────────────────────────────────────
    # Task Discovery
    # ─────────────────────────────────────────────────────────────────
    
    def _load_tasks_from_json(self) -> List[TaskInfo]:
        """
        Load tasks directly from JSON files in tasks_path.
        
        Fallback method when ROS service is not available.
        Reads infoSubtask.json files from each task folder.
        
        Returns:
            List of TaskInfo objects for root tasks (no parents)
        """
        if not self.tasks_path:
            logger.warning("tasks_path not configured for JSON fallback")
            return []
        
        tasks_dir = Path(self.tasks_path)
        if not tasks_dir.exists():
            logger.warning(f"Tasks directory not found: {tasks_dir}")
            return []
        
        tasks = []
        all_subtasks: Dict[str, dict] = {}  # id -> subtask data
        
        # First pass: load all subtask JSON files
        for folder in tasks_dir.iterdir():
            if not folder.is_dir():
                continue
            
            info_file = folder / "infoSubtask.json"
            if not info_file.exists():
                continue
            
            try:
                with open(info_file, 'r') as f:
                    data = json.load(f)
                    all_subtasks[data.get('ID', folder.name)] = data
            except Exception as e:
                logger.warning(f"Error reading {info_file}: {e}")
        
        # Second pass: find root tasks (no parents) and build TaskInfo
        for task_id, data in all_subtasks.items():
            # Root tasks have empty directParents
            if data.get('directParents', []):
                continue  # Skip non-root tasks
            
            # Build subtask list from children
            subtasks = []
            children_ids = data.get('directChildren', [])
            for child_id in children_ids:
                if child_id in all_subtasks:
                    child_data = all_subtasks[child_id]
                    subtasks.append(SubtaskInfo(
                        subtask_id=child_id,
                        subtask_name=child_data.get('name', child_id),
                        description=f"Type: {child_data.get('subTaskType', 0)}",
                        estimated_duration=0.0
                    ))
            
            task = TaskInfo(
                root_id=task_id,
                root_name=data.get('name', task_id),
                subtasks=subtasks
            )
            tasks.append(task)
            logger.debug(f"Loaded task from JSON: {task.root_name} ({task.root_id})")
        
        logger.info(f"Loaded {len(tasks)} tasks from JSON files")
        return tasks
    
    async def get_available_tasks(self, refresh: bool = True) -> List[TaskInfo]:
        """
        Get list of available tasks from the robot.
        
        Tries multiple methods:
        1. ROS service /setup1/getSubtasksInfo
        2. Fallback: Read JSON files directly from tasks_path
        
        Args:
            refresh: If True, query ROS; if False, return cached list
            
        Returns:
            List of TaskInfo objects
        """
        if not refresh and self._available_tasks:
            return self._available_tasks
        
        if self.simulate:
            logger.info("[SIM] Getting available tasks")
            await asyncio.sleep(0.2)  # Simulate network delay
            self._available_tasks = self._simulated_tasks
            return self._available_tasks
        
        # Try ROS service first
        if self._service_proxy:
            try:
                # Call service in thread pool to avoid blocking
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    self._call_get_subtasks_service
                )
                
                if response.result.data:
                    # Parse response into TaskInfo objects
                    # Response structure from /setup1/getSubtasksInfo:
                    # - tasksInfo[]: array of tasks
                    #   - rootId.data: string (task ID)
                    #   - rootName.data: string (task name)
                    #   - subtasksInfo[]: array of subtasks
                    #     - id.data: string
                    #     - name.data: string
                    #     - starter.data: bool (True for root tasks)
                    #     - deleted.data: bool
                    #     - type.data: int
                    #     - subTaskType.data: int
                    #     - parentsId[]: array of {data: string}
                    #     - childrenId[]: array of {data: string}
                    
                    self._available_tasks = []
                    for task_msg in response.tasksInfo:
                        root_id = task_msg.rootId.data
                        root_name = task_msg.rootName.data
                        
                        # Build subtask list
                        subtasks = []
                        for st_msg in task_msg.subtasksInfo:
                            # Extract subtask data using correct field names
                            st_id = st_msg.id.data if hasattr(st_msg, 'id') else ""
                            st_name = st_msg.name.data if hasattr(st_msg, 'name') else ""
                            st_type = st_msg.subTaskType.data if hasattr(st_msg, 'subTaskType') else 0
                            st_deleted = st_msg.deleted.data if hasattr(st_msg, 'deleted') else False
                            
                            # Skip deleted subtasks
                            if st_deleted:
                                continue
                            
                            subtask = SubtaskInfo(
                                subtask_id=st_id,
                                subtask_name=st_name,
                                description=f"Type: {st_type}",
                                estimated_duration=0.0
                            )
                            subtasks.append(subtask)
                        
                        task = TaskInfo(
                            root_id=root_id,
                            root_name=root_name,
                            subtasks=subtasks
                        )
                        self._available_tasks.append(task)
                        logger.debug(f"Task: {root_name} ({root_id}) - {len(subtasks)} subtasks")
                    
                    logger.info(f"Discovered {len(self._available_tasks)} tasks via ROS service")
                    return self._available_tasks
                else:
                    logger.warning(f"ROS service returned failure: {response.info.data}")
            except Exception as e:
                logger.warning(f"ROS service call failed: {e}")
        else:
            logger.info("ROS service proxy not available")
        
        # Fallback: Read tasks from JSON files
        logger.info("Using JSON fallback for task discovery")
        self._available_tasks = self._load_tasks_from_json()
        return self._available_tasks
    
    def _call_get_subtasks_service(self):
        """Blocking call to getSubtasksInfo service."""
        if self._service_proxy is None:
            raise RuntimeError("Service proxy not initialized")
        return self._service_proxy()
    
    def get_task_by_id(self, task_id: str) -> Optional[TaskInfo]:
        """Get a task by its ID."""
        for task in self._available_tasks:
            if task.root_id == task_id:
                return task
        return None
    
    def get_task_names(self) -> List[str]:
        """Get list of task names for dropdown."""
        return [task.root_name for task in self._available_tasks]
    
    def get_task_ids(self) -> List[str]:
        """Get list of task IDs."""
        return [task.root_id for task in self._available_tasks]
    
    # ─────────────────────────────────────────────────────────────────
    # Task Execution
    # ─────────────────────────────────────────────────────────────────
    
    async def execute_task(
        self,
        task_id: str,
        command: int = MobileCommand.PLAY,
        tasks_path: str = "",
        end_task_id: str = "",
        timeout: float = 300.0,
        progress_callback: Optional[Callable[[TaskFeedback], None]] = None
    ) -> TaskExecutionResult:
        """
        Execute a task on the robot via /setup1/state_exec action.

        Args:
            task_id: ID of the root task to execute (startTaskID)
            command: int32 command value (0=PLAY, see MobileCommand enum)
            tasks_path: Path to tasks configuration on robot PC (usually empty)
            end_task_id: Last subtask to execute (empty = run all)
            timeout: Maximum execution time in seconds
            progress_callback: Called with TaskFeedback during execution
        """
        start_time = time.time()
        self._current_task = task_id
        self._current_feedback = None
        
        logger.info(f"Executing task: {task_id} (mode={mode.name})")
        
        if self.simulate:
            return await self._simulate_task_execution(task_id, timeout, progress_callback)
        
        if not self._action_client:
            return TaskExecutionResult(
                success=False,
                message="Action server not connected",
                execution_time=0.0
            )
        
        try:
            # Build goal with new API (command int32 replaces state+mode)
            goal = self._ExecutionCommandGoal()
            goal.command = int(command)
            goal.tasksPath = tasks_path or ""
            goal.startTaskID = task_id
            goal.endTaskID = end_task_id or ""
            
            # Define feedback callback
            def feedback_cb(feedback_msg):
                fb = TaskFeedback(
                    current_subtask=feedback_msg.current_subtask,
                    status=feedback_msg.status
                )
                self._current_feedback = fb
                if progress_callback:
                    try:
                        progress_callback(fb)
                    except Exception as e:
                        logger.warning(f"Progress callback error: {e}")
            
            # Send goal
            self._action_client.send_goal(goal, feedback_cb=feedback_cb)
            logger.info(f"Task goal sent: {task_id}")
            
            # Wait for result
            while time.time() - start_time < timeout:
                action_state = self._action_client.get_state()

                if action_state == self._actionlib.GoalStatus.SUCCEEDED:  # type: ignore[union-attr]
                    result = self._action_client.get_result()
                    elapsed = time.time() - start_time

                    logger.info(f"Task completed: {task_id} ({elapsed:.1f}s)")
                    self._current_task = None

                    return TaskExecutionResult(
                        success=result.success,
                        message=result.msg,
                        execution_time=elapsed,
                        final_subtask=self._current_feedback.current_subtask if self._current_feedback else ""
                    )

                elif action_state in [
                    self._actionlib.GoalStatus.ABORTED,  # type: ignore[union-attr]
                    self._actionlib.GoalStatus.REJECTED,  # type: ignore[union-attr]
                    self._actionlib.GoalStatus.PREEMPTED  # type: ignore[union-attr]
                ]:
                    result = self._action_client.get_result()
                    elapsed = time.time() - start_time

                    logger.error(f"Task failed: {task_id} (state={action_state})")
                    self._current_task = None

                    return TaskExecutionResult(
                        success=False,
                        message=result.msg if result else f"Task failed with state {action_state}",
                        execution_time=elapsed,
                        final_subtask=self._current_feedback.current_subtask if self._current_feedback else ""
                    )
                
                await asyncio.sleep(0.1)
            
            # Timeout
            self._action_client.cancel_goal()
            elapsed = time.time() - start_time
            logger.warning(f"Task timeout: {task_id} ({elapsed:.1f}s)")
            self._current_task = None
            
            return TaskExecutionResult(
                success=False,
                message="Task execution timeout",
                execution_time=elapsed,
                final_subtask=self._current_feedback.current_subtask if self._current_feedback else ""
            )
            
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"Task execution error: {e}")
            self._current_task = None
            
            return TaskExecutionResult(
                success=False,
                message=str(e),
                execution_time=elapsed
            )
    
    async def _simulate_task_execution(
        self,
        task_id: str,
        timeout: float,
        progress_callback: Optional[Callable[[TaskFeedback], None]]
    ) -> TaskExecutionResult:
        """Simulate task execution for testing."""
        task = self.get_task_by_id(task_id)
        if not task:
            return TaskExecutionResult(
                success=False,
                message=f"Unknown task: {task_id}",
                execution_time=0.0
            )
        
        logger.info(f"[SIM] Executing task: {task.root_name}")
        start_time = time.time()
        
        for i, subtask in enumerate(task.subtasks):
            # Check timeout
            if time.time() - start_time > timeout:
                return TaskExecutionResult(
                    success=False,
                    message="Timeout during simulation",
                    execution_time=time.time() - start_time,
                    final_subtask=subtask.subtask_id
                )
            
            # Send feedback
            fb = TaskFeedback(
                current_subtask=subtask.subtask_name,
                status="executing",
                progress=(i / len(task.subtasks)) * 100
            )
            self._current_feedback = fb
            
            if progress_callback:
                try:
                    progress_callback(fb)
                except Exception as e:
                    logger.warning(f"Progress callback error: {e}")
            
            logger.info(f"[SIM] Subtask: {subtask.subtask_name}")
            
            # Simulate subtask duration
            await asyncio.sleep(min(subtask.estimated_duration / 5, 2.0))  # Speed up simulation
        
        elapsed = time.time() - start_time
        self._current_task = None
        
        return TaskExecutionResult(
            success=True,
            message=f"Task '{task.root_name}' completed successfully",
            execution_time=elapsed,
            final_subtask=task.subtasks[-1].subtask_name if task.subtasks else ""
        )
    
    async def cancel_task(self) -> bool:
        """Cancel the currently executing task."""
        if not self._current_task:
            logger.warning("No task currently executing")
            return False
        
        logger.info(f"Cancelling task: {self._current_task}")
        
        if self.simulate:
            self._current_task = None
            return True
        
        if self._action_client:
            self._action_client.cancel_goal()
            self._current_task = None
            return True
        
        return False
    
    def get_current_status(self) -> Optional[TaskFeedback]:
        """Get current execution status."""
        return self._current_feedback
    
    @property
    def is_executing(self) -> bool:
        """Check if a task is currently executing."""
        return self._current_task is not None


# ═══════════════════════════════════════════════════════════════════════════
#                              TEST / DEMO
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    """Test the ROS task bridge in simulation mode."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s │ %(levelname)-8s │ %(message)s',
        datefmt='%H:%M:%S'
    )
    
    print("=" * 60)
    print(" ROS Task Bridge - Simulation Test")
    print("=" * 60)
    
    # Create bridge in simulation mode
    bridge = ROSTaskBridge(simulate=True)
    
    # Start
    await bridge.start()
    print(f"\nConnected: {bridge.is_connected}")
    
    # Get available tasks
    print("\n--- Available Tasks ---")
    tasks = await bridge.get_available_tasks()
    for task in tasks:
        print(f"\n[{task.root_id}] {task.root_name}")
        for st in task.subtasks:
            print(f"    - {st.subtask_name} ({st.estimated_duration}s)")
    
    # Execute a task
    print("\n--- Executing Task: transport_plate ---")
    
    def progress_cb(feedback: TaskFeedback):
        print(f"  Progress: {feedback.current_subtask} - {feedback.status} ({feedback.progress:.0f}%)")
    
    result = await bridge.execute_task(
        task_id="transport_plate",
        progress_callback=progress_cb
    )
    
    print(f"\nResult: {'SUCCESS' if result.success else 'FAILED'}")
    print(f"Message: {result.message}")
    print(f"Duration: {result.execution_time:.1f}s")
    
    # Stop
    await bridge.stop()
    print("\nBridge stopped.")


if __name__ == "__main__":
    asyncio.run(main())
