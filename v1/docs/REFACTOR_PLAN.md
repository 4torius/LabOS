# LabOS — Structural Refactor Plan

Written 2026-09-09/10 after a full-system audit. This is a working document:
each phase carries acceptance criteria and is checked off as it lands. It
records *why* each change is made, so a later reader can tell a deliberate
decision from an accident.

Baseline commit: `a214be9` (checkpoint of all prior pending work).

---

## The problem in one paragraph

LabOS's foundations are sound: SiLA2-native discovery, servers that
self-describe their commands and their `Library/` file types, a real SQLite
scientific record, genuine safety guards on the robot. What is not sound is
the layer that *drives* those foundations. A workflow run is welded to the
HTTP request that started it. Pause and abort only take effect between steps
and never reach the instrument. The same execution logic is implemented three
times with three different sets of bugs. Instrument identity is resolved by
substring matching in five places. And several generations of superseded
machinery are still in the tree, so it is genuinely hard to tell which code
is load-bearing. The result is a system that works when everything goes right
and is hard to reason about when it does not.

---

## Design principles for this refactor

1. **One implementation per concept.** One executor, one instrument resolver,
   one settings object, one place that knows how to talk to a SiLA2 server.
2. **The orchestrator stays instrument-agnostic.** Nothing added here may
   hardcode an instrument name. Everything instrument-specific is declared by
   the server, in its FDL or its `library_catalog.json`.
3. **Fail loudly at the boundary, never silently mid-run.** Validation
   rejects a bad workflow before anything physical moves; a step that cannot
   be executed is an error, not a silent skip.
4. **Persistence is the source of truth, the WebSocket is a courtesy.** Any
   UI must be able to reconstruct full state from REST after a refresh.
5. **Physical safety is explicit.** Anything that can move hardware says so
   in its name, its FDL description, and its API docs.

---

## Phase 0 — Checkpoint ✅

Commit the ~127 files pending since 2026-07-30 so the refactor is a separate,
reviewable diff. Add ignore rules for generated output (`Results/Plots`,
`Results/Images`, `run_archive/`) that was about to be committed by accident.

**Done:** `a214be9`.

---

## Phase 1 — Remove superseded machinery ✅

Several execution strategies and parsers from earlier generations are still
present. They are not merely unused: they actively mislead, because a reader
cannot tell them apart from live code, and one of them (`instruments.json`)
silently serves *fabricated* instrument data when discovery fails.

**Removed:**

| What | Why it goes |
|---|---|
| `src/sila2_stub_generator.py`, `regen_stubs.py`, `src/pnp_stubs/` (except what the Tecan legacy bridge still needs) | "Strategy 1" dynamic gRPC stub execution. Documented as used by no current server. `launcher.py` regenerated these on every startup. |
| `PnPClient._execute_via_stub`, `_load_stubs_for_server`, `_try_load_stub`, `_build_request`, `_parse_response`, `_message_to_dict` | The Strategy 1 code path inside the client (~340 lines). |
| `SiLA2/sila2_common_servicer.py` | A third FDL parser plus a `SiLA2Common` servicer nothing registers. |
| `SiLA2/sila2_xml_parser.py` | A second FDL parser. `src/discovery.py::SiLAXMLParser` is the live one. |
| `SiLA2/sila2_mdns_registry.py` | Manual mDNS registration, superseded by the `sila2` library's own. |
| Opentrons `SiLA2CommonAdapter` + legacy `start()` in `src/server.py` (~480 lines) | A whole second gRPC server that `main.py` never calls. |
| `webapp/static/config/instruments.json` | Fallback catalog listing an "incubator" that does not exist and mobile commands that were never implemented. Serving invented commands is worse than serving none. |
| `Results/plate_tracking.json` | Pre-SQLite tracking store, superseded by `Results/labos.db`. |
| `tests/test_stub_loading.py`, `tests/test_xml_parser.py` | Test the two removed parsers. |

**Implication:** if a future non-`sila2` server ever needs raw gRPC, it gets a
purpose-built adapter rather than this speculative machinery. That is a better
trade than carrying 1,500 lines against a hypothetical.

**Acceptance:** unit suite green; `launcher.py --status` works; discovery and
command execution unaffected (they only ever used Strategy 0).

---

## Phase 2 — One typed settings object ✅

`lab_config.yaml` is parsed independently by nine modules, each with its own
defaults and error handling. `LabCore._command_timeout()` re-read and
re-parsed the file on *every command*. Timeout semantics live in six places.

**Built:** `src/settings.py` — frozen dataclasses (`Settings`, `ServerSpec`,
`WorkflowSettings`, `DiscoverySettings`, `WebappSettings`, `PathSettings`),
loaded once via `get_settings()`, invalidated by `reload_settings()` (called
by the config-save endpoint). Timeout normalisation (`0`/`null` → unlimited)
happens in exactly one place, `Settings.resolve_timeout()`.

Stale values fixed as part of this: `launcher.py`'s fallback ports
(50300/50201), `ServerConfig.port` default (50052), `conftest.py`'s manual
station port (50360 → 50500). Dead keys removed: `system.language`,
`mobile.network.ros_*` (never read by anything).

**Acceptance:** every previous config read site goes through `get_settings()`;
config save still atomic and still hot-reloads; tests cover unlimited-timeout
semantics.

---

## Phase 3 — Per-step persistence ✅

`run_results` gets one row written at run start and one at run end. Everything
in between — which step is running, what failed, how long it took — exists
only in the webapp process's memory and in WebSocket messages already sent. A
restart loses the run's history; a page opened mid-run cannot reconstruct it.

**Built:** `run_steps` table (one row per step attempt, with parameters,
status, timings, error, and structured result), written as each step starts
and finishes. `src/database.py` gains `save_run_step()`, `get_run_steps()`,
`update_run_status()`.

**Implication:** the Runs & Log tab becomes a real timeline instead of a
summary, and a crashed run leaves a diagnosable trail.

---

## Phase 4 — One instrument resolver, strict validation, schema versions ✅

Instrument names are matched by substring in `workflow.py`, `lab_core.py`,
`routes/workflows.py` (twice) and `routes/instruments.py`, each slightly
differently. Saved workflows call the same instrument `Tecan`,
`TecanM200Pro`, `OpentronsFlex` and `GoFaGo Mobile Robot`. One shipped
workflow calls a command (`ReadAbsorbance`) that does not exist.

**Built:**
- `LabCore.resolve_instrument(name) -> Instrument | None` — the single
  resolver: exact id, then exact name, then a *scored* match that requires an
  unambiguous winner. Ambiguity is an error, not a coin flip.
- Workflow validation rejects unknown instruments, unknown commands and
  unknown parameters up front, with the list of valid alternatives.
- `SchemaVersion` on workflow/recipe/HAL files, and
  `scripts/migrate_workflows.py` that rewrites saved workflows to canonical
  instrument ids and reports what it could not resolve.

**Implication:** loose aliasing (`recipe` → `RecipeName`) stays supported for
*reading* old files, but saving normalises to the canonical form, so the
alias-tolerance stops spreading.

---

## Phase 5 — One executor: topological order, pluggable steps, real control ✅

Three problems in one place:

- `_execute_sequential` iterates steps in file order and marks a step SKIPPED
  if its dependency appears later in the file. There is no topological sort.
  Meanwhile every shipped workflow has `DependsOn: []`, so parallel mode would
  dispatch all 15 steps of a mobile-robot workflow simultaneously.
- The webapp replaces the executor's step function wholesale and reimplements
  dispatch, giving two divergent behaviours (plate-id key case, timeout
  semantics, intervention timeout → SKIP here, ABORT there).
- Pause/abort set a flag read *between* steps. During a two-hour
  `ExecuteRecipe` neither does anything, and abort never tells the robot.

**Built:**
- `topological_waves()` — one dependency ordering used by both modes; cycles
  and missing dependencies are validation errors. Sequential = flattened
  waves, so a file-order dependency now works.
- A step-handler registry (`DelayHandler`, `ManualHandler`, `RefillHandler`,
  `InstrumentHandler`). The webapp supplies operator-interaction callbacks
  instead of reimplementing dispatch.
- Real control: the executor holds the in-flight `asyncio.Task`; abort
  cancels it *and* sends the instrument's own abort command, discovered from
  its FDL (`AbortRun`/`CancelTask`/`Stop`/`EmergencyStop`), never hardcoded.

---

## Phase 6 — Runs as background jobs ✅

`POST /api/workflows/execute` holds the executor lock and the HTTP connection
for the entire run — potentially hours. The `run_id` is generated at the start
but only returned at the end. A closed tab or a proxy timeout loses the reply.

**Built:** `src/run_manager.py`. `start_run()` returns a `run_id` immediately
and runs the workflow as a supervised background task; status is available
from REST at any time; events persist to `run_steps` and broadcast over the
existing WebSocket. Endpoints: `POST /api/runs`, `GET /api/runs/{id}`,
`GET /api/runs/{id}/steps`, `POST /api/runs/{id}/pause|resume|abort`. The old
workflow endpoints stay as thin compatibility wrappers.

**Implication:** the UI stops depending on a long-lived HTTP request, and a
second run attempt gets a clear "busy" answer naming the run that holds the
lock.

---

## Phase 7 — Opentrons: a command surface that matches the machine ✅

The FDL exposes 18 commands, of which several are pure remote file listings
(`ListRecipes`, `ListHardwareConfigs`, `ListTipRacks`) that the webapp already
reads far more cheaply from disk, and every response is JSON stuffed into a
String. Meanwhile the server *implements* but does not expose: `MoveLabware`
(gripper), the whole heater-shaker set, `Transfer`/`Distribute`/`Consolidate`/
`Mix`, and recipe-vs-HAL validation. There are no observable Properties, so
the dashboard polls `GetStatus`.

**Changes:**
- **Removed** from the FDL: `ListRecipes`, `ListHardwareConfigs`,
  `ListTipRacks`, `Initialize`, `GetRobotInfo`, `GetModulesStatus`. File
  listings belong to the webapp's `Library/` reads; `Initialize` duplicated
  what `start()` already does; the two `Get*` are now Properties.
- **Added**: `RunSteps` (execute inline recipe steps without a saved file —
  this is what makes atomic control possible without inventing one command
  per Opentrons primitive), `MoveLabware`, `SetModuleTemperature`,
  `ShakeModule`, `SetModuleLatch`, `ValidateRecipe`, `CaptureImage`.
- **Properties** (observable): `Status`, `CurrentRunId`, `ActiveConfig`,
  `TipsAvailable`, `RobotInfo`, `ModulesStatus`.

**Implication for workflows:** a plate can be moved between deck slots by the
gripper, or a heater-shaker driven, as a *workflow step* — today either
requires authoring a recipe file. Physically consequential commands say so in
their FDL descriptions.

---

## Phase 8 — The camera ✅

The Flex has an on-board camera and the code to use it already exists, wired
end to end but reachable by nobody:

- `protocol_generator.py` implements a `TakeSnapshot` recipe step that runs
  `gst-launch-1.0 v4l2src device=/dev/video2` on the robot and base64-encodes
  the JPEG into the run log via `IMG_START:`/`IMG_END` comment markers.
- `robot_client.extract_images_from_log()` decodes them back out, and
  `server.py` already calls it after every run.
- But: no SiLA2 command, no UI, and the images land in the Opentrons server's
  local `./images` directory, which nothing serves.

**Built:**
- Images are written to `Results/Images/<run_id>/` instead of a server-local
  folder, and served by the webapp (Results page gallery, and thumbnails on a
  run's step timeline).
- `CaptureImage(Label, AllowRobotMotion)` SiLA2 command, so a snapshot is a
  workflow step. Two acquisition paths: an HTTP probe of the robot's
  `/camera/picture` endpoint first (cheap, no motion), falling back to a
  one-step `TakeSnapshot` protocol. **The fallback runs a protocol, which
  homes the robot** — so it is refused unless `AllowRobotMotion` is true, and
  both the FDL description and the API docs say so.
- Camera settings (`enabled`, `device`, `http_probe`) in the Opentrons
  server's `config.yaml`, so a different `/dev/video*` is a config change.

**Not verified against hardware in this session** (no robot motion without a
human present): the `/camera/picture` HTTP path, and whether `/dev/video2` is
correct on this particular Flex. Both are one config value away, and the
capture command reports precisely which path it used and why the other failed.

---

## Phase 9 — Webapp and frontend ✅

~7,000 lines of JavaScript live inline in Jinja templates; the only shared
module is 50 lines. Instrument icons/colours are hardcoded by substring in two
templates and include an `incubator` that does not exist. The Operator page
polls every 3 s despite an open WebSocket.

**Built:** shared JS modules under `webapp/static/js/` (`labos-core.js`
for fetch/toast/format, `labos-ws.js` for one reconnecting socket with an
event-subscription API, `labos-instruments.js` for icon/colour/identity from
the server catalog). Templates consume them; the invented `incubator` block is
gone; the run timeline reads from `/api/runs/{id}/steps`.

---

## Phase 10 — `labos` CLI ✅

Adding an instrument is nine manual steps including codegen into a temp
directory. `scripts/labos.py` provides `status`, `validate`, `run`,
`instruments`, `new-server` and `codegen`, so the common paths are one command.

---

## Phase 11 — CI ✅

`.github/workflows/tests.yml`: the unit suite on Python 3.11 and 3.12,
`labos doctor`, a check that every server's generated bindings parse, a check
that the saved workflows are current, and `compileall` over everything so a
syntax error in a file no test imports still fails.

Integration tests stay out of CI by construction — a workflow that moves a
robot arm is not something to trigger from a push.

---

## What landed

Eight commits on top of the `a214be9` baseline: **80 files, +8,259 / −5,550**.
Unit suite **530 passing** (from 300), with no hardware required.

| Before | After |
|---|---|
| 9 modules parsing `lab_config.yaml` independently | 1 (`settings.py`), cached, hot-reloaded on save |
| 5 substring matchers for instrument names | 1 (`resolver.py`), ambiguity is an error |
| 2 divergent execution paths, no topological sort | 1 wave ordering, both modes |
| 3 FDL parsers, 2 execution strategies | 1 each |
| Abort set a flag read between steps | Abort reaches the instrument and reports what happened |
| A run lived and died with its HTTP request | A run is a background job with a queryable state |
| `run_results`: a start row and an end row | `run_steps`: one row per attempt, written live |
| Opentrons: 18 commands, 7 of which did nothing | 17 commands that act + 6 properties |
| The camera: implemented, unreachable, unserved | `CaptureImage` + a gallery |
| Adding an instrument: 9 manual steps | `labos.py new-server Centrifuge` |

### Bugs this surfaced

Not the goal, but the reason the goal was worth it. Each of these was found by
consolidating something, and each was live:

1. `workflow.default_timeout: null` was documented as unlimited; the executor
   turned it into a hard 300 s, so no config value could produce an unlimited
   step timeout — on the component that runs multi-hour protocols.
2. `parallel_execution` defaulted to `True` in the schema while the config said
   `False`, and no shipped workflow declared a dependency: deleting the key
   would have dispatched fifteen simultaneous commands to instruments sharing
   one plate.
3. Sequential execution marked a step SKIPPED when its dependency was written
   later in the file.
4. Abort never told the robot anything.
5. The Operator page's "Confirm" never released a ManualStation task: it
   imported stubs that do not exist, at the wrong port, and the ImportError was
   swallowed.
6. Editing a timeout in Settings wrote to disk and did nothing until a restart.
7. `launcher.py` crashed on startup whenever stdout was not a console.
8. The robot's HTTP session was never released on shutdown.
9. Validating a workflow with the instruments switched off blamed the commands
   rather than saying the server was not responding.
10. A parameter name the command does not declare was silently dropped, and the
    command ran without it — `Opentrons_Refill_Test` passed `slot` where the
    command declares `RackSlot`.
11. `Simple_PlateRead`, a shipped example, called a Tecan command that has
    never existed.

---

## Phase 12 — the two deferred items, and a deck that tells the truth ✅

Both items listed below as "not done" were completed on 2026-09-10.

**Inline JavaScript extracted.** Three shared modules under
`webapp/static/js/`: `labos-core.js` (escaping, fetch with real error messages,
formatting, toasts, the page log), `labos-ws.js` (one reconnecting socket with
per-type subscriptions, replacing a 40-line if/else chain every page shared
whether it cared or not), `labos-params.js` (parameter forms, superseding
param-form-shared.js). Two live bugs fell out: the websocket heartbeat called
`send()` unguarded on a timer, throwing every 30 seconds for as long as the
socket was down; and simultaneous toasts rendered exactly on top of each other.

The concern that stopped this before — no test can prove a refactor of
untested UI code — is answered as far as it can be without a browser:
`tests/unit/test_frontend_assets.py` verifies every module and every inline
block is structurally sound (balanced brackets, no unterminated literal, a
scanner that understands nested template literals and regex-versus-division),
which is precisely the failure mode string surgery on templates produces and
which turns into a blank page nothing else would catch.

**Observable properties are subscribed to.** `src/property_watch.py` subscribes
to every observable property of every instrument and pushes changes over the
websocket; unobservable ones are read on a slow timer. `GET /api/properties`
returns everything already known for a page loading mid-session.

**And the pickers now describe the real deck.** `MoveLabware` could not work:
its labware picker listed six invented load names matching nothing Opentrons
knows, and its destination picker listed the discovered *instruments*, because
the parameter is called `NewLocation`. `src/deck.py` answers both from the
active hardware config, the move is validated before the gripper starts, and
the config is rewritten afterwards — otherwise it lies from the moment a plate
moves.

**`CaptureTimelapse`** answers "can it record video": it cannot, because every
frame travels base64-encoded through the run log, which is fine for a
photograph and impractical for a clip. A timelapse is what the transport
supports, in one run so the robot homes once.

---

## Not done, and why

- **The remaining inline JavaScript.** The shared helpers are extracted; what
  is left in each template is that page's own layout code, which has no second
  copy to drift from. Moving it out too would be motion without benefit.
- **Verifying the camera against the robot.** The protocol fallback homes the
  robot, which needs someone at the bench. The HTTP path and the `/dev/video2`
  device node are both unverified on this particular Flex, both are one config
  value away, and the command reports exactly which path it took.
- **A run queue.** One run at a time is right for shared physical instruments;
  queueing is a product decision, not a technical gap.

---

## Deliberately out of scope

- **Authentication.** CORS is open and there is no token. On an isolated lab
  network this is a considered trade-off, not an oversight; adding auth
  without knowing the deployment topology would break the operator flow for
  no gain. Documented in `OPERATIONS.md` instead.
- **Rewriting the recipe generator.** 2,000 lines that work and are tested.
- **Touching the Tecan C# bridge.** Rebuilt and verified against the real
  instrument on 2026-09-01; out of scope here.
- **A workflow loop/conditional construct.** A real feature request, not a
  defect; it needs its own design.
