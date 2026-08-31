#!/usr/bin/env python3
"""
New Instrument Implementation Template
=======================================

TODO: Rename this file to newinstrument_impl.py (match your feature name).

This class implements every abstract method defined in
generated/newinstrument/newinstrument_base.py (auto-generated from your FDL XML).

Re-open that file whenever you add commands to the FDL — each new command
becomes a new abstract method you must implement here.

Usage pattern:
    impl = NewInstrumentImpl(sila_server, config)
    sila_server.set_feature_implementation(NewInstrumentFeature, impl)
"""

import logging
import os
import sys
import time
from typing import Optional

# Ensure the server root is on sys.path so "generated.*" can be imported
_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from sila2.server import MetadataDict, ObservableCommandInstanceWithIntermediateResponses

from generated.newinstrument.newinstrument_base import NewInstrumentBase
from generated.newinstrument.newinstrument_types import (
    GetStatus_Responses,
    Initialize_Responses,
    RunOperation_IntermediateResponses,
    RunOperation_Responses,
    SetParameter_Responses,
    Stop_Responses,
)

logger = logging.getLogger(__name__)


class NewInstrumentImpl(NewInstrumentBase):
    """
    Concrete implementation of the NewInstrument SiLA2 feature.

    TODO:
    1. Rename this class to match your instrument (e.g. CentrifugeImpl)
    2. Add hardware-specific attributes in __init__
    3. Fill in each command method with your hardware logic
    """

    def __init__(self, parent_server, config: dict):
        super().__init__(parent_server=parent_server)
        self.config = config
        hw = config.get("hardware", {})
        self._simulation = hw.get("simulation", True)

        # TODO: add your hardware connection attributes here
        # e.g. self._serial = None  or  self._client = httpx.AsyncClient(...)

        self._status = "idle"
        # Set initial value for the observable IsConnected property
        self.update_IsConnected(False)
        logger.info("NewInstrumentImpl initialised (simulation=%s)", self._simulation)

    # ── Non-observable property ────────────────────────────────────────────────

    def get_InstrumentName(self, *, metadata: MetadataDict) -> str:
        return self.config.get("sila2", {}).get("server_name", "New Instrument")

    # ── Commands ───────────────────────────────────────────────────────────────

    def Initialize(self, *, metadata: MetadataDict) -> Initialize_Responses:
        """Connect to hardware and prepare for operation."""
        logger.info("Initialize called")
        if self._simulation:
            time.sleep(0.2)
            self._status = "idle"
            self.update_IsConnected(True)
            return Initialize_Responses(Success=True)

        # TODO: open serial / TCP / USB connection here
        # try:
        #     self._device = MyDevice(self.config["hardware"]["port"])
        #     self.update_IsConnected(True)
        #     return Initialize_Responses(Success=True)
        # except Exception as e:
        #     logger.error("Initialization failed: %s", e)
        #     return Initialize_Responses(Success=False)
        return Initialize_Responses(Success=True)

    def SetParameter(
        self, ParameterName: str, Value: str, *, metadata: MetadataDict
    ) -> SetParameter_Responses:
        """Set a named instrument parameter."""
        logger.info("SetParameter: %s = %s", ParameterName, Value)
        # TODO: send parameter to hardware
        previous = "(unknown)"
        return SetParameter_Responses(PreviousValue=previous)

    def GetStatus(self, *, metadata: MetadataDict) -> GetStatus_Responses:
        """Return current instrument status."""
        return GetStatus_Responses(Status=self._status)

    def Stop(self, *, metadata: MetadataDict) -> Stop_Responses:
        """Emergency stop — halt all operations immediately."""
        logger.warning("STOP command received")
        self._status = "stopped"
        # TODO: send stop command to hardware
        return Stop_Responses(Stopped=True)

    def RunOperation(
        self,
        Duration: int,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[RunOperation_IntermediateResponses],
    ) -> RunOperation_Responses:
        """
        Run a long operation, streaming progress to the client.

        Use instance.send_intermediate_response() to push progress updates.
        This method runs synchronously in a thread executor — use time.sleep,
        not asyncio.sleep.
        """
        logger.info("RunOperation: duration=%ds", Duration)
        self._status = "running"

        for i in range(Duration):
            time.sleep(1)
            pct = int((i + 1) / Duration * 100)
            instance.send_intermediate_response(RunOperation_IntermediateResponses(Progress=pct))

        self._status = "idle"
        return RunOperation_Responses(Result=f"Completed after {Duration}s")

    # ── Cleanup ────────────────────────────────────────────────────────────────

    def stop(self):
        """Called by main() on shutdown. Close hardware connections here."""
        logger.info("NewInstrumentImpl stopping")
        self.update_IsConnected(False)
        # TODO: close self._device or similar
