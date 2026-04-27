# BicoccaLab - Documentazione Tecnica

Guida tecnica per sviluppatori che vogliono estendere o modificare il sistema.

## Indice

1. [Architettura Software](#architettura-software)
2. [Server SiLA2](#server-sila2)
3. [Orchestrator](#orchestrator)
4. [API REST](#api-rest)
5. [Node-RED Integration](#node-red-integration)
6. [Formato Workflow](#formato-workflow)
7. [Estensioni](#estensioni)

---

## Architettura Software

### Stack Tecnologico

| Componente | Tecnologia | Versione |
|------------|------------|----------|
| Orchestrator API | Python + FastAPI | 3.11+ |
| Server Tecan | C# + .NET | 8.0 |
| Server Opentrons | Python + gRPC | 3.11+ |
| Interfaccia | Node-RED | 4.x |
| Comunicazione | gRPC / SiLA2 | - |
| Dati | JSON / XML / AnIML | - |

### Flusso Dati

```
User → Node-RED → REST API → Orchestrator → gRPC → SiLA2 Servers → Instruments
                                    ↓
                              Workflow Engine
                                    ↓
                              Results Manager → File System
```

---

## Server SiLA2

### Tecan SiLA2 Server (C#)

**Porta**: 50051

#### Features implementate

| Feature | Descrizione | File |
|---------|-------------|------|
| `PlateReaderService` | Controllo lettore piastre | `Features/PlateReaderFeature.cs` |
| `MeasurementService` | Misure abs/fluo/lumi | `Features/MeasurementFeature.cs` |
| `MethodService` | Esecuzione metodi MDFX | `Features/MethodFeature.cs` |

#### HAL (Hardware Abstraction Layer)

```csharp
// Instrument/TecanController.cs
public interface ITecanController
{
    Task<bool> Connect();
    Task<MeasurementResult> RunMethod(string methodPath);
    Task<double[][]> MeasureAbsorbance(int wavelength);
    Task<double[][]> MeasureFluorescence(int excitation, int emission);
    Task<double[][]> MeasureLuminescence();
    Task EmergencyStop();
}
```

#### Configurazione

```json
// appsettings.json
{
  "SiLA2": {
    "ServerName": "Tecan M200 Pro",
    "Port": 50051
  },
  "Tecan": {
    "InstrumentPath": "C:\\Program Files\\Tecan\\iControl",
    "SimulationMode": false
  }
}
```

### Opentrons SiLA2 Server (Python)

**Porta**: 50052

#### Features implementate

| Feature | Descrizione | File |
|---------|-------------|------|
| `LiquidHandlerService` | Operazioni pipetting | `features/liquid_handler.py` |
| `ProtocolService` | Esecuzione protocolli | `features/protocol_service.py` |
| `DeckService` | Gestione deck | `features/deck_service.py` |

#### Client Robot

```python
# robot_client.py
class RobotClient:
    async def get_health(self) -> dict
    async def get_runs(self) -> list
    async def create_run(self, protocol_id: str) -> dict
    async def upload_protocol(self, content: str) -> dict
    async def execute_command(self, run_id: str, command: dict) -> dict
    async def home(self) -> dict
```

#### Protocol Generator

```python
# protocol_generator.py
class ProtocolGenerator:
    def generate_from_recipe(self, recipe: dict) -> str
    def generate_transfer(self, source, dest, volume) -> str
    def generate_serial_dilution(self, params) -> str
```

#### Tip Tracking

```python
# tip_tracker.py
class TipTracker:
    def get_next_tip(self, rack_slot: str) -> str
    def mark_tip_used(self, rack_slot: str, well: str)
    def reset_rack(self, rack_slot: str)
    def save_state()
    def load_state()
```

---

## Orchestrator

### Classi Principali

#### OrchestratorConfig

```python
class OrchestratorConfig:
    """Carica e gestisce la configurazione da YAML."""
    
    def __init__(self, config_path: str)
    
    @property
    def tecan_address(self) -> str
    @property
    def opentrons_address(self) -> str
    @property
    def dir_protocols(self) -> str
    @property
    def dir_results(self) -> str
```

#### WorkflowEngine

```python
class WorkflowEngine:
    """Gestisce il parsing e l'esecuzione dei workflow JSON."""
    
    def list_workflows(self) -> List[dict]
    def load_workflow(self, filename: str) -> dict
    def parse_workflow(self, data: dict) -> Workflow
    def resolve_variables(self, data: dict, variables: dict) -> dict
    def load_library_file(self, path: str) -> str
```

#### LabOrchestrator

```python
class LabOrchestrator:
    """Coordina l'esecuzione di workflow su più strumenti."""
    
    async def initialize(self)
    async def run_workflow_file(self, filename: str, variables: dict) -> dict
    async def emergency_stop_all(self) -> dict
    def get_status(self) -> dict
```

### Modelli Dati

```python
class InstrumentType(Enum):
    TECAN = "tecan"
    OPENTRONS = "opentrons"
    MOBILE = "mobile"
    SYSTEM = "system"

@dataclass
class WorkflowStep:
    name: str
    description: str
    instrument: InstrumentType
    command: str
    params: dict
    timeout: float = 300.0
    retry_on_fail: bool = False

@dataclass
class Workflow:
    id: str
    name: str
    description: str
    version: str
    steps: List[WorkflowStep]
    variables: dict
```

---

## API REST

### Struttura FastAPI

```python
# orchestrator_api.py

app = FastAPI(
    title="BicoccaLab Orchestrator",
    version="1.0.0"
)

# Middleware CORS per Node-RED
app.add_middleware(CORSMiddleware, allow_origins=["*"])

# Lifecycle management
@asynccontextmanager
async def lifespan(app):
    # Startup: inizializza orchestrator
    # Shutdown: cleanup
```

### Endpoints Dettagliati

#### GET /status

Restituisce lo stato di tutti gli strumenti.

**Response:**
```json
{
  "orchestrator": {
    "name": "SiLA2 Lab Orchestrator",
    "is_running": false,
    "current_workflow": null
  },
  "tecan": {
    "enabled": true,
    "connected": true,
    "status": "idle"
  },
  "opentrons": {
    "enabled": true,
    "connected": true,
    "status": "idle"
  }
}
```

#### POST /workflows/run

Esegue un workflow.

**Request:**
```json
{
  "workflow_file": "Transfer_And_Read.workflow.json",
  "variables": {
    "plate_id": "PLATE_001"
  }
}
```

**Response:**
```json
{
  "workflow": "Transfer Samples and Read",
  "status": "completed",
  "steps_executed": 3,
  "duration_seconds": 45.2,
  "results": [
    {"step": 1, "status": "success"},
    {"step": 2, "status": "success"},
    {"step": 3, "status": "success", "data_file": "Results/CSV/..."}
  ]
}
```

---

## Node-RED Integration

### Architettura Flow

```
┌──────────┐    ┌─────────────┐    ┌──────────┐    ┌───────────┐
│  Inject  │───▶│ HTTP Request│───▶│ Function │───▶│   Debug   │
│  Button  │    │  (API call) │    │ (format) │    │  (output) │
└──────────┘    └─────────────┘    └──────────┘    └───────────┘
```

### Nodi HTTP Request

Configurazione tipica:

```json
{
  "type": "http request",
  "method": "POST",
  "url": "http://localhost:8000/workflows/run",
  "ret": "obj",
  "headers": [
    {"Content-Type": "application/json"}
  ]
}
```

### Custom Function Node

```javascript
// Format Result node
const result = msg.payload;

if (result.error) {
    node.status({fill:"red", shape:"dot", text:result.error});
    node.error(result.error);
} else {
    const status = result.status || 'unknown';
    const color = status === 'completed' ? 'green' : 'red';
    node.status({fill:color, shape:"dot", text:`${result.workflow}: ${status}`});
}

return msg;
```

### Aggiungere nuovi flow

1. Modifica `nodered/flows.json`
2. Riavvia Node-RED
3. Oppure importa direttamente dall'editor

---

## Formato Workflow

### Schema JSON

Il file `Protocols/workflow.schema.json` definisce la struttura.

### Sezioni principali

#### workflow (metadata)

```json
{
  "workflow": {
    "id": "unique_identifier",
    "name": "Human Readable Name",
    "description": "What this workflow does",
    "version": "1.0.0",
    "author": "Author Name",
    "created": "2026-01-07",
    "tags": ["category1", "category2"]
  }
}
```

#### variables (parametri dinamici)

```json
{
  "variables": {
    "plate_id": {
      "type": "string",
      "default": "PLATE_001",
      "description": "Identificativo piastra",
      "required": true
    },
    "volume_ul": {
      "type": "number",
      "default": 100,
      "min": 1,
      "max": 1000
    }
  }
}
```

#### steps (sequenza operazioni)

```json
{
  "steps": [
    {
      "name": "Transfer Samples",
      "description": "Trasferisce campioni da source a dest",
      "instrument": "opentrons",
      "action": "run_recipe",
      "params": {
        "recipe": "transfer_96.json",
        "plate_id": "${plate_id}"
      },
      "timeout": 600,
      "retry_on_fail": true,
      "max_retries": 2
    }
  ]
}
```

### Azioni per strumento

#### Opentrons

| Action | Params | Descrizione |
|--------|--------|-------------|
| `run_recipe` | `recipe`, `variables` | Esegue recipe JSON |
| `pick_tip` | `rack_slot`, `well` | Preleva tip |
| `drop_tip` | - | Rilascia tip |
| `aspirate` | `labware`, `well`, `volume`, `rate` | Aspira liquido |
| `dispense` | `labware`, `well`, `volume`, `rate` | Dispensa liquido |
| `transfer` | `source`, `dest`, `volume`, `liquid_class` | Trasferimento con liquid class |
| `transfer_with_liquid_class` | `source`, `dest`, `volume`, `liquid_class` | API nativa Opentrons |
| `home` | - | Homing assi |

**Liquid Classes disponibili**: `Aqueous`, `Viscous`, `Volatile`, `HighlyViscous`, `Foaming` (definite in `Library/LiquidClasses/`)

#### Tecan

| Action | Params | Descrizione |
|--------|--------|-------------|
| `run_analysis` | `method_file`, `plate_id` | Esegue metodo MDFX |
| `measure_absorbance` | `wavelength`, `plate_id` | Misura assorbanza |
| `measure_fluorescence` | `excitation`, `emission`, `plate_id` | Misura fluorescenza |
| `measure_luminescence` | `plate_id` | Misura luminescenza |

#### System

| Action | Params | Descrizione |
|--------|--------|-------------|
| `delay` | `seconds` | Pausa |
| `log` | `message`, `level` | Log messaggio |
| `notify` | `message`, `channel` | Notifica |

---

## Estensioni

### Aggiungere un nuovo strumento

1. **Crea il server SiLA2** in `SiLA2/NuovoStrumentoServer/`

2. **Aggiungi l'InstrumentType**:
```python
# orchestrator.py
class InstrumentType(Enum):
    ...
    NUOVO = "nuovo"
```

3. **Crea il client**:
```python
class NuovoClient:
    def __init__(self, address: str)
    async def connect(self) -> bool
    async def execute(self, command: str, params: dict) -> dict
```

4. **Registra nell'Orchestrator**:
```python
class LabOrchestrator:
    def __init__(self, config):
        ...
        self.nuovo = NuovoClient(config.nuovo_address)
```

5. **Aggiungi endpoint API** se necessario

6. **Aggiorna la configurazione**:
```yaml
nuovo:
  host: "localhost"
  port: 50054
  enabled: true
```

### Aggiungere una nuova azione

1. **Definisci l'handler nell'orchestrator**:
```python
async def _execute_nuovo_action(self, action: str, params: dict):
    if action == "custom_action":
        return await self.nuovo.custom_action(**params)
```

2. **Aggiorna lo schema workflow** per documentare i nuovi params

3. **Testa con un workflow di esempio**

### Aggiungere un endpoint API

```python
# orchestrator_api.py

class CustomRequest(BaseModel):
    param1: str
    param2: int

@app.post("/custom/endpoint")
async def custom_endpoint(request: CustomRequest):
    """Descrizione endpoint."""
    result = await orchestrator.do_something(request.param1, request.param2)
    return result
```

---

## Testing

### Unit Test

```bash
cd SiLA2
python -m pytest tests/
```

### Test API

```bash
# Con httpie
http GET localhost:8000/status
http POST localhost:8000/workflows/run workflow_file=Simple_PlateRead.workflow.json

# Con curl
curl -X POST http://localhost:8000/workflows/run \
  -H "Content-Type: application/json" \
  -d '{"workflow_file": "Simple_PlateRead.workflow.json"}'
```

### Test End-to-End

1. Avvia tutti i server
2. Apri Node-RED
3. Clicca "Get Status" - verifica connessioni
4. Clicca "Run: Simple Plate Read"
5. Verifica risultati in `Results/`

---

## Logging

### Posizione log

- Orchestrator: `SiLA2/orchestrator_logs/`
- Tecan Server: `SiLA2/TecanSiLA2Server/logs/`
- Opentrons Server: `SiLA2/OpentronsSiLA2Server/logs/`

### Formato log

```
2026-01-07 10:30:45 | INFO     | LabOrchestrator | Starting workflow: Transfer_And_Read
2026-01-07 10:30:46 | INFO     | LabOrchestrator | Step 1/3: Opentrons transfer
2026-01-07 10:31:20 | INFO     | LabOrchestrator | Step 2/3: Tecan measurement
```

### Livelli di log

- `DEBUG`: Dettagli di sviluppo
- `INFO`: Operazioni normali
- `WARNING`: Situazioni anomale ma gestite
- `ERROR`: Errori che impediscono l'operazione
- `CRITICAL`: Errori gravi che richiedono intervento

---

## Performance

### Timeout raccomandati

| Operazione | Timeout (s) |
|------------|-------------|
| Connessione gRPC | 5 |
| Homing Opentrons | 60 |
| Transfer 96-well | 300 |
| Lettura Tecan | 120 |
| Workflow completo | 3600 |

### Ottimizzazioni

1. **Batch operations**: Raggruppa trasferimenti simili
2. **Parallelismo**: Esegui operazioni indipendenti in parallelo
3. **Pre-caching**: Carica metodi Tecan prima dell'uso
4. **Connection pooling**: Mantieni connessioni gRPC aperte

---

*Documentazione tecnica - BicoccaLab v1.0*
