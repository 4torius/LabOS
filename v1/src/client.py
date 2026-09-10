#!/usr/bin/env python3
"""
Plug & Play Generic Client
==========================

A GENERIC client that can execute commands on ANY SiLA2 server.
NO HARDCODED COMMAND MAPPINGS - commands are executed based on metadata the
server itself publishes (its FDL, fetched at runtime by the sila2 library's
SilaClient).

To add a new instrument:
- Create a SiLA2 server with the sila2 library (sila2-codegen + FeatureImplementationBase)
- The client discovers and calls it automatically

NO CODE CHANGES NEEDED HERE.
"""

import asyncio
import logging
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# The SiLA2 server packages each ship a top-level module named `src`; keep both
# this directory and the project root importable so relative and absolute forms
# of the orchestrator's own imports resolve the same way.
_src_dir = Path(__file__).parent.absolute()
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))
if str(_src_dir.parent) not in sys.path:
    sys.path.insert(0, str(_src_dir.parent))

try:
    import grpc
    GRPC_AVAILABLE = True
except ImportError:
    GRPC_AVAILABLE = False

from .discovery import PnPServer, PnPCommand, PnPFeature

logger = logging.getLogger(__name__)

# ── Shared SilaClient cache ────────────────────────────────────────────────────
# The sila2 library registers proto descriptors in a process-global pool on first
# import.  Creating a second SilaClient for the same server re-compiles the same
# proto and conflicts with the already-registered descriptor.  We keep exactly one
# SilaClient per (host, port) and share it between discovery and command execution.
_sila_clients: Dict[str, Any] = {}
_sila_clients_lock = threading.Lock()


def get_shared_sila_client(host: str, port: int) -> Any:
    """Return the cached SilaClient for (host, port), creating it if necessary."""
    key = f"{host}:{port}"
    with _sila_clients_lock:
        if key in _sila_clients:
            return _sila_clients[key]
        try:
            from sila2.client import SilaClient  # noqa: PLC0415
            client = SilaClient(host, port, insecure=True)
            _sila_clients[key] = client
            logger.debug("sila2 shared client created for %s", key)
            return client
        except Exception as exc:
            logger.warning("sila2 shared client creation failed for %s: %s", key, exc, exc_info=True)
            return None


def invalidate_shared_sila_client(host: str, port: int) -> None:
    """Remove and close the cached SilaClient (e.g. after a connection error)."""
    key = f"{host}:{port}"
    with _sila_clients_lock:
        client = _sila_clients.pop(key, None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


#                           HELPERS

def _normalize_params_for_command(
    params: Dict[str, Any],
    cmd_parameters: list,
) -> Dict[str, Any]:
    """
    Map workflow parameter keys to the SiLA2 command parameter identifiers.

    Three-pass strategy so workflow JSON files with slightly different naming
    conventions (e.g. "recipe", "Recipe", "recipe_name") still resolve to the
    correct SiLA2 identifier (e.g. "RecipeName").

    Keys that don't match any parameter are silently dropped (workflow-level
    hints like UseHAL / HALConfig are not SiLA2 params).
    """
    if not cmd_parameters:
        return {}

    out: Dict[str, Any] = {}
    remaining = {p.identifier: p for p in cmd_parameters}

    # Pass 1 — exact match
    for k, v in params.items():
        if k in remaining:
            out[k] = v
            del remaining[k]

    # Pass 2 — case-insensitive exact
    for k, v in params.items():
        if k in out:
            continue
        kl = k.lower()
        for pid in list(remaining):
            if kl == pid.lower():
                out[pid] = v
                del remaining[pid]
                break

    # Pass 3 — substring: "Recipe" ↔ "RecipeName", "recipe_name" ↔ "RecipeName"
    for k, v in params.items():
        if k in out:
            continue
        kn = k.replace("_", "").lower()
        for pid in list(remaining):
            pn = pid.replace("_", "").lower()
            if kn in pn or pn in kn:
                out[pid] = v
                del remaining[pid]
                break

    # Convert None values to "" so sila2 String validators don't raise TypeError.
    # Non-string params with None values will still fail in sila2, but Fix B in
    # _execute_via_sila2_client will surface the error properly instead of
    # cascading to the "Server needs sila2 library" fallback.
    return {k: ("" if v is None else v) for k, v in out.items()}


#                           DATA STRUCTURES

@dataclass
class CommandResult:
    """Result from executing a command."""
    success: bool
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    
    # Streaming info
    is_streaming: bool = False
    progress: int = 0
    status: str = ""


#                         GENERIC CLIENT

class PnPClient:
    """
    Generic Plug & Play client for ANY SiLA2 server.
    
    Executes commands based on server metadata fetched from the server at
    runtime, not on hardcoded mappings: the sila2 library's SilaClient pulls
    the protobuf descriptor from the server itself, so this client never
    imports instrument-specific stubs.

    A second "dynamic stub loading" path for non-sila2 servers used to live
    here. No such server was ever deployed, and carrying it meant a transport
    failure looked like "try the other protocol" instead of an error. If a raw
    gRPC instrument ever appears, give it a purpose-built adapter.
    """
    
    def __init__(self, base_dir: Optional[Path] = None):
        """
        Initialize the client.
        
        Args:
            base_dir: Project root, used to resolve Library/ paths
        """
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self._channels: Dict[str, grpc.aio.Channel] = {}
    
    async def connect(self, server: PnPServer, timeout: float = 5.0) -> bool:
        """
        Connect to a server.
        
        Args:
            server: Server to connect to
            timeout: Connection timeout
        
        Returns:
            True if connected successfully
        """
        if not GRPC_AVAILABLE:
            logger.error("gRPC not available")
            return False

        # Reuse existing channel — creating a new channel on every call breaks gRPC
        # state for consecutive steps to the same server (e.g. PlateOut → PlateIn).
        if server.address in self._channels:
            server.server_online = True
            return True

        try:
            channel = grpc.aio.insecure_channel(server.address)
            await asyncio.wait_for(channel.channel_ready(), timeout=timeout)
            
            self._channels[server.address] = channel
            server.server_online = True

            logger.info(f"Connected to {server.name} @ {server.address}")
            return True
            
        except asyncio.TimeoutError:
            logger.warning(f"Connection timeout: {server.address}")
            server.server_online = False
            return False
        except Exception as e:
            logger.warning(f"Connection failed: {server.address}: {e}")
            server.server_online = False
            return False
    
    async def disconnect(self, server: PnPServer):
        """Disconnect from a server."""
        address = server.address
        if address in self._channels:
            try:
                await self._channels[address].close()
            except Exception:
                pass
            del self._channels[address]
        self._invalidate_sila_client(server.host, server.port)
        server.server_online = False

    async def disconnect_all(self):
        """Disconnect from all servers."""
        for address in list(self._channels.keys()):
            try:
                await self._channels[address].close()
            except Exception:
                pass
        self._channels = {}
    
    #                         COMMAND EXECUTION
    
    async def execute(
        self,
        server: PnPServer,
        command: str,
        parameters: Optional[Dict[str, Any]] = None,
        feature: Optional[str] = None,
        timeout: Optional[float] = None,
        on_progress: Optional[Callable[[int, str], None]] = None
    ) -> CommandResult:
        """
        Execute a command on a server.
        
        Args:
            server: Target server
            command: Command identifier
            parameters: Command parameters
            feature: Feature identifier (optional, auto-detected if unique)
            timeout: Command timeout in seconds (None = wait indefinitely)
            on_progress: Progress callback(progress, status)
        
        Returns:
            CommandResult with success/error/data
        """
        if not server.server_online:
            return CommandResult(success=False, error=f"Server offline: {server.name}")

        params = parameters or {}

        # Normalize "no timeout" to the single sentinel used downstream: None.
        # Callers (workflow.py, lab_core.py) pass 0 to mean "unlimited" (e.g. a
        # multi-day protocol); without this, a falsy-but-not-None 0 would fall
        # through to per-path defaults instead of actually waiting forever.
        if timeout is not None and timeout <= 0:
            timeout = None

        # Find the command definition
        cmd_info = server.find_command(command)
        if not cmd_info:
            return CommandResult(
                success=False,
                error=f"Command not found: {command}. Available: {[c[1] for c in server.get_all_commands()]}"
            )

        feature_obj, cmd_obj = cmd_info

        result = await self._execute_via_sila2_client(server, feature_obj, cmd_obj, params, timeout, on_progress)
        if result is not None:
            return result

        # None means the transport itself failed (the SilaClient could not be
        # built, or a grpc.RpcError invalidated it) — a real error, not a cue to
        # try another protocol. There is no second protocol: every LabOS server
        # is a standard SiLA2 server.
        return CommandResult(
            success=False,
            error=f"Cannot reach {server.name} at {server.address} to execute {command}. "
                  f"The server is announced but its SiLA2 endpoint did not respond.",
        )
    
    async def get_property(
        self,
        server: PnPServer,
        feature_name: str,
        property_name: str,
    ) -> CommandResult:
        """
        Read a SiLA2 (non-observable) property via the sila2 library SilaClient.

        Returns CommandResult(data=<serialized value>) where NamedTuple-like
        structure responses are converted to dicts (recursively, so a
        List[Structure] property becomes a list of dicts).
        """
        if not server.server_online:
            return CommandResult(success=False, error=f"Server offline: {server.name}")

        def _serialize(value: Any) -> Any:
            if hasattr(value, "_asdict"):
                return {k: _serialize(v) for k, v in value._asdict().items()}
            if isinstance(value, (list, tuple)):
                return [_serialize(v) for v in value]
            return value

        def _run_sync() -> CommandResult:
            try:
                from sila2.framework.errors.defined_execution_error import DefinedExecutionError
            except ImportError:
                DefinedExecutionError = None

            try:
                sila_client = self._get_sila_client(server.host, server.port)
                if sila_client is None:
                    return CommandResult(success=False, error="Could not connect to sila2 client")

                feature_obj = getattr(sila_client, feature_name, None)
                if feature_obj is None:
                    return CommandResult(success=False, error=f"Feature '{feature_name}' not found on {server.name}")

                prop_obj = getattr(feature_obj, property_name, None)
                if prop_obj is None:
                    return CommandResult(success=False, error=f"Property '{property_name}' not found on feature '{feature_name}'")

                value = prop_obj.get()
                return CommandResult(success=True, data=_serialize(value))

            except Exception as exc:
                if DefinedExecutionError and isinstance(exc, DefinedExecutionError):
                    return CommandResult(success=False, error=str(exc))
                logger.warning(
                    "sila2 client property read (%s.%s) failed: %s", feature_name, property_name, exc, exc_info=True
                )
                if isinstance(exc, grpc.RpcError):
                    self._invalidate_sila_client(server.host, server.port)
                return CommandResult(success=False, error=str(exc))

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _run_sync)

    def _get_sila_client(self, host: str, port: int) -> Any:
        return get_shared_sila_client(host, port)

    def _invalidate_sila_client(self, host: str, port: int) -> None:
        invalidate_shared_sila_client(host, port)

    async def _execute_via_sila2_client(
        self,
        server: PnPServer,
        feature: PnPFeature,
        command: PnPCommand,
        params: Dict[str, Any],
        timeout: Optional[float],
        on_progress: Optional[Callable],
    ) -> Optional[CommandResult]:
        """
        Execute via sila2 library SilaClient (standard SiLA2 protocol).

        Works with any server built with the sila2 Python library.
        Runs the synchronous SilaClient in a thread executor so the event loop is not blocked.
        Uses a cached SilaClient to avoid descriptor pool conflicts on repeated calls.
        """
        fid = feature.identifier
        feature_name = fid.split("/")[-2] if "/" in fid else fid
        command_name = command.identifier

        def _run_sync() -> Optional[CommandResult]:
            import time as _time

            try:
                from sila2.framework.errors.defined_execution_error import DefinedExecutionError
            except ImportError:
                DefinedExecutionError = None

            try:
                from sila2.client.client_observable_command_instance import ClientObservableCommandInstance
            except ImportError:
                ClientObservableCommandInstance = None

            try:
                sila_client = self._get_sila_client(server.host, server.port)
                if sila_client is None:
                    return None

                feature_obj = getattr(sila_client, feature_name, None)
                if feature_obj is None:
                    logger.warning(
                        "sila2 client: feature '%s' not found on %s (available: %s)",
                        feature_name, server.name,
                        list(sila_client._features.keys()),
                    )
                    return None

                method = getattr(feature_obj, command_name, None)
                if method is None:
                    logger.warning(
                        "sila2 client: command '%s' not found in feature '%s'",
                        command_name, feature_name,
                    )
                    return None

                sila_params = _normalize_params_for_command(params, command.parameters)
                result = method(**sila_params)

                if command.observable and ClientObservableCommandInstance and isinstance(result, ClientObservableCommandInstance):
                    # sila2 observable command: poll until done, then fetch responses.
                    # The command is already running on the server at this point.
                    # timeout is already normalized by execute(): None means poll
                    # forever (multi-day protocols), otherwise it's a real deadline.
                    deadline = None if timeout is None else _time.monotonic() + timeout
                    while not result.done:
                        if deadline is not None and _time.monotonic() > deadline:
                            # The client gave up waiting - this is NOT success. The
                            # command is still running server-side (e.g. a long
                            # Opentrons ExecuteRecipe); reporting success=True here
                            # made every caller (webapp instruments.py -> dashboard.html)
                            # log "completed"/OK regardless of what actually happens
                            # to the run afterwards, including real failures that occur
                            # moments after this client stops watching. A timed-out
                            # command must surface as a failure so callers know to
                            # check the run's real status instead of assuming success.
                            execution_uuid = str(result.execution_uuid)
                            result.cancel_execution_info_subscription()
                            return CommandResult(
                                success=False,
                                error=(
                                    f"Command still running after {timeout}s client-side timeout "
                                    f"(execution_uuid={execution_uuid}) - not finished, not a success. "
                                    f"Check the run's real status directly."
                                ),
                                data={"status": "running", "execution_uuid": execution_uuid},
                            )
                        if on_progress and result.progress is not None:
                            on_progress(int(result.progress), str(result.status or ""))
                        _time.sleep(0.05)

                    try:
                        responses = result.get_responses()
                        result.cancel_execution_info_subscription()
                    except Exception as exc:
                        result.cancel_execution_info_subscription()
                        if DefinedExecutionError and isinstance(exc, DefinedExecutionError):
                            return CommandResult(success=False, error=str(exc))
                        logger.warning(
                            "sila2 observable get_responses (%s.%s) failed: %s",
                            feature_name, command_name, exc, exc_info=True,
                        )
                        # Return a proper error instead of None + invalidation.
                        # The SilaClient connection is still valid; only the response
                        # processing failed.  Invalidating then recreating the client
                        # causes proto descriptor pool conflicts on retry.
                        return CommandResult(
                            success=False,
                            error=f"Observable command response failed: {exc}",
                        )

                    if hasattr(responses, "_asdict"):
                        data = responses._asdict()
                        return CommandResult(success=True, data={k: str(v) for k, v in data.items()})
                    return CommandResult(success=True, data={"value": str(responses)})

                else:
                    # Non-observable command: result is the response directly.
                    if hasattr(result, "_asdict"):
                        data = result._asdict()
                        return CommandResult(
                            success=True,
                            data={k: str(v) for k, v in data.items()},
                        )
                    return CommandResult(success=True, data={"value": str(result)})

            except Exception as exc:
                if DefinedExecutionError and isinstance(exc, DefinedExecutionError):
                    return CommandResult(success=False, error=str(exc))
                logger.warning(
                    "sila2 client (%s.%s) failed: %s", feature_name, command_name, exc, exc_info=True
                )
                # Only invalidate on gRPC connection errors; data validation errors
                # (TypeError/ValueError from bad parameter types) don't break the
                # channel and invalidating causes proto descriptor pool conflicts on retry.
                if isinstance(exc, grpc.RpcError):
                    self._invalidate_sila_client(server.host, server.port)
                    return None
                return CommandResult(success=False, error=str(exc))

        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, _run_sync)
        except Exception as exc:
            logger.warning("sila2 client executor error: %s", exc, exc_info=True)
            return None

#                              REGISTRY INTEGRATION

class PnPRegistry:
    """
    Combined registry and client for plug & play operation.
    
    Provides a simple interface:
    - discover() - find all servers
    - connect() - connect to servers
    - execute() - run commands
    - disconnect() - cleanup
    """
    
    def __init__(self, base_dir: Optional[Path] = None):
        from .discovery import PnPDiscovery
        
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self.discovery = PnPDiscovery(base_dir)
        self.client = PnPClient(base_dir)
        self.servers: Dict[str, PnPServer] = {}
    
    async def discover(self, timeout: float = 2.0) -> int:
        """
        Discover all SiLA2 servers.
        
        Returns:
            Number of servers discovered
        """
        self.servers = await self.discovery.discover_all(timeout=timeout)
        return len(self.servers)
    
    async def connect_all(self, timeout: float = 5.0) -> Dict[str, bool]:
        """
        Connect to all discovered servers.
        
        Returns:
            Dict of {server_name: connected}
        """
        results = {}
        for key, server in self.servers.items():
            connected = await self.client.connect(server, timeout)
            results[server.name] = connected
        return results
    
    async def execute(
        self,
        server_name: str,
        command: str,
        parameters: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None
    ) -> CommandResult:
        """
        Execute a command on a server.
        
        Args:
            server_name: Server name or key
            command: Command identifier
            parameters: Command parameters
            timeout: Command timeout in seconds (None = wait indefinitely)
        
        Returns:
            CommandResult
        """
        server = self.discovery.get_server(server_name)
        if not server:
            return CommandResult(success=False, error=f"Server not found: {server_name}")

        await self.client.connect(server)
        return await self.client.execute(server, command, parameters, timeout=timeout)
    
    async def disconnect_all(self):
        """Disconnect from all servers."""
        await self.client.disconnect_all()
    
    def get_server(self, name: str) -> Optional[PnPServer]:
        """Get a server by name."""
        return self.discovery.get_server(name)
    
    def get_server_by_name(self, name: str) -> Optional[PnPServer]:
        """Alias for get_server (backwards compatibility)."""
        return self.get_server(name)
    
    def register(self, server_id: str, server: "PnPServer"):
        """Manually register a pre-discovered server (bypasses mDNS/port scan)."""
        self.discovery.servers[server_id] = server
        self.servers[server_id] = server

    def list_servers(self) -> List[PnPServer]:
        """List all servers."""
        return list(self.servers.values())

    def get_online_servers(self) -> List[PnPServer]:
        """Get online servers."""
        return [s for s in self.servers.values() if s.server_online]


#                              MAIN / TEST

async def main():
    """Test the plug & play client."""
    logging.basicConfig(level=logging.INFO)

    logger.info("Plug & Play Client Test")
    registry = PnPRegistry()

    count = await registry.discover()
    logger.info(f"[1] Found {count} servers")

    results = await registry.connect_all()
    for name, connected in results.items():
        logger.info(f"[2] {'OK' if connected else 'FAIL'} {name}")

    for server in registry.list_servers():
        cmds = server.get_all_commands()
        logger.info(f"[3] [{server.name}] {len(cmds)} commands")
        for feature_id, cmd_id, cmd in cmds[:5]:
            logger.info(f"    {cmd_id}")
        if len(cmds) > 5:
            logger.info(f"    ... and {len(cmds) - 5} more")

    await registry.disconnect_all()
    logger.info("[4] Disconnected")


if __name__ == "__main__":
    asyncio.run(main())
