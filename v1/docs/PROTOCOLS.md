# BicoccaLab - Guida ai Protocolli e Features

## 📋 Indice

1. [Generare un Protocollo](#generare-un-protocollo)
2. [Formato JSON Recipe (Opentrons)](#formato-json-recipe-opentrons)
3. [Formato Workflow](#formato-workflow)
4. [Features Tecan](#features-tecan)
5. [Features Opentrons](#features-opentrons)
6. [Esempi Pratici](#esempi-pratici)

---

## 🔧 Generare un Protocollo

Esistono tre modi per creare protocolli nel sistema BicoccaLab:

### Metodo 1: JSON Recipe (Opentrons)

I JSON Recipe sono il modo più semplice per definire operazioni di liquid handling.

**Struttura base:**
```json
{
  "ProtocolName": "NomeProtocollo",
  "Description": "Descrizione opzionale",
  "Requirements": {
    "SampleCount": 96,
    "TipType": "opentrons_flex_96_tiprack_200ul"
  },
  "Labware": {
    "SourcePlate": {"Type": "nest_96_wellplate_200ul_flat", "Slot": "D1"},
    "DestPlate": {"Type": "nest_96_wellplate_200ul_flat", "Slot": "D2"}
  },
  "TipRacks": {
    "Tips200": {"Type": "opentrons_flex_96_tiprack_200ul", "Slot": "C1"}
  },
  "Trash": {
    "Bin": {"Type": "TrashBin", "Slot": "A3"}
  },
  "Pipettes": {
    "left": "flex_1channel_1000"
  },
  "Steps": [
    {
      "Command": "Transfer",
      "Volume": 100,
      "Source": "SourcePlate:A1",
      "Dest": "DestPlate:A1",
      "PipetteMount": "left",
      "NewTip": "always"
    }
  ]
}
```

### Metodo 2: Workflow JSON (Orchestrato)

I Workflow coordinano operazioni su più strumenti.

**File:** `Protocols/*.workflow.json`

```json
{
  "workflow": {
    "id": "my_workflow",
    "name": "My Workflow",
    "description": "Descrizione",
    "version": "1.0.0",
    "tags": ["elisa", "automation"]
  },
  "variables": {
    "plate_id": "PLATE_001",
    "temperature": 37
  },
  "steps": [
    {
      "id": "step1",
      "name": "Prepare samples",
      "instrument": "opentrons",
      "action": "run_recipe",
      "params": {
        "recipe": "Library/Recipes/Transfer.json"
      }
    },
    {
      "id": "step2", 
      "name": "Read plate",
      "instrument": "tecan",
      "action": "run_analysis",
      "params": {
        "analysis": "Library/Analysis/TestAbs.mdfx",
        "plate_id": "${plate_id}"
      },
      "depends_on": ["step1"]
    }
  ]
}
```

### Metodo 3: Node-RED (Visual)

1. Apri Node-RED: `http://localhost:1880`
2. Trascina nodi dalla palette
3. Configura parametri
4. Collega nodi per creare il flusso
5. Deploy e esegui

---

## 📝 Formato JSON Recipe (Opentrons)

### Comandi Disponibili

| Comando | Descrizione | Parametri |
|---------|-------------|-----------|
| `Transfer` | Trasferisce liquido 1:1 | Volume, Source, Dest, PipetteMount, NewTip |
| `Distribute` | Distribuisce da 1 sorgente a N destinazioni | Volume, Source, Destinations[], PipetteMount |
| `Consolidate` | Consolida da N sorgenti a 1 destinazione | Volume, Sources[], Destination, PipetteMount |
| `Mix` | Mescola in una posizione | Volume, Location, PipetteMount, Repetitions |
| `PickUpTip` | Preleva un puntale | PipetteMount, TipRack, Well |
| `DropTip` | Scarta il puntale | PipetteMount, TrashLocation |
| `Comment` | Aggiunge un commento | Text |
| `Delay` | Pausa | Seconds |
| `Home` | Home del robot | - |
| `HeaterShaker` | Controlla Heater-Shaker | ModuleID, Temperature, RPM, Duration |

### Formato Posizione Well

Le posizioni sono specificate nel formato `LabwareID:Well`:
- `SourcePlate:A1` - Well A1 nel labware "SourcePlate"
- `Reservoir:A1` - Well A1 nel reservoir
- Range: `Plate:A1-H12` (tutti i well)
- Lista: `Plate:A1,A2,A3` (well specifici)

### Strategie NewTip

| Valore | Comportamento |
|--------|---------------|
| `always` | Nuovo puntale per ogni trasferimento |
| `once` | Un puntale per tutta l'operazione |
| `never` | Non cambiare puntale (usare con cautela) |

### Labware Supportati

**Piastre:**
- `nest_96_wellplate_200ul_flat`
- `nest_96_wellplate_2ml_deep`
- `corning_96_wellplate_360ul_flat`
- `biorad_96_wellplate_200ul_pcr`

**Tip Rack:**
- `opentrons_flex_96_tiprack_50ul`
- `opentrons_flex_96_tiprack_200ul`
- `opentrons_flex_96_tiprack_1000ul`

**Reservoir:**
- `nest_12_reservoir_15ml`
- `nest_1_reservoir_195ml`

**Pipette:**
- `flex_1channel_50`
- `flex_1channel_1000`
- `flex_8channel_50`
- `flex_8channel_1000`
- `flex_96channel_1000`

---

## 📊 Formato Workflow

### Schema Completo

```json
{
  "$schema": "./workflow.schema.json",
  
  "workflow": {
    "id": "unique_identifier",
    "name": "Human Readable Name",
    "description": "Detailed description",
    "version": "1.0.0",
    "author": "Nome Autore",
    "created": "2025-01-07T10:00:00Z",
    "tags": ["tag1", "tag2"]
  },
  
  "variables": {
    "plate_id": "default_value",
    "volume": 100,
    "temperature": 37.0,
    "use_heating": true
  },
  
  "steps": [
    {
      "id": "step_id",
      "name": "Step Name",
      "instrument": "opentrons|tecan|system|mobile",
      "action": "run_recipe|run_analysis|delay|wait_for_user",
      "params": {},
      "condition": "${variable} > 0",
      "on_error": "abort|retry|skip|continue",
      "max_retries": 3,
      "timeout_seconds": 300,
      "depends_on": ["other_step_id"]
    }
  ],
  
  "on_complete": {
    "notify": true,
    "archive_results": true,
    "results_folder": "Results/MyExperiment"
  },
  
  "on_error": {
    "emergency_stop": true,
    "notify": true,
    "save_state": true
  }
}
```

### Azioni Disponibili

| Azione | Strumento | Descrizione |
|--------|-----------|-------------|
| `run_recipe` | opentrons | Esegue un JSON recipe |
| `run_analysis` | tecan | Esegue un metodo MDFX |
| `run_task` | any | Esegue un task dalla Library |
| `delay` | system | Pausa per N secondi |
| `wait_for_user` | system | Attende conferma utente |
| `conditional` | system | Esecuzione condizionale |
| `parallel` | system | Step in parallelo |

### Variabili e Sostituzione

Le variabili sono sostituite a runtime:
```json
{
  "variables": {
    "experiment_id": "EXP001",
    "plate_id": "PLATE_${timestamp}"
  },
  "steps": [{
    "params": {
      "plate_id": "${plate_id}",
      "output_file": "Results/${experiment_id}_output.csv"
    }
  }]
}
```

Variabili speciali:
- `${timestamp}` - Data/ora corrente (YYYYMMDD_HHMMSS)
- `${workflow.id}` - ID del workflow
- `${workflow.name}` - Nome del workflow

---

## 🔬 Features Tecan

### PlateReaderService

**Properties (Observable):**

| Property | Tipo | Descrizione |
|----------|------|-------------|
| `IsConnected` | Boolean | Stato connessione |
| `OperationalStatus` | Enum | Idle, Busy, Error, Disconnected |
| `CurrentTemperature` | Real | Temperatura in °C |
| `InstrumentInfo` | Structure | Serial, Model, Simulated |

**Commands:**

| Comando | Parametri | Risposta | Descrizione |
|---------|-----------|----------|-------------|
| `Connect` | connection_string | success | Connette allo strumento |
| `Disconnect` | - | success | Disconnette |
| `PlateIn` | - | success | Inserisce piastra |
| `PlateOut` | - | success | Espelle piastra |
| `SetTemperature` | target (4-45°C) | success | Imposta temperatura |
| `TurnOffTemperature` | - | success | Disattiva controllo temp |
| `RunMeasurement` | protocol, plate_id | stream | Esegue misurazione |
| `GetAnIMLResult` | plate_id | document | Ottiene risultato AnIML |
| `ListProtocols` | - | protocols[] | Lista metodi MDFX |

**Tipi di Misurazione:**
- Absorbance (200-1000 nm)
- Fluorescence (excitation + emission)
- Luminescence
- Time-Resolved Fluorescence (TRF)
- Fluorescence Polarization

**Output Formats:**
- XML (nativo Tecan)
- CSV (compatibile i-control)
- Excel (opzionale)
- AnIML (ASTM E1947 standard)

---

## 🤖 Features Opentrons

### OpentronsFlex

| Comando | Parametri | Descrizione |
|---------|-----------|-------------|
| `Initialize` | - | Inizializza connessione |
| `Home` | - | Home di tutti gli assi |
| `EmergencyStop` | - | Stop immediato + drop tip |
| `RunProtocol` | content, type | Esegue protocollo |
| `AbortRun` | run_id | Interrompe run |
| `PauseRun` | run_id | Mette in pausa |
| `ResumeRun` | run_id | Riprende esecuzione |

### LiquidHandling

| Comando | Parametri | Descrizione |
|---------|-----------|-------------|
| `Transfer` | volume, source, dest, pipette, new_tip | Trasferimento 1:1 |
| `Distribute` | volume, source, dests[], pipette | Distribuzione 1:N |
| `Consolidate` | volume, sources[], dest, pipette | Consolidamento N:1 |
| `Mix` | volume, location, pipette, reps | Miscelazione |
| `PickUpTip` | pipette, rack, well | Preleva puntale |
| `DropTip` | pipette, location | Scarta puntale |
| `RefillTipRack` | rack_type | Reset tracking puntali |

### ModuleControl

| Comando | Parametri | Descrizione |
|---------|-----------|-------------|
| `HeaterShakerSetTemp` | module, temp, wait | Imposta temperatura |
| `HeaterShakerShake` | module, rpm, duration | Agitazione |
| `HeaterShakerOpenLatch` | module | Apre chiusura |
| `HeaterShakerCloseLatch` | module | Chiude chiusura |
| `HeaterShakerDeactivate` | module | Disattiva |
| `MoveLabware` | labware, from, to | Sposta labware |

---

## 📖 Esempi Pratici

### Esempio 1: Trasferimento Semplice

```json
{
  "ProtocolName": "SimpleTransfer",
  "Labware": {
    "Source": {"Type": "nest_96_wellplate_200ul_flat", "Slot": "D1"},
    "Dest": {"Type": "nest_96_wellplate_200ul_flat", "Slot": "D2"}
  },
  "TipRacks": {
    "Tips": {"Type": "opentrons_flex_96_tiprack_200ul", "Slot": "C1"}
  },
  "Trash": {"Bin": {"Type": "TrashBin", "Slot": "A3"}},
  "Pipettes": {"left": "flex_1channel_1000"},
  "Steps": [
    {"Command": "Transfer", "Volume": 50, "Source": "Source:A1", "Dest": "Dest:A1", "PipetteMount": "left", "NewTip": "always"},
    {"Command": "Transfer", "Volume": 50, "Source": "Source:A2", "Dest": "Dest:A2", "PipetteMount": "left", "NewTip": "always"}
  ]
}
```

### Esempio 2: Diluizione Seriale

```json
{
  "ProtocolName": "SerialDilution",
  "Labware": {
    "Plate": {"Type": "nest_96_wellplate_200ul_flat", "Slot": "D1"},
    "Diluent": {"Type": "nest_12_reservoir_15ml", "Slot": "D2"}
  },
  "TipRacks": {
    "Tips": {"Type": "opentrons_flex_96_tiprack_200ul", "Slot": "C1"}
  },
  "Trash": {"Bin": {"Type": "TrashBin", "Slot": "A3"}},
  "Pipettes": {"left": "flex_1channel_1000"},
  "Steps": [
    {"Command": "Comment", "Text": "Add diluent to all wells"},
    {"Command": "Distribute", "Volume": 90, "Source": "Diluent:A1", "Destinations": "Plate:A2,A3,A4,A5,A6", "PipetteMount": "left"},
    {"Command": "Comment", "Text": "Serial dilution 1:10"},
    {"Command": "Transfer", "Volume": 10, "Source": "Plate:A1", "Dest": "Plate:A2", "PipetteMount": "left", "NewTip": "always"},
    {"Command": "Mix", "Volume": 50, "Location": "Plate:A2", "PipetteMount": "left", "Repetitions": 3},
    {"Command": "Transfer", "Volume": 10, "Source": "Plate:A2", "Dest": "Plate:A3", "PipetteMount": "left", "NewTip": "always"},
    {"Command": "Mix", "Volume": 50, "Location": "Plate:A3", "PipetteMount": "left", "Repetitions": 3}
  ]
}
```

### Esempio 3: Workflow ELISA Completo

```json
{
  "workflow": {
    "id": "elisa_protocol",
    "name": "ELISA Complete",
    "version": "1.0.0",
    "tags": ["elisa", "immunoassay"]
  },
  "variables": {
    "plate_id": "ELISA_001",
    "incubation_temp": 37,
    "wash_volume": 300
  },
  "steps": [
    {
      "id": "add_samples",
      "name": "Add Samples",
      "instrument": "opentrons",
      "action": "run_recipe",
      "params": {"recipe": "Library/Recipes/AddSamples.json"}
    },
    {
      "id": "incubate_1",
      "name": "Incubate 1h at 37°C",
      "instrument": "tecan",
      "action": "run_analysis",
      "params": {
        "analysis": "Library/Analysis/Incubate.mdfx",
        "temperature": "${incubation_temp}"
      },
      "depends_on": ["add_samples"]
    },
    {
      "id": "wash_1",
      "name": "Wash 3x",
      "instrument": "opentrons",
      "action": "run_recipe",
      "params": {
        "recipe": "Library/Recipes/Wash.json",
        "variables": {"wash_volume": "${wash_volume}", "cycles": 3}
      },
      "depends_on": ["incubate_1"]
    },
    {
      "id": "read_plate",
      "name": "Read Absorbance",
      "instrument": "tecan",
      "action": "run_analysis",
      "params": {
        "analysis": "Library/Analysis/ReadAbs450.mdfx",
        "plate_id": "${plate_id}"
      },
      "depends_on": ["wash_1"]
    }
  ],
  "on_complete": {
    "archive_results": true,
    "results_folder": "Results/ELISA/${plate_id}"
  }
}
```

### Esempio 4: Uso Heater-Shaker

```json
{
  "ProtocolName": "HeaterShakerExample",
  "Labware": {
    "Plate": {"Type": "nest_96_wellplate_200ul_flat", "Slot": "D1"}
  },
  "Modules": {
    "HS1": {"Type": "heaterShakerModuleV1", "Slot": "D3"}
  },
  "TipRacks": {
    "Tips": {"Type": "opentrons_flex_96_tiprack_200ul", "Slot": "C1"}
  },
  "Trash": {"Bin": {"Type": "TrashBin", "Slot": "A3"}},
  "Pipettes": {"left": "flex_1channel_1000"},
  "Steps": [
    {"Command": "Comment", "Text": "Setup Heater-Shaker"},
    {"Command": "HeaterShaker", "ModuleID": "HS1", "Action": "CloseLatch"},
    {"Command": "HeaterShaker", "ModuleID": "HS1", "Temperature": 37, "WaitForTemp": true},
    {"Command": "HeaterShaker", "ModuleID": "HS1", "RPM": 500, "Duration": 60},
    {"Command": "HeaterShaker", "ModuleID": "HS1", "Action": "Deactivate"},
    {"Command": "HeaterShaker", "ModuleID": "HS1", "Action": "OpenLatch"}
  ]
}
```

---

## 🚀 Best Practices

1. **Naming**: Usa nomi descrittivi per labware e step
2. **Comments**: Aggiungi commenti per documentare il protocollo
3. **Error Handling**: Imposta `on_error` appropriatamente
4. **Tip Strategy**: Usa `always` per evitare contaminazione
5. **Volumes**: Verifica che i volumi siano nel range della pipetta
6. **Testing**: Testa prima in simulazione
7. **Version Control**: Usa versioning per i workflow

---

## 🔗 Risorse Utili

- [SiLA Standard](https://sila-standard.com/)
- [Opentrons Protocol Designer](https://designer.opentrons.com/)
- [Tecan iControl Documentation](https://lifesciences.tecan.com/)
- [Node-RED Documentation](https://nodered.org/docs/)
