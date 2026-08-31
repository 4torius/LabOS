# LabOS — System Architecture

## Overview

LabOS is a modular laboratory orchestration platform built on the [SiLA2](https://sila-standard.org) open standard. It coordinates heterogeneous laboratory instruments through a unified gRPC interface, enabling plug-and-play integration without instrument-specific code in the orchestration layer.

The system is organized as a four-layer stack, across **four instruments**: Opentrons Flex, Tecan M200 Pro, Manual Station (all three hosted locally on the Windows orchestrator machine), and the GoFaGo mobile manipulator (hosted remotely).

```
┌─────────────────────────────────────────────────────────┐
│  LAYER 4: USER INTERFACE                                │
│  FastAPI web app (port 5000) • Workflow builder         │
│  Recipe Generator • Dashboard • Operator console         │
└───────────────────────────┬─────────────────────────────┘
                            │ REST / WebSocket (/ws)
┌───────────────────────────▼─────────────────────────────┐
│  LAYER 3: ORCHESTRATION  (v1/src/)                       │
│  LabCore • PnP Discovery Registry • PnPWorkflowExecutor  │
└──────┬──────────────┬──────────────┬─────────────┬──────┘
       │ gRPC          │ gRPC         │ gRPC        │ gRPC (remote, LAN)
┌──────▼──────┐ ┌──────▼──────┐ ┌─────▼───────┐ ┌───▼─────────────┐
│ Opentrons   │ │ Tecan       │ │ Manual      │ │ Mobile Robot    │
│ WorkflowAPI │ │ PlateReader │ │ Station     │ │ (GoFaGo)        │
│ 17 commands │ │ 8 commands  │ │ 6 commands  │ │ remote @        │
│ port 50302  │ │ port 50051  │ │ port 50500  │ │ 192.168.11.22   │
└──────┬──────┘ └──────┬──────┘ └──────┬──────┘ └──────┬──────────┘
       │               │                │               │
┌──────▼──────┐ ┌──────▼──────┐ ┌───────▼─────┐ ┌───────▼─────────┐
│ Opentrons   │ │ Tecan M200  │ │ Human       │ │ RB-Kairos base +│
│ Flex        │ │ Pro         │ │ operator    │ │ ABB GoFa arm    │
└─────────────┘ └─────────────┘ └─────────────┘ └─────────────────┘
```

---

## Layer 1: Physical Instruments

| Instrument | Role | Connection |
|------------|------|------------|
| Opentrons Flex | Liquid handling (1–1000 µL, 12-slot deck, gripper) | HTTP API on port 31950 (configured IP in `SiLA2/OpentronsSiLA2Server/config.yaml`) |
| Tecan Infinite M200 Pro | Plate reading (absorbance/fluorescence/luminescence) | USB, via a Windows C# bridge process |
| Manual Station | Human-in-the-loop operator steps | Virtual — no physical hardware, drives web UI notifications |
| GoFaGo (Robotnik RB-Kairos + ABB GoFa + OnRobot RG6) | Mobile manipulation and plate transport | ROS1, hosted remotely at `192.168.11.22` — see [Mobile Robot](#mobile-robot-current-status) below |

---

## Layer 2: SiLA2 Servers

Each instrument is wrapped by a dedicated SiLA2 server built with the **`sila2` Python library** (Tecan additionally uses a C# bridge process for the vendor SDK). Each server exposes its capabilities via a native SiLA2 feature defined in a Feature Definition Language (FDL) XML file, and self-declares which `Library/` file types it consumes via the shared `LabOSLibraryProvider` feature (see below).

| Server | Feature | Commands | Port |
|--------|---------|----------|------|
| `OpentronsSiLA2Server` | `WorkflowAPI` | 17 (ExecuteRecipe, PauseRun/ResumeRun/AbortRun, LoadHardwareConfig, RefillTipRack, GetStatus, …) | 50302 |
| `TecanM200SiLA2Server` | `PlateReaderService` | 8 (RunMeasurement, Connect/Disconnect, PlateIn/PlateOut, SetTemperature, …) | 50051 (bridge internal: 50055) |
| `ManualStationSiLA2Server` | `ManualStation` | 6 (RequestOperatorTask, ConfirmTaskComplete, SendNotification, …) | 50500 |

Full per-command reference: [SERVERS.md](SERVERS.md).

### `LabOSLibraryProvider` — self-declaring library types

Every server also implements a small shared feature, `LabOSLibraryProvider`, exposing one property (`LibraryCatalog`) that returns a JSON description of which `Library/` subfolders and file types that server cares about (e.g. Opentrons declares `recipe`, `hal_config`, `liquid_class`; Tecan declares `analysis`). The orchestrator reads this at discovery time to populate UI dropdowns automatically. **Adding a new instrument never requires touching orchestrator code** — see [ADDING_NEW_INSTRUMENT.md](ADDING_NEW_INSTRUMENT.md).

### Execution strategies (`src/client.py`)

| Strategy | Protocol | Used by |
|----------|----------|---------|
| **0** | `sila2` library `SilaClient` — fetches the feature descriptor from the live server at runtime, no compile-time stub coupling | All current servers |
| **1** | Dynamic gRPC stub loading (`_pb2.py` files in `src/pnp_stubs/`) | Custom / non-sila2 servers only (none currently deployed) |

The legacy `SiLA2Common.ExecuteCommand` execution path has been **fully removed** from the codebase. The only remaining `SiLA2Common` reference is an optional legacy `GetStatus` health-check probe, not an execution path.

A process-wide cache (`get_shared_sila_client()`) keeps one `SilaClient` per `host:port`, invalidated only on a transport-level `grpc.RpcError` — this avoids repeated protobuf descriptor-pool churn.

**Timeout convention**: everywhere in the stack (`client.py`, `workflow.py`, `lab_core.py`, the Opentrons/Tecan servers), a timeout value of `0` or `null`/`None` means **unlimited**, not "expire immediately." This matters for long-running protocols (multi-hour Opentrons runs, long Tecan kinetic reads).

---

## Layer 3: Orchestration

All orchestration code lives in `v1/src/`.

### PnP Registry (`discovery.py`)

The registry uses a SiLA2-native two-phase strategy:

**Phase 1 — Bootstrap (startup):** `lab_config.yaml`'s `servers:` section provides `(host, port, name)` seeds so servers on remote/unreachable-by-mDNS network segments are still tried.

**Phase 2 — Runtime (continuous):** mDNS/DNS-SD (`_sila2._tcp.local.`) discovers every SiLA2-compliant server on the local network segment automatically, merging/correcting bootstrap entries as live announcements arrive.

In both phases, server self-description via `SiLAService` (`ServerName`, `ImplementedFeatures`, `GetFeatureDefinition`) plus `LabOSLibraryProvider.LibraryCatalog` is the source of truth for features, commands, and file-type dropdowns — nothing is hardcoded per instrument.

**Stable instrument IDs**: a server announcing on a `lab_config.yaml`-configured port only claims that config's instrument ID if its announcing host matches the configured host (or the host of whatever is already registered under that ID). This specifically guards against a stale/foreign server left running on a different machine after a redeploy silently taking over another instrument's identity in the UI — a real bug from an earlier version, now fixed (`lab_core.py::_server_to_instrument`).

### LabCore (`lab_core.py`)

`LabCore` is the central singleton (`get_lab_core()`) that owns the registry and the workflow executor, shared between the CLI and the web app. It starts discovery, and routes REST/WebSocket requests to the appropriate subsystem. `execute_command()` applies the configured command timeout (`workflow.default_timeout` from `lab_config.yaml`, `0`/`null` = unlimited).

### Workflow Executor (`workflow.py`)

`PnPWorkflowExecutor` processes JSON workflow files as DAGs (nodes = steps, edges = `DependsOn`):

1. **Parse** — load JSON into `WorkflowStep` objects
2. **Validate** — instrument/command existence against the live registry, required parameters, recipe-capacity checks
3. **Execute** — repeatedly collects the "wave" of steps whose dependencies are all satisfied and dispatches them concurrently via `asyncio.gather` (`_execute_parallel`), or runs steps one at a time (`_execute_sequential`)
4. **Report** — a `WorkflowResult` with per-step outcomes and a summary

See [WORKFLOW_SYSTEM.md](WORKFLOW_SYSTEM.md) for the full workflow model — note that only linear DAG dependencies are supported, there is no loop/conditional control-flow mechanism.

---

## Layer 4: User Interface

The web interface is a FastAPI app served on **port 5000** by default (`python launcher.py --webapp`, or `--all` for the full stack). Pages:

- **Dashboard** (`/dashboard`) — live instrument cards with direct command execution
- **Workflow Builder** (`/workflows`) — drag-and-drop DAG designer, save/load/version, execute
- **Recipe Generator** (`/batch`, alias `/recipes`) — Excel-to-recipe/HAL/workflow converter
- **Hardware** (`/hardware`) — HAL configuration switcher
- **Operator** (`/operator`) — pending manual tasks, Emergency Stop, tip refill
- **Plates** (`/plates`) and **Run History** (`/runs`) — SQLite-backed experiment records, searchable, with AI-context export
- **Results** (`/results`) — raw output file browser (CSV/Excel/AnIML/XML)
- **History** (`/history`) — ephemeral in-memory session log (lost on restart — **not** the same as Run History)
- **Settings** (`/settings`) — `lab_config.yaml` editor

A single WebSocket endpoint (`/ws`) broadcasts all real-time events (workflow progress, device status, manual-intervention requests) to every connected client; a global floating status bar with Pause/Resume/Abort (defined once in `base.html`) appears on every page whenever a workflow is running.

Full endpoint list: [API_REFERENCE.md](API_REFERENCE.md).

---

## Safety & Reliability Features (Opentrons server)

These were added in later hardening passes and are not visible anywhere except in behavior — worth knowing when troubleshooting:

- **Leftover-tip pre-run guard**: because the installed robot-server firmware has no real tip-presence sensor, the server tracks per-mount "might still have a tip" state across runs (`tip_mount_state.json`) and automatically prepends a forced drop-tip step for any mount it isn't sure is clear, before the recipe's own steps run. Fully automatic — there is no UI toggle.
- **Tip consumption tracking**: separate from the above, `tip_state.json` tracks how many tips remain per rack, surfaced in the Operator page's refill flow.
- **Run archive**: every run (success or failure) is logged to `run_archive/` as JSON, with automatic compression (after 3 days) and deletion (after 30 days) — configurable in `config.yaml`.
- **Waste comparison**: if the recipe carries a waste estimate (from the Recipe Generator), the archive summary compares it against the actual aspirate/dispense delta from the run log.
- **HAL persistence**: the active HAL config is written to `Library/HardwareConfig/.active` on every switch (from the webapp or a direct SiLA2 call), so it survives a server restart instead of reverting to `config.yaml`'s static default.
- **Pause/Resume/Abort/EmergencyStop**: served through a dedicated control lock so they can interrupt an in-progress run without queuing behind it. If the tip-drop/home sequence itself fails during an abort, the response is an explicit warning to verify manually — it never silently reports full success.

---

## Mobile Robot: Current Status

**Confirmed working and complete** — verified directly against the remote host's live Docker deployment and its actual FDL (not carried forward from an older draft). The GoFaGo mobile manipulator's SiLA2 server runs remotely on its own Linux/Docker host at `192.168.11.22:50053` (container `mobile-sila2`, `restart: always`) — `lab_config.yaml`'s `mobile` server entry (`enabled: true`, `remote: true`) points at it, and the orchestrator discovers and drives it exactly like the local instruments: same `sila2` library, same mDNS self-description, same `LabOSLibraryProvider` pattern (declares `mobile_task` as a dynamic library type backed by `Library/MobileTasks/`).

A few things worth knowing about this instrument specifically:

- **The server's source code is not part of this repository.** Unlike Opentrons/Tecan/Manual Station, GoFaGo's SiLA2 server is deployed and maintained entirely on the remote host — this repo only holds the *client-side* configuration (`lab_config.yaml`'s bootstrap entry). Don't look for it under `SiLA2/` here.
- Its feature is `TaskManagement` (9 commands, 6 properties) — task discovery/execution (`RefreshTasks`, `ExecuteTask`, `CancelTask`, `GetTaskDetails`) plus explicit hardware-activation control over the arm/gripper/camera/mobile base (`SetHardwareActivation(ByRole)`, `(De)ActivateAllHardware`). See [SERVERS.md](SERVERS.md#mobile-robot-sila2-server) for the full, FDL-verified reference.
- **Self-healing by design**: a background thread keeps retrying the ROS connection if it's down, without crashing the server. If the ROS master itself gets restarted underneath it (a scenario an in-process `rospy` session can't recover from), the server deliberately terminates itself and relies on Docker's `restart: always` to come back up clean — a well-chosen failure mode rather than an accidental one.
- New manipulation tasks are taught by physically guiding the ABB GoFa arm through the motion (LfD — learning from demonstration) via the Exsensia platform on the remote host, relative to each workstation's ArUco marker, then registered and refreshed into `Library/MobileTasks/`.
- An earlier session recorded an intermittent `"Server offline"` symptom (commands failing even while discovery showed it online). That appears resolved — the container has been up continuously for weeks as of this check.

---

## Key Design Decisions

**Why the `sila2` library instead of a custom protocol?**
The `sila2` library's `SilaClient` fetches the protobuf descriptor from the server at runtime, so the orchestrator never needs to import instrument-specific stubs. This uses the standard SiLA2 protocol rather than a proprietary extension, and any SiLA2-compliant client (not just LabOS) can interact with the servers.

**Why a Windows bridge for Tecan?**
The Tecan iControl SDK is Windows-only .NET. The C# bridge process wraps the SDK and exposes a local gRPC endpoint (port 50055) that the Python SiLA2 server connects to. The bridge is auto-restarted on crash or unresponsiveness by a background health monitor in `bridge_launcher.py`.

**Why is `ExecuteRecipe` just a recipe name, not recipe+HAL?**
Earlier versions passed both a recipe and a HAL config together on every call. The current design decouples them: the HAL config is selected once (via the Hardware page or `LoadHardwareConfig`) and persists in `.active`; `ExecuteRecipe(RecipeName)` always runs against whatever HAL is currently active. This matches how a real deck is used — you don't reconfigure the deck on every single run.

---

## Directory Structure

```
v1/
├── launcher.py              # Main entry point (--all / --servers / --webapp / --status / --stop)
├── LabOS_launcher.bat       # Windows double-click wrapper around launcher.py
├── lab_config.yaml          # Centralized configuration (servers, discovery, workflow, webapp)
├── requirements.txt         # Python dependencies
│
├── src/                     # Core orchestration code
│   ├── lab_core.py          # LabCore singleton, discovery→instrument mapping
│   ├── discovery.py         # PnP discovery engine (bootstrap + mDNS)
│   ├── workflow.py          # DAG workflow executor
│   ├── client.py            # SiLA2 client (Strategy 0/1), shared client cache
│   ├── config_schema.py     # DEFAULT_CONFIG + deep-merge for lab_config.yaml
│   └── excel_recipe_parser.py  # Excel → recipe/HAL/workflow generation engine
│
├── SiLA2/                   # Instrument servers
│   ├── OpentronsSiLA2Server/
│   ├── TecanM200SiLA2Server/
│   ├── ManualStationSiLA2Server/
│   ├── features/            # Shared FDL (LabOSLibraryProvider)
│   └── _NewInstrumentTemplate/  # Copy this to add a new instrument
│
├── webapp/                  # FastAPI web application
│   ├── app.py                # App shell, page routes, WebSocket, config editor
│   └── routes/                # batch, workflows, instruments, operator, hardware, db, plates
│
├── Library/                 # User-editable assets
│   ├── Workflows/            # JSON workflow definitions
│   ├── Recipes/              # Liquid handling recipes (+ excel/ source spreadsheets)
│   ├── HardwareConfig/       # HAL deck configurations (+ .active marker)
│   ├── LiquidClasses/        # Aspiration/dispense parameters
│   └── Analysis/             # Tecan .mdfx protocol files
│
├── Results/                  # Experiment outputs (auto-generated)
├── tests/                    # pytest suite (unit/ + integration)
└── docs/                     # This documentation
```
