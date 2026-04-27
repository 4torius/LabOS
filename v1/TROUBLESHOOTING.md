# LabOS — Troubleshooting Guide

## Quick diagnosis

```bash
cd v1
python launcher.py --status          # Check which servers are online
python -m pytest tests/ -m "not integration" -q   # Verify codebase is intact
python -c "from src.pnp_discovery import PnPDiscovery; print('imports OK')"
```

---

## Discovery Issues

### No instruments appear in CLI or WebApp

**Cause A — Servers not started**
```bash
python launcher.py --servers   # Start all configured servers
# Or individually:
python SiLA2/OpentronsSiLA2Server/main.py
python SiLA2/ManualStationSiLA2Server/main.py
dotnet run  # (in SiLA2/TecanSiLA2Server/) — requires .NET SDK
```

**Cause B — Wrong port in lab_config.yaml**

Each server has a `port:` entry in `lab_config.yaml`. Verify it matches `config.yaml` inside the server folder.
```bash
# Check what ports are actually listening
netstat -ano | findstr "LISTEN" | findstr "500"
```

**Cause C — gRPC check fails even though TCP is open**

`pnp_discovery.py` calls `_is_grpc_server()` after TCP check. If the server starts slowly:
```yaml
# In lab_config.yaml, increase startup_timeout:
servers:
  opentrons:
    startup_timeout: 60   # was 45
```

**Cause D — mDNS not working (Windows firewall)**

Discovery falls back to config automatically. If you see servers in config but not via mDNS, it's likely a Windows firewall rule blocking UDP port 5353.

---

### Server appears offline immediately after starting

The server needs a few seconds to bind the gRPC port. Discovery with `timeout=2.0` (default) may miss a slow-starting server.

```python
# Increase discovery timeout:
from src.pnp_discovery import PnPDiscovery
d = PnPDiscovery()
await d.discover_all(timeout=5.0)
```

---

### Remote server (Mobile Robot) not discovered

The mobile server runs on a remote IP. It only appears via `_discover_from_config()` (reads `lab_config.yaml`). Verify:
1. The `host:` field is correct in `lab_config.yaml`
2. TCP port is reachable: `Test-NetConnection -ComputerName 192.168.11.22 -Port 50053`
3. WiFi hotspot is connected

---

## Command Execution Issues

### "Instrument not found" error

The instrument ID is derived from `server.name.lower().replace(" ", "_")`. If the server name changes (e.g., from `"Opentrons Flex"` to `"OpentronsFlex"`), the ID changes.

```python
# Check actual IDs:
from src.lab_core import LabCore
import asyncio
core = LabCore()
asyncio.run(core.discover())
for i in core.list_instruments():
    print(i.id, i.name, i.status)
```

### "Command not found" error

Two scenarios:

1. **Server doesn't implement it**: Check the `.sila.xml` feature file for that server. The command `<Identifier>` must match exactly.

2. **GetFeatures failed**: Discovery may have succeeded on TCP but failed on gRPC GetFeatures. Re-run discovery or check server logs.

### ExecuteCommand returns `success=False` with no error detail

Enable debug logging in the server's `config.yaml`:
```yaml
logging:
  level: DEBUG
```

For Tecan: set `"Default": "Debug"` in `appsettings.json` → `Logging.LogLevel`.

---

## gRPC / Proto Issues

### `StatusCode.UNAVAILABLE` — "failed to connect to all addresses"

The gRPC channel can't reach the server. Verify:
```bash
# TCP connectivity:
python -c "import socket; s=socket.create_connection(('localhost',50057),2); print('OK'); s.close()"
```

### `StatusCode.UNIMPLEMENTED` — "Method not found"

The server doesn't implement the called RPC. Check:
- Python servers: does `SiLA2CommonAdapter` in `main.py` extend `SiLA2ServerInfo.SiLA2ServerInfoBase`?
- C# Tecan server: is `SiLA2ServerInfo.BindService(_commonServiceImpl)` in `Program.cs`?

### Proto field mismatch (wrong data in fields)

If `GetStatus()` returns nonsense (e.g., `server_online = False` when server is clearly running), the proto files are out of sync.

**Fix**: Regenerate Python stubs:
```bash
cd v1
python regen_stubs.py
```

Both sides must use the **same** `SiLA2Common.proto`. The canonical file is `v1/SiLA2/SiLA2Common.proto`.

---

## Tecan C# Server Issues

### `dotnet run` fails: "No .NET SDKs were found"

Install the .NET SDK (not just runtime):
- Download from https://aka.ms/dotnet/download
- Required version: .NET Framework 4.8 or .NET 6+ (check `.csproj` target)

### Tecan SDK DLL not found

The `lib/` directory must contain the Tecan SDK DLLs. These are not included in source control.  
Copy from the Tecan installation directory.

### Tecan connects in simulation but not real hardware

In `appsettings.json`:
```json
"DefaultConnectionString": "usb"   // Real hardware
// or
"DefaultConnectionString": "sim"   // Simulation (default)
```

Set `"AutoConnectOnStartup": true` to connect automatically.

### Proto compilation errors after editing `SiLA2Common.proto`

The C# proto is at `SiLA2/TecanSiLA2Server/Protos/SiLA2Common.proto`.  
After editing, `dotnet build` regenerates stubs automatically.

**If C# stubs differ from Python stubs**: see "Proto field mismatch" above. Always keep both protos identical (same field numbers, same types).

---

## WebApp Issues

### WebApp shows no instruments (dashboard empty)

Check `LAB_CORE_AVAILABLE` flag:
```bash
cd v1
python -c "from webapp.app import LAB_CORE_AVAILABLE; print('LabCore:', LAB_CORE_AVAILABLE)"
```

If `False`, check for import errors:
```bash
python -c "from src.lab_core import get_lab_core; print('OK')"
```

Common causes: missing `grpc` package, wrong working directory, circular import.

### WebApp starts but `/api/instruments/commands` returns empty

`lab_core.discover()` runs at startup but may timeout if all servers are offline. Start servers first, then the webapp, or call:
```
GET /api/instruments/commands
```
manually after servers are up (forces re-discovery).

### Node-RED URL wrong

Edit `lab_config.yaml`:
```yaml
webapp:
  nodered_url: "http://your-nodered-host:1880"
```

No restart needed — URL is loaded once at startup from `WEBAPP_CONFIG`.

### WebSocket not updating (stuck UI)

The WebSocket `/ws` is a simple ping/pong channel. If the browser shows the UI as stale:
1. Hard refresh (Ctrl+Shift+R)
2. Check browser console for WebSocket errors
3. Verify FastAPI/uvicorn is still running

---

## Stub / Import Issues

### `ModuleNotFoundError: No module named 'pnp_stubs'`

Regenerate gRPC stubs:
```bash
cd v1
python regen_stubs.py
```

Stubs are generated into `src/pnp_stubs/`. If `protoc` is not found:
```bash
pip install grpcio-tools
```

### `ImportError` when starting a Python SiLA2 server

Each server must be run from `v1/` as working directory, not from inside the server folder:
```bash
cd v1
python SiLA2/OpentronsSiLA2Server/main.py   # Correct
# NOT: cd SiLA2/OpentronsSiLA2Server && python main.py
```

Or use `launcher.py` which handles paths automatically.

---

## Running the Test Suite

```bash
cd LabOS
.venv/Scripts/activate   # Windows
# or: source .venv/bin/activate  (Linux/Mac)

cd v1

# Unit tests (no servers needed, fast):
python -m pytest tests/ -m "not integration" -v

# Integration tests (requires running servers):
python launcher.py --servers &   # Start servers in background
python -m pytest tests/ -m integration -v

# Specific server tests:
python -m pytest tests/ -m opentrons -v
python -m pytest tests/ -m tecan -v
python -m pytest tests/ -m manual_station -v

# Single test file:
python -m pytest tests/test_xml_parser.py -v
python -m pytest tests/test_discovery.py -v
```

Test markers:
| Marker | Requires |
|--------|----------|
| `integration` | Any running SiLA2 server |
| `opentrons` | Opentrons server on port 50057 |
| `tecan` | Tecan server on port 50051 |
| `manual_station` | ManualStation server on port 50360 |

---

## Logs

| Component | Log location |
|-----------|-------------|
| Launcher | stdout / terminal |
| Opentrons server | stdout + `v1/opentrons_server.log` (if redirected) |
| ManualStation server | stdout |
| Tecan server | stdout + Windows Event Log |
| WebApp | stdout (uvicorn) + `/api/logs` endpoint |
| Discovery | Python logging (`src.pnp_discovery` logger) |

Enable verbose discovery logging:
```python
import logging
logging.getLogger("src.pnp_discovery").setLevel(logging.DEBUG)
```
