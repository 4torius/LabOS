# LabOS — API Reference

The LabOS web server (FastAPI) runs on **http://localhost:8000**. Interactive documentation is available at `/docs` (Swagger UI) and `/redoc`.

Real-time events are delivered via WebSocket at `ws://localhost:8000/ws`.

---

## REST Endpoints

### System Status

#### `GET /api/status`
Returns overall system status.

**Response**
```json
{
  "status": "online",
  "instruments": {
    "opentrons": { "status": "idle", "version": "1.0.0" },
    "tecan":     { "status": "idle", "version": "1.0.0" },
    "mobile_robot": { "status": "idle", "version": "1.0.0" },
    "manual_station": { "status": "idle", "version": "1.0.0" }
  },
  "active_workflow": null,
  "uptime_seconds": 3600
}
```

---

### Instrument Discovery

#### `GET /api/instruments`
Returns all discovered instruments with their connection info.

#### `GET /api/instruments/commands`
Returns all instruments with their available commands (from FDL). Used by the visual designer to populate the toolbox.

**Response**
```json
{
  "opentrons": {
    "status": "online",
    "feature": "WorkflowAPI",
    "commands": [
      {
        "id": "ExecuteRecipe",
        "display_name": "Execute Recipe",
        "description": "Run a liquid handling recipe",
        "parameters": [
          { "id": "recipe", "type": "string", "required": true, "hint": "recipe" },
          { "id": "hal_config", "type": "string", "required": true, "hint": "hal_config" }
        ],
        "important": true
      }
    ]
  }
}
```

The `"hint"` field drives auto-population of dropdowns in the UI (`"recipe"` → lists from `Library/Recipes/`, `"task"` → queries mobile server, etc.).

---

### Workflows

#### `GET /api/workflows`
List all workflow files in `Library/Workflows/`.

**Response**: `[{ "name": "ELISA_Complete", "file": "ELISA_Complete.workflow.json" }, ...]`

#### `GET /api/workflows/{name}`
Load a specific workflow file.

#### `POST /api/workflows`
Save a new workflow.

**Body**: Workflow JSON object (see [WORKFLOW_SYSTEM.md](WORKFLOW_SYSTEM.md)).

#### `PUT /api/workflows/{name}`
Update an existing workflow.

#### `DELETE /api/workflows/{name}`
Delete a workflow file.

#### `POST /api/workflows/execute`
Start workflow execution.

**Body**
```json
{
  "workflow_name": "ELISA_Complete",
  "dry_run": false
}
```

**Response**
```json
{
  "execution_id": "exec-2025-05-15-001",
  "status": "started",
  "message": "Workflow execution started"
}
```

#### `POST /api/workflows/stop`
Stop the currently running workflow.

#### `GET /api/workflows/status`
Get current workflow execution status.

**Response**
```json
{
  "execution_id": "exec-2025-05-15-001",
  "workflow_name": "ELISA_Complete",
  "status": "running",
  "current_step": 3,
  "total_steps": 7,
  "steps": [
    { "id": 1, "status": "success", "duration_seconds": 245 },
    { "id": 2, "status": "success", "duration_seconds": 35 },
    { "id": 3, "status": "running",  "elapsed_seconds": 12 }
  ],
  "elapsed_seconds": 292
}
```

---

### Dynamic Options (UI Dropdowns)

#### `GET /api/dynamic-options/{hint}`
Returns dropdown options for a parameter type. Called by the properties panel in the visual designer.

| Hint | Returns |
|------|---------|
| `recipe` | Files from `Library/Recipes/` |
| `hal_config` | Files from `Library/HardwareConfig/` |
| `protocol` | `.mdfx` files from `Library/Analysis/` |
| `task` | Tasks from Mobile SiLA2 Server (live query) |
| `location` | Named map locations from ROS |
| `liquid_class` | Files from `Library/LiquidClasses/` |

---

### Recipes

#### `GET /api/recipes`
List all recipe files.

#### `GET /api/recipes/{name}`
Load a recipe file.

#### `POST /api/recipes`
Save a new recipe.

#### `POST /api/recipes/validate`
Validate a recipe against a HAL config without running it.

**Body**: `{ "recipe": "elisa_coating.json", "hal_config": "deck_config_A.json" }`

#### `POST /api/recipes/batch-import`
Upload an Excel file and generate a recipe automatically (Excel-to-recipe converter).

---

### Results

#### `GET /api/results`
List all result files.

#### `GET /api/results/{filename}`
Download a result file (Excel or AnIML).

#### `GET /api/plate-tracking`
Return the full plate tracking log.

---

### Manual Station

#### `POST /api/manual-station/confirm`
Confirm completion of the current manual step (equivalent to clicking "Confirm" in the web UI).

#### `GET /api/manual-station/status`
Get current manual station state.

---

### Library

#### `GET /api/library/workflows`
#### `GET /api/library/recipes`
#### `GET /api/library/hal-configs`
#### `GET /api/library/protocols`
#### `GET /api/library/mobile-tasks`

All return file lists from the corresponding `Library/` subdirectory.

#### `POST /api/library/mobile-tasks/refresh`
Query the Mobile SiLA2 Server for registered tasks and update `Library/MobileTasks/`.

---

## WebSocket Events

Connect to `ws://localhost:8000/ws` to receive real-time events. All messages are JSON.

### Workflow Progress

Sent whenever a workflow step changes state:

```json
{
  "type": "workflow_progress",
  "execution_id": "exec-2025-05-15-001",
  "step_id": 3,
  "step_status": "success",
  "instrument": "mobile_robot",
  "action": "execute_task",
  "duration_seconds": 35,
  "total_steps": 7,
  "completed_steps": 3
}
```

### Device Update

Sent when an instrument changes status (idle/busy/error/offline):

```json
{
  "type": "device_update",
  "instrument": "opentrons",
  "status": "running",
  "detail": "Executing protocol step 45/96"
}
```

### Workflow Complete

```json
{
  "type": "workflow_complete",
  "execution_id": "exec-2025-05-15-001",
  "success": true,
  "steps_completed": 7,
  "steps_failed": 0,
  "duration_seconds": 1845,
  "result_files": ["results/elisa_result.xlsx"]
}
```

### Workflow Error

```json
{
  "type": "workflow_error",
  "execution_id": "exec-2025-05-15-001",
  "failed_step": 4,
  "instrument": "tecan",
  "action": "RunMeasurement",
  "error": "Tecan server unavailable",
  "steps_completed": 3
}
```

### Manual Intervention Required

```json
{
  "type": "manual_intervention",
  "step_id": 5,
  "task_description": "Replenish tip rack in slot A1",
  "timeout_minutes": 60
}
```

---

## gRPC (SiLA2Common)

Each server exposes gRPC on its configured port. The proto file is at `v1/SiLA2/SiLA2Common.proto`.

### Connecting (Python)

```python
import grpc
import SiLA2Common_pb2 as pb2
import SiLA2Common_pb2_grpc as pb2_grpc
import json

channel = grpc.insecure_channel("localhost:50052")
stub = pb2_grpc.SiLA2CommonServiceStub(channel)

# Get server info
info = stub.GetServerInfo(pb2.Empty())
print(info.server_name)  # "opentrons"

# Execute a command
response = stub.ExecuteCommand(pb2.CommandRequest(
    command_id="ExecuteRecipe",
    params_json=json.dumps({
        "recipe": "my_recipe.json",
        "hal_config": "deck_config_A.json"
    })
))
print(response.success, response.result_json)
```

### Error Handling

`ExecuteCommand` always returns a `CommandResponse`. On failure:
- `success = false`
- `error_detail` contains a human-readable description
- No gRPC exception is raised for instrument-level errors (only for network failures)

Network/transport errors raise standard gRPC exceptions (`grpc.RpcError`). Handle them with:

```python
try:
    response = stub.ExecuteCommand(request)
except grpc.RpcError as e:
    print(e.code(), e.details())
```
