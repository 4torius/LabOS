# BicoccaLab - Architettura di Sistema

## 📐 Panoramica

BicoccaLab è un sistema di automazione di laboratorio basato sullo **standard SiLA2** (Standardization in Lab Automation). Il sistema coordina un plate reader Tecan M200 Pro e un liquid handler Opentrons Flex attraverso un'architettura a microservizi.

---

## 🏗️ Architettura a Strati

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           PRESENTATION LAYER                                 │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────────────┐ │
│  │   Web Dashboard │  │    Node-RED     │  │      Swagger UI            │ │
│  │   (index.html)  │  │  (port 1880)    │  │   (port 8000/docs)         │ │
│  └────────┬────────┘  └────────┬────────┘  └──────────────┬──────────────┘ │
└───────────┼─────────────────────┼─────────────────────────┼─────────────────┘
            │                     │                         │
            ▼                     ▼                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           REST API LAYER (FastAPI)                          │
│                              http://localhost:8000                          │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                      orchestrator_api.py                             │   │
│  │  • /status          • /tecan/*         • /opentrons/*               │   │
│  │  • /workflows/*     • /library/*       • /emergency-stop            │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────┬───────────────────────────────────┘
                                          │
                                          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         ORCHESTRATION LAYER                                  │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                        orchestrator.py                               │   │
│  │  • LabOrchestrator      - Coordinamento strumenti                   │   │
│  │  • WorkflowEngine       - Parsing ed esecuzione workflow JSON       │   │
│  │  • TecanClient          - Client stub per Tecan                     │   │
│  │  • OpentrionsClient     - Client stub per Opentrons                 │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                        grpc_clients.py                               │   │
│  │  • TecanGRPCClient      - Client gRPC reale per Tecan               │   │
│  │  • OpentrinsGRPCClient  - Client gRPC reale per Opentrons           │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────┬───────────────────────────────────┘
                                          │ gRPC (SiLA2)
                    ┌─────────────────────┴─────────────────────┐
                    ▼                                           ▼
┌──────────────────────────────────────┐  ┌──────────────────────────────────────┐
│     TECAN SiLA2 SERVER (C#/.NET)     │  │   OPENTRONS SiLA2 SERVER (Python)    │
│            Port 50051                │  │            Port 50052                │
│  ┌────────────────────────────────┐  │  │  ┌────────────────────────────────┐  │
│  │   PlateReaderService.proto     │  │  │  │  OpentronsFlex.sila.xml        │  │
│  │   • Connect/Disconnect         │  │  │  │  LiquidHandling.sila.xml       │  │
│  │   • PlateIn/PlateOut           │  │  │  │  ModuleControl.sila.xml        │  │
│  │   • SetTemperature             │  │  │  │  • Initialize/Home             │  │
│  │   • RunMeasurement             │  │  │  │  • Transfer/Distribute/Mix     │  │
│  │   • ListProtocols              │  │  │  │  • HeaterShaker control        │  │
│  └────────────────────────────────┘  │  │  └────────────────────────────────┘  │
│  ┌────────────────────────────────┐  │  │  ┌────────────────────────────────┐  │
│  │   Tecan SDK (iControl)         │  │  │  │   robot_client.py (HTTP)       │  │
│  │   - Measurement engine         │  │  │  │   protocol_generator.py        │  │
│  │   - MDFX protocol parser       │  │  │  │   tip_tracker.py               │  │
│  └────────────────────────────────┘  │  │  └────────────────────────────────┘  │
└──────────────────┬───────────────────┘  └──────────────────┬───────────────────┘
                   │                                         │
                   ▼                                         ▼
┌──────────────────────────────────────┐  ┌──────────────────────────────────────┐
│        TECAN M200 PRO                │  │         OPENTRONS FLEX               │
│        (USB/Network)                 │  │         (HTTP API :31950)            │
└──────────────────────────────────────┘  └──────────────────────────────────────┘
```

---

## 🔌 Standard SiLA2 Compliance

### Cos'è SiLA2?

**SiLA** (Standardization in Lab Automation) è uno standard internazionale per l'interoperabilità degli strumenti di laboratorio. La versione 2 definisce:

1. **Feature Definition Language (FDL)** - XML per descrivere le capacità di uno strumento
2. **Protocol Buffers (protobuf)** - Serializzazione dei messaggi
3. **gRPC** - Protocollo di comunicazione
4. **Patterns** - Observable properties, Commands, Errors

### Implementazione nel Progetto

| Aspetto SiLA2 | Implementazione | File |
|---------------|-----------------|------|
| Feature Definition | XML conforme a SiLA2 v1.1 | `*.sila.xml` |
| Protocol Buffers | Generati da .sila.xml | `*.proto`, `*_pb2.py` |
| gRPC Server | Async server con stub | `server.py`, `Program.cs` |
| Observable Properties | Streaming gRPC | `IsConnected`, `Temperature` |
| Commands | Unary e Server-streaming | `RunMeasurement`, `Transfer` |
| Defined Execution Errors | Custom error types | `OutOfTips`, `InvalidWell` |
| Metadata | Vendor, Model info | `<Metadata>` tag in FDL |

### Features Implementate

#### TecanSiLA2Server
```
Feature: PlateReaderService (it.chemlab.platereader)
├── Properties (Observable)
│   ├── IsConnected: Boolean
│   ├── OperationalStatus: Enum[Idle, Busy, Error, Disconnected]
│   ├── CurrentTemperature: Real
│   └── InstrumentInfo: Structure
│
└── Commands
    ├── Connect(connection_string) → success
    ├── Disconnect() → success
    ├── PlateIn() → success
    ├── PlateOut() → success
    ├── SetTemperature(target) → success
    ├── TurnOffTemperature() → success
    ├── RunMeasurement(protocol, plate_id) → stream[progress, result]
    ├── GetAnIMLResult(plate_id) → animl_document
    └── ListProtocols() → protocols[]
```

#### OpentronsSiLA2Server
```
Feature: OpentronsFlex (it.chemicallab.robot)
├── Commands
│   ├── Initialize() → result
│   ├── Home() → result [Observable]
│   ├── EmergencyStop() → result
│   ├── RunProtocol(content, type) → run_id [Observable]
│   ├── AbortRun(run_id) → result
│   ├── PauseRun(run_id) → result
│   └── ResumeRun(run_id) → result

Feature: LiquidHandling (it.chemicallab.liquidhandling)
├── Commands
│   ├── Transfer(volume, source, dest, pipette, new_tip) → result
│   ├── Distribute(volume, source, dests[], pipette) → result
│   ├── Consolidate(volume, sources[], dest, pipette) → result
│   ├── Mix(volume, location, pipette, repetitions) → result
│   ├── PickUpTip(pipette, location) → result
│   └── DropTip(pipette, location) → result
└── Defined Errors: OutOfTips, InvalidWell, VolumeOutOfRange

Feature: ModuleControl (it.chemicallab.modules)
├── Commands
│   ├── HeaterShakerSetTemperature(module, temp, wait) → result
│   ├── HeaterShakerShake(module, rpm, duration) → result
│   ├── HeaterShakerOpenLatch(module) → result
│   ├── HeaterShakerCloseLatch(module) → result
│   ├── HeaterShakerDeactivate(module) → result
│   └── MoveLabware(labware, from, to) → result
└── Defined Errors: LatchNotClosed, ShakerRunning
```

---

## 🧩 Ruolo di Ogni Componente

### 1. **Web Dashboard** (`static/index.html`)
- **Ruolo**: Interfaccia utente principale
- **Funzionalità**: 
  - Monitoraggio stato strumenti in tempo reale
  - Esecuzione comandi rapidi (temperature, movimenti)
  - Selezione e avvio workflow
  - Log console per debug
  - Accesso a Node-RED per protocolli complessi

### 2. **Orchestrator API** (`orchestrator_api.py`)
- **Ruolo**: Gateway REST per tutte le operazioni
- **Tecnologia**: FastAPI con async/await
- **Funzionalità**:
  - Traduce HTTP → gRPC
  - Gestisce autenticazione e CORS
  - Espone endpoint per ogni operazione
  - Serve file statici (Web UI)

### 3. **Orchestrator** (`orchestrator.py`)
- **Ruolo**: Motore di coordinamento centrale
- **Funzionalità**:
  - Parsing workflow JSON
  - Scheduling step sequenziali/paralleli
  - Gestione retry e error handling
  - Logging operazioni
  - Sostituzione variabili `${var}`

### 4. **gRPC Clients** (`grpc_clients.py`)
- **Ruolo**: Comunicazione diretta con server SiLA2
- **Tecnologia**: grpcio async
- **Funzionalità**:
  - Connessione persistente ai server
  - Chiamate RPC tipizzate
  - Gestione timeout e riconnessione
  - Conversione response → dict Python

### 5. **Tecan SiLA2 Server** (`TecanSiLA2Server/`)
- **Ruolo**: Interfaccia SiLA2 per Tecan M200 Pro
- **Tecnologia**: C# / .NET Framework 4.8
- **Componenti**:
  - `Program.cs` - Entry point e server gRPC
  - `Features/` - Implementazione comandi SiLA2
  - `Instrument/` - Bridge verso Tecan SDK
  - `AnIML/` - Convertitore formato ASTM E1947

### 6. **Opentrons SiLA2 Server** (`OpentronsSiLA2Server/`)
- **Ruolo**: Interfaccia SiLA2 per Opentrons Flex
- **Tecnologia**: Python 3.11+ con grpcio
- **Componenti**:
  - `server.py` - Server gRPC principale
  - `robot_client.py` - Client HTTP per API Opentrons
  - `protocol_generator.py` - JSON recipe → Python protocol
  - `tip_tracker.py` - Gestione stato puntali

### 7. **Node-RED** (`nodered/`)
- **Ruolo**: Programmazione visuale di protocolli
- **Funzionalità**:
  - Editor drag-and-drop
  - Integrazione con API REST
  - Dashboard personalizzabile
  - Flow esportabili come JSON

### 8. **Workflow Engine**
- **Ruolo**: Interprete file `.workflow.json`
- **Funzionalità**:
  - Caricamento da `Protocols/`
  - Variable substitution
  - Dependency resolution
  - Step execution con retry

---

## 📁 Struttura Directory

```
BicoccaLab/
├── config.yaml                 # Configurazione globale
├── StartLab.bat               # Avvio completo sistema
├── StartAPI.bat               # Solo API
├── StartNodeRED.bat           # Solo Node-RED
│
├── docs/                      # Documentazione
│   ├── QUICKSTART.md
│   ├── TECHNICAL.md
│   └── ARCHITECTURE.md        # Questo file
│
├── Library/                   # Libreria metodi
│   ├── Recipes/               # JSON recipes Opentrons
│   ├── Analysis/              # MDFX files Tecan
│   └── Tasks/                 # Task riutilizzabili
│
├── Protocols/                 # Workflow orchestrati
│   ├── *.workflow.json
│   └── workflow.schema.json
│
├── Results/                   # Output
│   ├── CSV/
│   ├── XML/
│   └── AnIML/
│
├── nodered/                   # Node-RED configuration
│   ├── flows.json
│   ├── settings.js
│   └── bicoccalab-theme.css
│
└── SiLA2/                   # Core del sistema
    ├── orchestrator.py        # Motore workflow
    ├── orchestrator_api.py    # REST API
    ├── grpc_clients.py        # Client gRPC
    ├── static/index.html      # Web UI
    ├── HardwareConfig/        # Config hardware
    ├── TecanSiLA2Server/      # Server C#
    └── OpentronsSiLA2Server/  # Server Python
```

---

## 🌐 API Endpoints Reference

### Status & Info
| Endpoint | Metodo | Descrizione |
|----------|--------|-------------|
| `/` | GET | Redirect a Web UI |
| `/ui` | GET | Interfaccia web |
| `/api` | GET | Info API |
| `/status` | GET | Stato tutti gli strumenti |
| `/docs` | GET | Swagger UI |

### Tecan M200 Pro
| Endpoint | Metodo | Descrizione |
|----------|--------|-------------|
| `/tecan/status` | GET | Stato dettagliato |
| `/tecan/temperature` | GET | Temperatura corrente |
| `/tecan/temperature` | POST | Imposta temperatura |
| `/tecan/temperature/off` | POST | Spegni controllo temperatura |
| `/tecan/plate/in` | POST | Piatto dentro |
| `/tecan/plate/out` | POST | Piatto fuori |
| `/tecan/connect` | POST | Connetti a iControl |
| `/tecan/disconnect` | POST | Disconnetti |
| `/tecan/protocols` | GET | Lista protocolli |
| `/tecan/analysis` | POST | Esegui analisi |
| `/tecan/reconnect` | POST | Riconnetti al server |

### Opentrons Flex
| Endpoint | Metodo | Descrizione |
|----------|--------|-------------|
| `/opentrons/status` | GET | Stato dettagliato |
| `/opentrons/home` | POST | Home robot |
| `/opentrons/pause` | POST | Pausa run |
| `/opentrons/resume` | POST | Riprendi run |
| `/opentrons/abort` | POST | Annulla run |
| `/opentrons/emergency` | POST | STOP emergenza |
| `/opentrons/tips` | GET | Stato punte |
| `/opentrons/tips/reset` | POST | Reset punte |
| `/opentrons/recipe` | POST | Esegui recipe |
| `/opentrons/reconnect` | POST | Riconnetti al server |

### Workflow & Library
| Endpoint | Metodo | Descrizione |
|----------|--------|-------------|
| `/workflows` | GET | Lista workflow |
| `/workflows/run` | POST | Esegui workflow |
| `/library/recipes` | GET | Lista recipes |
| `/library/analysis` | GET | Lista metodi analisi |
| `/results` | GET | Lista risultati |

### Server Management
| Endpoint | Metodo | Descrizione |
|----------|--------|-------------|
| `/servers/{server}/start` | POST | Avvia server (tecan/opentrons/nodered) |
| `/servers/{server}/stop` | POST | Ferma/disconnetti server |
| `/servers/status` | GET | Stato tutti i server |

### Emergency
| Endpoint | Metodo | Descrizione |
|----------|--------|-------------|
| `/emergency-stop` | POST | STOP di emergenza globale |

---

## 🔄 Flusso di Esecuzione Tipico

### Esecuzione Workflow
```
1. User seleziona workflow da Web UI
2. POST /workflows/run → orchestrator_api.py
3. WorkflowEngine carica e parsa JSON
4. Per ogni step:
   a. Determina strumento target (tecan/opentrons)
   b. Chiama client gRPC appropriato
   c. gRPC → SiLA2 Server
   d. Server esegue comando su strumento
   e. Risultato ritorna via gRPC
   f. Orchestrator logga e procede
5. Risultati salvati in Results/
6. Notifica completamento a Web UI
```

### Comando Diretto (es. SetTemperature)
```
1. User clicca "Imposta Temperatura" in Web UI
2. POST /tecan/temperature {temperature: 37}
3. orchestrator_api.py → tecan_client.set_temperature(37)
4. grpc_clients.py → gRPC call a localhost:50051
5. TecanSiLA2Server riceve SetTemperatureRequest
6. Chiama Tecan SDK per impostare temperatura
7. Risposta SetTemperatureResponse{success: true}
8. API risponde JSON {"success": true}
9. Web UI mostra toast "Temperatura impostata"
```

---

## 🔐 Sicurezza

- **Rete locale**: Sistema progettato per LAN
- **CORS**: Abilitato per sviluppo (`allow_origins=["*"]`)
- **Autenticazione**: Non implementata (aggiungere per produzione)
- **gRPC**: Insecure channel (aggiungere TLS per produzione)

---

## 📈 Performance

| Metrica | Valore Tipico |
|---------|---------------|
| Latenza REST API | < 10ms |
| Latenza gRPC | < 5ms |
| Tempo connessione strumento | 2-5s |
| Polling stato | 5s (configurabile) |

---

## 🔧 Estensibilità

Per aggiungere un nuovo strumento:
1. Creare Feature Definition XML
2. Generare proto/stub
3. Implementare server SiLA2
4. Aggiungere client in `grpc_clients.py`
5. Esporre endpoint in `orchestrator_api.py`
6. Aggiungere card in Web UI
