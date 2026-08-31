# LabOS Documentation

LabOS is a modular, plug-and-play laboratory automation platform built on the [SiLA2](https://sila-standard.org) open standard. It coordinates heterogeneous laboratory instruments through a unified gRPC interface without any instrument-specific hardcoding in the orchestration layer.

**New to LabOS, or preparing to train someone else on it?** Start with [TRAINING_GUIDE.md](TRAINING_GUIDE.md) — it's a self-contained walkthrough of the whole system, written for operators rather than developers.

## Instruments

| Instrument | Role |
|------------|------|
| Opentrons Flex | Liquid handling (1–1000 µL, 12-slot deck, SBS plates, gripper) |
| Tecan Infinite M200 Pro | Plate reading (absorbance, fluorescence, luminescence) |
| Manual Station | Human-in-the-loop operator steps (virtual, no hardware) |
| GoFaGo mobile manipulator | Mobile manipulation and plate transport — hosted remotely, see [ARCHITECTURE.md](ARCHITECTURE.md#mobile-robot-current-status) |

## Quick Start

1. `cd v1 && python launcher.py --all` (or double-click `LabOS_launcher.bat`)
2. Open the web interface: **http://localhost:5000**
3. Load a workflow from the Library, click Execute

First time on a new computer? See [SETUP.md](SETUP.md) for the full deployment guide.

---

## Documentation Index

| Document | Contents |
|----------|----------|
| [TRAINING_GUIDE.md](TRAINING_GUIDE.md) | Operator-facing walkthrough: talking points, page-by-page usage, setup, basic troubleshooting (Italian: [TRAINING_GUIDE_IT.md](TRAINING_GUIDE_IT.md)) |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Four-layer system architecture, directory structure, design decisions |
| [WORKFLOW_SYSTEM.md](WORKFLOW_SYSTEM.md) | JSON workflow format, DAG execution model, control flow, manual steps |
| [HAL_SYSTEM.md](HAL_SYSTEM.md) | Hardware Abstraction Layer: recipe format, HAL config, translation pipeline |
| [SILA2_INTEGRATION.md](SILA2_INTEGRATION.md) | SiLA2 standard, FDL format, sila2 library, execution strategies |
| [SERVERS.md](SERVERS.md) | Per-server command reference: Opentrons, Tecan, Manual Station (+ Mobile Robot caveat) |
| [API_REFERENCE.md](API_REFERENCE.md) | REST endpoints, WebSocket events, gRPC SiLA2 interface |
| [ADDING_NEW_INSTRUMENT.md](ADDING_NEW_INSTRUMENT.md) | Step-by-step guide to integrating a new instrument |
| [SETUP.md](SETUP.md) | New PC deployment: prerequisites, venv, configuration, what to change |
| [OPERATIONS.md](OPERATIONS.md) | Commissioning, day-to-day use, troubleshooting |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Code organization, testing, style guide, adding commands |

---

## Key Concepts

**Plug-and-Play**: The orchestrator discovers instruments at runtime through the PnP registry. Adding a new instrument means deploying a new SiLA2 server — no orchestration code changes.

**sila2 Library**: All current servers are built with the `sila2` Python library. The orchestrator tries **Strategy 0** — `SilaClient` (standard SiLA2, used by all current servers) — then falls back to **Strategy 1** — dynamic stub loading for non-sila2 servers (none currently deployed). The legacy `SiLA2Common.ExecuteCommand` strategy has been fully removed from the codebase.

**HAL**: The Hardware Abstraction Layer separates liquid-handling recipes (what to do) from deck configurations (where things physically are). The active HAL config persists across restarts; `ExecuteRecipe` always runs against whatever HAL is currently active.

**DAG Workflows**: Experiments are JSON files with steps and dependencies. Independent steps execute concurrently. Validation runs before any instrument is touched. There is no loop/conditional control-flow mechanism — only linear DAG dependencies.

**Recipe Generator**: Converts an Excel pipetting plan into a recipe (+ optional workflow + HAL) automatically — the normal way to create a recipe, rather than hand-writing JSON.
