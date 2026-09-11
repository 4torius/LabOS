# LabOS — Server Reference

All four instrument servers are registered by the PnP discovery engine. Opentrons, Tecan, and Manual Station live in `v1/SiLA2/` and are started locally (individually, or together via `launcher.py --all`). The mobile robot (GoFaGo) is hosted and started remotely — see its section at the bottom.

---

## Opentrons SiLA2 Server

**Location**: `v1/SiLA2/OpentronsSiLA2Server/`
**Port**: `50302` (config.yaml `server.port`)
**Feature**: `WorkflowAPI` v1.1 — 17 commands, 6 properties

### Starting
```bash
cd v1/SiLA2/OpentronsSiLA2Server
python main.py
```
Or `python launcher.py --all` from `v1/`.

### Two rules shape this surface

**Commands do something; readings are Properties.** `GetStatus`,
`GetCurrentConfig`, `GetRobotInfo` and `GetModulesStatus` used to be commands,
so asking "which config is loaded?" appeared in the UI as an operation to run.
They are properties now. `ListRecipes` and `ListHardwareConfigs` are gone
entirely: they asked the robot to list files the web interface already reads
from `Library/` far more cheaply, and can read while the robot is off.
`Initialize` is gone because `start()` already connects with retry.

**A command is one deliberate action.** `MoveLabware`, `ShakeModule`,
`SetModuleLatch` and `SetModuleTemperature` each name one thing an operator
would ask for, validate their own arguments against the loaded deck, and offer
real pickers in the workflow builder. A whole procedure is a recipe, run by
`ExecuteRecipe`.

There was briefly a general `RunSteps` that took a recipe's `Steps` array as a
JSON string parameter. It was removed: the caller had to know the recipe format
by heart, got no validation and no checking against the deck, and ended up with
something harder to use than the recipe file it was meant to avoid.

### Properties (read, never act)

| Property | Observable | Contents |
|----------|-----------|----------|
| `Status` | yes | JSON: `state`, `run_id`, `connected`, `config` |
| `CurrentRunId` | yes | The run in progress, empty when idle |
| `ActiveConfig` | yes | JSON `{name, config}` — the HAL `ExecuteRecipe` runs against |
| `TipsAvailable` | yes | JSON array: per rack, load name, slot, used, remaining |
| `RobotInfo` | no | JSON: name, model, serial, firmware |
| `CameraStatus` | no | JSON: the three camera enablement flags plus the `hls` and `rtmp` stream URLs |
| `ModulesStatus` | no | JSON array of attached modules |

The observable ones are pushed on every state change, so a subscribed client
does not poll. Read one from the CLI with
`python labos.py read opentrons Status`, or over REST at
`/api/devices/opentrons/property/WorkflowAPI/Status`.

### Commands

Descriptions beginning **MOVES HARDWARE** physically move the robot.

**Robot control**

| Command | Parameters | Description |
|---------|-----------|--------------|
| `Home` (obs) | — | MOVES HARDWARE. Home all axes |
| `EmergencyStop` | — | MOVES HARDWARE. Stop the run, force-drop tips, home. Never reports success if the drop/home itself failed — read the response before assuming the deck is safe |
| `SetLights` | `On` | Deck lights |

**Hardware configuration**

| Command | Parameters | Description |
|---------|-----------|--------------|
| `LoadHardwareConfig` | `ConfigName` | Switch the active HAL config. Persists to `Library/HardwareConfig/.active`, so it survives a restart |

**Recipe execution**

| Command | Parameters | Description |
|---------|-----------|--------------|
| `ExecuteRecipe` (obs) | `RecipeName` | MOVES HARDWARE. Run a saved recipe against the active HAL. Fails loudly if the run's own final state is not "succeeded" |
| `ValidateRecipe` | `RecipeName` | Check a recipe against the active HAL without running it. Returns `{valid, errors, warnings}` |
| `PauseRun` | — | Pause the running protocol |
| `ResumeRun` | — | MOVES HARDWARE. Resume |
| `AbortRun` | — | MOVES HARDWARE. Abort; drops tips per `safety.auto_drop_tip_on_abort` |

**Deck and modules**

| Command | Parameters | Description |
|---------|-----------|--------------|
| `MoveLabware` (obs) | `LabwareName`, `NewLocation`, `UseGripper` | MOVES HARDWARE. Move labware with the gripper — see below |
| `SetModuleTemperature` (obs) | `ModuleName`, `Celsius`, `WaitForTemperature` | MOVES HARDWARE. `Celsius: 0` deactivates heating |
| `ShakeModule` (obs) | `ModuleName`, `RPM`, `DurationSeconds` | MOVES HARDWARE. Latch must be closed; `DurationSeconds: 0` stops |
| `SetModuleLatch` | `ModuleName`, `Open` | MOVES HARDWARE. Closed before shaking, open before the gripper touches the plate |

**Tips**

| Command | Parameters | Description |
|---------|-----------|--------------|
| `RefillTipRack` | `RackSlot` (e.g. `"A1"`, or `"all"`) | Record that a rack was physically refilled |
| `ResetTipTracking` | — | Reset every counter to full |

**Camera**

| Command | Parameters | Description |
|---------|-----------|--------------|
| `SetCameraEnabled` | `Camera`, `LiveStream` | Turn the robot's camera and live stream on or off. Nothing moves; the setting lives on the robot |
| `CaptureImage` (obs) | `Label`, `AllowRobotMotion` | Photograph the deck — see below |

### `MoveLabware`: what it offers, and what it records

Both pickers come from the **active hardware config**, not from a list typed
into the UI:

- **Labware Name** — the labware that config declares, shown as what it is and
  where it currently sits (`TargetPlate — Target plate (slot D2)`). Modules and
  the trash bin are excluded: they are bolted to the deck.
- **New Location** — the slots nothing is standing in, including the column-4
  staging area, plus any module on the deck. `move_labware()` accepts a module
  as a destination, and the generated protocol resolves it by its logical name.

The move is checked **before the gripper starts**: unknown labware, or a
destination something else already occupies, is refused with the list of what
is actually available. Both used to fail deep inside the generated protocol,
after the run had begun and the robot had homed.

**After a successful move the hardware config is rewritten** with the new
position. Without that it lies from the moment a plate moves — it still claims
the labware is in the slot it started in, so the next recipe generates
`load_labware(..., "D2")` for a plate that is now somewhere else, and the deck
the operator sees stops matching the deck the software believes in. A
destination that is a module is recorded as `OnModule`, which the protocol
generator already understands (it loads such labware with
`module.load_labware()`). If the move succeeds but the config cannot be
updated, the response says so explicitly rather than reporting a clean success
over a config that is now wrong.

### `CaptureImage`: the on-board camera

The Flex has a camera, and the capture machinery has existed for a long time: a
`TakeSnapshot` recipe step grabs a frame with `gst-launch` on the robot and
base64-encodes it into the run log, and the server decodes it back out after
every run. What was missing was any way to ask for it, and anywhere to put the
result — images landed in the server's own `./images` directory, which nothing
served.

Now:

- Images are written to `Results/Images/` and served by the web interface.
  **Results → Deck photos** shows the gallery.
- Two acquisition paths. First the robot's own HTTP camera endpoint: nothing
  moves. If that is unavailable, capture falls back to running a one-step
  protocol — **and starting a protocol homes the robot**, so the fallback is
  refused unless you pass `AllowRobotMotion: true`. Leave it false unless
  someone is watching the deck.
- The response is JSON `{saved, path, url, method, detail}` and always says
  which path ran and why the other did not.

Configure it in `config.yaml`:

```yaml
camera:
  enabled: true
  device: "/dev/video2"   # the V4L2 node on the robot
  http_probe: true        # try the no-motion path first
```

If capture produces no frame, `device` is the value to check: list the robot's
video devices over SSH with `ls /dev/video*`. That only matters for the
protocol path; the HTTP path uses the robot's own camera service and needs no
device node.

### The camera has to be switched on, on the robot

Verified on the bench: the HTTP path works and returns a 1280x720 JPEG of the
deck without the robot moving at all.

It looked broken for two sessions because of a setting, not a fault. The Flex
firmware has three camera flags and **all three ship off**. With them off,
`POST /camera/picture` answers `422 Cannot take photo, camera is disabled`.

Read them with the `CameraStatus` property, set them with `SetCameraEnabled`:

```
SetCameraEnabled(Camera=true, LiveStream=false)
```

The setting lives on the robot, not on this server, and survives a restart of
either. `Camera` and `LiveStream` are deliberately separate parameters:
enabling the stream broadcasts the lab over the network, which should not ride
along with permission to take one photograph. The error-recovery camera flag is
left alone - whether the robot films its own error recovery is a third,
separate decision.

### Video: no

The Flex advertises a live stream - `CameraStatus` reports an HLS and an RTMP
URL, and the firmware has a whole service for it. It does not work on this
robot. Enabling it restarts the live-stream service cleanly, port 1935 listens,
the robot's own logs show no error, and no playlist or segment is ever
published. Checked with the camera completely idle, in case the stills endpoint
was holding the device. It is not.

Single frames come back at about one every two seconds, so there is no way to
build a video out of them either. A `CaptureTimelapse` command existed briefly
for that and was removed: a sped-up slideshow is not what anyone wanted.

The stream URLs are still reported, because reporting what the robot claims
costs nothing and is how anyone would notice if a firmware update fixed it.

So: **one photograph, on demand.** `CaptureImage` switches the camera on, takes
it, and switches the camera back to however it was found.

### Safety features (in `src/server.py`, not exposed as commands)

- **Leftover-tip pre-run guard** — the installed firmware has no tip-presence
  sensor, so the server tracks per-mount "might still have a tip" state
  (`tip_mount_state.json`) and prepends a forced drop-tip for any mount it
  cannot confirm is clear. Automatic; disable only with
  `safety.check_tip_before_run: false`.
- **Run archive** — every run logged to `run_archive/` as JSON, gzipped after
  `retention_uncompressed_days` (3) and deleted after
  `retention_compressed_days` (30).
- **Waste comparison** — if the recipe carries a waste estimate from the Recipe
  Generator, the archive summary compares it to the actual aspirate/dispense
  delta.

### Configuration (`config.yaml`, key sections)

```yaml
server:
  port: 50302

robot:
  ip: "169.254.161.83"     # link-local; self-heals via mDNS if it moves
  port: 31950
  run_timeout: 7200        # seconds; 0 = unlimited, for multi-day protocols

hardware:
  config_folder: "../../Library/HardwareConfig"
  default_config: "Standard_Flex_Setup.json"   # overridden by .active

safety:
  check_tip_before_run: true
  auto_drop_tip_on_abort: true
  auto_home_on_error: true

camera:
  enabled: true
  device: "/dev/video2"
  http_probe: true

run_archive:
  enabled: true
  retention_uncompressed_days: 3
  retention_compressed_days: 30
```

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
| `RunMeasurement` (Observable) | `ProtocolFile`, `PlateID`, `SampleSetID`, `PlateType` | Run a `.mdfx` protocol; streams progress; returns an AnIML result path. Python-side timeout: `bridge.measurement_timeout_seconds` (default 1200s; `0` = unlimited). `PlateType` is optional — see [MDFX Protocol Files](#mdfx-protocol-files) below for what it does |
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

Measurement protocols are created in Tecan iControl software (Reader Editor) and saved as `.mdfx` files in `Library/Analysis/`. The Recipe Generator / workflow builder / Database page's "Add analysis" button all populate their protocol dropdown by scanning this directory.

A `.mdfx` file hardcodes which physical plate it was created for as two coupled XML attributes:

```xml
<CyclePlate id="3" file="COS24ft" plateWithCover="False">
    <PlateRange id="0" range="A1~D6" auto="true">
```

`file` is a plate code from Tecan's internal library (not extractable from the installed DLLs as plain data — read it out of a real `.mdfx` instead, see below); `range` is the full well range for that plate and must always match `file` (e.g. a 96-well plate needs `A1:H12`, not `A1~D6`).

**`PlateType` override**: if you pass a non-empty `PlateType` to `RunMeasurement`, `TecanBridge.RewritePlateType()` looks it up in `Library/Analysis/PlateTypes.json` and rewrites both attributes in memory before the SDK loads the protocol — the `.mdfx` file on disk is never modified. Leave `PlateType` empty (the default from every UI) to use whatever plate the `.mdfx` was originally created for; this is a fully backward-compatible no-op. If the requested key isn't in `PlateTypes.json`, the measurement fails immediately with a clear error listing the known keys — it never silently falls back to the `.mdfx` default, which would look like success while reading the wrong geometry.

**To add support for a new plate type**:
1. In Tecan's Reader Editor (iControl), create or edit any `.mdfx` protocol and pick the new plate from its own plate picker, save it.
2. Open that `.mdfx` in a text editor and copy the two values: `file="..."` from `<CyclePlate>` and `range="..."` from `<PlateRange>`.
3. Add an entry to `Library/Analysis/PlateTypes.json`:
   ```json
   "your_plate_key": { "file": "COPIED_FILE_CODE", "range": "COPIED_RANGE" }
   ```
   Keys starting with `_` are treated as comments/metadata and ignored — use them for notes. Pick a key name that means something to your lab; matching an existing Opentrons `Library/Labware/Plates/*.json` `id` (e.g. `corning_24_wellplate_3.4ml_flat`) is a good convention when the same physical plate is used on both instruments, but it's just a string key, nothing enforces the match.
4. **Restart the Tecan server** (`main.py`, which relaunches the bridge) — the library is loaded once at bridge startup, not re-read per request. The startup log prints `[PlateTypeLibrary] Caricate N definizioni piastra da ...` confirming what was loaded.

If `PlateTypes.json` doesn't exist or is empty, `PlateType` is a documented no-op everywhere — nothing breaks, you just can't override the plate yet.

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

### When `Connect` fails

Two facts about how this connects explain nearly every failure, and neither is
about power or cabling:

- **The SDK enumerates USB devices when the *bridge process* starts**, not when
  you press Connect. A bridge that came up while iControl held the instrument,
  or before it finished powering up, will never see it — retrying cannot help,
  only a fresh bridge process can.
- **It connects using a stored "last connection".** Move the reader to a
  different USB port and that record is stale: the instrument is present,
  iControl still connects to it, and this does not. Windows keeps a separate
  device instance per physical port, so the old one becomes a ghost
  (`Get-PnpDevice | ? InstanceId -match 'VID_0C47'` shows them, with `Present`
  telling you which is real).

**The fix for a moved reader** is to force a fresh enumeration once:

```
Connect(ConnectionString = "dialog")
```

That opens the SDK's instrument-selection window on the lab PC. Pick the
reader; the selection is recorded as the new last connection, and plain `usb`
works again afterwards. `ConnectionString` is a dropdown in the interface —
`usb`, `dialog`, `last`, `sim`, `any`.

#### What `ConnectionString` actually selects

Each keyword picks a different SDK connection *method*, not just a different
string. They used to all funnel through `LastConnection` with a token appended,
which is why `dialog` and `last` behaved alike.

| Value | Methods tried, in order | What it does |
|---|---|---|
| *(empty)* | — | Uses `instrument.connection_string` from config.yaml |
| `usb` | `LastConnection` → `Manually` | The stored connection; if it matched nothing, a fresh USB scan. Both silent, so it is **safe unattended**, and it recovers a moved reader on its own |
| `ask` | `LastConnection` → `Manually` → `UserDialog` | As `usb`, and if the reader still is not found, the selection window. **Needs somebody at the machine** — the call waits until the window is answered |
| `dialog` | `UserDialog` | The selection window straight away |
| `last` | `LastConnection` | The stored connection only, no fallback |
| `sim` | `Manually`, `porttype=SIM` | The Tecan simulator |
| `any` | `Manually`, `porttype=USB\|SIM` | Whichever answers first — may pick the simulator |

**No default chain can open a window.** A modal dialog on the lab PC does not
fail: it waits, and a workflow running overnight waits with it. Escalating to
the picker is opt-in, which is what `ask` is for.

Does `LastConnection` not already show the picker by itself? It appears to,
when *several* instruments match — but not when none does, which is the case a
stale stored port produces. There is nothing to choose between, so it simply
returns nothing. That is why the escalation is explicit rather than relied
upon.

The empty string is deliberately still valid: it is the documented default, and
it is what `Simple_PlateRead.workflow.json` sends. That is why this parameter is
*not* a SiLA2 Constrained/Set — a Set is enforced before the server sees the
call and would reject it. The dropdown in the interface comes from the server's
`library_catalog.json` instead.

#### Older guidance

The bridge reports `ConnectionFailed`, and the server adds what that actually
means, in the order it is usually true:

1. **iControl (or Magellan) is open, or was opened since the server started.**
   It takes exclusive ownership of the USB connection and does not reliably
   release it on close. This is by far the most common cause. Close it, then
   restart the Tecan server so the bridge re-enumerates.
2. **The bridge started before the instrument finished powering up.** The SDK
   enumerates USB devices when it connects; restarting the Tecan server is
   enough.
3. The instrument is genuinely off, unplugged, or on a USB port Windows has
   suspended.

A useful distinction when reading the error: if the *server* answered the call
at all — the dashboard shows it online, its properties read — then the SiLA2
side is fine and the failure is strictly between the bridge and the instrument.
`IsConnected: false` with `OperationalStatus: Disconnected` is exactly that
state.

### Bridge binary is up to date

`bridge_launcher.py` prefers the **Release** build of `TecanSiLA2Server.exe` if one exists, falling back to Debug only if Release is absent. The Release build was rebuilt (`dotnet build -c Release`) as part of this documentation pass and now includes the `Connect()` stale-connection-flag fix — every `Connect()` call genuinely re-validates against the hardware instead of trusting a cached flag. It was rebuilt again on 2026-09-01 to add the `PlateType` override (see [MDFX Protocol Files](#mdfx-protocol-files)), verified against the real instrument (S/N 2511004584). **If you rebuild `TecanBridge.cs` again in the future, remember to rebuild Release, not just Debug** — the launcher will silently keep running the old Release binary otherwise (this bit the project twice already, including during the `PlateType` work).

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

**Location**: hosted remotely — Docker deployment on the Linux host at `192.168.11.22` (`mobile-sila2` container, image `mobile-sila2-server`, `restart: always`), **not part of this repository's checkout**. Verified live and running at the time of this documentation pass.
**Port**: `50053`
**Feature**: `TaskManagement` — 9 commands, 6 properties. Confirmed directly from the server's own FDL (`features/TaskManagement.sila.xml` on the remote host) — this is the authoritative command list, not carried forward from an older draft.

Like the local servers, it's built with the `sila2` library (self-describes via `SiLAService`, supports mDNS discovery, and implements `LabOSLibraryProvider` — declares `mobile_task` as a dynamic library type backed by `Library/MobileTasks/`). It runs a background reconnect loop against the ROS master: if ROS is down at startup it retries every interval without crashing, and if `roscore` itself gets restarted underneath it, the server deliberately exits (`SIGTERM`) so Docker's `restart: always` brings it back up with a clean ROS session, rather than trying to recover an unrecoverable `rospy` state in-process.

### Properties

| Property | Observable | Type | Description |
|----------|-----------|------|--------------|
| `AvailableTasks` | No | List of `TaskInfo` (`TaskId`, `TaskName`, `SubtaskCount`) | Discovered from the robot controller via ROS service; refreshed by `RefreshTasks` |
| `AvailableHardware` | No | List of `HardwareInfo` (`HardwareId`, `HardwareName`, `Brand`, `Model`, `Activated`) | Queried live from the robot controller on every read — not cached |
| `CurrentTaskId` | Yes | String | Empty when idle |
| `CurrentSubtask` | Yes | String | Name of the subtask currently executing |
| `TaskStatus` | Yes | String (`Idle`/`Executing`/`Paused`/`Completed`/`Failed`/`Cancelled`) | |
| `RobotConnectionStatus` | Yes | String (`Connected`/`Disconnected`/`Connecting`/`Error`) | Reflects the ROS connection, updated by the background reconnect loop |

### Commands

| Command | Parameters | Description |
|---------|-----------|--------------|
| `RefreshTasks` | — | Re-query the robot controller for available tasks; updates `AvailableTasks` |
| `ExecuteTask` (Observable) | `TaskId`, `ExecutionMode` (`Normal`/`StepByStep`/`DryRun`) | Runs a task via the ROS action server; streams `CurrentSubtask`/`Progress`/`Status` as intermediate responses. **`ExecutionMode` is declared in the FDL but the current server ignores it** — `DryRun` does not simulate. Leave it on `Normal` |
| `CancelTask` | — | Cancel the currently executing task |
| `GetTaskDetails` | `TaskId` | Full task info including all subtasks (JSON) |
| `ConnectToRobot` | `RosMasterUri` (empty = config default) | Explicitly (re)establish the ROS connection. **Physically consequential**: initializes the ROS node against the action server |
| `SetHardwareActivation` | `HardwareId`, `Activate` (bool) | Activate/deactivate one piece of hardware by its raw controller ID. **The FDL itself flags this as physically consequential — may engage motors/servos** |
| `SetHardwareActivationByRole` | `Hardware` (`Arm`/`Gripper`/`Camera`/`MobileBase`), `Activate` | Same, but by role — resolved to the real hardware ID live, so the caller never needs to know it |
| `ActivateAllHardware` | — | Activates every registered hardware item one at a time. **Physically consequential** |
| `DeactivateAllHardware` | — | Deactivates every registered hardware item |

Defined execution errors: `TaskNotFound`, `TaskExecutionFailed`, `RobotNotConnected`, `HardwareNotFound`.

**Note for anyone updating LabOS workflow examples**: earlier drafts of this documentation described a different, simpler command set (`execute_task`/`list_tasks`/`navigate_to`/`get_task_status`/`stop`) — that was stale. The commands above are what the server actually implements today.

### Hardware Registered on the Controller

From the current `config.yaml`, the robot controller manages: an ABB GoFa arm, an OnRobot gripper, a camera, and the Robotnik mobile base — each independently activatable via the hardware-activation commands above (`Arm`/`Gripper`/`Camera`/`MobileBase` roles).

### Registered Stations & Gripper Profiles (from the remote `config.yaml`)

**Do not trust the station list that used to be written here.** An earlier
documentation pass copied `Home`, `Opentrons`, `Tecan`, `Storage_A1`,
`Storage_A2` and `Incubator` out of the remote `config.yaml`; the lab does not
recognise the last three, and the host was unreachable when this was checked, so
the list could not be confirmed either way. The authority on what stations exist
is the robot: call `RefreshTasks` and read `AvailableTasks`, or read
`config.yaml` on the remote host directly.

Gripper labware profiles (width / grip force / grip offset) are pre-defined for `96_well_plate`, `384_well_plate`, `reservoir`, and `tube_rack` — the server selects grip parameters based on what's being handled.

### Safety Limits (from `config.yaml`)

`min_battery: 20%` (minimum charge to operate), `obstacle_distance: 0.3m` (stops if something closer), `max_velocity: 0.5 m/s` (base), `arm_max_velocity: 0.3 m/s` (end-effector).

### Task Library

Tasks are stored as JSON descriptors in `Library/MobileTasks/`, refreshed from the remote host's ROS action server (webapp: `POST /api/library/mobile-tasks/refresh`, or automatically via `RefreshTasks`). Each task encodes a complete arm trajectory (approach, grasp, transfer, retract) relative to a workstation's ArUco fiducial marker — not the global map frame — decoupling manipulation accuracy from SLAM/navigation accuracy.

### ArUco Marker Requirements

Each workstation needs an ArUco marker mounted rigidly at a fixed, repeatable position (recommended: aluminium substrate, 100×100mm, `DICT_4X4_50`). The marker ID encodes workstation identity.

### Teaching New Tasks (Learning from Demonstration)

New manipulation tasks are taught by physically guiding the arm, not by programming:

1. Position the robot so the arm camera has a clear, unobstructed view of the target workstation's ArUco marker.
2. On the remote host, open the Exsensia LfD platform and enable compliant mode (the ABB GoFa becomes backdrivable).
3. Physically guide the arm through the full task: approach → grasp → transfer → retract to a safe position. Poses are recorded relative to the ArUco marker frame.
4. Replay and validate with the Exsensia platform; re-record if execution isn't correct.
5. Register the task under a name — it publishes to the ROS action server.
6. Call `RefreshTasks` (or `POST /api/library/mobile-tasks/refresh` from the webapp) — the new task appears in the Workflow Builder toolbox immediately.

Anticipate the gripper's forward-push displacement during closure by pre-positioning slightly back before the final grasp. Verify a new task with at least 3 successful replays before using it in a real workflow.
