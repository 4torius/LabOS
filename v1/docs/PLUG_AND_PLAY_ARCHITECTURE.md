# Plug & Play Architecture Guide

## Overview

The Plug & Play (PnP) architecture transforms BicoccaLab from a system with hardcoded instrument support to one where **servers are the ONLY source of truth**.

### Core Principles

1. **NO HARDCODING** - No lists of known instruments, no instrument-specific handlers
2. **Self-Description** - Servers describe themselves via metadata and feature files
3. **Generic Execution** - One client can talk to any server
4. **Automatic Discovery** - New instruments appear automatically when started

## Architecture Components

```
┌──────────────────────────────────────────────────────────────────────┐
│                           USER INTERFACES                             │
├─────────────────────────┬────────────────────────────────────────────┤
│   pnp_console.py        │       WebApp (future)                      │
│   (Dynamic CLI)         │       (Same discovery API)                 │
└─────────────────────────┴────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          PnP Registry                                 │
│   src/pnp_client.py - PnPRegistry, PnPClient                         │
│   - execute(instrument, command, params)                              │
│   - Finds server by name                                              │
│   - Executes command generically                                      │
└──────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          PnP Discovery                                │
│   src/pnp_discovery.py - PnPDiscovery, PnPServer                     │
│   - Scans ports 50051-50100                                          │
│   - Parses *.sila.xml files                                          │
│   - Queries servers for metadata                                      │
└──────────────────────────────────────────────────────────────────────┘
                                      │
                   ┌──────────────────┼──────────────────┐
                   │                  │                  │
                   ▼                  ▼                  ▼
          ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
          │  Opentrons   │   │    Tecan     │   │    Mobile    │
          │   Server     │   │   Server     │   │    Robot     │
          │  :50051      │   │   :50052     │   │   :50053     │
          └──────────────┘   └──────────────┘   └──────────────┘
```

## Files Created

| File | Purpose |
|------|---------|
| `SiLA2/SiLA2Common.proto` | Standard metadata interface for all servers |
| `src/pnp_discovery.py` | Generic server discovery (no hardcoded lists) |
| `src/pnp_client.py` | Generic command execution |
| `pnp_console.py` | Dynamic CLI (menus generated from servers) |
| `src/pnp_workflow_executor.py` | Workflow execution via PnP |
| `SiLA2/sila2_common_servicer.py` | Helper to add SiLA2Common to any server |

## How It Works

### 1. Discovery

```python
from pathlib import Path
from src.pnp_discovery import PnPDiscovery

# Use the directory containing the script as base, or specify any path
base_dir = Path(__file__).parent  # Or: Path.cwd()

discovery = PnPDiscovery(base_dir=base_dir)
servers = await discovery.discover_all()

for server in servers:
    print(f"{server.name}: {len(server.get_all_commands())} commands")
```

Discovery works by:
1. **mDNS/DNS-SD** (preferred): SiLA2 standard service discovery via `_sila2._tcp.local`
2. **Config file**: Remote servers defined in `lab_config.yaml`
3. **Directory scan**: Local `SiLA2/*/features/` directories for `.sila.xml` files
4. **Port scan**: Scanning ports 50051-50100 for running gRPC servers (fallback)

### Server Deduplication

When the same server is discovered via multiple methods (e.g., mDNS + config + directory), the system automatically merges duplicate entries. Deduplication logic:

1. **Same port + localhost variants**: `localhost`, `127.0.0.1`, `::1` are equivalent
2. **Same port + one is localhost**: Treated as same server (handles mDNS discovering local servers via network IP like `169.254.x.x`)
3. **Exact host:port match**: Same server regardless of discovery method

```python
# Example: mDNS discovers TecanM200 at 169.254.161.10:50051
# Config has TecanM200 at localhost:50051
# → Merged into single entry (prefers config's name/settings)
```

### 2. Command Execution

```python
from src.pnp_client import PnPRegistry

registry = PnPRegistry(base_dir)
await registry.discover()
await registry.connect_all()

# Execute by name - no hardcoded handlers!
result = await registry.execute(
    "OpentronsFlex",    # Any discovered instrument
    "Transfer",         # Any command it supports
    {"source": "A1", "dest": "B1", "volume": 100}
)
```

### 3. Dynamic CLI

Run the new console:

```bash
python pnp_console.py
```

The console automatically:
- Discovers all servers
- Generates menus from their features/commands
- Prompts for parameters based on command definitions
- Executes commands via the generic client

**No code changes needed to add new instruments!**

## Adding a New Instrument

### Step 1: Create Server Directory

```
SiLA2/
└── YourInstrumentSiLA2Server/
    ├── features/
    │   ├── YourMainFeature.sila.xml
    │   └── AnotherFeature.sila.xml
    ├── src/
    │   └── your_servicer.py
    ├── main.py
    └── config.yaml
```

### Step 2: Define Features (`.sila.xml`)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Feature xmlns="http://www.sila-standard.org"
         Feature="YourInstrument/YourFeature"
         Category="instruments">
    
    <DisplayName>Your Feature</DisplayName>
    <Description>What this feature does</Description>
    
    <Command Identifier="DoSomething">
        <DisplayName>Do Something</DisplayName>
        <Description>Performs an action</Description>
        
        <Parameter Identifier="param1">
            <DisplayName>Parameter 1</DisplayName>
            <DataType><Basic>String</Basic></DataType>
        </Parameter>
        
        <Response Identifier="result">
            <DisplayName>Result</DisplayName>
            <DataType><Basic>String</Basic></DataType>
        </Response>
    </Command>
    
</Feature>
```

### Step 3: Implement Server with SiLA2Common

```python
from SiLA2.sila2_common_servicer import (
    SiLA2CommonServicer, 
    ServerMetadata,
    create_common_servicer_for_server
)

# Create metadata
metadata = ServerMetadata(
    server_name="YourInstrument",
    server_type="your_type",
    vendor="YourCompany",
    model="Model X",
)

# Create SiLA2Common servicer
common_servicer = create_common_servicer_for_server(
    server_name="YourInstrument",
    server_type="your_type", 
    feature_dir="features/",
    hardware_callback=lambda: {'status': 'idle', 'connected': True}
)

# Register command executors
async def do_something(params):
    # Your implementation
    return {'result': 'done'}

common_servicer.register_command("DoSomething", do_something)
```

### Step 4: Start Your Server

```bash
python main.py  # Starts on port 50054 (configured in config.yaml)
```

### Step 5: Verify Discovery

```bash
python pnp_console.py --discover
```

Your instrument should appear automatically!

## Migration from Old System

### What's Deprecated

These files contain hardcoded logic that will be replaced:

| Old File | Issue | Replacement |
|----------|-------|-------------|
| `instrument_registry.py` | `KNOWN_TYPES`, `STANDARD_NAMES` | `pnp_discovery.py` |
| `sila_discovery.py` | `KNOWN_SERVERS` dict | `pnp_discovery.py` |
| `lab_console.py` | Hardcoded menus | `pnp_console.py` |

### Migration Steps

1. **Update existing servers** to implement SiLA2Common service
2. **Replace imports** in your code:
   ```python
   # Old
   from src.instrument_registry import InstrumentRegistry
   
   # New
   from src.pnp_client import PnPRegistry
   ```

3. **Update command execution**:
   ```python
   # Old
   await registry.execute_opentrons("Transfer", params)
   
   # New
   await registry.execute("OpentronsFlex", "Transfer", params)
   ```

## SiLA2Common Protocol

The `SiLA2Common.proto` defines a standard interface for server metadata:

```protobuf
service SiLA2ServerInfo {
    rpc GetServerInfo(Empty) returns (ServerInfo);
    rpc GetFeatures(Empty) returns (FeatureList);
    rpc GetStatus(Empty) returns (ServerStatus);
    rpc ExecuteCommand(CommandRequest) returns (CommandResponse);
    rpc GetProperty(PropertyRequest) returns (PropertyResponse);
}
```

### Messages

- **ServerInfo**: name, type, vendor, model, serial_number, version
- **Feature**: identifier, display_name, commands[], properties[]
- **Command**: identifier, parameters[], responses[]
- **ServerStatus**: status, hardware_connected, is_busy

## Workflows

Workflows now use instrument NAMES (matched to discovered servers):

```json
{
    "WorkflowName": "My Workflow",
    "Steps": [
        {
            "StepNumber": 1,
            "Instrument": "OpentronsFlex",
            "Action": "Transfer",
            "Parameters": {
                "source": "A1",
                "dest": "B1",
                "volume": 100
            }
        }
    ]
}
```

The workflow executor:
1. Validates that all instruments exist
2. Validates that all actions are supported
3. Validates parameter types
4. Executes via generic `PnPRegistry.execute()`

## Future: WebApp Integration

The WebApp will use the same `PnPRegistry` API:

```python
# In Flask/FastAPI route
@app.get("/api/instruments")
async def list_instruments():
    servers = registry.list_servers()
    return [s.to_dict() for s in servers]

@app.get("/api/instruments/{name}/commands")
async def get_commands(name: str):
    server = registry.get_server_by_name(name)
    return server.get_all_commands()

@app.post("/api/execute")
async def execute(body: ExecuteRequest):
    result = await registry.execute(
        body.instrument,
        body.command,
        body.parameters
    )
    return result.to_dict()
```

## Troubleshooting

### Server Not Discovered

1. Check that the server directory exists: `SiLA2/{Name}SiLA2Server/`
2. Check that feature files exist: `features/*.sila.xml`
3. If server is running, check the port is in range 50051-50100
4. Check logs for parsing errors

### Command Not Found

1. Verify command is defined in `.sila.xml`
2. Check command identifier matches (case-sensitive)
3. Ensure feature file is valid XML

### Execution Fails

1. Check server is online: `registry.get_server_by_name(name).server_online`
2. Check hardware is ready: `server.hardware_online`
3. Check command is registered in server: `common_servicer.register_command()`

## Summary

| Before | After |
|--------|-------|
| Edit code to add instrument | Just create server directory |
| Hardcoded menus | Menus generated from server |
| Specific client handlers | Generic execution |
| Known server lists | Auto-discovery |
| Code changes for features | Just add .sila.xml |
