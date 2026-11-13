# LabOS — Physical Resource Model

How LabOS knows that two steps must not run at the same time, and who gets to
say so. Written 2026-10-29. Companion to [REFACTOR_PLAN.md](REFACTOR_PLAN.md);
same convention — it records *why* each decision was made, so a later reader
can tell a deliberate choice from an accident.

---

## The problem, from the only workflow we ship

`Library/Workflows/Test_Bicocca.workflow.json` has 19 steps: ten on the mobile
robot, four on the Tecan, one Opentrons recipe, two operator tasks. Every one
of them declares `DependsOn: []`.

`topological_waves()` therefore puts all 19 in wave 0. Today that is harmless
because `workflow.parallel_execution` is `false` and a single wave is walked in
file order — but file order is, by the function's own docstring, only a tiebreak
*within* a wave, not a guarantee. The correctness of the lab's real workflow
rests on a property the model explicitly does not promise.

Set that one config flag to `true` and all 19 dispatch together: ten concurrent
`ExecuteTask` calls to one robot arm, `PlateIn` + `PlateOut` + `RunMeasurement`
racing on one plate reader, and an Opentrons recipe starting while the arm is
inside its deck.

That last one is the case that matters most, and it is the reason an
instrument-level lock is not enough. Look at what the robot's tasks are called:

```
place_on_OT      pick_from_OT      pick_from_tecan      place_on_tecan
```

`place_on_OT` is a step **on the mobile robot** that physically occupies **the
Opentrons' deck**. Nothing in the step says "opentrons". A lock keyed on the
step's instrument would let the Opentrons pipette into a deck that has a robot
arm in it, and would be working exactly as designed while it happened.

So the unit of exclusion cannot be the instrument. It has to be the physical
thing, whoever reaches for it.

## Two concerns, previously conflated into one boolean

**Logical order** — what must happen before what. This is science: incubate
before you read, pipette before you transport. It belongs in the workflow,
it is already expressible as `DependsOn`, and it is unused.

**Physical exclusion** — what cannot happen at the same time. This is safety.
It does not belong in the workflow: the person composing an experiment should
not have to know that the robot's `place_on_OT` and the Opentrons' recipe
contend for one space. It belongs with the instruments, and it did not exist.

They are independent, and both are required:

| | without resources | with resources |
|---|---|---|
| **without dependencies** | correct only by order of writing; one flag from a collision | collisions avoided, **arbitrary sequence** |
| **with dependencies** | right sequence, **collisions inside a wave** | correct |

Resources alone do not save `Test_Bicocca`: with claims but no dependencies,
`pick_from_tecan` could be serialised *before* `place_on_OT`. No collision, and
a ruined experiment that reports success — the worst available outcome. Hence
both halves of this document: declared claims, and a real DAG.

---

## Who declares what: by who is able to know

The guiding question is not "abstract or concrete names" but **who is in a
position to state the fact**.

> **In the server, what the server can know. In `lab_config.yaml`, what no
> server can speak for.**

This is not a new principle. It is why `servers:` carries host and port — a
server on another subnet cannot announce itself over mDNS, so the deployment
tells the orchestrator instead — and why timeouts live there: they are operator
policy, not a property of an Opentrons. The project has spent months *moving*
facts out of `lab_config.yaml` and into server catalogs (see the comment on
`ui_dropdowns`). Resource topology is an instrument fact and belongs in the
server. The exceptions below are the cases where that is impossible.

### Layer 1 — the server declares (the normal case)

In `library_catalog.json`, served over `LabOSLibraryProvider.LibraryCatalog`.
The file is each server's own configuration; the orchestrator never reads
anyone's file — it reads the property, over gRPC, at discovery. That is how the
mobile robot participates at all: its catalog lives on its own host and its code
is not in this repository.

```jsonc
// Opentrons — owns its deck, and says so
"resources": {
  "provides": ["instrument:opentrons", "deck:opentrons"],
  "commands": {
    "ExecuteRecipe":      { "claims": ["instrument:opentrons", "deck:opentrons"] },
    "LoadHardwareConfig": { "claims": ["deck:opentrons"] },
    "CaptureImage":       { "claims": ["instrument:opentrons"] }
  }
}

// Mobile robot — owns itself, reaches into other people's spaces
"resources": {
  "provides": ["instrument:mobile"],
  "commands": {
    "ExecuteTask": {
      "claims": ["instrument:mobile"],
      "by_parameter": {
        "TaskName": {
          "place_on_OT":     ["deck:opentrons"],
          "pick_from_OT":    ["deck:opentrons"],
          "place_on_tecan":  ["tray:tecan"],
          "pick_from_tecan": ["tray:tecan"]
        }
      }
    }
  }
}
```

`by_parameter` exists because on the robot the claim depends on *which task*,
not on which command: every transport is `ExecuteTask`. It is one level of
indirection, placed exactly where the real variability is.

**`provides` and `claims` are deliberately separate.** An earlier draft had
claims only, with servers agreeing on a name the way two processes agree on a
lockfile path. That fails badly: if the robot's author writes `deck:OT` and the
Opentrons' writes `deck:opentrons`, there is no error — there is simply **no
mutual exclusion, silently**. A safety mechanism that does not engage without
saying so is worse than none, because it is trusted in the meantime. With an
owner for every name, a claim nobody provides is a configuration error that
surfaces at discovery.

### Layer 2 — the config declares, for servers that cannot

A SiLA2 server shipped by an instrument vendor will never declare
`deck:opentrons`. It does not know an Opentrons exists, it does not know LabOS
exists, and it cannot be modified. For that class of server **nothing can state
the claim from the inside**, so the deployment states it:

```yaml
resources:
  declare:
    tecan:
      provides: ["tray:tecan"]
      commands:
        RunMeasurement: { claims: ["tray:tecan"] }
        PlateIn:        { claims: ["tray:tecan"] }
        PlateOut:       { claims: ["tray:tecan"] }
```

This is a *supplement*, never an override: see the resolution rules below. It is
what keeps the model open to instruments nobody here wrote, which is the
direction the system is meant to grow.

**What this layer is not about: reachability.** The mobile robot *was*
declared here, and the reason was narrow — its server's source was not in this
repository. It now is (`SiLA2/MobileSiLA2Server`), so the robot declares for
itself like every other instrument and this layer is, in the shipped
configuration, empty. It earns its place only for servers nobody here can
modify at all.

An entry is keyed by the instrument's key under `servers:` (`mobile`), which
is what a workflow writes. It is **not** the name the server announces
(`GoFaGo Mobile Robot`) — see *What the first review found* below for what
that confusion cost. An entry whose key matches no discovered instrument is
reported by `labos.py resources` and on the Settings page rather than dropped.

## `workflow.parallel_execution` goes away

The flag conflated two different assertions: *"my DAG is correct"* and *"run
things at once"*. With claims, neither needs configuring — a wave dispatches
concurrently for steps whose claims are disjoint and serially for steps that
contend, and there is no setting to get wrong.

It is replaced by `workflow.max_concurrency`, which is a resource limit rather
than a change of semantics: `1` forces strict serial execution, `0` means no
cap. A configuration that says nothing gets `1` — a lab that has not decided
runs one step at a time — and the shipped `lab_config.yaml` says `1`
explicitly. The old key is still read: `parallel_execution: false` is a cap of
one.

## No deadlock, by construction — and why that justifies the phasing

A step acquires **all** of its claims as one atomic set before dispatch and
releases them when it finishes. It never holds one claim while waiting for
another, so a cycle cannot form: step-scoped claims cannot deadlock, whatever
the workflow looks like.

Deadlock becomes possible only with resources *kept across* steps — the plate
that sits in the reader between the robot putting it there and the robot taking
it away, where the position is genuinely occupied for the duration of several
steps and no single step holds it. That is real, and it is handled in two
places:

* **Validation** refuses the case that is decidable from the workflow alone: a
  step needs a kept resource, and nothing able to release it can run before
  that step does (nothing releases it at all, or the releaser itself depends on
  the waiting step).
* **The scheduler** handles the rest — two plates each waiting for the position
  the other is in, which depends on the order things happen to run. When every
  step still alive is waiting on the ledger, none of them can ever be granted;
  they fail with a message naming what each was waiting for and who left it
  occupied, instead of the run hanging with nothing on screen.

A step that merely has to *wait* for a kept resource is not an error. It waits,
exactly as it would for a step-scoped claim.

---

## What this gives beyond the state of the art

Surveyed against the orchestrators in the 2026 literature:

| | elsewhere | here |
|---|---|---|
| Device abstraction | hand-written adapters (AlabOS: 28 device instances, one adapter each), or central config (UniLabOS) | server declares itself |
| Resource model | reservation over centrally configured resources (AlabOS) | **resources declared by the device, cross-checked at discovery** |
| Conflict scope | the instrument | **the physical thing, whoever reaches for it** |

A SiLA2 device declaring which physical resources its commands occupy, with
an owner per resource and validation at discovery, is not something the
surveyed systems do. It is a candidate for a proposed SiLA2 feature rather than
only an internal extension.

---

## Implementation phases

| Phase | What | Status |
|---|---|---|
| 0 | This document | ✅ |
| 1 | `src/resources.py` — parsing, three-layer resolution, fail-closed cross-check | ✅ |
| 2 | Enforcement in `_execute_waves`; `parallel_execution` retired | ✅ |
| 3 | `GET /api/resources`; `labos.py resources`; Settings panel | ✅ |
| 4 | `Test_Bicocca` annotated with its real dependencies | ✅ |
| 5 | Template server + authoring guide | ✅ |
| 6 | `holds` for resources kept across steps | ✅ |
| 7 | Review before the first bench run: topology key, scheduler, `acquires`/`releases`, retries, emergency stop | ✅ |
| 8 | The robot's server in the repository, declaring by task name | ✅ |
| 9 | Simulated lab, end-to-end tests ([SIMULATED_LAB.md](SIMULATED_LAB.md)) | ✅ |
| 10 | Bench validation with concurrency raised above 1 | **not done** |

## What the first review found

The branch was reviewed before it had run at the bench. Every unit test passed;
the following were nonetheless true, and each is now pinned by a test.

**The robot's claims never reached the executor.** The executor keyed its
topology by the name a server announces; `resources.declare.mobile` is keyed
the way a workflow is written. The robot announces `GoFaGo Mobile Robot`, so
its declaration matched nothing and `place_on_OT` claimed only the robot — the
motivating exclusion of this whole document did not engage. The Settings page,
which keyed by `mobile`, showed it as applied; the tests used doubles whose key
and name were the same string. Executor, API and CLI now build the topology
through one function (`topology_of`), and the end-to-end tests run against
servers that announce their real names.

**Concurrency could not be switched on except through the retired flag.**
Callers read `workflow.parallel_execution` — false when absent — and passed it
on, which forced one step at a time regardless of `max_concurrency`. This is
why the bug above never did any harm.

**The run advanced in waves.** A step waited for every step of the previous
wave, not for its own dependencies, so two independent chains moved in lockstep
and a `DeadlineSeconds` was missed by construction. A step now starts when its
own dependencies have settled. After a failure that halts the run nothing new
is started; what is already moving is left to finish.

**A kept resource counted as a step in progress.** Under a limit of one — the
shipped configuration — the first hold filled the limit and nothing could start
again; an abort could not reach a step queued for a resource either.

**A releaser was matched by command name alone**, so the same command on a
second instrument would have freed the first one's resource.

### The plate in the reader, resolved

The section below records why a hold on `PlateIn` was backed out. The overload
it describes was a symptom; the hold was on the wrong command. `PlateIn` moves
the tray. The plate enters the reader when the **robot** puts it there
(`place_on_TC`) and leaves when the robot picks it up (`pick_from_tecan`) —
with a hold on `PlateIn` there was even a gap, between the robot letting go and
the tray closing, in which a plate sat on a tray the model called free.

So there are two resources, and the robot's tasks declare the second:

| | what it is | who occupies it | for how long |
|---|---|---|---|
| `tray:tecan` | the tray as a space something moves in | `PlateIn`, `PlateOut`, a measurement, the robot's place/pick | one step (`claims`) |
| `slot:tecan` | "a plate is in the reader" | acquired by `place_on_TC`, released by `pick_from_tecan` | from one step to another (`acquires` / `releases`) |

```json
"ExecuteTask": {
  "claims": ["instrument:mobile"],
  "exhaustive": true,
  "by_parameter": { "TaskName": {
    "place_on_TC":     { "claims": ["tray:tecan"], "acquires": ["slot:tecan"] },
    "pick_from_tecan": { "claims": ["tray:tecan"], "releases": ["slot:tecan"] },
    "go_to_Tecan":     []
  } }
}
```

The same pair exists for the Opentrons deck (`slot:opentrons`). What happens to
a kept resource when a step ends:

* the step **succeeded** — what it acquires stays occupied; what it releases is
  free;
* the step **never ran** (skipped by the operator, or halted) — nothing changes;
* the step **failed** — nobody knows where the plate is, so what it acquires
  stays occupied *and* what it was releasing stays occupied.

A resource still occupied when the run ends is released and reported
(*"slot:tecan was still occupied at the end of the run (since step 12 …)"*):
the operator taking the plate out by hand is a legitimate ending, and worth a
line in the record.

`holds: {resource: Command}` remains, as shorthand for the case it fits — an
instrument whose own load and unload commands mean one thing each. It is read
as "this command acquires, and `Command` **on this server** releases".

### Declared by name, served by id

`Test_Bicocca` sends `TaskId`, an opaque id that changes whenever a task is
taught again. Declared by id in a file, the declaration stops matching the day
somebody re-teaches a pick, silently. The robot's catalog is therefore written
by task **name**, and the server — the only party that knows both — adds the id
entries from the robot's live task list each time it serves the catalog
(`SiLA2/MobileSiLA2Server/src/task_resources.py`).

`"exhaustive": true` says the table lists every value. A step whose task is not
in it is refused at validation, rather than believed to occupy nothing: a task
taught yesterday is not known to be harmless because nobody has written it down
yet. A task that only moves the robot gets an empty entry.

### What is still open

* **Nothing here has run at the bench with concurrency above one.** The
  simulated lab exercises the whole orchestrator against the real interfaces
  and declarations; it cannot tell whether a declaration is *physically true*.
  [SIMULATED_LAB.md](SIMULATED_LAB.md) has the order to do it in.
* **The robot PC must be redeployed** from this repository before the cap is
  raised. Until then the running server declares nothing and the Settings page
  lists the robot under "Declaring nothing".
* **The Tecan cannot be stopped from software.** Its server has no stop
  command, and the bridge's `RunMeasurementAsync` accepts a cancellation token
  it never uses. An emergency stop reports the reader as not stopped.
* **Whether `CancelTask` stops the arm mid-motion** is the robot platform's
  behaviour and has not been measured.
* **A robot carrying a plate** is not modelled (a `gripper:mobile` kept between
  a pick and a place would be the same mechanism).


### Why the Tecan does not declare a hold on `PlateIn`

`holds` exists and is tested, and the obvious place for it is the plate sitting
in the reader between `PlateIn` and `PlateOut`. Declaring it there was tried
against the real workflow and backed out, which is worth recording.

`Test_Bicocca` uses `PlateIn` twice: at step 13 to insert the plate, and at
step 17 to close the tray after the robot has taken the plate away. The first
occupies the tray; the second occupies nothing. A per-command declaration
cannot distinguish them, so a hold on `PlateIn` made step 17 keep the tray for
the rest of the run and validation — correctly — refused a workflow that is
right. The command is overloaded at the instrument's own API level, which is
not something the resource model can or should paper over.

Trying it also found two real bugs, which is the better argument for trying it:

* `RunMeasurement` claimed `tray:tecan`. With a hold in place that is
  double-counting — the tray is held *for* the plate being read — and it made
  the read wait for the `PlateOut` that follows it. Claiming the instrument and
  not the tray is the correct declaration either way.
* The deadlock check asked only "does this step depend on the releaser?", which
  rejected any *earlier* use of the same resource. `Test_Bicocca` empties and
  refills the tray, so a correct workflow was refused. A step the holder itself
  depends on runs strictly earlier and is never at risk; the rule now says so.

At the time the tray was left protected by `DependsOn` alone. It is now also
protected by `slot:tecan`, declared where the plate actually arrives and leaves
— see *The plate in the reader, resolved* above.

Two things the real declarations taught us, now part of the model:

**`control: true`.** The implicit per-instrument claim would have queued
`EmergencyStop`, `AbortRun` and `PauseRun` behind the very run they exist to
interrupt. A command marked `control` occupies nothing at all, not even its own
instrument. It is a flag rather than an empty `claims` list because an empty
list is far too easy to arrive at by accident for something that switches a
safety rule off; declaring both is contradictory and reported.

**Declare the parameter name the workflow really sends.** `Test_Bicocca` sends
`TaskId`, not `TaskName`. A `by_parameter` block keyed on `TaskName` alone
would have matched nothing, every transport would have claimed only the robot,
and the cross-instrument exclusion would not have engaged — silently. Both
spellings are declared; an unmatched key contributes nothing, so listing both
costs nothing. This is the failure mode `provides` was introduced to kill, met
again from a different direction, and it is worth remembering that the model
only protects what somebody actually named correctly.

See [ADDING_NEW_INSTRUMENT.md](ADDING_NEW_INSTRUMENT.md) for how to declare
resources on a new instrument, and `SiLA2/_NewInstrumentTemplate/` for a
working example.

---

## The same rule elsewhere: library types and dropdowns — done

The rule this document arrives at — *in the server what the server can know, in
`lab_config.yaml` what no server can speak for* — is not specific to resources,
and it is now applied to the other declared fact in the system: where a
parameter's picker gets its values. One reader, `src/options.py`, three layers:

1. the server, in its `library_catalog.json` `library_types` (recipe, analysis,
   hal_config, liquid_class — unchanged, this always worked);
2. `lab_config.yaml` `library_types.declare.<server>`, for a server that cannot
   declare;
3. `lab_config.yaml` `ui_dropdowns`, for hints that belong to no server.

**The labware catalogs stay in the config by right, not as a fallback**, and
this was the part worth getting straight. `plate`, `tiprack` and `pipette` are
properties of *the lab's inventory*; the Opentrons does not own the list of
plate definitions this lab possesses. "Who can know?" answers "the lab", and the
lab's voice is `lab_config.yaml`. Moving them into a server would have been the
mistake.

**The defect was elsewhere.** `_get_options_for_hint` had `if ui_hint ==
"plate"`, `"labware"`, `"tiprack"` and `"pipette"` with the folder walks written
out in Python. Instrument-agnostic, so they did not break plug-and-play the way
a `"tecan" in name` check would — but code where the architecture promises data,
each sitting beside a `ui_dropdowns` entry for the same hint: two sources that
could disagree, which is the shape `provides` was introduced to kill. They are
declarations now, and `test_options.py` pins each against the old
implementation's exact output rather than approximately.

`mobile_task` was already layer 2, invented before it was named — its comment
in `lab_config.yaml` said a server could declare it but the robot's runs
elsewhere. It is on the same mechanism now.

Two source kinds had to be added for the lab-wide catalogs, which the original
`folder`-or-`values` shape could not express: a field read out of every file
matching a glob (`plate`), and a list or a templated set of objects inside one
index file (`tiprack`, `pipette`). `labware` is the union of three sources,
which is why `sources` is a list.

**What stays in code, and why.** Two hints are *computed*, not declared: deck
contents (read from the active hardware config) and `location` (the discovered
instruments). Neither is a list of files, so neither is data, and neither names
an instrument.

**One bug this turned up.** For a dynamic hint, a top-level `folder` is the
snapshot of the last list read from the server — not somewhere to find values.
Declaring it as a source made the snapshot folder's own file names, `_options`
among them, appear as pickable robot tasks. Two different roles sharing a key
name; `HintSpec.snapshot_folder` keeps them apart.

`GET /api/dropdowns` and a card in the Settings Resources tab show which picker
is declared where, from what, and how many values it currently offers — because
an empty dropdown looks identical whether a server never declared it or the
folder behind it is empty, and an operator has no way to tell.
