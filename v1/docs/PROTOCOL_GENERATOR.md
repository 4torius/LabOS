# 🔧 Guida al Protocol Generator

Il **Protocol Generator** converte Recipe JSON in codice Python eseguibile dall'Opentrons Flex. Questa guida spiega come creare e strutturare le recipe.

---

## 📋 Architettura

```
┌─────────────────────┐      ┌─────────────────────┐      ┌─────────────────────┐
│   Recipe JSON       │ ──▶  │  Protocol Generator │ ──▶  │  Python Protocol    │
│  (Library/Recipes)  │      │  (protocol_gen.py)  │      │  (Opentrons API)    │
└─────────────────────┘      └─────────────────────┘      └─────────────────────┘
         │
         │ + HAL Mapping
         ▼
┌─────────────────────┐
│  Hardware Config    │
│(Library/HardwareConfig)│
└─────────────────────┘
```

### Hardware Abstraction Layer (HAL)

Il sistema usa un **HAL** per separare la logica del protocollo dalla configurazione fisica:
- **Recipe**: Descrive COSA fare (comandi, volumi, posizioni logiche)
- **HardwareConfig**: Descrive DOVE farlo (slot, moduli, pipette fisiche)

---

## 📁 Struttura Recipe JSON

Una Recipe completa ha questa struttura:

```json
{
  "ProtocolName": "Nome Protocollo",
  "Description": "Descrizione opzionale",
  
  "Requirements": {
    "LogicalName": "HardwareConfigKey"
  },
  
  "Labware": {
    "PlateLogico": {
      "LoadName": "corning_96_wellplate_360ul_flat",
      "Slot": "C1"
    }
  },
  
  "Liquids": {
    "Sample": { "Color": "#FF0000" },
    "Buffer": { "Color": "#0000FF" }
  },
  
  "Steps": [
    { "Command": "...", "Parameters": "..." }
  ]
}
```

### Sezioni

| Sezione | Obbligatoria | Descrizione |
|---------|--------------|-------------|
| `ProtocolName` | ✅ | Nome visualizzato |
| `Requirements` | ❌ | Mappatura logico→fisico via HAL |
| `Labware` | ❌ | Labware aggiuntivo (oltre a HAL) |
| `Modules` | ❌ | Moduli aggiuntivi |
| `Liquids` | ❌ | Definizione liquidi per visualizzazione |
| `Steps` | ✅ | Lista comandi da eseguire |

---

## 🎯 Comandi Disponibili

### Transfer
Trasferisce liquido da source a destination.

```json
{
  "Command": "Transfer",
  "PipetteMount": "left",
  "Volume": 100,
  "Source": "Reservoir_Main:A1",
  "Dest": "Piastra_Target:A1",
  "NewTip": "once",
  "TrashLocation": "default"
}
```

| Parametro | Tipo | Default | Descrizione |
|-----------|------|---------|-------------|
| `PipetteMount` | string | - | `"left"` o `"right"` |
| `Volume` | number | - | Volume in µL |
| `Source` | string | - | `"LabwareID:Well"` |
| `Dest` | string | - | `"LabwareID:Well"` |
| `NewTip` | string | `"once"` | `"always"`, `"once"`, `"never"` |
| `TrashLocation` | string | `"default"` | ID del trash |

### Distribute
Distribuisce da una sorgente a multiple destinazioni.

```json
{
  "Command": "Distribute",
  "PipetteMount": "left",
  "Volume": 50,
  "Source": "Reservoir_Main:A1",
  "Destinations": ["Plate:A1", "Plate:A2", "Plate:A3", "Plate:A4"],
  "NewTip": "once"
}
```

### Consolidate
Consolida da multiple sorgenti a una destinazione.

```json
{
  "Command": "Consolidate",
  "PipetteMount": "left",
  "Volume": 25,
  "Sources": ["Plate:A1", "Plate:A2", "Plate:A3"],
  "Dest": "Tube:A1",
  "NewTip": "once"
}
```

### PickUpTip / DropTip
Controllo manuale delle punte.

```json
{ "Command": "PickUpTip", "PipetteMount": "left" }
```

```json
{ 
  "Command": "DropTip", 
  "PipetteMount": "left",
  "TrashLocation": "default",
  "Force": false
}
```

| Parametro | Descrizione |
|-----------|-------------|
| `Force` | Se `true`, ignora errori se non c'è punta |

### MoveLabware
Sposta labware usando il gripper.

```json
{
  "Command": "MoveLabware",
  "LabwareID": "Piastra_Target",
  "NewLocation": "HeaterShaker",
  "UseGripper": true
}
```

### HeaterShaker
Controlla il modulo Heater-Shaker.

```json
{
  "Command": "HeaterShaker",
  "ModuleID": "HeaterShaker",
  "CloseLatch": true,
  "Temperature": 37,
  "WaitForTemp": true,
  "RPM": 500,
  "Duration": 300,
  "OpenLatch": true,
  "DeactivateHeater": true
}
```

| Parametro | Descrizione |
|-----------|-------------|
| `CloseLatch` | Chiude il blocco prima di iniziare |
| `Temperature` | Temperatura target in °C |
| `WaitForTemp` | Attende il raggiungimento temperatura |
| `RPM` | Velocità agitazione |
| `Duration` | Durata in secondi |
| `KeepShaking` | Non fermare l'agitazione alla fine |
| `OpenLatch` | Apre il blocco alla fine |
| `DeactivateHeater` | Spegne il riscaldamento |

### Home
Riporta il robot alla posizione iniziale.

```json
{ "Command": "Home" }
```

### Comment
Aggiunge un commento al log.

```json
{ "Command": "Comment", "Text": "Inizio fase di incubazione" }
```

### Delay
Pausa l'esecuzione.

```json
{ "Command": "Delay", "Seconds": 60 }
```

### ConsumeTips
Consuma un numero specifico di punte (utile per test).

```json
{ "Command": "ConsumeTips", "PipetteMount": "left", "Quantity": 8 }
```

### TakeSnapshot
Cattura un'immagine dalla camera (se disponibile).

```json
{ "Command": "TakeSnapshot", "Filename": "pre_incubation.jpg" }
```

---

## 🧪 Liquid Classes - Gestione Fluidi Speciali

Il sistema supporta **Liquid Classes** per ottimizzare il pipettaggio di fluidi con proprietà diverse (viscosi, volatili, schiumogeni). Le liquid classes sono definite centralmente nella cartella `Library/LiquidClasses/`.

### Liquid Classes Predefinite

| Nome | LoadName API | Descrizione | Uso Tipico |
|------|-------------|-------------|------------|
| `Aqueous` | `water` | Liquidi acquosi standard | Buffer, media, acqua |
| `Viscous` | `glycerol_50` | Liquidi viscosi (glicerolo 50%) | Glicerolo, PEG, miele |
| `Volatile` | `ethanol_80` | Liquidi volatili (etanolo 80%) | Etanolo, metanolo, acetone |
| `HighlyViscous` | - | Liquidi molto viscosi (>70% glicerolo) | Sciroppi, oli |
| `Foaming` | - | Liquidi schiumogeni | Detergenti, proteine |

### Usare Liquid Classes nelle Recipe

#### Metodo 1: API Nativa Opentrons (Consigliato)

Usa i comandi `*WithLiquidClass` per trasferimenti ottimizzati:

```json
{
  "Command": "TransferWithLiquidClass",
  "PipetteMount": "left",
  "Volume": 100,
  "Source": "Reservoir:A1",
  "Dest": "Plate:A1",
  "LiquidClass": "Viscous"
}
```

Comandi disponibili:
- `TransferWithLiquidClass`
- `DistributeWithLiquidClass`
- `ConsolidateWithLiquidClass`

#### Metodo 2: Parametro LiquidClass su Transfer Standard

```json
{
  "Command": "Transfer",
  "PipetteMount": "left",
  "Volume": 100,
  "Source": "Reservoir:A1",
  "Dest": "Plate:A1",
  "LiquidClass": "Viscous"
}
```

#### Metodo 3: Parametri Manuali (Controllo Completo)

Per controllo granulare su aspirazione e dispensazione:

```json
{
  "Command": "Aspirate",
  "PipetteMount": "left",
  "Volume": 100,
  "Location": "Reservoir:A1",
  "Rate": 0.25
}
```

| Parametro | Descrizione | Valori |
|-----------|-------------|--------|
| `Rate` | Velocità relativa | 0.05 - 2.0 (1.0 = default) |
| `BlowOut` | Espelli aria residua | `true`/`false` |
| `TouchTip` | Tocca il bordo | `true`/`false` |

### Struttura File Liquid Class

Le liquid classes sono JSON in `Library/LiquidClasses/`:

```json
{
  "name": "Viscous",
  "loadName": "glycerol_50",
  "description": "Per liquidi viscosi come glicerolo 50%",
  "basedOn": "glycerol_50",
  
  "parameters": {
    "aspirate_rate": 0.25,
    "dispense_rate": 0.3,
    "blow_out": true,
    "touch_tip": true,
    "air_gap": 5,
    "delay_aspirate": 1.0,
    "delay_dispense": 0.5
  },
  
  "notes": [
    "Usare per glicerolo 40-60%",
    "Aumentare delay per concentrazioni maggiori"
  ]
}
```

### Parametri Liquid Class

| Parametro | Range | Default | Descrizione |
|-----------|-------|---------|-------------|
| `aspirate_rate` | 0.05-2.0 | 1.0 | Velocità aspirazione (1.0 = normale) |
| `dispense_rate` | 0.05-2.0 | 1.0 | Velocità dispensazione |
| `blow_out` | bool | false | Espelli aria dopo dispense |
| `touch_tip` | bool | false | Tocca bordo dopo dispense |
| `air_gap` | 0-20 µL | 0 | Gap d'aria dopo aspirazione |
| `push_out` | 0-20 µL | 2.0 | Volume extra in dispensazione |
| `delay_aspirate` | 0-10 s | 0.2 | Pausa dopo aspirazione |
| `delay_dispense` | 0-10 s | 0.2 | Pausa dopo dispensazione |
| `mix_after` | [n, frac] | null | Mix dopo dispense |

### Creare Liquid Class Custom

1. Crea un nuovo file JSON in `Library/LiquidClasses/`:

```json
{
  "name": "MyCustomLiquid",
  "description": "Descrizione del liquido e uso tipico",
  "basedOn": "water",
  
  "parameters": {
    "aspirate_rate": 0.5,
    "dispense_rate": 0.6,
    "blow_out": true,
    "air_gap": 3
  }
}
```

2. Usala nelle recipe:

```json
{
  "Command": "Transfer",
  "LiquidClass": "MyCustomLiquid",
  ...
}
```

### Esempio Completo: Diluizione Seriale con Glicerolo

```json
{
  "ProtocolName": "Serial_Dilution_Glycerol",
  "Description": "Diluizione seriale di stock in glicerolo 50%",
  
  "Steps": [
    { "Command": "Comment", "Text": "Distribuzione buffer con pipettaggio standard" },
    
    { "Command": "Distribute",
      "PipetteMount": "left",
      "Volume": 180,
      "Source": "BufferRes:A1",
      "Destinations": ["Plate:A2", "Plate:A3", "Plate:A4"]
    },
    
    { "Command": "Comment", "Text": "Trasferimento stock viscoso con liquid class" },
    
    { "Command": "TransferWithLiquidClass",
      "PipetteMount": "left",
      "Volume": 20,
      "Source": "GlycerolStock:A1",
      "Dest": "Plate:A1",
      "LiquidClass": "Viscous"
    },
    
    { "Command": "Comment", "Text": "Diluizioni seriali con mix" },
    
    { "Command": "Transfer",
      "PipetteMount": "left",
      "Volume": 20,
      "Source": "Plate:A1",
      "Dest": "Plate:A2",
      "LiquidClass": "Viscous",
      "MixAfter": [3, 100],
      "NewTip": "always"
    }
  ]
}
```

---

## 🔄 Uso del HAL (Hardware Abstraction Layer)

### Problema
Se specifichi slot fisici nella recipe, devi modificarla ogni volta che cambi configurazione hardware.

### Soluzione: Requirements
Usa nomi logici nella recipe e mappali a ID della HardwareConfig:

**Recipe:**
```json
{
  "ProtocolName": "Diluizione",
  "Requirements": {
    "MiaPiastra": "Piastra_Target",
    "MioReservoir": "Reservoir_Main"
  },
  "Steps": [
    {
      "Command": "Transfer",
      "Volume": 100,
      "Source": "MioReservoir:A1",
      "Dest": "MiaPiastra:A1"
    }
  ]
}
```

**HardwareConfig (Standard_Flex_Setup.json):**
```json
{
  "ConfigName": "Standard_Flex_Setup",
  "Labware": {
    "Piastra_Target": { "LoadName": "corning_24_wellplate_3.4ml_flat", "Slot": "D2" },
    "Reservoir_Main": { "LoadName": "nest_1_reservoir_290ml", "Slot": "C2" }
  }
}
```

Il Protocol Generator risolverà automaticamente:
- `MiaPiastra` → `Piastra_Target` → Slot D2
- `MioReservoir` → `Reservoir_Main` → Slot C2

### Compatibilità con Generic_HAL

Per garantire la massima portabilità, si consiglia di usare i nomi standard definiti nel file **Generic_HAL.yaml**:

| ID Logico | Descrizione | Slot Default |
|-----------|-------------|--------------|
| `Source_1` | Reservoir sorgente primario | C1 |
| `Source_2` | Reservoir sorgente secondario | C2 |
| `Source_3` | Reservoir sorgente terziario | B1 |
| `Source_4` | Reservoir sorgente quaternario | B2 |
| `TargetPlate` | Piastra destinazione | D2 |
| `MyTips` | Rack di punte | D3 |
| `MyTrash` | Contenitore rifiuti | A3 |

**Esempio di Recipe compatibile con Generic_HAL:**

```json
{
  "ProtocolName": "HAL_Compatible_Distribution",
  "Description": "Recipe compatibile con Generic_HAL",
  
  "Requirements": {
    "Source": "Source_1",
    "Plate": "TargetPlate",
    "Tips": "MyTips",
    "Trash": "MyTrash"
  },
  
  "Steps": [
    { "Command": "Distribute", "PipetteMount": "left", "Volume": 50,
      "Source": "Source:A1", "Dest": "Plate:A1-A12", "NewTip": "always" }
  ]
}
```

> **Nota**: Le ricette scritte con i nomi Generic_HAL funzioneranno su qualsiasi sistema che utilizzi Generic_HAL.yaml senza modifiche.

---

## 📝 Esempi Completi

### Esempio 1: Diluizione Seriale

```json
{
  "ProtocolName": "Serial_Dilution_1to10",
  "Description": "Diluizione seriale 1:10 su 8 colonne",
  
  "Requirements": {
    "Diluent": "Reservoir_Main",
    "Plate": "Piastra_Target"
  },
  
  "Steps": [
    { "Command": "Comment", "Text": "Inizio diluizione seriale 1:10" },
    
    { "Command": "Transfer", "PipetteMount": "left", "Volume": 180,
      "Source": "Diluent:A1", "Dest": "Plate:A2", "NewTip": "once" },
    { "Command": "Transfer", "PipetteMount": "left", "Volume": 180,
      "Source": "Diluent:A1", "Dest": "Plate:A3", "NewTip": "never" },
    { "Command": "Transfer", "PipetteMount": "left", "Volume": 180,
      "Source": "Diluent:A1", "Dest": "Plate:A4", "NewTip": "never" },
    
    { "Command": "DropTip", "PipetteMount": "left" },
    
    { "Command": "Transfer", "PipetteMount": "left", "Volume": 20,
      "Source": "Plate:A1", "Dest": "Plate:A2", "NewTip": "always" },
    { "Command": "Transfer", "PipetteMount": "left", "Volume": 20,
      "Source": "Plate:A2", "Dest": "Plate:A3", "NewTip": "always" },
    { "Command": "Transfer", "PipetteMount": "left", "Volume": 20,
      "Source": "Plate:A3", "Dest": "Plate:A4", "NewTip": "always" },
    
    { "Command": "Comment", "Text": "Diluizione completata" }
  ]
}
```

### Esempio 2: Incubazione con Heater-Shaker

```json
{
  "ProtocolName": "Incubation_37C",
  "Description": "Incuba piastra a 37°C con agitazione",
  
  "Modules": {
    "HeaterShaker": { "Type": "heaterShakerModuleV1", "Slot": "D1" }
  },
  
  "Requirements": {
    "Plate": "Piastra_Target"
  },
  
  "Steps": [
    { "Command": "Comment", "Text": "Spostamento piastra su Heater-Shaker" },
    
    { "Command": "MoveLabware", "LabwareID": "Plate", 
      "NewLocation": "HeaterShaker", "UseGripper": true },
    
    { "Command": "HeaterShaker", "ModuleID": "HeaterShaker",
      "CloseLatch": true,
      "Temperature": 37,
      "WaitForTemp": true,
      "RPM": 300,
      "Duration": 1800,
      "OpenLatch": true,
      "DeactivateHeater": true
    },
    
    { "Command": "MoveLabware", "LabwareID": "Plate", 
      "NewLocation": "C1", "UseGripper": true },
    
    { "Command": "Comment", "Text": "Incubazione completata" }
  ]
}
```

---

## 🖥️ Utilizzo Programmatico

### Da Python

```python
from protocol_generator import ProtocolGenerator, HardwareManager
import json

# Carica hardware config
hw = HardwareManager(config_folder="Library/HardwareConfig")
hw.switch_config("Standard_Flex_Setup")

# Carica recipe
with open("Library/Recipes/my_recipe.json") as f:
    recipe = json.load(f)

# Applica HAL mapping
recipe = hw.apply_hardware_to_recipe(recipe)

# Genera protocollo Python
gen = ProtocolGenerator()
protocol_path = gen.generate(json.dumps(recipe))

print(f"Protocollo generato: {protocol_path}")
```

### Via API REST

```bash
# Esegui recipe
curl -X POST http://localhost:8000/opentrons/recipe \
  -H "Content-Type: application/json" \
  -d '{"recipe_file": "my_recipe.json"}'
```

---

## ⚠️ Note Importanti

1. **Tip Racks**: Vengono auto-iniettati dalla HardwareConfig
2. **Pipette**: Definite nella HardwareConfig, non nella recipe
3. **Trash**: Default in A3 se non specificato
4. **Well Format**: Usa `"LabwareID:Well"` (es. `"Plate:A1"`)
5. **Volumi**: Sempre in microlitri (µL)

---

## 🔍 Troubleshooting

| Errore | Causa | Soluzione |
|--------|-------|-----------|
| `HAL Error: Requirement X not found` | ID logico non in HardwareConfig | Verifica mapping in Requirements |
| `No tip racks found` | Mancano tip rack in HardwareConfig | Aggiungi Tips_* in HardwareConfig.Labware |
| `Pipette not found` | Mount errato | Usa `"left"` o `"right"` come in HardwareConfig |

---

## 📚 Riferimenti

- [Opentrons Python API v2.26](https://docs.opentrons.com/v2/)
- [Labware Library](https://labware.opentrons.com/)
- [HardwareConfig Examples](../Library/HardwareConfig/)
