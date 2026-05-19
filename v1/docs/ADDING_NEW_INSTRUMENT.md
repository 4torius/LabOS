# LabOS — Adding a New Instrument

This guide shows how to integrate a new scientific instrument into LabOS.

## Quick Summary (3 Steps)

The system is **truly plug & play**:

1. **Copy the template**: `cp -r SiLA2/_NewInstrumentTemplate SiLA2/YourInstrumentSiLA2Server`
2. **Define commands**: Edit `features/YourInstrument.sila.xml`
3. **Implement servicer**: Edit `src/servicer.py`

**Done!** The system auto-discovers your server.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│         web interface / lab_core.py             │  <- User interfaces
├─────────────────────────────────────────────────┤
│              PnP Discovery (src/discovery.py)   │  <- Auto-discovers servers
│         mDNS/DNS-SD + config seed (2-phase)     │      via SiLA2-native strategy
├─────────────────────────────────────────────────┤
│    Execution Client (src/client.py)             │  <- Tries strategies in order
│    Strategy 0: sila2 SilaClient (primary)       │      all sila2-compliant servers
│    Strategy 1: Dynamic stub loading (fallback)  │      custom/non-sila2 servers
├─────────────────────────────────────────────────┤
│           Your SiLA2 Server                     │  <- Your new server
│     (FeatureImplementationBase + .sila.xml)     │      built with sila2 library
├─────────────────────────────────────────────────┤
│              Physical Hardware                  │
└─────────────────────────────────────────────────┘
```

For new servers, the orchestrator automatically selects **Strategy 0** (sila2 SilaClient).

---

## Step-by-Step Guide

### Step 1: Copy the Template

```bash
cd SiLA2
cp -r _NewInstrumentTemplate CentrifugeSiLA2Server  # Example: Centrifuge
```

### Step 2: Configure the Server

Edit `config.yaml`:

```yaml
server:
  port: 50061  # Choose a unique port (50061-50099)

hardware:
  # Your hardware connection settings
  serial_port: "COM3"
  baud_rate: 9600
  # Or for network:
  # host: "192.168.1.100"
  # port: 8080

sila2:
  server_name: "Centrifuge"
  server_uuid: "centrifuge-001"
```

### Step 3: Define Your Commands

Edit `features/YourInstrument.sila.xml`:

```xml
<Feature xmlns="http://www.sila-standard.org"
         FeatureVersion="1.0.0">
    
    <Identifier>Centrifuge</Identifier>
    <DisplayName>Centrifuge</DisplayName>
    
    <!-- Define your commands -->
    <Command>
        <Identifier>Spin</Identifier>
        <DisplayName>Spin</DisplayName>
        <Description>Run centrifuge at specified RPM</Description>
        <Parameter>
            <Identifier>RPM</Identifier>
            <DataType><Basic>Integer</Basic></DataType>
        </Parameter>
        <Parameter>
            <Identifier>Duration</Identifier>
            <DataType><Basic>Integer</Basic></DataType>
        </Parameter>
    </Command>
    
    <Command>
        <Identifier>Stop</Identifier>
        <DisplayName>Emergency Stop</DisplayName>
    </Command>
    
</Feature>
```

### Step 4: Implement the Commands

First generate the base classes from your FDL:
```bash
cd SiLA2/CentrifugeSiLA2Server
sila2-codegen features/Centrifuge.sila.xml --output-dir generated/
```

Then edit `src/centrifuge_impl.py`:

```python
import serial
from generated.centrifuge.centrifuge_base import CentrifugeBase

class CentrifugeImpl(CentrifugeBase):
    def __init__(self, config):
        super().__init__()
        self.serial = serial.Serial(
            config['hardware']['serial_port'],
            config['hardware']['baud_rate']
        )

    def Spin(self, *, RPM: int, Duration: int):
        self.serial.write(f"SPIN {RPM} {Duration}\n".encode())
        self.serial.readline()
        return self.SpinResponses(status="running")

    def Stop(self):
        self.serial.write(b"STOP\n")
        return self.StopResponses(stopped=True)
```

And update `main.py`:
```python
from sila2.server import SilaServer
from src.centrifuge_impl import CentrifugeImpl

config = yaml.safe_load(open("config.yaml"))
server = SilaServer(
    name=config["sila2"]["server_name"],
    features=[CentrifugeImpl(config)],
    port=config["server"]["port"],
    insecure=True,
)
server.run()
```

### Step 5: Test Your Server

```bash
cd SiLA2/CentrifugeSiLA2Server
python main.py
```

In another terminal, start the orchestrator and check the dashboard at http://localhost:8000 — your server should appear as online in the instruments list.

Or verify directly via the API:
```bash
curl http://localhost:8000/api/instruments | python -m json.tool
# Your new server should appear in the response
```

---

## What Gets Auto-Discovered

The PnP system automatically detects:

| What | Source |
|------|--------|
| Server name | `config.yaml` → sila2.server_name |
| Port | `config.yaml` → server.port |
| Commands | `features/*.sila.xml` |
| Parameters | From XML Parameter elements |

---

## Communication Protocols

Your servicer can use any protocol to talk to hardware:

| Protocol | Use Case | Python Library |
|----------|----------|----------------|
| Serial/RS232 | Lab instruments | `pyserial` |
| HTTP REST | Modern devices | `httpx` |
| TCP Socket | PLCs, controllers | `socket` |
| USB | Sensors, scales | `pyusb` |

---

## Tips

1. **Start simple**: Get basic commands working first
2. **Use simulation mode**: Test without hardware using `config.yaml` → `hardware.simulation: true`
3. **Add logging**: Use `logger.info()` for debugging
4. **Handle errors**: Return meaningful error messages

---

## Files Modified

When you add a new instrument, you **only modify files in your server folder**:

```
SiLA2/YourInstrumentSiLA2Server/
├── config.yaml          ← EDIT: Port and settings
├── features/*.sila.xml  ← EDIT: Command definitions
└── src/servicer.py      ← EDIT: Implementation
```

**No other files in the project need to be changed!**
