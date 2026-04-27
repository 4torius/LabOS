#!/usr/bin/env python3
"""
SiLA2Common Service Implementation
==================================

This module provides a drop-in SiLA2Common service implementation for any SiLA2 server.
It enables the plug-and-play architecture by allowing generic clients to discover
and interact with any server without prior knowledge.

TO ADD TO AN EXISTING SERVER:
1. Import this module
2. Create a SiLA2CommonServicer with your server metadata
3. Add it to your gRPC server

Example:
    from sila2_common_servicer import SiLA2CommonServicer, ServerMetadata
    
    metadata = ServerMetadata(
        server_name="MyInstrument",
        server_type="liquid_handler",
        vendor="MyCompany",
        server_version="1.0.0"
    )
    
    common_servicer = SiLA2CommonServicer(
        metadata=metadata,
        feature_directory="features/",
        hardware_status_callback=lambda: my_server.get_hardware_status()
    )
    
    # Add to gRPC server
    SiLA2CommonService_pb2_grpc.add_SiLA2ServerInfoServicer_to_server(
        common_servicer, server
    )
"""

import asyncio
import json
import logging
import os
import platform
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import grpc

logger = logging.getLogger(__name__)


# =============================================================================
#                           SiLA2 ERROR DEFINITIONS
# =============================================================================

class SiLA2ErrorType:
    """
    Standard SiLA2 Defined Execution Error identifiers.
    
    Per SiLA2 standard, servers MUST use these error identifiers when
    returning errors from command execution.
    """
    VALIDATION_ERROR = "ValidationError"          # Invalid parameters
    CONNECTION_FAILED = "ConnectionFailed"        # Hardware connection lost
    HARDWARE_ERROR = "HardwareError"              # Physical hardware malfunction
    TIMEOUT_ERROR = "TimeoutError"                # Operation exceeded timeout
    BUSY_ERROR = "BusyError"                      # Device is busy
    NOT_SUPPORTED_ERROR = "NotSupportedError"     # Command not supported
    CONFIGURATION_ERROR = "ConfigurationError"    # Invalid configuration
    RESOURCE_UNAVAILABLE = "ResourceUnavailable"  # Required resource not available
    UNSPECIFIED_ERROR = "UnspecifiedError"        # Generic error


@dataclass
class SiLA2Error:
    """
    SiLA2 compliant error structure.
    
    Use create_sila2_error() helper to construct these errors.
    """
    error_identifier: str
    message: str
    parameters: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for gRPC response."""
        return {
            "error_identifier": self.error_identifier,
            "message": self.message,
            "parameters": [
                {"identifier": k, "value": str(v)} 
                for k, v in self.parameters.items()
            ]
        }


def create_sila2_error(
    error_type: str,
    message: str,
    **params
) -> SiLA2Error:
    """
    Create a SiLA2 compliant error.
    
    Args:
        error_type: One of SiLA2ErrorType constants
        message: Human-readable error message
        **params: Additional error parameters (key=value)
        
    Returns:
        SiLA2Error instance
        
    Example:
        error = create_sila2_error(
            SiLA2ErrorType.VALIDATION_ERROR,
            "Temperature must be between 4 and 95°C",
            parameter="target_temperature",
            value="120",
            min_value="4",
            max_value="95"
        )
    """
    return SiLA2Error(
        error_identifier=error_type,
        message=message,
        parameters=params
    )


def create_error_response(error: SiLA2Error) -> Dict[str, Any]:
    """
    Create a complete error response for ExecuteCommand.
    
    Args:
        error: SiLA2Error instance
        
    Returns:
        Dictionary suitable for ExecuteCommandResponse
    """
    return {
        "success": False,
        "error": error.message,  # Backward compatibility
        "sila2_error": error.to_dict()
    }


# =============================================================================
#                           DATA STRUCTURES
# =============================================================================

@dataclass
class ServerMetadata:
    """Server metadata for SiLA2Common service."""
    server_name: str
    server_type: str  # e.g., "opentrons", "plate_reader", "mobile_robot"
    vendor: str = ""
    model: str = ""
    serial_number: str = ""
    server_version: str = "1.0.0"
    sila_version: str = "2.0"
    description: str = ""
    
    # Capabilities
    capabilities: List[str] = field(default_factory=list)


@dataclass
class FeatureInfo:
    """Parsed feature information."""
    identifier: str
    display_name: str
    description: str
    category: str
    version: str
    commands: List[Dict[str, Any]]
    properties: List[Dict[str, Any]]


@dataclass
class CommandDefinition:
    """Definition of a command for dynamic execution."""
    command_id: str
    feature_id: str
    executor: Callable  # async function to execute the command
    parameters: List[Dict[str, Any]] = field(default_factory=list)


# =============================================================================
#                     FEATURE PARSER
# =============================================================================

class FeatureParser:
    """Parses SiLA2 Feature Definition Language (FDL) files."""
    
    # SiLA2 FDL namespace
    NS = {'sila': 'http://www.sila-standard.org'}
    
    @classmethod
    def parse_feature_file(cls, path: Path) -> Optional[FeatureInfo]:
        """Parse a .sila.xml feature file."""
        try:
            tree = ET.parse(path)
            root = tree.getroot()
            
            # Get namespace from root if different
            ns = cls.NS
            if root.tag.startswith('{'):
                ns_uri = root.tag.split('}')[0][1:]
                ns = {'sila': ns_uri}
            
            # Get feature attributes
            identifier = root.get('Feature', '') or root.get('Identifier', '')
            display_name = cls._get_text(root, './/sila:DisplayName', ns)
            description = cls._get_text(root, './/sila:Description', ns)
            category = root.get('Category', '')
            version = root.get('Version', '1.0')
            
            if not identifier:
                identifier = path.stem.replace('.sila', '')
            
            # Parse commands
            commands = cls._parse_commands(root, ns)
            
            # Parse properties
            properties = cls._parse_properties(root, ns)
            
            return FeatureInfo(
                identifier=identifier,
                display_name=display_name or identifier,
                description=description,
                category=category,
                version=version,
                commands=commands,
                properties=properties
            )
            
        except Exception as e:
            logger.error(f"Failed to parse feature file {path}: {e}")
            return None
    
    @classmethod
    def _get_text(cls, element: ET.Element, xpath: str, ns: dict) -> str:
        """Get text from element by XPath."""
        found = element.find(xpath, ns)
        if found is not None and found.text:
            return found.text.strip()
        return ""
    
    @classmethod
    def _parse_commands(cls, root: ET.Element, ns: dict) -> List[Dict[str, Any]]:
        """Parse command definitions."""
        commands = []
        
        for cmd in root.findall('.//sila:Command', ns):
            cmd_info = {
                'identifier': cmd.get('Identifier', ''),
                'display_name': cls._get_text(cmd, 'sila:DisplayName', ns),
                'description': cls._get_text(cmd, 'sila:Description', ns),
                'observable': cmd.get('Observable', 'No') == 'Yes',
                'parameters': cls._parse_parameters(cmd, ns),
                'responses': cls._parse_responses(cmd, ns)
            }
            commands.append(cmd_info)
        
        return commands
    
    @classmethod
    def _parse_properties(cls, root: ET.Element, ns: dict) -> List[Dict[str, Any]]:
        """Parse property definitions."""
        properties = []
        
        for prop in root.findall('.//sila:Property', ns):
            prop_info = {
                'identifier': prop.get('Identifier', ''),
                'display_name': cls._get_text(prop, 'sila:DisplayName', ns),
                'description': cls._get_text(prop, 'sila:Description', ns),
                'observable': prop.get('Observable', 'No') == 'Yes',
                'data_type': cls._parse_data_type(prop, ns)
            }
            properties.append(prop_info)
        
        return properties
    
    @classmethod
    def _parse_parameters(cls, cmd: ET.Element, ns: dict) -> List[Dict[str, Any]]:
        """Parse command parameters."""
        params = []
        
        for param in cmd.findall('.//sila:Parameter', ns):
            param_info = {
                'identifier': param.get('Identifier', ''),
                'display_name': cls._get_text(param, 'sila:DisplayName', ns),
                'description': cls._get_text(param, 'sila:Description', ns),
                'data_type': cls._parse_data_type(param, ns),
                'required': True  # All SiLA2 parameters are required by default
            }
            params.append(param_info)
        
        return params
    
    @classmethod
    def _parse_responses(cls, cmd: ET.Element, ns: dict) -> List[Dict[str, Any]]:
        """Parse command responses."""
        responses = []
        
        for resp in cmd.findall('.//sila:Response', ns):
            resp_info = {
                'identifier': resp.get('Identifier', ''),
                'display_name': cls._get_text(resp, 'sila:DisplayName', ns),
                'description': cls._get_text(resp, 'sila:Description', ns),
                'data_type': cls._parse_data_type(resp, ns)
            }
            responses.append(resp_info)
        
        return responses
    
    @classmethod
    def _parse_data_type(cls, element: ET.Element, ns: dict) -> str:
        """Parse data type from element."""
        # Look for Basic types
        basic = element.find('.//sila:Basic', ns)
        if basic is not None:
            return basic.text or "String"
        
        # Look for Constrained type
        constrained = element.find('.//sila:Constrained', ns)
        if constrained is not None:
            inner = constrained.find('.//sila:Basic', ns)
            if inner is not None:
                return inner.text or "String"
        
        # Look for List type
        list_type = element.find('.//sila:List', ns)
        if list_type is not None:
            return "List"
        
        return "String"


# =============================================================================
#                     SiLA2 COMMON SERVICER
# =============================================================================

class SiLA2CommonServicer:
    """
    SiLA2Common gRPC Service Implementation.
    
    Provides:
    - GetServerInfo: Server metadata
    - GetFeatures: Available features/commands
    - GetStatus: Current server and hardware status
    - ExecuteCommand: Generic command execution
    - GetProperty: Generic property access
    """
    
    def __init__(
        self,
        metadata: ServerMetadata,
        feature_directory: Optional[str] = None,
        hardware_status_callback: Optional[Callable[[], Dict[str, Any]]] = None,
        command_executors: Optional[Dict[str, Callable]] = None
    ):
        """
        Initialize the servicer.
        
        Args:
            metadata: Server metadata
            feature_directory: Path to .sila.xml feature files
            hardware_status_callback: Function that returns hardware status dict
            command_executors: Dict mapping command_id to async executor function
        """
        self.metadata = metadata
        self.feature_directory = feature_directory
        self.hardware_status_callback = hardware_status_callback
        self.command_executors = command_executors or {}
        
        # Parsed features (cached)
        self._features: List[FeatureInfo] = []
        self._features_loaded = False
        
        # Server start time
        self._start_time = datetime.now()
    
    def _ensure_features_loaded(self):
        """Load features if not already loaded."""
        if self._features_loaded:
            return
        
        if self.feature_directory:
            self._features = self._load_features(Path(self.feature_directory))
        
        self._features_loaded = True
    
    def _load_features(self, directory: Path) -> List[FeatureInfo]:
        """Load all feature files from directory."""
        features = []
        
        if not directory.exists():
            logger.warning(f"Feature directory does not exist: {directory}")
            return features
        
        for sila_file in directory.glob("*.sila.xml"):
            feature = FeatureParser.parse_feature_file(sila_file)
            if feature:
                features.append(feature)
        
        logger.info(f"Loaded {len(features)} features from {directory}")
        return features
    
    def register_command(self, command_id: str, executor: Callable):
        """
        Register a command executor.
        
        The executor should be an async function with signature:
            async def executor(parameters: Dict[str, Any]) -> Dict[str, Any]
        """
        self.command_executors[command_id] = executor
    
    # =========================================================================
    #                      gRPC SERVICE METHODS
    # =========================================================================
    
    async def GetServerInfo(self, request, context):
        """Get server metadata."""
        # Build response as dict (actual proto response depends on generated code)
        uptime = (datetime.now() - self._start_time).total_seconds()
        
        return {
            'server_name': self.metadata.server_name,
            'server_type': self.metadata.server_type,
            'vendor': self.metadata.vendor,
            'model': self.metadata.model,
            'serial_number': self.metadata.serial_number,
            'server_version': self.metadata.server_version,
            'sila_version': self.metadata.sila_version,
            'description': self.metadata.description,
            'host': platform.node(),
            'uptime_seconds': int(uptime),
            'capabilities': self.metadata.capabilities
        }
    
    async def GetFeatures(self, request, context):
        """Get available features and commands."""
        self._ensure_features_loaded()
        
        features = []
        for f in self._features:
            feature_dict = {
                'identifier': f.identifier,
                'display_name': f.display_name,
                'description': f.description,
                'category': f.category,
                'version': f.version,
                'commands': f.commands,
                'properties': f.properties
            }
            features.append(feature_dict)
        
        return {'features': features}
    
    async def GetStatus(self, request, context):
        """Get current server and hardware status."""
        # Get hardware status from callback
        hardware_status = {}
        if self.hardware_status_callback:
            try:
                hardware_status = self.hardware_status_callback()
                if asyncio.iscoroutine(hardware_status):
                    hardware_status = await hardware_status
            except Exception as e:
                logger.error(f"Error getting hardware status: {e}")
                hardware_status = {'error': str(e)}
        
        return {
            'server_status': 'running',
            'hardware_status': hardware_status.get('status', 'unknown'),
            'hardware_connected': hardware_status.get('connected', False),
            'is_busy': hardware_status.get('busy', False),
            'last_error': hardware_status.get('last_error', ''),
            'details': hardware_status
        }
    
    async def ExecuteCommand(self, request, context):
        """
        Execute a command generically.
        
        This enables fully dynamic command execution without hardcoded handlers.
        """
        # Extract from request
        command_id = getattr(request, 'command_id', '') or request.get('command_id', '')
        parameters_json = getattr(request, 'parameters_json', '') or request.get('parameters_json', '{}')
        
        try:
            parameters = json.loads(parameters_json) if parameters_json else {}
        except json.JSONDecodeError as e:
            return {
                'success': False,
                'error': f'Invalid JSON parameters: {e}',
                'result_json': '{}'
            }
        
        # Find executor
        executor = self.command_executors.get(command_id)
        if not executor:
            return {
                'success': False,
                'error': f'Unknown command: {command_id}',
                'result_json': '{}'
            }
        
        # Execute
        try:
            result = executor(parameters)
            if asyncio.iscoroutine(result):
                result = await result
            
            return {
                'success': True,
                'error': '',
                'result_json': json.dumps(result) if result else '{}'
            }
        except Exception as e:
            logger.exception(f"Command {command_id} failed")
            return {
                'success': False,
                'error': str(e),
                'result_json': '{}'
            }
    
    async def GetProperty(self, request, context):
        """Get a property value."""
        property_id = getattr(request, 'property_id', '') or request.get('property_id', '')
        
        # Property getters could be registered similar to command executors
        # For now, return not implemented
        return {
            'success': False,
            'error': f'Property access not implemented: {property_id}',
            'value_json': '{}'
        }


# =============================================================================
#                     HELPER FUNCTIONS
# =============================================================================

def create_common_servicer_for_server(
    server_name: str,
    server_type: str,
    feature_dir: str,
    hardware_callback: Optional[Callable] = None,
    **metadata_kwargs
) -> SiLA2CommonServicer:
    """
    Factory function to create a SiLA2CommonServicer.
    
    Args:
        server_name: Name of the server
        server_type: Type identifier (e.g., "opentrons", "plate_reader")
        feature_dir: Path to features directory
        hardware_callback: Optional callback for hardware status
        **metadata_kwargs: Additional ServerMetadata fields
    
    Returns:
        Configured SiLA2CommonServicer
    """
    metadata = ServerMetadata(
        server_name=server_name,
        server_type=server_type,
        **metadata_kwargs
    )
    
    return SiLA2CommonServicer(
        metadata=metadata,
        feature_directory=feature_dir,
        hardware_status_callback=hardware_callback
    )


# =============================================================================
#                     JSON-BASED IMPLEMENTATION
# =============================================================================

class SimpleSiLA2CommonHandler:
    """
    Simple JSON-based SiLA2Common handler.
    
    For servers that don't use gRPC generated code, this provides
    a simple JSON-in/JSON-out interface.
    """
    
    def __init__(
        self,
        metadata: ServerMetadata,
        feature_directory: Optional[str] = None
    ):
        self.servicer = SiLA2CommonServicer(
            metadata=metadata,
            feature_directory=feature_directory
        )
    
    async def handle_request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle a JSON request.
        
        Args:
            method: Method name (GetServerInfo, GetFeatures, etc.)
            params: Method parameters
        
        Returns:
            JSON-serializable response dict
        """
        if method == "GetServerInfo":
            return await self.servicer.GetServerInfo(params, None)
        elif method == "GetFeatures":
            return await self.servicer.GetFeatures(params, None)
        elif method == "GetStatus":
            return await self.servicer.GetStatus(params, None)
        elif method == "ExecuteCommand":
            return await self.servicer.ExecuteCommand(params, None)
        elif method == "GetProperty":
            return await self.servicer.GetProperty(params, None)
        else:
            return {'error': f'Unknown method: {method}'}
    
    def register_command(self, command_id: str, executor: Callable):
        """Register a command executor."""
        self.servicer.register_command(command_id, executor)


# =============================================================================
#                     EXAMPLE USAGE
# =============================================================================

if __name__ == "__main__":
    # Example: Create servicer for Opentrons server
    async def example():
        metadata = ServerMetadata(
            server_name="OpentronsFlex",
            server_type="opentrons",
            vendor="Opentrons",
            model="Flex",
            serial_number="OT-001",
            description="Opentrons Flex liquid handling robot"
        )
        
        servicer = SiLA2CommonServicer(
            metadata=metadata,
            feature_directory="features/"
        )
        
        # Register command executors
        async def execute_transfer(params):
            print(f"Executing transfer: {params}")
            return {'status': 'completed'}
        
        servicer.register_command("Transfer", execute_transfer)
        
        # Test GetServerInfo
        info = await servicer.GetServerInfo({}, None)
        print(f"Server Info: {json.dumps(info, indent=2)}")
        
        # Test GetFeatures
        features = await servicer.GetFeatures({}, None)
        print(f"Features: {len(features.get('features', []))}")
    
    asyncio.run(example())
