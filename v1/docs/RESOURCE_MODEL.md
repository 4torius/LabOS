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

### Layer 3 — aliases, for the one genuinely variable binding

Tasks are taught by demonstration relative to each workstation's ArUco marker.
If the instrument behind a marker is swapped, the taught *motion* stays valid
but the *claim target* changes. That is the one binding that is deployment
variable rather than intrinsic, so it gets an indirection:

```yaml
resources:
  aliases:
    station_1: "deck:opentrons"
```

A claim on `station_1` resolves through it. Everything else resolves to itself.

### Why not make the indirection mandatory

Because for a safety mechanism, every extra place the truth can be wrong is a
cost, and mandatory abstract-name-plus-binding buys no expressive power this
scheme lacks. With server-declared names there is one way to be wrong — a claim
nobody provides, reported loudly. With a mandatory binding layer there are two
sources that can disagree, and the common implementation of a missing binding
is to fail open, which is silent and unsafe. With four instruments the config
cost would also be ~40 lines to keep correct, against zero here.

### What is deliberately *not* in the model

**Cross-resource mutex groups** ("these two resources cannot be held together
even though they are distinct") were considered and dropped. The motivating
example — "the arm cannot be in two places at once" — turns out to be covered
already, since every robot task claims `instrument:mobile`. The remaining cases
(two instruments on one power circuit, overlapping manoeuvring envelopes) are
hazards this lab does not have. Phase 1 of the refactor removed 1,500 lines of
machinery built against a hypothetical; this is not the moment to add more. The
extension point is `ResourceTopology.conflicts()`, which is the only place that
would need to change.

---

## Resolution rules

1. **The server wins for what it declares.** If a server declares a
   `resources` section, `resources.declare` in the config is ignored for that
   server's listed commands. The instrument stays the authority on itself, so
   plug-and-play is preserved: adding an instrument never edits central config.
2. **The config supplements where the server is silent** — a server with no
   `resources` section at all, or a command it did not list.
3. **Aliases are applied to claims after collection**, one step, no chains. An
   alias whose target is itself an alias is a configuration error.
4. **Anything unresolved is an error, fail closed.** A claim that names a
   resource no server provides and no alias translates makes every workflow
   containing that step fail validation, naming the instrument, the command and
   the resource. It is never downgraded to "no exclusion".

## Implicit claims, and why concurrency has to be earned

Every step implicitly claims `instrument:<instrument_id>`, declared or not. A
server that says nothing still gets serialised against itself.

That is not sufficient on its own. If an undeclared server's steps carried
*only* that implicit claim, then today — when no server has a `resources`
section yet — every step would have disjoint claims, nothing would conflict, and
the executor would happily dispatch all 19 steps of `Test_Bicocca` at once. The
new model would have introduced exactly the hazard it exists to remove.

So a step on an instrument whose server declares **no** resources at all also
claims a shared token, `lab:undeclared`. Every other undeclared step claims it
too, so they all serialise against each other.

The consequence is the migration path:

- **Today**, with no catalog declaring anything, every step claims
  `lab:undeclared` and execution is fully serial — byte-for-byte today's
  behaviour under `parallel_execution: false`. Nothing changes until something
  is declared.
- **As declarations land**, parallelism appears only between steps whose
  instruments have stated what they touch.

Concurrency is opted into by declaring, never by flipping a flag. For hardware
that is the right direction for the default to fail.

## `workflow.parallel_execution` goes away

The flag conflated two different assertions: *"my DAG is correct"* and *"run
things at once"*. With claims, neither needs configuring — a wave dispatches
concurrently for steps whose claims are disjoint and serially for steps that
contend, and there is no setting to get wrong.

It is replaced by `workflow.max_concurrency` (default unlimited, `1` to force
strict serial execution as a brake), which is a resource limit rather than a
change of semantics. The old key is read for one release and warned about.

## No deadlock, by construction — and why that justifies the phasing

A step acquires **all** of its claims as one atomic set before dispatch and
releases them when it finishes. It never holds one claim while waiting for
another, so a cycle cannot form: step-scoped claims cannot deadlock, whatever
the workflow looks like.

Deadlock becomes possible only with claims *held across* steps — the plate that
sits in the Tecan between `PlateIn` and `PlateOut`, where the tray is genuinely
occupied for the duration of three steps and no single step holds it. That is
real, and it is phase 2 (`holds` / `release_on`, declared by the server), with
validation rejecting a workflow that holds a claim and then depends on a step
needing it — before anything physical moves. It is deliberately second: a
deadlock in a system that moves a robot arm is not something to introduce
before the resource model itself has run at the bench.

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
| 6 | `holds` / `release_on` for claims held across steps | deferred |

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
