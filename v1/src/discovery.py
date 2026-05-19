#!/usr/bin/env python3
"""
Plug & Play Discovery Module
============================

SiLA2-native plug-and-play discovery.  Two-phase strategy:

  Phase 1 — Bootstrap (startup):
    lab_config.yaml provides (host, port, name) seeds for servers that may not
    yet have mDNS announcements at startup.  All capability metadata is fetched
    from the running server via SiLAService — no hardcoded command knowledge.

  Phase 2 — Runtime (continuous):
    mDNS/DNS-SD (_sila2._tcp.local.) discovers every SiLA2-compliant server on
    the network segment automatically.  start_continuous_discovery() keeps the
    registry up-to-date as servers come and go.

  In both phases server self-description via SiLAService is the only source of
  truth for features, commands, and metadata.
"""

from __future__ import annotations  # Defer type hint evaluation to avoid NameError

import asyncio
import logging
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
        "service_type": "_sila._tcp.local.",  # sila2 library standard (was _sila2._tcp.local.)
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
        
        # Locations/stations — exclude compound names like "SourceInstrument" that are free-text
        _loc_kw = ['source', 'destination', 'location', 'station', 'from', 'to']
        _loc_excl = ['instrument', 'name', 'type', 'file', 'class', 'id']
        if (any(x in name_lower for x in _loc_kw)
                and not any(ex in name_lower for ex in _loc_excl)):
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
    """Parse SiLA2 Feature Definition Language (FDL) XML returned by GetFeatureDefinition()."""

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
    SiLA2-native Plug & Play Discovery.

    Phase 1 — bootstrap from lab_config.yaml seeds (startup).
    Phase 2 — mDNS/DNS-SD (_sila2._tcp.local.) continuous runtime discovery.
    Server self-description via SiLAService is the only source of truth.
    """

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
        timeout: float = 2.0
    ) -> Dict[str, PnPServer]:
        """
        Discover all SiLA2 servers.

        Phase 1: bootstrap from lab_config.yaml seeds (handles startup and
                 cross-subnet servers that cannot use mDNS multicast).
        Phase 2: mDNS/DNS-SD — SiLA2-standard runtime discovery.

        For continuous background discovery after startup call
        start_continuous_discovery().

        Returns:
            Dict of {server_key: PnPServer}
        """
        self.servers = {}

        # Phase 1: bootstrap from config seeds
        await self._bootstrap_from_config(timeout)

        # Phase 2: mDNS (primary SiLA2-standard runtime discovery)
        if use_mdns and ZEROCONF_AVAILABLE and self._discovery_config.get("enabled", True):
            await self._discover_via_mdns(timeout)

        # Fetch full metadata from each online server via SiLAService
        await self._query_server_metadata(timeout)

        return self.servers
    
    async def _bootstrap_from_config(self, timeout: float = 2.0):
        """
        Bootstrap the server registry from lab_config.yaml seeds.

        Provides startup seeds for servers that may not yet have mDNS
        announcements (local servers before they register) or that live on
        remote subnets where multicast cannot reach.  Only (host, port, name)
        is taken from config; all capability metadata is fetched from the
        running server via SiLAService in _query_server_metadata().
        """
        config_path = self.base_dir / "lab_config.yaml"
        if not config_path.exists():
            return

        try:
            with open(config_path) as f:
                config = yaml.safe_load(f)

            servers_config = config.get("servers", {})
            loop = asyncio.get_running_loop()

            async def _check_one(srv_key: str, srv_config: dict):
                if not srv_config.get("enabled", True):
                    return

                host = srv_config.get("host", "localhost")
                port = srv_config.get("port", 0)
                name = srv_config.get("name", srv_key)

                if not port:
                    return

                server_key = self._make_server_key(srv_key)

                # Skip if already in registry (e.g. found by mDNS first)
                if server_key in self.servers:
                    return
                if self._find_server_by_address(host, port):
                    return

                is_online = await loop.run_in_executor(
                    None, self._is_grpc_server, host, port, timeout
                )
                server = PnPServer(
                    name=name,
                    host=host,
                    port=port,
                    server_online=is_online,
                    hardware_online=is_online,
                    hardware_status="idle" if is_online else "offline",
                    discovered_via="config",
                )
                self.servers[server_key] = server
                logger.info(f"Bootstrap: {name} ({host}:{port}) — {'ONLINE' if is_online else 'OFFLINE'}")

            await asyncio.gather(*[_check_one(k, v) for k, v in servers_config.items()])

        except Exception as e:
            logger.warning(f"Error bootstrapping from config: {e}")
    
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

    
    
    def _try_sila2_client_query(self, host: str, port: int, server: PnPServer) -> bool:
        """
        Query server metadata via the sila2 standard SilaClient (synchronous).

        Uses SiLAService standard properties (ServerName, ServerType, ImplementedFeatures, etc.)
        Falls back silently if the sila2
        library is not available or the server does not respond.

        Returns True if at least the server name was successfully retrieved.
        """
        try:
            import io
            import xml.etree.ElementTree as ET

            from src.client import get_shared_sila_client  # noqa: PLC0415

            client = get_shared_sila_client(host, port)
            if client is None:
                return False

            # ── Server identity ────────────────────────────────────────────
            try:
                name = client.SiLAService.ServerName.get()
                if name:
                    server.name = name
            except Exception:
                pass
            try:
                srv_type = client.SiLAService.ServerType.get()
                if srv_type:
                    server.server_type = srv_type
            except Exception:
                pass
            try:
                desc = client.SiLAService.ServerDescription.get()
                if desc:
                    server.description = desc
            except Exception:
                pass
            try:
                vendor = client.SiLAService.ServerVendorURL.get()
                if vendor:
                    server.vendor = str(vendor)
            except Exception:
                pass
            try:
                ver = client.SiLAService.ServerVersion.get()
                if ver:
                    server.version = str(ver)
            except Exception:
                pass
            try:
                uuid_val = client.SiLAService.ServerUUID.get()
                if uuid_val:
                    server.uuid = str(uuid_val)
            except Exception:
                pass

            # ── Feature definitions ────────────────────────────────────────
            sila_features = []
            try:
                feature_ids = client.SiLAService.ImplementedFeatures.get()
                for fid in feature_ids:
                    try:
                        result = client.SiLAService.GetFeatureDefinition(fid)
                        xml_str = result.FeatureDefinition
                        root = ET.parse(io.StringIO(xml_str)).getroot()
                        feature = self.xml_parser._parse_feature(root)
                        if feature:
                            sila_features.append(feature)
                    except Exception:
                        pass
            except Exception:
                pass

            if sila_features:
                server.features = sila_features

            server.server_online = True
            server.hardware_online = True
            if server.hardware_status in ("unknown", "offline"):
                server.hardware_status = "idle"

            logger.info(
                "sila2 query OK: %s (%s) — %d feature(s)", server.name, server.server_type, len(sila_features)
            )
            return True

        except Exception as exc:
            logger.debug("sila2 client query failed for %s:%d: %s", host, port, exc)
            return False

    async def _query_server_metadata(self, timeout: float = 2.0):
        """Query each online server for metadata and features via the sila2 SilaClient."""
        if not GRPC_AVAILABLE:
            return

        loop = asyncio.get_running_loop()

        async def _query_one(server: PnPServer):
            if not server.port or not server.server_online:
                return
            try:
                await loop.run_in_executor(
                    None, self._try_sila2_client_query, server.host, server.port, server
                )
            except Exception as e:
                logger.debug(f"Could not query {server.name} metadata: {e}")

        await asyncio.gather(*[_query_one(s) for s in self.servers.values()], return_exceptions=True)
    
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
    
    def _make_server_key(self, folder_name: str) -> str:
        """Make a unique key from folder name."""
        # "TecanSiLA2Server" -> "tecan"
        key = folder_name.lower()
        key = key.replace("sila2server", "").replace("server", "")
        key = key.strip("_- ")
        return key or folder_name.lower()
    
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
            logger.info("Add a server: implement SiLA2 + mDNS registration, or add it to lab_config.yaml bootstrap seeds.")
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
    """Quick discovery smoke-test: bootstrap + mDNS, then print report."""
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
