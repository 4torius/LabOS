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
| GET/DELETE | `/api/logs` | In-memory application log buffer |
| GET/DELETE | `/api/history` | In-memory command history (ephemeral — separate from the persistent Run History / `/api/db/runs`) |

## Instruments (`routes/instruments.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/devices` | All discovered instruments |
| GET | `/api/devices/{id}` | One instrument's detail |
| GET | `/api/devices/{id}/property/{feature}/{prop}` | Read a live SiLA2 property |
| POST | `/api/devices/{device_id}/command` | Execute any command on any discovered instrument — this is what the Dashboard's command modals call |
| GET | `/api/instruments/commands` | Instrument + command catalog (feeds the Workflow Builder toolbox) |
| GET | `/api/dynamic-options/{ui_hint}` | Dropdown options for a parameter hint (`recipe`, `hal_config`, `liquid_class`, `protocol`, …) — driven by each server's `LabOSLibraryProvider` catalog |
| GET | `/api/files/hal\|recipes\|protocols\|analyses\|workflows\|liquidclasses` | Raw file listings from the matching `Library/` subfolder |
| POST | `/api/opentrons/refill-tips` | Mark a tip rack refilled |
| GET | `/api/opentrons/tip-status` | Current tip consumption |

## Workflows (`routes/workflows.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/workflows` / `/api/workflows/{id}` | List / load workflow files |
| POST | `/api/workflows/save` | Save a workflow |
| POST | `/api/workflows/scan-variables` | Detect `${var}` placeholders in a workflow |
| POST | `/api/workflows/execute` | Start execution (single global lock — a second call while one is running returns `{"status": "busy"}`) |
| POST | `/api/workflows/pause` \| `/resume` \| `/abort` | Live control of the currently running workflow |
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

## Database — Plates & Run History (`routes/db.py`, prefix `/api/db`)

This is the SQLite-backed store actually used by the **Plates** and **Run History** pages (an older, unused in-memory/JSON plate-tracking mechanism was removed in this documentation pass — `_wf_track_tecan`/`_wf_track_opentrons` in `routes/workflows.py` now write directly here).

| Method | Path | Purpose |
|---|---|---|
| GET | `/plates`, `/plates/{id}`, `/plates/{id}/heatmap` | Plate search, detail, well-value heatmap |
| PUT/PATCH/DELETE | `/plates/{id}` | Status update, edit, delete |
| GET | `/runs` | Search/filter runs (status, date, text) |
| GET/PATCH/DELETE | `/runs/{id}` | Run detail; rename/notes edit; delete (removes measurements, preserves plate data) |
| GET | `/export/plate/{id}.csv`, `/export/run/{id}.json`, `/export/ai-context.json` | AI-context export, used by the Run History page's export links |
| GET | `/stats` | Summary counters |

## Results

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/results/{fmt}/{filename}` | Download a raw result file |
| GET | `/api/results/{fmt}/{filename}/view` | Inline text view |
| GET | `/api/files/animl_results` | List `.animl` files |
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

**`manual_intervention`** — a `RequestOperatorTask`-style step is waiting; drives both the Operator page's Pending Actions list and any active notification toast.

**`intervention_required`** — a step failed and is waiting for a Retry/Skip/Abort decision (full-screen modal).

---

## gRPC (SiLA2) — direct access, bypassing the webapp

Each server exposes a standard SiLA2 gRPC endpoint on its configured port (see [SERVERS.md](SERVERS.md) for the current list: Opentrons 50302, Tecan 50051, Manual Station 50500).

```python
from sila2.client import SilaClient

client = SilaClient("localhost", 50302, insecure=True)
result = client.WorkflowAPI.ExecuteRecipe(RecipeName="my_recipe.json")
print(result)
client.close()
```

Instrument-level errors raise `DefinedExecutionError` subclasses (see each server's FDL for its declared error identifiers); transport errors raise `grpc.RpcError`.
