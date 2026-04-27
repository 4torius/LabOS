# LabOS — Architettura del Sistema

## Indice

1. [Panoramica](#panoramica)
2. [Struttura delle directory](#struttura-delle-directory)
3. [Moduli principali](#moduli-principali)
4. [Flusso di discovery](#flusso-di-discovery)
5. [Flusso di esecuzione comandi](#flusso-di-esecuzione-comandi)
6. [Flusso di esecuzione workflow](#flusso-di-esecuzione-workflow)
7. [Server SiLA2](#server-sila2)
8. [WebApp](#webapp)
9. [CLI](#cli)
10. [Proto e gRPC](#proto-e-grpc)
11. [Strutture dati](#strutture-dati)
12. [Aggiungere un nuovo strumento](#aggiungere-un-nuovo-strumento)

---

## Panoramica

LabOS è un sistema di automazione di laboratorio **plug & play**: ogni strumento fisico è accessibile tramite un server SiLA2 autonomo. Il sistema centrale non conosce nulla degli strumenti a priori — li scopre a runtime via gRPC, si adatta ai loro comandi dinamicamente, e costruisce la UI senza nessun hardcoding.

```
                        ┌─────────────────────────────────────────────────┐
                        │                  Interfacce utente               │
                        │   CLI (pnp_console.py)   WebApp (webapp/app.py) │
                        └──────────────────┬──────────────────────────────┘
                                           │  chiama
                                           ▼
                        ┌─────────────────────────────────────────────────┐
                        │               LabCore (src/lab_core.py)          │
                        │  • discover()     • execute_command()            │
                        │  • list_files()   • run_workflow()               │
                        └───────┬──────────────────────┬───────────────────┘
                                │ usa                  │ usa
                     ┌──────────▼───────┐   ┌──────────▼──────────┐
                     │  PnPDiscovery    │   │   PnPClient         │
                     │  (discovery.py)  │   │   (pnp_client.py)   │
                     └──────────┬───────┘   └──────────┬──────────┘
                                │ gRPC                 │ gRPC
                    ┌───────────┼──────────────────────┤
                    ▼           ▼                      ▼
           ┌────────────┐ ┌────────────┐      ┌──────────────┐
           │  Opentrons │ │  Manual    │  ... │  Tecan C#    │
           │  SiLA2     │ │  Station   │      │  SiLA2       │
           │  (Python)  │ │  (Python)  │      │  (C#/.NET)   │
           └────────────┘ └────────────┘      └──────────────┘
```

**Principio cardine**: i server sono l'unica fonte di verità. Tutto ciò che il sistema sa di uno strumento viene chiesto al server stesso tramite `GetServerInfo`, `GetFeatures`, `GetStatus`, `ExecuteCommand`.

---

## Struttura delle directory

```
LabOS/
├── .venv/                          # Virtualenv condiviso (Python)
└── v1/
    ├── lab_config.yaml             # Config centrale: server, discovery, UI
    ├── launcher.py                 # Entry point: avvia server + webapp
    ├── regen_stubs.py              # Rigenera gRPC stubs da .proto
    ├── pnp_console.py              # CLI interattiva
    │
    ├── src/                        # Core logic
    │   ├── lab_core.py             # Interfaccia unificata (singleton)
    │   ├── pnp_discovery.py        # Discovery multi-sorgente
    │   ├── pnp_client.py           # Client gRPC generico
    │   ├── pnp_workflow_executor.py# Esecuzione workflow JSON
    │   ├── pnp_stubs/              # Stub gRPC generati (pb2.py)
    │   ├── sila2_stub_generator.py # Generazione stub a runtime
    │   ├── excel_recipe_parser.py  # Parser ricette da Excel
    │   └── sila2_server_template.py# Template per nuovi server
    │
    ├── SiLA2/
    │   ├── SiLA2Common.proto       # Proto CANONICO (Python-side)
    │   ├── sila2_common_servicer.py# Helper: aggiunge SiLA2Common a qualsiasi server
    │   ├── sila2_mdns_registry.py  # Registrazione mDNS (Zeroconf)
    │   ├── sila2_xml_parser.py     # Parser .sila.xml → dict Python
    │   ├── OpentronsSiLA2Server/   # Server Opentrons Flex (Python)
    │   ├── ManualStationSiLA2Server/ # Server stazione manuale (Python)
    │   ├── TecanSiLA2Server/       # Server Tecan M200 Pro (C#/.NET)
    │   └── _NewInstrumentTemplate/ # Template per nuovo strumento
    │
    ├── webapp/
    │   ├── app.py                  # FastAPI: routes, WebSocket, startup
    │   └── templates/              # Jinja2 HTML templates
    │
    ├── Library/
    │   ├── Recipes/                # JSON ricette Opentrons
    │   ├── Workflows/              # JSON workflow multi-step
    │   ├── Analysis/               # Protocolli Tecan (.mdfx)
    │   ├── HardwareConfig/         # HAL configuration
    │   ├── MobileTasks/            # Task robot mobile
    │   └── Results/                # Output dati esperimenti
    │
    └── tests/
        ├── conftest.py             # Fixtures condivise (base_dir, port checks)
        ├── test_xml_parser.py      # Unit test parser XML
        ├── test_discovery.py       # Unit + integration test discovery
        ├── test_server_info.py     # Integration test gRPC per server
        └── test_integration.py     # End-to-end via LabCore
```

---

## Moduli principali

### `src/lab_core.py` — Interfaccia unificata

**Ruolo**: Singleton condiviso tra CLI e WebApp. Coordina discovery, esecuzione comandi, accesso ai file Library, e avvio workflow.

**Stato interno**:
```python
_instruments: Dict[str, Instrument]      # cache strumenti scoperti
_discovery: PnPDiscovery                 # istanza discovery
_registry: PnPRegistry                   # istanza client gRPC
_last_discovery_time: float              # timestamp ultima discovery
_discovery_cache_ttl: float = 10.0      # TTL cache in secondi
```

**API pubblica**:
| Metodo | Descrizione |
|--------|-------------|
| `discover(timeout)` | Trova strumenti online, ritorna lista `Instrument` |
| `list_instruments()` | Lista strumenti dall'ultima discovery (senza gRPC) |
| `execute_command(id, cmd, params)` | Esegue comando su strumento, ritorna `ExecutionResult` |
| `list_files(category)` | Lista file da `Library/` (recipes, analyses, hal, workflows) |
| `run_workflow(path, callbacks)` | Esegue workflow JSON multi-step |
| `get_lab_core()` | Funzione globale che ritorna il singleton |

**Cache TTL**: `discover()` ritorna il risultato cachato se chiamato entro 10s dall'ultima discovery. Questo evita decine di roundtrip gRPC su ogni chiamata API HTTP.

---

### `src/pnp_discovery.py` — Discovery multi-sorgente

**Ruolo**: Trova tutti i server SiLA2 disponibili sulla rete e li interroga per costruire la loro rappresentazione completa (nome, comandi, features).

**4 sorgenti di discovery** (parallele):

```
discover_all()
    ├── _discover_via_mdns()         # UDP multicast 5353 — istantaneo se mDNS funziona
    ├── _discover_from_config()      # lab_config.yaml → TCP + gRPC check
    ├── _discover_from_directories() # Scansiona SiLA2/*/ per config.yaml
    └── _discover_via_port_scan()    # TCP sweep 50051–50100 (fallback)
```

Per ogni server trovato vivo (TCP ok), chiama `_query_server_info()`:
1. `GetServerInfo` → nome, tipo, versione, vendor
2. `GetFeatures` → lista feature con comandi, parametri, properties
3. `GetStatus` → stato online/offline/busy

Il risultato finale è una lista di `PnPServer` con tutti i metadati.

**Nota mDNS su Windows**: il firewall blocca spesso UDP 5353. La discovery fallisce silenziosamente e usa le sorgenti di fallback (config + directory scan). Non è un errore.

---

### `src/pnp_client.py` — Client gRPC generico

**Ruolo**: Esegue comandi su qualsiasi server SiLA2 senza sapere a priori cosa il server implementa.

**Strategia di esecuzione** (in ordine di priorità):
1. **`SiLA2Common.ExecuteCommand`** — via stub pre-generato `pnp_stubs/`. È il canale principale.
2. **Stub dinamico** — se il server ha stub generati (`*_pb2.py`), li carica con `importlib` a runtime.
3. **Reflection gRPC** — introspection del server per scoprire metodi disponibili (fallback).

**`PnPRegistry`**: tiene un pool di connessioni gRPC aperte (una per server). Le connessioni vengono riusate tra chiamate successive.

**`CommandResult`**:
```python
@dataclass
class CommandResult:
    success: bool
    data: Dict[str, Any]       # Risultato strutturato dal server
    error: Optional[str]       # Messaggio di errore se success=False
    is_streaming: bool         # True se risposta a chunks
    progress: float            # 0.0–1.0 per comandi lunghi
```

---

### `src/pnp_workflow_executor.py` — Esecuzione workflow

**Ruolo**: Esegue file `.workflow.json` da `Library/Workflows/`. Un workflow è una sequenza di step, ciascuno con strumento, comando, parametri, e dipendenze da altri step.

**Struttura workflow JSON**:
```json
{
  "name": "Esperimento X",
  "steps": [
    {
      "id": "prepare",
      "instrument": "Opentrons Flex",
      "command": "ExecuteRecipe",
      "params": { "RecipeName": "aliquoting.json" },
      "depends_on": []
    },
    {
      "id": "measure",
      "instrument": "Tecan M200 Pro",
      "command": "RunProtocol",
      "params": { "ProtocolFile": "absorbance.mdfx" },
      "depends_on": ["prepare"]
    }
  ]
}
```

**Esecuzione**:
1. Valida che tutti gli strumenti referenziati siano online
2. Costruisce un grafo delle dipendenze
3. Esegue in parallelo gli step senza dipendenze pendenti
4. Su errore: categorizza (`ErrorCategory`) → decide strategia (retry, skip, intervention)
5. Se non recuperabile: emette `InterventionRequest` → WebApp mostra dialog all'operatore

**Categorie di errore**:
| Categoria | Azione |
|-----------|--------|
| `DEVICE_UNAVAILABLE` | Retry con backoff esponenziale (max 3×) |
| `OPERATION_FAILURE` | Retry immediato (1×) |
| `HARDWARE_ERROR` | Intervention request all'operatore |
| `TIMEOUT` | Retry con timeout esteso |
| `VALIDATION_ERROR` | Abort immediato (parametri sbagliati) |

---

## Flusso di discovery

```
CLI o WebApp chiama lab_core.discover(timeout=2.0)
        │
        ▼
[cache valida? → ritorna cache istantaneamente]
        │ no
        ▼
PnPDiscovery.discover_all(timeout=2.0)
        │
        ├── asyncio.gather():
        │       ├── _discover_via_mdns()       → UDP 5353, attende timeout
        │       ├── _discover_from_config()    → legge lab_config.yaml, TCP check, gRPC check
        │       ├── _discover_from_directories()→ scansiona SiLA2/*/, legge config.yaml locale
        │       └── _discover_via_port_scan()  → TCP sweep 50051-50100
        │
        ▼ (per ogni IP:porta trovato vivo)
_query_server_info(host, port)
        │
        ├── GetServerInfo(GetServerInfoRequest)
        │       → server_name, server_type, vendor, version, uuid
        │
        ├── GetFeatures(GetFeaturesRequest)
        │       → lista Feature { identifier, commands[], properties[] }
        │         ogni Command { identifier, parameters[], observable }
        │
        └── GetStatus(GetStatusRequest)
                → status, server_online, hardware_online, hardware_status
        │
        ▼
PnPServer(name, host, port, features, server_online)
        │
        ▼ (in lab_core)
Instrument(id=name.lower().replace(" ","_"), name, status, commands)
        │
        ▼
self._instruments[id] = instrument
self._last_discovery_time = time.monotonic()
```

---

## Flusso di esecuzione comandi

```
lab_core.execute_command("opentrons_flex", "ExecuteRecipe", {"RecipeName": "test.json"})
        │
        ▼
Cerca strumento in self._instruments["opentrons_flex"]
        │ non trovato → ExecutionResult(success=False, error="Instrument not found")
        │ trovato ↓
        ▼
PnPRegistry.execute(server, "ExecuteRecipe", params)
        │
        ▼
PnPClient._execute_via_sila2common(server, command_id, params)
        │
        ├── Crea/riusa gRPC channel: grpc.insecure_channel("localhost:50057")
        │
        ├── stub = SiLA2Common_pb2_grpc.SiLA2CommonStub(channel)
        │
        ├── request = ExecuteCommandRequest(
        │       command_id = "ExecuteRecipe",
        │       parameters = { "RecipeName": "test.json" }   # map<string, string>
        │   )
        │
        ├── response = stub.ExecuteCommand(request, timeout=30)
        │       Server riceve → esegue → ritorna:
        │           ExecuteCommandResponse(
        │               success=True,
        │               result={"status": "completed"},
        │               progress=1.0
        │           )
        │
        └── CommandResult(success=True, data={"status": "completed"})
        │
        ▼
ExecutionResult(success=True, data={"status": "completed"})
```

---

## Flusso di esecuzione workflow

```
lab_core.run_workflow("Library/Workflows/experiment.workflow.json")
        │
        ▼
PnPWorkflowExecutor.execute(workflow, registry, intervention_callback)
        │
        ├── Valida strumenti: tutti i nomi nel workflow sono strumenti online?
        │
        ├── Costruisce grafo dipendenze: { step_id → set(step_ids_da_attendere) }
        │
        ├── Loop finché step pendenti:
        │       ready = [step per cui tutti i depends_on sono completati]
        │       asyncio.gather(*[_execute_step(s) for s in ready])
        │
        ├── Per ogni step:
        │       result = registry.execute(instrument, command, params)
        │       if not result.success:
        │           err = CategorizedError.from_exception(...)
        │           if err.recoverable: retry con backoff
        │           else: intervention_callback(InterventionRequest(...))
        │                 await operatore sceglie: Retry / Skip / Abort
        │
        └── WorkflowResult(completed_steps, failed_steps, duration)
```

---

## Server SiLA2

Ogni server implementa il contratto **SiLA2Common** (5 RPC obbligatorie):

| RPC | Request | Response | Descrizione |
|-----|---------|----------|-------------|
| `GetServerInfo` | `GetServerInfoRequest` | `ServerInfoResponse` | Identità server |
| `GetFeatures` | `GetFeaturesRequest` | `FeaturesResponse` | Lista feature/comandi |
| `GetStatus` | `GetStatusRequest` | `StatusResponse` | Stato hardware/software |
| `ExecuteCommand` | `ExecuteCommandRequest` | stream `ExecuteCommandResponse` | Esegue un comando |
| `GetProperty` | `GetPropertyRequest` | `PropertyResponse` | Legge una proprietà |

### Server Python (Opentrons, ManualStation)

```
OpentronsSiLA2Server/
├── main.py              # Entry point: crea gRPC server, registra servicer, avvia mDNS
├── config.yaml          # port, server_name, hardware settings
├── features/
│   └── WorkflowAPI.sila.xml   # Definizione feature: comandi, parametri
└── src/
    ├── sila2_common_adapter.py # Legge .sila.xml, implementa GetFeatures/GetServerInfo/GetStatus
    └── workflow_service.py     # Logica specifica: esecuzione ricette Opentrons
```

**Flusso startup** (Python):
1. `main.py` legge `config.yaml`
2. Crea `grpc.server()`
3. Aggiunge `SiLA2CommonAdapter` (che legge `features/*.sila.xml`)
4. Aggiunge servicer specifico (`WorkflowServiceServicer`)
5. Registra mDNS via `sila2_mdns_registry.py` (tipo: `_sila2._tcp.local.`)
6. `server.start()` → bind porta

**`sila2_common_servicer.py`**: helper riusabile. Prende una cartella `features/`, parsa tutti i `.sila.xml`, e risponde a `GetFeatures` con la lista risultante. Ogni server Python lo instanzia passando il proprio path features.

### Server C# (Tecan)

```
TecanSiLA2Server/
├── Program.cs               # Entry point: ASP.NET Core DI + Hosted Service
├── appsettings.json         # Config: porta, connection string, simulazione
├── ServerConfiguration.cs   # DTO config typed
├── Protos/
│   └── SiLA2Common.proto    # DEVE essere identico al proto Python (field numbers!)
├── Features/
│   └── SiLA2CommonServiceImpl.cs  # Implementazione 5 RPC SiLA2Common
└── Services/
    ├── TecanBridge.cs        # Wrapper Tecan SDK (COM via STA thread)
    └── MdnsService.cs        # Registrazione mDNS (.NET Zeroconf)
```

**Flusso startup** (C#):
1. `Program.cs` legge `appsettings.json` → `ServerConfiguration`
2. DI container registra `TecanBridge`, `SiLA2CommonServiceImpl`, `MdnsService`
3. `SiLA2ServerHostedService.StartAsync()`:
   - Crea `Grpc.Core.Server`
   - Aggiunge `SiLA2Common.BindService(serviceImpl)`
   - `server.Start()`
   - `_mdnsService.Register(port, "PlateReaderService")`

**Compatibilità proto cross-language**: il file `.proto` C# (`Protos/SiLA2Common.proto`) deve avere gli stessi **field numbers** del proto Python (`SiLA2/SiLA2Common.proto`). I nomi dei campi non contano sul wire — contano solo i numeri. Una discrepanza causa corruzione silenziosa dei dati (es. un `bool` letto come stringa vuota).

---

## WebApp

`webapp/app.py` è un'applicazione **FastAPI** con Jinja2 templates e WebSocket.

**Startup** (all'avvio uvicorn):
```python
@app.on_event("startup")
async def startup():
    lab_core = get_lab_core()               # Singleton condiviso con CLI
    instruments = await lab_core.discover() # Discovery iniziale
    for inst in instruments:
        state.devices[inst.id] = DeviceState(...)
```

**Route principali**:
| Route | Metodo | Descrizione |
|-------|--------|-------------|
| `/` | GET | Dashboard HTML (lista strumenti) |
| `/api/instruments` | GET | Lista strumenti JSON (chiama `lab_core.discover()`) |
| `/api/instruments/{name}/commands` | GET | Comandi dello strumento (da cache discovery) |
| `/api/execute` | POST | Esegue comando → `lab_core.execute_command()` |
| `/api/workflows` | GET | Lista workflow da `Library/Workflows/` |
| `/api/workflows/{name}/run` | POST | Avvia workflow → `lab_core.run_workflow()` |
| `/api/library/{category}` | GET | Lista file Library (recipes, analyses, ecc.) |
| `/ws` | WebSocket | Ping/pong per aggiornamenti real-time UI |

**`LAB_CORE_AVAILABLE`**: flag booleano impostato all'import. Se `get_lab_core` o `PnPDiscovery` non sono importabili (grpc mancante, path sbagliato), il flag è `False` e tutte le route `/api/*` ritornano errore 503. Il flag viene controllato a ogni chiamata.

**UI dinamica**: i dropdown nelle form HTML non sono hardcodati. Vengono generati dall'endpoint `/api/instruments/{name}/commands` che legge i parametri dal `PnPServer.features` discoverto. Nessun template HTML conosce i nomi dei comandi.

---

## CLI

`pnp_console.py` è una CLI interattiva a menu testuali.

**Struttura**:
1. All'avvio: `lab_core.discover()` → mostra lista strumenti trovati
2. Menu principale: scegli strumento
3. Menu strumento: scegli feature → scegli comando
4. Input parametri: generato dai metadati `PnPCommand.parameters`
5. Esecuzione: `lab_core.execute_command()` → mostra `CommandResult`

La CLI usa lo stesso `LabCore` singleton della WebApp — se entrambi sono aperti, condividono la cache discovery.

---

## Proto e gRPC

### File canonico

`v1/SiLA2/SiLA2Common.proto` è la fonte di verità. Tutti gli stub Python vengono generati da questo file.

### Struttura messaggi principali

```protobuf
// Identità del server
message ServerInfoResponse {
    string server_name = 1;
    string server_type = 2;
    string vendor = 3;
    string version = 4;
    string uuid = 5;
    bool hardware_connected = 6;
    string hardware_status = 7;
}

// Lista feature esposte
message Feature {
    string identifier = 1;
    string display_name = 2;
    string description = 3;
    string version = 4;       // FIELD 4 (non 5!)
    string category = 5;      // FIELD 5 (non 4!)
    repeated Command commands = 6;
    repeated Property properties = 7;
}

// Stato runtime
message StatusResponse {
    string status = 1;           // "online" | "offline" | "busy" | "error"
    bool server_online = 2;      // BOOL (non string!)
    bool hardware_online = 3;
    string hardware_status = 4;
    string error_message = 5;
    int64 uptime_seconds = 6;
    map<string, string> details = 7;
}

// Esecuzione comando
message ExecuteCommandRequest {
    string command_id = 1;
    map<string, string> parameters = 2;
    string feature_id = 3;
    int32 timeout_seconds = 4;
}

message ExecuteCommandResponse {
    bool success = 1;
    bool is_intermediate = 2;
    float progress = 3;
    string status = 4;
    map<string, string> result = 5;
    string error = 6;
    SiLA2Error sila2_error = 7;
}
```

### Rigenera stubs Python

```bash
cd v1
python regen_stubs.py
# Genera: src/pnp_stubs/SiLA2Common_pb2.py
#         src/pnp_stubs/SiLA2Common_pb2_grpc.py
```

### Regola critica cross-language

I field **numbers** (non i nomi) determinano il wire format di protobuf. Se il proto C# ha `version=5` dove il proto Python ha `version=4`, i due lati decodificano campi diversi in modo silenzioso. Verificare sempre che i field numbers corrispondano esattamente.

---

## Strutture dati

### Discovery output

```python
@dataclass
class PnPCommand:
    identifier: str          # "ExecuteRecipe"
    display_name: str        # "Execute Recipe"
    description: str
    parameters: List[dict]   # [{"identifier": "RecipeName", "data_type": "string", ...}]
    observable: bool         # True se il comando emette risposte streaming

@dataclass
class PnPFeature:
    identifier: str          # "WorkflowAPI"
    display_name: str
    description: str
    commands: List[PnPCommand]
    properties: List[dict]

@dataclass
class PnPServer:
    name: str                # "Opentrons Flex"
    host: str                # "localhost"
    port: int                # 50057
    server_type: str         # "liquid_handler"
    features: List[PnPFeature]
    server_online: bool
    # Proprietà derivate:
    # .address → "localhost:50057"
    # .get_all_commands() → generator (feature_id, cmd_id, PnPCommand)
```

### LabCore output

```python
@dataclass
class Instrument:
    id: str                  # "opentrons_flex" (da name.lower().replace(" ","_"))
    name: str                # "Opentrons Flex"
    status: str              # "online" | "offline" | "busy" | "error"
    commands: List[dict]     # Lista comandi piatta con feature_id incluso
    host: str
    port: int

@dataclass
class ExecutionResult:
    success: bool
    data: Dict[str, Any]
    error: Optional[str]
    instrument_id: str
    command_id: str
    duration_ms: float
```

---

## Aggiungere un nuovo strumento

Il sistema è progettato per non richiedere modifiche al codice core. I passaggi sono:

1. **Copia il template**:
   ```bash
   cp -r v1/SiLA2/_NewInstrumentTemplate v1/SiLA2/MyInstrumentSiLA2Server
   ```

2. **Definisci i comandi** in `features/MyFeature.sila.xml`:
   ```xml
   <Feature>
     <Identifier>MyFeature</Identifier>
     <Command>
       <Identifier>DoSomething</Identifier>
       <Parameter><Identifier>param1</Identifier><DataType>String</DataType></Parameter>
     </Command>
   </Feature>
   ```

3. **Implementa la logica** in `src/my_service.py` (eredita il servicer generato)

4. **Configura** `config.yaml` con porta e parametri hardware

5. **Aggiungi a `lab_config.yaml`**:
   ```yaml
   servers:
     my_instrument:
       host: localhost
       port: 50099
       startup_command: "python SiLA2/MyInstrumentSiLA2Server/main.py"
   ```

6. **Avvia il server** — viene scoperto automaticamente via mDNS o config. CLI e WebApp lo mostrano immediatamente senza restart.

Per i dettagli tecnici completi: `v1/SiLA2/ADDING_INSTRUMENTS.md`.

---

## Riferimenti

| File | Scopo |
|------|-------|
| `v1/lab_config.yaml` | Config centrale: porte, discovery, UI dropdowns |
| `v1/SiLA2/SiLA2Common.proto` | Schema protobuf canonico |
| `v1/SiLA2/ADDING_INSTRUMENTS.md` | Guida step-by-step per nuovi strumenti |
| `v1/TROUBLESHOOTING.md` | Diagnosi problemi comuni |
| `v1/tests/` | Suite pytest: unit + integration |
