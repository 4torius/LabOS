# LabOS — SiLA2 Integration

## What Is SiLA2?

SiLA2 (Standardization in Lab Automation 2) is an open standard for laboratory instrument communication. It defines:
- **gRPC** as the transport layer
- **Protocol Buffers** for message serialization
- **Feature Definition Language (FDL)** — XML files that describe instrument capabilities

SiLA2 is maintained by the [SiLA2 Consortium](https://sila-standard.org) and is adopted by major instrument vendors (Sartorius, Hamilton, Tecan, Roche).

---

## SiLA2 Stack in LabOS

```
FDL XML file              (human-readable capability description)
    │
    ▼  sila2-codegen
Protocol Buffer (.proto)  (message and service definitions)
    │
    ▼  protoc + grpc plugin
Python stubs              (generated client/server binding code)
    │
    ▼  implemented in
SiLA2 Server              (instrument wrapper process)
    │
    ▼  gRPC over TCP (default port range 50051–50099)
SiLA2 Client              (orchestrator or test script)
```

---

## FDL File Structure

Each SiLA2 server declares its capabilities in a `.sila.xml` file stored in `SiLA2/<ServerName>/features/`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<Feature SiLA2Version="1.1" FeatureVersion="1.0.0"
         MaturityLevel="Draft"
         Originator="it.bicoccalab"
         Category="automation"
         xmlns="http://www.sila-standard.org">

  <Identifier>WorkflowAPI</Identifier>
  <DisplayName>Workflow API</DisplayName>
  <Description>Liquid handling workflow execution on Opentrons Flex OT-3.</Description>

  <!-- Commands -->
  <Command>
    <Identifier>ExecuteRecipe</Identifier>
    <DisplayName>Execute Recipe</DisplayName>
    <Description>Execute a liquid handling recipe with a HAL configuration.</Description>
    <Parameters>
      <Parameter>
        <Identifier>recipe</Identifier>
        <DisplayName>Recipe File</DisplayName>
        <Description>Filename in Library/Recipes/</Description>
        <DataType><Basic>String</Basic></DataType>
      </Parameter>
      <Parameter>
        <Identifier>hal_config</Identifier>
        <DisplayName>HAL Config File</DisplayName>
        <Description>Filename in Library/HardwareConfig/</Description>
        <DataType><Basic>String</Basic></DataType>
      </Parameter>
    </Parameters>
    <Responses>
      <Response>
        <Identifier>status</Identifier>
        <DataType><Basic>String</Basic></DataType>
      </Response>
    </Responses>
  </Command>

  <!-- Properties -->
  <Property Identifier="RobotStatus" Observable="Yes">
    <DisplayName>Robot Status</DisplayName>
    <DataType>
      <Constrained>
        <DataType><Basic>String</Basic></DataType>
        <Constraints>
          <Set>
            <Value>idle</Value>
            <Value>running</Value>
            <Value>error</Value>
          </Set>
        </Constraints>
      </Constrained>
    </DataType>
  </Property>

</Feature>
```

---

## Execution Strategies

The orchestrator (`src/client.py`) selects the communication strategy automatically, trying them in order:

### Strategy 0 — `sila2` Library `SilaClient` (Preferred)

All current LabOS servers are built with the **`sila2` Python library** (v0.14). The library generates and registers protobuf descriptors automatically via `sila2-codegen`, and publishes them over the standard `SiLAService` feature. The orchestrator's `SilaClient` fetches these descriptors at runtime and invokes commands without any compile-time stub coupling:

```python
from sila2.client import SilaClient

client = SilaClient("localhost", 50052, insecure=True)
# Feature and command names come from the live descriptor:
result = client.WorkflowAPI.ExecuteRecipe(
    recipe="my_recipe.json",
    hal_config="deck_config_A.json"
)
```

**Why this is preferred over SiLA2Common**: it uses the standard SiLA2 protocol (no custom extension), descriptors are typed and versioned, and any SiLA2-compliant client (not just LabOS) can interact with the server.

### Strategy 1 — `SiLA2Common` (Legacy Fallback)

For older custom servers that pre-date the `sila2` library migration, a custom `SiLA2Common` gRPC service is available as a fallback. It accepts string-keyed commands with JSON parameters:

```protobuf
service SiLA2CommonService {
  rpc ExecuteCommand (CommandRequest) returns (stream CommandResponse);
  rpc GetServerInfo  (Empty)          returns (ServerInfo);
  rpc GetFeatures    (Empty)          returns (FeatureList);
  rpc GetProperty    (PropertyRequest) returns (PropertyResponse);
}
```

The stubs are kept in `src/pnp_stubs/SiLA2Common_pb2*.py`. New servers should **not** implement this service.

### Strategy 2 — Dynamic Stub Loading (Last Resort)

Statically generated `_pb2` / `_pb2_grpc` files placed in `src/pnp_stubs/` are loaded at runtime for any server that supports neither Strategy 0 nor Strategy 1.

---

## Implementing a SiLA2 Server

Every server in `v1/SiLA2/` follows the same pattern using the **`sila2` library**:

### File Structure
```
SiLA2/YourInstrumentSiLA2Server/
├── config.yaml              # Port, hardware connection, server name
├── main.py                  # Entry point — SilaServer + feature registration
├── features/
│   └── YourInstrument.sila.xml  # FDL capability description
├── generated/
│   └── yourinstrument/      # sila2-codegen output (do not edit)
└── src/
    ├── __init__.py
    └── your_instrument_impl.py  # FeatureImplementationBase subclass
```

### Implementation Template

```python
# src/your_instrument_impl.py
from generated.yourinstrument.yourinstrument_base import YourInstrumentBase

class YourInstrumentImpl(YourInstrumentBase):

    def __init__(self, config):
        super().__init__()
        self.config = config
        # Initialize hardware connection here

    def YourCommand(self, *, Param1: str, Param2: int):
        # Instrument interaction
        return self.YourCommandResponses(Result="done")
```

```python
# main.py
from sila2.server import SilaServer
from src.your_instrument_impl import YourInstrumentImpl

config = yaml.safe_load(open("config.yaml"))
feature = YourInstrumentImpl(config)
server = SilaServer(
    name=config["sila2"]["server_name"],
    features=[feature],
    port=config["server"]["port"],
    insecure=True
)
server.run()
```

Regenerate `generated/` after editing the FDL:

```bash
sila2-codegen features/YourInstrument.sila.xml --output-dir generated/
```

---

## Regenerating Protobuf Stubs

After editing a `.sila.xml` FDL file, regenerate the stubs:

```bash
cd SiLA2/YourInstrumentSiLA2Server
sila2-codegen features/YourInstrument.sila.xml --output-dir generated/
```

This regenerates the `generated/yourinstrument/` directory. Do not edit generated files manually.

For the legacy `SiLA2Common` proto (Strategy 1 fallback only):

```bash
cd v1
python regen_stubs.py
```

This regenerates `src/pnp_stubs/SiLA2Common_pb2*.py`.

---

## Port Assignments

| Server | Default Port | Protocol | Strategy |
|--------|-------------|----------|----------|
| Opentrons SiLA2 Server | 50052 | `sila2` library | **0** |
| Tecan SiLA2 Server | 50051 | `sila2` library | **0** |
| Mobile SiLA2 Server | 50053 | `sila2` library | **0** |
| Manual Station SiLA2 Server | 50500 | `sila2` library | **0** |
| Tecan C# bridge (internal) | 50055 | custom gRPC | — (not a SiLA2 server) |
| New instruments | 50056–50099 | `sila2` library | **0** |

Ports are configured in each server's `config.yaml`.

---

## Discovery Over mDNS

To enable automatic mDNS discovery, a server can broadcast a `_sila._tcp.local.` service record:

```python
from zeroconf import ServiceInfo, Zeroconf
import socket

info = ServiceInfo(
    "_sila._tcp.local.",
    "opentrons._sila._tcp.local.",
    addresses=[socket.inet_aton("127.0.0.1")],
    port=50052,
    properties={"server-name": "opentrons", "server-type": "WorkflowAPI"}
)
zeroconf = Zeroconf()
zeroconf.register_service(info)
```

The discovery engine in `v1/src/discovery.py` listens for these records and registers matching servers automatically.
