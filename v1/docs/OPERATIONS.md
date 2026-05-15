# LabOS — Operations Guide

## First-Time Setup

### Prerequisites

**Windows orchestrator computer:**
- Python 3.10+
- Required packages: `pip install -r v1/requirements.txt`
- Tecan iControl software installed (for SDK access)
- TecanBridge.exe compiled and accessible in PATH

**Linux robotics computer:**
- ROS Noetic installed and sourced
- Python 3.8+
- Exsensia Robotics platform installed and configured
- Robotnik RB-Kairos driver package installed

### Network Configuration

All components communicate over the laboratory LAN:

| Component | IP | Port |
|-----------|-----|------|
| Orchestrator (Windows) | `192.168.x.10` | 8000 (web), 50054 (manual station) |
| Opentrons Flex | `169.254.69.185` | 31950 (HTTP API) |
| Opentrons SiLA2 Server | `192.168.x.10` | 50052 |
| Tecan SiLA2 Server | `192.168.x.10` | 50051 |
| Mobile SiLA2 Server (Linux) | `192.168.x.20` | 50053 |
| Manual Station Server | `192.168.x.10` | 50054 |

Update `v1/SiLA2/servers_config.yaml` with the actual IP addresses of your deployment.

### Server Configuration

Edit `v1/SiLA2/servers_config.yaml`:

```yaml
servers:
  - name: "opentrons"
    host: "localhost"
    port: 50052
  - name: "tecan"
    host: "localhost"
    port: 50051
  - name: "mobile_robot"
    host: "192.168.1.20"   # Linux computer IP
    port: 50053
  - name: "manual_station"
    host: "localhost"
    port: 50054
```

---

## Starting the System

### Option A: START.bat (Windows, all servers)

```
v1/START.bat
```

This script starts all Windows-hosted servers and the orchestrator in sequence. Check the console windows for any startup errors.

### Option B: Manual start (for debugging)

Open separate terminal windows for each component:

**Terminal 1 — Tecan server**
```bash
cd v1/SiLA2/TecanSiLA2Server
python main.py
```

**Terminal 2 — Opentrons server**
```bash
cd v1/SiLA2/OpentronsSiLA2Server
python main.py
```

**Terminal 3 — Manual station server**
```bash
cd v1/SiLA2/ManualStationSiLA2Server
python main.py
```

**Terminal 4 (Linux) — Mobile robot server**
```bash
source /opt/ros/noetic/setup.bash
cd v1/SiLA2/MobileSiLA2Server
python main.py
```

**Terminal 5 — Orchestrator (start last)**
```bash
cd v1
python src/lab_core.py
```

Open the web interface at **http://localhost:8000**.

---

## Commissioning Checklist

Run this checklist before the first workflow execution in a new environment.

### Hardware
- [ ] Opentrons Flex powered on, connected via USB, IP `169.254.69.185` reachable
- [ ] Tecan M200 Pro powered on and USB-connected to the Windows computer
- [ ] GoFaGo powered on: base booted (Ubuntu), arm powered, gripper cable connected
- [ ] All ArUco markers mounted rigidly at their workstation positions (slots A, B, C)
- [ ] Deck loaded per the selected HAL config (tip racks, labware in correct slots)

### Software
- [ ] All four SiLA2 servers started without errors
- [ ] Dashboard shows all instruments as "online" (green dots)
- [ ] `GET /api/instruments` returns all four servers
- [ ] Opentrons: run `GET /api/instruments` and verify `RobotStatus = idle`
- [ ] Tecan: run `RunMeasurement` with a short test protocol and verify Excel output
- [ ] Mobile robot: run `list_tasks` and verify the 7 expected tasks appear
- [ ] Manual station: run a manual intervention test workflow and confirm it pauses/resumes correctly

### HAL Validation
- [ ] Select the HAL config matching the current deck layout
- [ ] Run `POST /api/recipes/validate` with a test recipe — verify no errors
- [ ] Run a short `Opentrons_Only_Test.workflow.json` with a scrap plate

---

## Day-to-Day Operations

### Before Each Experiment

1. Verify all instruments show "online" in the dashboard
2. Confirm tip racks are loaded correctly for the selected HAL config
3. Confirm reagents are loaded in the correct reservoirs
4. If using the mobile robot, confirm the ArUco markers are clean and undamaged

### Running a Workflow

1. Open the web interface at http://localhost:8000
2. Go to **Workflow Builder**, select or create a workflow
3. Click **Run** — the executor validates the workflow before dispatching any commands
4. Monitor progress in the dashboard's real-time step tracker
5. For manual steps: a notification appears with the task description; click **Confirm** when done
6. Download results from the **Results** tab when complete

### After Each Experiment

1. Home the GoFaGo arm if it was used (run the `Home` task from the dashboard or a manual command)
2. Clear completed plates from the deck
3. Note tip state — the system tracks consumption automatically, but verify the displayed count is correct

---

## Troubleshooting

### Server shows "offline" in the dashboard

1. Check if the server process is still running (look for error in its terminal window)
2. Check port availability: `netstat -an | grep 5005`
3. Check the server's `config.yaml` for correct host/port
4. Try restarting the server; the PnP discovery engine reconnects automatically within ~10 seconds

### Opentrons: "Could not connect to robot"

1. Verify the robot is powered and the USB cable is connected
2. Verify the robot IP: `ping 169.254.69.185`
3. Check the Opentrons app on the touchscreen — if it shows an error, restart the robot
4. Check `config.yaml` for correct `robot_ip`

### Tecan: "Bridge process not responding"

1. Look for `TecanBridge.exe` in Task Manager — if absent, the bridge crashed
2. The Python server will attempt to restart the bridge automatically; wait 15 seconds
3. If repeated failures: check the Tecan iControl software is not open (it locks the USB connection)
4. Verify the Tecan USB driver is installed: Device Manager should show "Tecan USB Device"

### Mobile robot: "ROS action server not reachable"

1. Verify the Linux workstation is powered and ROS is running: `rostopic list` should not hang
2. Verify the Mobile SiLA2 Server is running on the Linux machine: `ps aux | grep main.py`
3. Check the IP in `config.yaml` matches the current Linux machine IP
4. If the ROS master restarted, the action server may need a restart: `roslaunch robot_tasks robot_tasks.launch`

### Workflow fails at a robot step

Most robot task failures are physical (positioning or grasp), not software. Check:
1. Dashboard error detail for the failed step
2. ArUco marker visibility — is it lit, unobstructed, not deformed?
3. Plate position on the deck — was it placed correctly by the previous step?
4. Re-run the failing step individually from the Manual Robot Control panel
5. If the failure is repeatable, the task may need to be re-taught (see Teaching New Tasks below)

### Workflow validation error: "Missing labware mapping"

The recipe references a logical labware name that is not defined in the selected HAL config.
1. Open the HAL config file from `Library/HardwareConfig/`
2. Add the missing labware entry (see [HAL_SYSTEM.md](HAL_SYSTEM.md))
3. Re-run validation

### Tip count mismatch

If the tip state file is out of sync with reality:
1. Run `POST /api/instruments` → `opentrons` → `ResetTipState` to reset to full racks
2. Physically verify all tip racks are full before resetting

---

## Teaching New Robot Tasks (LfD)

New manipulation tasks are taught using the Exsensia Robotics LfD platform:

1. **Position the robot** at the target workstation. The arm camera must have a clear, unobstructed view of the workstation's ArUco marker.
2. **Open the Exsensia app** on the Linux workstation.
3. **Enable compliant mode** — the ABB GoFa arm becomes backdrivable.
4. **Physically guide the arm** through the complete task: approach object → close gripper → perform manipulation → open gripper → retract to safe position. All poses are recorded relative to the ArUco marker.
5. **Validate** — the Exsensia platform replays the recording. If execution is correct, confirm; otherwise delete and re-record.
6. **Register** under a name (e.g., `PickFromIncubator`). The task is published to the ROS action server.
7. **Refresh** the task library in LabOS: `POST /api/library/mobile-tasks/refresh`. The new task appears in the workflow designer's toolbox immediately.

**Key teaching tips:**
- Anticipate the RG6 gripper's forward-push displacement during closure: pre-position the gripper slightly back before the final grasp to compensate
- Record all poses in the ArUco marker frame (Exsensia default after marker detection)
- Verify the task with at least 3 successful replays before using it in production workflows

---

## Maintenance

### Weekly
- Visually inspect ArUco markers for deformation or damage; replace paper-printed markers if warped
- Check tip rack consumption and restock as needed
- Archive or delete old result files from `Results/` to free disk space

### Monthly
- Run the commissioning checklist to verify all instrument connections are stable
- Check for updates to the Opentrons Flex firmware (robot touchscreen → Settings → Update)
- Review the plate tracking log for any anomalies
- Camera recalibration for the Intel RealSense (run `rs-calibration` on the Linux workstation)

### As Needed
- When switching deck configurations: update the HAL config and run a validation workflow with a scrap plate before starting real experiments
- When adding a new instrument: follow [ADDING_NEW_INSTRUMENT.md](ADDING_NEW_INSTRUMENT.md)
