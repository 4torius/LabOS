# LabOS — Operations Guide

## First-Time Setup

See [SETUP.md](SETUP.md) for a full new-PC deployment guide. Summary of prerequisites:

- Windows 10/11, Python 3.10/3.11
- Tecan iControl SDK installed (for the C# bridge)
- Opentrons Flex reachable on the network
- `pip install -r v1/requirements.txt`

### Server Ports

| Component | Port |
|-----------|------|
| Web interface | 5000 |
| Tecan SiLA2 Server | 50051 |
| Opentrons SiLA2 Server | 50302 |
| Manual Station SiLA2 Server | 50500 |
| Tecan bridge (internal only, not exposed) | 50055 |
| Mobile Robot (remote, hosted on its own Linux host) | 50053 |

All configured in `v1/lab_config.yaml` (bootstrap seeds) and each server's own `config.yaml`.

---

## Starting the System

### Option A — Launcher (recommended)

```powershell
cd C:\LabOS\v1
python launcher.py --all      # start every server + the webapp
```

or double-click `LabOS_launcher.bat` (Windows).

Other launcher modes: `--servers` (SiLA2 servers only), `--webapp` (web app only), `--status` (check what's running), `--stop` (stop everything the launcher started).

### Option C — Check without a browser

```bash
python labos.py doctor       # dependencies, config validity, schema versions
python labos.py status       # which servers and interfaces are up
python labos.py instruments  # what was discovered, and what it can do
```

`doctor` is the first thing to run on a machine that "used to work": it
validates `lab_config.yaml`, checks every dependency, counts the Library files
and flags workflows that predate the current schema version.

### Option B — Manual start (for debugging one component)

```bash
# Terminal 1
cd v1/SiLA2/TecanM200SiLA2Server && python main.py
# Terminal 2
cd v1/SiLA2/OpentronsSiLA2Server && python main.py
# Terminal 3
cd v1/SiLA2/ManualStationSiLA2Server && python main.py
# Terminal 4 — webapp
cd v1 && python webapp/app.py
```

Open **http://localhost:5000**.

---

## Commissioning Checklist

Run this before the first workflow execution in a new environment or after any hardware change.

### Hardware
- [ ] Opentrons Flex powered on, network-reachable at the IP in `SiLA2/OpentronsSiLA2Server/config.yaml`
- [ ] Tecan M200 Pro powered on and USB-connected
- [ ] Tecan iControl software is **closed** (it locks the USB connection the bridge needs)
- [ ] Deck loaded per the HAL config you intend to select (tip racks, labware in the correct slots)
- [ ] If using the mobile robot: GoFaGo base booted, arm powered, gripper connected; ArUco markers mounted and unobstructed at each workstation

### Software
- [ ] `python labos.py doctor` reports no problems
- [ ] `python labos.py status` shows all four servers reachable
- [ ] Dashboard (`/dashboard`) shows all four as online, with values in each
      card's **Live readings** panel (an instrument whose readings say "error"
      is announced but not really answering)
- [ ] Hardware page: select the correct HAL config, confirm "Set Active" reports the config was actually applied to the robot (a warning toast appears if it wasn't — see below)
- [ ] Recipe Generator: generate or load a small test recipe
- [ ] `python labos.py validate Opentrons_Only_Test` passes
- [ ] Run `Opentrons_Only_Test.workflow.json` from the Workflow Builder with a scrap plate
- [ ] Press **Abort** during that run and confirm the robot actually stops, and
      that the interface does not raise "Abort incomplete"
- [ ] Tecan: `RunMeasurement` with a short test protocol, verify an AnIML result appears on the Database page's Plates tab
- [ ] Mobile robot: `RefreshTasks` populates `AvailableTasks` with the expected task set

---

## Day-to-Day Operations

### Before Each Experiment
1. Confirm all instruments in use (Opentrons, Tecan, Manual Station, Mobile Robot) show online on the Dashboard
2. Confirm the correct HAL config is active on the Hardware page
3. Confirm tip racks and reagents match the active HAL config
4. If Tecan will be used, confirm iControl software is closed
5. If the mobile robot will move plates between instruments, confirm ArUco markers are clean and unobstructed

### Running an Experiment (Golden Path)

1. **Recipe Generator** (`/batch`) — upload/select an Excel pipetting plan, configure phases/reservoirs/treatments/liquid classes, generate the recipe (+ workflow if multi-phase, + HAL if needed)
2. **Hardware** (`/hardware`) — select and activate the matching HAL config
3. **Workflow Builder** (`/workflows`) — load the generated workflow (add mobile-robot transport steps between instrument steps if plates need to move), click **Execute**
4. **Monitor** — the global status bar at the bottom of every page shows live progress; Pause/Resume/Abort are available there
5. **Manual steps** — if the workflow includes one, respond via the **Operator** page's Pending Actions
6. **Review results** — **Database** (`/database`) for the persistent record: **Runs & Log** tab for the searchable/renamable/exportable step timeline, **Plates** tab for well-level content and linked measurements; **Results** (`/results`) for raw output files

### After Each Experiment
1. Clear completed plates from the deck
2. Check tip consumption on the Operator page; use "Refill Tips" if racks were physically replaced
3. If anything looked wrong mid-run, check the **Database** page's **Runs & Log** tab for that run's step timeline before assuming it's a hardware fault

---

## Network Exposure

The web interface binds `0.0.0.0:5000` with CORS open and **no authentication**.
Anyone who can reach that port can run a workflow, which on this system means
moving a robot arm.

That is a deliberate trade-off for an isolated lab network, not an oversight —
adding a login to a machine an operator uses with gloves on has its own cost.
It is only safe while the network is actually isolated. If the orchestrator
host ever gets a route to a wider network:

- bind it to `127.0.0.1` (`python webapp/app.py --host 127.0.0.1`) and reach it
  through an authenticated reverse proxy, or
- restrict port 5000 with a host firewall rule to the operator workstations.

The SiLA2 servers themselves (50051, 50302, 50500) are equally unauthenticated
and equally consequential.

---

## Troubleshooting

### A command that used to exist has disappeared from the dashboard

An instrument's commands come from the server that is *running*, not from the
files on disk. After editing a feature definition and regenerating, the server
must be restarted before the new commands appear — and until it is, the old
ones are what everything sees.

```bash
python labos.py instruments        # what the running servers actually offer
```

### Server shows "offline" in the dashboard
1. `python labos.py status` — it names the address it tried
2. Check the server's terminal/process is still running
3. Check the port isn't taken by something else: `netstat -an | findstr 50302` (or 50051/50500)
3. Check `config.yaml` host/port match `lab_config.yaml`
4. Restart the server — discovery reconnects automatically within ~10 seconds
5. If a stale server on a *different* host was previously using the same configured port, this used to cause the wrong instrument to appear under a given name — that specific bug is fixed (host-aware instrument-ID resolution), but if you ever see an instrument's identity look wrong after a network change, that's the first thing to double-check.

### Opentrons: "Could not connect to robot"
1. Verify the robot is powered and reachable: `ping <robot_ip from config.yaml>`
2. Check `SiLA2/OpentronsSiLA2Server/config.yaml` → `robot.ip`
3. Check the robot's own touchscreen for an error state

### Tecan: `Connect` fails, but iControl connects fine

That combination is the symptom, not a contradiction: **iControl takes exclusive
ownership of the USB connection**, and does not reliably release it on close.
If it has been opened at any point since the Tecan server started, the bridge
cannot get the instrument.

1. Close iControl (and Magellan) completely.
2. Restart the Tecan server, so the bridge re-enumerates USB devices. The
   bridge caches nothing across restarts, but the SDK's device enumeration
   happens when it connects.
3. Only then check power and cable.

A useful distinction while reading the error: if the dashboard shows the Tecan
**online** and its properties read (`IsConnected: false`,
`OperationalStatus: Disconnected`), the SiLA2 server is fine and answering. The
failure is strictly between the bridge and the instrument — nothing to do with
the network, the orchestrator or the workflow.

### Tecan: bridge not responding
1. Check Task Manager for `TecanSiLA2Server.exe` — if absent, the Python server's health monitor should auto-restart it within seconds (with backoff)
2. Confirm Tecan iControl software is closed (it locks the USB connection)
3. `Connect()` always re-validates against the real hardware state now (fixed) — it will no longer report "connected" after a silent USB drop. If you ever rebuild `TecanBridge.cs` in the future, remember the launcher prefers the **Release** build over Debug — rebuild Release too, or the fix you just made won't actually run (see [SERVERS.md](SERVERS.md#bridge-binary-is-up-to-date)).

### Tecan: `RunMeasurement` fails with "PlateType 'X' not found in PlateTypes.json"
This is intentional — you asked for a plate override that isn't registered yet, and the bridge refuses to silently fall back to the `.mdfx`'s own default plate (that would look like success while reading the wrong geometry). Either leave `PlateType` empty to use the protocol's default plate, or register the plate first — see [SERVERS.md](SERVERS.md#mdfx-protocol-files). Remember to restart the Tecan server after editing `PlateTypes.json` — it's loaded once at bridge startup.

### Mobile robot: task fails or "Server offline" despite discovery showing it online
1. Verify the remote Docker container is up: `docker ps` / `docker logs mobile-sila2` on `192.168.11.22`
2. Check the `RobotConnectionStatus` property — the server's own background reconnect loop will show `Connecting`/`Disconnected` if it can't reach ROS, and self-restarts (via Docker) if the ROS master was itself restarted underneath it
3. Confirm the ArUco marker at the target workstation is clean, lit, and unobstructed
4. Re-run `RefreshTasks` to confirm the task registry (`AvailableTasks`) is intact before assuming a connectivity fault

### Workflow validation error: "Instrument(s) not found: X"
The instrument name doesn't match a currently discovered server — check spelling and that the server is online. (The old Loop/IF/Note toolbox blocks that used to trigger this error have been removed from the Workflow Builder entirely.)

### Workflow validation error: "Missing labware mapping" / recipe fails against the active HAL
The recipe references a logical labware/slot the active HAL config doesn't define.
1. Confirm the correct HAL is active on the Hardware page — a warning toast now appears automatically if "Set Active" didn't actually reach the robot, so absence of a warning is a reasonable (not absolute) signal it applied correctly
2. Regenerate the HAL config from the Recipe Generator, or edit it directly in `Library/HardwareConfig/`

### Tip count or tip-guard behavior looks wrong
1. The Opentrons server automatically force-drops any tip it isn't sure is clear before a run — this is expected and not a bug; check the run's archived log if you want to confirm what happened
2. To reset tip consumption counts: `ResetTipTracking` (SiLA2 command) or the Operator page's refill flow
3. Physically verify racks match the displayed count before resetting — resetting doesn't check reality, it just clears the counter

### "Add Log Note" on the Operator page didn't do anything visible
It writes a free-text entry to the session log (now including what you typed, not just the note type) — it is not a tracked workflow step or pending action, by design. Use `RequestOperatorTask` (via a workflow step) for anything that needs to actually gate execution.

---

## Maintenance

### Weekly
- Check tip rack consumption; restock as needed
- Archive or delete old files from `Results/` and the Opentrons server's `run_archive/` if disk space is a concern (the run archive already auto-compresses after 3 days and deletes after 30 by default)

### Monthly
- Run the commissioning checklist to verify all instrument connections are stable
- Check for Opentrons Flex firmware updates (robot touchscreen → Settings → Update)
- Visually inspect ArUco markers for deformation or damage if the mobile robot is in regular use
- Confirm the Tecan bridge binary in use is up to date with the latest `TecanBridge.cs` source — after any bridge source change, rebuild **Release**, not just Debug (see [SERVERS.md](SERVERS.md#bridge-binary-is-up-to-date))

### As Needed
- When switching deck configurations: update/generate the HAL config and validate with a scrap plate before real experiments
- When adding a new instrument: follow [ADDING_NEW_INSTRUMENT.md](ADDING_NEW_INSTRUMENT.md)
- When teaching the mobile robot a new manipulation task: see [SERVERS.md](SERVERS.md#teaching-new-tasks-learning-from-demonstration)
