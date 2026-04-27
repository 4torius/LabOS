#!/usr/bin/env python3
"""
Instrument Servicer Template
============================

This is where you implement your instrument's commands.
Each method corresponds to a command defined in features/NewInstrument.sila.xml

INSTRUCTIONS:
1. Implement each command method
2. Add your hardware communication code
3. The PnP system handles everything else!
"""

import asyncio
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class InstrumentServicer:
    """
    SiLA2 Servicer for New Instrument.
    
    Implements the commands defined in features/NewInstrument.sila.xml
    """
    
    def __init__(self, config: dict):
        """
        Initialize the servicer.
        
        Args:
            config: Configuration from config.yaml
        """
        self.config = config
        self.hardware_config = config.get('hardware', {})
        self.simulation = self.hardware_config.get('simulation', True)
        
        # Instrument state
        self._connected = False
        self._status = "idle"
        
        # TODO: Initialize your hardware connection here
        # Example:
        # self.serial_port = self.hardware_config.get('serial_port')
        # self.baud_rate = self.hardware_config.get('baud_rate', 9600)
        
        logger.info(f"InstrumentServicer initialized (simulation={self.simulation})")
    
    #                           COMMAND IMPLEMENTATIONS
    
    async def Initialize(self) -> bool:
        """
        Initialize the instrument and establish connection.
        
        Returns:
            True if initialization was successful
        """
        logger.info("Initialize called")
        
        if self.simulation:
            # Simulate initialization
            await asyncio.sleep(0.5)
            self._connected = True
            self._status = "ready"
            logger.info("Simulated initialization complete")
            return True
        
        # TODO: Implement real hardware initialization
        # Example:
        # try:
        #     self.connection = serial.Serial(self.serial_port, self.baud_rate)
        #     response = self.connection.readline()
        #     self._connected = True
        #     return True
        # except Exception as e:
        #     logger.error(f"Initialization failed: {e}")
        #     return False
        
        return True
    
    async def SetParameter(self, parameter_name: str, value: str) -> str:
        """
        Set an instrument parameter.
        
        Args:
            parameter_name: Name of the parameter to set
            value: New value for the parameter
            
        Returns:
            The previous value before change
        """
        logger.info(f"SetParameter: {parameter_name} = {value}")
        
        # TODO: Implement parameter setting
        # Example:
        # previous = self._parameters.get(parameter_name, "")
        # self._parameters[parameter_name] = value
        # self._send_command(f"SET {parameter_name} {value}")
        # return previous
        
        return "(previous value)"
    
    async def RunOperation(self, duration: int):
        """
        Execute a long-running operation with progress updates.
        
        This is an Observable command - it yields progress updates.
        
        Args:
            duration: How long to run the operation (seconds)
            
        Yields:
            Progress percentage (0-100)
            
        Returns:
            Operation result
        """
        logger.info(f"RunOperation started: duration={duration}s")
        
        self._status = "running"
        
        for i in range(duration):
            await asyncio.sleep(1)
            progress = int((i + 1) / duration * 100)
            logger.debug(f"Progress: {progress}%")
            yield {"Progress": progress}
        
        self._status = "idle"
        
        # Final response
        yield {"Result": f"Operation completed after {duration} seconds"}
    
    async def GetStatus(self) -> str:
        """
        Get current instrument status.
        
        Returns:
            Current status (idle, running, error, etc.)
        """
        return self._status
    
    async def Stop(self) -> bool:
        """
        Emergency stop - halt all operations.
        
        Returns:
            Whether stop was successful
        """
        logger.warning("STOP command received - halting operations")
        
        # TODO: Implement emergency stop
        # Example:
        # self._send_command("STOP")
        # self._abort_current_operation()
        
        self._status = "stopped"
        return True
    
    #                           PROPERTY IMPLEMENTATIONS
    
    def get_InstrumentName(self) -> str:
        """Get the instrument name/model."""
        return self.config.get('sila2', {}).get('server_name', 'New Instrument')
    
    def get_IsConnected(self) -> bool:
        """Get connection status."""
        return self._connected
    
    #                           HELPER METHODS
    
    def _send_command(self, command: str) -> str:
        """
        Send a command to the hardware.
        
        TODO: Implement for your specific hardware protocol
        """
        if self.simulation:
            logger.debug(f"[SIM] Command: {command}")
            return "OK"
        
        # Example serial implementation:
        # self.connection.write(f"{command}\n".encode())
        # return self.connection.readline().decode().strip()
        
        raise NotImplementedError("Hardware communication not implemented")
    
    async def close(self):
        """Clean up resources."""
        logger.info("Closing servicer")
        
        # TODO: Close hardware connections
        # Example:
        # if self.connection:
        #     self.connection.close()
        
        self._connected = False
