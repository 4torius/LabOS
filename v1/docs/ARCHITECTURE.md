# LabOS — System Architecture

## Overview

LabOS is a modular laboratory orchestration platform built on the [SiLA2](https://sila-standard.org) open standard. It coordinates heterogeneous laboratory instruments through a unified gRPC interface, enabling plug-and-play integration without instrument-specific code in the orchestration layer.

The system is organized as a four-layer stack:

```
┌─────────────────────────────────────────────────────────┐
│  LAYER 4: USER INTERFACE                                │
│  FastAPI web app • Workflow designer • Real-time monitor│
└───────────────────────────┬─────────────────────────────┘
                            │ REST / WebSocket
┌───────────────────────────▼─────────────────────────────┐
│  LAYER 3: ORCHESTRATION                                 │
│  LabCore • PnP Registry • PnPWorkflowExecutor           │
└──────────┬──────────────────┬─────────────┬─────────────┘
           │ gRPC             │ gRPC        │ gRPC
           │ sila2 SilaClient │ sila2       │ sila2
┌──────────▼──────┐  ┌────────▼───────┐  ┌──▼─────────────┐
│ LAYER 2: SERVERS│  │ LAYER 2: ...   │  │ LAYER 2: ...   │
│ Opentrons       │  │ Tecan          │  │ Mobile / Manual│
│ WorkflowAPI     │  │ PlateReader    │  │ TaskMgmt/Manual│
│ 18 commands     │  │ Service        │  │ 5 commands each│
│ sila2 library   │  │ 8 commands     │  │ sila2 library  │
└──────────┬──────┘  └───────┬────────┘  └──┬─────────────┘
           │                 │              │
┌──────────▼──────┐  ┌───────▼────────┐  ┌──▼─────────────┐
│ LAYER 1: HW     │  │ LAYER 1: HW    │  │ LAYER 1: HW    │
│ Opentrons Flex  │  │ Tecan M200 Pro │  │ GoFaGo / Human │
└─────────────────┘  └────────────────┘  └────────────────┘
```

---

## Layer 1: Physical Instruments

| Instrument | Role | Connection |
|------------|------|------------|
| Opentrons Flex OT-3 | Liquid handling (1–1000 µL, 12-slot deck) | HTTP API on port 31950 |
| Tecan Infinite M200 Pro | Plate reading (absorbance/fluorescence/luminescence) | USB via Windows bridge |
| GoFaGo (Robotnik RB-Kairos + ABB GoFa + OnRobot RG6) | Mobile manipulation and plate transport | ROS1 on Linux workstation |
| Manual Station | Human-in-the-loop operator steps | UI-confirmed SiLA2 commands |

---

## Layer 2: SiLA2 Servers

Each instrument is wrapped by a dedicated Python (or C#) SiLA2 server built with the **`sila2` Python library** (v0.14). Each server exposes its capabilities via a native SiLA2 feature defined in a Feature Definition Language (FDL) XML file.

| Server | Feature | Commands | Strategy |
|--------|---------|----------|----------|
| `OpentronsSiLA2Server` | `WorkflowAPI` | 18 (ExecuteRecipe, LoadProtocol, GetStatus, …) | **0** |
| `TecanSiLA2Server` | `PlateReaderService` | 8 (RunMeasurement, SetTemperature, Shake, …) | **0** |
| `MobileSiLA2Server` | `MobileRobot` | 5 (execute_task, list_tasks, navigate_to, …) | **0** |
| `ManualStationSiLA2Server` | `ManualStation` | 5 (RequestOperatorTask, ConfirmCompletion, …) | **0** |

The orchestrator (`src/client.py`) applies execution strategies in order of preference:

| Strategy | Protocol | Used by |
|----------|----------|---------|
| **0** | `sila2` library `SilaClient` — fetches descriptor at runtime, no stub coupling | All current servers |
| **1** | Dynamic stub loading (`_pb2` files in `src/pnp_stubs/`) | Custom / non-sila2 servers |

Strategy 0 is selected automatically for any server built with the `sila2` library. The FDL files serve as documentation and drive UI generation (populating command dropdowns in the visual designer).

---

## Layer 3: Orchestration

All orchestration code lives in `v1/src/`.

### PnP Registry (`discovery.py`)

The registry uses a SiLA2-native two-phase strategy:

**Phase 1 — Bootstrap (startup):** `lab_config.yaml` provides `(host, port, name)` seeds for servers that may not yet have mDNS announcements at startup (e.g., instruments on remote network segments). All capability metadata is fetched from the live server via `SiLAService` — no hardcoded command knowledge.

**Phase 2 — Runtime (continuous):** mDNS/DNS-SD (`_sila._tcp.local.`) discovers every SiLA2-compliant server on the network segment automatically. A background listener detects new servers without operator intervention.

In both phases server self-description via `SiLAService` (`ServerName`, `ImplementedFeatures`, `GetFeatureDefinition`) is the only source of truth for features, commands, and metadata. Discovered servers are stored in a thread-safe registry dict:

```
{ server_name → PnPServer(address, features_metadata, online_status) }
```

A periodic health check detects disconnections; reconnection triggers automatic re-registration via the mDNS callback.

### LabCore (`lab_core.py`)

`LabCore` is the central singleton that owns the registry and the workflow executor. It starts the discovery engine, hosts the FastAPI web server, and routes REST/WebSocket requests to the appropriate subsystem.

### Workflow Executor (`workflow.py`)

`PnPWorkflowExecutor` processes JSON workflow files as DAGs:

1. **Parse** — load and validate JSON; build dependency graph
2. **Validate** — check instrument availability, command existence, parameter types (against live registry)
3. **Execute** — topological traversal; dispatch concurrent-eligible steps via async `ExecuteCommand` calls
4. **Report** — return per-step results and summary on completion or failure

See [WORKFLOW_SYSTEM.md](WORKFLOW_SYSTEM.md) for the full workflow model.

---

## Layer 4: User Interface

The web interface is served by FastAPI on port 8000:

- **Dashboard** (`/`) — live instrument status, active workflow progress
- **Workflow Builder** (`/workflow`) — block-based visual designer
- **Recipes** (`/recipes`) — create and edit liquid handling recipes
- **Batch** (`/batch`) — Excel-to-recipe converter
- **Results** (`/results`) — view measurement outputs and plate tracking data
- **API Docs** (`/docs`) — Swagger UI for all REST endpoints

Real-time events (step completions, device status changes) are broadcast via WebSocket to all connected clients.

---

## Key Design Decisions

**Why the `sila2` library instead of SiLA2Common?**  
The `sila2` library's `SilaClient` fetches the protobuf descriptor from the server at runtime, so the orchestrator never needs to import instrument-specific stubs. This solves the compile-time coupling problem more cleanly than the custom `SiLA2Common` service it replaced — and it uses the standard SiLA2 protocol rather than a proprietary extension. The `SiLA2Common` execution strategy has been fully removed; `SiLA2Common_pb2` stubs are retained in `pnp_stubs/` only for server health checks (`GetStatus`).

**Why a Windows bridge for Tecan?**  
The Tecan iControl SDK is Windows-only .NET. The C# bridge process wraps the SDK and exposes a local gRPC endpoint. The Python SiLA2 server connects to this bridge. The bridge runs as a background Windows service and is automatically restarted on failure.

**Why ROS1 for the mobile robot?**  
The Robotnik RB-Kairos navigation stack and the Exsensia LfD platform both require ROS1. The Mobile SiLA2 Server runs on the Linux workstation (running ROS), while the orchestrator runs on Windows. They communicate over the LAN via gRPC.

---

## Directory Structure

```
v1/
├── src/                    # Core orchestration code
│   ├── lab_core.py         # Central orchestrator
│   ├── discovery.py        # PnP discovery engine
│   ├── workflow.py         # DAG workflow executor
│   ├── client.py           # Generic SiLA2 client (Strategy 0/1)
│   └── api/                # FastAPI routes and WebSocket
├── SiLA2/                  # Instrument servers
│   ├── OpentronsSiLA2Server/
│   ├── TecanSiLA2Server/
│   ├── MobileSiLA2Server/
│   ├── ManualStationSiLA2Server/
│   └── SiLA2Common_pb2*.py # SiLA2Common stubs (health checks only; not an execution strategy)
├── Library/                # User-editable assets
│   ├── Workflows/          # JSON workflow definitions
│   ├── Recipes/            # Liquid handling recipes
│   ├── HardwareConfig/     # HAL deck configurations
│   ├── LiquidClasses/      # Aspiration/dispense parameters
│   ├── Analysis/           # Tecan MDFX protocol files
│   └── MobileTasks/        # Registered robot task descriptors
├── Results/                # Experiment outputs (auto-generated)
└── docs/                   # This documentation
```
