# LabOS — Hardware Abstraction Layer (HAL) & Recipes

## What the HAL Does

The Hardware Abstraction Layer separates a recipe's **liquid handling steps** from the **physical deck layout** (which slot holds which labware, which pipette is mounted where). A HAL config is selected once (it persists across restarts via `Library/HardwareConfig/.active`), and every subsequent `ExecuteRecipe` call runs against whatever HAL is currently active — recipes and HAL configs are not paired per-call.

```
Recipe JSON (Steps)   +   Active HAL Config   →   Generated Opentrons Python protocol
```

Most recipes in this project are **generated**, not hand-written — normally via the web UI's **Recipe Generator** (`/batch` or `/recipes`), which converts an Excel pipetting plan into a recipe JSON automatically. This document describes the underlying format for anyone who needs to hand-edit a recipe or understand what the generator produces.

---

## HAL Configuration Format

A HAL config (`Library/HardwareConfig/*.json`) declares the pipettes mounted and the labware loaded on each deck slot. Real examples ship in the repo (`Standard_Flex_Setup.json`, `SerialDilution_Setup.json`, and several auto-generated `Generic_HAL_*.json` files from past Recipe Generator sessions) — inspect one of those directly for the exact current schema rather than relying on a hand-written example here, since the generator's HAL output evolves alongside the recipe format.

The active config is tracked in a plain-text marker file, `Library/HardwareConfig/.active`, containing just the filename of the currently selected config. This is written whenever you click "Set Active" on the **Hardware** page, and read by the Opentrons server at startup (overriding `config.yaml`'s static `hardware.default_config`) — so a HAL switch survives a server restart.

**Caution**: setting a HAL config "active" from the webapp succeeds even if the Opentrons server or robot is offline at that moment — the webapp best-effort pushes the change live via `LoadHardwareConfig`, and reports `applied: false` in that case without failing the whole request. If you switch HAL while the robot is offline, re-apply it (or restart the Opentrons server) once it's back online, rather than assuming the deck state matches the webapp.

---

## Recipe Format

A recipe is a JSON file (`Library/Recipes/*.json`) with a `Steps` array of Opentrons commands, plus optional `Metadata` (produced automatically by the Recipe Generator — required volumes, waste estimates, notes).

### Running one step without a recipe file

The operations common enough to want on their own have their own SiLA2
command — `MoveLabware`, `ShakeModule`, `SetModuleLatch`,
`SetModuleTemperature` — which validate against the loaded deck and offer real
pickers in the workflow builder. See [SERVERS.md](SERVERS.md). Anything else is
a recipe file, however short.

### Supported Step Commands

**High-level (automatic tip management)**
| Command | Description |
|---------|-------------|
| `Transfer` | Single source → single destination. Optional `"VerifyLiquid": true` — see below |
| `Distribute` | Single source → multiple destinations. Optional `"VerifyLiquid": true` — see below |
| `Consolidate` | Multiple sources → single destination. Optional `"VerifyLiquid": true` — see below |

`VerifyLiquid` on these three toggles Opentrons' native `pipette.liquid_presence_detection` for the duration of that one call: the Flex pipette physically probes for the liquid surface with its pressure sensor before aspirating, positions itself just above it, and only then aspirates — no manual retry/offset logic and no safety-margin config needed, the hardware does the probing and enforces its own calibrated limits. If it doesn't find liquid, the run enters Flex's `awaiting-recovery` state; `robot_client.py::wait_for_run_completion()` recognizes this and stops the run immediately with a clear message (see [SERVERS.md](SERVERS.md)) rather than letting it sit waiting for touchscreen intervention.

**Important limitation** (confirmed against Opentrons' own source, `instrument_context.py`): this native probe only re-arms when the pipette's tip is both empty AND "clean" (`has_clean_tip()`, true only right after `pick_up_tip()`, false from the first aspirate/dispense onward) — so within a single `Distribute`/`Consolidate` call that reloads the same tip several times, the probe fires only on the very first aspirate, never again on later reloads. `Transfer` is unaffected (a fresh tip per destination already, by construction). To get real per-reload coverage on `Distribute`, the Recipe Generator emits one `PickUpTip`/`Distribute`/`DropTip` triplet **per reload trip** instead of one for the whole reagent whenever "Verify aspiration" is checked — a fresh, clean tip every trip re-arms the probe every time. Trade-off: more tips consumed (one per trip instead of one per reagent) and a somewhat slower run.

**Multi-well liquid fallback**: whenever "Verify aspiration" is on for a reagent, it automatically gets extra "backup" wells in the same reservoir — no count to type in. The number is sized the same way the existing reservoir-capacity overflow-split is (`ceil(with_excess_ul / well_capacity)`, at least 1): enough wells to cover the reagent's own required volume *again*, in case the primary well(s) turn out to be a total loss. Backups are drawn from whatever the primary/overflow well assignment didn't already claim (collision-free even when several reagents share a reservoir) and, since a reservoir's own well ordering already groups physically-adjacent wells together, consecutive free wells end up physically adjacent too. If the reservoir doesn't have enough free wells left, whatever's available is used and a note in "Notes" says so - it never fails generation. Each reload trip's `Source` becomes a candidate list whenever at least one backup was assigned; before aspirating, the generated protocol tries each candidate in order with `detect_liquid_presence()` (a boolean check, never raises) and uses the first one with liquid, remembering how far it got so later trips don't re-probe wells already found dry. Only if every candidate is dry does the run stop, via the same `require_liquid_presence()` → `awaiting-recovery` path as the single-well case above — same message, whether it's one well or an exhausted fallback list. Not available for `Consolidate` (its `Sources` already means "merge these", not "try these in order").

**Exposed in the webapp**: a single "Verify aspiration" checkbox per reagent in the Recipe Generator's advanced options — backup wells (if any) are assigned automatically and listed in "Quantità richieste / note" after generating.

**Low-level (explicit control)**
| Command | Description |
|---------|-------------|
| `Aspirate` | Draw liquid from a well. Optional `"VerifyLiquid": true` calls `detect_liquid_presence()` once and manually raises a Python exception if no liquid is found — a different, older mechanism from the one above (immediate hard failure vs. `awaiting-recovery` + programmatic stop), kept as-is since it's already tested and working. **No webapp UI control for this one specifically, JSON-only** — the Recipe Generator never emits a standalone `Aspirate` step, only `Transfer`/`Distribute` |
| `Dispense` | Deliver liquid to a well |
| `Mix` | Aspirate+dispense N times in place |
| `BlowOut`, `TouchTip`, `AirGap` | Standard liquid-handling refinements |

**Tip Management**
| Command | Description |
|---------|-------------|
| `PickUpTip`, `DropTip`, `DropTipInPlace`, `ReturnTip`, `ConsumeTips` | Standard tip lifecycle. `DropTip` also accepts `"Force": true` (used internally by the server's automatic leftover-tip guard — see [SERVERS.md](SERVERS.md)) |

**Module & Deck Control**
| Command | Description |
|---------|-------------|
| `MoveLabware` | Move a plate within the deck via the gripper. Accepts optional `PickUpOffset`/`DropOffset` (`{"x":.., "y":.., "z":..}`, millimeters, additive on top of the labware's own calibrated default) — **JSON-only, no webapp UI control** to set these. **Default when omitted**: the code doesn't send an explicit `(0,0,0)` — it simply omits the kwarg entirely, which is functionally equivalent to zero *additional* offset. The labware's own built-in `gripperOffsets.default` (from its Opentrons labware definition) always applies regardless, on top of nothing extra. |
| `Delay`, `Comment`, `Pause` | Control/annotation steps |
| `TakeSnapshot` | Photograph the deck with the robot's on-board camera. Optional `Filename` and `Device` (the V4L2 node on the robot; defaults to the server's `camera.device`). The frame is base64-encoded into the run log and decoded back into `Results/Images/`, where the web interface serves it under **Results → Deck photos**. `CaptureImage` is the standalone command version — see [SERVERS.md](SERVERS.md#captureimage-the-on-board-camera) |
| Heater-shaker / temperature module steps | Emitted automatically by the Recipe Generator's module-treatment engine when a treatment is configured — see below |

### Module Treatments (Recipe Generator feature)

The Recipe Generator's "+ Add step" UI lets you insert a module treatment (e.g. heater-shaker mix, temperature hold) before the first phase, after any phase, or after the last. This is backed by a treatment catalog in `src/excel_recipe_parser.py` and is **fully wired into the UI** — this is the one advanced feature that's genuinely easy to use without editing JSON by hand.

### Reservoir Sharing

When multiple reagents share a physical reservoir type, the Recipe Generator's `reservoirType`/`numReservoirs` controls (also UI-wired) split reagent distribution into per-reservoir chunks automatically, inserting a module treatment between chunks if configured.

### Liquid Classes

Liquid classes define aspiration/dispense parameters for non-aqueous liquids, stored in `Library/LiquidClasses/*.json`:

| Class | Use Case |
|-------|----------|
| `Aqueous` | Default, water-like liquids |
| `Viscous` | Glycerol, PEG, hydrogels |
| `HighlyViscous` | Gels, pastes |
| `Volatile` | Ethanol, acetone, DMSO |
| `Foaming` | Detergents, SDS |

Selectable per-reagent in the Recipe Generator UI, along with advanced per-reagent options (air gap, blow-out, mix-after cycles/volume).

### Waste Estimates

The Recipe Generator estimates per-reagent disposal waste (based on Opentrons' documented `distribute()` behavior) and folds it into `Metadata.RequiredVolumes[reagent].estimated_waste_ul` plus a plain-text line in `Metadata.Notes`. **This is surfaced in the UI only as a line inside the collapsible "Notes" section** — there is no dedicated waste widget, so remember to expand Notes rather than assume there's no output. After a real run, the Opentrons server's run archive compares this estimate against actual aspirate/dispense volumes from the run log.

### Tip-Presence Safety (fully automatic, no UI)

Because the installed robot firmware has no real tip-presence sensor, the Opentrons server maintains its own persisted per-mount tip-tracking state and automatically prepends a forced drop-tip step to any recipe run if it can't confirm a mount is clear. This happens transparently — you will not see it in the recipe JSON you generated, only in the executed protocol's log.

---

## Tip State Persistence

Two independent, automatically-maintained files track tip state (both under the Opentrons server's directory, not in `Library/`):

- `tip_state.json` — how many tips remain per rack (drives the Operator page's refill prompts)
- `tip_mount_state.json` — per-mount "might still have a tip" state (drives the safety guard above)

Both survive a server restart (atomic writes) and are reconciled automatically from the robot's own command log after every run, including aborted/emergency-stopped ones.

---

## Excel-to-Recipe Converter (Recipe Generator)

The primary way to create recipes. From the **Recipe Generator** page (`/batch` or `/recipes` — same page, two URLs):

1. Pick an existing `.xlsx` from `Library/Recipes/excel/`, or upload one. Expected format: one column identifies wells, other columns are reagents with volumes (column header contains a volume unit, e.g. "µL").
2. Group reagent columns into **Phases**, optionally with a delay between phases.
3. Configure per-reagent liquid class and advanced aspirate/dispense options.
4. Configure reservoir sharing (type + count) if reagents share physical reservoirs.
5. Insert module treatments (heater-shaker, temperature) between phases if needed.
6. Generate — choose "Multi-Recipe + Workflow" (one recipe per phase, chained by an auto-generated workflow) or "Single Recipe with Delays" (one recipe, `Delay` steps between phases).
7. The generator can also produce a matching generic HAL config alongside the recipe.

One feature from this pipeline still exists only in the generated JSON, with **no corresponding webapp control**: gripper offsets (`MoveLabware.PickUpOffset`/`DropOffset`) — set by hand-editing the generated recipe if needed. `VerifyLiquid` on `Transfer`/`Distribute` **is** wired into step 3 above (the "Verify aspiration" checkbox, with backup-well fallback assigned automatically); only the standalone-`Aspirate` variant of `VerifyLiquid` remains JSON-only, since the Recipe Generator never emits that command.
