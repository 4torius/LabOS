"""
Configuration Management for OpentronsSiLA2Server
=================================================

Handles loading and validation of server configuration from YAML files.
Supports environment variable overrides and mDNS discovery for robot IP.

Environment Variables:
- OPENTRONS_ROBOT_IP: Override robot IP address
- OPENTRONS_ROBOT_PORT: Override robot HTTP API port  
- OPENTRONS_SERVER_PORT: Override SiLA2 server port
- OPENTRONS_LOG_LEVEL: Override logging level
"""

import os
import logging
import socket
from dataclasses import dataclass, field
from typing import Optional, Tuple
from logging.handlers import RotatingFileHandler
from datetime import datetime

import yaml

# Try to import zeroconf for mDNS discovery
try:
    from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False

logger = logging.getLogger(__name__)


#                           mDNS DISCOVERY

class OpentronsmDNSListener:
    """Listener for Opentrons robot mDNS announcements."""
    
    def __init__(self):
        self.robots: dict = {}
        self._found = False
    
    def add_service(self, zc: 'Zeroconf', type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info:
            addresses = [socket.inet_ntoa(addr) for addr in info.addresses]
            if addresses:
                self.robots[name] = {
                    "ip": addresses[0],
                    "port": info.port,
                    "name": name.split(".")[0] if "." in name else name
                }
                self._found = True
                logger.info(f"mDNS: Found Opentrons robot at {addresses[0]}:{info.port}")
    
    def remove_service(self, zc: 'Zeroconf', type_: str, name: str) -> None:
        self.robots.pop(name, None)
    
    def update_service(self, zc: 'Zeroconf', type_: str, name: str) -> None:
        self.add_service(zc, type_, name)


def discover_opentrons_via_mdns(timeout: float = 5.0) -> Optional[Tuple[str, int]]:
    """
    Discover Opentrons robot via mDNS/Bonjour.
    
    Args:
        timeout: Discovery timeout in seconds
        
    Returns:
        Tuple of (ip, port) if found, None otherwise
    """
    if not ZEROCONF_AVAILABLE:
        logger.debug("zeroconf not available for mDNS discovery")
        return None
    
    try:
        zc = Zeroconf()
        listener = OpentronsmDNSListener()
        
        # Opentrons robots announce as _http._tcp.local.
        # Cast to satisfy type checker - OpentronsmDNSListener implements required methods
        browser = ServiceBrowser(zc, "_http._tcp.local.", listener)  # type: ignore[arg-type]
        
        # Wait for discovery
        import time
        start = time.time()
        while time.time() - start < timeout:
            if listener._found:
                # Look for Opentrons-like names
                for name, info in listener.robots.items():
                    name_lower = name.lower()
                    if "opentrons" in name_lower or "flex" in name_lower or "ot" in name_lower:
                        zc.close()
                        return (info["ip"], info["port"])
                # If no Opentrons-specific name, return first found
                if listener.robots:
                    first = list(listener.robots.values())[0]
                    zc.close()
                    return (first["ip"], first["port"])
            time.sleep(0.1)
        
        zc.close()
    except Exception as e:
        logger.debug(f"mDNS discovery failed: {e}")
    
    return None


@dataclass
class ServerConfig:
    """
    Server configuration loaded from YAML file.
    
    Priority (highest to lowest):
    1. Environment variables (OPENTRONS_ROBOT_IP, etc.)
    2. YAML config file
    3. mDNS discovery (for robot_ip only)
    4. Default values
    """
    
    # Server settings
    host: str = "0.0.0.0"
    port: int = 50052
    name: str = "OpentronsSiLA2Server"
    description: str = "SiLA2 Server for Opentrons Flex"
    vendor: str = "ChemicalLab"
    version: str = "1.0.0"
    
    # Robot settings
    robot_ip: str = "169.254.161.83"
    robot_port: int = 31950
    robot_timeout: float = 30.0
    robot_local_address: Optional[str] = None
    connection_retry_count: int = 3
    connection_retry_delay: float = 2.0
    robot_discovered_via: str = "default"  # "env", "config", "mdns", "default"
    
    # Hardware config
    hardware_config_folder: str = "../../../Library/HardwareConfig"
    hardware_default_config: str = "HardwareConfig.json"
    
    # Directories
    dir_input: str = "./input"
    dir_processed: str = "./processed"
    dir_errors: str = "./errors"
    dir_output: str = "./output"
    dir_images: str = "./images"
    dir_logs: str = "./logs"
    dir_temp: str = "./temp"
    
    # Tip tracking
    tip_tracking_enabled: bool = True
    tip_state_file: str = "./tip_state.json"
    
    # Safety
    emergency_protocol: str = "../000_EMERGENCY_RESET.json"
    auto_home_on_error: bool = True
    auto_drop_tip_on_abort: bool = True
    
    # Logging
    log_level: str = "INFO"
    log_file_enabled: bool = True
    log_console_enabled: bool = True
    log_max_file_size_mb: int = 10
    log_backup_count: int = 5
    
    def __init__(self, config_path: Optional[str] = None, use_mdns: bool = True):
        """
        Load configuration from YAML file with env var overrides and mDNS fallback.
        
        Args:
            config_path: Path to YAML config file
            use_mdns: Whether to try mDNS discovery for robot IP
        """
        if config_path is None:
            # Usa config.yaml nella root di OpentronsSiLA2Server rispetto a questo file
            config_path = os.path.join(os.path.dirname(__file__), "../config.yaml")
        config_path = os.path.abspath(config_path)
        if os.path.exists(config_path):
            self._load_from_yaml(config_path)
        
        # Apply environment variable overrides (highest priority)
        self._apply_env_overrides()
        
        # Try mDNS discovery if robot_ip not set via env and it's the default
        if use_mdns and self.robot_discovered_via == "config":
            mdns_result = discover_opentrons_via_mdns(timeout=3.0)
            if mdns_result:
                self.robot_ip, discovered_port = mdns_result
                if discovered_port:
                    self.robot_port = discovered_port
                self.robot_discovered_via = "mdns"
                logger.info(f"Robot IP discovered via mDNS: {self.robot_ip}:{self.robot_port}")
    
    def _apply_env_overrides(self):
        """Apply environment variable overrides."""
        # Robot IP (highest priority)
        env_robot_ip = os.environ.get("OPENTRONS_ROBOT_IP")
        if env_robot_ip:
            self.robot_ip = env_robot_ip
            self.robot_discovered_via = "env"
            logger.info(f"Robot IP from env: {self.robot_ip}")
        
        # Robot port
        env_robot_port = os.environ.get("OPENTRONS_ROBOT_PORT")
        if env_robot_port:
            try:
                self.robot_port = int(env_robot_port)
            except ValueError:
                logger.warning(f"Invalid OPENTRONS_ROBOT_PORT: {env_robot_port}")
        
        # Server port
        env_server_port = os.environ.get("OPENTRONS_SERVER_PORT")
        if env_server_port:
            try:
                self.port = int(env_server_port)
            except ValueError:
                logger.warning(f"Invalid OPENTRONS_SERVER_PORT: {env_server_port}")
        
        # Log level
        env_log_level = os.environ.get("OPENTRONS_LOG_LEVEL")
        if env_log_level:
            self.log_level = env_log_level.upper()
    
    def _load_from_yaml(self, path: str):
        """Load settings from YAML file."""
        with open(path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
            
        # Server section
        server = cfg.get("server", {})
        self.host = server.get("host", self.host)
        self.port = server.get("port", self.port)
        self.name = server.get("name", self.name)
        self.description = server.get("description", self.description)
        self.vendor = server.get("vendor", self.vendor)
        self.version = server.get("version", self.version)
        
        # Robot section
        robot = cfg.get("robot", {})
        self.robot_ip = robot.get("ip", self.robot_ip)
        self.robot_port = robot.get("port", self.robot_port)
        self.robot_timeout = robot.get("timeout", self.robot_timeout)
        self.robot_local_address = robot.get("local_address", self.robot_local_address)
        self.connection_retry_count = robot.get("connection_retry_count", self.connection_retry_count)
        self.connection_retry_delay = robot.get("connection_retry_delay", self.connection_retry_delay)
        self.robot_discovered_via = "config"  # Mark as loaded from config file
        
        # Hardware section
        hw = cfg.get("hardware", {})
        self.hardware_config_folder = hw.get("config_folder", self.hardware_config_folder)
        self.hardware_default_config = hw.get("default_config", self.hardware_default_config)
        
        # Directories section
        dirs = cfg.get("directories", {})
        self.dir_input = dirs.get("input_queue", self.dir_input)
        self.dir_processed = dirs.get("processed", self.dir_processed)
        self.dir_errors = dirs.get("errors", self.dir_errors)
        self.dir_output = dirs.get("output", self.dir_output)
        self.dir_images = dirs.get("images", self.dir_images)
        self.dir_logs = dirs.get("logs", self.dir_logs)
        
        # Tip tracking section
        tip = cfg.get("tip_tracking", {})
        self.tip_tracking_enabled = tip.get("enabled", self.tip_tracking_enabled)
        self.tip_state_file = tip.get("state_file", self.tip_state_file)
        
        # Safety section
        safety = cfg.get("safety", {})
        self.emergency_protocol = safety.get("emergency_protocol", self.emergency_protocol)
        self.auto_home_on_error = safety.get("auto_home_on_error", self.auto_home_on_error)
        self.auto_drop_tip_on_abort = safety.get("auto_drop_tip_on_abort", self.auto_drop_tip_on_abort)
        
        # Logging section
        log = cfg.get("logging", {})
        self.log_level = log.get("level", self.log_level)
        self.log_file_enabled = log.get("file_enabled", self.log_file_enabled)
        self.log_console_enabled = log.get("console_enabled", self.log_console_enabled)
        self.log_max_file_size_mb = log.get("max_file_size_mb", self.log_max_file_size_mb)
        self.log_backup_count = log.get("backup_count", self.log_backup_count)
    
    def validate(self) -> tuple[bool, str]:
        """Validate configuration. Returns (is_valid, error_message)."""
        if self.port < 1 or self.port > 65535:
            return False, f"Invalid port: {self.port}"
        if self.robot_port < 1 or self.robot_port > 65535:
            return False, f"Invalid robot port: {self.robot_port}"
        if self.robot_timeout <= 0:
            return False, f"Invalid robot timeout: {self.robot_timeout}"
        return True, ""


def setup_logging(config: ServerConfig, name: str = "OpentronsSiLA2Server") -> logging.Logger:
    """
    Setup logging with file and console handlers.
    
    Args:
        config: Server configuration
        name: Logger name
        
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))
    
    # Clear existing handlers
    logger.handlers.clear()
    
    # Formatter
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Console handler
    if config.log_console_enabled:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)
        
    # File handler
    if config.log_file_enabled:
        os.makedirs(config.dir_logs, exist_ok=True)
        log_file = os.path.join(
            config.dir_logs, 
            f"server_{datetime.now().strftime('%Y%m%d')}.log"
        )
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=config.log_max_file_size_mb * 1024 * 1024,
            backupCount=config.log_backup_count,
            encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        
    return logger
