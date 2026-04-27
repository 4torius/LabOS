#!/usr/bin/env python3
"""
Plug & Play Discovery Module
============================

TRUE plug-and-play SiLA2 server discovery.
NO HARDCODED LISTS - servers are the ONLY source of truth.

Discovery methods:
1. mDNS/DNS-SD (SiLA2 standard) - automatic network discovery via Zeroconf
2. Port scanning (50051-50100) - find running gRPC servers (fallback)
3. Directory scanning - find SiLA2 server folders with features/*.sila.xml
4. gRPC reflection - query servers for their capabilities

To add a new instrument:
1. Create SiLA2/YourInstrumentSiLA2Server/
2. Add features/*.sila.xml files describing commands
3. Implement the server with SiLA2Common service
4. Start the server - it's automatically discovered!

NO CODE CHANGES NEEDED IN THIS FILE OR ANYWHERE ELSE.
"""

from __future__ import annotations  # Defer type hint evaluation to avoid NameError

import asyncio
import logging
import os
import re
import socket
import xml.etree.ElementTree as ET
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, Callable

try:
    import grpc
    GRPC_AVAILABLE = True
except ImportError:
    GRPC_AVAILABLE = False

# mDNS/Zeroconf for automatic SiLA2 discovery (optional but recommended)
try:
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf, ServiceInfo
    from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False

logger = logging.getLogger(__name__)


#                           CONFIGURATION

def _load_discovery_config() -> Dict[str, Any]:
    """Load discovery configuration from lab_config.yaml."""
    from src.config_schema import load_lab_config
    config_path = Path(__file__).parent.parent / "lab_config.yaml"
    defaults = {
        "enabled": True,
        "scan_interval": 30,
        "service_type": "_sila2._tcp.local."
    }
    try:
        config, _ = load_lab_config(config_path, apply_defaults=False, strict=False)
        return config.get("discovery", defaults)
    except Exception:
        return defaults


#                           DATA STRUCTURES

@dataclass
class PnPParameter:
    """A parameter for a command or property."""
    identifier: str
    display_name: str = ""
    description: str = ""
    data_type: str = "String"
    required: bool = True
    default_value: str = ""
    constraints: List[str] = field(default_factory=list)
    ui_hint: str = ""  # Hint for UI: "recipe", "analysis", "hal_config", "liquid_class", "location", "labware"
    
    def infer_ui_hint(self) -> str:
        """Infer UI hint from parameter identifier if not explicitly set."""
        if self.ui_hint:
            return self.ui_hint
        
        name_lower = self.identifier.lower()
        
        # Mobile robot task ID (for dropdown from MobileSiLA2Server)
        # Match TaskId parameter in TaskManagement feature
        if name_lower in ['taskid', 'task_id'] or name_lower == 'starttaskid':
            return 'mobile_task'
        
        # Recipe files
        if any(x in name_lower for x in ['recipe', 'recipename', 'recipefile']):
            return 'recipe'
        
        # Analysis/Protocol files (Tecan .mdfx)
        if any(x in name_lower for x in ['analysis', 'analysisfile', 'protocol', 'measurement']):
            return 'analysis'
        
        # HAL/Hardware config
        if any(x in name_lower for x in ['configname', 'config', 'halconfig', 'hardwareconfig', 'setup']):
            return 'hal_config'
        
        # Liquid classes
        if any(x in name_lower for x in ['liquidclass', 'liquid_class']):
            return 'liquid_class'
        
        # Locations/stations
        if any(x in name_lower for x in ['source', 'destination', 'location', 'station', 'from', 'to']):
            return 'location'
        
        # Tip rack types (specific for RefillTipRack command)
        if any(x in name_lower for x in ['rackslot', 'racktype', 'rack_type', 'rack_slot', 'tiprack']):
            return 'tip_rack'
        
        # Labware types (but NOT ID fields like "plate_id", "plateid")
        # ID fields should remain free text, not dropdowns
        is_id_field = 'id' in name_lower or name_lower.endswith('id')
        if not is_id_field and any(x in name_lower for x in ['labware', 'rack', 'plate']):
            return 'labware'
        
        return ''


@dataclass
class PnPCommand:
    """A SiLA2 command definition."""
    identifier: str
    display_name: str = ""
    description: str = ""
    observable: bool = False
    parameters: List[PnPParameter] = field(default_factory=list)
    responses: List[PnPParameter] = field(default_factory=list)


@dataclass
class PnPProperty:
    """A SiLA2 property definition."""
    identifier: str
    display_name: str = ""
    description: str = ""
    data_type: str = "String"
    observable: bool = False
    readonly: bool = True


@dataclass
class PnPFeature:
    """A SiLA2 feature (collection of commands and properties)."""
    identifier: str
    display_name: str = ""
    description: str = ""
    version: str = "1.0.0"
    category: str = ""
    commands: List[PnPCommand] = field(default_factory=list)
    properties: List[PnPProperty] = field(default_factory=list)
    
    def get_command(self, identifier: str) -> Optional[PnPCommand]:
        """Get command by identifier."""
        for cmd in self.commands:
            if cmd.identifier == identifier:
                return cmd
        return None


@dataclass
class PnPServer:
    """A discovered SiLA2 server with all its capabilities."""
    # Identity
    name: str                               # Human-readable name from server
    host: str = "localhost"
    port: int = 0
    uuid: str = ""
    
    # Metadata (from server's GetServerInfo)
    server_type: str = ""                   # "plate_reader", "liquid_handler", etc.
    vendor: str = ""
    model: str = ""                         # Hardware model (e.g., "Infinite M200 Pro")
    version: str = ""
    description: str = ""
    serial_number: str = ""
    capabilities: List[str] = field(default_factory=list)
    
    # Features (from .sila.xml or GetFeatures)
    features: List[PnPFeature] = field(default_factory=list)
    
    # Status
    server_online: bool = False             # gRPC server responding
    hardware_online: bool = False           # Physical instrument connected
    hardware_status: str = "unknown"
    
    # Source info
    server_dir: str = ""                    # Directory path if discovered from filesystem
    discovered_via: str = ""                # "port_scan", "directory", "mdns"
    
    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"
    
    @property
    def connected(self) -> bool:
        return self.server_online and self.hardware_online
    
    def get_all_commands(self) -> List[tuple]:
        """Get all commands: [(feature_id, command_id, command_obj), ...]"""
        result = []
        for feature in self.features:
            for cmd in feature.commands:
                result.append((feature.identifier, cmd.identifier, cmd))
        return result
    
    def find_command(self, command_id: str) -> Optional[tuple]:
        """Find a command by ID across all features. Returns (feature, command) or None."""
        for feature in self.features:
            for cmd in feature.commands:
                if cmd.identifier == command_id:
                    return (feature, cmd)
        return None


#                         XML PARSER (SiLA2 FDL)

class SiLAXMLParser:
    """Parse SiLA2 Feature Definition Language (FDL) XML files."""
    
    def parse_file(self, filepath: str) -> Optional[PnPFeature]:
        """Parse a .sila.xml file and return a PnPFeature."""
        try:
            tree = ET.parse(filepath)
            root = tree.getroot()
            return self._parse_feature(root)
        except Exception as e:
            logger.warning(f"Error parsing {filepath}: {e}")
            return None
    
    def _parse_feature(self, root: ET.Element) -> PnPFeature:
        """Parse Feature element."""
        # Detect namespace
        ns_match = re.match(r'\{(.+)\}', root.tag)
        prefix = "{" + ns_match.group(1) + "}" if ns_match else ""
        
        identifier = self._get_text(root, f"{prefix}Identifier") or "Unknown"
        
        feature = PnPFeature(
            identifier=identifier,
            display_name=self._get_text(root, f"{prefix}DisplayName") or identifier,
            description=self._get_text(root, f"{prefix}Description") or "",
            version=root.get("FeatureVersion", "1.0.0"),
            category=root.get("Category", "")
        )
        
        # Parse commands
        for cmd_elem in root.findall(f".//{prefix}Command"):
            cmd = self._parse_command(cmd_elem, prefix)
            if cmd:
                feature.commands.append(cmd)
        
        # Parse properties
        for prop_elem in root.findall(f".//{prefix}Property"):
            prop = self._parse_property(prop_elem, prefix)
            if prop:
                feature.properties.append(prop)
        
        return feature
    
    def _parse_command(self, elem: ET.Element, prefix: str) -> Optional[PnPCommand]:
        """Parse Command element."""
        identifier = self._get_identifier(elem, prefix)
        if not identifier:
            return None
        
        cmd = PnPCommand(
            identifier=identifier,
            display_name=self._get_text(elem, f"{prefix}DisplayName") or identifier,
            description=self._get_text(elem, f"{prefix}Description") or "",
            observable=self._get_observable(elem, prefix)
        )
        
        # Parse parameters
        for param_elem in elem.findall(f".//{prefix}Parameter"):
            param = self._parse_parameter(param_elem, prefix)
            if param:
                cmd.parameters.append(param)
        
        return cmd
    
    def _parse_parameter(self, elem: ET.Element, prefix: str) -> Optional[PnPParameter]:
        """Parse Parameter element."""
        identifier = self._get_identifier(elem, prefix)
        if not identifier:
            return None
        
        param = PnPParameter(
            identifier=identifier,
            display_name=self._get_text(elem, f"{prefix}DisplayName") or identifier,
            description=self._get_text(elem, f"{prefix}Description") or "",
            data_type=self._parse_data_type(elem, prefix),
            constraints=self._parse_constraints(elem, prefix) or []
        )
        return param
    
    def _parse_property(self, elem: ET.Element, prefix: str) -> Optional[PnPProperty]:
        """Parse Property element."""
        identifier = self._get_identifier(elem, prefix)
        if not identifier:
            return None
        
        return PnPProperty(
            identifier=identifier,
            display_name=self._get_text(elem, f"{prefix}DisplayName") or identifier,
            description=self._get_text(elem, f"{prefix}Description") or "",
            data_type=self._parse_data_type(elem, prefix),
            observable=self._get_observable(elem, prefix)
        )
    
    def _parse_data_type(self, elem: ET.Element, prefix: str) -> str:
        """Extract data type from element."""
        basic = elem.find(f".//{prefix}Basic")
        if basic is not None and basic.text:
            return basic.text
        return "String"
    
    def _parse_constraints(self, elem: ET.Element, prefix: str) -> List[str]:
        """Extract constraint values (Set or Constraints/AllowedValue)."""
        values = []
        # Format 1: Set/Value
        for value_elem in elem.findall(f".//{prefix}Set/{prefix}Value"):
            if value_elem.text:
                values.append(value_elem.text)
        # Format 2: Constraints/AllowedValue (SiLA2 FDL)
        for value_elem in elem.findall(f".//{prefix}Constraints/{prefix}AllowedValue"):
            if value_elem.text:
                values.append(value_elem.text)
        # Also check without prefix (some implementations)
        if not values:
            for value_elem in elem.findall(".//Set/Value"):
                if value_elem.text:
                    values.append(value_elem.text)
            for value_elem in elem.findall(".//Constraints/AllowedValue"):
                if value_elem.text:
                    values.append(value_elem.text)
        return values
    
    def _get_text(self, elem: ET.Element, tag: str) -> Optional[str]:
        """Get text from child element."""
        child = elem.find(tag)
        if child is not None and child.text:
            return child.text.strip()
        return None
    
    def _get_identifier(self, elem: ET.Element, prefix: str) -> Optional[str]:
        """Get identifier - check both attribute and child element."""
        # First check attribute (SiLA2 FDL format 2)
        if elem.get("Identifier"):
            return elem.get("Identifier")
        # Then check child element (SiLA2 FDL format 1)
        return self._get_text(elem, f"{prefix}Identifier")
    
    def _get_observable(self, elem: ET.Element, prefix: str) -> bool:
        """Get observable flag - check both attribute and child element."""
        # Check attribute first
        obs_attr = elem.get("Observable")
        if obs_attr:
            return obs_attr.lower() in ("yes", "true", "1")
        # Then check child element
        obs_text = self._get_text(elem, f"{prefix}Observable")
        return obs_text == "Yes" if obs_text else False


#                         PLUG & PLAY DISCOVERY

class PnPDiscovery:
    """
    TRUE Plug & Play Discovery.
    
    NO HARDCODED LISTS. Servers are discovered by:
    1. mDNS/DNS-SD (SiLA2 standard) - automatic network discovery
    2. Directory scanning - find SiLA2 server folders with features/*.sila.xml
    3. Port scanning - fallback to find running gRPC servers
    4. gRPC queries - get server metadata via SiLA2Common.GetServerInfo
    
    The server's self-description is the ONLY source of truth.
    """
    
    # Port range to scan for SiLA2 servers (fallback only)
    PORT_RANGE_START = 50051
    PORT_RANGE_END = 50100
    
    def __init__(self, base_dir: Optional[Union[str, Path]] = None):
        """
        Initialize discovery.
        
        Args:
            base_dir: Base directory containing SiLA2/ folder
        """
        self.base_dir = Path(base_dir).absolute() if base_dir else Path.cwd()
        self.xml_parser = SiLAXMLParser()
        self.servers: Dict[str, PnPServer] = {}
        self._discovery_config = _load_discovery_config()
        
        # mDNS discovery state
        self._zeroconf: Optional[Any] = None
        self._browser: Optional[Any] = None
        self._mdns_servers: Dict[str, Dict[str, Any]] = {}
        self._on_server_discovered: Optional[Callable[[PnPServer], None]] = None
    
    def set_discovery_callback(self, callback: Optional[Callable[[PnPServer], None]]):
        """Set callback for when new servers are discovered via mDNS."""
        self._on_server_discovered = callback
    
    def _find_server_by_address(self, host: str, port: int) -> Optional[tuple]:
        """
        Find existing server by host:port address.
        Returns (server_key, server) if found, None otherwise.
        
        This is used to deduplicate servers discovered via multiple methods
        (mDNS, config, port scan) that are actually the same server.
        
        Deduplication logic:
        - localhost/127.0.0.1/::1 are equivalent
        - If same port and one host is localhost, consider as same server
          (handles mDNS discovery of local servers via network IP)
        """
        localhost_variants = {"localhost", "127.0.0.1", "::1"}
        host_is_localhost = host in localhost_variants
        
        for key, server in self.servers.items():
            if server.port != port:
                continue
            
            server_is_localhost = server.host in localhost_variants
            
            # Case 1: Both are localhost variants
            if host_is_localhost and server_is_localhost:
                return (key, server)
            
            # Case 2: Exact host match
            if host == server.host:
                return (key, server)
            
            # Case 3: Same port, one is localhost - likely same local server
            # This handles mDNS discovering a server via its network IP (e.g., 169.254.x.x)
            # while config has it as localhost
            if host_is_localhost or server_is_localhost:
                logger.debug(f"Dedup: same port {port}, merging {host} with {server.host}")
                return (key, server)
        
        return None
    
    async def discover_all(
        self,
        use_mdns: bool = True,
        scan_ports: bool = True,
        scan_directories: bool = True,
        timeout: float = 2.0
    ) -> Dict[str, PnPServer]:
        """
        Discover all SiLA2 servers.
        
        Discovery priority:
        1. mDNS/DNS-SD (SiLA2 standard) - automatic and preferred
        2. Directory scanning - for local development
        3. Port scanning - fallback only
        
        Args:
            use_mdns: Use mDNS/DNS-SD discovery (SiLA2 standard)
            scan_ports: Scan port range for running servers (fallback)
            scan_directories: Scan SiLA2/ directory for server folders
            timeout: Connection timeout per server
        
        Returns:
            Dict of {server_key: PnPServer}
        """
        self.servers = {}
        
        # Step 1: mDNS discovery (SiLA2 standard - PREFERRED)
        if use_mdns and ZEROCONF_AVAILABLE and self._discovery_config.get("enabled", True):
            await self._discover_via_mdns(timeout)
        
        # Step 2: Discover remote servers from config (lab_config.yaml)
        await self._discover_from_config(timeout)
        
        # Step 3: Scan directories for server definitions (local development)
        if scan_directories:
            await self._discover_from_directories()
        
        # Step 4: Scan ports to find running servers (fallback)
        # Only scan ports not already discovered via mDNS
        if scan_ports:
            await self._discover_from_ports(timeout)
        
        # Step 4: Query each online server for its metadata
        await self._query_server_metadata(timeout)
        
        return self.servers
    
    async def _discover_from_config(self, timeout: float = 2.0):
        """
        Discover servers from lab_config.yaml servers section.
        
        This is used for remote servers that can't be discovered via
        directory scan (e.g., Mobile Robot on 10.16.0.114).
        """
        config_path = self.base_dir / "lab_config.yaml"
        if not config_path.exists():
            return
        
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f)
            
            servers_config = config.get("servers", {})
            
            for srv_key, srv_config in servers_config.items():
                if not srv_config.get("enabled", True):
                    continue
                
                host = srv_config.get("host", "localhost")
                port = srv_config.get("port", 0)
                name = srv_config.get("name", srv_key)
                
                # Skip if already discovered by name key
                server_key = self._make_server_key(srv_key)
                if server_key in self.servers:
                    # Update host if different (config overrides directory)
                    if host != "localhost":
                        self.servers[server_key].host = host
                    continue
                
                # Check for duplicate by address (host:port)
                if port > 0:
                    existing = self._find_server_by_address(host, port)
                    if existing:
                        existing_key, existing_server = existing
                        # Config has higher priority - update existing server
                        existing_server.name = name  # Use configured name
                        if host != "localhost":
                            existing_server.host = host
                        logger.info(f"Config: Merged {name} into existing server at {host}:{port}")
                        continue
                
                # Check if this is a remote server (not localhost)
                is_remote = host != "localhost" and host != "127.0.0.1"

                # For remote servers, verify via gRPC (not just TCP) — double timeout for WiFi
                if is_remote and port > 0:
                    tcp_ok = self._is_port_open(host, port, timeout=timeout * 2)
                    is_online = tcp_ok and self._is_grpc_server(host, port, timeout=timeout)

                    # Week 3: no local XML loading — features come from GetFeatures gRPC
                    server_dir = srv_config.get("directory", "")
                    server = PnPServer(
                        name=name,
                        host=host,
                        port=port,
                        features=[],
                        server_dir=server_dir,
                        discovered_via="config",
                        server_online=is_online,
                        hardware_online=is_online,
                        hardware_status="idle" if is_online else "offline"
                    )
                    
                    self.servers[server_key] = server
                    status = "ONLINE" if is_online else "OFFLINE"
                    logger.info(f"Config server: {name} ({host}:{port}) - {status}")
                    
        except Exception as e:
            logger.warning(f"Error loading servers from config: {e}")
    
    async def _discover_via_mdns(self, timeout: float = 3.0):
        """
        Discover SiLA2 servers via mDNS/DNS-SD (SiLA2 standard).
        
        SiLA2 servers advertise themselves as _sila2._tcp.local. services.
        This is the preferred discovery method per SiLA2 specification.
        """
        if not ZEROCONF_AVAILABLE:
            logger.info("Zeroconf not available, skipping mDNS discovery")
            return
        
        service_type = self._discovery_config.get("service_type", "_sila2._tcp.local.")
        logger.info(f"Starting mDNS discovery for {service_type}")
        
        discovered = []
        
        class SiLA2ServiceListener(ServiceListener):
            """Listener for SiLA2 mDNS service announcements."""
            
            def __init__(self, zc: Zeroconf):
                self.zc = zc
            
            def add_service(self, zc: Zeroconf, service_type: str, name: str):
                info = zc.get_service_info(service_type, name)
                if info:
                    discovered.append(info)
            
            def remove_service(self, zc: Zeroconf, service_type: str, name: str):
                pass
            
            def update_service(self, zc: Zeroconf, service_type: str, name: str):
                info = zc.get_service_info(service_type, name)
                if info:
                    discovered.append(info)
        
        try:
            zc = Zeroconf()
            listener = SiLA2ServiceListener(zc)
            browser = ServiceBrowser(zc, service_type, listener)
            
            # Wait for discovery
            await asyncio.sleep(timeout)
            
            # Process discovered services
            for info in discovered:
                try:
                    # Extract server information from mDNS
                    if info.addresses:
                        addr = socket.inet_ntoa(info.addresses[0])
                    else:
                        addr = "localhost"
                    
                    port = info.port or 0
                    if port == 0:
                        continue  # Skip services without port
                        
                    name = info.name.replace(f".{service_type}", "")
                    
                    # Get properties from TXT records
                    properties = {}
                    if info.properties:
                        for key, value in info.properties.items():
                            if isinstance(key, bytes):
                                key = key.decode('utf-8')
                            if isinstance(value, bytes):
                                value = value.decode('utf-8')
                            properties[key] = value
                    
                    server_key = self._make_server_key(name)
                    
                    # Check for duplicate by name key
                    if server_key in self.servers:
                        logger.debug(f"mDNS: Server {name} already discovered by name key, skipping")
                        continue
                    
                    # Check for duplicate by address (host:port)
                    # This catches cases where the same server is discovered via mDNS 
                    # and config but with different names
                    existing = self._find_server_by_address(addr, port)
                    if existing:
                        existing_key, existing_server = existing
                        # Update existing server with mDNS info if it has more details
                        if properties.get("uuid"):
                            existing_server.uuid = properties["uuid"]
                        if properties.get("vendor"):
                            existing_server.vendor = properties["vendor"]
                        existing_server.server_online = True
                        existing_server.hardware_online = True
                        logger.info(f"mDNS: Merged {name} into existing server {existing_server.name} at {addr}:{port}")
                        continue
                    
                    server = PnPServer(
                        name=properties.get("name", name),
                        host=addr,
                        port=port,
                        uuid=properties.get("uuid", ""),
                        server_type=properties.get("type", ""),
                        vendor=properties.get("vendor", ""),
                        version=properties.get("version", ""),
                        description=properties.get("description", ""),
                        server_online=True,
                        hardware_online=True,
                        hardware_status="idle",
                        discovered_via="mdns"
                    )
                    
                    self.servers[server_key] = server
                    logger.info(f"Discovered via mDNS: {server.name} at {addr}:{port}")
                    
                    # Notify callback if set
                    if self._on_server_discovered:
                        try:
                            self._on_server_discovered(server)
                        except Exception as e:
                            logger.error(f"Discovery callback error: {e}")
                            
                except Exception as e:
                    logger.warning(f"Error processing mDNS service: {e}")
            
            browser.cancel()
            zc.close()
            
            logger.info(f"mDNS discovery found {len(discovered)} SiLA2 servers")
            
        except Exception as e:
            logger.error(f"mDNS discovery error: {e}")
    
    async def start_continuous_discovery(self) -> bool:
        """
        Start continuous mDNS discovery in the background.
        
        Servers will be discovered automatically as they come online.
        Use set_discovery_callback() to receive notifications.
        
        Returns:
            True if started successfully, False if Zeroconf not available
        """
        if not ZEROCONF_AVAILABLE:
            logger.warning("Zeroconf not available for continuous discovery")
            return False
        
        if self._zeroconf:
            return True  # Already running
        
        service_type = self._discovery_config.get("service_type", "_sila2._tcp.local.")
        
        class ContinuousListener(ServiceListener):
            def __init__(self, parent: "PnPDiscovery"):
                self.parent = parent
            
            def add_service(self, zc: Zeroconf, service_type: str, name: str):
                asyncio.create_task(self.parent._handle_mdns_service_added(zc, service_type, name))
            
            def remove_service(self, zc: Zeroconf, service_type: str, name: str):
                asyncio.create_task(self.parent._handle_mdns_service_removed(name))
            
            def update_service(self, zc: Zeroconf, service_type: str, name: str):
                asyncio.create_task(self.parent._handle_mdns_service_added(zc, service_type, name))
        
        try:
            self._zeroconf = Zeroconf()
            self._browser = ServiceBrowser(self._zeroconf, service_type, ContinuousListener(self))
            logger.info(f"Started continuous mDNS discovery for {service_type}")
            return True
        except Exception as e:
            logger.error(f"Failed to start continuous discovery: {e}")
            return False
    
    async def stop_continuous_discovery(self):
        """Stop continuous mDNS discovery."""
        if self._browser:
            self._browser.cancel()
            self._browser = None
        if self._zeroconf:
            self._zeroconf.close()
            self._zeroconf = None
        logger.info("Stopped continuous mDNS discovery")
    
    async def _handle_mdns_service_added(self, zc: Zeroconf, service_type: str, name: str):
        """Handle new mDNS service announcement."""
        try:
            info = zc.get_service_info(service_type, name)
            if not info:
                return
            
            if info.addresses:
                addr = socket.inet_ntoa(info.addresses[0])
            else:
                addr = "localhost"
            
            port = info.port or 0
            if port == 0:
                logger.warning(f"mDNS service {name} has no port, skipping")
                return
                
            server_name = name.replace(f".{service_type}", "")
            server_key = self._make_server_key(server_name)
            
            # Get properties
            properties = {}
            if info.properties:
                for key, value in info.properties.items():
                    if isinstance(key, bytes):
                        key = key.decode('utf-8')
                    if isinstance(value, bytes):
                        value = value.decode('utf-8')
                    properties[key] = value
            
            server = PnPServer(
                name=properties.get("name", server_name),
                host=addr,
                port=port,
                uuid=properties.get("uuid", ""),
                server_type=properties.get("type", ""),
                vendor=properties.get("vendor", ""),
                version=properties.get("version", ""),
                description=properties.get("description", ""),
                server_online=True,
                hardware_online=True,
                hardware_status="idle",
                discovered_via="mdns"
            )
            
            self.servers[server_key] = server
            logger.info(f"mDNS: Server online - {server.name} at {addr}:{port}")
            
            if self._on_server_discovered:
                try:
                    self._on_server_discovered(server)
                except Exception as e:
                    logger.error(f"Discovery callback error: {e}")
                    
        except Exception as e:
            logger.error(f"Error handling mDNS service add: {e}")
    
    async def _handle_mdns_service_removed(self, name: str):
        """Handle mDNS service removal."""
        try:
            # Find and mark server as offline
            server_key = self._make_server_key(name.split(".")[0])
            if server_key in self.servers:
                self.servers[server_key].server_online = False
                self.servers[server_key].hardware_online = False
                self.servers[server_key].hardware_status = "offline"
                logger.info(f"mDNS: Server offline - {name}")
        except Exception as e:
            logger.error(f"Error handling mDNS service remove: {e}")

    async def _discover_from_directories(self):
        """
        Discover servers from SiLA2/ directory structure.
        
        Looks for folders matching: *SiLA2Server or *Server
        with features/*.sila.xml files.
        """
        sila_dir = self.base_dir / "SiLA2"
        if not sila_dir.exists():
            logger.info(f"SiLA2 directory not found: {sila_dir}")
            return
        
        for item in os.listdir(sila_dir):
            item_path = sila_dir / item
            
            # Skip non-directories and special folders
            if not item_path.is_dir():
                continue
            if item.startswith(".") or item.startswith("_"):
                continue
            if item in ("temp", "Orchestrator", "__pycache__"):
                continue
            
            # Check if it looks like a SiLA2 server
            if not (item.endswith("Server") or item.endswith("SiLA2Server")):
                continue

            # Get full config from config file first — need port to verify it's real
            config = self._read_server_config(item_path)
            port = config["port"]

            # Require a valid port — without it we can't connect or deduplicate
            if not port:
                continue

            # Week 3: don't load features from local XML.
            # Features come from gRPC GetFeatures once the server is online.
            features = []
            
            # Create server entry
            server_key = self._make_server_key(item)
            server_name = config["name"] or self._format_server_name(item)
            
            # Check if already discovered (e.g., from config with remote host)
            if server_key in self.servers:
                # Update features from directory but preserve remote host & status
                existing = self.servers[server_key]
                existing.features = features
                existing.server_dir = str(item_path)
                if existing.discovered_via == "config":
                    existing.discovered_via = "config+directory"
                # Keep existing host (may be remote), status, etc.
                logger.info(f"Updated features for: {existing.name} (keeping host {existing.host})")
                continue
            
            # Check for duplicate by address (host:port) - catches mDNS discoveries
            if port > 0:
                existing_by_addr = self._find_server_by_address("localhost", port)
                if existing_by_addr:
                    existing_key, existing_server = existing_by_addr
                    # Update existing with directory info
                    existing_server.features = features
                    existing_server.server_dir = str(item_path)
                    if not existing_server.discovered_via.endswith("+directory"):
                        existing_server.discovered_via += "+directory"
                    logger.info(f"Directory: Merged {server_name} into existing {existing_server.name} at port {port}")
                    continue
            
            server = PnPServer(
                name=server_name,
                host="localhost",
                port=port,
                features=features,
                server_dir=str(item_path),
                discovered_via="directory",
                description=config["description"],
                vendor=config["vendor"],
                version=config["version"],
                hardware_status="idle"  # Default to idle - will be updated when queried
            )
            
            self.servers[server_key] = server
            logger.info(f"Discovered from directory: {server_name} (port {port})")
    
    async def _discover_from_ports(self, timeout: float = 2.0):
        """
        Check port status for discovered servers.
        
        Optimized: only checks ports from directory-discovered servers + common ports.
        Uses gRPC check (not just TCP port) to verify servers are actually running.
        """
        # Get ports we already know about from directory scan
        known_ports = {s.port for s in self.servers.values() if s.port > 0}
        
        # Also check a few common additional ports quickly
        additional_ports = {50054, 50055, 50056}  # Potential new servers
        ports_to_check = known_ports | additional_ports
        
        # Check ports sequentially with gRPC verification
        # Use _is_grpc_server for accurate detection (not just port open)
        grpc_ports = []
        for port in ports_to_check:
            # First quick check if port is open at all
            if self._is_port_open("localhost", port, timeout=0.1):
                # Then verify it's actually a gRPC server
                if self._is_grpc_server("localhost", port, timeout=0.5):
                    grpc_ports.append(port)
                    logger.debug(f"Port {port}: gRPC server confirmed")
                else:
                    logger.debug(f"Port {port}: open but not gRPC")
        
        logger.info(f"Found {len(grpc_ports)} gRPC servers (checked {len(ports_to_check)} ports)")
        
        # Mark servers as online/offline based on gRPC check
        # ONLY for localhost servers - remote servers are checked in _discover_from_config()
        for server in self.servers.values():
            # Skip remote servers - their status was already set in _discover_from_config()
            if server.host not in ("localhost", "127.0.0.1", ""):
                continue
                
            if server.port in grpc_ports:
                server.server_online = True
                # If server is online, assume hardware is also online (ready)
                # (servers that implement GetServerInfo will override this)
                server.hardware_online = True
                if server.hardware_status in ("unknown", "offline"):
                    server.hardware_status = "idle"
            elif server.port > 0:
                server.server_online = False
                server.hardware_online = False
                server.hardware_status = "offline"
        
        # For each gRPC port, check if it's a known server
        for port in grpc_ports:
            # Check if we already have this server from directory scan
            existing = self._find_server_by_port(port)
            if existing:
                existing.server_online = True
                existing.hardware_online = True  # Assume hw online if server online
                if existing.hardware_status in ("unknown", "offline"):
                    existing.hardware_status = "idle"
                logger.info(f"Port {port}: matched to {existing.name}")
                continue
            
            # New server - add it
            server_key = f"server_{port}"
            server = PnPServer(
                name=f"Server {port}",
                host="localhost",
                port=port,
                server_online=True,
                hardware_online=True,
                hardware_status="idle",
                discovered_via="port_scan"
            )
            self.servers[server_key] = server
            logger.info(f"Discovered from port scan: {server.name}")
    
    async def _query_server_metadata(self, timeout: float = 2.0):
        """
        Query each online server for its metadata via SiLA2Common service.
        Updates server info with data from the server itself.
        Runs queries in parallel for speed.
        """
        if not GRPC_AVAILABLE:
            return
        
        # Query all servers in parallel
        async def query_one(key: str, server: PnPServer):
            if not server.port:
                return
            
            # Skip if we know it's offline
            if not server.server_online:
                return
            
            try:
                await self._query_server_info(server, timeout)
            except Exception as e:
                logger.debug(f"Could not query {server.name} metadata: {e}")
        
        tasks = [query_one(key, server) for key, server in self.servers.items()]
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _query_server_info(self, server: PnPServer, timeout: float):
        """
        Query server via SiLA2Common.GetServerInfo and GetStatus.
        Falls back to GetFeatures if server info not available.
        """
        if not GRPC_AVAILABLE:
            return
        
        try:
            # Try to import generated stubs for SiLA2Common
            HAS_COMMON_STUBS = False
            try:
                from .grpc import SiLA2Common_pb2 as common_pb2
                from .grpc import SiLA2Common_pb2_grpc as common_grpc
                HAS_COMMON_STUBS = True
            except ImportError:
                pass
            
            # Fallback: try from SiLA2/ directory
            if not HAS_COMMON_STUBS:
                try:
                    import sys
                    sila2_dir = str(self.base_dir / "SiLA2")
                    if sila2_dir not in sys.path:
                        sys.path.insert(0, sila2_dir)
                    import SiLA2Common_pb2 as common_pb2
                    import SiLA2Common_pb2_grpc as common_grpc
                    HAS_COMMON_STUBS = True
                except ImportError:
                    pass
            
            if HAS_COMMON_STUBS:
                channel = grpc.aio.insecure_channel(server.address)
                stub = common_grpc.SiLA2ServerInfoStub(channel)
                
                try:
                    # Get server info
                    response = await asyncio.wait_for(
                        stub.GetServerInfo(common_pb2.GetServerInfoRequest()),
                        timeout=timeout
                    )
                    
                    # Update server with response data (using getattr for safety)
                    # Field numbers now match C# proto: server_name=1, server_type=2, etc.
                    if getattr(response, 'server_name', None):
                        server.name = response.server_name
                    if getattr(response, 'server_uuid', None):
                        server.uuid = response.server_uuid
                    if getattr(response, 'server_type', None):
                        server.server_type = response.server_type
                    if getattr(response, 'vendor', None):
                        server.vendor = response.vendor
                    if getattr(response, 'model', None):
                        server.model = response.model
                    if getattr(response, 'server_version', None):
                        server.version = response.server_version
                    if getattr(response, 'description', None):
                        server.description = response.description
                    if getattr(response, 'serial_number', None):
                        server.serial_number = response.serial_number
                    if getattr(response, 'capabilities', None):
                        server.capabilities = list(response.capabilities)
                    
                    if hasattr(response, 'hardware_connected'):
                        server.hardware_online = response.hardware_connected
                    if hasattr(response, 'hardware_status'):
                        server.hardware_status = response.hardware_status or "unknown"
                    
                    logger.info(f"Got server info from {server.name}: type={server.server_type}, hw={server.hardware_online}")
                    
                except asyncio.TimeoutError:
                    logger.debug(f"Timeout getting server info from {server.address}")
                except grpc.RpcError as e:
                    if e.code() == grpc.StatusCode.UNIMPLEMENTED:
                        logger.debug(f"Server {server.address} doesn't implement GetServerInfo")
                    else:
                        logger.debug(f"gRPC error from {server.address}: {e}")
                except Exception as e:
                    logger.debug(f"Error parsing server info from {server.address}: {e}")
                finally:
                    await channel.close()
            
            # Week 3: always fetch features via gRPC — server is the source of truth.
            # Local .sila.xml are for development reference only, not for discovery.
            if HAS_COMMON_STUBS:
                try:
                    channel = grpc.aio.insecure_channel(server.address)
                    stub = common_grpc.SiLA2ServerInfoStub(channel)
                    
                    features_response = await asyncio.wait_for(
                        stub.GetFeatures(common_pb2.GetFeaturesRequest()),
                        timeout=timeout
                    )
                    
                    # Convert gRPC features to PnPFeature objects
                    features = []
                    for grpc_feature in features_response.features:
                        commands = []
                        for grpc_cmd in grpc_feature.commands:
                            params = []
                            for grpc_param in grpc_cmd.parameters:
                                params.append(PnPParameter(
                                    identifier=grpc_param.identifier,
                                    display_name=grpc_param.display_name or grpc_param.identifier,
                                    description=grpc_param.description,
                                    data_type=grpc_param.data_type or "String",
                                    required=grpc_param.required,
                                    constraints=list(grpc_param.constraints) if grpc_param.constraints else []
                                ))
                            commands.append(PnPCommand(
                                identifier=grpc_cmd.identifier,
                                display_name=grpc_cmd.display_name or grpc_cmd.identifier,
                                description=grpc_cmd.description,
                                observable=grpc_cmd.observable,
                                parameters=params
                            ))
                        
                        features.append(PnPFeature(
                            identifier=grpc_feature.identifier,
                            display_name=grpc_feature.display_name or grpc_feature.identifier,
                            description=grpc_feature.description,
                            version=grpc_feature.version,
                            category=grpc_feature.category,
                            commands=commands
                        ))
                    
                    server.features = features
                    logger.info(f"Fetched {len(features)} features via gRPC from {server.name}")
                    
                    await channel.close()
                except asyncio.TimeoutError:
                    logger.debug(f"Timeout getting features from {server.address}")
                except grpc.RpcError as e:
                    if e.code() != grpc.StatusCode.UNIMPLEMENTED:
                        logger.debug(f"gRPC error getting features from {server.address}: {e}")
                except Exception as e:
                    logger.debug(f"Error getting features from {server.address}: {e}")
                
        except Exception as e:
            logger.debug(f"Error querying {server.name}: {e}")
    
    def _load_features_from_directory(self, server_dir: Path) -> List[PnPFeature]:
        """Load all features from a server directory."""
        features = []
        found_sila_xml = False
        features_dir_found = None
        
        # Check for features/ directory (preferred - SiLA2 standard)
        # On Windows, 'features' and 'Features' are the same - only check once
        for features_dir_name in ["features", "Features"]:
            features_dir = server_dir / features_dir_name
            if features_dir.exists():
                # Skip if we already found this directory (case-insensitive match)
                if features_dir_found and features_dir.resolve() == features_dir_found.resolve():
                    continue
                features_dir_found = features_dir
                
                for filename in os.listdir(features_dir):
                    if filename.endswith('.sila.xml'):
                        filepath = features_dir / filename
                        feature = self.xml_parser.parse_file(str(filepath))
                        if feature:
                            features.append(feature)
                            found_sila_xml = True
        
        # Only use Protos/ as fallback if no .sila.xml files found
        if not found_sila_xml:
            protos_dir_found = None
            for protos_dir_name in ["Protos", "protos"]:
                protos_dir = server_dir / protos_dir_name
                if protos_dir.exists():
                    # Skip if we already found this directory
                    if protos_dir_found and protos_dir.resolve() == protos_dir_found.resolve():
                        continue
                    protos_dir_found = protos_dir
                    
                    for filename in os.listdir(protos_dir):
                        if filename.endswith('.proto') and not filename.startswith('SiLA2Common'):
                            # Parse proto for command names (simplified)
                            feature = self._parse_proto_file(protos_dir / filename)
                            if feature:
                                features.append(feature)
        
        return features
    
    def _parse_proto_file(self, filepath: Path) -> Optional[PnPFeature]:
        """Parse a .proto file to extract service/rpc definitions."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Extract service name
            service_match = re.search(r'service\s+(\w+)\s*\{', content)
            if not service_match:
                return None
            
            service_name = service_match.group(1)
            
            feature = PnPFeature(
                identifier=service_name,
                display_name=self._camel_to_display(service_name),
                description=f"Service from {filepath.name}"
            )
            
            # Extract rpc methods
            rpc_pattern = r'rpc\s+(\w+)\s*\(\s*(\w+)\s*\)\s*returns\s*\(\s*(stream\s+)?(\w+)\s*\)'
            for match in re.finditer(rpc_pattern, content):
                method_name = match.group(1)
                is_streaming = match.group(3) is not None
                
                # Skip getters - they're properties
                if method_name.startswith('Get') and not method_name.startswith('GetAll'):
                    continue
                
                cmd = PnPCommand(
                    identifier=method_name,
                    display_name=self._camel_to_display(method_name),
                    observable=is_streaming
                )
                
                # TODO: Parse request message for parameters
                
                feature.commands.append(cmd)
            
            return feature
            
        except Exception as e:
            logger.warning(f"Error parsing proto {filepath}: {e}")
            return None
    
    def _read_server_config(self, server_dir: Path) -> dict:
        """
        Read full server configuration from config.yaml or appsettings.json.
        Returns dict with: port, name, description, vendor, version
        """
        result = {"port": 0, "name": "", "description": "", "vendor": "", "version": ""}
        
        # Try config.yaml first (Python servers)
        for config_name in ["config.yaml", "config.yml"]:
            config_path = server_dir / config_name
            if config_path.exists():
                try:
                    import yaml
                    with open(config_path, encoding='utf-8') as f:
                        config = yaml.safe_load(f)
                    if config:
                        # Handle nested 'server' section
                        server_section = config.get("server", config)
                        result["port"] = int(server_section.get("port", 0))
                        result["name"] = server_section.get("name", "")
                        result["description"] = server_section.get("description", "")
                        result["vendor"] = server_section.get("vendor", "")
                        result["version"] = server_section.get("version", "")
                        return result
                except Exception as e:
                    logger.debug(f"Failed to read {config_path}: {e}")
        
        # Try appsettings.json (.NET/C# servers)
        settings_path = server_dir / "appsettings.json"
        if settings_path.exists():
            try:
                import json
                with open(settings_path, encoding='utf-8') as f:
                    content = f.read()
                    # Remove C#-style comments
                    lines = content.split('\n')
                    clean_lines = [l.split('//')[0] for l in lines]
                    content = '\n'.join(clean_lines)
                    config = json.loads(content)
                
                # Try multiple port key names
                port_keys = ["Port", "port", "GrpcPort", "grpcPort", "ServerPort"]
                for key in port_keys:
                    if key in config:
                        result["port"] = int(config[key])
                        break
                
                # Check nested sections
                for section_name, section in config.items():
                    if isinstance(section, dict):
                        for key in port_keys:
                            if key in section and result["port"] == 0:
                                result["port"] = int(section[key])
                        # Also get other info
                        result["name"] = result["name"] or section.get("ServerName", section.get("Name", ""))
                        result["description"] = result["description"] or section.get("Description", "")
                        result["vendor"] = result["vendor"] or section.get("Vendor", "")
                        result["version"] = result["version"] or section.get("Version", "")
                        
            except Exception as e:
                logger.debug(f"Failed to read appsettings.json: {e}")
        
        return result
    
    def _read_port_from_config(self, server_dir: Path) -> int:
        """Read server port from config file."""
        # Try config.yaml
        for config_name in ["config.yaml", "config.yml"]:
            config_path = server_dir / config_name
            if config_path.exists():
                try:
                    import yaml
                    with open(config_path) as f:
                        config = yaml.safe_load(f)
                    if config:
                        if "port" in config:
                            return int(config["port"])
                        if "server" in config and "port" in config["server"]:
                            return int(config["server"]["port"])
                except Exception:
                    pass
        
        # Try appsettings.json (multiple formats: .NET/C# servers)
        settings_path = server_dir / "appsettings.json"
        if settings_path.exists():
            try:
                import json
                with open(settings_path, encoding='utf-8') as f:
                    content = f.read()
                    # Remove C#-style comments (// comment)
                    lines = content.split('\n')
                    clean_lines = [l.split('//')[0] for l in lines]
                    content = '\n'.join(clean_lines)
                    config = json.loads(content)
                
                # Try multiple possible keys
                port_keys = ["Port", "port", "GrpcPort", "grpcPort", "ServerPort"]
                
                # Check root level
                for key in port_keys:
                    if key in config:
                        return int(config[key])
                
                # Check nested (SiLAServer, TecanSiLA2Server, etc.)
                for section_name, section in config.items():
                    if isinstance(section, dict):
                        for key in port_keys:
                            if key in section:
                                return int(section[key])
            except Exception as e:
                logger.debug(f"Failed to read appsettings.json: {e}")
        
        # Return 0 to indicate unknown port
        return 0
    
    def _is_port_open(self, host: str, port: int, timeout: float = 1.0) -> bool:
        """Check if a port is open."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except Exception:
            return False
    
    def _is_grpc_server(self, host: str, port: int, timeout: float = 1.0) -> bool:
        """
        Check if a gRPC server is actually responding on the given port.
        This is more reliable than just checking if port is open.
        
        Returns True only if:
        1. Port is open AND
        2. Server responds with valid gRPC protocol
        """
        if not GRPC_AVAILABLE:
            # Fall back to port check if gRPC not available
            return self._is_port_open(host, port, timeout)
        
        try:
            import grpc
            
            # Create a channel with short timeout
            address = f"{host}:{port}"
            channel = grpc.insecure_channel(
                address,
                options=[
                    ('grpc.connect_timeout_ms', int(timeout * 1000)),
                    ('grpc.enable_http_proxy', 0),
                ]
            )
            
            # Try to check channel connectivity
            # This sends a real gRPC request to verify the server
            try:
                # Use channel_ready_future with timeout
                grpc.channel_ready_future(channel).result(timeout=timeout)
                channel.close()
                return True
            except grpc.FutureTimeoutError:
                channel.close()
                return False
            except Exception:
                channel.close()
                return False
                
        except Exception as e:
            logger.debug(f"gRPC check failed for {host}:{port}: {e}")
            return False
    
    def _find_server_by_port(self, port: int) -> Optional[PnPServer]:
        """Find a server by its port."""
        for server in self.servers.values():
            if server.port == port:
                return server
        return None
    
    def _make_server_key(self, folder_name: str) -> str:
        """Make a unique key from folder name."""
        # "TecanSiLA2Server" -> "tecan"
        key = folder_name.lower()
        key = key.replace("sila2server", "").replace("server", "")
        key = key.strip("_- ")
        return key or folder_name.lower()
    
    def _format_server_name(self, folder_name: str) -> str:
        """Format folder name as display name."""
        # "TecanSiLA2Server" -> "Tecan"
        name = folder_name.replace("SiLA2Server", "").replace("Server", "")
        # CamelCase to spaces
        name = re.sub(r'([A-Z])', r' \1', name).strip()
        return name or folder_name
    
    def _camel_to_display(self, name: str) -> str:
        """Convert CamelCase to Display Name."""
        return re.sub(r'([A-Z])', r' \1', name).strip()
    
    #                         PUBLIC API
    
    def get_server(self, name_or_key: str) -> Optional[PnPServer]:
        """
        Get a server by name or key.
        
        Supports:
        - Exact key match: "tecan"
        - Exact name match: "Tecan"
        - Partial match: "tecan" matches "Tecan M200 Pro"
        """
        name_lower = name_or_key.lower()
        
        # Try exact key match
        if name_lower in self.servers:
            return self.servers[name_lower]
        
        # Try exact name match
        for server in self.servers.values():
            if server.name.lower() == name_lower:
                return server
        
        # Try partial match
        for server in self.servers.values():
            if name_lower in server.name.lower():
                return server
        
        return None
    
    def list_servers(self) -> List[PnPServer]:
        """List all discovered servers."""
        return list(self.servers.values())
    
    def get_online_servers(self) -> List[PnPServer]:
        """Get only online servers."""
        return [s for s in self.servers.values() if s.server_online]
    
    def print_discovery_report(self):
        """Print a formatted discovery report."""
        logger.info("=" * 70)
        logger.info("         PLUG & PLAY DISCOVERY REPORT")
        logger.info("=" * 70)

        if not self.servers:
            logger.info("No servers discovered.")
            logger.info("To add a new server: create SiLA2/YourInstrumentSiLA2Server/, add features/*.sila.xml, start the server")
            return

        for key, server in self.servers.items():
            status = "ONLINE" if server.server_online else "OFFLINE"
            hw_status = "HW OK" if server.hardware_online else "HW N/A"
            logger.info(f"[{server.name}] {status} {hw_status} | key={key} addr={server.address} via={server.discovered_via}")
            if server.features:
                for feature in server.features:
                    logger.info(f"  {feature.display_name}: {len(feature.commands)} commands, {len(feature.properties)} properties")

        logger.info("=" * 70)


#                              MAIN / TEST

async def main():
    """Test the plug & play discovery."""
    logging.basicConfig(level=logging.INFO)

    logger.info("Plug & Play Discovery Test")
    discovery = PnPDiscovery()
    await discovery.discover_all()
    discovery.print_discovery_report()

    logger.info("AVAILABLE COMMANDS")
    for server in discovery.list_servers():
        logger.info(f"[{server.name}]")
        for feature_id, cmd_id, cmd in server.get_all_commands():
            params = ", ".join(p.identifier for p in cmd.parameters)
            logger.info(f"  {cmd_id}({params})")


if __name__ == "__main__":
    asyncio.run(main())
