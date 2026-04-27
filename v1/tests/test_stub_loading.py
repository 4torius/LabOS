#!/usr/bin/env python3
"""Test stub loading."""
import sys
import logging
from pathlib import Path

# Setup
sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

print("=" * 60)
print("TEST 1: Direct import from pnp_stubs")
print("=" * 60)
try:
    import importlib
    pb2 = importlib.import_module('pnp_stubs.OpentronsService_pb2')
    pb2_grpc = importlib.import_module('pnp_stubs.OpentronsService_pb2_grpc')
    print(f"SUCCESS: pb2={pb2}")
    print(f"SUCCESS: pb2_grpc={pb2_grpc}")
    
    # Check for Stub class
    for name in dir(pb2_grpc):
        if 'Stub' in name:
            print(f"  Found stub class: {name}")
except Exception as e:
    print(f"FAILED: {e}")

print()
print("=" * 60)
print("TEST 2: PnPClient._load_stubs_for_server()")
print("=" * 60)
try:
    import asyncio
    from src.client import PnPClient
    from src.discovery import PnPServer, PnPFeature
    
    server = PnPServer(name='Opentrons Flex', host='localhost', port=50051)
    server.features = [PnPFeature('OpentronsService', 'Test', [])]
    
    client = PnPClient()
    print(f"sys.path includes src: {'src' in sys.path}")
    print(f"Trying to load stubs for: {server.name}")
    
    asyncio.run(client._load_stubs_for_server(server))
    
    print(f"Stubs loaded: {list(client._stubs.keys())}")
    if client._stubs:
        print("SUCCESS: Stubs are loaded!")
    else:
        print("FAILED: No stubs loaded")
except Exception as e:
    import traceback
    print(f"FAILED: {e}")
    traceback.print_exc()

print()
print("=" * 60)
print("TEST 3: Check sys.path at runtime")
print("=" * 60)
print("sys.path entries containing 'v7':")
for p in sys.path:
    if 'v7' in p.lower() or 'bicoccalab' in p.lower():
        print(f"  {p}")
