# gRPC generated stubs for BicoccaLab PnP
"""
This package contains generated gRPC stubs for SiLA2 servers.

The generated stubs use absolute imports (e.g., `import SiLA2Common_pb2`),
so we add this directory to sys.path to make them work when imported
from other packages.
"""
import os
import sys

# Add this directory to sys.path so that generated stubs can import each other
_stub_dir = os.path.dirname(os.path.abspath(__file__))
if _stub_dir not in sys.path:
    sys.path.insert(0, _stub_dir)

# Now import the stubs
from . import SiLA2Common_pb2
from . import SiLA2Common_pb2_grpc
from . import OpentronsService_pb2
from . import OpentronsService_pb2_grpc
from . import ManualStationService_pb2
from . import ManualStationService_pb2_grpc
from . import PlateReaderService_pb2
from . import PlateReaderService_pb2_grpc

__all__ = [
    'SiLA2Common_pb2',
    'SiLA2Common_pb2_grpc',
    'OpentronsService_pb2',
    'OpentronsService_pb2_grpc',
    'ManualStationService_pb2',
    'ManualStationService_pb2_grpc',
    'PlateReaderService_pb2',
    'PlateReaderService_pb2_grpc',
]
