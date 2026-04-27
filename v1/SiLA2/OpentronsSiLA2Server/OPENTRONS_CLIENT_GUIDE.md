# Opentrons SiLA2 Client - Guida Utente

## Panoramica

Il client Opentrons SiLA2 è uno strumento per controllare il robot Opentrons Flex attraverso il server SiLA2. 
Supporta il sistema **HAL (Hardware Abstraction Layer)** che permette di scrivere ricette con riferimenti logici invece di slot fisici.

---

## Installazione

### Requisiti
```bash
pip install httpx pyyaml
```

### File Necessari
```
OpentronsSiLA2Server/
├── opentrons_client.py      # Client ottimizzato
├── config.yaml              # Configurazione
├── protocol_generator.py    # Generatore protocolli
└── input/                   # Cartella ricette
```

---

## Configurazione

### config.yaml
```yaml
robot:
  ip: "169.254.161.83"
  port: 31950
  local_address: "169.254.161.1"  # Per Hyper-V

hardware:
  config_folder: "../HardwareConfig"

directories:
  input_queue: "./input"
  processed: "./processed"
  errors: "./errors"
  output: "./output"
```

---

## Uso Interattivo

### Avvio
```bash
cd OpentronsSiLA2Server
python opentrons_client.py
```

### Comandi Disponibili

| Comando | Descrizione |
|---------|-------------|
| `status` | Mostra stato robot, moduli, pipette |
| `hw` | Mostra hardware config caricata |
| `hwlist` | Lista configurazioni disponibili |
| `hwload [nome]` | Carica configurazione hardware |
| `list` | Lista protocolli in input/ |
| `run [file]` | Esegue un protocollo |
| `home` | Home del robot |
| `pause` | Pausa esecuzione |
| `resume` | Riprende esecuzione |
| `stop` | Ferma esecuzione |
| `estop` | **EMERGENZA** - Ferma tutto e home |
| `lon` / `loff` | Luci on/off |
| `exit` | Esci |

---

## Hardware Abstraction Layer (HAL)

### Concetto
L'HAL permette di scrivere ricette **indipendenti dall'hardware fisico**.
Invece di specificare slot direttamente, si usano **nomi logici** che vengono risolti dalla configurazione hardware.

### Esempio Senza HAL (vecchio metodo)
```json
{
  "ProtocolName": "MyProtocol",
  "Labware": {
    "Tips_1000": {
      "LoadName": "opentrons_flex_96_filtertiprack_1000ul",
      "Slot": "B2"
    },
    "Plate": {
      "LoadName": "corning_24_wellplate_3.4ml_flat",
      "Slot": "D2"
    }
  }
}
```

### Esempio Con HAL (nuovo metodo)
```json
{
  "ProtocolName": "MyProtocol_HAL",
  "Requirements": {
    "MiaReservoir": "Reservoir_Main",
    "MiaPiastra": "Piastra_Target",
    "HeaterShaker": "HeaterShaker"
  },
  "Steps": [
    {"Command": "Transfer", "Source": "MiaReservoir:A1", "Dest": "MiaPiastra:A1", ...}
  ]
}
```

### File Hardware Config (Standard_Flex_Setup.json)
```json
{
  "Labware": {
    "Reservoir_Main": {
      "LoadName": "nest_1_reservoir_290ml",
      "Slot": "C2"
    },
    "Piastra_Target": {
      "LoadName": "corning_24_wellplate_3.4ml_flat",
      "Slot": "D2"
    }
  },
  "Modules": {
    "HeaterShaker": {"Type": "heaterShakerModuleV1", "Slot": "A1"}
  },
  "Pipettes": {"left": "flex_1channel_1000"},
  "Trash": {"Bin": {"Type": "TrashBin", "Slot": "A3"}}
}
```

### Risoluzione HAL
Quando esegui una ricetta con `Requirements`:

1. Client legge `Requirements`
2. Per ogni requisito, cerca nell'hardware config
3. Costruisce le sezioni `Labware`, `Modules`, `Trash`
4. Gli Steps usano i nomi logici che vengono risolti

---

## Uso Programmatico

### Esempio Base
```python
import asyncio
from opentrons_client import OpentronsSiLA2Client

async def main():
    client = OpentronsSiLA2Client("config.yaml")
    
    # Connetti
    await client.connect()
    
    # Carica hardware config
    client.load_hw_config("Standard_Flex_Setup.json")
    
    # Esegui ricetta
    success, run_id = await client.execute_file("input/myrecipe.json")
    
    await client.disconnect()

asyncio.run(main())
```

### Esecuzione con Ricetta Dinamica
```python
async def run_custom():
    client = OpentronsSiLA2Client("config.yaml")
    await client.connect()
    client.load_hw_config("Standard_Flex_Setup.json")
    
    recipe = {
        "ProtocolName": "Dynamic_Test",
        "Requirements": {
            "Source": "Reservoir_Main",
            "Dest": "Piastra_Target"
        },
        "Steps": [
            {"Command": "Comment", "Text": "Test dinamico"},
            {"Command": "Transfer", "Volume": 100, 
             "Source": "Source:A1", "Dest": "Dest:A1",
             "PipetteMount": "left", "NewTip": "always"}
        ]
    }
    
    success, run_id = await client.execute_recipe(recipe)
    print(f"Success: {success}, Run: {run_id}")
    
    await client.disconnect()
```

### Controllo Run
```python
async def control_run():
    client = OpentronsSiLA2Client()
    await client.connect()
    
    # Avvia
    success, run_id = await client.execute_file("recipe.json", monitor=False)
    
    # Pausa
    await client.pause()
    
    # Riprendi
    await client.resume()
    
    # Ferma
    await client.stop()
    
    # Monitora
    await client.monitor_run(run_id)
```

---

## Struttura Ricette JSON

### Campi Principali
```json
{
  "ProtocolName": "Nome del protocollo",
  "Description": "Descrizione",
  "Author": "Autore",
  "Version": "1.0",
  
  "Requirements": { },      // HAL - mappatura logica
  "Modules": { },           // Moduli (HeaterShaker, etc.)
  "Labware": { },           // Labware (piastre, tip rack, etc.)
  "Trash": { },             // Trash bin
  "Pipettes": { },          // Pipette
  
  "Steps": [ ]              // Lista comandi
}
```

### Comandi Supportati

#### Transfer
```json
{
  "Command": "Transfer",
  "Volume": 100,
  "Source": "Reservoir:A1",
  "Dest": "Plate:A1",
  "PipetteMount": "left",
  "NewTip": "always"  // always, once, never
}
```

#### Distribute (1 source → N dest)
```json
{
  "Command": "Distribute",
  "Volume": 50,
  "Source": "Reservoir:A1",
  "Destinations": ["Plate:A1", "Plate:A2", "Plate:A3"],
  "PipetteMount": "left",
  "NewTip": "once"
}
```

#### Consolidate (N source → 1 dest)
```json
{
  "Command": "Consolidate",
  "Volume": 50,
  "Sources": ["Plate:A1", "Plate:A2", "Plate:A3"],
  "Dest": "Reservoir:A1",
  "PipetteMount": "left"
}
```

#### Mix
```json
{
  "Command": "Mix",
  "Volume": 100,
  "Repetitions": 5,
  "Location": "Plate:A1",
  "PipetteMount": "left"
}
```

#### PickUpTip / DropTip
```json
{"Command": "PickUpTip", "PipetteMount": "left"}
{"Command": "DropTip", "PipetteMount": "left"}
```

#### HeaterShaker
```json
{
  "Command": "HeaterShaker",
  "ModuleID": "HeaterShaker",
  "Temperature": 37,
  "WaitForTemp": true,
  "RPM": 500,
  "Duration": 60,
  "OpenLatch": true,
  "CloseLatch": false,
  "DeactivateHeater": false
}
```

#### MoveLabware (con Gripper)
```json
{
  "Command": "MoveLabware",
  "LabwareID": "Plate",
  "NewLocation": "HeaterShaker",
  "UseGripper": true
}
```

#### Comment
```json
{"Command": "Comment", "Text": "Messaggio nel log"}
```

#### Home
```json
{"Command": "Home"}
```

---

## Risoluzione Problemi

### Errore Connessione
```
✗ Connection failed: [Errno 10049]
```
**Soluzione**: Verifica IP robot e `local_address` in config.yaml

### Zombie Run
```
⚠ Zombie run detected, cleaning...
```
**Soluzione**: Il client pulisce automaticamente. Se persiste: `estop`

### Protocol Upload Failed
```
✗ Upload failed: {...}
```
**Soluzione**: 
1. Verifica sintassi JSON
2. Controlla che tutti i labware siano definiti
3. Verifica nomi pipette corretti

### HAL Resolution Warning
```
⚠ Cannot resolve requirement: MyLabware -> Unknown
```
**Soluzione**: Verifica che il nome nel `Requirements` corrisponda a un elemento nell'hardware config

---

## Workflow Consigliato

1. **Crea Hardware Config** per il tuo setup fisico
2. **Scrivi ricette con HAL** usando `Requirements`
3. **Testa con dry-run** (conferma "n" all'esecuzione)
4. **Esegui** quando pronto

### Esempio Workflow Completo
```bash
# Terminal
python opentrons_client.py

# Shell interattiva
Opentrons❯ hwlist                    # Lista configs
Opentrons❯ hwload ELISA_Setup.json   # Carica config
Opentrons❯ hw                        # Verifica
Opentrons❯ status                    # Stato robot
Opentrons❯ list                      # Lista ricette
Opentrons❯ run myprotocol.json       # Esegui
```

---

## Appendice: Migrazione da Vecchio Client

### Vecchio (interactive_client.py)
- Comandi: `hwselect`, `hwconfig`, `hsreset`, `refill`
- HAL semplice (merge config)

### Nuovo (opentrons_client.py)  
- Comandi: `hwload`, `hw`, `estop`
- HAL completo con `Requirements`
- Dataclass per stato
- Logging migliorato
- Codice più pulito e mantenibile

---

## Contatti

**BicoccaLab** - Laboratorio Automazione
