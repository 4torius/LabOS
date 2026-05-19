#!/usr/bin/env python3
"""Test that available gRPC stubs load correctly from pnp_stubs package."""
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

STUBS = [
    ('SiLA2Common_pb2', 'SiLA2Common_pb2_grpc'),
    ('TecanLegacyBridge_pb2', 'TecanLegacyBridge_pb2_grpc'),
]

all_ok = True
for pb2_name, grpc_name in STUBS:
    print(f"\n{'='*60}")
    print(f"TEST: {pb2_name}")
    print('='*60)
    try:
        import importlib
        pb2 = importlib.import_module(f'src.pnp_stubs.{pb2_name}')
        pb2_grpc = importlib.import_module(f'src.pnp_stubs.{grpc_name}')
        stubs = [n for n in dir(pb2_grpc) if n.endswith('Stub') and not n.startswith('_')]
        print(f"OK  pb2={pb2.__name__}, stubs={stubs}")
    except Exception as e:
        print(f"FAIL: {e}")
        all_ok = False

print(f"\n{'='*60}")
print(f"Result: {'ALL OK' if all_ok else 'FAILURES DETECTED'}")
