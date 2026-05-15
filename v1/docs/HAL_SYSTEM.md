# LabOS — Hardware Abstraction Layer (HAL)

## What the HAL Does

The Hardware Abstraction Layer separates the **logical description** of a liquid handling operation (the recipe) from the **physical implementation** (which deck slot, which pipette, how many tips). The same recipe JSON runs correctly on any compatible Opentrons Flex deck configuration without modification.

```
Recipe JSON          +   HAL Config JSON     →   Opentrons Python protocol
(what to do)             (where things are)      (how to do it on this deck)
```

---

## Recipe Format

A recipe defines logical liquid handling without referencing physical positions:

```json
{
  "id": "elisa_coating",
  "name": "ELISA Coating Step",
  "description": "Distribute coating antibody to all wells",
  "inputs": {
    "source_plate": { "type": "reservoir", "wells": ["A1"] },
    "dest_plate":   { "type": "96_well_plate" }
  },
  "tip_strategy": "new_tip_per_source",
  "operations": [
    {
      "command": "Distribute",
      "source": "source_plate.A1",
      "destinations": "dest_plate.all",
      "volume": 100,
      "liquid_class": "Aqueous"
    }
  ]
}
```

### What a recipe specifies
- Source and destination **labware roles** (not deck slots)
- **Volumes** and **well addresses**
- **Tip strategy** (new tip per source, reuse, etc.)
- **Liquid class** reference (for viscous, volatile, foaming liquids)
- **Operation sequence**

### What a recipe does NOT specify
- Which deck slot holds the source plate
- Which pipette to use
- Exact tip rack positions
- Number of tips available

---

## HAL Configuration Format

A HAL config maps logical labware names to physical deck positions:

```json
{
  "id": "deck_config_A",
  "name": "Standard Screening Deck",
  "pipettes": {
    "left":  { "type": "flex_1channel_1000", "mount": "left" },
    "right": { "type": "flex_8channel_50",  "mount": "right" }
  },
  "labware": {
    "source_plate": {
      "type":  "opentrons_1_reservoir_290ml",
      "slot":  "D1"
    },
    "dest_plate": {
      "type":  "opentrons_96_wellplate_200ul_pcr_full_skirt",
      "slot":  "C2"
    },
    "tip_rack_1000": {
      "type":  "opentrons_flex_96_tiprack_1000ul",
      "slot":  "A1"
    },
    "tip_rack_50": {
      "type":  "opentrons_flex_96_tiprack_50ul",
      "slot":  "B1"
    }
  },
  "tip_state_file": "Library/TipState/deck_config_A_tips.json"
}
```

HAL configs are stored in `Library/HardwareConfig/`.

---

## Translation Pipeline

When `ExecuteRecipe` is called, the `RecipeTranslator` performs three steps:

### Step 1: Labware Resolution
Each logical name in the recipe (`"source_plate"`, `"dest_plate"`) is looked up in the HAL config. If any logical name is missing from the config, execution is blocked with an explicit error identifying the missing element.

### Step 2: Pipette Selection
The translator selects the appropriate pipette based on the requested volume range:
- Single-channel 1000 µL → volumes 20–1000 µL, single-well operations
- 8-channel 50 µL → volumes 1–50 µL, column-wise operations
- Selection falls back to the left pipette if volume fits both

### Step 3: Protocol Generation
An executable Opentrons Python protocol is generated and uploaded to the robot via its HTTP API on port 31950. The generated protocol includes:
- All `load_labware()` calls with resolved deck slots
- All `pick_up_tip()` / `drop_tip()` calls based on tip strategy
- All `aspirate()` / `dispense()` / `mix()` calls with volumes and positions
- All `delay()` calls for liquid class timing

---

## Supported Recipe Commands

### High-Level (automatic tip management)
| Command | Description |
|---------|-------------|
| `Transfer` | Single source → single destination, one-to-one |
| `Distribute` | Single source → multiple destinations |
| `Consolidate` | Multiple sources → single destination |

### Low-Level (explicit control)
| Command | Description |
|---------|-------------|
| `Aspirate` | Draw liquid from a well |
| `Dispense` | Deliver liquid to a well |
| `Mix` | Aspirate+dispense N times in place |
| `BlowOut` | Expel remaining volume |
| `TouchTip` | Touch tip to well wall after dispense |
| `AirGap` | Draw air into tip to prevent dripping |

### Tip Management
| Command | Description |
|---------|-------------|
| `PickUpTip` | Pick tip from specified rack position |
| `DropTip` | Drop tip to trash |
| `DropTipInPlace` | Drop tip without moving to trash |
| `ReturnTip` | Return tip to its original position |
| `ConsumeTips` | Mark tips as used (update tip state) |
| `VerifyTipPresence` | Assert tip is attached |

### Control
| Command | Description |
|---------|-------------|
| `Delay` | Wait N seconds (fixed or LfD-linked) |
| `Comment` | Insert a comment in the protocol |
| `Pause` | Pause protocol and wait for manual resume |
| `MoveLabware` | Move a plate within the deck (gripper) |

---

## Liquid Classes

Liquid classes define aspiration and dispense parameters for non-aqueous liquids. They are stored in `Library/LiquidClasses/` as JSON files.

Pre-defined classes:

| Class | Use Case | Modifications |
|-------|----------|---------------|
| `Aqueous` | Default for water-like liquids | Standard rates |
| `Viscous` | Glycerol, PEG, hydrogels | Reduced speeds, extra blowout |
| `HighlyViscous` | Gels, pastes | Very slow speeds, multiple mixes |
| `Volatile` | Ethanol, acetone, DMSO | Faster aspiration, air gap |
| `Foaming` | Detergents, SDS | Slow dispense, no mix |

Custom classes can be added by creating a new JSON file in `Library/LiquidClasses/`. The visual designer auto-populates the liquid class dropdown from this directory.

---

## Tip State Persistence

To recover from crashes mid-protocol, tip consumption is persisted after each protocol execution:

```json
{
  "tip_rack_1000": {
    "A1": false, "B1": false, "C1": false,
    "D1": true, "E1": true, ...
  },
  "tip_rack_50": { ... },
  "last_updated": "2025-05-15T14:23:01"
}
```

`false` = tip already consumed. On restart, the `RecipeTranslator` reads this file and skips consumed positions rather than re-picking used tips.

---

## Plate Tracking

Every recipe execution is logged with a `plate_id`:

```json
{
  "plate_id": "P2025-05-15-001",
  "recipe": "elisa_coating.json",
  "hal_config": "deck_config_A.json",
  "timestamp": "2025-05-15T14:20:00",
  "wells_dispensed": 96,
  "operator": "workflow_executor"
}
```

This record is stored in `Results/plate_tracking.json` and links to the Tecan measurement output that references the same `plate_id`, enabling full traceability from pipetting to result.

---

## Excel-to-Recipe Converter

For researchers who design experiments in spreadsheets, the converter at `/batch` auto-generates recipes from pipetting plans:

1. Upload an Excel file where rows = wells and columns = reagents with volumes
2. The parser detects well columns (header contains "Well") and reagent columns (header contains µL or uL)
3. Optionally group columns into phases with inter-phase delays
4. Download the generated recipe JSON ready for use in any workflow
