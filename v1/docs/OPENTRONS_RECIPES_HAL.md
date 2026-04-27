# Opentrons Recipes & HAL Guide

Complete guide for writing Opentrons Flex recipes using the Hardware Abstraction Layer (HAL).

---

## Table of Contents

1. [Overview](#overview)
2. [HAL System](#hal-system)
3. [Hardware Configurations](#hardware-configurations)
4. [Recipe Structure](#recipe-structure)
5. [Available Commands](#available-commands)
6. [Labware References](#labware-references)
7. [Advanced Features](#advanced-features)
8. [Liquid Classes](#liquid-classes)
9. [Examples](#examples)

---

## Overview

The BicoccaLab system uses a **Hardware Abstraction Layer (HAL)** to decouple recipes from specific hardware configurations. This means:

- **Recipes** describe *what* to do (transfer 100µL from A1 to B1)
- **Hardware Configs** describe *how* to do it (which pipette, which deck slot)

This separation allows the same recipe to run on different hardware setups without modification.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Recipe (JSON)                            │
│   "Transfer 100µL from Reservoir:A1 to Plate:A1-H1"             │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Hardware Config (JSON)                        │
│   Reservoir → opentrons_96_wellplate... @ Slot C2               │
│   Plate → corning_96_wellplate... @ Slot D1                     │
│   Pipette → flex_8channel_1000 @ left mount                     │ 
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│              Generated Opentrons Protocol (Python)              │
└─────────────────────────────────────────────────────────────────┘
```

---

## HAL System

### How It Works

1. **User writes a recipe** using logical names (e.g., `Plate`, `Reservoir`)
2. **HAL maps logical names** to physical labware via hardware config
3. **Protocol Generator** creates executable Python code
4. **Server executes** the protocol on the Opentrons Flex

### Benefits

| Feature | Without HAL | With HAL |
|---------|-------------|----------|
| Change deck layout | Edit every recipe | Edit one config file |
| Use different pipettes | Rewrite protocols | Select different config |
| Labware substitution | Manual code changes | Update config mapping |
| Multi-setup support | Duplicate recipes | One recipe, many configs |

---

## Hardware Configurations

Hardware configurations are stored in `Library/HardwareConfig/`. Each file defines a complete deck setup.

### Configuration Structure

```json
{
  "ConfigName": "Standard_Flex_Setup",
  "Description": "Standard configuration for routine liquid handling",
  
  "Pipettes": {
    "left": "flex_8channel_1000",
    "right": "flex_1channel_50"
  },
  
  "Labware": {
    "TipRack1000": {
      "LoadName": "opentrons_flex_96_tiprack_1000ul",
      "Slot": "A2",
      "AdapterName": null
    },
    "TipRack50": {
      "LoadName": "opentrons_flex_96_tiprack_50ul",
      "Slot": "A3",
      "AdapterName": null
    },
    "Reservoir": {
      "LoadName": "nest_12_reservoir_15ml",
      "Slot": "C2",
      "AdapterName": null
    },
    "Plate": {
      "LoadName": "corning_96_wellplate_360ul_flat",
      "Slot": "D1",
      "AdapterName": null
    }
  },
  
  "Modules": {
    "HeaterShaker": {
      "Type": "heaterShakerModuleV1",
      "Slot": "D3",
      "Labware": {
        "LoadName": "opentrons_96_wellplate_200ul_pcr_full_skirt",
        "AdapterName": "opentrons_96_flat_bottom_adapter"
      }
    }
  },
  
  "TrashBin": "A3"
}
```

### Available Configurations

| Config Name | Description | Use Case |
|-------------|-------------|----------|
| `Standard_Flex_Setup` | Basic 8-channel + 1-channel | General liquid handling |
| `ELISA_Setup` | Optimized for ELISA plates | Immunoassays |
| `SerialDilution_Setup` | Multiple reservoirs | Serial dilution protocols |

### Switching Configurations

```bash
# Via Lab Console
orchestrator> exec opentrons OpentronsFlex SwitchConfig ELISA_Setup

# Or in workflow JSON
{
  "command": "SwitchConfig",
  "params": { "config_name": "ELISA_Setup" }
}
```

---

## Recipe Structure

Recipes are JSON files stored in `Library/Recipes/`.

### Basic Structure

```json
{
  "name": "My Protocol",
  "description": "Protocol description",
  "author": "Your Name",
  "version": "1.0",
  
  "requirements": {
    "labware": ["Plate", "Reservoir", "TipRack1000"],
    "pipettes": ["8channel"]
  },
  
  "steps": [
    {
      "action": "transfer",
      "params": {
        "volume": 100,
        "source": "Reservoir:A1",
        "destination": "Plate:A1-H1"
      }
    }
  ]
}
```

### Recipe Types

#### 1. Requirements-based (Recommended)

Specifies what labware/pipettes are needed. HAL validates against current config.

```json
{
  "requirements": {
    "labware": ["Plate", "Reservoir"],
    "pipettes": ["8channel"]
  },
  "steps": [...]
}
```

#### 2. Self-contained with Pipettes

Includes pipette specifications for standalone execution.

```json
{
  "pipettes": {
    "left": "flex_8channel_1000"
  },
  "labware": {
    "Plate": {
      "load_name": "corning_96_wellplate_360ul_flat",
      "slot": "D1"
    }
  },
  "steps": [...]
}
```

#### 3. Standalone (Full Protocol)

Complete protocol definition, doesn't use HAL.

```json
{
  "metadata": {
    "protocolName": "Standalone Protocol",
    "apiLevel": "2.15"
  },
  "pipettes": {...},
  "labware": {...},
  "steps": [...]
}
```

---

## Available Commands

### Transfer

Move liquid from source to destination(s).

```json
{
  "action": "transfer",
  "params": {
    "volume": 100,
    "source": "Reservoir:A1",
    "destination": "Plate:A1-H12",
    "new_tip": "always",
    "mix_before": [3, 50],
    "mix_after": [2, 50],
    "blow_out": true,
    "touch_tip": true
  }
}
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `volume` | number | required | Volume in µL |
| `source` | string | required | Source well(s) |
| `destination` | string | required | Destination well(s) |
| `new_tip` | string | "always" | "always", "once", "never" |
| `mix_before` | [int, number] | null | [iterations, volume] before aspirate |
| `mix_after` | [int, number] | null | [iterations, volume] after dispense |
| `blow_out` | bool | false | Blow out after dispense |
| `touch_tip` | bool | false | Touch tip after dispense |
| `air_gap` | number | 0 | Air gap in µL |

### Distribute

Distribute from one source to multiple destinations.

```json
{
  "action": "distribute",
  "params": {
    "volume": 50,
    "source": "Reservoir:A1",
    "destinations": ["Plate:A1-A12", "Plate:B1-B12"],
    "disposal_volume": 10,
    "new_tip": "once"
  }
}
```

### Consolidate

Consolidate from multiple sources to one destination.

```json
{
  "action": "consolidate",
  "params": {
    "volume": 20,
    "sources": ["Plate:A1-A12"],
    "destination": "Reservoir:A1",
    "new_tip": "always"
  }
}
```

### Mix

Mix contents of well(s).

```json
{
  "action": "mix",
  "params": {
    "repetitions": 5,
    "volume": 100,
    "location": "Plate:A1-H1"
  }
}
```

### Aspirate / Dispense

Low-level pipetting commands.

```json
{
  "action": "aspirate",
  "params": {
    "volume": 100,
    "location": "Reservoir:A1",
    "rate": 1.0
  }
}
```

```json
{
  "action": "dispense",
  "params": {
    "volume": 100,
    "location": "Plate:A1",
    "rate": 1.0,
    "push_out": 5
  }
}
```

### Pick Up / Drop Tip

Manual tip management.

```json
{
  "action": "pick_up_tip",
  "params": {
    "location": "TipRack1000:A1"
  }
}
```

```json
{
  "action": "drop_tip",
  "params": {
    "location": "trash"
  }
}
```

### Module Commands

#### Heater-Shaker

```json
{
  "action": "heater_shaker_set_temperature",
  "params": {
    "module": "HeaterShaker",
    "temperature": 37
  }
}
```

```json
{
  "action": "heater_shaker_shake",
  "params": {
    "module": "HeaterShaker",
    "speed": 500,
    "duration": 60
  }
}
```

#### Move Labware

```json
{
  "action": "move_labware",
  "params": {
    "labware": "Plate",
    "destination": "HeaterShaker",
    "use_gripper": true
  }
}
```

### Delay

```json
{
  "action": "delay",
  "params": {
    "seconds": 30,
    "message": "Incubating..."
  }
}
```

### Comment

```json
{
  "action": "comment",
  "params": {
    "message": "Starting wash steps"
  }
}
```

---

## Labware References

### Well Notation

| Format | Example | Description |
|--------|---------|-------------|
| Single well | `Plate:A1` | Well A1 of Plate |
| Row | `Plate:A1-A12` | Wells A1 through A12 |
| Column | `Plate:A1-H1` | Wells A1 through H1 |
| Range | `Plate:A1-H12` | All 96 wells |
| Multiple | `["Plate:A1", "Plate:B1"]` | Specific wells |

### Logical Names

Use logical names defined in your hardware config:

```json
// In hardware config
"Labware": {
  "SourcePlate": { "LoadName": "...", "Slot": "C1" },
  "DestPlate": { "LoadName": "...", "Slot": "D1" }
}

// In recipe
"source": "SourcePlate:A1",
"destination": "DestPlate:A1"
```

---

## Advanced Features

### Variables

Use variables for dynamic values:

```json
{
  "variables": {
    "sample_volume": 50,
    "num_replicates": 3
  },
  "steps": [
    {
      "action": "transfer",
      "params": {
        "volume": "${sample_volume}",
        "source": "Reservoir:A1",
        "destination": "Plate:A1-A${num_replicates}"
      }
    }
  ]
}
```

### Loops

Repeat steps:

```json
{
  "action": "loop",
  "params": {
    "iterations": 3,
    "steps": [
      {
        "action": "transfer",
        "params": { "volume": 100, "source": "Reservoir:A1", "destination": "Plate:A1" }
      },
      {
        "action": "delay",
        "params": { "seconds": 60 }
      }
    ]
  }
}
```

### Conditionals

Execute steps based on conditions:

```json
{
  "action": "if",
  "params": {
    "condition": "${include_wash}",
    "then": [
      { "action": "transfer", "params": { "volume": 200, "source": "WashBuffer:A1", "destination": "Plate:A1-H12" } }
    ],
    "else": [
      { "action": "comment", "params": { "message": "Skipping wash" } }
    ]
  }
}
```

### Tip Tracking

The system automatically tracks tip usage. To reset:

```json
{
  "action": "reset_tip_tracking",
  "params": {
    "tiprack": "TipRack1000"
  }
}
```

### Liquid Classes

Liquid classes optimize pipetting for fluids with different physical properties. Predefined classes are stored in `Library/LiquidClasses/`.

#### Available Liquid Classes

| Class | API LoadName | Rate | Description |
|-------|--------------|------|-------------|
| `Aqueous` | `water` | 1.0 | Standard aqueous liquids |
| `Viscous` | `glycerol_50` | 0.25 | 50% glycerol, PEG solutions |
| `Volatile` | `ethanol_80` | 1.0 | Ethanol, acetone |
| `HighlyViscous` | - | 0.15 | >70% glycerol, oils |
| `Foaming` | - | 0.5 | Detergents, proteins |

#### Using Liquid Classes

**Method 1: Native Opentrons API (Recommended)**

```json
{
  "action": "transfer_with_liquid_class",
  "params": {
    "volume": 100,
    "source": "GlycerolStock:A1",
    "destination": "Plate:A1",
    "liquid_class": "Viscous"
  }
}
```

**Method 2: LiquidClass Parameter**

```json
{
  "action": "transfer",
  "params": {
    "volume": 100,
    "source": "GlycerolStock:A1",
    "destination": "Plate:A1",
    "liquid_class": "Viscous"
  }
}
```

**Method 3: Manual Rate Control**

```json
{
  "action": "aspirate",
  "params": {
    "volume": 100,
    "location": "GlycerolStock:A1",
    "rate": 0.25
  }
}
```

#### Creating Custom Liquid Classes

Add a JSON file to `Library/LiquidClasses/`:

```json
{
  "name": "MyViscousLiquid",
  "description": "Custom liquid for specific application",
  "basedOn": "glycerol_50",
  
  "parameters": {
    "aspirate_rate": 0.2,
    "dispense_rate": 0.25,
    "blow_out": true,
    "touch_tip": true,
    "air_gap": 5,
    "delay_aspirate": 1.0,
    "delay_dispense": 0.5
  }
}
```

#### Liquid Class Parameters

| Parameter | Range | Default | Description |
|-----------|-------|---------|-------------|
| `aspirate_rate` | 0.05-2.0 | 1.0 | Relative speed (1.0 = normal) |
| `dispense_rate` | 0.05-2.0 | 1.0 | Relative speed |
| `blow_out` | bool | false | Blow out after dispense |
| `touch_tip` | bool | false | Touch tip after dispense |
| `air_gap` | 0-20 µL | 0 | Air gap after aspirate |
| `push_out` | 0-20 µL | 2.0 | Extra push in dispense |
| `delay_aspirate` | 0-10 s | 0.2 | Pause after aspirate |
| `delay_dispense` | 0-10 s | 0.2 | Pause after dispense |
| `mix_after` | [n, frac] | null | Mix cycles after dispense |

---

## Examples

### Example 1: Simple Transfer

Transfer 100µL from reservoir to all wells of a 96-well plate.

```json
{
  "name": "Simple_Plate_Fill",
  "description": "Fill all wells with 100µL buffer",
  "version": "1.0",
  
  "requirements": {
    "labware": ["Reservoir", "Plate", "TipRack1000"],
    "pipettes": ["8channel"]
  },
  
  "steps": [
    {
      "action": "transfer",
      "params": {
        "volume": 100,
        "source": "Reservoir:A1",
        "destination": "Plate:A1-H12",
        "new_tip": "once"
      }
    }
  ]
}
```

### Example 2: Serial Dilution

Perform a 1:2 serial dilution across a plate row.

```json
{
  "name": "Serial_Dilution_1to2",
  "description": "1:2 serial dilution across columns",
  "version": "1.0",
  
  "requirements": {
    "labware": ["Reservoir", "Plate", "TipRack1000"],
    "pipettes": ["8channel"]
  },
  
  "steps": [
    {
      "action": "comment",
      "params": { "message": "Adding diluent to columns 2-12" }
    },
    {
      "action": "transfer",
      "params": {
        "volume": 100,
        "source": "Reservoir:A1",
        "destination": "Plate:A2-H12",
        "new_tip": "once"
      }
    },
    {
      "action": "comment",
      "params": { "message": "Adding sample to column 1" }
    },
    {
      "action": "transfer",
      "params": {
        "volume": 200,
        "source": "Reservoir:A2",
        "destination": "Plate:A1-H1",
        "new_tip": "always"
      }
    },
    {
      "action": "comment",
      "params": { "message": "Performing serial dilution" }
    },
    {
      "action": "transfer",
      "params": {
        "volume": 100,
        "source": "Plate:A1-H1",
        "destination": "Plate:A2-H2",
        "mix_after": [3, 80],
        "new_tip": "always"
      }
    },
    {
      "action": "transfer",
      "params": {
        "volume": 100,
        "source": "Plate:A2-H2",
        "destination": "Plate:A3-H3",
        "mix_after": [3, 80],
        "new_tip": "always"
      }
    }
  ]
}
```

### Example 3: ELISA Protocol

Complete ELISA workflow with wash steps.

```json
{
  "name": "ELISA_Protocol",
  "description": "Complete ELISA with coating, blocking, and detection",
  "version": "1.0",
  
  "variables": {
    "coating_volume": 100,
    "sample_volume": 50,
    "wash_volume": 200,
    "wash_cycles": 3
  },
  
  "requirements": {
    "labware": ["CoatingBuffer", "WashBuffer", "SamplePlate", "ELISAPlate", "TipRack1000"],
    "pipettes": ["8channel"]
  },
  
  "steps": [
    {
      "action": "comment",
      "params": { "message": "=== COATING ===" }
    },
    {
      "action": "transfer",
      "params": {
        "volume": "${coating_volume}",
        "source": "CoatingBuffer:A1",
        "destination": "ELISAPlate:A1-H12",
        "new_tip": "once"
      }
    },
    {
      "action": "comment",
      "params": { "message": "Incubate overnight at 4°C" }
    },
    {
      "action": "delay",
      "params": { "seconds": 5, "message": "Simulating incubation..." }
    },
    {
      "action": "comment",
      "params": { "message": "=== WASH ===" }
    },
    {
      "action": "loop",
      "params": {
        "iterations": "${wash_cycles}",
        "steps": [
          {
            "action": "transfer",
            "params": {
              "volume": "${wash_volume}",
              "source": "WashBuffer:A1",
              "destination": "ELISAPlate:A1-H12",
              "new_tip": "once"
            }
          },
          {
            "action": "delay",
            "params": { "seconds": 30 }
          },
          {
            "action": "aspirate",
            "params": {
              "volume": "${wash_volume}",
              "location": "ELISAPlate:A1-H12"
            }
          },
          {
            "action": "dispense",
            "params": {
              "volume": "${wash_volume}",
              "location": "trash"
            }
          }
        ]
      }
    },
    {
      "action": "comment",
      "params": { "message": "=== ADD SAMPLES ===" }
    },
    {
      "action": "transfer",
      "params": {
        "volume": "${sample_volume}",
        "source": "SamplePlate:A1-H12",
        "destination": "ELISAPlate:A1-H12",
        "new_tip": "always",
        "mix_after": [2, 30]
      }
    }
  ]
}
```

### Example 4: With Heater-Shaker Module

Protocol using the heater-shaker module.

```json
{
  "name": "Heated_Mixing_Protocol",
  "description": "Transfer and mix with temperature control",
  "version": "1.0",
  
  "requirements": {
    "labware": ["Reservoir", "Plate", "TipRack1000"],
    "pipettes": ["8channel"],
    "modules": ["HeaterShaker"]
  },
  
  "steps": [
    {
      "action": "comment",
      "params": { "message": "Pre-heating module" }
    },
    {
      "action": "heater_shaker_set_temperature",
      "params": {
        "module": "HeaterShaker",
        "temperature": 37
      }
    },
    {
      "action": "move_labware",
      "params": {
        "labware": "Plate",
        "destination": "HeaterShaker",
        "use_gripper": true
      }
    },
    {
      "action": "transfer",
      "params": {
        "volume": 100,
        "source": "Reservoir:A1",
        "destination": "Plate:A1-H12",
        "new_tip": "once"
      }
    },
    {
      "action": "heater_shaker_shake",
      "params": {
        "module": "HeaterShaker",
        "speed": 500,
        "duration": 300
      }
    },
    {
      "action": "heater_shaker_set_temperature",
      "params": {
        "module": "HeaterShaker",
        "temperature": null
      }
    },
    {
      "action": "move_labware",
      "params": {
        "labware": "Plate",
        "destination": "D1",
        "use_gripper": true
      }
    }
  ]
}
```

---

## Troubleshooting

### Common Errors

| Error | Cause | Solution |
|-------|-------|----------|
| `Labware 'X' not found` | Logical name not in config | Add labware to hardware config |
| `No suitable pipette` | Recipe needs pipette not mounted | Switch to compatible config |
| `Tips exhausted` | No more tips available | Add more tip racks to config |
| `Well out of range` | Invalid well reference | Check well notation (A1-H12) |

### Validation

Validate recipes before execution:

```bash
# Via Lab Console
orchestrator> validate recipe.json

# Or programmatically
python -c "from src.instrument_registry import validate_recipe; validate_recipe('recipe.json')"
```

---

## See Also

- [ARCHITECTURE.md](ARCHITECTURE.md) - System architecture
- [OPENTRONS_SERVER.md](OPENTRONS_SERVER.md) - Server details
- [PROTOCOLS.md](PROTOCOLS.md) - Workflow protocols
- [Library/HardwareConfig/](../Library/HardwareConfig/) - Hardware configurations
- [Library/Recipes/](../Library/Recipes/) - Example recipes
