# LabOS — Workflow System

## Overview

A workflow in LabOS is a JSON file that defines a complete experimental procedure as a sequence of instrument commands. Workflows are stored in `Library/Workflows/` and executed by `PnPWorkflowExecutor`.

---

## Workflow File Format

A workflow file contains metadata and a list of steps:

```json
{
  "name": "ELISA Complete",
  "description": "Full ELISA plate preparation and reading",
  "version": "1.0",
  "author": "Andrea Rota",
  "steps": [
    {
      "StepID": 1,
      "Instrument": "opentrons",
      "Action": "ExecuteRecipe",
      "Parameters": {
        "recipe": "elisa_coating.json",
        "hal_config": "deck_config_A.json"
      },
      "DependsOn": [],
      "RetryCount": 0,
      "TimeoutSeconds": 600,
      "OnFailure": "stop"
    },
    {
      "StepID": 2,
      "Instrument": "mobile_robot",
      "Action": "execute_task",
      "Parameters": { "task_name": "PickFromOT3" },
      "DependsOn": [1],
      "RetryCount": 1,
      "TimeoutSeconds": 120,
      "OnFailure": "stop"
    },
    {
      "StepID": 3,
      "Instrument": "mobile_robot",
      "Action": "execute_task",
      "Parameters": { "task_name": "PlaceOnTecan" },
      "DependsOn": [2],
      "TimeoutSeconds": 120,
      "OnFailure": "stop"
    },
    {
      "StepID": 4,
      "Instrument": "tecan",
      "Action": "RunMeasurement",
      "Parameters": {
        "protocol": "Absorbance/abs_450nm.mdfx",
        "output_file": "results/elisa_result.xlsx"
      },
      "DependsOn": [3],
      "TimeoutSeconds": 300,
      "OnFailure": "stop"
    }
  ]
}
```

### Step Fields

| Field | Required | Description |
|-------|----------|-------------|
| `StepID` | yes | Unique integer identifier used in `DependsOn` references |
| `Instrument` | yes | Server name as registered in the PnP registry (e.g. `"opentrons"`, `"tecan"`, `"mobile_robot"`, `"manual_station"`) |
| `Action` | yes | Command identifier from the instrument's FDL (or SiLA2Common `ExecuteCommand` ID) |
| `Parameters` | yes | Key-value map of command parameters |
| `DependsOn` | no | List of StepIDs that must complete before this step can start. Empty list = no dependencies |
| `RetryCount` | no | Number of automatic retry attempts on failure (default: 0) |
| `TimeoutSeconds` | no | Maximum execution time before the step is considered failed (default: 300) |
| `OnFailure` | no | `"stop"` (default), `"skip"`, or `"continue"` |

---

## DAG Execution Model

The executor represents workflows as a Directed Acyclic Graph (DAG):
- Nodes = steps
- Edges = `DependsOn` relationships

```
          Step 1 (Opentrons: serial dilution)
         /                \
        /                  \
   Step 2 (Opentrons:    Step 3 (Opentrons:
   dispense samples)     add controls)
        \                  /
         \                /
          Step 4 (Robot: pick plate)
               |
          Step 5 (Robot: place at Tecan)
               |
          Step 6 (Tecan: measure)
               |
          Step 7 (Robot: pick from Tecan)
```

Steps 2 and 3 in the above example have no dependency on each other, so `PnPWorkflowExecutor` dispatches them **concurrently** as soon as Step 1 completes.

---

## Execution Pipeline

```
Load JSON
    │
    ▼
Validate (against live PnP registry)
    │  ├── Instrument availability check
    │  ├── Command existence check (FDL lookup)
    │  ├── Parameter type validation
    │  └── Dependency graph validation (no cycles, no dangling refs)
    │
    ▼ (valid)
Build DAG
    │
    ▼
Execute loop:
    ├── Collect all steps whose DependsOn are satisfied AND instrument is idle
    ├── Dispatch each as async ExecuteCommand via SiLA2Common
    ├── On completion: mark step done, update progress, notify WebSocket clients
    ├── On failure:
    │     ├── RetryCount > 0 → retry with exponential backoff
    │     ├── OnFailure = "skip" → mark skipped, continue
    │     ├── OnFailure = "stop" → halt workflow, report error
    │     └── OnFailure = "continue" → mark failed, continue remaining steps
    └── Repeat until all steps done or terminal failure
    │
    ▼
Return WorkflowResult
    ├── success (bool)
    ├── steps_completed / steps_failed / steps_skipped
    ├── total_duration_seconds
    ├── errors (list of {stepID, instrument, action, error_detail})
    └── step_results (list of {stepID, output})
```

---

## Control Flow Blocks

The visual designer supports control flow structures that translate to special step types:

### Loop
```json
{ "StepID": 5, "Instrument": "_control", "Action": "RepeatN", "Parameters": {"count": 3} },
...steps to repeat...
{ "StepID": 9, "Instrument": "_control", "Action": "EndLoop", "Parameters": {} }
```

### Wait (Delay)
```json
{ "StepID": 10, "Instrument": "_control", "Action": "Wait", "Parameters": {"seconds": 3600} }
```

### Conditional
```json
{ "StepID": 11, "Instrument": "_control", "Action": "IfThen", "Parameters": {"condition": "last_result.absorbance > 0.5"} },
...steps if true...
{ "StepID": 14, "Instrument": "_control", "Action": "Else", "Parameters": {} },
...steps if false...
{ "StepID": 16, "Instrument": "_control", "Action": "EndIf", "Parameters": {} }
```

---

## Workflow Validation

Validation runs against the **live PnP registry** before any instrument is touched. Four categories:

1. **Instrument discovery** — every `Instrument` value must match a registered, responsive server
2. **Command availability** — every `Action` must exist in the server's FDL (matched by command ID or display name)
3. **Parameter validation** — all required parameters provided; no unknown parameters; types match FDL definitions
4. **Dependency validation** — all `DependsOn` references point to existing StepIDs; no circular dependencies

Validation errors are returned as a structured list before execution starts, so the operator can fix issues without starting a partial run.

---

## Step Status Values

| Status | Meaning |
|--------|---------|
| `pending` | Not yet eligible for execution |
| `running` | Currently executing on the instrument |
| `success` | Completed successfully |
| `failed` | Execution failed (after retries if any) |
| `skipped` | Skipped due to `OnFailure: skip` |
| `waiting_intervention` | Paused, waiting for human confirmation |

---

## Manual Intervention Steps

A manual step pauses workflow execution and notifies the operator:

```json
{
  "StepID": 7,
  "Instrument": "manual_station",
  "Action": "RequestOperatorTask",
  "Parameters": {
    "task_description": "Replenish tip rack in slot A1, then click Confirm",
    "timeout_minutes": 30
  },
  "DependsOn": [6]
}
```

The workflow executor blocks on this step until the operator clicks "Confirm" in the web interface (or calls `POST /api/manual-station/confirm`). The step is visible in the real-time monitor with a countdown timer.

---

## Workflow Library

Workflows are stored in `Library/Workflows/`. The web interface reads this folder at startup and on each request, so adding a new `.json` file makes it immediately available without restarting LabOS.

Example workflows shipped with the system:

| File | Description |
|------|-------------|
| `ELISA_Complete.workflow.json` | Full ELISA: pipetting + transport + reading |
| `example.workflow.json` | Minimal two-step example |
| `example_no_robot.workflow.json` | Opentrons + Tecan only (no mobile robot) |
| `Manual_Intervention_Test.workflow.json` | Tests manual station integration |
| `Opentrons_Only_Test.workflow.json` | Liquid handling validation |
