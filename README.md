# LabOS — Laboratory Operating System

A modular, plug-and-play laboratory automation platform built on the [SiLA2](https://sila-standard.org) open standard. Coordinates heterogeneous instruments through a unified gRPC interface without instrument-specific hardcoding in the orchestration layer.

## Instruments

| Instrument | Role |
|------------|------|
| Opentrons Flex | Liquid handling |
| Tecan Infinite M200 Pro | Plate reading (absorbance, fluorescence, luminescence) |
| Manual Station | Human-in-the-loop operator steps |
| GoFaGo mobile manipulator | Mobile manipulation and plate transport — hosted remotely, see [architecture doc](v1/docs/ARCHITECTURE.md#mobile-robot-current-status) |

## Quick Start

```
cd v1
double-click LabOS_launcher.bat   # Windows
python launcher.py --all          # any OS
```

Open the web interface at **http://localhost:5000**.

See [v1/docs/SETUP.md](v1/docs/SETUP.md) for full deployment instructions, or [v1/docs/TRAINING_GUIDE.md](v1/docs/TRAINING_GUIDE.md) for an operator-facing walkthrough of the whole system.

## Documentation

All documentation lives in [v1/docs/](v1/docs/):

| Document | Contents |
|----------|----------|
| [TRAINING_GUIDE.md](v1/docs/TRAINING_GUIDE.md) | Operator-facing walkthrough and training talking points |
| [SETUP.md](v1/docs/SETUP.md) | New PC deployment, prerequisites, what to configure |
| [ARCHITECTURE.md](v1/docs/ARCHITECTURE.md) | Four-layer system architecture |
| [WORKFLOW_SYSTEM.md](v1/docs/WORKFLOW_SYSTEM.md) | JSON workflow format, DAG execution |
| [HAL_SYSTEM.md](v1/docs/HAL_SYSTEM.md) | Hardware Abstraction Layer, recipe format |
| [SILA2_INTEGRATION.md](v1/docs/SILA2_INTEGRATION.md) | SiLA2 standard, FDL, discovery |
| [SERVERS.md](v1/docs/SERVERS.md) | Per-server command reference |
| [API_REFERENCE.md](v1/docs/API_REFERENCE.md) | REST endpoints, WebSocket, gRPC |
| [ADDING_NEW_INSTRUMENT.md](v1/docs/ADDING_NEW_INSTRUMENT.md) | Integrate a new instrument in 3 steps |
| [OPERATIONS.md](v1/docs/OPERATIONS.md) | Day-to-day use, troubleshooting |

## Key Concepts

**Plug-and-Play**: The orchestrator discovers instruments at runtime through a PnP registry. Adding a new instrument means deploying a new SiLA2 server — no orchestration code changes.

**sila2 Library**: All current servers are built with the `sila2` Python library. The orchestrator's generic `SilaClient` (Strategy 0) discovers and invokes any command at runtime from the live protobuf descriptor — no compile-time coupling. A dynamic stub-loading fallback (Strategy 1) exists for non-sila2 servers but none are currently deployed. The legacy `SiLA2Common.ExecuteCommand` strategy has been fully removed.

**HAL**: The Hardware Abstraction Layer separates liquid-handling recipes (what to do) from deck configurations (where things are). The same recipe runs on any compatible deck layout.

**DAG Workflows**: Experiments are JSON files with steps and dependencies. Independent steps execute concurrently. Full validation runs before any instrument is touched.

---

*Thesis: "Towards Self-Driving Robotic Chemical Lab for Research Applications" — Andrea Rota, Università degli Studi di Bergamo, 2024/2025*
