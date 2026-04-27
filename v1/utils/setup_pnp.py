#!/usr/bin/env python3
"""
Plug & Play System Setup
========================

This script:
1. Compiles SiLA2Common.proto to Python stubs
2. Verifies the installation
3. Tests discovery
"""

import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.absolute()
SILA2_DIR = BASE_DIR / "SiLA2"
PROTO_FILE = SILA2_DIR / "SiLA2Common.proto"
OUTPUT_DIR = BASE_DIR / "src" / "grpc"


def ok(msg): print(f"[OK] {msg}")
def err(msg): print(f"[ERR] {msg}")
def info(msg): print(f"[i] {msg}")


def compile_proto():
    """Compile SiLA2Common.proto to Python."""
    info(f"Compiling {PROTO_FILE}...")
    
    if not PROTO_FILE.exists():
        err(f"Proto file not found: {PROTO_FILE}")
        return False
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # Try to compile with grpc_tools
    try:
        from grpc_tools import protoc
        
        result = protoc.main([
            'grpc_tools.protoc',
            f'-I{SILA2_DIR}',
            f'--python_out={OUTPUT_DIR}',
            f'--grpc_python_out={OUTPUT_DIR}',
            str(PROTO_FILE)
        ])
        
        if result == 0:
            ok("Proto compilation successful")
            return True
        else:
            err(f"Proto compilation failed with code {result}")
            return False
            
    except ImportError:
        info("grpc_tools not installed, trying system protoc...")
        
        try:
            subprocess.run([
                'python', '-m', 'grpc_tools.protoc',
                f'-I{SILA2_DIR}',
                f'--python_out={OUTPUT_DIR}',
                f'--grpc_python_out={OUTPUT_DIR}',
                str(PROTO_FILE)
            ], check=True)
            ok("Proto compilation successful")
            return True
        except Exception as e:
            err(f"Proto compilation failed: {e}")
            info("Install grpc_tools: pip install grpcio-tools")
            return False


def verify_imports():
    """Verify all PnP modules can be imported."""
    info("Verifying imports...")
    
    modules = [
        ("discovery", "src.discovery"),
        ("client", "src.client"),
        ("workflow", "src.workflow"),
    ]
    
    sys.path.insert(0, str(BASE_DIR))
    
    all_ok = True
    for name, module_path in modules:
        try:
            __import__(module_path)
            ok(f"  {name}")
        except Exception as e:
            err(f"  {name}: {e}")
            all_ok = False
    
    return all_ok


async def test_discovery():
    """Test server discovery."""
    info("Testing discovery...")
    
    from src.discovery import PnPDiscovery
    
    discovery = PnPDiscovery(BASE_DIR)
    servers_dict = await discovery.discover_all()
    
    ok(f"Found {len(servers_dict)} servers:")
    for key, server in servers_dict.items():
        cmd_count = len(server.get_all_commands())
        online = "🟢" if server.server_online else "⚪"
        print(f"    {online} {server.name} ({cmd_count} commands)")
    
    return True


def main():
    """Run setup."""
    print()
    print("=" * 60)
    print("  PLUG & PLAY SYSTEM SETUP")
    print("=" * 60)
    print()
    
    # Step 1: Compile proto
    if not compile_proto():
        info("Proto compilation optional - system can work without it")
    
    print()
    
    # Step 2: Verify imports
    if not verify_imports():
        err("Some imports failed!")
        return 1
    
    print()
    
    # Step 3: Test discovery
    import asyncio
    asyncio.run(test_discovery())
    
    print()
    print("=" * 60)
    print("  SETUP COMPLETE")
    print("=" * 60)
    print()
    print("Next steps:")
    print("  1. Start your SiLA2 servers")
    print("  2. Run: python launcher.py --all")
    print("  3. Use API docs at: http://localhost:5000/docs")
    print()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
