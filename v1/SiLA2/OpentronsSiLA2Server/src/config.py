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
import urllib.request
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

# The Opentrons HTTP API always listens on this TCP port (fixed on both Flex
# and OT-2 — not user-configurable). mDNS `_http._tcp.local.` announcements
# from the robot advertise its *web* port (80) instead, so a discovered IP is
# only ever paired with THIS port, never the announced one.
OPENTRONS_HTTP_API_PORT = 31950


#                           mDNS DISCOVERY

def robot_http_responds(ip: str, port: int = OPENTRONS_HTTP_API_PORT,
                        timeout: float = 2.0) -> bool:
    """Return True if an Opentrons HTTP API answers /health at ip:port.

    Used both to check whether the statically-configured IP is still valid
    and to verify an mDNS candidate before adopting it, so the server never
    latches onto some unrelated `_http._tcp` device on the LAN.
    """
    if not ip:
        return False
    url = f"http://{ip}:{port}/health"
    try:
        req = urllib.request.Request(url, headers={"Opentrons-Version": "*"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed http scheme)
            if resp.status != 200:
                return False
            body = resp.read(4096).decode("utf-8", "ignore").lower()
            # Opentrons /health always carries these keys; guards against a
            # random 200 from an unrelated service.
            return "api_version" in body or "fw_version" in body
    except Exception:
        return False

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


def discover_opentrons_via_mdns(
    timeout: float = 5.0,
    verify: bool = True,
) -> Optional[Tuple[str, int]]:
    """
    Discover the Opentrons robot on the LAN via mDNS/Bonjour.

    The switch hands the robot its IP by DHCP, so that IP can change between
    power cycles. This lets the server find the robot again wherever it
    landed, without anyone editing config.yaml.

    Args:
        timeout: Discovery timeout in seconds.
        verify: If True, only return a candidate whose Opentrons HTTP API
            actually answers on OPENTRONS_HTTP_API_PORT — this filters out
            any other `_http._tcp` device on the network (printers, NAS, the
            robot's own web port, ...).

    Returns:
        (ip, OPENTRONS_HTTP_API_PORT) if found, None otherwise. The port is
        always the fixed API port, never the port from the mDNS record.
    """
    if not ZEROCONF_AVAILABLE:
        logger.debug("zeroconf not available for mDNS discovery")
        return None

    import time

    def _rank(name: str) -> int:
        n = name.lower()
        if "opentrons" in n or "flex" in n:
            return 2
        if n.startswith("ot2") or "ot-2" in n:
            return 1
        return 0  # unrelated _http._tcp device — only used if it verifies

    zc = None
    try:
        zc = Zeroconf()
        listener = OpentronsmDNSListener()
        # Opentrons robots announce as _http._tcp.local.
        # Cast to satisfy type checker - OpentronsmDNSListener implements required methods
        ServiceBrowser(zc, "_http._tcp.local.", listener)  # type: ignore[arg-type]

        start = time.time()
        while time.time() - start < timeout:
            time.sleep(0.2)
            if not listener.robots:
                continue
            # Best-named candidates first; on a tie, keep discovery order.
            candidates = sorted(
                {info["ip"] for info in listener.robots.values() if info.get("ip")},
                key=lambda ip: -max(
                    _rank(name)
                    for name, info in listener.robots.items()
                    if info.get("ip") == ip
                ),
            )
            for ip in candidates:
                if not verify or robot_http_responds(ip, OPENTRONS_HTTP_API_PORT):
                    logger.info(
                        f"mDNS: Opentrons robot at {ip}:{OPENTRONS_HTTP_API_PORT}"
                        f"{'' if verify else ' (unverified)'}"
                    )
                    return (ip, OPENTRONS_HTTP_API_PORT)
        logger.debug("mDNS discovery finished without a usable Opentrons robot")
    except Exception as e:
        logger.debug(f"mDNS discovery failed: {e}")
    finally:
        if zc is not None:
            zc.close()

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
    port: int = 50302
    name: str = "OpentronsSiLA2Server"
    description: str = "SiLA2 Server for Opentrons Flex"
    vendor: str = "ChemicalLab"
    version: str = "1.0.0"
    
    # Robot settings
    robot_ip: str = "169.254.161.83"
    robot_port: int = 31950
    # Per-HTTP-call timeout to the Opentrons robot API (detects a genuinely
    # unresponsive robot, e.g. powered off/unreachable) — a different concern
    # from run_timeout (protocol-duration limit) and deliberately kept finite,
    # not subject to the "0 = unlimited" convention used for run_timeout.
    robot_timeout: float = 30.0
    robot_local_address: Optional[str] = None
    connection_retry_count: int = 3
    connection_retry_delay: float = 2.0
    robot_discovered_via: str = "default"  # "env", "config", "mdns", "default"
    # Max seconds to wait for a run to reach a terminal state.
    # 0 or null in config.yaml means unlimited (for multi-day protocols).
    run_timeout: Optional[float] = 7200.0
    
    # Hardware config
    hardware_config_folder: str = "../../Library/HardwareConfig"
    hardware_default_config: str = "HardwareConfig.json"
    
    # Directories
    dir_input: str = "./input"
    dir_processed: str = "./processed"
    dir_errors: str = "./errors"
    dir_output: str = "./output"
    dir_images: str = "./images"
    dir_logs: str = "./logs"
    dir_temp: str = "./temp"
    dir_recipes: str = "../../Library/Recipes"
    dir_library: str = "../../Library"
    dir_results: str = "../../Results"

    # ── Camera ──────────────────────────────────────────────────────────────
    # The Flex has an on-board camera. `camera_device` is the V4L2 node the
    # snapshot step reads *on the robot*; `camera_http_probe` first tries the
    # robot's own camera endpoint, which takes a picture without moving
    # anything. Scalars rather than a nested dict because this class declares a
    # custom __init__, so dataclass field(default_factory=...) never runs.
    camera_enabled: bool = True
    camera_device: str = "/dev/video2"
    camera_http_probe: bool = True
    
    # Tip tracking
    tip_tracking_enabled: bool = True
    tip_state_file: str = "./tip_state.json"
    tip_mount_state_file: str = "./tip_mount_state.json"
    
    # Safety
    emergency_protocol: str = "../000_EMERGENCY_RESET.json"
    auto_home_on_error: bool = True
    auto_drop_tip_on_abort: bool = True
    check_tip_before_run: bool = True
    
    # Logging
    log_level: str = "INFO"
    log_file_enabled: bool = True
    log_console_enabled: bool = True
    log_max_file_size_mb: int = 10
    log_backup_count: int = 5

    # Run archive (Fase 6/7): local run log + summary with rotation
    run_archive_enabled: bool = True
    dir_run_archive: str = "./run_archive"
    run_archive_retention_uncompressed_days: float = 3
    run_archive_retention_compressed_days: float = 30


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
        
        # Robot IP resolution (the switch assigns it by DHCP, so it drifts):
        #  - env var  -> absolute, never touched here.
        #  - config / default -> trust it ONLY while the robot still answers
        #    there; otherwise self-heal via mDNS so a changed IP doesn't
        #    require editing config.yaml.
        if use_mdns and self.robot_discovered_via in ("config", "default"):
            if robot_http_responds(self.robot_ip, self.robot_port):
                logger.info(
                    f"Robot reachable at configured {self.robot_ip}:{self.robot_port}"
                )
            else:
                logger.warning(
                    f"Robot not answering at configured {self.robot_ip}:{self.robot_port} "
                    "— searching via mDNS..."
                )
                mdns_result = discover_opentrons_via_mdns(timeout=5.0)
                if mdns_result:
                    self.robot_ip, self.robot_port = mdns_result
                    self.robot_discovered_via = "mdns"
                    logger.info(
                        f"Robot found via mDNS at {self.robot_ip}:{self.robot_port}"
                    )
                else:
                    logger.warning(
                        "mDNS found no Opentrons robot — keeping configured "
                        f"{self.robot_ip}:{self.robot_port} (server will start disconnected)"
                    )
    
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
        self.run_timeout = robot.get("run_timeout", self.run_timeout)
        if self.run_timeout is not None and self.run_timeout <= 0:
            self.run_timeout = None  # 0 = unlimited
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
        self.dir_recipes = dirs.get("recipes", self.dir_recipes)
        self.dir_library = dirs.get("library", self.dir_library)
        self.dir_results = dirs.get("results", self.dir_results)

        # Camera section
        cam = cfg.get("camera", {})
        self.camera_enabled = cam.get("enabled", self.camera_enabled)
        self.camera_device = cam.get("device", self.camera_device)
        self.camera_http_probe = cam.get("http_probe", self.camera_http_probe)
        
        # Tip tracking section
        tip = cfg.get("tip_tracking", {})
        self.tip_tracking_enabled = tip.get("enabled", self.tip_tracking_enabled)
        self.tip_state_file = tip.get("state_file", self.tip_state_file)
        self.tip_mount_state_file = tip.get("mount_state_file", self.tip_mount_state_file)
        
        # Safety section
        safety = cfg.get("safety", {})
        self.emergency_protocol = safety.get("emergency_protocol", self.emergency_protocol)
        self.auto_home_on_error = safety.get("auto_home_on_error", self.auto_home_on_error)
        self.auto_drop_tip_on_abort = safety.get("auto_drop_tip_on_abort", self.auto_drop_tip_on_abort)
        self.check_tip_before_run = safety.get("check_tip_before_run", self.check_tip_before_run)
        
        # Logging section
        log = cfg.get("logging", {})
        self.log_level = log.get("level", self.log_level)
        self.log_file_enabled = log.get("file_enabled", self.log_file_enabled)
        self.log_console_enabled = log.get("console_enabled", self.log_console_enabled)
        self.log_max_file_size_mb = log.get("max_file_size_mb", self.log_max_file_size_mb)
        self.log_backup_count = log.get("backup_count", self.log_backup_count)

        # Run archive section
        archive = cfg.get("run_archive", {})
        self.run_archive_enabled = archive.get("enabled", self.run_archive_enabled)
        self.dir_run_archive = archive.get("dir", self.dir_run_archive)
        self.run_archive_retention_uncompressed_days = archive.get(
            "retention_uncompressed_days", self.run_archive_retention_uncompressed_days
        )
        self.run_archive_retention_compressed_days = archive.get(
            "retention_compressed_days", self.run_archive_retention_compressed_days
        )

    def validate(self) -> tuple[bool, str]:
        """Validate configuration. Returns (is_valid, error_message)."""
        if self.port < 1 or self.port > 65535:
            return False, f"Invalid port: {self.port}"
        if self.robot_port < 1 or self.robot_port > 65535:
            return False, f"Invalid robot port: {self.robot_port}"
        if self.robot_timeout <= 0:
            return False, f"Invalid robot timeout: {self.robot_timeout}"
        # run_timeout is normalized to None ("unlimited") by _load_from_yaml
        # before this runs, so a non-None value here is always meant to be finite.
        if self.run_timeout is not None and self.run_timeout <= 0:
            return False, f"Invalid run timeout: {self.run_timeout}"
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
