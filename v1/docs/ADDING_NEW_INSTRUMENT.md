# LabOS — Adding a New Instrument

This guide shows how to integrate a new scientific instrument into LabOS.

## Quick Summary (5 steps)

1. **Copy the template** and rename the folder
2. **Edit the FDL XML** — define commands and parameters
3. **Re-run codegen** — regenerate Python bindings from your FDL
4. **Implement the commands** — fill in hardware logic
5. **Declare library files** — edit `library_catalog.json`

Register the server in `lab_config.yaml` and it is discovered automatically.  
**No other LabOS files need to be modified.**

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│         web interface / lab_core.py             │  ← user-facing
├─────────────────────────────────────────────────┤
│     PnP Discovery (src/discovery.py)            │  ← auto-discovers servers
│     mDNS/DNS-SD + config seed (2-phase)         │     via SiLA2 native strategy
├─────────────────────────────────────────────────┤
│     Execution Client (src/client.py)            │
│     Strategy 0: sila2 SilaClient (primary)      │  ← all sila2-compliant servers
├─────────────────────────────────────────────────┤
│     Your SiLA2 Server                           │
│     FeatureImpl(FeatureBase)  +  SilaServer     │  ← what you implement
│     LabOSLibraryProviderFeature (auto-wired)    │  ← declares library types
├─────────────────────────────────────────────────┤
│     Physical Hardware                           │
└─────────────────────────────────────────────────┘
```

---

## Step-by-Step Guide

### Step 1 — Copy the Template

```bash
cd v1/SiLA2
cp -r _NewInstrumentTemplate CentrifugeSiLA2Server   # example: centrifuge
```

### Step 2 — Edit `config.yaml`

```yaml
server:
  host: "0.0.0.0"
  port: 50061          # unique port, 50061–50099

hardware:
  serial_port: "COM3"  # or host/port for network devices
  simulation: false

sila2:
  server_name: "Centrifuge"          # CamelCase, shown in UI
  server_type: "Centrifuge"          # must match regex [A-Z][a-zA-Z0-9]*
  vendor_url: "https://bicocca.lab"
  description: "Centrifuge controller"
```

### Step 3 — Define Your Feature (FDL XML)

Rename and edit `features/NewInstrument.sila.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Feature xmlns="http://www.sila-standard.org"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://www.sila-standard.org https://gitlab.com/SiLA2/sila_base/-/raw/master/schema/FeatureDefinition.xsd"
         SiLA2Version="1.1" FeatureVersion="1.0" MaturityLevel="Draft"
         Originator="de.bicocca.labos" Category="instruments">

  <Identifier>Centrifuge</Identifier>
  <DisplayName>Centrifuge</DisplayName>
  <Description>Controls a benchtop centrifuge.</Description>

  <Command>
    <Identifier>Spin</Identifier>
    <DisplayName>Spin</DisplayName>
    <Observable>Yes</Observable>
    <Parameter>
      <Identifier>RPM</Identifier>
      <DataType><Basic>Integer</Basic></DataType>
    </Parameter>
    <Parameter>
      <Identifier>DurationSeconds</Identifier>
      <DataType><Basic>Integer</Basic></DataType>
    </Parameter>
    <Response>
      <Identifier>FinalRPM</Identifier>
      <DataType><Basic>Integer</Basic></DataType>
    </Response>
    <IntermediateResponse>
      <Identifier>CurrentRPM</Identifier>
      <DataType><Basic>Integer</Basic></DataType>
    </IntermediateResponse>
  </Command>

</Feature>
```

**FDL schema rules (common mistakes):**
- `FeatureVersion` must be `"1.0"` format, NOT `"1.0.0"`
- For Observable commands: `<Response>` must come **before** `<IntermediateResponse>`
- `<Parameter>` elements come before `<Response>` and `<IntermediateResponse>`

### Step 4 — Run `sila2-codegen`

Generate Python bindings from your FDL. Run from your server's directory:

```bash
cd v1/SiLA2/CentrifugeSiLA2Server

# Generate into a temp directory
sila2-codegen new-package \
    --package-name centrifuge \
    --output-directory /tmp/sila2_gen \
    --no-generate-main \
    features/Centrifuge.sila.xml

# Move the inner generated package into place
mkdir -p generated/centrifuge
cp /tmp/sila2_gen/centrifuge/generated/centrifuge/* generated/centrifuge/
```

> On Windows use `%TEMP%\sila2_gen` instead of `/tmp/sila2_gen`.
> Run this step every time you edit the FDL XML.

The generated `generated/centrifuge/centrifuge_base.py` contains the abstract methods you must implement.

### Step 5 — Implement the Commands

Rename `src/servicer.py` → `src/centrifuge_impl.py` and implement every abstract method from the generated base:

```python
import time
from sila2.server import MetadataDict, ObservableCommandInstanceWithIntermediateResponses
from generated.centrifuge.centrifuge_base import CentrifugeBase
from generated.centrifuge.centrifuge_types import Spin_Responses, Spin_IntermediateResponses

class CentrifugeImpl(CentrifugeBase):
    def __init__(self, parent_server, config: dict):
        super().__init__(parent_server=parent_server)
        self._simulation = config.get("hardware", {}).get("simulation", True)
        self._status = "idle"

    def Spin(
        self,
        RPM: int,
        DurationSeconds: int,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[Spin_IntermediateResponses],
    ) -> Spin_Responses:
        self._status = "spinning"
        for i in range(DurationSeconds):
            time.sleep(1)
            current = int(RPM * (i + 1) / DurationSeconds)
            instance.send_intermediate_response(Spin_IntermediateResponses(CurrentRPM=current))
        self._status = "idle"
        return Spin_Responses(FinalRPM=RPM)
```

### Step 6 — Update `main.py` Imports

In `main.py`, update the feature imports to match your renamed classes:

```python
from generated.centrifuge.centrifuge_feature import CentrifugeFeature
from src.centrifuge_impl import CentrifugeImpl
```

And update the registration:

```python
impl = CentrifugeImpl(sila_server, config)
sila_server.set_feature_implementation(CentrifugeFeature, impl)
# LabOSLibraryProvider is already wired in — no changes needed
sila_server.set_feature_implementation(
    LabOSLibraryProviderFeature, LibraryProviderImpl(sila_server)
)
```

### Step 7 — Declare Library Files (`library_catalog.json`)

If your instrument uses files from `Library/` (e.g. method files, protocols), declare them in `library_catalog.json`. The orchestrator reads this at discovery time and automatically populates the workflow builder dropdowns — **no changes to `lab_config.yaml` or `discovery.py` needed**.

```json
{
  "library_types": [
    {
      "hint":            "centrifuge_method",
      "display_name":    "Centrifuge Method",
      "folder":          "Library/CentrifugeMethods",
      "file_extension":  ".json",
      "parameter_names": ["MethodName", "MethodFile"]
    }
  ]
}
```

- `hint` — internal key used by the UI (must be unique across all servers)
- `parameter_names` — SiLA2 parameter identifiers that should show this file picker
- `folder` / `file_extension` — where the files are located relative to `v1/`

Create `Library/CentrifugeMethods/` and add your method files.

### Step 8 — Register the Server

Add your server to `v1/lab_config.yaml` under `servers`:

```yaml
servers:
  centrifuge:
    name: Centrifuge
    enabled: true
    host: localhost
    port: 50061
    directory: SiLA2/CentrifugeSiLA2Server
    command_windows: [python, main.py]
    command_unix:    [python, main.py]
    startup_timeout: 30
    check_file: main.py
```

### Step 9 — Test

```bash
# Start your server
cd v1/SiLA2/CentrifugeSiLA2Server
python main.py

# In another terminal, start LabOS
cd v1
python launcher.py
```

Open http://localhost:5000 — your server should appear under **Instruments**.
In the **Workflow Builder**, your commands should be listed with correct dropdowns.

---

## What the Orchestrator Discovers Automatically

| What | How |
|------|-----|
| Server name, type | `config.yaml` → `sila2.server_name/server_type` |
| Port | `lab_config.yaml` → `servers.your_key.port` |
| Commands and parameters | From FDL XML via `SiLAService.GetFeatureDefinition()` |
| Library file dropdowns | From `LabOSLibraryProvider.LibraryCatalog` property (reads `library_catalog.json`) |

---

## Files in Your Server Folder

After completing the guide:

```
SiLA2/CentrifugeSiLA2Server/
├── config.yaml                      ← port, hardware settings, server metadata
├── features/
│   └── Centrifuge.sila.xml          ← FDL: commands, parameters, responses
├── generated/
│   ├── centrifuge/                  ← codegen output (commit this)
│   │   ├── centrifuge_base.py       ← abstract methods to implement
│   │   ├── centrifuge_feature.py    ← Feature object for set_feature_implementation
│   │   ├── centrifuge_types.py      ← Response NamedTuples
│   │   └── ...
│   └── laboslibraryprovider/        ← shared, already in template (do not modify)
├── library_catalog.json             ← declares library file types
├── main.py                          ← server entry point (update imports only)
└── src/
    ├── centrifuge_impl.py           ← YOUR IMPLEMENTATION (renamed from servicer.py)
    └── library_provider_impl.py     ← shared, already in template (do not modify)
```

**Files to never modify:** `generated/laboslibraryprovider/`, `src/library_provider_impl.py`  
**Files to run codegen on change:** `features/Centrifuge.sila.xml`

---

## Communication Protocols

Your implementation can use any Python library for hardware communication:

| Protocol | Use Case | Library |
|----------|----------|---------|
| Serial/RS232 | Lab instruments, PLCs | `pyserial` |
| HTTP REST | Modern devices, web APIs | `httpx` |
| TCP socket | Industrial controllers | `socket` |
| USB/HID | Sensors, balances | `pyusb`, `hid` |
| MODBUS | Industrial automation | `pymodbus` |

---

## Tips

- **Simulation first**: set `hardware.simulation: true` in `config.yaml` to develop and test without hardware
- **Observable vs Non-Observable**: use Observable for long-running operations (measurements, runs); Non-Observable for instant reads/sets
- **Error handling**: raise `DefinedExecutionError` subclasses from your FDL for expected failure modes; uncaught exceptions become `UndefinedExecutionError`
- **Codegen is idempotent**: safe to re-run; always re-run after editing the FDL
