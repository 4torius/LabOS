# LabOS — New PC Setup Guide

This guide covers deploying LabOS on a new Windows computer from scratch.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Windows 10/11 64-bit | The orchestrator and all servers except the mobile robot run on Windows |
| Python 3.10 or 3.11 | 3.12 is not recommended (some grpcio wheels lag behind) |
| Git | To clone the repository |
| Tecan iControl software | Required for the Tecan C# bridge (Tecan SDK) |
| Opentrons App | Required for Opentrons Flex |


---

## 1 — Copy the project

```
git clone https://github.com/4torius/LabOS.git C:\LabOS
```

Or copy the folder directly. The working directory for all commands below is `C:\LabOS\v1`.

---

## 2 — Create a Python virtual environment

```powershell
cd C:\LabOS\v1
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

For development/testing add:
```powershell
pip install -r requirements-dev.txt
```

If `Activate.ps1` is blocked by execution policy:
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

---

## 3 — Compile the Tecan bridge

The bridge is a C# console application (`TecanSiLA2Server.exe`) that wraps the Tecan iControl SDK.

1. Open `v1/SiLA2/TecanM200SiLA2Server/bridge/TecanSiLA2Server.sln` in Visual Studio (any edition, including Community).
2. Set build configuration to **Release**.
3. Build → the output is `bridge/bin/Release/net48/TecanSiLA2Server.exe`.

TecanBridge requires `Tecan.iControl.SDK.dll` installed with iControl. If it is not in the GAC, copy it into the project directory alongside the exe.

**Important**: `src/bridge_launcher.py` always prefers the **Release** build over Debug if both exist. If you rebuild only in Debug configuration while an older Release exe is still present, your changes will silently not run — either rebuild Release too, or delete the stale Release build. See [SERVERS.md](SERVERS.md#bridge-binary-is-up-to-date) for a concrete case of this happening.

---

## 4 — What to configure

Everything is in **`v1/lab_config.yaml`** and individual server `config.yaml` files.

### 4.1 — Mobile robot IP (deployed and maintained remotely)

```yaml
# v1/lab_config.yaml
servers:
  mobile:
    host: 192.168.11.22    # ← the Linux/Docker host running GoFaGo's SiLA2 server
    port: 50053
    remote: true            # runs on external Linux PC, never started locally
    network:
      ros_master_uri: http://localhost:11311
      ros_namespace: /setup1
```

The mobile robot's SiLA2 server runs entirely on this remote host (Docker deployment) — there is nothing to install locally for it beyond this bootstrap entry. Set `enabled: false` here only if you're deploying LabOS somewhere that genuinely doesn't have a GoFaGo unit. See [ARCHITECTURE.md](ARCHITECTURE.md#mobile-robot-current-status) and [SERVERS.md](SERVERS.md#mobile-robot-sila2-server).

### 4.2 — Opentrons robot IP

```yaml
# v1/SiLA2/OpentronsSiLA2Server/config.yaml
hardware:
  robot_ip: 169.254.69.185   # ← update to your robot's IP (link-local or LAN)
```

The Opentrons Flex defaults to `169.254.69.185` over USB. If connected via Wi-Fi or static LAN, change accordingly.

### 4.3 — Tecan connection

```yaml
# v1/SiLA2/TecanM200SiLA2Server/config.yaml
hardware:
  simulation: false          # set true to run without physical instrument
  bridge_exe: TecanBridge.exe
```

If iControl is not installed, set `simulation: true` to start the server in stub mode.

### 4.4 — Server ports (only if there are conflicts)

Default port assignments:

| Server | Port |
|--------|------|
| Tecan M200 | 50051 |
| Opentrons Flex | 50302 |
| Mobile Robot | 50053 |
| Manual Station | 50500 |
| Web interface | 5000 |

If another process occupies a port, change it in both `v1/lab_config.yaml` (under `servers.<name>.port`) and the server's own `config.yaml`.

### 4.5 — Network topology (multi-PC lab)

If the Windows PC and the Linux robot PC are on different subnets, ensure:
- Port 50053 (mobile robot gRPC) is reachable from the Windows PC
- Port 50051, 50302, 50500 (all Windows servers) are firewalled from external access

```powershell
# Open required inbound ports in Windows Firewall (run as Administrator)
New-NetFirewallRule -DisplayName "LabOS gRPC" -Direction Inbound -Protocol TCP -LocalPort 50051,50302,50500 -Action Allow
```

---

## 5 — First run

```powershell
cd C:\LabOS\v1
python launcher.py --all
```

This starts all Windows-hosted SiLA2 servers and the web application. Each server opens in a separate terminal window.

Open **http://localhost:5000** — the dashboard should show all instruments. Servers that are online appear with a green indicator.

If the mobile robot is not yet running (Linux PC not set up), its indicator will show "offline" — that is expected and the system still operates without it.

---

## 6 — Mobile robot (GoFaGo) — deployed remotely, nothing to set up here

The mobile robot's SiLA2 server is **not part of this repository's checkout** — it runs entirely from its own deployment on the remote Linux/Docker host (see [ARCHITECTURE.md](ARCHITECTURE.md#mobile-robot-current-status)). As long as `lab_config.yaml`'s `mobile` entry points at the right host/port (step 4.1), the orchestrator will discover and use it automatically. Confirm reachability from Windows if you want to double-check before a session:

```powershell
python -c "import grpc; ch = grpc.insecure_channel('192.168.11.22:50053'); print(grpc.channel_ready_future(ch).result(timeout=5))"
```

If it doesn't connect on a given day (network issue, remote host down), LabOS still operates fully with Opentrons + Tecan + Manual Station — the mobile robot just shows offline on the Dashboard.

---

## 7 — Verify the installation

From the dashboard:
1. Opentrons, Tecan, Manual Station, and Mobile Robot all show "online"
2. Go to **Workflow Builder** → load `Opentrons_Only_Test.workflow.json` → click **Validate** — should pass with no errors
3. Run it with a scrap plate to confirm liquid handling is functional

From the API:
```powershell
Invoke-RestMethod http://localhost:5000/api/instruments | ConvertTo-Json -Depth 3
Invoke-RestMethod http://localhost:5000/api/status
```

---

## 8 — Quick reference: files you will modify

| File | What to change |
|------|---------------|
| `v1/lab_config.yaml` | Mobile robot IP, ROS master URI, ROS namespace |
| `v1/SiLA2/OpentronsSiLA2Server/config.yaml` | Opentrons robot IP |
| `v1/SiLA2/TecanM200SiLA2Server/config.yaml` | Tecan simulation flag, bridge exe path |
| `v1/SiLA2/ManualStationSiLA2Server/config.yaml` | Port if 50500 is in use |

All other files — orchestration code, workflows, recipes, HAL configs — do not need changes for a clean deployment.

---

## 9 — Troubleshooting setup issues

**`ModuleNotFoundError: No module named 'sila2'`**
The virtual environment is not activated. Run `.\.venv\Scripts\Activate.ps1` first.

**`grpcio` or `protobuf` import errors after install**
Run `pip install --force-reinstall grpcio grpcio-tools protobuf` inside the venv.

**TecanBridge.exe not found**
The `config.yaml` `bridge_exe` path is relative to the server's directory. Compile and copy the exe there (step 3 above).

**`OSError: [WinError 10061] Connection refused` on port 50053**
The mobile server on the Linux PC is not running. Start it manually or set `enabled: false` for the mobile server in `lab_config.yaml` if not needed.

**Web interface blank or 404**
The launcher is still starting up (takes ~10 seconds for all servers). Refresh after a moment. If it persists, check the launcher terminal for startup errors.

**Server shows as online but commands fail with "Feature not found"**
The orchestrator uses Strategy 0 (sila2 SilaClient) for all current servers. If a server was recently rebuilt, the cached SilaClient may be stale. Restart the orchestrator to invalidate the client cache.

**"Cannot reach <server> ... its SiLA2 endpoint did not respond"**
The server is announced (mDNS, or a bootstrap seed in `lab_config.yaml`) but its
gRPC endpoint is not answering. Check the process is running and the port
matches: `python labos.py status`. There is no second protocol to fall back to —
every LabOS instrument is a standard SiLA2 server — so this message means the
server itself, not a protocol mismatch.
