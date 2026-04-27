"""
SiLA2 mDNS Registry - Server Self-Registration Utility
======================================================

Provides a simple interface for SiLA2 servers to register themselves
for automatic discovery via mDNS/DNS-SD.

Usage:
    from SiLA2.sila2_mdns_registry import SiLA2ServerRegistry
    
    # Create registry for your server
    registry = SiLA2ServerRegistry(
        name="OpentronsFlex",
        port=50052,
        features=["OpentronsFlex", "LiquidHandling"],
        vendor="Laboratory Systems"
    )
    
    # At startup
    await registry.register()
    
    # At shutdown
    await registry.unregister()
"""

import asyncio
import logging
import socket
import uuid as uuid_module
from typing import List, Optional

logger = logging.getLogger(__name__)

# Try to import zeroconf
try:
    from zeroconf import ServiceInfo
    from zeroconf.asyncio import AsyncZeroconf
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False
    logger.warning("zeroconf not available - mDNS registration disabled. Run: pip install zeroconf")


# SiLA2 standard service type
SILA2_SERVICE_TYPE = "_sila2._tcp.local."


def get_local_ip() -> str:
    """Get local IP address for mDNS advertising."""
    try:
        # Connect to external address to determine local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class SiLA2ServerRegistry:
    """
    mDNS registry for a single SiLA2 server.
    
    This class provides simple register/unregister functionality
    for SiLA2 servers to advertise themselves on the network.
    
    Example:
        registry = SiLA2ServerRegistry(
            name="MyServer",
            port=50051,
            features=["Feature1", "Feature2"],
            vendor="MyCompany"
        )
        
        async def main():
            await registry.register()
            # ... server runs ...
            await registry.unregister()
    """
    
    def __init__(
        self,
        name: str,
        port: int,
        features: List[str],
        vendor: str = "BicoccaLab",
        version: str = "1.0.0",
        server_type: str = "Real",
        server_uuid: Optional[str] = None,
        host: Optional[str] = None
    ):
        """
        Initialize mDNS registry for a server.
        
        Args:
            name: Server name (e.g., "OpentronsFlex")
            port: gRPC server port
            features: List of SiLA2 feature IDs
            vendor: Vendor name
            version: Server version
            server_type: "Real", "Simulation", or "Mock"
            server_uuid: Optional UUID (auto-generated if not provided)
            host: Optional host IP (auto-detected if not provided)
        """
        self.name = name
        self.port = port
        self.features = features
        self.vendor = vendor
        self.version = version
        self.server_type = server_type
        self.server_uuid = server_uuid or str(uuid_module.uuid4())
        self.host = host or get_local_ip()
        
        self._zeroconf: Optional[AsyncZeroconf] = None
        self._service_info: Optional[ServiceInfo] = None
        self._registered = False
    
    async def register(self) -> bool:
        """
        Register this server for mDNS discovery.
        
        Returns:
            True if registered successfully, False otherwise
        """
        if not ZEROCONF_AVAILABLE:
            logger.warning(f"[{self.name}] mDNS not available - server will not be discoverable")
            return False
        
        if self._registered:
            logger.warning(f"[{self.name}] Already registered")
            return True
        
        try:
            # Create Zeroconf instance
            self._zeroconf = AsyncZeroconf()
            
            # Create TXT record properties
            properties = {
                'uuid': self.server_uuid,
                'type': self.server_type,
                'version': self.version,
                'features': ','.join(self.features),
                'vendor': self.vendor
            }
            
            # Create ServiceInfo
            self._service_info = ServiceInfo(
                type_=SILA2_SERVICE_TYPE,
                name=f"{self.name}.{SILA2_SERVICE_TYPE}",
                port=self.port,
                properties=properties,
                server=f"{self.name}.local.",
                addresses=[socket.inet_aton(self.host)]
            )
            
            # Register service
            await self._zeroconf.async_register_service(self._service_info)
            self._registered = True
            
            logger.info(f"[{self.name}] Registered on mDNS: {self.host}:{self.port}")
            logger.info(f"[{self.name}] Features: {', '.join(self.features)}")
            return True
            
        except Exception as e:
            logger.error(f"[{self.name}] Failed to register on mDNS: {e}")
            return False
    
    async def unregister(self) -> bool:
        """
        Unregister this server from mDNS.
        
        Returns:
            True if unregistered successfully, False otherwise
        """
        if not self._registered or not self._zeroconf:
            return True
        
        try:
            # Unregister service
            if self._service_info:
                await self._zeroconf.async_unregister_service(self._service_info)
            
            # Close zeroconf
            await self._zeroconf.async_close()
            
            self._registered = False
            self._zeroconf = None
            self._service_info = None
            
            logger.info(f"[{self.name}] Unregistered from mDNS")
            return True
            
        except Exception as e:
            logger.error(f"[{self.name}] Error unregistering from mDNS: {e}")
            return False
    
    @property
    def is_registered(self) -> bool:
        """Check if server is currently registered."""
        return self._registered


# Convenience function for servers that don't need the class
_global_registries: dict = {}


async def register_sila2_server(
    name: str,
    port: int,
    features: List[str],
    vendor: str = "BicoccaLab",
    version: str = "1.0.0",
    server_type: str = "Real"
) -> bool:
    """
    Convenience function to register a SiLA2 server.
    
    Stores the registry instance internally for later unregistration.
    
    Args:
        name: Server name
        port: Server port
        features: List of feature IDs
        vendor: Vendor name
        version: Server version
        server_type: Real/Simulation/Mock
    
    Returns:
        True if registered successfully
    """
    registry = SiLA2ServerRegistry(
        name=name,
        port=port,
        features=features,
        vendor=vendor,
        version=version,
        server_type=server_type
    )
    _global_registries[name] = registry
    return await registry.register()


async def unregister_sila2_server(name: str) -> bool:
    """
    Convenience function to unregister a SiLA2 server.
    
    Args:
        name: Server name (must match what was used in register_sila2_server)
    
    Returns:
        True if unregistered successfully
    """
    if name in _global_registries:
        result = await _global_registries[name].unregister()
        del _global_registries[name]
        return result
    return False
