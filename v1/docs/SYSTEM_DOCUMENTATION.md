# BicoccaLab v6 - Documentazione Completa del Sistema

**Università degli Studi di Milano-Bicocca**  
**Laboratory Automation System**  
*Versione 6.0 - Gennaio 2026*

---

## Indice

1. [Panoramica del Sistema](#1-panoramica-del-sistema)
2. [Architettura](#2-architettura)
3. [Componenti Hardware](#3-componenti-hardware)
4. [Stack Software](#4-stack-software)
5. [HAL - Hardware Abstraction Layer](#5-hal---hardware-abstraction-layer)
6. [Sistema Workflow](#6-sistema-workflow)
7. [Sfide Affrontate e Soluzioni](#7-sfide-affrontate-e-soluzioni)
8. [Guida all'Uso](#8-guida-alluso)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Panoramica del Sistema

BicoccaLab v6 è un sistema di automazione di laboratorio che integra multiple strumentazioni scientifiche attraverso il protocollo **SiLA2** (Standardization in Lab Automation 2).

### Obiettivi del Sistema

- **Automazione completa**: Esecuzione automatizzata di protocolli di laboratorio
- **Multi-strumento**: Orchestrazione di diversi dispositivi (Opentrons, Tecan, Mobile Robot)
- **Flessibilità**: Configurazioni hardware intercambiabili tramite HAL
- **Riproducibilità**: Protocolli standardizzati e tracciabilità completa

### Strumenti Integrati

| Strumento | Tipo | Protocollo | Porta |
|-----------|------|------------|-------|
| Opentrons Flex | Liquid Handler | HTTP REST | 31950 |
| Tecan M200Pro | Plate Reader | gRPC/SiLA2 | 50051 |
| Mobile Robot | Transport | gRPC/SiLA2 | 50053 |

---

## 2. Architettura

```
┌─────────────────────────────────────────────────────────────────────┐
│                         USER INTERFACE                               │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐     │
│  │  lab_master.py  │  │ lab_console.py  │  │   Orchestrator  │     │
│  │  (Launcher)     │  │ (Interactive)   │  │   (Batch)       │     │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘     │
└───────────┼─────────────────────┼─────────────────────┼─────────────┘
            │                     │                     │
            ▼                     ▼                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      WORKFLOW LAYER                                  │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    WorkflowExecutor                          │   │
│  │  • Multi-step orchestration                                  │   │
│  │  • HAL config resolution                                     │   │
│  │  • Error handling                                            │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     HAL LAYER (Hardware Abstraction)                 │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐     │
│  │ Standard_Flex   │  │  ELISA_Setup    │  │SerialDilution   │     │
│  │   _Setup.json   │  │    .json        │  │  _Setup.json    │     │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘     │
└─────────────────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     DEVICE LAYER                                     │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐     │
│  │  OpentronsFlex  │  │  TecanM200Pro   │  │   MobileRobot   │     │
│  │  (HTTP Client)  │  │ (gRPC Client)   │  │  (gRPC Client)  │     │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘     │
└───────────┼─────────────────────┼─────────────────────┼─────────────┘
            │                     │                     │
            ▼                     ▼                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     PHYSICAL HARDWARE                                │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐     │
│  │ Opentrons Flex  │  │ Tecan M200 Pro  │  │  Mobile Robot   │     │
│  │ 169.254.161.83  │  │   localhost     │  │   localhost     │     │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Componenti Hardware

### 3.1 Opentrons Flex

**Descrizione**: Sistema di liquid handling robotico per manipolazione automatizzata di liquidi.

**Specifiche**:
- Connessione: USB/Ethernet (IP: 169.254.161.83)
- API: HTTP REST (porta 31950)
- Versione API: 8.8.0
- Robot name: "jntlbot"

**Componenti supportati**:
- **Pipette**: Flex 1-Channel 1000µL, Flex 8-Channel 1000µL, Flex 96-Channel 1000µL
- **Moduli**: Heater-Shaker, Magnetic Block, Temperature Module, Thermocycler
- **Labware**: Piastre 96-well, reservoirs, tip racks (varie capacità)

### 3.2 Tecan M200 Pro

**Descrizione**: Lettore di micropiastre multimodale per misure di assorbanza, fluorescenza e luminescenza.

**Specifiche**:
- Connessione: Seriale via bridge SiLA2
- Server: gRPC (porta 50051)
- Protocolli: Assorbanza, Fluorescenza, Luminescenza, Hybrid

**File di analisi**: `.mdfx` (Tecan Magellan format)
- Posizione: `Library/Analysis/`
- Esempi: TestAbs.mdfx, TestFluo.mdfx, TestLumi.mdfx

### 3.3 Mobile Robot (Simulato)

**Descrizione**: Robot mobile per trasporto di labware tra stazioni.

**Specifiche**:
- Server: gRPC (porta 50053)
- Modalità: Simulazione

---

## 4. Stack Software

### 4.1 File Principali

| File | Descrizione |
|------|-------------|
| `lab_master.py` | Launcher principale, gestione server |
| `lab_console.py` | Console interattiva, controllo diretto |
| `protocol_generator.py` | Generazione protocolli Opentrons |
| `SiLA2/TecanSiLA2Server/` | Server SiLA2 per Tecan |
| `SiLA2/OpentronsSiLA2Server/` | Server SiLA2 per Opentrons |
| `SiLA2/Orchestrator/` | Orchestratore batch |

### 4.2 Struttura Directory

```
v6/
├── docs/                      # Documentazione
├── Library/
│   ├── Analysis/              # Protocolli Tecan (.mdfx)
│   ├── HardwareConfig/        # Configurazioni HAL
│   ├── Protocols/             # Workflow templates
│   ├── Recipes/               # Ricette Opentrons
│   └── Tasks/                 # Task singoli
├── Protocols/                 # Workflow attivi
├── Queue/pending_workflows/   # Coda esecuzione
├── Results/                   # Output (CSV, XML, AnIML)
├── SiLA2/                   # Server SiLA2
├── lab_master.py              # Launcher
├── lab_console.py             # Console interattiva
└── protocol_generator.py      # Generatore protocolli
```

### 4.3 Dipendenze

```python
# Core
httpx          # HTTP async client (Opentrons)
grpcio         # gRPC client (Tecan, Mobile)
pyyaml         # YAML config parsing
protobuf       # gRPC protocol buffers

# Optional
rich           # Console formatting
```

---

## 5. HAL - Hardware Abstraction Layer

### 5.1 Concetto

L'HAL (Hardware Abstraction Layer) permette di separare la **logica del protocollo** dalla **configurazione hardware specifica**. 

**Vantaggi**:
- Stesso protocollo eseguibile su configurazioni diverse
- Cambio rapido di setup senza modificare le ricette
- Validazione automatica della compatibilità hardware

### 5.2 Struttura HAL Config

```json
{
  "Name": "Standard_Flex_Setup",
  "Description": "Standard Opentrons Flex configuration",
  "Labware": {
    "tiprack_1000": {
      "type": "opentrons_flex_96_tiprack_1000ul",
      "slot": "C3"
    },
    "source_plate": {
      "type": "corning_96_wellplate_360ul_flat",
      "slot": "D2"
    }
  },
  "Modules": {
    "heater_shaker": {
      "type": "heaterShakerModuleV1",
      "slot": "D1"
    }
  },
  "Trash": {
    "main": {
      "type": "trashBin",
      "slot": "A3"
    }
  },
  "Pipettes": {
    "left": {
      "type": "flex_1channel_1000",
      "tipracks": ["tiprack_1000"]
    }
  }
}
```

### 5.3 Recipe con Requirements

Le ricette possono dichiarare **Requirements** invece di specifiche hardware:

```json
{
  "Name": "Transfer Protocol",
  "Requirements": {
    "needs_labware": ["tiprack_1000", "source_plate", "dest_plate"],
    "needs_modules": ["heater_shaker"],
    "needs_pipette": "single_channel"
  },
  "Operations": [...]
}
```

### 5.4 Trasformazione HAL

La funzione `transform_recipe_with_hal()` risolve i Requirements:

```
Recipe (Requirements)  +  HAL Config  →  Recipe Completa
     ↓                       ↓                 ↓
needs_labware        →  Labware (type, slot)
needs_modules        →  Modules (type, slot)
needs_pipette        →  Pipettes (type, tipracks)
```

**Algoritmo**:
1. Se la recipe ha `Requirements` e nessun `Labware/Modules`:
   - Mappa ogni requirement al corrispondente item HAL
   - Aggiungi `Trash` dalla config HAL
   - Aggiungi `Pipettes` dalla config HAL
2. Se la recipe è già completa:
   - Aggiungi solo `Pipettes` se mancanti

---

## 6. Sistema Workflow

### 6.1 Concetto

Un **Workflow** è una sequenza di step che coinvolge più strumenti:

```json
{
  "Name": "ELISA_Complete",
  "Description": "Full ELISA workflow",
  "Steps": [
    {"Type": "Opentrons - RunRecipe", "Recipe": "sample_prep.json"},
    {"Type": "Tecan - Connect"},
    {"Type": "Tecan - RunProtocol", "Protocol": "TestAbs.mdfx"},
    {"Type": "Opentrons - Home"}
  ]
}
```

### 6.2 Tipi di Step Supportati

| Step Type | Strumento | Descrizione |
|-----------|-----------|-------------|
| `Opentrons - RunRecipe` | Opentrons | Esegue una ricetta |
| `Opentrons - Pause` | Opentrons | Pausa con messaggio |
| `Opentrons - Home` | Opentrons | Homing robot |
| `Tecan - Connect` | Tecan | Connessione al reader |
| `Tecan - RunProtocol` | Tecan | Esecuzione protocollo .mdfx |

### 6.3 Gestione HAL nei Workflow

**Priorità HAL**:
1. Parametri dello step (`HALConfig` nello step)
2. Default del workflow (`set_default_hal()`)
3. Richiesta runtime all'utente

**Configurazione flessibile**:
```json
// Nel workflow
{"Type": "Opentrons - RunRecipe", "Recipe": "...", "UseHAL": true}

// All'esecuzione: l'utente sceglie quale HAL config usare
```

---

## 7. Sfide Affrontate e Soluzioni

### 7.1 HTTP 201 Trattato Come Errore

**Problema**: L'API Opentrons restituisce HTTP 201 (Created) per risorse create con successo, ma il client lo trattava come errore.

**Causa**: La funzione `_request()` accettava solo status 200:
```python
if res.status_code != 200:  # BUG!
    raise Exception(...)
```

**Soluzione**: Accettare anche 201:
```python
if res.status_code not in (200, 201):
    raise Exception(...)
```

### 7.2 KeyError 'left' - Trasformazione HAL Errata

**Problema**: Eseguendo una ricetta con HAL, il robot restituiva `KeyError: 'left'` (pipetta mancante).

**Causa**: La funzione di merge HAL faceva un semplice `update()` senza risolvere i Requirements:
```python
# SBAGLIATO
final_recipe = {**recipe, **hal_config}  # Sovrascrive tutto!
```

**Analisi del flusso errato**:
1. Recipe con `Requirements: {needs_labware: [tiprack]}` 
2. HAL con `Labware: {tiprack: {type: ..., slot: ...}}`
3. Merge → Recipe ancora con `Requirements` invece di `Labware`
4. Generatore Python cerca `Pipettes` → KeyError!

**Soluzione**: Implementata `transform_recipe_with_hal()`:
```python
def transform_recipe_with_hal(recipe: dict, hal_config: dict) -> dict:
    # 1. Se ha Requirements, risolvi con HAL
    if "Requirements" in recipe and "Labware" not in recipe:
        # Mappa requirements → labware/modules specifici
        for req_name in recipe["Requirements"].get("needs_labware", []):
            if req_name in hal_config.get("Labware", {}):
                final["Labware"][req_name] = hal_config["Labware"][req_name]
        # ... stesso per modules, trash
    
    # 2. Aggiungi sempre Pipettes dalla HAL
    if "Pipettes" not in final and "Pipettes" in hal_config:
        final["Pipettes"] = hal_config["Pipettes"]
    
    return final
```

### 7.3 Tecan NotConnected nel Workflow

**Problema**: Il workflow falliva con errore "Reader not connected" anche se il Tecan era online.

**Causa**: Il workflow eseguiva direttamente `RunProtocol` senza prima connettersi.

**Soluzione**: Aggiunto step `Tecan - Connect` nel workflow composer e validazione:
```python
# Nel workflow
{"Type": "Tecan - Connect"},  # PRIMA
{"Type": "Tecan - RunProtocol", "Protocol": "..."}  # POI
```

### 7.4 Caratteri Unicode Corrotti su Windows

**Problema**: Banner e simboli (✓, ✗, █, ═) visualizzati come caratteri illeggibili su CMD/PowerShell Windows.

**Causa**: Il terminale Windows usa encoding CP1252/CP850 di default, non UTF-8.

**Soluzione**: Sostituiti tutti i caratteri Unicode con ASCII:
```python
# PRIMA (Unicode)
print("✓ OK")
print("═══ Title ═══")

# DOPO (ASCII)
print("[OK] OK")
print("=== Title ===")
```

### 7.5 HAL Hardcoded nei Workflow

**Problema**: Il composer salvava `HALConfig: "Standard_Flex_Setup"` in ogni workflow, limitando la flessibilità.

**Analisi opzioni**:
- **Opzione A**: Specificare HAL nel workflow → Meno flessibile
- **Opzione B**: Chiedere HAL all'esecuzione → Più flessibile

**Soluzione scelta (B)**: HAL non salvato nel workflow, chiesto a runtime:
```python
# Nel workflow JSON
{"Type": "Opentrons - RunRecipe", "UseHAL": true}  # Solo flag

# All'esecuzione
hal_configs = list_hal_configs()
selected = ask_user_to_choose(hal_configs)
executor.set_default_hal(selected)
```

---

## 8. Guida all'Uso

### 8.1 Avvio del Sistema

```bash
# Opzione 1: Menu interattivo
python lab_master.py

# Opzione 2: Comandi diretti
python lab_master.py status          # Stato servizi
python lab_master.py start all       # Avvia tutto
python lab_master.py console         # Console interattiva
```

### 8.2 Console Interattiva

```bash
python lab_console.py
```

**Menu principale**:
```
1. Opentrons Flex (Liquid Handler)
2. Tecan M200 Pro (Plate Reader)
3. Mobile Robot (GoFaGo)
4. Status di tutti i sistemi
5. Workflow Multi-Strumento
6. SiLA2 Feature Discovery
0. Esci
```

### 8.3 SiLA2 Feature Discovery

Il sistema include un modulo di **discovery dinamica** che legge le feature SiLA2 direttamente dai file di definizione:

- **`.sila.xml`**: Feature Definition Language (Opentrons, Mobile Robot)
- **`.proto`**: Protocol Buffers gRPC (Tecan)

**Funzionalità**:
- Lista automatica di tutti i comandi e proprietà disponibili
- Descrizioni e parametri per ogni azione
- Report completo delle feature
- Menu dinamico per ogni server

**File**: `sila_discovery.py`

```python
# Uso programmatico
from sila_discovery import SiLADiscovery

discovery = SiLADiscovery("path/to/v6")
servers = discovery.discover_all()

# Accesso alle feature
opentrons = discovery.get_server("opentrons")
for feature in opentrons.features:
    for cmd in feature.commands:
        print(f"{cmd.display_name}: {cmd.description}")
```

### 8.3 Esecuzione Ricette

1. Menu → **4. Recipe Manager**
2. Seleziona ricetta da `Library/Recipes/`
3. Scegli se usare HAL config
4. Se sì, seleziona config da `Library/HardwareConfig/`
5. Conferma esecuzione

### 8.4 Creazione Workflow

1. Menu → **5. Workflow** → **Compose new workflow**
2. Aggiungi step:
   - `Opentrons - RunRecipe`: Seleziona ricetta
   - `Tecan - Connect`: Connessione reader
   - `Tecan - RunProtocol`: Seleziona .mdfx
3. Salva con nome

### 8.5 Esecuzione Workflow

1. Menu → **5. Workflow** → **Execute workflow**
2. Seleziona workflow
3. Se contiene step Opentrons con HAL:
   - Scegli configurazione HAL
4. Il sistema esegue tutti gli step in sequenza

---

## 9. Troubleshooting

### Errore: "Connection refused"

**Causa**: Server non avviato o porta errata.

**Soluzione**:
```bash
python lab_master.py status  # Verifica stato
python lab_master.py start [servizio]  # Avvia servizio
```

### Errore: "KeyError 'left'" o "KeyError 'right'"

**Causa**: Pipetta non definita nella ricetta o HAL config.

**Soluzione**:
1. Verificare che la HAL config contenga `Pipettes`:
```json
"Pipettes": {
  "left": {"type": "flex_1channel_1000", "tipracks": ["tiprack_1000"]}
}
```
2. Usare `UseHAL: true` nella ricetta/workflow

### Errore: "Reader not connected"

**Causa**: Tecan non connesso prima di RunProtocol.

**Soluzione**: Aggiungere step `Tecan - Connect` prima di `Tecan - RunProtocol`.

### Caratteri illeggibili nel terminale

**Causa**: Encoding UTF-8 non supportato.

**Soluzione**: Già risolto in v6 con caratteri ASCII.

---

## Appendice A: File di Configurazione HAL

### Standard_Flex_Setup.json
Setup standard per Opentrons Flex con pipetta singola 1000µL.

### ELISA_Setup.json
Setup per protocolli ELISA con heater-shaker e piastre multiple.

### SerialDilution_Setup.json
Setup per diluizioni seriali con reservoir e piastra destinazione.

---

## Appendice B: Formato Ricette

```json
{
  "Name": "Nome Protocollo",
  "Author": "Autore",
  "Description": "Descrizione",
  
  "Requirements": {
    "needs_labware": ["tiprack_1000", "source_plate"],
    "needs_modules": [],
    "needs_pipette": "single_channel"
  },
  
  "Operations": [
    {
      "Type": "Transfer",
      "Pipette": "left",
      "Volume": 100,
      "Source": {"Labware": "source_plate", "Wells": ["A1"]},
      "Destination": {"Labware": "dest_plate", "Wells": ["B1"]}
    }
  ]
}
```

---

## Appendice C: Riferimenti

- [SiLA2 Standard](https://sila-standard.com/)
- [Opentrons HTTP API](https://docs.opentrons.com/v2/)
- [gRPC Documentation](https://grpc.io/docs/)

---

*Documento generato il 22 Gennaio 2026*  
*BicoccaLab v6 - Università degli Studi di Milano-Bicocca*
