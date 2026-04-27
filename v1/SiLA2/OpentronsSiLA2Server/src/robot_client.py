"""
Robot Client - HTTP Client for Opentrons Flex API
=================================================

Asynchronous HTTP client for communicating with Opentrons Flex robot.
Implements the Opentrons HTTP API v2.
"""

import asyncio
import base64
import logging
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class RobotClient:
    """
    HTTP Client for communicating with Opentrons Flex robot.
    
    Features:
        - Async HTTP communication
        - Protocol upload and execution
        - Run management (create, play, pause, stop)
        - Module status monitoring
        - Image extraction from run logs
        - Tip usage analysis for partial runs
        - Connection retry with configurable backoff
    """
    
    def __init__(
        self, 
        host: str = "169.254.161.83", 
        port: int = 31950, 
        timeout: float = 30.0, 
        local_address: Optional[str] = None
    ):
        """
        Initialize the robot client.
        
        Args:
            host: Robot IP address
            port: Robot HTTP API port
            timeout: Request timeout in seconds
            local_address: Local interface to bind (for link-local routing)
        """
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.timeout = timeout
        self.local_address = local_address
        self._client: Optional[httpx.AsyncClient] = None
        self._current_run_id: Optional[str] = None
        
    # ═══════════════════════════════════════════════════════════════════
    #                      CONTEXT MANAGER
    # ═══════════════════════════════════════════════════════════════════
        
    async def __aenter__(self):
        await self.connect()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()
        
    # ═══════════════════════════════════════════════════════════════════
    #                      CONNECTION MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════
        
    async def connect(self):
        """Initialize the HTTP client."""
        transport = None
        if self.local_address:
            transport = httpx.AsyncHTTPTransport(local_address=self.local_address)
        
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={"Opentrons-Version": "*"},
            transport=transport
        )
        
        addr_info = f" via {self.local_address}" if self.local_address else ""
        logger.info(f"Connected to Opentrons at {self.base_url}{addr_info}")
        
    async def disconnect(self):
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.info("Disconnected from robot")
            
    @property
    def is_connected(self) -> bool:
        """Check if client is connected."""
        return self._client is not None
    
    async def connect_with_retry(
        self, 
        max_retries: int = 5, 
        retry_delay: float = 2.0,
        on_retry: Optional[Callable] = None
    ) -> bool:
        """
        Attempt to connect with retry logic.
        
        Args:
            max_retries: Maximum number of connection attempts
            retry_delay: Delay between retries in seconds
            on_retry: Optional callback called on each retry
            
        Returns:
            True if connected successfully, False otherwise
        """
        for attempt in range(max_retries):
            try:
                await self.connect()
                health = await self.get_health()
                if "error" not in health:
                    logger.info(f"Connected after {attempt + 1} attempt(s)")
                    return True
            except Exception as e:
                logger.warning(f"Connection attempt {attempt + 1}/{max_retries} failed: {e}")
                
            if attempt < max_retries - 1:
                if on_retry:
                    await on_retry(attempt + 1, max_retries)
                await asyncio.sleep(retry_delay)
                
        logger.error(f"Failed to connect after {max_retries} attempts")
        return False
            
    # ═══════════════════════════════════════════════════════════════════
    #                         HEALTH & INFO
    # ═══════════════════════════════════════════════════════════════════
    
    async def get_health(self) -> Dict[str, Any]:
        """Get robot health status."""
        try:
            response = await self._client.get("/health")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return {"status": "disconnected", "error": str(e)}
            
    async def get_robot_info(self) -> Dict[str, Any]:
        """Get robot information (name, serial, version)."""
        try:
            response = await self._client.get("/robot/settings")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Failed to get robot info: {e}")
            return {}
            
    # ═══════════════════════════════════════════════════════════════════
    #                         PROTOCOL MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════
    
    async def upload_protocol(self, file_path: str) -> str:
        """
        Upload a Python protocol file to the robot.
        
        Args:
            file_path: Path to the protocol file
            
        Returns:
            Protocol ID
        """
        filename = os.path.basename(file_path)
        with open(file_path, 'rb') as f:
            files = {"files": (filename, f, "application/x-python")}
            response = await self._client.post("/protocols", files=files)
            
        if not response.is_success:
            raise Exception(f"Protocol upload failed: {response.status_code} - {response.text}")
            
        data = response.json()
        protocol_id = data.get("data", {}).get("id")
        
        if not protocol_id:
            raise Exception("Protocol ID not found in response")
            
        logger.info(f"Protocol uploaded: {protocol_id}")
        return protocol_id
        
    async def upload_protocol_content(self, content: str, filename: str = "protocol.py") -> str:
        """
        Upload protocol content directly.
        
        Args:
            content: Python protocol code
            filename: Filename for the protocol
            
        Returns:
            Protocol ID
        """
        files = {"files": (filename, content.encode('utf-8'), "application/x-python")}
        response = await self._client.post("/protocols", files=files)
        
        if not response.is_success:
            raise Exception(f"Protocol upload failed: {response.status_code} - {response.text}")
            
        data = response.json()
        protocol_id = data.get("data", {}).get("id")
        
        if not protocol_id:
            raise Exception("Protocol ID not found in response")
            
        logger.info(f"Protocol uploaded: {protocol_id}")
        return protocol_id
        
    async def delete_protocol(self, protocol_id: str):
        """Delete a protocol from the robot."""
        try:
            await self._client.delete(f"/protocols/{protocol_id}")
            logger.debug(f"Protocol deleted: {protocol_id}")
        except Exception as e:
            logger.warning(f"Failed to delete protocol {protocol_id}: {e}")
            
    # ═══════════════════════════════════════════════════════════════════
    #                           RUN MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════
    
    async def create_run(self, protocol_id: str) -> str:
        """
        Create a new run from a protocol.
        
        Args:
            protocol_id: ID of the uploaded protocol
            
        Returns:
            Run ID
        """
        payload = {"data": {"protocolId": protocol_id}}
        response = await self._client.post("/runs", json=payload)
        
        # Handle zombie run conflict
        if response.status_code == 409:
            logger.warning("Zombie run detected, cleaning up...")
            await self._deactivate_current_run()
            response = await self._client.post("/runs", json=payload)
            
        if not response.is_success:
            raise Exception(f"Run creation failed: {response.status_code} - {response.text}")
            
        data = response.json()
        run_id = data.get("data", {}).get("id")
        
        if not run_id:
            raise Exception("Run ID not found in response")
            
        self._current_run_id = run_id
        logger.info(f"Run created: {run_id}")
        return run_id
        
    async def _deactivate_current_run(self):
        """Deactivate any currently active run."""
        try:
            response = await self._client.get("/runs?pageLength=1")
            if response.is_success:
                data = response.json()
                runs = data.get("data", [])
                for run in runs:
                    if run.get("current"):
                        await self.dismiss_run(run["id"])
        except Exception as e:
            logger.warning(f"Failed to deactivate current run: {e}")
            
    async def dismiss_run(self, run_id: str):
        """Dismiss (deactivate) a run."""
        try:
            payload = {"data": {"current": False}}
            await self._client.patch(f"/runs/{run_id}", json=payload)
            logger.info(f"Run dismissed: {run_id}")
        except Exception as e:
            logger.warning(f"Failed to dismiss run {run_id}: {e}")
            
    # ═══════════════════════════════════════════════════════════════════
    #                          RUN CONTROL
    # ═══════════════════════════════════════════════════════════════════
    
    async def play_run(self, run_id: str):
        """Start or resume a run."""
        await self._send_action(run_id, "play")
        logger.info(f"Run started: {run_id}")
        
    async def pause_run(self, run_id: str):
        """Pause a running run."""
        await self._send_action(run_id, "pause")
        logger.info(f"Run paused: {run_id}")
        
    async def stop_run(self, run_id: str):
        """Stop a run."""
        await self._send_action(run_id, "stop")
        logger.info(f"Run stopped: {run_id}")
        
    async def _send_action(self, run_id: str, action: str):
        """Send an action to a run."""
        payload = {"data": {"actionType": action}}
        response = await self._client.post(f"/runs/{run_id}/actions", json=payload)
        if not response.is_success:
            logger.warning(f"Action {action} failed: {response.status_code}")
            
    # ═══════════════════════════════════════════════════════════════════
    #                          RUN STATUS
    # ═══════════════════════════════════════════════════════════════════
    
    async def get_run_status(self, run_id: str) -> str:
        """Get the current status of a run."""
        try:
            response = await self._client.get(f"/runs/{run_id}")
            if not response.is_success:
                return "error"
            data = response.json()
            return data.get("data", {}).get("status", "unknown")
        except Exception as e:
            logger.error(f"Failed to get run status: {e}")
            return "connection-error"
            
    async def get_run_commands(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Get the commands log for a run."""
        try:
            response = await self._client.get(
                f"/runs/{run_id}/commands",
                params={"cursor": 0, "pageLength": 10000}
            )
            if response.is_success:
                return response.json()
        except Exception as e:
            logger.error(f"Failed to get run commands: {e}")
        return None
        
    async def wait_for_run_completion(
        self, 
        run_id: str, 
        poll_interval: float = 1.0,
        on_status_change: Optional[Callable] = None
    ) -> str:
        """
        Wait for a run to complete.
        
        Args:
            run_id: Run ID to monitor
            poll_interval: Polling interval in seconds
            on_status_change: Optional callback for status changes
            
        Returns:
            Final status string
        """
        last_status = ""
        terminal_states = {"succeeded", "failed", "stopped"}
        
        while True:
            status = await self.get_run_status(run_id)
            
            if status != last_status:
                logger.info(f"Run status: {status}")
                if on_status_change:
                    await on_status_change(status)
                last_status = status
                
            # Check for terminal state
            if status in terminal_states or "failed" in status or "stopped" in status:
                return status
                
            # Handle "finishing" state
            if status == "finishing":
                await asyncio.sleep(2.0)
                continue
                
            await asyncio.sleep(poll_interval)
            
    # ═══════════════════════════════════════════════════════════════════
    #                          HOME & LIGHTS
    # ═══════════════════════════════════════════════════════════════════
    
    async def home(self):
        """Home all robot axes."""
        try:
            response = await self._client.post("/robot/home", json={"target": "robot"})
            if response.is_success:
                logger.info("Robot homed")
            else:
                logger.warning(f"Home failed: {response.status_code}")
        except Exception as e:
            logger.error(f"Home error: {e}")
            
    async def set_lights(self, on: bool) -> bool:
        """Turn robot lights on or off."""
        try:
            response = await self._client.post("/robot/lights", json={"on": on})
            return response.is_success
        except Exception as e:
            logger.warning(f"Failed to set lights: {e}")
            return False
    
    async def get_lights(self) -> Optional[bool]:
        """Get robot lights status."""
        try:
            response = await self._client.get("/robot/lights")
            if response.is_success:
                return response.json().get("on", False)
            return None
        except Exception as e:
            logger.warning(f"Failed to get lights: {e}")
            return None
            
    # ═══════════════════════════════════════════════════════════════════
    #                          MODULES
    # ═══════════════════════════════════════════════════════════════════
    
    async def get_modules(self) -> List[Dict[str, Any]]:
        """Get list of attached modules."""
        try:
            response = await self._client.get("/modules")
            if response.is_success:
                return response.json().get("data", [])
        except Exception as e:
            logger.error(f"Failed to get modules: {e}")
        return []
        
    # ═══════════════════════════════════════════════════════════════════
    #                      IMAGE EXTRACTION
    # ═══════════════════════════════════════════════════════════════════
    
    async def extract_images_from_log(self, run_id: str, output_dir: str = "./images") -> int:
        """
        Extract base64-encoded images from run command log.
        
        Images are embedded via TakeSnapshot command with IMG_START/IMG_END markers.
        
        Args:
            run_id: Run ID to extract images from
            output_dir: Directory to save images
            
        Returns:
            Number of images extracted
        """
        os.makedirs(output_dir, exist_ok=True)
        
        try:
            data = await self.get_run_commands(run_id)
            commands = data.get("data", []) if data else []
            
            capturing = False
            filename = "image.jpg"
            image_data = []
            count = 0
            
            for cmd in commands:
                if cmd.get("commandType") == "comment":
                    message = cmd.get("params", {}).get("message", "")
                    
                    if message.startswith("IMG_START:"):
                        capturing = True
                        filename = message[10:]
                        image_data = []
                        
                    elif message == "IMG_END":
                        if capturing and image_data:
                            try:
                                timestamp = datetime.now().strftime("%H%M%S")
                                output_path = os.path.join(output_dir, f"{timestamp}_{filename}")
                                image_bytes = base64.b64decode("".join(image_data))
                                with open(output_path, 'wb') as f:
                                    f.write(image_bytes)
                                count += 1
                                logger.info(f"Image saved: {output_path}")
                            except Exception as e:
                                logger.error(f"Failed to decode image: {e}")
                        capturing = False
                        
                    elif capturing:
                        image_data.append(message)
                        
            if count > 0:
                logger.info(f"Extracted {count} images from run log")
            return count
            
        except Exception as e:
            logger.error(f"Image extraction failed: {e}")
            return 0
            
    # ═══════════════════════════════════════════════════════════════════
    #                    PARTIAL TIP TRACKING
    # ═══════════════════════════════════════════════════════════════════
    
    async def get_actual_tip_usage(self, run_id: str) -> Dict[str, int]:
        """
        Analyze run commands to determine actual tip usage.
        
        Useful for partial runs or crash recovery.
        
        Args:
            run_id: Run ID to analyze
            
        Returns:
            Dict mapping rack load names to tip counts used
        """
        try:
            data = await self.get_run_commands(run_id)
            commands = data.get("data", []) if data else []
            
            # Build labware map: labwareId -> loadName
            labware_map = {}
            for cmd in commands:
                if cmd.get("commandType") == "loadLabware":
                    labware_id = cmd.get("result", {}).get("labwareId", "")
                    load_name = cmd.get("params", {}).get("loadName", "")
                    if labware_id and "tip" in load_name.lower():
                        labware_map[labware_id] = load_name
                        
            # Count successful pickUpTip commands
            usage = {}
            for cmd in commands:
                if cmd.get("commandType") == "pickUpTip" and cmd.get("status") == "succeeded":
                    labware_id = cmd.get("params", {}).get("labwareId", "")
                    if labware_id in labware_map:
                        rack = labware_map[labware_id]
                        usage[rack] = usage.get(rack, 0) + 1
                        
            return usage
            
        except Exception as e:
            logger.error(f"Failed to analyze tip usage: {e}")
            return {}
