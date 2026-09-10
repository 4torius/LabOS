"""
PlateReaderImpl
===============
SiLA2 feature implementation for the Tecan Infinite M200 Pro plate reader.
Delegates all hardware calls to TecanBridgeClient, which proxies to the C#
TecanSiLA2Server COM bridge (or simulates in stub mode when that is absent).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from typing import Optional

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from generated.platereaderservice.platereaderservice_base import PlateReaderServiceBase
from generated.platereaderservice.platereaderservice_errors import (
    ConnectionFailed,
    InstrumentBusy,
    MeasurementFailed,
    MovementFailed,
    NotConnected,
    ProtocolNotFound,
    ResultNotFound,
    TemperatureOutOfRange,
)
from generated.platereaderservice.platereaderservice_types import (
    Connect_Responses,
    Disconnect_Responses,
    GetAnIMLResult_Responses,
    ListProtocols_Responses,
    PlateIn_Responses,
    PlateOut_Responses,
    RunMeasurement_IntermediateResponses,
    RunMeasurement_Responses,
    SetTemperature_Responses,
    TurnOffTemperature_Responses,
)
from sila2.server import MetadataDict, ObservableCommandInstanceWithIntermediateResponses

from .tecan_bridge_client import TecanBridgeClient

logger = logging.getLogger(__name__)


class PlateReaderImpl(PlateReaderServiceBase):

    def __init__(self, parent_server, config: dict) -> None:
        super().__init__(parent_server)
        bridge_cfg = config.get("bridge", {})
        self._bridge = TecanBridgeClient(
            host=bridge_cfg.get("host", "127.0.0.1"),
            port=bridge_cfg.get("port", 50055),
        )
        instrument_cfg = config.get("instrument", {})
        # Default: "usb" → C# bridge translates to porttype=USB, type=READER, LastConnection
        self._default_connection_string = instrument_cfg.get("connection_string", "usb")
        self._stop_event = threading.Event()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)

    # ── start / stop ───────────────────────────────────────────────────────

    def start(self) -> None:
        super().start()
        self.update_IsConnected(self._bridge.get_is_connected())
        self.update_OperationalStatus(self._bridge.get_operational_status())
        self.update_CurrentTemperature(self._bridge.get_current_temperature())
        self._poll_thread.start()
        logger.info("PlateReaderImpl started — bridge at %s:%d",
                    self._bridge._host, self._bridge._port)

    def stop(self) -> None:
        self._stop_event.set()
        self._bridge.close()

    # ── observable property polling ────────────────────────────────────────

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(timeout=2.0):
            try:
                connected = self._bridge.get_is_connected()
                self.update_IsConnected(connected)
                if connected:
                    self.update_OperationalStatus(self._bridge.get_operational_status())
                    temp = self._bridge.get_current_temperature()
                    if temp == temp:  # not NaN
                        self.update_CurrentTemperature(temp)
                else:
                    self.update_OperationalStatus("Disconnected")
            except Exception as exc:
                logger.debug("poll error: %s", exc)

    # ── on_subscription hooks (send immediate value to new subscriber) ─────

    def IsConnected_on_subscription(self, *, metadata: MetadataDict):
        try:
            self.update_IsConnected(self._bridge.get_is_connected())
        except Exception:
            pass

    def OperationalStatus_on_subscription(self, *, metadata: MetadataDict):
        try:
            self.update_OperationalStatus(self._bridge.get_operational_status())
        except Exception:
            pass

    def CurrentTemperature_on_subscription(self, *, metadata: MetadataDict):
        try:
            temp = self._bridge.get_current_temperature()
            if temp == temp:
                self.update_CurrentTemperature(temp)
        except Exception:
            pass

    # ── non-observable property ────────────────────────────────────────────

    def get_InstrumentInfo(self, *, metadata: MetadataDict) -> str:
        return self._bridge.get_instrument_info()

    # ── commands ───────────────────────────────────────────────────────────

    def Connect(self, ConnectionString: str, *, metadata: MetadataDict) -> Connect_Responses:
        cs = ConnectionString if ConnectionString else self._default_connection_string
        ok, msg = self._bridge.connect(cs)
        if not ok:
            raise ConnectionFailed(msg)
        return Connect_Responses()

    def Disconnect(self, *, metadata: MetadataDict) -> Disconnect_Responses:
        self._bridge.disconnect()
        return Disconnect_Responses()

    def PlateIn(self, *, metadata: MetadataDict) -> PlateIn_Responses:
        if not self._bridge.get_is_connected():
            raise NotConnected()
        ok, msg = self._bridge.plate_in()
        if not ok:
            raise MovementFailed(msg)
        return PlateIn_Responses()

    def PlateOut(self, *, metadata: MetadataDict) -> PlateOut_Responses:
        if not self._bridge.get_is_connected():
            raise NotConnected()
        ok, msg = self._bridge.plate_out()
        if not ok:
            raise MovementFailed(msg)
        return PlateOut_Responses()

    def SetTemperature(self, TargetTemperature: float, *, metadata: MetadataDict) -> SetTemperature_Responses:
        if not self._bridge.get_is_connected():
            raise NotConnected()
        if not (4.0 <= TargetTemperature <= 45.0):
            raise TemperatureOutOfRange(f"Temperature {TargetTemperature}°C is outside 4–45°C range")
        ok, msg = self._bridge.set_temperature(TargetTemperature)
        if not ok:
            raise TemperatureOutOfRange(msg)
        return SetTemperature_Responses()

    def TurnOffTemperature(self, *, metadata: MetadataDict) -> TurnOffTemperature_Responses:
        if not self._bridge.get_is_connected():
            raise NotConnected()
        self._bridge.turn_off_temperature()
        return TurnOffTemperature_Responses()

    def RunMeasurement(
        self,
        ProtocolFile: str,
        PlateID: str,
        SampleSetID: str,
        PlateType: str,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[RunMeasurement_IntermediateResponses],
    ) -> RunMeasurement_Responses:
        if ProtocolFile and not ProtocolFile.lower().endswith('.mdfx'):
            ProtocolFile = ProtocolFile + '.mdfx'

        # Generate a meaningful PlateID when none is provided, so the AnIML
        # filename never falls back to "Unknown_timestamp.animl".
        if not PlateID:
            from datetime import datetime as _dt
            proto_slug = ProtocolFile.replace('.mdfx', '').replace(' ', '_')[:20]
            PlateID = f"{proto_slug}_{_dt.now().strftime('%Y%m%d_%H%M%S')}"

        if not self._bridge.get_is_connected():
            raise NotConnected()

        op_status = self._bridge.get_operational_status()
        if op_status == "Busy":
            raise InstrumentBusy()

        instance.begin_execution()

        animl_path = ""
        meas_type = "Unknown"

        for chunk in self._bridge.run_measurement(ProtocolFile, PlateID, SampleSetID, PlateType):
            if "error" in chunk:
                err = chunk["error"]
                if "not found" in err.lower() or "protocol" in err.lower():
                    raise ProtocolNotFound(err)
                raise MeasurementFailed(err)

            if chunk.get("intermediate"):
                instance.send_intermediate_response(
                    RunMeasurement_IntermediateResponses(
                        Progress=chunk["progress"],
                        StatusMessage=chunk["status"],
                    )
                )
            else:
                animl_path = chunk.get("animl_file_path", "")
                meas_type = chunk.get("measurement_type", "Unknown")

        return RunMeasurement_Responses(AnIMLFilePath=animl_path, MeasurementType=meas_type)

    def GetAnIMLResult(self, PlateID: str, *, metadata: MetadataDict) -> GetAnIMLResult_Responses:
        found, path_or_err = self._bridge.get_animl_result(PlateID)
        if not found:
            raise ResultNotFound(path_or_err)
        return GetAnIMLResult_Responses(AnIMLFilePath=path_or_err)

    def ListProtocols(self, *, metadata: MetadataDict) -> ListProtocols_Responses:
        protocols = self._bridge.list_protocols()
        return ListProtocols_Responses(ProtocolsJson=json.dumps(protocols))
