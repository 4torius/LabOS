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

## SiLA2Common — The Generic Execution Protocol

LabOS extends SiLA2 with a custom `SiLA2Common` service that every server implements alongside its native feature. This is the interface the orchestrator uses exclusively.

### Proto Definition (`SiLA2Common.proto`)

```protobuf
syntax = "proto3";
package sila2common;

service SiLA2CommonService {
  rpc GetServerInfo  (Empty)         returns (ServerInfo);
  rpc GetFeatures    (Empty)         returns (FeatureList);
  rpc ExecuteCommand (CommandRequest) returns (CommandResponse);
  rpc GetProperty    (PropertyRequest) returns (PropertyResponse);
}

message ServerInfo {
  string server_name    = 1;
  string server_type    = 2;
  string server_version = 3;
  string status         = 4;
}

message CommandRequest {
  string command_id  = 1;
  string params_json = 2;   // JSON-encoded key-value map
}

message CommandResponse {
  bool   success      = 1;
  string result_json  = 2;
  string error_detail = 3;
}
```

### Why SiLA2Common?

Without it, the orchestrator would need to import each instrument's generated Python stub at compile time — tightly coupling the orchestration layer to every instrument integration. SiLA2Common breaks this coupling: the orchestrator dispatches commands as JSON strings over a single generic interface, and instrument-specific logic lives entirely in the server process.

**Adding a new instrument** means deploying a new SiLA2Common-compliant server. The orchestrator discovers and commands it with zero code changes.

---

## Implementing a SiLA2 Server

Every server in `v1/SiLA2/` follows the same pattern:

### File Structure
```
SiLA2/YourInstrumentSiLA2Server/
├── config.yaml              # Port, hardware connection, server name
├── main.py                  # Entry point — starts gRPC server
├── features/
│   └── YourInstrument.sila.xml  # FDL capability description
└── src/
    ├── __init__.py
    ├── servicer.py          # Implements native feature + SiLA2Common
    ├── YourInstrument_pb2.py      # Generated (do not edit)
    └── YourInstrument_pb2_grpc.py # Generated (do not edit)
```

### Servicer Template

```python
# src/servicer.py
import json
from SiLA2Common_pb2 import ServerInfo, CommandResponse
from SiLA2Common_pb2_grpc import SiLA2CommonServiceServicer

class YourInstrumentServicer(SiLA2CommonServiceServicer):

    def GetServerInfo(self, request, context):
        return ServerInfo(
            server_name="your_instrument",
            server_type="YourInstrument",
            server_version="1.0.0",
            status="idle"
        )

    def GetFeatures(self, request, context):
        # Return FDL metadata parsed from .sila.xml
        ...

    def ExecuteCommand(self, request, context):
        params = json.loads(request.params_json)
        try:
            result = self._dispatch(request.command_id, params)
            return CommandResponse(success=True, result_json=json.dumps(result))
        except Exception as e:
            return CommandResponse(success=False, error_detail=str(e))

    def _dispatch(self, command_id, params):
        if command_id == "YourCommand":
            return self._your_command(params)
        raise ValueError(f"Unknown command: {command_id}")

    def _your_command(self, params):
        # Instrument interaction here
        return {"status": "done"}
```

---

## Regenerating Protobuf Stubs

If you modify a `.proto` file:

```bash
python -m grpc_tools.protoc \
  -I. \
  --python_out=. \
  --grpc_python_out=. \
  SiLA2Common.proto
```

This regenerates `SiLA2Common_pb2.py` and `SiLA2Common_pb2_grpc.py`. Do not edit generated files manually.

---

## Port Assignments

| Server | Default Port |
|--------|-------------|
| Opentrons SiLA2 Server | 50052 |
| Tecan SiLA2 Server | 50051 |
| Mobile SiLA2 Server | 50053 |
| Manual Station SiLA2 Server | 50054 |
| New instruments | 50055–50099 |

Ports are configured in each server's `config.yaml`.

---

## Discovery Over mDNS

To enable automatic mDNS discovery, a server can broadcast a `_sila2._tcp.local.` service record:

```python
from zeroconf import ServiceInfo, Zeroconf
import socket

info = ServiceInfo(
    "_sila2._tcp.local.",
    "opentrons._sila2._tcp.local.",
    addresses=[socket.inet_aton("127.0.0.1")],
    port=50052,
    properties={"server-name": "opentrons", "server-type": "WorkflowAPI"}
)
zeroconf = Zeroconf()
zeroconf.register_service(info)
```

The discovery engine in `v1/src/discovery.py` listens for these records and registers matching servers automatically.
