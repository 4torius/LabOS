# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BicoccaLab v7 (LabOS 2.0) is a SiLA2-compliant laboratory automation system that orchestrates scientific instruments (Opentrons Flex liquid handler, Tecan Infinite M200Pro plate reader, mobile robots, manual stations) through a plug-and-play architecture. No hardcoded instruments or commands — servers describe themselves via `.sila.xml` feature definitions.

Working directory for all commands: `v1/`

## Commands

### Setup
```bash
# Venv is at repo root (LabOS\.venv), not inside v1/
cd LabOS
.\.venv\Scripts\Activate.ps1
cd v1
pip install -r requirements.txt
```

### Run
```bash
python launcher.py --all      # Start all servers + webapp
python launcher.py --servers  # Start SiLA2 servers only
python launcher.py --webapp   # Start webapp only (http://127.0.0.1:5000)
python launcher.py --cli      # Start interactive CLI
python launcher.py --status   # Check system status
```

### Individual servers
```bash
python SiLA2/OpentronsSiLA2Server/main.py   # Port 50052 (Python)
python SiLA2/ManualStationSiLA2Server/main.py  # Port 50360 (Python)
dotnet run  # (in SiLA2/TecanSiLA2Server/) Port 50051 (C#)
```

### Tests
```bash
python test_commands.py       # Test command execution against instruments
python test_stub_loading.py   # Test gRPC stub loading
python test_refill.py         # Test refill functionality
python regen_stubs.py         # Regenerate gRPC stubs from .proto files
```

### Type checking
```bash
pyright  # Config in pyrightconfig.json
```

## Architecture

### Core Data Flow

```
CLI (pnp_console.py) ─┐
                       ├─→ LabCore (src/lab_core.py) ─→ PnPClient ─→ gRPC ─→ Instrument Servers
WebApp (webapp/app.py) ┘         ↑
                          PnPDiscovery
```

**`src/lab_core.py`** is the single unified interface for both CLI and WebApp. All instrument interactions go through it.

### Discovery (`src/pnp_discovery.py`)

Finds servers via four mechanisms (tried in order): mDNS (`_sila2._tcp.local`), port scan (50051–50100), `lab_config.yaml`, and `.sila.xml` XML parsing. Returns `PnPServer` objects with name, host, port, and parsed commands/features.

### Command Execution (`src/pnp_client.py`)

Generic gRPC client that loads or generates stubs dynamically. Connects to any SiLA2 server and executes commands without hardcoded instrument knowledge. Streaming results are supported.

### Workflow Execution (`src/pnp_workflow_executor.py`)

Loads `.workflow.json` files from `Library/Workflows/`. Validates all steps, builds a dependency graph, executes steps in parallel where possible, and handles failures via categorized retry/intervention logic. On unrecoverable errors, pauses and prompts the operator via the WebApp.

### SiLA2 Servers (`SiLA2/`)

Each server follows this layout:
```
SiLA2/<InstrumentName>/
  features/*.sila.xml    # Feature definitions (auto-parsed for commands/params)
  src/                   # Implementation
  main.py                # Entry point
  config.yaml            # Connection settings
```

The `SiLA2/_NewInstrumentTemplate/` directory is the canonical starting point for adding new instruments. Copy it and implement the feature services — no changes to core code needed.

`SiLA2/SiLA2Common.proto` defines the standard metadata interface all servers implement. `SiLA2/sila2_common_servicer.py` is a helper to add SiLA2Common to any server. `SiLA2/sila2_mdns_registry.py` handles mDNS registration.

### WebApp (`webapp/app.py`)

FastAPI + Jinja2 + WebSocket. Key routes: `/api/instruments`, `/api/instruments/{name}/commands`, `/api/execute`, `/api/workflows`, `/api/library/*`, `/ws` (real-time updates). The app dynamically generates UI from server metadata — menus are never hardcoded.

### Configuration (`lab_config.yaml`)

Central config for everything: server definitions (host/port/startup command), discovery settings, workflow execution parameters, error retry strategy, and UI dropdown options (recipes, analysis protocols, locations). Per-server overrides live in each server's own `config.yaml` or `appsettings.json` (Tecan).

### Library Resources (`Library/`)

- `Recipes/` — JSON pipetting recipes for Opentrons
- `Workflows/` — JSON multi-step workflow definitions
- `HardwareConfig/` — HAL configuration files
- `Analysis/` — Tecan measurement protocols (`.mdfx`)
- `MobileTasks/` — Mobile robot task definitions
- `Results/` — Output data from runs

## Tech Stack

- **Python 3.10+** with asyncio throughout
- **gRPC + Protocol Buffers** — all inter-service communication (SiLA2 standard)
- **FastAPI + Uvicorn** — web server
- **Zeroconf** — mDNS service discovery
- **C#/.NET** — Tecan server only (`SiLA2/TecanSiLA2Server/`)
- **pytest + pytest-asyncio** — test framework

## Key Patterns

- **Plug & Play**: Adding a new instrument means creating a new SiLA2 server (copy `_NewInstrumentTemplate`). The system discovers it automatically via mDNS or config — no core code changes.
- **Dynamic UI**: CLI menus and WebApp dropdowns are generated from `.sila.xml` metadata at runtime.
- **Async-first**: All I/O uses asyncio. New code in `src/` and `webapp/` should be async.
- **Human-in-the-loop**: Workflow failures route to `InterventionRequest` objects consumed by the WebApp for operator decisions (Retry / Skip / Abort).
- **gRPC stubs**: Pre-generated stubs live in `src/pnp_stubs/`. Run `python regen_stubs.py` after editing any `.proto` file.

## roadmap sviluppo:
C:\Users\PC\.claude\projects\c--Users-PC-Desktop-LabOS\memory\project_roadmap.md