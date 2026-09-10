# LabOS — API Reference

The LabOS web server (FastAPI) runs on **http://localhost:5000** by default (`python launcher.py --webapp`, or `--all`). Interactive Swagger docs are available at `/docs`.

Real-time events are delivered via a **single** WebSocket at `ws://localhost:5000/ws` — every event type (workflow progress, device status, operator notifications) is broadcast on this one connection.

This reference is organized by page/feature area, matching how the webapp routes are actually organized in `webapp/routes/`. It is not exhaustive of every parameter — check the route source or `/docs` for full request/response schemas.

---

## System / Config

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/config` | Read / write `lab_config.yaml` (deep-merged, atomic write) |
| POST | `/api/network/test` | TCP/gRPC reachability probe for a given host:port |

The old ephemeral `/api/logs` and `/api/history` endpoints (backing the removed **History** page) were dropped — `state.add_log`/`add_command` still exist and broadcast over the WebSocket for their side effects, there's just no page/endpoint reading them back anymore. Persistent records live at `/api/db/*` (below).

## Instruments (`routes/instruments.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/devices` | All discovered instruments |
| GET | `/api/devices/{id}` | One instrument's detail |
| GET | `/api/devices/{id}/property/{feature}/{prop}` | Read a live SiLA2 property. This is how state is read now — `GetStatus`-style *commands* were removed in favour of properties, which the dashboard shows in a "Live readings" panel rather than as buttons an operator presses to find something out. Example: `/api/devices/opentrons/property/WorkflowAPI/Status` |
| POST | `/api/devices/{device_id}/command` | Execute any command on any discovered instrument — this is what the Dashboard's command modals call |
| GET | `/api/instruments/commands` | Instrument + command catalog (feeds the Workflow Builder toolbox) |
| GET | `/api/dynamic-options/{ui_hint}` | Dropdown options for a parameter hint (`recipe`, `hal_config`, `liquid_class`, `location`, …) — driven by each server's `LabOSLibraryProvider` catalog. `location` returns the instruments discovery actually found |
| GET | `/api/files/hal\|recipes\|protocols\|analyses\|workflows\|liquidclasses` | Raw file listings from the matching `Library/` subfolder |
| POST | `/api/opentrons/refill-tips` | Mark a tip rack refilled |
| GET | `/api/opentrons/tip-status` | Current tip consumption |

## Runs (`routes/workflows.py`)

**A run is a background job.** `POST /api/workflows/execute` returns as soon as
the run has started, carrying its `run_id`; the run continues past the request
that created it. It used to hold the executor lock *and the HTTP connection*
for the whole run — hours, for a workflow that incubates a plate — and only
return the id at the very end, so a closed tab or a proxy read timeout lost the
reply entirely.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/runs` | Start a run. Alias of `/api/workflows/execute` |
| GET | `/api/runs` | Runs this process knows about, and which one is active |
| GET | `/api/runs/{id}` | Live state: status, current step, counts, percent. Falls back to the database for older runs |
| GET | `/api/runs/{id}/steps` | The real step timeline, available *while the run is going* |
| POST | `/api/runs/{id}/pause` \| `/resume` \| `/abort` | Control one run by id |

Starting a run returns:

```json
{ "started": true, "run_id": "20260910_142530_ELISA", "workflow_name": "ELISA",
  "status": "running", "total_steps": 7, "active": true, "percent": 0 }
```

`status` carries the run's own state, in the same vocabulary
`GET /api/runs/{id}` uses — `running`, `paused`, `completed`, `failed`,
`aborted`, `error` — so a caller does not have to learn a second one. `started`
says the request was accepted; `run_id` is the thing to hold on to.

Or, when the instruments are busy:

```json
{ "status": "busy",
  "error": "'ELISA' is already running (run 20260910_142530_ELISA, step 3 of 7). Wait for it to finish, or abort it first.",
  "active_run": { ... } }
```

One run at a time is deliberate: the instruments are physical and share a deck.
A refusal names the run holding the slot and how far along it is.

**Aborting** stops the run *and tells each instrument mid-step to stop*, using
that instrument's own command discovered from its FDL. The response reports
what each one actually did:

```json
{ "status": "aborted", "run_id": "...",
  "report": ["opentrons: AbortRun sent", "tecan: no abort command exposed — stop it at the instrument"],
  "requires_attention": true,
  "warning": "Some instruments did not confirm the stop — verify them physically" }
```

`requires_attention` is the field to act on: it means something may still be
moving.

## Workflows (`routes/workflows.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/workflows` / `/api/workflows/{id}` | List / load workflow files |
| POST | `/api/workflows/save` | Save a workflow |
| POST | `/api/workflows/scan-variables` | Detect `${var}` placeholders in a workflow |
| POST | `/api/workflows/execute` | Start a run; returns immediately with `run_id` (see Runs above) |
| POST | `/api/workflows/pause` \| `/resume` \| `/abort` | Control the active run; aliases of the `/api/runs/{id}` forms |
| GET | `/api/workflows/live-status` | Snapshot of the active run, for a page syncing on load |
| POST | `/api/workflows/batch-execute` | Run the same workflow N times with per-sample variable substitution (API-only, see [WORKFLOW_SYSTEM.md](WORKFLOW_SYSTEM.md)) |
| POST | `/api/intervention/respond` / GET `/api/intervention/pending` | Retry/Skip/Abort response to a failed-step intervention prompt |
| GET | `/api/templates` / `/{id}`, POST `/save`, POST `/{id}/instantiate`, DELETE `/{id}` | Workflow templates with variables |
| GET | `/{id}/export`, POST `/import`, GET `/{id}/versions`, POST `/{id}/versions/snapshot`, POST `/{id}/versions/{vid}/restore` | Import/export and version history per workflow |

## Recipe Generator (`routes/batch.py`, prefix `/api/batch`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/parse-chemical-excel` | Parse an uploaded Excel pipetting plan |
| GET | `/step-schema` | Module-treatment catalog for the "+ Add step" UI |
| GET | `/library-excel-files` | List `.xlsx` files already in `Library/Recipes/excel/` |
| POST | `/generate-recipe-with-config` | Generate a recipe JSON from the configured phases/reservoirs/treatments |
| POST | `/generate-workflow` | Generate a chaining workflow for multi-recipe output |
| POST | `/generate-hal` | Generate a matching generic HAL config |

## Hardware / HAL (`routes/hardware.py`)

| Method | Path | Purpose |
|---|---|---|
| GET/PUT | `/api/hardware/active` | Read / set the active HAL config. Writing persists to `Library/HardwareConfig/.active` and best-effort pushes it to the robot via `apply: {attempted, applied, message}`. The Hardware page surfaces this: a warning toast appears if `applied` is `false` even though the HTTP request itself succeeded |
| GET/POST | `/api/hardware/configs`, GET/DELETE `/api/hardware/configs/{filename}` | CRUD for HAL config files |
| GET | `/api/hardware/catalog/pipettes\|modules\|slots\|labware` | Reference catalogs for building a new HAL config |

## Operator (`routes/operator.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/operator/pending` | Pending manual-intervention / notification queue |
| POST | `/api/operator/notify` | Push a notification |
| POST | `/api/operator/acknowledge` | Confirm a pending action (manual step, refill, etc.) |
| POST | `/api/operator/manual-step` | "Add Log Note" on the Operator page. Writes a free-text entry (type/description/value) to the session log only — does not create a tracked workflow step or pending action. |
| POST | `/api/emergency/stop` | Broadcast a stop attempt (tries `EmergencyStop`/`Stop`/`Abort`/`Cancel`/`Halt`, whichever the instrument exposes) to **every** discovered instrument |

## Database — Plates & Runs & Log (`routes/db.py`, prefix `/api/db`)

This is the SQLite-backed store behind the unified **Database** page (`/database`, tabs: Plates | Runs & Log — `/plates` and `/runs` are kept as redirects for old bookmarks). `src/plate_tracking.py` writes here from both the workflow-execution path and manual Dashboard commands (see [ARCHITECTURE.md](ARCHITECTURE.md#layer-35-persistence)).

| Method | Path | Purpose |
|---|---|---|
| GET | `/plates`, `/plates/{id}` | Plate search, detail (wells, measurements, linked raw AnIML results) |
| GET | `/plates/{id}/heatmap` | Well-value grid for one plate. Optional `?measurement_type=...` — required to get a sane result on a plate measured with more than one technique (otherwise unfiltered wells with 2+ techniques get averaged together); the Database page's UI always passes this once a plate has more than one technique |
| PUT/PATCH/DELETE | `/plates/{id}` | Status update, edit, delete |
| GET | `/runs` | Search/filter runs (status, date, text) |
| GET/PATCH/DELETE | `/runs/{id}` | Run detail; rename/notes edit; delete (removes measurements, preserves plate data) |
| GET | `/export/plate/{id}.csv`, `/export/run/{id}.json`, `/export/ai-context.json` | AI-context export, used by the Database page's export links |
| GET | `/stats` | Summary counters |

## Results

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/results/{fmt}/{filename}` | Download a raw result file |
| GET | `/api/results/{fmt}/{filename}/view` | Inline text view |
| GET | `/api/files/animl_results` | List `.animl` files |
| GET | `/api/properties` | Every instrument property value currently known, optionally `?instrument_id=`. Values are pushed to the server by the instruments; this is the snapshot a page reads on load instead of firing one request per property |
| GET | `/api/images` | Deck photos taken with the robot's camera (`CaptureImage`, or a `TakeSnapshot` recipe step) |
| GET | `/api/images/{filename}` | One image. Shown in **Results → Deck photos** |
| GET | `/api/results/animl/{filename}/parse` | Parse an AnIML file into plate-heatmap data |

---

## WebSocket Events (`ws://localhost:5000/ws`)

All messages are JSON with a `type` field.

**`workflow_progress`** — sent on every step-state change:
```json
{ "type": "workflow_progress", "step_id": 3, "step_status": "success",
  "instrument": "tecan", "action": "RunMeasurement",
  "total_steps": 7, "completed_steps": 3 }
```

**`workflow_complete`** / **`workflow_error`** — terminal events with `steps_completed`/`steps_failed` and, on error, `failed_step`/`error`.

**`device_update`** — instrument status change (`idle`/`running`/`error`/`offline`).

**`property_update`** — an instrument published a new value for one of its
observable properties. Pushed by the instrument, not polled for:

```json
{ "type": "property_update", "instrument_id": "opentrons",
  "feature": "WorkflowAPI", "property": "Status",
  "value": "{\"state\": \"running\", \"run_id\": \"abc\"}",
  "source": "subscription" }
```

`source` is `subscription` when the instrument pushed it and `poll` when the
property is not observable and had to be read.

**`manual_intervention`** — a `RequestOperatorTask`-style step is waiting; drives both the Operator page's Pending Actions list and any active notification toast.

**`intervention_required`** — a step failed and is waiting for a Retry/Skip/Abort decision (full-screen modal).

---

## gRPC (SiLA2) — direct access, bypassing the webapp

Each server exposes a standard SiLA2 gRPC endpoint on its configured port (see [SERVERS.md](SERVERS.md) for the current list: Opentrons 50302, Tecan 50051, Manual Station 50500).

```python
from sila2.client import SilaClient

client = SilaClient("localhost", 50302, insecure=True)

# A property is read, not called
print(client.WorkflowAPI.Status.get())

# A command acts
result = client.WorkflowAPI.ExecuteRecipe(RecipeName="my_recipe.json")
print(result)

client.close()
```

Instrument-level errors raise `DefinedExecutionError` subclasses (see each server's FDL for its declared error identifiers); transport errors raise `grpc.RpcError`.

---

## Command line

Most of this is reachable without a browser:

```bash
python labos.py status                     # which servers and interfaces are up
python labos.py instruments -v             # commands and properties, with descriptions
python labos.py read opentrons Status      # one property
python labos.py validate ELISA             # check a workflow without running it
python labos.py run ELISA                  # run it, streaming per-step progress
python labos.py doctor                     # check the installation
python labos.py new-server Centrifuge      # scaffold a new instrument
python labos.py codegen OpentronsSiLA2Server
```

`run` confirms before starting, because it drives real instruments; `-y` skips
the prompt for scripted use.
