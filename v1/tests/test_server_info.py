"""
Integration tests for SiLA2Common service (GetServerInfo, GetFeatures, GetStatus).
All tests require a running server and are skipped if unavailable.
"""
import asyncio
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(V1_DIR))

try:
    import grpc
    from SiLA2 import SiLA2Common_pb2, SiLA2Common_pb2_grpc  # type: ignore
    STUBS_OK = True
except ImportError:
    STUBS_OK = False


def _make_stub(host: str, port: int):
    channel = grpc.insecure_channel(f"{host}:{port}")
    return SiLA2Common_pb2_grpc.SiLA2ServerInfoStub(channel), channel


# Opentrons

@pytest.mark.integration
@pytest.mark.opentrons
@pytest.mark.asyncio
async def test_opentrons_get_server_info(opentrons_available):
    if not opentrons_available or not STUBS_OK:
        pytest.skip("Opentrons not available or stubs missing")
    stub, ch = _make_stub("localhost", 50057)
    resp = stub.GetServerInfo(SiLA2Common_pb2.GetServerInfoRequest(), timeout=5)
    assert resp.server_name, "server_name should not be empty"
    assert resp.server_type, "server_type should not be empty"
    ch.close()


@pytest.mark.integration
@pytest.mark.opentrons
@pytest.mark.asyncio
async def test_opentrons_get_features(opentrons_available):
    if not opentrons_available or not STUBS_OK:
        pytest.skip("Opentrons not available or stubs missing")
    stub, ch = _make_stub("localhost", 50057)
    resp = stub.GetFeatures(SiLA2Common_pb2.GetFeaturesRequest(), timeout=5)
    assert len(resp.features) > 0, "Server must advertise at least one feature"
    for feat in resp.features:
        assert feat.identifier, "Each feature must have an identifier"
        assert len(feat.commands) > 0, f"Feature {feat.identifier} has no commands"
    ch.close()


@pytest.mark.integration
@pytest.mark.opentrons
@pytest.mark.asyncio
async def test_opentrons_get_status(opentrons_available):
    if not opentrons_available or not STUBS_OK:
        pytest.skip("Opentrons not available or stubs missing")
    stub, ch = _make_stub("localhost", 50057)
    resp = stub.GetStatus(SiLA2Common_pb2.GetStatusRequest(), timeout=5)
    assert resp.status in ("online", "offline", "busy", "error", "initializing", "running")
    assert resp.server_online is True
    ch.close()


# Manual Station

@pytest.mark.integration
@pytest.mark.manual_station
@pytest.mark.asyncio
async def test_manual_station_get_features(manual_station_available):
    if not manual_station_available or not STUBS_OK:
        pytest.skip("ManualStation not available or stubs missing")
    stub, ch = _make_stub("localhost", 50360)
    resp = stub.GetFeatures(SiLA2Common_pb2.GetFeaturesRequest(), timeout=5)
    assert len(resp.features) > 0
    ch.close()


# Tecan

@pytest.mark.integration
@pytest.mark.tecan
@pytest.mark.asyncio
async def test_tecan_get_server_info(tecan_available):
    if not tecan_available or not STUBS_OK:
        pytest.skip("Tecan not available or stubs missing")
    stub, ch = _make_stub("localhost", 50051)
    resp = stub.GetServerInfo(SiLA2Common_pb2.GetServerInfoRequest(), timeout=5)
    assert resp.server_name
    assert resp.server_type == "plate_reader"
    ch.close()


@pytest.mark.integration
@pytest.mark.tecan
@pytest.mark.asyncio
async def test_tecan_get_features_has_plate_reader(tecan_available):
    if not tecan_available or not STUBS_OK:
        pytest.skip("Tecan not available or stubs missing")
    stub, ch = _make_stub("localhost", 50051)
    resp = stub.GetFeatures(SiLA2Common_pb2.GetFeaturesRequest(), timeout=5)
    identifiers = [f.identifier for f in resp.features]
    assert "PlateReaderService" in identifiers, f"PlateReaderService not found, got: {identifiers}"
    ch.close()


@pytest.mark.integration
@pytest.mark.tecan
@pytest.mark.asyncio
async def test_tecan_get_status_fields(tecan_available):
    if not tecan_available or not STUBS_OK:
        pytest.skip("Tecan not available or stubs missing")
    stub, ch = _make_stub("localhost", 50051)
    resp = stub.GetStatus(SiLA2Common_pb2.GetStatusRequest(), timeout=5)
    # After proto fix: server_online is a bool, status is a string
    assert isinstance(resp.server_online, bool)
    assert isinstance(resp.status, str)
    assert resp.server_online is True
    ch.close()


# Cross-server: all online servers must comply with SiLA2Common contract

@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_servers_sila2common_compliant(base_dir, any_server_available):
    if not any_server_available or not STUBS_OK:
        pytest.skip("No servers available or stubs missing")

    from src.discovery import PnPDiscovery
    d = PnPDiscovery(base_dir)
    await d.discover_all(timeout=2.0)
    online = [s for s in d.list_servers() if s.server_online]

    failures = []
    for server in online:
        stub, ch = _make_stub(server.host, server.port)
        try:
            info = stub.GetServerInfo(SiLA2Common_pb2.GetServerInfoRequest(), timeout=3)
            if not info.server_name:
                failures.append(f"{server.name}: empty server_name")
            feats = stub.GetFeatures(SiLA2Common_pb2.GetFeaturesRequest(), timeout=3)
            if len(feats.features) == 0:
                failures.append(f"{server.name}: no features")
        except Exception as e:
            failures.append(f"{server.name}: {e}")
        finally:
            ch.close()

    assert not failures, "SiLA2Common compliance failures:\n" + "\n".join(failures)
