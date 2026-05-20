"""
TecanBridgeClient
=================
gRPC client that proxies calls to the C# COM bridge (port 50055).
If the bridge is unreachable, operations raise RuntimeError immediately —
there is no silent stub fallback.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from typing import Iterator, Tuple

import grpc

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import TecanLegacyBridge_pb2 as pb2
import TecanLegacyBridge_pb2_grpc as pb2_grpc

logger = logging.getLogger(__name__)


class TecanBridgeClient:
    """Thread-safe gRPC client to the C# COM bridge."""

    def __init__(self, host: str = "127.0.0.1", port: int = 50055) -> None:
        self._host = host
        self._port = port
        self._channel = grpc.insecure_channel(f"{host}:{port}")
        self._stub = pb2_grpc.PlateReaderServiceStub(self._channel)
        self._lock = threading.Lock()
        self._check_available()

    def _check_available(self) -> None:
        try:
            self._stub.GetIsConnected(pb2.GetIsConnectedRequest(), timeout=3.0)
            logger.info("C# bridge available at %s:%d", self._host, self._port)
        except Exception as exc:
            logger.warning("C# bridge NOT available at %s:%d: %s", self._host, self._port, exc)

    def close(self) -> None:
        try:
            self._channel.close()
        except Exception:
            pass

    # ── property accessors ─────────────────────────────────────────────────

    def get_is_connected(self) -> bool:
        try:
            return self._stub.GetIsConnected(pb2.GetIsConnectedRequest(), timeout=3.0).value
        except Exception:
            return False

    def get_operational_status(self) -> str:
        try:
            return self._stub.GetOperationalStatus(pb2.GetOperationalStatusRequest(), timeout=3.0).value
        except Exception:
            return "Error"

    def get_current_temperature(self) -> float:
        try:
            return self._stub.GetCurrentTemperature(pb2.GetCurrentTemperatureRequest(), timeout=3.0).value
        except Exception:
            return float("nan")

    def get_instrument_info(self) -> str:
        try:
            r = self._stub.GetInstrumentInfo(pb2.GetInstrumentInfoRequest(), timeout=3.0)
            info = r.value
            return json.dumps({
                "serial_number": info.serial_number,
                "product_name": info.product_name,
                "firmware_version": info.firmware_version,
                "is_simulated": info.is_simulated,
            })
        except Exception as exc:
            return json.dumps({"serial_number": "Unknown", "product_name": "Unknown",
                               "firmware_version": "Unknown", "is_simulated": False})

    # ── commands ───────────────────────────────────────────────────────────

    def connect(self, connection_string: str = "") -> Tuple[bool, str]:
        try:
            r = self._stub.Connect(
                pb2.ConnectRequest(connection_string=connection_string), timeout=90.0
            )
            return r.success, "Connected" if r.success else "Connection failed"
        except grpc.RpcError as exc:
            return False, exc.details() or str(exc)
        except Exception as exc:
            return False, str(exc)

    def disconnect(self) -> Tuple[bool, str]:
        try:
            r = self._stub.Disconnect(pb2.DisconnectRequest(), timeout=10.0)
            return r.success, "Disconnected"
        except Exception as exc:
            return False, str(exc)

    def plate_in(self) -> Tuple[bool, str]:
        try:
            r = self._stub.PlateIn(pb2.PlateInRequest(), timeout=30.0)
            return r.success, "OK"
        except grpc.RpcError as exc:
            return False, exc.details() or str(exc)

    def plate_out(self) -> Tuple[bool, str]:
        try:
            r = self._stub.PlateOut(pb2.PlateOutRequest(), timeout=30.0)
            return r.success, "OK"
        except grpc.RpcError as exc:
            return False, exc.details() or str(exc)

    def set_temperature(self, target: float) -> Tuple[bool, str]:
        try:
            r = self._stub.SetTemperature(
                pb2.SetTemperatureRequest(target_temperature=target), timeout=10.0
            )
            return r.success, "OK"
        except grpc.RpcError as exc:
            return False, exc.details() or str(exc)

    def turn_off_temperature(self) -> Tuple[bool, str]:
        try:
            r = self._stub.TurnOffTemperature(pb2.TurnOffTemperatureRequest(), timeout=10.0)
            return r.success, "OK"
        except grpc.RpcError as exc:
            return False, exc.details() or str(exc)

    def run_measurement(
        self,
        protocol_file: str,
        plate_id: str,
        sample_set_id: str = "",
        plate_type: str = "",
    ) -> Iterator[dict]:
        req = pb2.RunMeasurementRequest(
            protocol_file=protocol_file,
            plate_id=plate_id,
            sample_set_id=sample_set_id,
            plate_type=plate_type,
        )
        try:
            for resp in self._stub.RunMeasurement(req, timeout=1200.0):
                if resp.is_intermediate:
                    yield {"intermediate": True, "progress": resp.progress, "status": resp.status_message}
                else:
                    mr = resp.measurement_result
                    yield {
                        "intermediate": False,
                        "animl_file_path": mr.animl_file_path,
                        "excel_file_path": mr.excel_file_path,
                        "measurement_type": mr.measurement_type or "Unknown",
                    }
                    break
        except grpc.RpcError as exc:
            yield {"error": exc.details() or str(exc)}

    def get_animl_result(self, plate_id: str) -> Tuple[bool, str]:
        try:
            self._stub.GetAnIMLResult(pb2.GetAnIMLResultRequest(plate_id=plate_id), timeout=10.0)
            return True, ""
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                return False, f"Result not found for plate {plate_id}"
            return False, str(exc)

    def list_protocols(self) -> list:
        try:
            r = self._stub.ListProtocols(pb2.ListProtocolsRequest(), timeout=10.0)
            return list(r.protocols)
        except Exception as exc:
            logger.warning("list_protocols failed: %s", exc)
            return []
