"""
OpentronsSiLA2Server - SiLA2 Server for Opentrons Flex Robot
============================================================

A robust SiLA2 server implementation for controlling Opentrons Flex robots.

Modules:
    - server: Main SiLA2 server with gRPC interface
    - robot_client: HTTP client for Opentrons API
    - protocol_generator: JSON recipe to Python protocol converter
    - hardware_manager: Hardware Abstraction Layer (HAL)
    - tip_tracker: Tip consumption tracking with persistence
    - config: Configuration management

Usage:
    from src import OpentronsSiLA2Server, ServerConfig
    
    config = ServerConfig("config.yaml")
    server = OpentronsSiLA2Server(config)
    await server.start()
"""

__version__ = "1.0.0"
__author__ = "ChemicalLab"

from .config import ServerConfig
from .robot_client import RobotClient
from .protocol_generator import ProtocolGenerator
from .hardware_manager import HardwareManager
from .tip_tracker import TipTracker, calculate_tips_from_recipe
from .server import OpentronsSiLA2Server

__all__ = [
    "ServerConfig",
    "RobotClient", 
    "ProtocolGenerator",
    "HardwareManager",
    "TipTracker",
    "calculate_tips_from_recipe",
    "OpentronsSiLA2Server",
]
