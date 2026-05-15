# LabOS Documentation

LabOS is a modular, plug-and-play laboratory automation platform built on the [SiLA2](https://sila-standard.org) open standard. It coordinates heterogeneous laboratory instruments through a unified gRPC interface without any instrument-specific hardcoding in the orchestration layer.

## Instruments

| Instrument | Role |
|------------|------|
| Opentrons Flex OT-3 | Liquid handling (1–1000 µL, 12-slot deck, SBS plates) |
| Tecan Infinite M200 Pro | Plate reading (absorbance, fluorescence, luminescence) |
| GoFaGo (Robotnik RB-Kairos + ABB GoFa + OnRobot RG6) | Mobile manipulation and plate transport |
| Manual Station | Human-in-the-loop operator steps |

## Quick Start

1. Configure server addresses in `v1/SiLA2/servers_config.yaml`
2. Start all servers and the orchestrator: `v1/START.bat` (Windows)
3. Open the web interface: http://localhost:8000
4. Load a workflow from the Library, click Run

First time on a new computer? See [SETUP.md](SETUP.md) for the full deployment guide.

---

## Documentation Index

| Document | Contents |
|----------|----------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Four-layer system architecture, directory structure, design decisions |
| [WORKFLOW_SYSTEM.md](WORKFLOW_SYSTEM.md) | JSON workflow format, DAG execution model, control flow, manual steps |
| [HAL_SYSTEM.md](HAL_SYSTEM.md) | Hardware Abstraction Layer: recipe format, HAL config, translation pipeline |
| [SILA2_INTEGRATION.md](SILA2_INTEGRATION.md) | SiLA2 standard, FDL format, SiLA2Common protocol, stub regeneration |
| [SERVERS.md](SERVERS.md) | Per-server command reference: Opentrons, Tecan, Mobile Robot, Manual Station |
| [API_REFERENCE.md](API_REFERENCE.md) | REST endpoints, WebSocket events, gRPC SiLA2Common interface |
| [ADDING_NEW_INSTRUMENT.md](ADDING_NEW_INSTRUMENT.md) | Step-by-step guide to integrating a new instrument |
| [SETUP.md](SETUP.md) | New PC deployment: prerequisites, venv, configuration, what to change |
| [OPERATIONS.md](OPERATIONS.md) | Commissioning, day-to-day use, troubleshooting, LfD task teaching |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Code organization, testing, style guide, adding commands |

---

## Key Concepts

**Plug-and-Play**: The orchestrator discovers instruments at runtime through the PnP registry. Adding a new instrument means deploying a new SiLA2 server — no orchestration code changes.

**SiLA2Common**: A generic execution protocol (GetServerInfo, GetFeatures, ExecuteCommand, GetProperty) that every server implements alongside its native feature interface. This is the only interface the orchestrator uses.

**HAL**: The Hardware Abstraction Layer separates liquid-handling recipes (what to do) from deck configurations (where things physically are). The same recipe runs on any compatible deck.

**DAG Workflows**: Experiments are JSON files with steps and dependencies. Independent steps execute concurrently. Validation runs before any instrument is touched.

**LfD Task Teaching**: Mobile robot manipulation tasks are taught by physically guiding the ABB GoFa arm through the task. No robot programming required.
