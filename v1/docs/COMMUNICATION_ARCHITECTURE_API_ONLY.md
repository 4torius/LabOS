# LabOS API-Only Communication Architecture

## Scope

This document defines who talks to whom, through which protocol and port, and why.
It is aligned with API-first operation: no CLI dependency is required for normal use.

## High-Level Roles

- Client scripts: terminal automation tools (PowerShell, curl, Python scripts).
- WebApp API: HTTP control-plane endpoint exposed by FastAPI.
- LabCore: orchestration and business logic layer.
- Discovery: server discovery and metadata retrieval.
- PnP Client: gRPC command execution bridge.
- SiLA2 servers: instrument-specific control services.

## Control and Data Planes

- Control plane:
  - Client scripts -> WebApp API -> LabCore -> PnP Client -> SiLA2 servers
- Data plane:
  - Instrument-specific payloads and execution results over gRPC between PnP Client and SiLA2 servers

## Communication Flow

1. A client calls an HTTP endpoint on WebApp API.
2. WebApp route delegates to LabCore.
3. LabCore refreshes discovery cache if needed.
4. LabCore resolves target instrument and command.
5. PnP Client calls SiLA2 server through gRPC.
6. Result is returned to WebApp API and then to the client.

## Protocol and Port Model

### User-facing API

- HTTP: WebApp API (default localhost:5000)
- Purpose: single stable entrypoint for scripts and operators

### Instrument services

- gRPC ports are per server and configured in lab_config.yaml
- Typical values in current setup:
  - Tecan: 50051
  - Opentrons: 50057
  - Mobile: 50053
  - Manual Station: 50360

### Discovery

- mDNS uses UDP 5353 when enabled
- Fallback discovery uses config and filesystem scanning

## Source of Truth Rules

1. Host and port must come from lab_config.yaml for runtime behavior.
2. Hardcoded service ports in routes or launcher are forbidden.
3. Remote servers are never started locally by launcher.
4. API callers only depend on WebApp API, not on internal service addresses.

## Why API-Only

- Stable terminal automation through one interface.
- Reduced operational complexity versus dual API + CLI maintenance.
- Cleaner security and networking boundaries.
- Better portability for orchestration scripts.

## Minimal API Operational Surface

- Discovery and devices:
  - GET /api/devices
  - GET /api/instruments/commands
- Execution:
  - POST /api/devices/{device_id}/command
- Workflows:
  - routes under /api/workflows (existing module)
- Operator actions:
  - routes under /api/operator
  - POST /api/emergency/stop

## Network Practices Before Physical Runs

- Use reserved/static addresses for remote instrument hosts.
- Keep one subnet/VLAN for lab control traffic where possible.
- Open only required ports between orchestrator and instrument hosts.
- Validate host:port reachability before each session.

## Validation Checklist

- Launcher status shows expected host:port for all enabled servers.
- API discovery endpoints list expected devices and commands.
- Emergency stop endpoint returns attempted/successful/skipped summary.
- Full test suite passes on baseline environment.
