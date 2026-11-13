# The simulated lab

A stand-in for the bench: the real servers' names, commands and declarations,
with a clock where the hardware would be. It exists so that the orchestrator
can be exercised end to end — discovery, validation, scheduling, resource
exclusion, abort, emergency stop — without a robot to break and without
touching the code that runs the real lab.

```bash
cd v1
python -m sim_lab run Test_Bicocca
```

```
COMPLETED: 19 completed, 0 failed, 0 skipped, 14.6s

What the instruments did (seconds from the first command):
    0.00 ->   0.80  ok       mobile.ExecuteTask(WorkingPose)
    ...
    3.64 ->   4.44  ok       mobile.ExecuteTask(place_on_OT)
    4.50 ->   6.51  ok       opentrons.ExecuteRecipe(test_6.json)
    ...
Commands on different instruments in flight together: 0
No two commands overlapped on a resource they both occupy.
```

## What is real and what is not

| | |
|---|---|
| The SiLA2 features | **real** — each simulated server is built from the real server's generated bindings, so commands, parameters and types are what the orchestrator meets at the bench |
| The names the servers announce | **real** — `TecanM200Pro`, `OpentronsFlex`, `GoFaGo Mobile Robot`, `ManualStation`, which are *not* the keys a workflow writes |
| `library_catalog.json` | **real** — served by each server's own `LabOSLibraryProvider`, including the robot's task-name-to-id expansion. What it declares it occupies is what will be in force on the hardware |
| The orchestrator | **real**, all of it |
| What a command does | **nothing**. It records when it started, waits, and records when it ended |

The record is the point. `sim_lab` writes a timeline at the instruments, on
their own clocks, and overlaps are read from that — not from the scheduler's
account of what it scheduled.

What it therefore cannot tell you is whether a declaration is **physically
true**. If `place_on_OT` really reaches into the deck and the catalog does not
say so, the simulated lab will happily run it alongside a recipe and report no
overlap. That is what the bench is for.

## It cannot reach the real lab

* every simulated server listens on `127.0.0.1` only, on ports of its own
  (51051, 51302, 51053, 51500), and is not announced over mDNS;
* the orchestrator is pointed at them by a **copy** of `lab_config.yaml`
  written to a working folder, in which every server is on `127.0.0.1`, mDNS
  discovery is off, and any server that is not simulated is disabled.
  `lab_config.yaml` itself is never written to;
* the copy is selected with the `LABOS_CONFIG` environment variable, so it
  applies only to the terminal it was set in;
* runs made through the web interface go to `Results/labos.sim.db`, not the
  lab's own database.

## Using it

**One workflow, from a terminal**

```bash
python -m sim_lab run Test_Bicocca
python -m sim_lab --max-concurrency 1 run Test_Bicocca      # one step at a time, as the bench runs today
python -m sim_lab --scenario my_scenario.json run my.workflow.json
```

The simulated configuration has **no concurrency cap** unless you give one —
the opposite of the real one, because overlapping is what there is to look at.
Exit code 0 if the run completed, 1 if it did not, 2 if two commands overlapped
on a resource both occupy.

**The whole application**

```bash
python -m sim_lab up
```

and, in another terminal, set `LABOS_CONFIG` as it prints and use LabOS as
usual — `python labos.py instruments`, `python labos.py resources`,
`python webapp/app.py --port 5050`. Use a port other than 5000 so it cannot be
mistaken for the real one.

**The tests**

```bash
python -m pytest tests/simlab -q
```

About forty seconds. They run in CI with the unit tests.

## Scenarios: making things go wrong

`sim_lab/scenario.default.json` says how long each command takes. A scenario
can also make a command fail, or never return:

```json
{
  "default_duration": 0.3,
  "opentrons": {
    "ExecuteRecipe": {"duration": 2.0, "fail": "pipette error", "fail_times": 1}
  },
  "tecan": {
    "RunMeasurement": {"hang": true}
  }
}
```

| key | effect |
|---|---|
| `duration` | seconds the command takes |
| `fail` | the command fails with this message |
| `fail_times` | how many times it fails before it succeeds (default 1 when `fail` is set) |
| `hang` | it never returns until the instrument is stopped |
| `response` | values for the command's response fields |

A stop command (`EmergencyStop`, `AbortRun`, the robot's `CancelTask`)
interrupts whatever that instrument has in flight, which then ends with status
`stopped` in the timeline.

## Adding an instrument

Add a line to `BICOCCA` in `sim_lab/lab.py`: the key from `lab_config.yaml`,
the server's folder, the name it announces, a free port. Nothing else — the
commands come from its generated bindings and the declarations from its
catalog.

## What it has already caught

Running the branch against it for the first time turned up, before anything
else, that a configuration which says nothing about concurrency runs one step
at a time — so "remove the line to lift the cap" in a comment was wrong. The
unit tests could not have: they construct the executor directly.

---

# Before raising `max_concurrency` at the bench

`lab_config.yaml` ships with `max_concurrency: 1`: one step at a time, as the
lab has always run. With that setting the scheduler changes nothing about what
overlaps, because nothing does. Do these in order, and stop at the first
surprise.

`main` is untouched by any of this. Work in the same folder and `git switch`
between branches — **not** in a second copy: `tip_state.json` and `labos.db`
are not in git, and a fresh copy would believe every tip rack is full.

**0. A way back.** `git tag bench-known-good main`.

**1. Off the bench.** `python -m pytest tests -q` and
`python -m sim_lab run Test_Bicocca`.

**2. Redeploy the robot's server** from `SiLA2/MobileSiLA2Server` (see its
`DEPLOYMENT.md`). Then, with everything on:

```bash
python labos.py doctor
python labos.py resources
```

`resources` must show no problems, nothing under "declaring nothing", and the
robot's `place_on_OT` claiming `deck:opentrons`. If a task is reported as not
declared, add it to the robot's `library_catalog.json` — with what it really
occupies.

**3. The same run as always.** `Test_Bicocca`, cap still 1. It must behave as
on `main`. Two differences are intended: a failed step is no longer sent again
on its own (the operator is asked at once), and the run summary ends with a
line if a plate was left in an instrument.

**4. The emergency stop, with nothing at stake.** Press it during an operator
step, then during a robot move in free space. Check on the Operator page which
instruments acknowledged: the Tecan is expected to be listed as *not stopped*.
Check that the arm actually stops — whether `CancelTask` halts a motion in
progress is the robot platform's behaviour and has not been measured — and that
the workflow does not carry on.

**5. Exclusion, harmlessly.** Set `max_concurrency: 0`. Run a two-step workflow
with **no** `DependsOn` whose steps claim the same resource and are safe in
either order — the Tecan's `PlateOut` and `PlateIn`. They must run one after
the other. Then two that share nothing — `PlateOut` and the Opentrons `Home`.
They must overlap.

**6. The real one.** `place_on_OT` and a recipe with no `DependsOn` between
them, empty labware, a hand on the hardware stop. The recipe must not start
until the arm is clear.

Only then leave the cap raised. Put it back to 1 the moment anything is not as
expected: with a cap of one, no declaration can be wrong in a way that matters.
