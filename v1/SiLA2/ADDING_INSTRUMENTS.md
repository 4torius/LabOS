# Adding a New Instrument to LabOS

## What you need to provide
1. A folder in `SiLA2/` with your server code
2. A `features/*.sila.xml` file that describes your commands
3. A `config.yaml` with port and metadata
4. An `InstrumentServicer` class with one method per command

That's it. The system handles discovery, UI generation, and command routing automatically.

---

## Quick start (copy the template)

```bash
# 1 — Copy the template
cp -r v1/SiLA2/_NewInstrumentTemplate v1/SiLA2/MyInstrumentSiLA2Server

# 2 — Edit the 4 files below, then:
python v1/SiLA2/MyInstrumentSiLA2Server/main.py
```

The server will appear in the CLI and WebApp within the next discovery cycle.

---

## Step 1 — config.yaml

```yaml
server:
  host: "0.0.0.0"
  port: 50065          # ← Must be unique; use 50061–50099 for new instruments

logging:
  level: INFO

hardware:
  simulation: true      # Set false when real hardware is connected

sila2:
  server_name: "My Instrument"     # Human-readable name shown in UI
  server_uuid: "my-instrument-001" # Must be globally unique (use uuidgen)
  vendor: "BicoccaLab"
  version: "1.0.0"
  description: "One-line description"
```

Also add an entry to `v1/lab_config.yaml` under `servers:`:
```yaml
servers:
  my_instrument:
    name: My Instrument
    enabled: true
    host: localhost
    port: 50065
    directory: SiLA2/MyInstrumentSiLA2Server
    command_windows: [python, main.py]
    command_unix:    [python, main.py]
    startup_timeout: 15
    check_file: main.py
```

---

## Step 2 — features/MyInstrument.sila.xml

This XML is the **single source of truth** for what your instrument can do.
The UI, CLI menus, and mDNS TXT records are all generated from this file.

```xml
<?xml version="1.0" encoding="utf-8"?>
<Feature SiLA2Version="1.1" FeatureVersion="1.0.0" MaturityLevel="Draft"
         Originator="it.chemicallab" Category="instrument"
         xmlns="http://www.sila-standard.org"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://www.sila-standard.org https://gitlab.com/SiLA2/sila_base/raw/master/schema/FeatureDefinition.xsd">

    <Identifier>MyInstrument</Identifier>
    <DisplayName>My Instrument</DisplayName>
    <Description>What this instrument does in one sentence.</Description>

    <!-- Simple command (no parameters) -->
    <Command>
        <Identifier>Initialize</Identifier>
        <DisplayName>Initialize</DisplayName>
        <Description>Connect to hardware and verify it is ready.</Description>
        <Observable>No</Observable>
        <Response>
            <Identifier>Result</Identifier>
            <DisplayName>Result</DisplayName>
            <Description>Status message.</Description>
            <DataType><Basic>String</Basic></DataType>
        </Response>
    </Command>

    <!-- Command with parameters -->
    <Command>
        <Identifier>SetTemperature</Identifier>
        <DisplayName>Set Temperature</DisplayName>
        <Description>Set the target temperature in Celsius.</Description>
        <Observable>No</Observable>
        <Parameter>
            <Identifier>Temperature</Identifier>
            <DisplayName>Temperature (°C)</DisplayName>
            <Description>Target temperature between 4 and 100°C.</Description>
            <DataType><Basic>Real</Basic></DataType>
        </Parameter>
        <Response>
            <Identifier>ActualTemperature</Identifier>
            <DisplayName>Actual Temperature</DisplayName>
            <Description>Confirmed setpoint.</Description>
            <DataType><Basic>Real</Basic></DataType>
        </Response>
    </Command>

    <!-- Command with constrained (dropdown) parameter -->
    <Command>
        <Identifier>SetMode</Identifier>
        <DisplayName>Set Mode</DisplayName>
        <Description>Switch operating mode.</Description>
        <Observable>No</Observable>
        <Parameter>
            <Identifier>Mode</Identifier>
            <DisplayName>Mode</DisplayName>
            <Description>Operating mode.</Description>
            <DataType>
                <Constrained>
                    <DataType><Basic>String</Basic></DataType>
                    <Constraints>
                        <Set>
                            <Value>idle</Value>
                            <Value>measuring</Value>
                            <Value>cleaning</Value>
                        </Set>
                    </Constraints>
                </Constrained>
            </DataType>
        </Parameter>
        <Response>
            <Identifier>Result</Identifier><DisplayName>Result</DisplayName>
            <Description>Confirmation.</Description>
            <DataType><Basic>String</Basic></DataType>
        </Response>
    </Command>

    <!-- Long-running (Observable) command with progress streaming -->
    <Command>
        <Identifier>RunMeasurement</Identifier>
        <DisplayName>Run Measurement</DisplayName>
        <Description>Execute a measurement protocol.</Description>
        <Observable>Yes</Observable>        <!-- ← streams progress -->
        <Parameter>
            <Identifier>ProtocolName</Identifier>
            <DisplayName>Protocol Name</DisplayName>
            <Description>Name of the measurement protocol file.</Description>
            <DataType><Basic>String</Basic></DataType>
        </Parameter>
        <Response>
            <Identifier>ResultFile</Identifier>
            <DisplayName>Result File</DisplayName>
            <Description>Path to the result file.</Description>
            <DataType><Basic>String</Basic></DataType>
        </Response>
    </Command>

    <!-- Observable property -->
    <Property>
        <Identifier>CurrentTemperature</Identifier>
        <DisplayName>Current Temperature</DisplayName>
        <Description>Live temperature reading in °C.</Description>
        <Observable>Yes</Observable>
        <DataType><Basic>Real</Basic></DataType>
    </Property>

</Feature>
```

### Rules
| Rule | Reason |
|------|--------|
| `<Identifier>` must be unique within the feature | Used as the method name in servicer |
| `<Observable>Yes</Observable>` → use async generator in servicer | Enables progress streaming |
| Constrained parameters auto-render as dropdowns in the UI | |
| Rename the file to match `<Identifier>` (optional but conventional) | |

---

## Step 3 — src/servicer.py

Implement one method per command. The method name **must match** the `<Identifier>` in the XML exactly.

```python
class InstrumentServicer:

    def __init__(self, config: dict):
        self.config = config
        self._connected = False
        self._status = "idle"
        # Initialize your hardware here

    # ── Non-observable command ────────────────────────────────────────────────
    async def Initialize(self) -> str:
        """Matches <Identifier>Initialize</Identifier>"""
        # connect to hardware...
        self._connected = True
        self._status = "ready"
        return "Connected"

    async def SetTemperature(self, Temperature: float) -> dict:
        """Params are passed as keyword args, auto-converted from str."""
        # send command to hardware...
        return {"ActualTemperature": str(Temperature)}

    async def SetMode(self, Mode: str) -> str:
        return f"Mode set to {Mode}"

    # ── Observable command (yields progress) ─────────────────────────────────
    async def RunMeasurement(self, ProtocolName: str):
        """Async generator → streams intermediate results."""
        self._status = "measuring"
        for i in range(10):
            await asyncio.sleep(1)
            yield {"Progress": (i + 1) * 10, "status": "running"}
        self._status = "idle"
        yield {"ResultFile": f"Results/{ProtocolName}_result.csv"}

    # ── Property getter ───────────────────────────────────────────────────────
    def get_CurrentTemperature(self) -> float:
        """Matches <Identifier>CurrentTemperature</Identifier> property."""
        return 37.2  # read from hardware

    async def close(self):
        """Called on server shutdown."""
        self._connected = False
```

### Param type conversion
`main.py` converts string parameters automatically using Python type annotations:
- `arg: int` → `int(value)`
- `arg: float` → `float(value)`
- `arg: bool` → `value.lower() in ('true', '1', 'yes')`
- `arg: str` (default) → unchanged

---

## Step 4 — Verify it works

```bash
# Start the server
python v1/SiLA2/MyInstrumentSiLA2Server/main.py

# In another terminal, check discovery
cd v1
python -c "
import asyncio
from src.pnp_discovery import PnPDiscovery
async def main():
    d = PnPDiscovery()
    await d.discover_all()
    for s in d.list_servers():
        print(s.name, s.address, [c.identifier for f in s.features for c in f.commands])
asyncio.run(main())
"
```

Expected output:
```
My Instrument localhost:50065 ['Initialize', 'SetTemperature', 'SetMode', 'RunMeasurement']
```

---

## SiLA2 compliance checklist

- [ ] `<Identifier>` in XML matches method name in `InstrumentServicer`
- [ ] `<Observable>Yes</Observable>` commands use `async def ... yield` (async generator)
- [ ] `config.yaml` has a unique `port` (50061–50099) and `server_uuid`
- [ ] Server is added to `lab_config.yaml` `servers:` section
- [ ] `main.py` unchanged (template handles all SiLA2 wiring)

---

## Architecture reference

```
features/MyInstrument.sila.xml
        │
        ├── GetFeatures() ──────→ pnp_discovery knows commands (via gRPC)
        ├── GetServerInfo() ────→ pnp_discovery knows name/type/description
        ├── ExecuteCommand() ───→ routes to InstrumentServicer.<Identifier>()
        └── mDNS TXT record ───→ auto-discovery by name and feature list

src/servicer.py
        └── InstrumentServicer
              ├── Initialize()       ← command implementation
              ├── SetTemperature()   ← command implementation
              ├── RunMeasurement()   ← observable (yields progress)
              └── get_CurrentTemperature() ← property getter
```

**No changes to any LabOS core file are ever needed.**
Instrument discovery, UI generation, and command routing are fully automatic.
