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

## How a Command Is Executed

One path, for every server.

`src/client.py` uses the `sila2` library's `SilaClient`, which fetches the
server's protobuf descriptor **from the server itself** at connection time. The
orchestrator therefore never imports instrument-specific stubs, never needs
regenerating when a server's FDL changes, and works with any SiLA2-compliant
server — not only ones written for LabOS.

```python
from sila2.client import SilaClient

client = SilaClient("localhost", 50302, insecure=True)
print(client.WorkflowAPI.Status.get())                      # property: read
client.WorkflowAPI.ExecuteRecipe(RecipeName="my_recipe.json")  # command: act
client.close()
```

One `SilaClient` is kept per `host:port` process-wide
(`get_shared_sila_client()`) and invalidated only on a transport-level
`grpc.RpcError`. That is not an optimisation: the library registers proto
descriptors in a process-global pool, so a second client for the same server
re-compiles the same proto and conflicts with the descriptor already registered.

### What used to be here

Two other execution paths existed and are gone:

- **`SiLA2Common.ExecuteCommand`** — a proprietary LabOS extension that no
  third-party server implements.
- **Dynamic stub loading** ("Strategy 1") — statically generated `_pb2` stubs in
  `src/pnp_stubs/`, loaded at runtime for hypothetical non-`sila2` servers. No
  such server was ever deployed. Carrying it meant a genuine transport failure
  looked like "try the other protocol" rather than an error, and the launcher
  regenerated the stubs on every startup.

If a raw-gRPC instrument ever does appear, give it a purpose-built adapter
rather than reviving a generic mechanism for a single case. The far easier
option is to wrap it in a `sila2` server, which is what the template in
`SiLA2/_NewInstrumentTemplate/` is for — see
[ADDING_NEW_INSTRUMENT.md](ADDING_NEW_INSTRUMENT.md).

### Timeouts

`0` and `null` both mean **unlimited**, everywhere. An Opentrons protocol can
legitimately run for days, and a client-side deadline on such a command reports
a false failure while the instrument is still working correctly.
`src/settings.py::normalize_timeout()` is the single implementation of that
rule; `None` is the sentinel every layer below understands.

Observable commands are never given a client-side deadline by default: they
already report real progress from the instrument, and the authoritative bound
is the server's own (`robot.run_timeout` for Opentrons).

---

## Commands and Properties

A **command** does something. A **property** is a value the server publishes.
Get this the right way round: a `GetStatus` *command* makes a client "run an
operation" to find out what is already true, and shows up in the interface as a
button an operator has to press. A `Status` property is a reading — displayed
in a panel, and, when declared `<Observable>Yes</Observable>`, pushed to
subscribers instead of polled.

```xml
<Property>
    <Identifier>Status</Identifier>
    <DisplayName>Status</DisplayName>
    <Description>Current state as JSON.</Description>
    <Observable>Yes</Observable>
    <DataType><Basic>String</Basic></DataType>
</Property>
```

Read one over REST at
`/api/devices/{id}/property/{feature}/{property}`, from the CLI with
`python labos.py read opentrons Status`, or directly with
`client.WorkflowAPI.Status.get()`.

The Opentrons server's rework is the worked example: four `Get*` commands
became properties, three remote file listings were deleted outright (the web
interface reads `Library/` from disk far more cheaply, and can do it while the
robot is off), and the capabilities that were implemented but unreachable —
gripper, heater-shaker, camera — became commands. See
[SERVERS.md](SERVERS.md#opentrons-sila2-server).

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

## Regenerating Bindings

After any FDL change:

```bash
python labos.py codegen OpentronsSiLA2Server
```

That runs `sila2-codegen` into a temp directory and copies the inner package
into `generated/<feature>/`, which is the part you want — the generator also
writes a server skeleton nobody uses. Doing it by hand is where people get it
wrong.

Every command you added is now an abstract method on the generated base class,
and the server refuses to start until your implementation provides it. That is
deliberate: a missing method is caught at startup, not on the first call.

---

## Port Assignments

| Server | Default Port | Protocol | Strategy |
|--------|-------------|----------|----------|
| Opentrons SiLA2 Server | 50302 | `sila2` library | **0** |
| Tecan SiLA2 Server | 50051 | `sila2` library | **0** |
| Manual Station SiLA2 Server | 50500 | `sila2` library | **0** |
| Mobile SiLA2 Server (remote, hosted on its own Linux/Docker host — see [ARCHITECTURE.md](ARCHITECTURE.md#mobile-robot-current-status)) | 50053 | `sila2` library | **0** |
| Tecan C# bridge (internal) | 50055 | custom gRPC | — (not a SiLA2 server) |
| New instruments | 50061–50099 | `sila2` library | **0** |

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
