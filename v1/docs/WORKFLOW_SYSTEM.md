# LabOS — Workflow System

## Overview

A workflow in LabOS is a JSON file that chains instrument commands into a complete experimental procedure. Workflows are stored in `Library/Workflows/` and executed by `PnPWorkflowExecutor` (`src/workflow.py`), driven from the **Workflow Builder** page (`/workflows`).

---

## Workflow File Format

```json
{
  "name": "ELISA Coating and Read",
  "description": "Coat plate, incubate, read absorbance",
  "version": "1.0",
  "steps": [
    {
      "StepID": 1,
      "Instrument": "opentrons",
      "Action": "ExecuteRecipe",
      "Parameters": { "RecipeName": "elisa_coating.json" },
      "DependsOn": [],
      "RetryCount": 0,
      "TimeoutSeconds": 3600,
      "OnFailure": "stop"
    },
    {
      "StepID": 2,
      "Instrument": "manual_station",
      "Action": "RequestOperatorTask",
      "Parameters": {
        "TaskDescription": "Move plate to Tecan and load it manually",
        "TaskType": "manual_operation",
        "TimeoutSeconds": 1800
      },
      "DependsOn": [1],
      "OnFailure": "stop"
    },
    {
      "StepID": 3,
      "Instrument": "tecan",
      "Action": "RunMeasurement",
      "Parameters": {
        "ProtocolFile": "Absorbance/abs_450nm.mdfx",
        "PlateID": "P001",
        "SampleSetID": "run1",
        "PlateType": "96-well"
      },
      "DependsOn": [2],
      "TimeoutSeconds": 300,
      "OnFailure": "stop"
    }
  ]
}
```

**Important**: `Action` and `Parameters` must match the real SiLA2 command names and parameter names from the instrument's FDL — see [SERVERS.md](SERVERS.md) for the authoritative, verified list per server. `Parameters` keys are case-sensitive PascalCase (e.g. `RecipeName`, not `recipe`).

### Step Fields

| Field | Required | Description |
|-------|----------|--------------|
| `StepID` | yes | Unique integer, referenced by `DependsOn` |
| `Instrument` | yes | Server key as registered in `lab_config.yaml` / discovered live (`opentrons`, `tecan`, `manual_station`, `mobile`) |
| `Action` | yes | Command identifier from the instrument's FDL |
| `Parameters` | yes | Key-value map matching the command's declared parameters |
| `DependsOn` | no | StepIDs that must complete first; empty = no dependency |
| `RetryCount` | no | Automatic retry attempts on failure (default 0) |
| `TimeoutSeconds` | no | Max execution time; **`0` or omitted-with-default means unlimited/uses the configured default**, not "fails instantly" |
| `OnFailure` | no | `"stop"` (default), `"skip"`, or `"continue"` |

A pre-flight check rejects the whole workflow before anything runs if any `Instrument` name doesn't match a currently discovered server ("Instrument(s) not found: ...").

---

## DAG Execution Model

Nodes = steps, edges = `DependsOn`. The executor repeatedly collects the "wave" of steps whose dependencies are all satisfied and dispatches that wave concurrently via `asyncio.gather` — steps with no dependency relationship to each other run in parallel automatically; there is no separate "parallel" flag to set per step.

---

## Execution Pipeline

```
Load JSON
    │
    ▼
Validate (against live PnP registry)
    │  ├── Instrument availability check
    │  ├── Command existence check (FDL lookup)
    │  ├── Parameter presence check
    │  └── Dependency graph validation
    │
    ▼ (valid)
Execute in dependency-waves:
    ├── Collect all steps whose DependsOn are satisfied
    ├── Dispatch each wave concurrently (asyncio.gather)
    ├── On completion: mark step done, broadcast workflow_progress over /ws
    ├── On failure:
    │     ├── RetryCount > 0 → retry
    │     ├── OnFailure = "skip" → mark skipped, continue
    │     ├── OnFailure = "stop" → halt, surface an intervention modal (Retry/Skip/Abort)
    │     └── OnFailure = "continue" → mark failed, continue remaining steps
    └── Repeat until all steps done or terminal failure
    │
    ▼
WorkflowResult: success, steps_completed/failed/skipped, step_results, errors
```

Only **one workflow can execute at a time** system-wide (a single shared executor lock) — starting a second run while one is active returns a "busy" status instead of queuing it.

---

## Live Control: Pause / Resume / Abort

Unlike a per-page button, Pause/Resume/Abort are rendered as a **global floating status bar at the bottom of every page** (not just the Workflow Builder), which appears automatically whenever a workflow is running and disappears when it finishes. It calls `POST /api/workflows/pause|resume|abort`, which signal the single active executor instance.

---

## Manual Intervention Steps

Any step whose `Instrument` value **contains the substring `"manual"`** (not just an exact `"manual_station"` match) is intercepted by the executor's manual-step handling instead of being sent to a real SiLA2 server: it posts an operator notification and blocks (polling) until the Operator page's "Confirm" button is clicked or the timeout fires. This is what actually implements `manual_station` / `ManualStation.RequestOperatorTask` steps in practice.

---

## No Loop / Conditional Control Flow

There is currently no loop or conditional (IF/Else) mechanism in LabOS beyond linear DAG dependencies — plan workflows accordingly. (An earlier version of the Workflow Builder toolbox had Loop/IF-Else/Note blocks that looked usable but weren't actually implemented by the executor; they've since been removed from the UI rather than left as a misleading affordance.)

---

## Batch / Multi-Sample Execution

`POST /api/workflows/batch-execute` runs the same workflow N times with per-sample variable substitution (`${var}` placeholders in step parameters, auto-detected via `/api/workflows/scan-variables`). This is currently **API-only** — no dedicated webapp form was found wired to it as of this writing; use it via a direct API call, or via the "Workflow Variables" modal for a single run.

---

## Workflow Library

Workflows are stored in `Library/Workflows/`. The webapp reads this folder live — adding a new `.json` file makes it available immediately, no restart needed. Templates (a separate, parameterized concept) live in `Library/Templates/` and support instantiate-with-variables plus version snapshots.

Shipped example workflows (check `Library/Workflows/` for the current set — files get added/removed as recipes evolve):

| File | Description |
|------|--------------|
| `Opentrons_Only_Test.workflow.json` | Liquid handling validation, no other instrument |
| `Manual_Intervention_Test.workflow.json` | Exercises the manual-station pause/confirm cycle |
| `Simple_PlateRead.workflow.json` | Minimal Tecan-only example |
| `Opentrons_Refill_Test.workflow.json` | Exercises the tip refill flow |
