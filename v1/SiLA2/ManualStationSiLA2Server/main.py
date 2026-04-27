#!/usr/bin/env python3
"""
Manual Station SiLA2 Server
===========================

Virtual station for operator interactions in automated workflows.
Handles manual tasks, tip refill notifications, and confirmations.

Usage:
    python main.py [--port PORT] [--host HOST]
"""

import asyncio
import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from concurrent import futures

import grpc
import httpx
import yaml

# WebApp URL for operator notifications
WEBAPP_URL = os.environ.get("WEBAPP_URL", "http://127.0.0.1:5000")

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

# Add parent directory for SiLA2 common modules
_sila2_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _sila2_path not in sys.path:
    sys.path.insert(0, _sila2_path)

# mDNS registration
try:
    from sila2_mdns_registry import SiLA2ServerRegistry
    MDNS_AVAILABLE = True
except ImportError:
    MDNS_AVAILABLE = False

# SiLA2Common for Plug & Play
try:
    import SiLA2Common_pb2 as common_pb2
    import SiLA2Common_pb2_grpc as common_grpc
    SILA2_COMMON_AVAILABLE = True
except ImportError:
    SILA2_COMMON_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("SiLA2Common not available - generic command execution disabled")

from src import ManualStationService_pb2_grpc as pb2_grpc
from src import ManualStationService_pb2 as pb2
from src.manual_station_servicer import ManualStationServicer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#                              SILA2 COMMON ADAPTER
# ═══════════════════════════════════════════════════════════════════════════

class SiLA2CommonAdapter(common_grpc.SiLA2ServerInfoServicer if SILA2_COMMON_AVAILABLE else object):
    """Adapter for SiLA2Common Plug & Play support."""
    
    def __init__(self, servicer: ManualStationServicer):
        self._servicer = servicer
        self._start_time = asyncio.get_event_loop().time()
    
    async def GetServerInfo(self, request, context):
        _features_dir = str(Path(__file__).parent / "features")
        description = "Operator interaction station for manual tasks in automated workflows"
        try:
            from sila2_xml_parser import features_from_xml_dir
            xml_features = features_from_xml_dir(_features_dir)
            if xml_features:
                description = xml_features[0].get('description', description)
        except Exception:
            pass

        return common_pb2.ServerInfoResponse(
            server_name="Manual Station",
            server_type="operator_interface",
            vendor="BicoccaLab",
            server_version="1.0.0",
            sila_version="2.0",
            description=description,
            host="localhost"
        )

    async def GetFeatures(self, request, context):
        """Return available features — read from features/*.sila.xml."""
        _features_dir = str(Path(__file__).parent / "features")
        try:
            from sila2_xml_parser import features_from_xml_dir, build_proto_features
            xml_features = features_from_xml_dir(_features_dir)
            proto_features = build_proto_features(xml_features, common_pb2)
        except Exception as e:
            logger.warning(f"Could not load features from XML: {e}")
            proto_features = []
        return common_pb2.FeaturesResponse(features=proto_features)

    async def GetStatus(self, request, context):
        pending = len(self._servicer.get_pending_tasks())
        return common_pb2.StatusResponse(
            status="running",
            server_online=True,
            hardware_online=True,
            hardware_status=f"{pending} pending tasks",
            details={"pending_tasks": str(pending)}
        )
    
    async def ExecuteCommand(self, request, responseStream, context):
        """Execute ManualStation commands generically."""
        cmd = request.command
        params = dict(request.parameters)
        
        try:
            result = {}
            
            # Accept multiple aliases for RequestOperatorTask
            if cmd in ("RequestTask", "RequestOperatorTask", "RequestManualTask"):
                req = pb2.RequestOperatorTaskRequest(
                    task_type=params.get("task_type", "manual"),
                    task_description=params.get("description", params.get("task_description", "")),
                    source_instrument=params.get("source_instrument", "orchestrator"),
                    priority=params.get("priority", "normal"),
                    timeout_seconds=int(params.get("timeout_seconds", "0"))
                )
                # RequestOperatorTask is a streaming RPC - collect first response
                async for resp in self._servicer.RequestOperatorTask(req, context):
                    result = {"task_id": resp.task_id, "status": resp.status, "message": resp.message}
                    break  # Get first response (task created)
            
            elif cmd == "ConfirmTaskComplete":
                req = pb2.ConfirmTaskCompleteRequest(
                    task_id=params.get("task_id", ""),
                    notes=params.get("notes", params.get("operator_notes", ""))
                )
                resp = await self._servicer.ConfirmTaskComplete(req, context)
                result = {"success": str(resp.success), "message": resp.message}
            
            elif cmd in ("GetPendingTasks", "GetActiveTasks"):
                req = pb2.GetActiveTasksRequest()
                resp = await self._servicer.GetActiveTasks(req, context)
                result = {"count": str(len(resp.tasks)), "task_ids": ",".join(t.task_id for t in resp.tasks)}
            
            elif cmd == "SendNotification":
                req = pb2.SendNotificationRequest(
                    message=params.get("message", ""),
                    level=params.get("level", "info"),
                    source_instrument=params.get("source_instrument", "orchestrator")
                )
                resp = await self._servicer.SendNotification(req, context)
                result = {"success": str(resp.success)}
            
            elif cmd == "CancelTask":
                req = pb2.CancelTaskRequest(
                    task_id=params.get("task_id", ""),
                    reason=params.get("reason", "")
                )
                resp = await self._servicer.CancelTask(req, context)
                result = {"success": str(resp.success), "message": resp.message}
            
            else:
                raise ValueError(f"Unknown command: {cmd}")
            
            await responseStream.write(common_pb2.ExecuteCommandResponse(
                success=True,
                result=result,
                is_intermediate=False,
                progress=100
            ))
            
        except Exception as e:
            await responseStream.write(common_pb2.ExecuteCommandResponse(
                success=False,
                error=str(e),
                is_intermediate=False
            ))
    
    async def GetProperty(self, request, context):
        prop = request.property_name
        
        if prop == "PendingTaskCount":
            return common_pb2.PropertyResponse(
                property_name=prop,
                value=str(len(self._servicer.get_pending_tasks())),
                value_type="int"
            )
        
        return common_pb2.PropertyResponse(
            property_name=prop,
            error=f"Unknown property: {prop}"
        )


def load_config() -> dict:
    """Load configuration from config.yaml."""
    config_path = Path(__file__).parent / "config.yaml"
    
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f)
    
    # Default config
    return {
        "server": {
            "host": "0.0.0.0",
            "port": 50055
        },
        "logging": {
            "level": "INFO"
        }
    }


async def serve(host: str, port: int):
    """Start the gRPC server."""
    
    # Create servicer
    servicer = ManualStationServicer()
    
    # Helper to send notification to WebApp in background thread
    def notify_webapp(task):
        """Send task notification to WebApp (runs in thread)."""
        try:
            # Map task priority to webapp priority
            priority_map = {
                "urgent": "urgent",
                "high": "warning", 
                "normal": "warning",
                "low": "info"
            }
            
            notification = {
                "id": hash(task.task_id) & 0x7FFFFFFF,  # Positive int ID
                "title": f"🔔 {task.task_type.replace('_', ' ').title()}: {task.source_instrument}",
                "message": task.description,
                "priority": priority_map.get(task.priority.lower(), "warning"),
                "requires_action": True,
                "action": "operator_task",
                "params": {
                    "task_id": task.task_id,
                    "task_type": task.task_type,
                    "source_instrument": task.source_instrument
                }
            }
            
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(f"{WEBAPP_URL}/api/operator/notify", json=notification)
                if resp.status_code == 200:
                    logger.info(f"Task {task.task_id} sent to WebApp operator page")
                else:
                    logger.warning(f"WebApp notification failed: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Could not notify WebApp: {e}")
    
    # Callback that prints to console AND sends to WebApp
    def on_task_created(task):
        print(f"\n{'='*60}")
        print(f"  🔔 OPERATOR TASK REQUIRED")
        print(f"{'='*60}")
        print(f"  Task ID:    {task.task_id}")
        print(f"  Type:       {task.task_type}")
        print(f"  Priority:   {task.priority.upper()}")
        print(f"  From:       {task.source_instrument}")
        print(f"  Description: {task.description}")
        print(f"{'='*60}")
        print(f"  To confirm: call ConfirmTaskComplete with task_id='{task.task_id}'")
        print(f"{'='*60}\n")
        
        # Send to WebApp in background thread (non-blocking)
        threading.Thread(target=notify_webapp, args=(task,), daemon=True).start()
    
    def on_task_completed(task):
        print(f"\n  ✓ Task {task.task_id} completed by operator")
        if task.operator_notes:
            print(f"    Notes: {task.operator_notes}")
        print()
    
    def on_notification(notification):
        level_icon = {"info": "ℹ️", "warning": "⚠️", "error": "❌"}.get(notification.level, "📢")
        print(f"\n  {level_icon} [{notification.source_instrument}] {notification.message}\n")
    
    servicer.set_on_task_created(on_task_created)
    servicer.set_on_task_completed(on_task_completed)
    servicer.set_on_notification(on_notification)
    
    # Create server
    server = grpc.aio.server(futures.ThreadPoolExecutor(max_workers=10))
    pb2_grpc.add_ManualStationServiceServicer_to_server(servicer, server)
    
    # Add SiLA2Common for Plug & Play support
    common_adapter = None
    if SILA2_COMMON_AVAILABLE:
        common_adapter = SiLA2CommonAdapter(servicer)
        common_grpc.add_SiLA2ServerInfoServicer_to_server(common_adapter, server)
        logger.info("SiLA2Common service enabled (Plug & Play)")
    
    listen_addr = f"{host}:{port}"
    try:
        bound_port = server.add_insecure_port(listen_addr)
    except RuntimeError as exc:
        logger.error("Failed to bind gRPC server to %s", listen_addr)
        if os.name == "nt":
            logger.error(
                "On Windows this can be caused by excluded TCP port ranges. "
                "Check with: netsh interface ipv4 show excludedportrange protocol=tcp"
            )
        raise

    if bound_port == 0:
        raise RuntimeError(
            f"Failed to bind to {listen_addr}. "
            "Port may already be in use or blocked by OS policy."
        )
    
    # Start server
    await server.start()
    
    # Register on mDNS for automatic discovery
    mdns_registry = None
    if MDNS_AVAILABLE:
        _features_dir = str(Path(__file__).parent / "features")
        try:
            from sila2_xml_parser import features_from_xml_dir
            _feature_ids = [f['identifier'] for f in features_from_xml_dir(_features_dir)]
        except Exception:
            _feature_ids = ["ManualStation"]
        _feature_ids.append("SiLA2Common")

        mdns_registry = SiLA2ServerRegistry(
            name="ManualStation",
            port=port,
            features=_feature_ids,
            vendor="BicoccaLab",
            version="1.0.0",
            server_type="Real"
        )
        await mdns_registry.register()
    
    print()
    print("=" * 60)
    print("  MANUAL STATION - SiLA2 Server")
    print("=" * 60)
    print(f"  gRPC Server:  {listen_addr}")
    print(f"  mDNS:         {'Registered' if mdns_registry and mdns_registry.is_registered else 'Disabled'}")
    print(f"  Plug & Play:  {'Enabled' if common_adapter else 'Disabled'}")
    print(f"  Status:       Running")
    print()
    print("  This server handles:")
    print("    - Operator task requests from workflows")
    print("    - Tip refill notifications")
    print("    - Quality control checkpoints")
    print("    - Manual operation confirmations")
    print()
    print("  Waiting for tasks...")
    print("=" * 60)
    print()
    
    # Wait for termination (Ctrl+C or SIGTERM)
    try:
        await server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutdown signal received")
    finally:
        # Unregister from mDNS
        if mdns_registry:
            await mdns_registry.unregister()
        
        # Graceful shutdown
        logger.info("Shutting down server...")
        await server.stop(grace=5)
        logger.info("Server stopped")


def main():
    parser = argparse.ArgumentParser(description="Manual Station SiLA2 Server")
    parser.add_argument("--host", default=None, help="Host to bind to")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on")
    parser.add_argument("--config", help="Path to config file")
    args = parser.parse_args()
    
    # Load config
    config = load_config()
    
    # Override with command line args
    host = args.host or config.get("server", {}).get("host", "0.0.0.0")
    port = args.port or config.get("server", {}).get("port", 50360)
    
    # Set logging level
    log_level = config.get("logging", {}).get("level", "INFO")
    logging.getLogger().setLevel(getattr(logging, log_level))
    
    # Run server
    try:
        asyncio.run(serve(host, port))
    except KeyboardInterrupt:
        print("\nServer stopped by user")


if __name__ == "__main__":
    main()
