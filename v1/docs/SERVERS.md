# LabOS — Server Reference

All four instrument servers are registered by the PnP discovery engine. Opentrons, Tecan, and Manual Station live in `v1/SiLA2/` and are started locally (individually, or together via `launcher.py --all`). The mobile robot (GoFaGo) is hosted and started remotely — see its section at the bottom.

---

## Opentrons SiLA2 Server

**Location**: `v1/SiLA2/OpentronsSiLA2Server/`
**Port**: `50302` (config.yaml `server.port`)
**Feature**: `WorkflowAPI` — 17 commands, 0 properties (status is read via `GetStatus`, not a SiLA2 Property)

### Starting
```bash
cd v1/SiLA2/OpentronsSiLA2Server
python main.py
```
Or via `python launcher.py --all` from `v1/`.

### Commands

**Robot Control**
| Command | Parameters | Description |
|---------|-----------|--------------|
| `Initialize` | — | Verify the connection to the robot is ready |
| `Home` (Observable) | — | Home all robot axes |
| `EmergencyStop` | — | Stop current run, force-drop tips, home. Never silently reports success if the drop/home sequence itself fails — check the response text |
| `GetStatus` | — | Current status + active-run info (JSON) |

**HAL Configuration**
| Command | Parameters | Description |
|---------|-----------|--------------|
| `ListHardwareConfigs` | — | Available HAL configs (JSON array) |
| `LoadHardwareConfig` | `ConfigName` | Switch the active HAL config. Persists to `Library/HardwareConfig/.active` — survives a restart |
| `GetCurrentConfig` | — | Currently active HAL config (JSON) |

**Recipe Execution**
| Command | Parameters | Description |
|---------|-----------|--------------|
| `ListRecipes` | — | Available recipes (JSON array) |
| `ExecuteRecipe` (Observable) | `RecipeName` | Run a recipe against the **currently active** HAL config (HAL is not a per-call parameter — set it once via `LoadHardwareConfig`) |
| `PauseRun` | — | Pause the running protocol |
| `ResumeRun` | — | Resume a paused protocol |
| `AbortRun` | — | Abort; drops tips per `safety.auto_drop_tip_on_abort` |

**Tip Management**
| Command | Parameters | Description |
|---------|-----------|--------------|
| `ListTipRacks` | — | Tip racks + usage counts (JSON array) |
| `RefillTipRack` | `RackSlot` (e.g. `"A1"`) | Mark a rack as freshly refilled |
| `ResetTipTracking` | — | Reset all tip-consumption tracking |

**Monitoring**
| Command | Parameters | Description |
|---------|-----------|--------------|
| `SetLights` | `On` (bool) | Deck lights on/off |
| `GetRobotInfo` | — | Robot metadata (JSON) |
| `GetModulesStatus` | — | Attached modules (heater-shaker, temp module) status (JSON array) |

### Safety features (implemented in `src/server.py`, not exposed as commands)

- **Leftover-tip pre-run guard** — before running a recipe, the server checks its own persisted per-mount tip state (`tip_mount_state.json`) and automatically prepends a forced drop-tip step for any pipette mount it can't confirm is clear. This exists because the installed robot firmware has no real tip-presence sensor. Fully automatic, no UI control, no way to disable except `safety.check_tip_before_run: false` in `config.yaml`.
- **Run archive** — every run is logged to `run_archive/` (JSON, `{summary, commands}`), with automatic compression after `run_archive.retention_uncompressed_days` (default 3) and deletion after `retention_compressed_days` (default 30).
- **Waste comparison** — if the executed recipe carries a waste estimate (from the Recipe Generator's Excel converter), the archive summary includes estimated-vs-actual waste.

### Configuration (`config.yaml`, key sections)

```yaml
server:
  port: 50302
  name: "Opentrons Flex"

robot:
  ip: "10.16.0.137"       # Opentrons robot IP — the most commonly changed value
  port: 31950
  run_timeout: 7200        # seconds; 0 = unlimited (needed for multi-day protocols)

hardware:
  config_folder: "../../Library/HardwareConfig"
  default_config: "Standard_Flex_Setup.json"   # overridden by .active if present

safety:
  check_tip_before_run: true
  auto_drop_tip_on_abort: true
  auto_home_on_error: true

tip_tracking:
  state_file: "./tip_state.json"
  mount_state_file: "./tip_mount_state.json"

run_archive:
  enabled: true
  retention_uncompressed_days: 3
  retention_compressed_days: 30
```

**Known config issue**: `directories.protocols` in `config.yaml` points at a `Protocols/` folder that does not exist on disk. In practice this key is never actually read by the server (`src/config.py` doesn't map it), so it's dead/cosmetic — but don't be alarmed if you notice it; it's not causing failures.

---

## Tecan SiLA2 Server

**Location**: `v1/SiLA2/TecanM200SiLA2Server/`
**Port**: `50051` (bridge internal port: `50055`, not exposed externally)
**Feature**: `PlateReaderService` — 8 commands, 4 properties

The Tecan server requires a Windows host and the C# bridge process (`TecanSiLA2Server.exe`).

### Architecture
```
Python SiLA2 Server (port 50051)
    │ local gRPC
C# Bridge Process (TecanSiLA2Server.exe, port 50055)
    │ .NET SDK
Tecan iControl SDK
    │ USB
Tecan Infinite M200 Pro
```

### Starting
The bridge is started and health-monitored automatically by the Python server (auto-restart with exponential backoff on crash or unresponsiveness). Ensure the exe is built — see [Bridge Binary Is Up to Date](#bridge-binary-is-up-to-date) below.

```bash
cd v1/SiLA2/TecanM200SiLA2Server
python main.py
```

### Commands

| Command | Parameters | Description |
|---------|-----------|--------------|
| `Connect` | `ConnectionString` (empty = default USB, `"sim"` = simulator) | Connect to the reader — always re-validates against real hardware state, never trusts a cached "connected" flag |
| `Disconnect` | — | Disconnect |
| `PlateIn` | — | Move plate carrier in |
| `PlateOut` | — | Eject plate carrier |
| `SetTemperature` | `TargetTemperature` (4.0–45.0 °C) | Set incubation temperature |
| `TurnOffTemperature` | — | Disable temperature control |
| `RunMeasurement` (Observable) | `ProtocolFile`, `PlateID`, `SampleSetID`, `PlateType` | Run a `.mdfx` protocol; streams progress; returns an AnIML result path. Python-side timeout: `bridge.measurement_timeout_seconds` (default 1200s; `0` = unlimited) |
| `ListProtocols` | — | Available `.mdfx` files (JSON array) |

`GetAnIMLResult(PlateID)` also exists for looking up a previously saved result.

### Properties

| Property | Observable | Type |
|----------|-----------|------|
| `IsConnected` | Yes | Boolean |
| `OperationalStatus` | Yes | String (`Idle`/`Busy`/`Error`/`Disconnected`) |
| `CurrentTemperature` | Yes | Real |
| `InstrumentInfo` | No | String (JSON: serial, firmware, simulated flag) |

### MDFX Protocol Files

Measurement protocols are created in Tecan iControl software and saved as `.mdfx` files in `Library/Analysis/`. The Recipe Generator / workflow builder populate their protocol dropdown by scanning this directory.

### Configuration (`config.yaml`)

```yaml
server:
  port: 50051

bridge:
  host: "127.0.0.1"
  port: 50055
  measurement_timeout_seconds: 1200   # 0 = unlimited (the C# bridge itself has no cap)

instrument:
  connection_string: "usb"   # "usb" (default), or "sim"/"simulator" for simulation mode
```

### Bridge binary is up to date

`bridge_launcher.py` prefers the **Release** build of `TecanSiLA2Server.exe` if one exists, falling back to Debug only if Release is absent. The Release build was rebuilt (`dotnet build -c Release`) as part of this documentation pass and now includes the `Connect()` stale-connection-flag fix — every `Connect()` call genuinely re-validates against the hardware instead of trusting a cached flag. **If you rebuild `TecanBridge.cs` again in the future, remember to rebuild Release, not just Debug** — the launcher will silently keep running the old Release binary otherwise (this bit the project once already).

---

## Manual Station SiLA2 Server

**Location**: `v1/SiLA2/ManualStationSiLA2Server/`
**Port**: `50500`
**Feature**: `ManualStation` — 6 commands, 3 properties

A virtual station — no physical hardware. It drives operator notifications and confirmation gates in the web UI.

### Starting
```bash
cd v1/SiLA2/ManualStationSiLA2Server
python main.py
```

### Commands

| Command | Parameters | Description |
|---------|-----------|--------------|
| `RequestOperatorTask` (Observable) | `TaskDescription`, `TaskType` (manual_operation/tip_refill/quality_check/sample_prep/custom), `Priority`, `SourceInstrument`, `TimeoutSeconds` (0 = no timeout) | Pauses the calling workflow and notifies the operator; blocks until confirmed |
| `ConfirmTaskComplete` | `TaskId`, `Notes` | Operator confirms a task — releases the waiting workflow |
| `CancelTask` | `TaskId`, `Reason` | Cancel a pending task |
| `GetActiveTasks` | — | Pending/in-progress tasks (JSON) |
| `GetTaskHistory` | `Limit` | Completed/cancelled tasks (JSON) |
| `SendNotification` | `Message`, `Level` (info/warning/error), `SourceInstrument` | Informational message, no confirmation required |

### Properties

| Property | Observable | Type |
|----------|-----------|------|
| `StationStatus` | Yes | String (`idle`/`waiting_for_operator`/`busy`) |
| `PendingTaskCount` | Yes | Integer |
| `LastNotification` | Yes | String (JSON) |

### Usage in Workflows

Any workflow step whose `Instrument` value contains `"manual"` is intercepted by the workflow executor's manual-step handling — it posts an operator notification and blocks until the Operator page's "Confirm" is clicked or the timeout fires. See [WORKFLOW_SYSTEM.md](WORKFLOW_SYSTEM.md).

---

## Mobile Robot SiLA2 Server

**Location**: hosted remotely — Docker deployment on the Linux host at `192.168.11.22`, **not part of this repository's checkout** (only the client-side bootstrap entry in `lab_config.yaml` lives here)
**Port**: `50053`
**Feature**: `TaskManagement` — task-based execution driving an ABB GoFa arm on a Robotnik RB-Kairos mobile base with an OnRobot RG6 gripper

Confirmed working. Runs entirely on the remote Linux workstation; the Windows orchestrator only connects to it over the LAN (`remote: true` in `lab_config.yaml` — the launcher never tries to start it locally).

### Commands (as last documented — reconfirm against the remote host's FDL if a command appears missing)

| Command | Parameters | Description |
|---------|-----------|--------------|
| `execute_task` | `task_name` | Navigate to a workstation and execute a pre-taught manipulation task |
| `list_tasks` | — | Return all tasks registered on the remote ROS action server |
| `navigate_to` | `location_name` | Send the mobile base to a named map location |
| `get_task_status` | — | Poll current task execution status |
| `stop` | — | Interrupt the current task and return the arm to a safe/home position |

### Task Library

Tasks are stored as JSON descriptors in `Library/MobileTasks/`, refreshed from the remote host's ROS action server (webapp: `POST /api/library/mobile-tasks/refresh`, or automatically at `list_tasks` time). Each task encodes a complete arm trajectory (approach, grasp, transfer, retract) relative to a workstation's ArUco fiducial marker — not the global map frame — decoupling manipulation accuracy from SLAM/navigation accuracy.

Known task names from the shared workflow library (`Library/Workflows/`): station pick/place tasks for Opentrons and Tecan, plus generic storage pick and a `Home`/`Perception` (ArUco re-detection) utility task.

### ArUco Marker Requirements

Each workstation needs an ArUco marker mounted rigidly at a fixed, repeatable position (recommended: aluminium substrate, 100×100mm, `DICT_4X4_50`). The marker ID encodes workstation identity.

### Teaching New Tasks (Learning from Demonstration)

New manipulation tasks are taught by physically guiding the arm, not by programming:

1. Position the robot so the arm camera has a clear, unobstructed view of the target workstation's ArUco marker.
2. On the remote host, open the Exsensia LfD platform and enable compliant mode (the ABB GoFa becomes backdrivable).
3. Physically guide the arm through the full task: approach → grasp → transfer → retract to a safe position. Poses are recorded relative to the ArUco marker frame.
4. Replay and validate with the Exsensia platform; re-record if execution isn't correct.
5. Register the task under a name — it publishes to the ROS action server.
6. Refresh the task library in LabOS (`POST /api/library/mobile-tasks/refresh`) — the new task appears in the Workflow Builder toolbox immediately.

Anticipate the RG6 gripper's forward-push displacement during closure by pre-positioning slightly back before the final grasp. Verify a new task with at least 3 successful replays before using it in a real workflow.
