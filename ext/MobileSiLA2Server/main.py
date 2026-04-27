#!/usr/bin/env python3
"""
Mobile Robot SiLA2 Server
=========================

SiLA2 server for the GoFaGo mobile manipulator.
Runs on the Ubuntu robot PC and bridges SiLA2 <-> ROS.

Features:
- Get available tasks from ROS service /setup1/getSubtasksInfo
- Execute tasks via ROS action /setup1/state_exec
- Expose tasks to orchestrator for dropdown selection
- SiLA2Common.ExecuteCommand support for plug-and-play

Usage:
    ./run_server.sh              # Normal mode
    python3 main.py --simulate   # Test without ROS
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from concurrent import futures
from typing import Any, Dict, Optional

import grpc
import yaml

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s │ %(levelname)-8s │ %(name)s │ %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Add parent for common modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import local modules
from ros_task_bridge import ROSTaskBridge, TaskInfo, TaskFeedback, TaskExecutionResult, MobileCommand

# Import generated protobuf
from features import TaskManagement_pb2, TaskManagement_pb2_grpc

# Import SiLA2Common for plug-and-play support
try:
    # When running from MobileSiLA2Server directory
    _sila2_parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, _sila2_parent_dir)
    from sila2_common_servicer import SiLA2CommonServicer, ServerMetadata
    
    # Import the common proto stubs
    # Try multiple locations
    SiLA2Common_pb2 = None
    SiLA2Common_pb2_grpc = None
    
    try:
        # Try direct import first (works if sys.path is set correctly)
        import SiLA2Common_pb2
        import SiLA2Common_pb2_grpc
        logger.info("SiLA2Common stubs imported via sys.path")
    except ImportError:
        # Fallback: Try from parent SiLA2 directory with importlib
        try:
            import importlib.util
            common_pb2_path = os.path.join(_sila2_parent_dir, "SiLA2Common_pb2.py")
            if os.path.exists(common_pb2_path):
                # Load pb2 first and register in sys.modules
                spec = importlib.util.spec_from_file_location("SiLA2Common_pb2", common_pb2_path)
                SiLA2Common_pb2 = importlib.util.module_from_spec(spec)
                sys.modules["SiLA2Common_pb2"] = SiLA2Common_pb2  # Register so grpc can import it
                spec.loader.exec_module(SiLA2Common_pb2)
                
                # Now load grpc (it will find SiLA2Common_pb2 in sys.modules)
                grpc_path = common_pb2_path.replace("_pb2.py", "_pb2_grpc.py")
                spec_grpc = importlib.util.spec_from_file_location("SiLA2Common_pb2_grpc", grpc_path)
                SiLA2Common_pb2_grpc = importlib.util.module_from_spec(spec_grpc)
                spec_grpc.loader.exec_module(SiLA2Common_pb2_grpc)
                logger.info("SiLA2Common stubs loaded via importlib")
        except Exception as e:
            logger.warning(f"Could not load SiLA2Common stubs from parent: {e}")
    
    SILA2_COMMON_AVAILABLE = SiLA2Common_pb2_grpc is not None
    if SILA2_COMMON_AVAILABLE:
        logger.info("SiLA2Common support enabled")
    else:
        logger.warning("SiLA2Common stubs not found - generic command execution disabled")
        
except ImportError as e:
    logger.warning(f"SiLA2Common not available: {e}")
    SILA2_COMMON_AVAILABLE = False
    
    # Define stub class so type hints don't cause NameError
    class ServerMetadata:
        """Stub when SiLA2Common not available."""
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)


# ═══════════════════════════════════════════════════════════════════════════
#                              CONFIG
# ═══════════════════════════════════════════════════════════════════════════

class ServerConfig:
    """Server configuration from YAML."""
    
    def __init__(self, config_path: str = "config.yaml"):
        self.host = "0.0.0.0"
        self.port = 50053
        self.name = "GoFaGo Mobile Robot"
        
        # ROS config
        self.ros_master_uri = "http://localhost:11311"
        self.ros_namespace = "/setup1"
        self.ros_tasks_path = ""
        self.ros_task_timeout = 300.0
        
        if os.path.exists(config_path):
            self._load(config_path)
    
    def _load(self, path: str):
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        
        server = data.get('server', {})
        self.host = server.get('host', self.host)
        self.port = server.get('port', self.port)
        self.name = server.get('name', self.name)
        
        ros = data.get('ros', {})
        self.ros_master_uri = ros.get('master_uri', self.ros_master_uri)
        self.ros_namespace = ros.get('namespace', self.ros_namespace)
        
        task_action = ros.get('task_action', {})
        self.ros_tasks_path = task_action.get('tasks_path', '')
        self.ros_task_timeout = task_action.get('timeout', 300.0)


# ═══════════════════════════════════════════════════════════════════════════
#                          TASK MANAGEMENT SERVICER
# ═══════════════════════════════════════════════════════════════════════════

class TaskManagementServicer(TaskManagement_pb2_grpc.TaskManagementServicer):
    """
    SiLA2 gRPC servicer for task management.
    
    Provides:
    - Get_AvailableTasks: Returns list of tasks for dropdown
    - ExecuteTask: Executes selected task via ROS action
    - GetTaskDetails: Get subtasks of a task
    """
    
    def __init__(self, bridge: ROSTaskBridge, config: ServerConfig):
        self._bridge = bridge
        self._config = config
        self._status = "Idle"
        self._current_task_id = ""
        self._current_subtask = ""
        logger.info("TaskManagementServicer initialized")
    
    # ─────────────────────────────────────────────────────────────────
    # Properties (for dropdown population)
    # ─────────────────────────────────────────────────────────────────
    
    async def Get_AvailableTasks(self, request, context):
        """
        Get list of available tasks for UI dropdown.
        
        Returns tasks that can be executed (root tasks with starter=True).
        """
        logger.info("Get_AvailableTasks called")
        
        try:
            tasks = await self._bridge.get_available_tasks(refresh=True)
            
            response = TaskManagement_pb2.Get_AvailableTasks_Response()
            for task in tasks:
                task_info = TaskManagement_pb2.TaskInfo(
                    task_id=task.root_id,
                    task_name=task.root_name,
                    subtask_count=len(task.subtasks)
                )
                response.tasks.append(task_info)
            
            logger.info(f"Returning {len(response.tasks)} available tasks")
            return response
            
        except Exception as e:
            logger.error(f"Get_AvailableTasks error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return TaskManagement_pb2.Get_AvailableTasks_Response()
    
    async def Get_CurrentTaskId(self, request, context):
        """Get ID of currently executing task."""
        return TaskManagement_pb2.Get_CurrentTaskId_Response(
            current_task_id=self._current_task_id
        )
    
    async def Get_CurrentSubtask(self, request, context):
        """Get name of current subtask being executed."""
        return TaskManagement_pb2.Get_CurrentSubtask_Response(
            current_subtask=self._current_subtask
        )
    
    async def Get_TaskStatus(self, request, context):
        """Get current execution status."""
        return TaskManagement_pb2.Get_TaskStatus_Response(
            task_status=self._status
        )
    
    async def Get_RobotConnectionStatus(self, request, context):
        """Get ROS connection status."""
        connected = self._bridge.is_connected
        status = "Connected" if connected else "Disconnected"
        return TaskManagement_pb2.Get_RobotConnectionStatus_Response(
            connection_status=status
        )
    
    # ─────────────────────────────────────────────────────────────────
    # Commands
    # ─────────────────────────────────────────────────────────────────
    
    async def RefreshTasks(self, request, context):
        """Refresh task list from ROS service."""
        logger.info("RefreshTasks called")
        
        try:
            tasks = await self._bridge.get_available_tasks(refresh=True)
            return TaskManagement_pb2.RefreshTasks_Response(task_count=len(tasks))
        except Exception as e:
            logger.error(f"RefreshTasks error: {e}")
            return TaskManagement_pb2.RefreshTasks_Response(task_count=0)
    
    async def ExecuteTask(self, request, context):
        """
        Execute a task by ID (selected from dropdown).

        Streams ExecuteTask_Response messages:
        - Intermediate: success=True, message="[STEP] <subtask> (<N>%)", execution_time=progress
        - Final: success=<result>, message=<outcome>, execution_time=<seconds>

        NOTE: proto will be refactored to a proper is_final field after regen_proto.sh
        is run on the Ubuntu machine.
        """
        task_id = request.task_id
        mode_str = request.execution_mode or "Normal"

        logger.info(f"ExecuteTask called: task_id={task_id}, mode={mode_str}")

        # Validate task exists
        task = self._bridge.get_task_by_id(task_id)
        if not task:
            logger.warning(f"Task not found: {task_id}")
            yield TaskManagement_pb2.ExecuteTask_Response(
                success=False,
                message=f"Task '{task_id}' not found",
                execution_time=0.0
            )
            return

        # Map execution_mode string → command int32 for new ROS action API
        command_map = {
            "Normal": MobileCommand.PLAY,
            "StepByStep": MobileCommand.STEP,
            "DryRun": MobileCommand.DRY_RUN,
        }
        command = int(command_map.get(mode_str, MobileCommand.PLAY))

        self._status = "Executing"
        self._current_task_id = task_id

        progress_queue: asyncio.Queue = asyncio.Queue()

        def on_progress(feedback: TaskFeedback):
            try:
                asyncio.get_event_loop().call_soon_threadsafe(
                    progress_queue.put_nowait, feedback
                )
            except Exception:
                pass

        exec_task = asyncio.create_task(
            self._bridge.execute_task(
                task_id=task_id,
                command=command,
                timeout=self._config.ros_task_timeout,
                progress_callback=on_progress
            )
        )

        try:
            while not exec_task.done():
                try:
                    feedback = await asyncio.wait_for(progress_queue.get(), timeout=0.5)
                    self._current_subtask = feedback.current_subtask
                    yield TaskManagement_pb2.ExecuteTask_Response(
                        is_final=False,
                        success=True,
                        current_subtask=feedback.current_subtask,
                        progress=feedback.progress,
                        status=feedback.status,
                        message=feedback.current_subtask,
                        execution_time=0.0
                    )
                except asyncio.TimeoutError:
                    continue

            result = await exec_task
            self._status = "Completed" if result.success else "Failed"
            self._current_task_id = ""
            self._current_subtask = ""

            yield TaskManagement_pb2.ExecuteTask_Response(
                is_final=True,
                success=result.success,
                message=result.message,
                execution_time=result.execution_time
            )

        except Exception as e:
            logger.error(f"ExecuteTask error: {e}")
            self._status = "Failed"
            self._current_task_id = ""
            yield TaskManagement_pb2.ExecuteTask_Response(
                is_final=True,
                success=False,
                message=str(e),
                execution_time=0.0
            )
    
    async def CancelTask(self, request, context):
        """Cancel currently executing task."""
        logger.info("CancelTask called")
        
        cancelled = await self._bridge.cancel_task()
        if cancelled:
            self._status = "Cancelled"
            self._current_task_id = ""
            self._current_subtask = ""
        
        return TaskManagement_pb2.CancelTask_Response(cancelled=cancelled)
    
    async def GetTaskDetails(self, request, context):
        """Get detailed info about a task including subtasks."""
        task_id = request.task_id
        logger.debug(f"GetTaskDetails: {task_id}")
        
        task = self._bridge.get_task_by_id(task_id)
        if not task:
            return TaskManagement_pb2.GetTaskDetails_Response(
                task_id=task_id,
                task_name="",
                subtasks_json="[]"
            )

        subtasks_json = json.dumps([
            {
                "id": st.subtask_id,
                "name": st.subtask_name,
                "description": st.description
            }
            for st in task.subtasks
        ])
        
        return TaskManagement_pb2.GetTaskDetails_Response(
            task_id=task.root_id,
            task_name=task.root_name,
            subtasks_json=subtasks_json
        )
    
    async def ConnectToRobot(self, request, context):
        """Connect/reconnect to ROS."""
        uri = request.ros_master_uri or self._config.ros_master_uri
        logger.info(f"ConnectToRobot: {uri}")
        
        try:
            if self._bridge.is_connected:
                await self._bridge.stop()
            
            self._bridge.ros_master_uri = uri
            success = await self._bridge.start()
            
            if success:
                # Auto-refresh tasks on connect
                await self._bridge.get_available_tasks(refresh=True)
            
            return TaskManagement_pb2.ConnectToRobot_Response(
                connected=success,
                message="Connected" if success else "Connection failed"
            )
        except Exception as e:
            logger.error(f"ConnectToRobot error: {e}")
            return TaskManagement_pb2.ConnectToRobot_Response(
                connected=False,
                message=str(e)
            )


# ═══════════════════════════════════════════════════════════════════════════
#                              MAIN SERVER
# ═══════════════════════════════════════════════════════════════════════════

class SiLA2CommonAdapter:
    """
    Adapter to make SiLA2CommonServicer work with gRPC.
    
    Translates between the servicer's dict responses and actual proto messages.
    """
    
    def __init__(self, servicer: 'TaskManagementServicer', bridge: ROSTaskBridge, metadata: ServerMetadata):
        self._servicer = servicer
        self._bridge = bridge
        self._metadata = metadata
        self._start_time = asyncio.get_event_loop().time()
    
    async def GetServerInfo(self, request, context):
        """Return server metadata."""
        return SiLA2Common_pb2.ServerInfoResponse(
            server_name=self._metadata.server_name,
            server_type=self._metadata.server_type,
            vendor=self._metadata.vendor,
            server_version=self._metadata.server_version,
            sila_version="2.0",
            description=self._metadata.description,
            hardware_connected=self._bridge.is_connected,
            hardware_status="connected" if self._bridge.is_connected else "disconnected"
        )
    
    async def GetFeatures(self, request, context):
        """Return available features."""
        # TaskManagement feature
        feature = SiLA2Common_pb2.Feature(
            identifier="TaskManagement",
            display_name="Task Management",
            description="Mobile robot task execution",
            version="1.0"
        )
        
        # Add commands
        commands = [
            ("RefreshTasks", "Refresh task list from ROS"),
            ("Get_AvailableTasks", "Get list of available tasks for dropdown"),
            ("ExecuteTask", "Execute a task by ID"),
            ("CancelTask", "Cancel current task"),
            ("GetTaskDetails", "Get task subtasks"),
            ("ConnectToRobot", "Connect to ROS master")
        ]
        
        for cmd_id, desc in commands:
            feature.commands.append(SiLA2Common_pb2.Command(
                identifier=cmd_id,
                display_name=cmd_id,
                description=desc
            ))
        
        return SiLA2Common_pb2.FeaturesResponse(features=[feature])
    
    async def GetStatus(self, request, context):
        """Return current status."""
        return SiLA2Common_pb2.StatusResponse(
            status="running",
            server_online=True,
            hardware_online=self._bridge.is_connected,
            hardware_status="connected" if self._bridge.is_connected else "disconnected"
        )
    
    async def ExecuteCommand(self, request, context):
        """
        Execute any command generically.
        
        This is the key method for plug-and-play support!
        """
        feature = request.feature
        command = request.command
        params = dict(request.parameters)
        
        logger.info(f"ExecuteCommand: {feature}/{command} params={params}")
        
        try:
            # Route to appropriate handler
            if command == "RefreshTasks":
                tasks = await self._bridge.get_available_tasks(refresh=True)
                # Return tasks as JSON in the response
                import json
                tasks_json = json.dumps([{
                    "id": t.root_id,
                    "name": t.root_name,
                    "subtask_count": len(t.subtasks)
                } for t in tasks])
                yield SiLA2Common_pb2.ExecuteCommandResponse(
                    success=True,
                    is_intermediate=False,
                    result={
                        "task_count": str(len(tasks)),
                        "tasks": tasks_json
                    }
                )
                
            elif command == "ExecuteTask":
                task_id = params.get("TaskId", params.get("task_id", ""))
                mode_str = params.get("ExecutionMode", "Normal")

                if not task_id:
                    yield SiLA2Common_pb2.ExecuteCommandResponse(
                        success=False,
                        error="TaskId parameter required"
                    )
                    return

                command_map = {
                    "Normal": MobileCommand.PLAY,
                    "StepByStep": MobileCommand.STEP,
                    "DryRun": MobileCommand.DRY_RUN,
                }
                ros_command = int(command_map.get(mode_str, MobileCommand.PLAY))

                progress_queue: asyncio.Queue = asyncio.Queue()

                def on_progress(fb: TaskFeedback):
                    try:
                        asyncio.get_event_loop().call_soon_threadsafe(
                            progress_queue.put_nowait, fb
                        )
                    except Exception:
                        pass

                exec_task = asyncio.create_task(
                    self._bridge.execute_task(
                        task_id=task_id,
                        command=ros_command,
                        timeout=300.0,
                        progress_callback=on_progress
                    )
                )
                
                # Stream progress
                while not exec_task.done():
                    try:
                        fb = await asyncio.wait_for(progress_queue.get(), timeout=0.5)
                        yield SiLA2Common_pb2.ExecuteCommandResponse(
                            success=True,
                            is_intermediate=True,
                            progress=int(fb.progress),
                            status=fb.current_subtask
                        )
                    except asyncio.TimeoutError:
                        continue
                
                # Final result
                result = await exec_task
                yield SiLA2Common_pb2.ExecuteCommandResponse(
                    success=result.success,
                    is_intermediate=False,
                    result={
                        "success": str(result.success),
                        "message": result.message,
                        "execution_time": str(result.execution_time)
                    },
                    error="" if result.success else result.message
                )
                
            elif command == "CancelTask":
                cancelled = await self._bridge.cancel_task()
                yield SiLA2Common_pb2.ExecuteCommandResponse(
                    success=cancelled,
                    is_intermediate=False,
                    result={"cancelled": str(cancelled)}
                )
                
            elif command == "GetTaskDetails":
                task_id = params.get("TaskId", params.get("task_id", ""))
                task = self._bridge.get_task_by_id(task_id)
                
                if task:
                    subtasks = json.dumps([st.subtask_name for st in task.subtasks])
                    yield SiLA2Common_pb2.ExecuteCommandResponse(
                        success=True,
                        is_intermediate=False,
                        result={
                            "task_id": task.root_id,
                            "task_name": task.root_name,
                            "subtasks": subtasks
                        }
                    )
                else:
                    yield SiLA2Common_pb2.ExecuteCommandResponse(
                        success=False,
                        error=f"Task not found: {task_id}"
                    )
                    
            elif command == "ConnectToRobot":
                uri = params.get("RosMasterUri", "")
                if uri:
                    self._bridge.ros_master_uri = uri
                
                if self._bridge.is_connected:
                    await self._bridge.stop()
                
                success = await self._bridge.start()
                if success:
                    await self._bridge.get_available_tasks(refresh=True)
                
                yield SiLA2Common_pb2.ExecuteCommandResponse(
                    success=success,
                    is_intermediate=False,
                    result={"connected": str(success)}
                )
                
            elif command == "Get_AvailableTasks":
                # Special: return task list for dropdown
                tasks = await self._bridge.get_available_tasks(refresh=False)
                task_list = json.dumps([{"id": t.root_id, "name": t.root_name} for t in tasks])
                yield SiLA2Common_pb2.ExecuteCommandResponse(
                    success=True,
                    is_intermediate=False,
                    result={"tasks": task_list, "count": str(len(tasks))}
                )
                
            else:
                yield SiLA2Common_pb2.ExecuteCommandResponse(
                    success=False,
                    error=f"Unknown command: {command}"
                )
                
        except Exception as e:
            logger.exception(f"ExecuteCommand error: {e}")
            yield SiLA2Common_pb2.ExecuteCommandResponse(
                success=False,
                error=str(e)
            )
    
    async def GetProperty(self, request, context):
        """Get property value."""
        feature = request.feature
        prop = request.property
        
        if prop == "AvailableTasks":
            tasks = await self._bridge.get_available_tasks(refresh=False)
            return SiLA2Common_pb2.PropertyResponse(
                success=True,
                value=json.dumps([t.root_id for t in tasks]),
                data_type="List[String]"
            )
        elif prop == "CurrentTaskId":
            return SiLA2Common_pb2.PropertyResponse(
                success=True,
                value=self._servicer._current_task_id or "",
                data_type="String"
            )
        elif prop == "TaskStatus":
            return SiLA2Common_pb2.PropertyResponse(
                success=True,
                value=self._servicer._status,
                data_type="String"
            )
        
        return SiLA2Common_pb2.PropertyResponse(
            success=False,
            error=f"Unknown property: {prop}"
        )


class MobileSiLA2Server:
    """Main SiLA2 server for mobile robot."""

    def __init__(self, config: ServerConfig, simulate: bool = False):
        self.config = config
        self.simulate = simulate

        self._bridge = ROSTaskBridge(
            simulate=simulate,
            ros_master_uri=config.ros_master_uri,
            namespace=config.ros_namespace,
            tasks_path=config.ros_tasks_path
        )

        self._server: Optional[grpc.aio.Server] = None
        self._servicer: Optional[TaskManagementServicer] = None
        self._mdns_registry = None
    
    async def start(self):
        """Start the SiLA2 server."""
        logger.info("=" * 60)
        logger.info(" MobileSiLA2Server Starting")
        logger.info("=" * 60)

        logger.info("Connecting to ROS...")
        if not await self._bridge.start():
            if not self.simulate:
                logger.warning("ROS connection failed - continuing anyway")

        tasks = await self._bridge.get_available_tasks(refresh=True)
        logger.info(f"Loaded {len(tasks)} tasks from ROS")
        for t in tasks:
            logger.info(f"  - [{t.root_id[:8]}...] {t.root_name}")

        self._server = grpc.aio.server(futures.ThreadPoolExecutor(max_workers=10))

        self._servicer = TaskManagementServicer(self._bridge, self.config)
        TaskManagement_pb2_grpc.add_TaskManagementServicer_to_server(
            self._servicer, self._server
        )

        if SILA2_COMMON_AVAILABLE:
            metadata = ServerMetadata(
                server_name=self.config.name,
                server_type="mobile_robot",
                vendor="JOiiNT Lab",
                server_version="1.0.0",
                description="GoFaGo Mobile Manipulator - Task-based robot control via ROS"
            )
            self._common_adapter = SiLA2CommonAdapter(self._servicer, self._bridge, metadata)
            SiLA2Common_pb2_grpc.add_SiLA2ServerInfoServicer_to_server(
                self._common_adapter, self._server
            )
            logger.info("SiLA2Common service added (ExecuteCommand enabled)")

        address = f"{self.config.host}:{self.config.port}"
        self._server.add_insecure_port(address)
        await self._server.start()

        # mDNS registration for plug-and-play discovery
        try:
            from sila2_mdns_registry import SiLA2ServerRegistry
            self._mdns_registry = SiLA2ServerRegistry(
                name=self.config.name,
                port=self.config.port,
                features=["TaskManagement"],
                vendor="JOiiNT Lab"
            )
            await self._mdns_registry.register()
            logger.info(f"mDNS registered: {self.config.name} on port {self.config.port}")
        except Exception as e:
            logger.warning(f"mDNS registration failed (discovery via config still works): {e}")

        logger.info("=" * 60)
        logger.info(f" SiLA2 Server running on {address}")
        logger.info(f" Mode: {'SIMULATION' if self.simulate else 'PRODUCTION'}")
        logger.info(f" SiLA2Common: {'ENABLED' if SILA2_COMMON_AVAILABLE else 'DISABLED'}")
        logger.info("=" * 60)

        return True
    
    async def stop(self):
        """Stop the server."""
        logger.info("Stopping server...")

        if self._mdns_registry:
            try:
                await self._mdns_registry.unregister()
            except Exception as e:
                logger.warning(f"mDNS unregistration error: {e}")

        if self._server:
            await self._server.stop(grace=5.0)

        await self._bridge.stop()
        logger.info("Server stopped.")
    
    async def wait(self):
        """Wait for server to terminate."""
        if self._server:
            await self._server.wait_for_termination()


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Mobile Robot SiLA2 Server")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--simulate", action="store_true", help="Run in simulation mode")
    parser.add_argument("--production", action="store_true", help="Run in production mode (real ROS)")
    args = parser.parse_args()
    
    # Default to simulation unless --production specified
    simulate = not args.production and (args.simulate or True)
    if args.production:
        simulate = False
    
    config = ServerConfig(args.config)
    server = MobileSiLA2Server(config, simulate=simulate)
    
    # Handle shutdown
    loop = asyncio.get_event_loop()
    
    def shutdown_handler():
        logger.info("Shutdown signal received")
        asyncio.create_task(server.stop())
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass
    
    try:
        await server.start()
        await server.wait()
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
