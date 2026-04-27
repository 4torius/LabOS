"""
Manual Station SiLA2 Server - Source Package
=============================================
"""

# Proto imports will be available after stub generation
try:
    from . import ManualStationService_pb2
    from . import ManualStationService_pb2_grpc
except ImportError:
    pass

from .manual_station_servicer import ManualStationServicer

__all__ = [
    "ManualStationServicer",
]
