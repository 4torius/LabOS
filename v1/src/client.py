#!/usr/bin/env python3
"""
Plug & Play Generic Client
==========================

A GENERIC gRPC client that can execute commands on ANY SiLA2 server.
NO HARDCODED COMMAND MAPPINGS - commands are executed based on server metadata.

Execution strategies:
0. sila2 library SilaClient - standard SiLA2 protocol (preferred for new servers)
1. SiLA2Common.ExecuteCommand - legacy custom protocol (old servers)
2. Dynamic stub loading - load generated stubs at runtime (legacy)

To add a new instrument:
- Create a SiLA2 server with the sila2 library (sila2-codegen + FeatureImplementationBase)
- The client automatically discovers and calls it via Strategy 0

NO CODE CHANGES NEEDED HERE.
"""

import asyncio
import importlib
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Ensure grpc stubs can be imported
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
    
    Executes commands based on server metadata, not hardcoded mappings.
    Uses multiple strategies to maximize compatibility:
    
    1. SiLA2Common.ExecuteCommand - truly generic, works with any compliant server
    2. Dynamic stub loading - loads generated stubs at runtime
    3. Reflection - for servers with gRPC reflection enabled
    """
    
    def __init__(self, base_dir: Optional[Path] = None):
        """
        Initialize the client.
        
        Args:
            base_dir: Base directory for finding stubs and configs
        """
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self._channels: Dict[str, grpc.aio.Channel] = {}
        self._stubs: Dict[str, Any] = {}
        self._stub_modules: Dict[str, Any] = {}
    
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
            
            # Try to load stubs for this server
            await self._load_stubs_for_server(server)
            
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
        if address in self._stubs:
            del self._stubs[address]
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
        self._stubs = {}
    
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
        
        # Find the command definition
        cmd_info = server.find_command(command)
        if not cmd_info:
            # Try direct execution anyway via SiLA2Common (command might be new/not in cache)
            logger.warning(f"Command '{command}' not in cache, trying direct execution via SiLA2Common")
            result = await self._execute_direct_common(server, feature, command, params, timeout, on_progress)
            if result is not None:
                return result
            return CommandResult(
                success=False,
                error=f"Command not found: {command}. Available: {[c[1] for c in server.get_all_commands()]}"
            )
        
        feature_obj, cmd_obj = cmd_info

        # Try execution strategies in order of preference

        # Strategy 0: sila2 standard library SilaClient (preferred, works with all sila2-compliant servers)
        result = await self._execute_via_sila2_client(server, feature_obj, cmd_obj, params, timeout, on_progress)
        if result is not None:
            return result

        # Strategy 1: SiLA2Common.ExecuteCommand (legacy custom protocol)
        result = await self._execute_via_common(server, feature_obj, cmd_obj, params, timeout, on_progress)
        if result is not None:
            return result

        # Strategy 2: Dynamic stub (loaded at runtime, legacy)
        result = await self._execute_via_stub(server, feature_obj, cmd_obj, params, timeout, on_progress)
        if result is not None:
            return result

        # Strategy 3: Fallback - indicate what's needed
        return CommandResult(
            success=False,
            error=f"Cannot execute {command} on {server.name}. "
                  f"Server needs sila2 library, SiLA2Common.ExecuteCommand, or gRPC stubs."
        )
    
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
        timeout: float,
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
                    poll_timeout = timeout if (timeout and timeout > 0) else 14400.0  # 4h default for long-running recipes
                    deadline = _time.monotonic() + poll_timeout
                    while not result.done:
                        if _time.monotonic() > deadline:
                            result.cancel_execution_info_subscription()
                            return CommandResult(
                                success=True,
                                data={"status": "running", "execution_uuid": str(result.execution_uuid)},
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

    async def _execute_via_common(
        self,
        server: PnPServer,
        feature: PnPFeature,
        command: PnPCommand,
        params: Dict[str, Any],
        timeout: float,
        on_progress: Optional[Callable]
    ) -> Optional[CommandResult]:
        """
        Execute via SiLA2Common.ExecuteCommand (legacy custom protocol).
        """
        try:
            # Try to import SiLA2Common stubs
            try:
                from .pnp_stubs import SiLA2Common_pb2 as common_pb2
                from .pnp_stubs import SiLA2Common_pb2_grpc as common_grpc
            except ImportError:
                return None  # Stubs not available, try next strategy
            
            channel = self._channels.get(server.address)
            if not channel:
                return None
            
            stub = common_grpc.SiLA2ServerInfoStub(channel)
            
            # Build request
            params_str = {k: str(v) for k, v in params.items()}
            
            # timeout=None means wait indefinitely (use 0 to signal server)
            timeout_secs = 0 if timeout is None else int(timeout)
            
            request = common_pb2.ExecuteCommandRequest(
                feature=feature.identifier,
                command=command.identifier,
                parameters=params_str,
                timeout_seconds=timeout_secs
            )
            
            # Execute (streaming response) - no client-side timeout, wait for server
            try:
                final_result = None
                last_response = None
                async for response in stub.ExecuteCommand(request):
                    last_response = response
                    if on_progress and response.is_intermediate:
                        on_progress(response.progress, response.status)
                    
                    if not response.is_intermediate:
                        final_result = response
                        break

                # Some servers close the stream without sending an explicit
                # non-intermediate terminal frame. If the last frame looks
                # complete, treat it as final to avoid false workflow failures.
                if final_result is None and last_response is not None:
                    status_text = str(getattr(last_response, 'status', '') or '').lower()
                    progress_val = int(getattr(last_response, 'progress', 0) or 0)
                    if status_text in {'complete', 'completed', 'done', 'success', 'succeeded', 'finished'} or progress_val >= 100:
                        final_result = last_response
                
                if final_result:
                    data = dict(final_result.result)
                    if getattr(final_result, 'status', '') and 'status' not in data:
                        data['status'] = final_result.status
                    if getattr(final_result, 'progress', 0) and 'progress' not in data:
                        data['progress'] = str(final_result.progress)
                    return CommandResult(
                        success=final_result.success,
                        data=data,
                        error=final_result.error if not final_result.success else None,
                        progress=final_result.progress,
                        status=final_result.status
                    )
                
                return CommandResult(success=False, error="No response from server")
                
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.UNIMPLEMENTED:
                    return None  # Server doesn't implement ExecuteCommand, try next strategy
                return CommandResult(success=False, error=f"gRPC error: {e.details()}")
            
        except Exception as e:
            logger.debug(f"SiLA2Common execution failed: {e}")
            return None
    
    async def _execute_direct_common(
        self,
        server: PnPServer,
        feature_name: str,
        command_name: str,
        params: Dict[str, Any],
        timeout: float,
        on_progress: Optional[Callable]
    ) -> Optional[CommandResult]:
        """
        Execute via SiLA2Common.ExecuteCommand directly without command lookup.
        
        Used when command is not in cache (e.g., server was updated).
        """
        try:
            try:
                from .pnp_stubs import SiLA2Common_pb2 as common_pb2
                from .pnp_stubs import SiLA2Common_pb2_grpc as common_grpc
            except ImportError:
                return None
            
            channel = self._channels.get(server.address)
            if not channel:
                return None
            
            stub = common_grpc.SiLA2ServerInfoStub(channel)
            
            params_str = {k: str(v) for k, v in params.items()}
            timeout_secs = 0 if timeout is None else int(timeout)
            
            request = common_pb2.ExecuteCommandRequest(
                feature=feature_name or "",
                command=command_name,
                parameters=params_str,
                timeout_seconds=timeout_secs
            )
            
            try:
                final_result = None
                last_response = None
                async for response in stub.ExecuteCommand(request):
                    last_response = response
                    if on_progress and response.is_intermediate:
                        on_progress(response.progress, response.status)
                    
                    if not response.is_intermediate:
                        final_result = response
                        break

                # Tolerate streams that end with only intermediate frames.
                if final_result is None and last_response is not None:
                    status_text = str(getattr(last_response, 'status', '') or '').lower()
                    progress_val = int(getattr(last_response, 'progress', 0) or 0)
                    if status_text in {'complete', 'completed', 'done', 'success', 'succeeded', 'finished'} or progress_val >= 100:
                        final_result = last_response
                
                if final_result:
                    data = dict(final_result.result)
                    if getattr(final_result, 'status', '') and 'status' not in data:
                        data['status'] = final_result.status
                    if getattr(final_result, 'progress', 0) and 'progress' not in data:
                        data['progress'] = str(final_result.progress)
                    return CommandResult(
                        success=final_result.success,
                        data=data,
                        error=final_result.error if not final_result.success else None,
                        progress=final_result.progress,
                        status=final_result.status
                    )
                
                return CommandResult(success=False, error="No response from server")
                
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.UNIMPLEMENTED:
                    return None
                return CommandResult(success=False, error=f"gRPC error: {e.details()}")
            
        except Exception as e:
            logger.debug(f"Direct SiLA2Common execution failed: {e}")
            return None
    
    async def _execute_via_stub(
        self,
        server: PnPServer,
        feature: PnPFeature,
        command: PnPCommand,
        params: Dict[str, Any],
        timeout: float,
        on_progress: Optional[Callable]
    ) -> Optional[CommandResult]:
        """
        Execute via dynamically loaded stub.
        
        This finds and loads the appropriate _pb2.py and _pb2_grpc.py files
        for the server at runtime.
        """
        try:
            channel = self._channels.get(server.address)
            if not channel:
                return None
            
            # Get or create stub for this server
            stub_info = self._stubs.get(server.address)
            if not stub_info:
                return None
            
            pb2 = stub_info.get("pb2")
            stub_class = stub_info.get("stub_class")
            
            if not stub_class or not pb2:
                return None
            
            # Create stub instance on-demand
            stub = stub_info.get("stub")
            if not stub:
                try:
                    stub = stub_class(channel)
                    stub_info["stub"] = stub
                except Exception as e:
                    logger.debug(f"Failed to create stub instance: {e}")
                    return None
            
            # Find the method on the stub
            method = getattr(stub, command.identifier, None)
            if not method:
                # Try alternative names
                for alt_name in [command.identifier, f"Get{command.identifier}", command.identifier.replace("Get", "")]:
                    method = getattr(stub, alt_name, None)
                    if method:
                        break
            
            if not method:
                return None
            
            # Build request message - try multiple naming conventions
            request_class = None
            
            # Try exact match first: ExecuteRecipe -> ExecuteRecipeRequest
            request_class_name = f"{command.identifier}Request"
            request_class = getattr(pb2, request_class_name, None)
            
            if not request_class:
                # Try shorter names: SwitchHardwareConfig -> SwitchConfigRequest
                # Remove common middle words like "Hardware" 
                base_name = command.identifier
                for middle in ["Hardware", "Current", "Available", "Loaded"]:
                    short_name = base_name.replace(middle, "")
                    request_class = getattr(pb2, f"{short_name}Request", None)
                    if request_class:
                        logger.debug(f"Found request class by removing '{middle}': {short_name}Request")
                        break
            
            if not request_class:
                # Try common prefix patterns
                base_name = command.identifier
                for prefix in ["Get", "Set", "Switch", "Execute", "Run", "List"]:
                    if base_name.startswith(prefix) and len(base_name) > len(prefix):
                        short_name = base_name[len(prefix):]
                        request_class = getattr(pb2, f"{prefix}{short_name}Request", None)
                        if not request_class:
                            request_class = getattr(pb2, f"{short_name}Request", None)
                        if request_class:
                            break
            
            if not request_class:
                # Try to find any matching request class by word overlap
                # Require at least 2 matching words OR the first verb must match
                cmd_words = self._split_camel_case(command.identifier).lower().split()
                cmd_word_set = set(cmd_words)
                first_word = cmd_words[0] if cmd_words else ""
                best_match = None
                best_score = 0
                
                for name in dir(pb2):
                    if name.endswith("Request"):
                        req_words = self._split_camel_case(name.replace("Request", "")).lower().split()
                        req_word_set = set(req_words)
                        req_first = req_words[0] if req_words else ""
                        
                        # Count overlapping words
                        overlap = len(cmd_word_set & req_word_set)
                        
                        # Require either 2+ matching words OR the first word (action verb) matches
                        if overlap >= 2 or (overlap >= 1 and first_word == req_first):
                            if overlap > best_score:
                                best_score = overlap
                                best_match = name
                
                if best_match:
                    request_class = getattr(pb2, best_match)
                    logger.debug(f"Found request class by word overlap ({best_score} words): {best_match}")
            
            if not request_class:
                # Use empty request (for "Get" commands that take Empty)
                try:
                    from google.protobuf import empty_pb2
                    request = empty_pb2.Empty()
                except ImportError:
                    return None
            else:
                # Build request with parameters
                request = self._build_request(request_class, params)
            
            # Execute
            try:
                if command.observable:
                    # Streaming response
                    final_result = None
                    last_response = None
                    async for response in method(request):
                        last_response = response
                        # Try to detect intermediate vs final
                        is_intermediate = getattr(response, 'is_intermediate', False)
                        if on_progress and is_intermediate:
                            progress = getattr(response, 'progress', 0)
                            status = getattr(response, 'status', '')
                            on_progress(progress, status)
                        
                        if not is_intermediate:
                            final_result = response
                            break

                    if final_result is None and last_response is not None:
                        status_text = str(getattr(last_response, 'status', '') or '').lower()
                        progress_val = int(getattr(last_response, 'progress', 0) or 0)
                        if status_text in {'complete', 'completed', 'done', 'success', 'succeeded', 'finished'} or progress_val >= 100:
                            final_result = last_response
                    
                    return self._parse_response(final_result)
                else:
                    # Unary response
                    response = await asyncio.wait_for(method(request), timeout=timeout)
                    return self._parse_response(response)
                    
            except grpc.RpcError as e:
                error_msg = e.details() if e.details() else str(e.code())
                if e.code() == grpc.StatusCode.UNIMPLEMENTED or "not found" in error_msg.lower():
                    return CommandResult(
                        success=False, 
                        error=f"Server doesn't implement '{command.identifier}'. "
                              f"Restart the server if you recently added this method."
                    )
                return CommandResult(success=False, error=f"gRPC error: {error_msg}")
            
        except Exception as e:
            logger.debug(f"Stub execution failed: {e}")
            return None
    
    def _build_request(self, request_class: Any, params: Dict[str, Any]) -> Any:
        """Build a protobuf request message from parameters."""
        # Normalize parameter keys
        params_normalized = {}
        for key, value in params.items():
            # Try different key formats
            params_normalized[key] = value
            params_normalized[key.lower()] = value
            params_normalized[self._to_snake_case(key)] = value
        
        # Get field names from request class
        try:
            fields = request_class.DESCRIPTOR.fields_by_name.keys()
        except Exception:
            fields = []
        
        # Get field descriptors for type coercion
        try:
            field_descriptors = {f.name: f for f in request_class.DESCRIPTOR.fields}
        except Exception:
            field_descriptors = {}

        # Build kwargs for request
        kwargs = {}
        for field_name in fields:
            for param_key in [field_name, field_name.lower(), self._to_camel_case(field_name)]:
                if param_key in params_normalized:
                    value = params_normalized[param_key]
                    # Coerce value to match proto field type
                    fd = field_descriptors.get(field_name)
                    if fd is not None:
                        from google.protobuf.descriptor import FieldDescriptor as FD
                        if fd.type == FD.TYPE_BOOL:
                            if isinstance(value, str):
                                value = value.lower() in ('true', '1', 'yes', 'on')
                            else:
                                value = bool(value)
                        elif fd.type in (FD.TYPE_INT32, FD.TYPE_INT64, FD.TYPE_SINT32,
                                         FD.TYPE_SINT64, FD.TYPE_UINT32, FD.TYPE_UINT64):
                            try:
                                value = int(value)
                            except (TypeError, ValueError):
                                pass
                        elif fd.type in (FD.TYPE_FLOAT, FD.TYPE_DOUBLE):
                            try:
                                value = float(value)
                            except (TypeError, ValueError):
                                pass
                        elif fd.type == FD.TYPE_STRING:
                            value = str(value)
                    kwargs[field_name] = value
                    break

        return request_class(**kwargs)
    
    def _parse_response(self, response: Any) -> CommandResult:
        """Parse a protobuf response into CommandResult."""
        if response is None:
            return CommandResult(success=False, error="No response")
        
        # Check for success field
        success = getattr(response, 'success', True)
        
        # Check for error field
        error = getattr(response, 'error', None) or getattr(response, 'error_message', None)
        
        # Extract all fields as data
        data = {}
        try:
            for field in response.DESCRIPTOR.fields:
                value = getattr(response, field.name, None)
                if value is not None:
                    if hasattr(value, 'DESCRIPTOR'):
                        # Nested message - convert to dict
                        data[field.name] = self._message_to_dict(value)
                    else:
                        data[field.name] = value
        except Exception:
            # Fallback: just use the response
            data = {"response": str(response)}
        
        return CommandResult(
            success=success if not error else False,
            data=data,
            error=str(error) if error else None
        )
    
    def _message_to_dict(self, message: Any) -> Dict:
        """Convert protobuf message to dict."""
        result = {}
        try:
            for field in message.DESCRIPTOR.fields:
                value = getattr(message, field.name, None)
                if value is not None:
                    if hasattr(value, 'DESCRIPTOR'):
                        result[field.name] = self._message_to_dict(value)
                    else:
                        result[field.name] = value
        except Exception:
            pass
        return result
    
    def _to_snake_case(self, name: str) -> str:
        """Convert CamelCase to snake_case."""
        import re
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
        return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()
    
    def _to_camel_case(self, name: str) -> str:
        """Convert snake_case to CamelCase."""
        components = name.split('_')
        return components[0] + ''.join(x.title() for x in components[1:])
    
    def _split_camel_case(self, name: str) -> str:
        """Split CamelCase into space-separated words. SwitchHardwareConfig -> Switch Hardware Config"""
        import re
        return re.sub('([a-z])([A-Z])', r'\1 \2', name)
    
    #                         STUB LOADING
    
    async def _load_stubs_for_server(self, server: PnPServer):
        """
        Load gRPC stubs for a server.
        
        Searches for _pb2.py and _pb2_grpc.py files in:
        1. src/grpc/ directory
        2. Server's own directory
        """
        if not server.features:
            return
        
        # Determine possible stub module names
        # Priority: feature names first, then server name patterns
        possible_names = []
        
        # 1. From features (highest priority - most specific)
        for feature in server.features:
            possible_names.append(feature.identifier)
            if not feature.identifier.endswith("Service"):
                possible_names.append(f"{feature.identifier}Service")
        
        # 2. From server name - multiple patterns
        server_key = server.name.lower().replace(" ", "").replace("-", "")
        possible_names.append(f"{server_key}Service")
        possible_names.append(server_key)
        
        # 3. Try extracting first word for common patterns like "Opentrons Flex" -> "OpentronsService"
        first_word = server.name.split()[0] if server.name else ""
        if first_word:
            possible_names.append(f"{first_word}Service")
            possible_names.append(first_word)
        
        # Try to load stubs
        logger.debug(f"Trying stub names for {server.name}: {possible_names[:5]}...")
        for name in possible_names:
            stub_info = self._try_load_stub(name, server)
            if stub_info:
                self._stubs[server.address] = stub_info
                logger.info(f"Loaded stubs for {server.name}: {name}")
                return
        
        logger.warning(f"Could not load stubs for {server.name} (tried {len(possible_names)} names)")
    
    def _try_load_stub(self, service_name: str, server: PnPServer) -> Optional[Dict]:
        """Try to load a stub module by name."""
        pb2 = None
        pb2_grpc = None
        
        # Try direct import from pnp_stubs subpackage
        try:
            # This works when src is in sys.path
            pb2 = importlib.import_module(f"pnp_stubs.{service_name}_pb2")
            pb2_grpc = importlib.import_module(f"pnp_stubs.{service_name}_pb2_grpc")
            logger.debug(f"Loaded stubs from pnp_stubs.{service_name}")
        except ImportError as e1:
            # Try from src.pnp_stubs
            try:
                pb2 = importlib.import_module(f"src.pnp_stubs.{service_name}_pb2")
                pb2_grpc = importlib.import_module(f"src.pnp_stubs.{service_name}_pb2_grpc")
                logger.debug(f"Loaded stubs from src.pnp_stubs.{service_name}")
            except ImportError as e2:
                logger.debug(f"Import failed for {service_name}: {e1}, {e2}")
                return None
        
        if not pb2_grpc:
            logger.debug(f"Could not load stubs for {service_name}")
            return None
        
        # Find stub class
        stub_class = None
        for name in dir(pb2_grpc):
            if name.endswith("Stub") and not name.startswith("_"):
                stub_class = getattr(pb2_grpc, name)
                break
        
        if not stub_class:
            return None
        
        # Return modules and class - stub instance created on demand
        return {
            "pb2": pb2,
            "pb2_grpc": pb2_grpc,
            "stub_class": stub_class,
            "stub": None  # Created on-demand when channel available
        }
    
    #                         HEALTH CHECK
    
    async def check_health(self, server: PnPServer, timeout: float = 5.0) -> Dict:
        """
        Check server and hardware health.
        
        Returns:
            Dict with server_online, hardware_online, hardware_status
        """
        result = {
            "server_online": False,
            "hardware_online": False,
            "hardware_status": "unknown"
        }
        
        if not GRPC_AVAILABLE:
            return result
        
        # Check if server is reachable
        try:
            channel = self._channels.get(server.address)
            if not channel:
                channel = grpc.aio.insecure_channel(server.address)
            
            await asyncio.wait_for(channel.channel_ready(), timeout=timeout)
            result["server_online"] = True
            server.server_online = True
            
        except Exception:
            server.server_online = False
            return result
        
        # Try to get status via SiLA2Common
        try:
            from .pnp_stubs import SiLA2Common_pb2 as common_pb2
            from .pnp_stubs import SiLA2Common_pb2_grpc as common_grpc
            
            stub = common_grpc.SiLA2ServerInfoStub(channel)
            response = await asyncio.wait_for(
                stub.GetStatus(common_pb2.GetStatusRequest()),
                timeout=timeout
            )
            
            result["hardware_online"] = response.hardware_online
            result["hardware_status"] = response.hardware_status
            server.hardware_online = response.hardware_online
            server.hardware_status = response.hardware_status
            
        except Exception:
            # SiLA2Common not available - try alternative methods
            pass
        
        return result


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
