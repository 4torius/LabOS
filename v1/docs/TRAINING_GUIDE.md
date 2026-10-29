# LabOS — Training Guide

This is the one document to use to prepare and deliver a training session on LabOS. It assumes the trainer already knows the system; it does **not** assume the trainees do. It's organized as: what to say, what to show, what to warn people about, and what to do when something breaks.

For deeper technical detail on any topic, this guide links out to the rest of `v1/docs/` rather than repeating it.

---

## 1. The 30-Second Pitch (open the session with this)

> "LabOS is the software that runs our automated lab. It lets us describe an experiment once — as a recipe and a workflow — and have the robot, the plate reader, and manual steps all execute in the right order, automatically, with safety checks built in. You interact with it entirely through a web page; you never need to write code to run an experiment."

Then show the architecture picture from [ARCHITECTURE.md](ARCHITECTURE.md) if the audience is technical, or skip straight to the browser if they're not.

---

## 2. What's Actually in the System Today

Be upfront about this — it avoids confusion later when someone asks about the mobile robot and it doesn't show up:

| Instrument | In active use | Notes |
|---|---|---|
| **Opentrons Flex** | Yes | Liquid handling, the most fully-featured server (17 commands, tip safety, run archive) |
| **Tecan Infinite M200 Pro** | Yes | Plate reading, via a Windows C# bridge |
| **Manual Station** | Yes | Virtual — no hardware, just drives operator prompts in the web UI |
| **GoFaGo mobile robot** | Yes | Mobile manipulation + plate transport, hosted remotely on its own Linux/Docker host (`192.168.11.22`) — not part of this repo's checkout, but discovered and driven the same way as the local instruments |

---

## 3. Before the Training: Checklist

Run this the morning of (or the night before) the session — nothing kills a training session faster than a broken demo.

- [ ] `cd v1 && python launcher.py --all` (or double-click `LabOS_launcher.bat`) starts cleanly, no errors in any terminal window
- [ ] http://localhost:5000 loads, Dashboard shows Opentrons, Tecan, Manual Station, and Mobile Robot all **online**
- [ ] A HAL config is active on the **Hardware** page and matches the physical deck you've set up
- [ ] Tecan iControl software is **closed** on the host machine (it locks the USB port the bridge needs)
- [ ] Run `Test_Bicocca.workflow.json` once end-to-end with a scrap plate — confirms the instruments actually work today, not just that the software is "online"
- [ ] If mobile-robot plate transport is part of the demo: confirm ArUco markers at each workstation are clean and unobstructed, and run one `ExecuteTask` with someone watching the robot first — `ExecutionMode: DryRun` does **not** simulate, the current server ignores it and the robot moves

---

## 4. Suggested Agenda (adapt to your time budget)

| Time | Topic | Format |
|---|---|---|
| 10 min | The pitch + architecture overview (§1, §2) | Talk + diagram |
| 15 min | Starting the system, tour of every page (§6) | Live in browser |
| 20 min | Golden path: run a real experiment start to finish (§7) | Live demo, let one trainee drive |
| 10 min | Safety features — what protects you, what doesn't (§8) | Talk + show |
| 10 min | Things NOT to do (§9) | Talk — this section prevents most future support requests |
| 10 min | Basic troubleshooting (§10) | Talk + Q&A |
| 5 min | Where to find help / logs / this doc | Wrap-up |

---

## 5. Setup (if trainees need their own environment)

Full detail: [SETUP.md](SETUP.md). Short version:

```powershell
cd C:\LabOS\v1
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python launcher.py --all
```

Open **http://localhost:5000**.

Things that commonly need changing per machine: the Opentrons robot IP (`SiLA2/OpentronsSiLA2Server/config.yaml` → `robot.ip`), and whether the Tecan bridge exe has actually been built (see §9).

---

## 6. Page-by-Page Tour

Walk through these live, in this order — it roughly matches how an experiment actually flows through the system.

### Dashboard (`/dashboard`)
One card per instrument, live status, and a button to run any of its commands directly (with a parameter form if the command needs one). This is the "manual override" view — useful for a quick one-off command (e.g. `Home` the robot) without building a whole workflow.

### Recipe Generator (`/batch`, also reachable as `/recipes` — same page)
The normal way to create a recipe: upload or pick an Excel pipetting plan, group columns into phases, pick liquid classes and reservoir sharing per reagent, optionally insert module treatments (heater-shaker, temperature). Click Generate. **Point out explicitly**: the waste estimate is there, but it's inside the collapsible "Notes" panel, not a headline number — expand it or trainees will think it's missing.

**How the waste estimate is calculated, and how much extra to prepare** (this will come up — it's exactly what tells you how much excess solution to make):
The system doesn't aspirate once per well. With the "Distribute" strategy, it loads the pipette **once** and dispenses into as many wells as fit before the tip runs low, then reloads (a "trip"). Every time it reloads, a small fixed amount — the pipette's own minimum documented volume, e.g. 5 µL for the Flex 1-Channel 1000 µL pipette (a real spec-sheet number, not arbitrary) — stays in the tip as a safety margin and is never dispensed. That's the waste.
So: more trips needed (more destination wells, or a tip whose capacity is small relative to the total volume) means more waste. It isn't a fixed percentage — it depends on how many reloads that specific reagent needs.
*Concrete example*: distributing 50 µL to 24 wells with a Flex 1000 µL pipette (995 µL usable after reserving 5 µL of waste) → about 19 wells per trip → 2 trips to cover 24 wells → total waste ≈ 10 µL, against a required volume of 24 × 50 = 1200 µL (under 1%). A different pipette changes the per-trip waste accordingly (e.g. the Flex 50 µL pipette's minimum is 1 µL).
*How much excess to actually prepare*: required volume (sum across wells) + the estimated waste (shown per reagent in the "Notes" panel) + your own margin for the source reservoir's dead volume (the system does **not** compute that — it depends on the container's shape, not the recipe). With the "Transfer" strategy (fresh tip per well) there's no reload-waste accumulation at all — slower, but without this effect.

### Hardware (`/hardware`)
Pick which HAL config (deck layout) is active. This persists across restarts. **Point out**: clicking "Set Active" now shows a warning toast if the robot itself was offline and never actually got the update (versus a plain success toast when it did) — if you see the warning, re-apply once the robot is confirmed online.

### Workflow Builder (`/workflows`)
Drag instruments onto a canvas, set parameters, connect dependencies, save. Click Execute to run. A live status bar appears at the bottom of **every page** (not just this one) with Pause/Resume/Abort whenever something is running. **Point out**: there's no loop or IF/Else block — LabOS only supports linear step dependencies, not control flow.

### Operator (`/operator`)
Where a human responds to the system: confirm a manual step, respond to Emergency Stop, refill tips. **Point out**: "Add Log Note" here writes a free-text entry to the session log — it doesn't create a real workflow step, and its own modal says so. Don't rely on it for anything that needs to gate execution.

### Database (`/database`)
The persistent, searchable record of every experiment, in two tabs:
- **Plates** — the chemical record. Every successful Opentrons `ExecuteRecipe` automatically creates a plate here with the recipe's content per well (reagent, volume, % composition). Click a plate to see the well grid, click a well for its full content + linked measurements. If a plate was analyzed with more than one technique (e.g. fluorescence and absorbance), a "Tecnica" dropdown appears — the grid and stats always show one technique at a time, never an average across them. **Point out**: the "Add analysis" button on a plate's detail — it launches a new Tecan `RunMeasurement` with that plate's ID already filled in, so linking a follow-up analysis done days later (e.g. luminescence tomorrow, on the same plate you fluoresced today) is one click instead of copy-pasting an ID.
- **Runs & Log** — the automation record: what the orchestrator did, step by step, with rename/notes/delete and an "export for AI context" button useful if someone wants to feed a run's data to an LLM for analysis later.

**Point out**: this linking is **automatic only within a single workflow run** — a manual Dashboard command, or a workflow run on a different day, doesn't inherit a previous plate automatically. That's exactly what the "Add analysis" button is for.

### Results (`/results`)
Raw output files (Excel, AnIML, CSV) — the actual instrument readouts, separate from the Database's Runs & Log metadata.

### Settings (`/settings`)
Direct editor for `lab_config.yaml`. Developer/admin territory — most operators won't need this.

---

## 7. Golden Path Demo: One Full Experiment

A script you can literally follow live:

1. **Recipe Generator** — load a sample Excel plan from `Library/Recipes/excel/`, configure one phase, generate a recipe (and a HAL if you don't already have a matching one).
2. **Hardware** — select and activate the matching HAL config; load the physical deck to match (tip racks, plate, reagents) while narrating what each slot is for.
3. **Workflow Builder** — build (or load a pre-made) workflow: Opentrons `ExecuteRecipe` → either a Manual Station `RequestOperatorTask` ("move the plate to the reader") or, if the mobile robot is set up for this session, a `mobile` `ExecuteTask` step (`TaskId`, `ExecutionMode: Normal`) to transport the plate automatically → Tecan `RunMeasurement`. Click Execute.
4. **Watch the status bar** as the Opentrons step runs — narrate that the system is automatically protecting against leftover tips even though you can't see it happening (§8).
5. **Operator page** — when the manual step fires, show the trainee confirming it themselves.
6. **Database** — after completion, open the **Runs & Log** tab to show the step timeline, then switch to **Plates** to show the same plate's recipe content and its linked measurement, and open **Results** to show the actual Tecan output file.

This single walkthrough touches almost every page and most of the safety machinery worth explaining.

---

## 8. Safety Features to Explain

These operate automatically and are easy to misunderstand as "not working" if nobody explains them:

- **Leftover-tip guard (Opentrons)**: before every run, the system checks its own memory of which pipette mounts might still have a tip attached (the robot has no physical sensor for this) and automatically force-drops if unsure. You will sometimes see an extra drop-tip motion at the start of a run that isn't in the recipe you wrote — that's this guard, not a bug.
- **Emergency Stop (Operator page)**: stops the current run and tries to safely drop tips + home the robot. If that safety sequence itself fails, the system reports a warning rather than silently claiming success — take that warning seriously and check the robot physically. This also broadcasts a stop attempt to every discovered instrument, including the mobile robot if one is active.
- **Pause/Resume/Abort**: available from the global status bar on any page, not just the Workflow Builder.
- **HAL persistence + apply feedback**: the active deck config survives a restart, and the Hardware page now shows a clear warning toast if a HAL selection didn't actually reach the robot (rather than silently looking fine).
- **Run archive**: every run is logged automatically; nothing needs to be manually saved to get a permanent record.
- **Tecan `Connect()`**: always re-validates against the real hardware state now — it will not report "connected" after a silent USB disconnect.
- **Mobile robot hardware-activation commands** (`SetHardwareActivation`, `ActivateAllHardware`, etc.) are explicitly flagged by the server itself as physically consequential — they can engage real motors/servos on the arm, gripper, or base. Treat them with the same caution as `Home` on Opentrons or `PlateIn`/`PlateOut` on Tecan: confirm someone is physically present and watching before calling them, don't fire them just to "see what happens."

---

## 9. Things NOT to Do / Current Limitations

Cover this section explicitly — it prevents most "is this broken?" questions later.

- **There is no loop or IF/Else control flow** — the Workflow Builder only supports linear step dependencies. (An earlier version had Loop/IF-Else/Note toolbox blocks that looked usable but weren't actually implemented; they've been removed.)
- **"Add Log Note"** on the Operator page writes a free-text entry to the session log — it's not a tracked workflow step, and the modal says so. Use a `RequestOperatorTask` workflow step for anything that needs to actually gate execution.
- **Gripper offsets** can only be set by hand-editing a recipe's JSON — no web UI control yet.
- **Liquid-presence verification** (`VerifyLiquid`) *is* now wired into the UI for `Transfer`/`Distribute` — a "Verify aspiration" checkbox per reagent in the Recipe Generator's advanced options. Turning it on makes the Flex pipette physically probe for liquid before aspirating and stop the run cleanly if it doesn't find any — no manual retry needed, the hardware does the probing. Note for `Distribute`: Opentrons only re-arms this probe on a fresh, clean tip, so to actually catch a source well running dry mid-operation (not just being empty at the start), the system automatically uses a new tip for every reload trip when this checkbox is on — more tips used, somewhat slower, but every reload gets checked. Only turn it on for reagents where you actually need the safety net. Backup wells are also assigned automatically whenever the checkbox is on — no count to type in: the system reserves enough extra wells in the same reservoir to cover the reagent's own required volume again, and if the well in use runs dry mid-run it automatically moves to the next one instead of stopping — the run only stops if every assigned well (primary + backups) turns out empty. Check "Quantità richieste / note" after generating to see exactly which wells got assigned, so you know where to load the backup liquid. (The older, standalone-`Aspirate`-only variant of `VerifyLiquid` is still JSON-only, but the Recipe Generator never emits that command anyway.)
- **Waste estimates** from the Recipe Generator are real but only surfaced as a line of text inside the collapsible "Notes" panel — remind people to expand it.

---

## 10. Basic Troubleshooting Quick-Reference

Full detail and more scenarios: [OPERATIONS.md](OPERATIONS.md#troubleshooting).

| Symptom | First thing to check |
|---|---|
| An instrument shows "offline" | Is its process/terminal window (or, for the mobile robot, the remote Docker container) still running? Restart it — reconnects automatically in ~10s |
| Opentrons "could not connect to robot" | `ping` the robot IP in `config.yaml`; check the robot's own touchscreen |
| Tecan bridge not responding | Is iControl software closed? Check Task Manager for the bridge exe — it should auto-restart within seconds |
| Mobile robot task fails or shows "Server offline" | Check `docker ps`/`docker logs` on `192.168.11.22`; confirm the target ArUco marker is clean and unobstructed |
| A workflow won't validate ("Instrument(s) not found") | Check spelling and that the named instrument is currently online |
| Recipe fails against the active HAL ("missing labware mapping") | Confirm the correct HAL is active **and actually applied to the robot** — the Hardware page now warns you explicitly if it wasn't |
| Tip count looks wrong | The automatic tip guard may have force-dropped a tip you didn't expect — check the run's archived log before assuming a bug |
| Can't find a plate/run you're sure exists | Make sure you're on the **Database** page (SQLite-backed), on the right tab (**Plates** vs **Runs & Log**) |
| Tecan `RunMeasurement` fails with "PlateType not found" | Expected — that plate isn't registered in `PlateTypes.json` yet, or leave `PlateType` empty to use the protocol's default plate. See [SERVERS.md](SERVERS.md#mdfx-protocol-files) |

---

## 11. Where to Find Things

| What | Where |
|---|---|
| Full documentation index | [README.md](README.md) |
| System architecture | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Every SiLA2 command, per server | [SERVERS.md](SERVERS.md) |
| REST/WebSocket API | [API_REFERENCE.md](API_REFERENCE.md) |
| Recipe/HAL format | [HAL_SYSTEM.md](HAL_SYSTEM.md) |
| Workflow JSON format | [WORKFLOW_SYSTEM.md](WORKFLOW_SYSTEM.md) |
| Adding a new instrument | [ADDING_NEW_INSTRUMENT.md](ADDING_NEW_INSTRUMENT.md) |
| Application logs | Each server's own `logs/` folder — there's no in-browser live log viewer page anymore, it was ephemeral and removed |
| Permanent experiment records | **Database** page (Plates + Runs & Log tabs) / `Results/` folder / each Opentrons run's `run_archive/` |

---

## 12. Quick Reference Cheat Sheet

```
Start everything:     cd v1 && python launcher.py --all
Web interface:        http://localhost:5000
Opentrons SiLA2:      localhost:50302
Tecan SiLA2:          localhost:50051   (bridge internal: 50055)
Manual Station SiLA2: localhost:50500
Mobile robot (remote):     192.168.11.22:50053
```
