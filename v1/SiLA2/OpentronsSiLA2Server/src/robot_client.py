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
        """Initialize the HTTP client and verify the robot responds."""
        transport = None
        if self.local_address:
            transport = httpx.AsyncHTTPTransport(local_address=self.local_address)

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={"Opentrons-Version": "*"},
            transport=transport
        )

        # Verify the robot actually responds before declaring success
        try:
            response = await self._client.get("/health")
            response.raise_for_status()
        except Exception as exc:
            await self._client.aclose()
            self._client = None
            raise ConnectionError(f"Robot at {self.base_url} did not respond: {exc}") from exc

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

    def _require_connected(self) -> None:
        """Raise a clear error if the robot HTTP client is not initialized."""
        if self._client is None:
            raise ConnectionError(
                f"Not connected to Opentrons robot at {self.base_url}. "
                "The robot may be off or unreachable."
            )

    @property
    def _http(self) -> httpx.AsyncClient:
        """The active HTTP client, narrowed to non-Optional.

        Raises the same error as _require_connected() if accessed before
        connect() has run. Use this (instead of self._client directly) at
        any call site that dereferences the client, so type checkers can
        see it is never None here.
        """
        if self._client is None:
            raise ConnectionError(
                f"Not connected to Opentrons robot at {self.base_url}. "
                "The robot may be off or unreachable."
            )
        return self._client

    async def _rediscover_via_mdns(self) -> bool:
        """Ask mDNS where the robot is now and re-point this client at it.

        The switch assigns the robot's IP by DHCP, so it can move between
        power cycles. Returns True if a *different*, reachable address was
        found and adopted (caller should then retry the connection).
        """
        try:
            from .config import discover_opentrons_via_mdns
        except Exception:  # pragma: no cover - defensive
            return False

        loop = asyncio.get_event_loop()
        found = await loop.run_in_executor(None, discover_opentrons_via_mdns)
        if not found:
            logger.warning("mDNS re-discovery found no Opentrons robot")
            return False

        new_host, new_port = found
        if (new_host, new_port) == (self.host, self.port):
            return False  # same place we already failed against

        logger.warning(
            f"Robot not at {self.host}:{self.port} — mDNS says {new_host}:{new_port}; switching"
        )
        await self.disconnect()
        self.host, self.port = new_host, new_port
        self.base_url = f"http://{new_host}:{new_port}"
        return True

    async def connect_with_retry(
        self,
        max_retries: int = 5,
        retry_delay: float = 2.0,
        on_retry: Optional[Callable] = None,
        rediscover: bool = True,
    ) -> bool:
        """
        Attempt to connect with retry logic.

        Args:
            max_retries: Maximum number of connection attempts
            retry_delay: Delay between retries in seconds
            on_retry: Optional callback called on each retry
            rediscover: If True, after all retries fail, try one mDNS
                re-discovery (in case the switch moved the robot to a new IP)
                and retry once against the address it reports.

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

        if rediscover and await self._rediscover_via_mdns():
            try:
                await self.connect()
                health = await self.get_health()
                if "error" not in health:
                    logger.info(f"Connected after mDNS re-discovery ({self.base_url})")
                    return True
            except Exception as e:
                logger.warning(f"Connection after mDNS re-discovery failed: {e}")

        logger.error(f"Failed to connect after {max_retries} attempts")
        return False
            
    # ═══════════════════════════════════════════════════════════════════
    #                         HEALTH & INFO
    # ═══════════════════════════════════════════════════════════════════
    
    async def get_health(self) -> Dict[str, Any]:
        """Get robot health status."""
        self._require_connected()
        try:
            response = await self._http.get("/health")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return {"status": "disconnected", "error": str(e)}
            
    async def get_robot_info(self) -> Dict[str, Any]:
        """Get robot information (name, serial, version)."""
        self._require_connected()
        try:
            response = await self._http.get("/robot/settings")
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
            response = await self._http.post("/protocols", files=files)
            
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
        self._require_connected()
        files = {"files": (filename, content.encode('utf-8'), "application/x-python")}
        response = await self._http.post("/protocols", files=files)
        
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
            await self._http.delete(f"/protocols/{protocol_id}")
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
        response = await self._http.post("/runs", json=payload)
        
        # Handle zombie run conflict
        if response.status_code == 409:
            logger.warning("Zombie run detected, cleaning up...")
            await self._deactivate_current_run()
            response = await self._http.post("/runs", json=payload)
            
        if not response.is_success:
            raise Exception(f"Run creation failed: {response.status_code} - {response.text}")
            
        data = response.json()
        run_id = data.get("data", {}).get("id")
        
        if not run_id:
            raise Exception("Run ID not found in response")
            
        self._current_run_id = run_id
        logger.info(f"Run created: {run_id}")
        return run_id

    async def add_labware_definition(self, run_id: str, definition: Dict[str, Any]) -> None:
        """
        Register a custom labware definition against a run, before it's
        loaded via ctx.load_labware(). Needed for any labware not already
        known to the robot's own built-in "opentrons" catalog - without this,
        the run fails at the first loadLabware command with a
        FileNotFoundError (confirmed on real hardware: a custom EuroClone
        plate that was referenced by LoadName but never registered).

        Args:
            run_id: Run ID (from create_run()) to register the definition against.
            definition: Full Opentrons v2 labware definition dict (the same
                shape read from Library/Labware/Reservoirs/<LoadName>.json).
        """
        response = await self._http.post(
            f"/runs/{run_id}/labware_definitions", json={"data": definition}
        )
        if not response.is_success:
            raise Exception(
                f"Labware definition registration failed for run {run_id} "
                f"(loadName={definition.get('parameters', {}).get('loadName', '?')}): "
                f"{response.status_code} - {response.text}"
            )
        logger.info(
            f"Labware definition registered: "
            f"{definition.get('parameters', {}).get('loadName', '?')} (run {run_id})"
        )

    async def _deactivate_current_run(self):
        """Deactivate any currently active run."""
        try:
            response = await self._http.get("/runs?pageLength=1")
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
            await self._http.patch(f"/runs/{run_id}", json=payload)
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
        self._require_connected()
        payload = {"data": {"actionType": action}}
        response = await self._http.post(f"/runs/{run_id}/actions", json=payload)
        if not response.is_success:
            # Must raise, not just log: callers (play_run/pause_run/stop_run)
            # otherwise report success unconditionally right after calling this,
            # even when the robot actually rejected the action.
            raise RuntimeError(f"Action '{action}' rejected by robot API ({response.status_code}): {response.text}")
            
    # ═══════════════════════════════════════════════════════════════════
    #                          RUN STATUS
    # ═══════════════════════════════════════════════════════════════════
    
    async def get_run_status(self, run_id: str) -> str:
        """Get the current status of a run."""
        self._require_connected()
        try:
            response = await self._http.get(f"/runs/{run_id}")
            if not response.is_success:
                return "error"
            data = response.json()
            return data.get("data", {}).get("status", "unknown")
        except Exception as e:
            logger.error(f"Failed to get run status: {e}")
            return "connection-error"
            
    async def get_run_commands(self, run_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the commands log for a run.

        NOTE: this is the /runs/{id}/commands *list* endpoint, which on this
        robot's API does not populate each command's "result" field (e.g. a
        loadPipette command here never carries the pipetteId it produced) -
        only "params" and "status". Anything needing a command's result
        (e.g. resolving which pipetteId maps to which mount) must come from
        get_run_detail() instead, not from here.
        """
        self._require_connected()
        try:
            response = await self._http.get(
                f"/runs/{run_id}/commands",
                params={"cursor": 0, "pageLength": 10000}
            )
            if response.is_success:
                return response.json()
        except Exception as e:
            logger.error(f"Failed to get run commands: {e}")
        return None

    async def get_run_detail(self, run_id: str) -> Optional[Dict[str, Any]]:
        """
        Get full run detail: status, pipettes (id -> mount/name), labware,
        errors, etc. Unlike get_run_commands(), this DOES include the
        pipetteId -> mount mapping (under data.pipettes), needed to resolve
        which mount a pickUpTip/dropTip command in the command log belongs to.
        """
        self._require_connected()
        try:
            response = await self._http.get(f"/runs/{run_id}")
            if response.is_success:
                return response.json()
        except Exception as e:
            logger.error(f"Failed to get run detail: {e}")
        return None
        
    async def wait_for_run_completion(
        self,
        run_id: str,
        poll_interval: float = 1.0,
        on_status_change: Optional[Callable] = None,
        timeout: Optional[float] = 7200.0
    ) -> str:
        """
        Wait for a run to complete.

        Args:
            run_id: Run ID to monitor
            poll_interval: Polling interval in seconds
            on_status_change: Optional callback for status changes
            timeout: Max seconds to wait before giving up. None disables the
                bound (not recommended: a run that never reaches a terminal
                state would otherwise poll forever and hold any caller-side lock).

        Returns:
            Final status string

        Raises:
            TimeoutError: If the run doesn't reach a terminal state within `timeout`.
        """
        last_status = ""
        terminal_states = {"succeeded", "failed", "stopped"}
        loop = asyncio.get_event_loop()
        deadline = None if timeout is None else loop.time() + timeout

        while True:
            status = await self.get_run_status(run_id)

            if status != last_status:
                logger.info(f"Run status: {status}")
                if on_status_change:
                    await on_status_change(status)
                last_status = status

            # Flex firmware "error recovery" state - entered on ANY unhandled
            # command failure the robot would otherwise pause for a human to
            # resolve via its own touchscreen (which LabOS has no UI for):
            # liquid_presence_detection (VerifyLiquid) finding no liquid, but
            # also e.g. a pickUpTip finding no physical tip where the
            # software expected one (tipPhysicallyMissing - confirmed on
            # hardware), or other hardware-detected mismatches. Not in
            # terminal_states and doesn't contain "failed"/"stopped", so
            # without this branch the run would sit here silently until
            # `timeout` (default 7200s). Stop it immediately instead,
            # matching the fail-fast behavior of the existing
            # (Aspirate-only) VerifyLiquid mechanism - the actual cause is
            # read from the run's own error detail below, never assumed.
            if status == "awaiting-recovery":
                reason = "unspecified error (no error detail available from the run)"
                try:
                    detail = await self.get_run_detail(run_id)
                    errors = (detail or {}).get("data", {}).get("errors") or []
                    if errors:
                        first = errors[0]
                        reason = first.get("detail") or first.get("errorType") or reason
                    else:
                        # data.errors is empty for a command-level failure
                        # (confirmed on hardware: a pickUpTip that hit
                        # tipPhysicallyMissing left data.errors=[] on the run
                        # detail even though the command itself carries a
                        # full error object) - fall back to the last failed
                        # command in the run's own command log instead of
                        # leaving `reason` at the generic default.
                        commands_resp = await self.get_run_commands(run_id)
                        cmds = (commands_resp or {}).get("data") or []
                        failed = [c for c in cmds if c.get("status") == "failed" and c.get("error")]
                        if failed:
                            err = failed[-1]["error"]
                            reason = err.get("detail") or err.get("errorType") or reason
                except Exception as e:
                    logger.warning(f"Could not fetch run detail for awaiting-recovery reason: {e}")
                logger.warning(f"Run {run_id} entered awaiting-recovery ({reason}) - stopping")
                try:
                    await self.stop_run(run_id)
                except Exception as e:
                    logger.error(f"Failed to stop run {run_id} after awaiting-recovery: {e}")
                return f"stopped-error-recovery: {reason}"

            # Check for terminal state
            if status in terminal_states or "failed" in status or "stopped" in status:
                return status

            if deadline is not None and loop.time() >= deadline:
                raise TimeoutError(
                    f"Run {run_id} did not reach a terminal state within {timeout}s "
                    f"(last known status: {status!r})"
                )

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
        self._require_connected()
        response = await self._http.post("/robot/home", json={"target": "robot"})
        if not response.is_success:
            raise RuntimeError(f"Home failed: {response.status_code} - {response.text}")
        logger.info("Robot homed")
            
    async def set_lights(self, on: bool) -> bool:
        """Turn robot lights on or off."""
        self._require_connected()
        response = await self._http.post("/robot/lights", json={"on": on})
        if not response.is_success:
            raise RuntimeError(f"SetLights failed: {response.status_code} - {response.text}")
        return True
    
    async def get_lights(self) -> Optional[bool]:
        """Get robot lights status."""
        self._require_connected()
        try:
            response = await self._http.get("/robot/lights")
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
        self._require_connected()
        try:
            response = await self._http.get("/modules")
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
        self._require_connected()
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
    
    async def get_actual_tip_usage(self, run_id: str) -> Optional[Dict[str, int]]:
        """
        Analyze run commands to determine actual tip usage.

        Useful for partial runs or crash recovery.

        Args:
            run_id: Run ID to analyze

        Returns:
            Dict mapping rack load names to tip counts actually picked up
            (successfully) in this run - EMPTY dict is a real, meaningful
            answer ("determined: zero tips were actually picked up", e.g. a
            run that failed on its very first PickUpTip), NOT "couldn't
            tell". Returns None only when the analysis itself failed (robot
            unreachable, malformed response, ...) - the caller
            (_update_tracker_smart) must fall back to the planned usage
            ONLY in that case, never for a genuine zero-usage result, or
            it reintroduces the exact bug fixed below by another path.

        BUG FIX (confirmed on hardware): this used to build the labwareId ->
        loadName map from each loadLabware command's own "result" field on
        the /runs/{id}/commands *list* endpoint - but that endpoint never
        populates "result" at all (the same gap already documented on
        reconstruct_tip_mount_state's pipette_mounts parameter, in
        tip_tracker.py), so labware_map was ALWAYS empty and this function
        ALWAYS returned {} regardless of what actually happened, for every
        partial/failed run ever. Since _update_tracker_smart() only reads
        "actual" usage from here for a non-succeeded run and falls back to
        the full PLANNED usage whenever this comes back empty, that meant a
        run that failed on its very first PickUpTip (0 tips actually taken)
        still had its full planned tip count charged to the tracker -
        confirmed by a real run (label DA_hydrogel_96_combinations_05,
        pickUpTip on MyTips_50 well A1 failed with tipPhysicallyMissing/"No
        Tip Detected", 0 tips ever picked up) whose archived summary shows
        planned_tip_usage {"MyTips_50": 4, "MyTips_1000": 1} charged in full
        despite the run stopping at its first, failed pickup.

        Fixed the same way reconstruct_tip_mount_state's mapping is sourced:
        from get_run_detail()'s data.labware (confirmed to carry id/loadName
        for every loaded labware, unlike the commands list), not from
        anything on the individual commands.
        """
        self._require_connected()
        try:
            detail = await self.get_run_detail(run_id)
            labware_list = (detail or {}).get("data", {}).get("labware", [])
            labware_map = {
                lw["id"]: lw["loadName"]
                for lw in labware_list
                if lw.get("id") and "tip" in (lw.get("loadName") or "").lower()
            }

            data = await self.get_run_commands(run_id)
            commands = data.get("data", []) if data else []

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
            return None
