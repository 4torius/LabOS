# LabOS — Server Reference

All servers live in `v1/SiLA2/`. Each is started independently and registered by the PnP discovery engine.

---

## Opentrons SiLA2 Server

**Location**: `v1/SiLA2/OpentronsSiLA2Server/`  
**Port**: 50052  
**Feature**: `WorkflowAPI`

### Starting
```bash
python v1/SiLA2/OpentronsSiLA2Server/main.py
```
Or via `v1/START.bat` (starts all servers together).

### Commands

| Command | Parameters | Description |
|---------|-----------|-------------|
| `ExecuteRecipe` | `recipe` (str), `hal_config` (str) | Translate recipe + HAL config to Opentrons protocol and run it |
| `LoadProtocol` | `protocol_path` (str) | Upload a pre-generated Python protocol directly |
| `RunProtocol` | — | Execute the currently loaded protocol |
| `GetStatus` | — | Returns `idle`, `running`, or `error` |
| `GetDeckConfiguration` | — | Returns current deck layout |
| `HomeRobot` | — | Move all axes to home position |
| `Pause` | — | Pause a running protocol |
| `Resume` | — | Resume a paused protocol |
| `Stop` | — | Stop protocol execution |
| `GetTipState` | `hal_config` (str) | Return current tip consumption state |
| `ResetTipState` | `hal_config` (str) | Mark all tips as fresh |
| `ListRecipes` | — | Return available recipes in Library/Recipes/ |
| `ListHALConfigs` | — | Return available HAL configs in Library/HardwareConfig/ |
| `ValidateRecipeHAL` | `recipe` (str), `hal_config` (str) | Check recipe/HAL consistency without running |
| `GetTemperatureModule` | `slot` (str) | Read temperature module in the given slot |
| `SetTemperatureModule` | `slot` (str), `temperature` (float) | Set temperature module target |
| `GetHeaterShaker` | `slot` (str) | Read heater-shaker state |
| `SetHeaterShaker` | `slot` (str), `rpm` (int), `temperature` (float) | Configure heater-shaker |

### Properties

| Property | Observable | Type | Description |
|----------|-----------|------|-------------|
| `RobotStatus` | Yes | String (enum) | Current robot state |
| `DeckLayout` | No | String (JSON) | Current deck slot assignments |
| `ActiveProtocol` | Yes | String | Name of currently loaded protocol |

### Configuration (`config.yaml`)

```yaml
server:
  port: 50052
  name: "opentrons"

hardware:
  robot_ip: "169.254.69.185"   # Opentrons robot IP (USB connection)
  robot_port: 31950
```

---

## Tecan SiLA2 Server

**Location**: `v1/SiLA2/TecanSiLA2Server/`  
**Port**: 50051  
**Feature**: `PlateReaderService`

The Tecan server requires a Windows host and the C# bridge process.

### Architecture
```
Python SiLA2 Server (port 50051)
    │ named pipe / local gRPC
C# Bridge Process (TecanBridge.exe)
    │ .NET SDK
Tecan iControl SDK
    │ USB
Tecan Infinite M200 Pro
```

### Starting
The bridge is started automatically by the Python server. Ensure `TecanBridge.exe` is in PATH or configure its path in `config.yaml`.

### Commands

| Command | Parameters | Description |
|---------|-----------|-------------|
| `RunMeasurement` | `protocol` (str), `output_file` (str) | Load MDFX protocol and execute measurement |
| `SetTemperature` | `temperature` (float) | Set incubation temperature (°C) |
| `GetTemperature` | — | Read current plate temperature |
| `Shake` | `rpm` (int), `duration` (int) | Shake plate for N seconds |
| `PlateIn` | — | Move motorized carrier to loading position |
| `PlateOut` | — | Move motorized carrier to eject position |
| `ListProtocols` | — | Return available .mdfx files in Library/Analysis/ |
| `ExportAnIML` | `result_file` (str), `animl_file` (str) | Convert Excel result to AnIML XML |

### Properties

| Property | Observable | Type |
|----------|-----------|------|
| `ReaderStatus` | Yes | String (enum: idle/running/error) |
| `CurrentTemperature` | Yes | Float |

### MDFX Protocol Files

Measurement protocols are created in Tecan iControl software and saved as `.mdfx` files. Store them in `Library/Analysis/` using this naming convention:

```
Library/Analysis/
├── Absorbance/
│   ├── abs_260nm.mdfx
│   ├── abs_280nm.mdfx
│   └── abs_600nm_OD.mdfx
├── Fluorescence/
│   ├── GFP_ex485_em535.mdfx
│   └── mCherry_ex587_em610.mdfx
└── Kinetic/
    └── growth_curve_OD600.mdfx
```

The visual designer populates the protocol dropdown by scanning this directory.

### Configuration (`config.yaml`)

```yaml
server:
  port: 50051
  name: "tecan"

hardware:
  bridge_executable: "TecanBridge.exe"
  bridge_port: 50050        # local bridge port (not exposed externally)
  tecan_connection: "usb"   # "usb" or "network"
```

---

## Mobile Robot SiLA2 Server

**Location**: `v1/SiLA2/MobileSiLA2Server/` or `ext/MobileSiLA2Server/`  
**Port**: 50053  
**Feature**: `TaskManagement`

Runs on the **Linux workstation** (not the Windows orchestrator computer). The Python orchestrator connects to it over the LAN.

### Starting
```bash
# On the Linux robotics workstation, with ROS sourced:
source /opt/ros/noetic/setup.bash
python v1/SiLA2/MobileSiLA2Server/main.py
```

### Commands

| Command | Parameters | Description |
|---------|-----------|-------------|
| `execute_task` | `task_name` (str) | Navigate to workstation and execute pre-taught manipulation task |
| `list_tasks` | — | Return all tasks registered in the ROS action server |
| `navigate_to` | `location_name` (str) | Send the mobile base to a named map location |
| `get_task_status` | — | Poll current task execution status |
| `stop` | — | Interrupt the current task and return arm to home position |

### Task Library

Tasks are stored as JSON descriptors in `Library/MobileTasks/`. The system populates this folder automatically when `list_tasks` is called, reading from the ROS action server.

Each task encodes the complete arm trajectory (approach, grasp, transfer, retract) relative to the workstation's ArUco fiducial marker frame — not the global map frame. This decouples manipulation accuracy from SLAM navigation accuracy.

**Available tasks in the current configuration:**

| Task | Station | Subtasks | Notes |
|------|---------|---------|-------|
| `PickFromOT3` | Opentrons | 5 | Picks from deck slot |
| `PlaceOnOT3` | Opentrons | 6 | Places onto deck slot |
| `PickFromTECAN` | Tecan | 5 | Retrieves from carrier |
| `PlaceOnTecan` | Tecan | 4 | Inserts into carrier |
| `PickPlate` | Storage | 4 | General storage pick |
| `Perception` | Any | 1 | ArUco detection + reference frame update |
| `Home` | Any | 1 | Return arm to home position |

### ArUco Marker Requirements

Each workstation requires an ArUco marker (recommended: aluminium substrate, 100mm × 100mm, dictionary DICT_4X4_50) mounted rigidly at a fixed, repeatable position. The marker ID encodes the workstation identity.

### Configuration (`config.yaml`)

```yaml
server:
  port: 50053
  name: "mobile_robot"

ros:
  master_uri: "http://localhost:11311"
  action_server: "/robot_tasks"
  nav_goal_topic: "/move_base_simple/goal"

hardware:
  camera_topic: "/realsense/color/image_raw"
  aruco_dictionary: "DICT_4X4_50"
```

---

## Manual Station SiLA2 Server

**Location**: `v1/SiLA2/ManualStationSiLA2Server/`  
**Port**: 50054  
**Feature**: `ManualStation`

### Starting
```bash
python v1/SiLA2/ManualStationSiLA2Server/main.py
```

### Commands

| Command | Parameters | Description |
|---------|-----------|-------------|
| `RequestOperatorTask` | `task_description` (str), `timeout_minutes` (int) | Pause workflow; display task to operator in web UI |
| `ConfirmCompletion` | — | Resume workflow after operator confirms |
| `RequestSampleInput` | `prompt` (str) | Request the operator to provide a text input |
| `NotifyOperator` | `message` (str), `level` (str) | Display an informational message (no pause required) |
| `GetStationStatus` | — | Returns `idle` or `waiting_confirmation` |

### Properties

| Property | Observable | Type |
|----------|-----------|------|
| `StationStatus` | Yes | String (enum) |
| `PendingTask` | Yes | String |
| `WaitingTimeSeconds` | Yes | Integer |

### Usage in Workflows

The manual station is invoked like any other instrument in a workflow step:

```json
{
  "StepID": 5,
  "Instrument": "manual_station",
  "Action": "RequestOperatorTask",
  "Parameters": {
    "task_description": "Replenish tip rack in slot A1 and add reagent to reservoir. Click Confirm when ready.",
    "timeout_minutes": 60
  },
  "DependsOn": [4]
}
```

The web interface shows a prominent notification with the task description and a "Confirm" button. A countdown timer is displayed if `timeout_minutes` is set.
