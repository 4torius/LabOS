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
  "SchemaVersion": "1.0",
  "steps": [
    {
      "StepNumber": 1,
      "Instrument": "opentrons",
      "Action": "ExecuteRecipe",
      "Parameters": { "RecipeName": "elisa_coating.json" },
      "DependsOn": [],
      "RetryCount": 0,
      "TimeoutSeconds": 3600,
      "OnFailure": "stop"
    },
    {
      "StepNumber": 2,
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
      "StepNumber": 3,
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
| `StepNumber` | yes | Unique integer, referenced by `DependsOn` |
| `Instrument` | yes | The instrument's canonical key from `lab_config.yaml` (`opentrons`, `tecan`, `manual_station`, `mobile`) |
| `Action` | yes | Command identifier from the instrument's FDL |
| `Parameters` | yes | Key-value map matching the command's declared parameters |
| `DependsOn` | no | StepNumbers that must complete first; empty = no dependency |
| `RetryCount` | no | Automatic retry attempts on failure (default 0) |
| `TimeoutSeconds` | no | Max execution time. **`0` means unlimited**, not "fails instantly"; omitted uses `workflow.default_timeout` |
| `OnFailure` | no | `"stop"` (default), `"skip"`, or `"continue"` |

The file also carries `SchemaVersion` (currently `"1.0"`). Files without it
still load and run under the same rules. The Workflow Builder does not write it
yet, and it saves instruments under their display names (`TecanM200Pro`,
`GoFaGo Mobile Robot`), so a workflow saved from the web app has neither.
`python scripts/migrate_workflows.py --apply` adds `SchemaVersion` and
rewrites instrument names to their canonical form; run it after saving a
workflow you intend to commit. Without `--apply` it only reports.

### Instrument names

Use the canonical key. The resolver still accepts looser spellings when
*reading* an old file — `Tecan`, `TecanM200Pro` and `Tecan M200 Pro` all find
the same instrument, matching by exact id, then exact name, then ignoring case
and punctuation, then unique containment — but **a name that matches two
instruments is an error, not a coin flip**. It reports the collision and
refuses to guess, because on a machine that moves liquid, picking the wrong
instrument by dictionary ordering is not an acceptable failure mode.

### Validation happens before anything moves

The whole workflow is rejected up front if any of these hold:

- an instrument name matches nothing, or matches more than one
- an instrument is registered but not responding (so its capabilities could not
  be read) — reported once per instrument, naming the address
- an action does not exist on that instrument
- a required parameter is missing, or a parameter is named something the
  command does not declare (a typo used to be silently dropped, and the command
  ran without it)
- two steps share a `StepNumber`, a step depends on itself, a dependency does
  not exist, or the dependency graph contains a cycle
- an `ExecuteRecipe` step targets more wells than the plate holds

Check a file without running it:

```bash
python labos.py validate ELISA
```

---

## DAG Execution Model

Nodes = steps, edges = `DependsOn`. The steps are sorted into **dependency
waves**: wave 0 is everything with no unmet dependency, wave 1 everything that
depends only on wave 0, and so on. Inside a wave the file order is preserved,
so two independent steps still run in the order they were written.

Both execution modes use this same ordering. `workflow.parallel_execution` in
`lab_config.yaml` decides only whether one wave is dispatched concurrently or
one step after another — never what runs before what.

This matters more than it sounds. Sequential execution used to walk the file
top to bottom and mark a step SKIPPED when its dependency happened to be
written further down, so a valid workflow silently lost steps because of the
order it was typed in.

**Declare your dependencies.** With `parallel_execution: true`, a file whose
steps declare none dispatches all of them at once — for a workflow that passes
one plate between a mobile robot, a liquid handler and a plate reader, that is
a collision. The Workflow Builder saves `DependsOn: []` unless you tick the
boxes, so a workflow built there runs in file order only because parallel
execution is off. `python scripts/migrate_workflows.py --apply --chain-linear`
gives such a file the sequential chain explicitly.

---

## A Run Is a Background Job

Starting a workflow returns immediately with a `run_id`; the run continues past
the request that created it. Progress arrives over the WebSocket, and the state
can be read at any point from `GET /api/runs/{id}`.

Every step transition is written to the database as it happens — one row per
step *attempt*, so a retry keeps the attempt that failed instead of
overwriting it. That means a run that crashes halfway still leaves a
diagnosable trail, and a page opened mid-run can reconstruct exactly where
things got to. The **Database → Runs & Log** tab renders it as a timeline with
per-step durations and parameters.

One run at a time, deliberately: the instruments are physical and share a deck.
A second attempt is refused with the name of the run holding the slot and how
far along it is.

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

Pause/Resume/Abort are rendered as a **global floating status bar at the bottom
of every page**, which appears whenever a run is active.

**Pause** takes effect between steps. It cannot interrupt a command already
running on an instrument — a two-hour `ExecuteRecipe` finishes first.

**Abort** does reach the instrument. It sends each instrument still executing a
step that instrument's own stop command, discovered from its FDL (`AbortRun`,
`CancelTask`, `Stop`, `EmergencyStop` — whichever it declares, never
hardcoded), and only then stops waiting locally, so a cancelled wait can never
leave hardware running with nobody watching. Previously abort set a flag read
only between steps: the operator pressed Abort, the interface said "aborted",
and the robot carried on.

The response reports what each instrument actually did. If one did not confirm
the stop, the interface raises a persistent **"Abort incomplete — check the
instruments"** warning rather than folding it into a generic "aborted". Treat
that as: something may still be moving.

An instrument that exposes none of those commands is named explicitly, so you
know which one to stop by hand.

---

## Manual Steps: a Person Is a Station

The only step the orchestrator executes itself is the builder's **Delay** block
(`Instrument: Delay`, or an action of `Wait`). Every other step goes to a SiLA2
server — including the steps a person does.

A manual step is a command on the **Manual Station** server:

```json
{"StepNumber": 2, "Instrument": "Manual Station", "Action": "RequestOperatorTask",
 "Parameters": {"TaskDescription": "Move the plate to the dialysis bench"}, "DependsOn": [1]}
```

The server puts the task on the Operator page and the command returns when the
operator clicks **Confirm** (or after its `TimeoutSeconds`; `0` waits
indefinitely). `TaskType`, `Priority`, `SourceInstrument` and `TimeoutSeconds`
may be left out: the Manual Station declares defaults for them in its catalog.
`SendNotification` shows a message and does not wait.

This used to be different, and it matters if an old workflow now behaves
differently. Any instrument whose name contained `"manual"`, and any action
containing `"refill"`, was intercepted by the web app and handled there. The
Manual Station server was never called by a workflow; `SendNotification`
became a confirmation that stopped the run until someone clicked; and a
workflow that asked the operator to refill tips and then recorded the refill
(`Opentrons_Refill_Test`) asked the operator twice. A manual step is now an
ordinary step: it validates, retries, times out and appears in the run timeline
like any other - and the Manual Station must be running.

---

## No Loop / Conditional Control Flow

There is currently no loop or conditional (IF/Else) mechanism in LabOS beyond linear DAG dependencies — plan workflows accordingly. (An earlier version of the Workflow Builder toolbox had Loop/IF-Else/Note blocks that looked usable but weren't actually implemented by the executor; they've since been removed from the UI rather than left as a misleading affordance.)

---

## Batch / Multi-Sample Execution

`POST /api/workflows/batch-execute` runs the same workflow N times with per-sample variable substitution (`${var}` placeholders in step parameters, auto-detected via `/api/workflows/scan-variables`). This is currently **API-only** — no dedicated webapp form was found wired to it as of this writing; use it via a direct API call, or via the "Workflow Variables" modal for a single run.

---

## Timing a step

Two optional fields express a time constraint. Both are measured from the
moment the step's **last dependency finished** — or from the start of the run,
for a step with no dependencies.

```json
{
  "StepNumber": 14, "Instrument": "tecan", "Action": "RunMeasurement",
  "DependsOn": [7],
  "NotBeforeSeconds": 1800,
  "DeadlineSeconds": 1920
}
```

`NotBeforeSeconds` is a guarantee: the step waits. This is how an incubation is
expressed — the reading does not go out until thirty minutes after the step
that set the plate up completed. The wait happens **before** the step takes any
resource claim, so a run waiting out an incubation is not sitting on a deck or
a reader tray it is not using. An abort during a long wait takes effect
promptly rather than waiting the incubation out.

`DeadlineSeconds` is deliberately **not** a guarantee, and the distinction
matters. Nothing here can promise the reader is free at exactly thirty minutes;
making that happen would be a scheduler's job, and there is no scheduler (see
[RESOURCE_MODEL.md](RESOURCE_MODEL.md) for why that is a deliberate division).
What the executor can do is tell you the truth: if the step starts late it
still runs — the measurement happened and the data is real — and the overrun is
written into the step's result as `deadline_exceeded_by_seconds` and reported
in the run's errors. A plate read at 45 minutes when the protocol said 30 is a
result with a caveat, not a result, and the caveat should not be something you
have to reconstruct from timestamps afterwards.

Omitting both is the normal case and changes nothing.


## Workflow Library

Workflows are stored in `Library/Workflows/`. The webapp reads this folder live — adding a new `.json` file makes it available immediately, no restart needed. Templates (a separate, parameterized concept) live in `Library/Templates/` and support instantiate-with-variables plus version snapshots.

Shipped workflows (check `Library/Workflows/` for the current set — files get added/removed as recipes evolve):

| File | Description |
|------|--------------|
| `Test_Bicocca.workflow.json` | The full chain on all four instruments: the mobile robot carries a plate to the Opentrons (`ExecuteRecipe` with `test_6.json`), then to the Tecan (`RunMeasurement` with `Script1`), with a manual load at the start and a manual pickup at the end |
