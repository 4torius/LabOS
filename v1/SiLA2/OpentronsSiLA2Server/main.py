#!/usr/bin/env python3
"""
OpentronsSiLA2Server - Main Entry Point
=======================================

Start the SiLA2 server for Opentrons Flex robot control.

Usage:
    python main.py                  # Start with default config
    python main.py --config other.yaml  # Start with custom config
    python main.py --test           # Run connection test
"""

import argparse
import asyncio
import sys
import os

# Ensure src is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import OpentronsSiLA2Server, ServerConfig


def print_banner():
    """Print startup banner."""
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║           OPENTRONS SiLA2 SERVER                         ║")
    print("║           Python Implementation v1.0.0                   ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()


async def run_server(config_path: str):
    """Run the SiLA2 server."""
    print_banner()
    
    # Load configuration
    print(f"Loading config: {config_path}")
    config = ServerConfig(config_path)
    
    # Validate
    is_valid, error = config.validate()
    if not is_valid:
        print(f"❌ Configuration error: {error}")
        return 1
    
    print(f"  Server:  {config.host}:{config.port}")
    print(f"  Robot:   {config.robot_ip}:{config.robot_port}")
    print(f"  HAL:     {config.hardware_config_folder}")
    print()
    
    # Create and run server
    server = OpentronsSiLA2Server(config)
    
    try:
        await server.start()
        return 0
    except KeyboardInterrupt:
        print("\n⚠️  Shutdown requested...")
        return 0
    except Exception as e:
        print(f"❌ Server error: {e}")
        return 1
    finally:
        await server.stop()


async def run_test():
    """Run connection test."""
    from tests.test_connection import test_robot_connection
    
    print_banner()
    print("Running connection test...\n")
    
    # Load config for parameters
    config = ServerConfig("config.yaml")
    
    success = await test_robot_connection(
        host=config.robot_ip,
        port=config.robot_port,
        local_address=config.robot_local_address or ""
    )
    
    return 0 if success else 1


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="OpentronsSiLA2Server - SiLA2 Server for Opentrons Flex"
    )
    parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="Path to configuration file (default: config.yaml)"
    )
    parser.add_argument(
        "--test", "-t",
        action="store_true",
        help="Run connection test instead of starting server"
    )
    
    args = parser.parse_args()
    
    if args.test:
        return asyncio.run(run_test())
    else:
        return asyncio.run(run_server(args.config))


if __name__ == "__main__":
    sys.exit(main())
