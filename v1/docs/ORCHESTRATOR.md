# 🎯 Orchestrator - Documentazione Dettagliata

> Sistema di orchestrazione multi-dispositivo per workflow di laboratorio

## 📋 Informazioni Generali

| Proprietà | Valore |
|-----------|--------|
| **Linguaggio** | Python 3.10+ |
| **Interfaccia** | CLI (Command Line Interface) |
| **Protocollo** | SiLA2 (gRPC) per comunicazione dispositivi |
| **Formato Workflow** | JSON |

---

## 🏗️ Architettura del Sistema

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              ORCHESTRATOR                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────┐                                                           │
│  │ __main__.py  │  Entry Point & CLI                                        │
│  │              │  - Argparse commands                                       │
│  │  Commands:   │  - Interactive shell                                       │
│  │  - status    │  - Workflow execution                                      │
│  │  - devices   │                                                            │
│  │  - run       │                                                            │
│  │  - execute   │                                                            │
│  └──────────────┘                                                           │
│         │                                                                    │
│         ▼                                                                    │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │                        LabOrchestrator                                   ││
│  │                                                                          ││
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────────┐  ││
│  │  │  DeviceManager  │  │WorkflowExecutor │  │      SiLAGateway        │  ││
│  │  │                 │  │                 │  │                         │  ││
│  │  │ - Device pool   │  │ - Step executor │  │ - Auto discovery        │  ││
│  │  │ - Adapters      │  │ - Dependencies  │  │ - Feature extraction    │  ││
│  │  │ - Operations    │  │ - Variables     │  │ - Dynamic mapping       │  ││
│  │  │ - Health check  │  │ - Error handler │  │                         │  ││
│  │  └─────────────────┘  └─────────────────┘  └─────────────────────────┘  ││
│  │         │                    │                       │                   ││
│  └─────────┼────────────────────┼───────────────────────┼───────────────────┘│
│            │                    │                       │                    │
│            └────────────────────┼───────────────────────┘                    │
│                                 │                                            │
│                    ┌────────────┴────────────┐                               │
│                    ▼            ▼            ▼                               │
│            ┌───────────┐ ┌───────────┐ ┌───────────┐                        │
│            │  Tecan    │ │ Opentrons │ │  Mobile   │                        │
│            │  Adapter  │ │  Adapter  │ │  Adapter  │                        │
│            └───────────┘ └───────────┘ └───────────┘                        │
│                 │              │              │                              │
└─────────────────│──────────────│──────────────│──────────────────────────────┘
                  │              │              │
                  ▼              ▼              ▼
           ┌───────────┐  ┌───────────┐  ┌───────────┐
           │  Tecan    │  │ Opentrons │  │  Mobile   │
           │  Server   │  │  Server   │  │  Server   │
           │  :50051   │  │  :50052   │  │  :50053   │
           └───────────┘  └───────────┘  └───────────┘
```

---

## 📁 Struttura File

```
Orchestrator/
├── __init__.py
├── __main__.py           # Entry point e CLI
├── config.py             # Configurazione
├── device_manager.py     # Gestione dispositivi
├── workflow_executor.py  # Esecutore workflow
├── gateway.py            # SiLA2 Gateway discovery
├── mqtt_bridge.py        # Bridge MQTT (opzionale)
└── adapters/
    ├── __init__.py
    ├── base.py           # BaseDeviceAdapter
    ├── tecan.py          # TecanAdapter
    ├── opentrons.py      # OpentronsAdapter
    └── mobile.py         # MobileAdapter
```

---

## 🖥️ CLI Interface

### Comandi Disponibili

```bash
# Avvio orchestrator
python -m Orchestrator

# Comandi specifici
python -m Orchestrator status           # Stato sistema
python -m Orchestrator devices          # Lista dispositivi
python -m Orchestrator capabilities     # Capacità dispositivi
python -m Orchestrator run <workflow>   # Esegui workflow
python -m Orchestrator execute <recipe> # Esegui ricetta singola
```

### Comando: status

Mostra lo stato complessivo del sistema.

```bash
$ python -m Orchestrator status

╔═══════════════════════════════════════════════════════════╗
║                 LAB ORCHESTRATOR STATUS                    ║
╠═══════════════════════════════════════════════════════════╣
║  Orchestrator: ONLINE                                      ║
║  Devices:      3 connected                                 ║
║  Workflows:    5 available                                 ║
║  Queue:        0 pending                                   ║
╠═══════════════════════════════════════════════════════════╣
║  Tecan M200 Pro      │ CONNECTED │ Idle                   ║
║  Opentrons Flex      │ CONNECTED │ Idle                   ║
║  GoFaGo Mobile       │ CONNECTED │ Idle                   ║
╚═══════════════════════════════════════════════════════════╝
```

### Comando: devices

Lista dettagliata dei dispositivi.

```bash
$ python -m Orchestrator devices

┌─────────────────────────────────────────────────────────────┐
│                    REGISTERED DEVICES                        │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  [1] Tecan M200 Pro                                          │
│      Type: PlateReader                                       │
│      Address: localhost:50051                                │
│      Status: Connected                                       │
│      Features: PlateReaderService                            │
│                                                              │
│  [2] Opentrons Flex                                          │
│      Type: LiquidHandler                                     │
│      Address: localhost:50052                                │
│      Status: Connected                                       │
│      Features: OpentronsFlex, LiquidHandling, ModuleControl  │
│                                                              │
│  [3] GoFaGo Mobile Robot                                     │
│      Type: MobileRobot                                       │
│      Address: localhost:50053                                │
│      Status: Connected (Simulation)                          │
│      Features: MobileRobot, Logistics                        │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Comando: run

Esegue un workflow completo.

```bash
$ python -m Orchestrator run Library/Workflows/ELISA_Complete.workflow.json

╔═══════════════════════════════════════════════════════════════╗
║               WORKFLOW: ELISA_Complete                         ║
║               Steps: 6                                         ║
╠═══════════════════════════════════════════════════════════════╣
║                                                                ║
║  [1/6] Opentrons: ExecuteRecipe                    ▓▓▓░░ 33%  ║
║        Preparazione campioni su piastra                        ║
║                                                                ║
╚═══════════════════════════════════════════════════════════════╝
```

---

## 📦 Componenti Principali

### 1. LabOrchestrator (__main__.py)

Classe principale che coordina tutto il sistema.

```python
class LabOrchestrator:
    """Orchestratore principale del laboratorio."""
    
    def __init__(self, config: OrchestratorConfig):
        self.config = config
        self.device_manager = DeviceManager(config)
        self.workflow_executor = WorkflowExecutor(self.device_manager, config)
        self.gateway = SiLAGateway(config)
    
    async def initialize(self):
        """Inizializza tutti i componenti."""
        await self.gateway.discover_devices()
        await self.device_manager.connect_all()
    
    async def run_workflow(self, workflow_path: str, variables: dict = None):
        """Esegue un workflow completo."""
        run = self.workflow_executor.load_workflow(workflow_path, variables)
        return await self.workflow_executor.execute(run)
    
    async def execute_command(self, device: str, action: str, params: dict):
        """Esegue comando singolo su dispositivo."""
        return await self.device_manager.execute(device, action, params)
```

### 2. DeviceManager (device_manager.py)

Gestisce il pool di dispositivi e le operazioni.

```python
class DeviceManager:
    """Gestisce dispositivi e operazioni."""
    
    def __init__(self, config: OrchestratorConfig):
        self.config = config
        self._devices: Dict[str, DiscoveredDevice] = {}
        self._adapters: Dict[str, BaseDeviceAdapter] = {}
    
    def register_device(self, device: DiscoveredDevice):
        """Registra nuovo dispositivo."""
    
    def get_adapter(self, device_type: str) -> BaseDeviceAdapter:
        """Ottieni adapter per tipo dispositivo."""
    
    async def connect_all(self) -> Dict[str, bool]:
        """Connetti a tutti i dispositivi registrati."""
    
    async def execute(
        self, 
        device_name: str, 
        action: str, 
        params: Dict
    ) -> OperationResult:
        """Esegue operazione su dispositivo."""
    
    async def health_check(self) -> Dict[str, HealthStatus]:
        """Verifica salute di tutti i dispositivi."""
```

### 3. WorkflowExecutor (workflow_executor.py)

Esegue workflow multi-step con gestione dipendenze.

```python
class WorkflowExecutor:
    """Esecutore workflow multi-dispositivo."""
    
    def load_workflow(self, filepath: str, variables: dict = None) -> WorkflowRun:
        """Carica workflow da file JSON."""
    
    def create_workflow(
        self, 
        workflow_id: str, 
        name: str, 
        steps: List[Dict]
    ) -> WorkflowRun:
        """Crea workflow programmaticamente."""
    
    async def execute(
        self, 
        run: WorkflowRun, 
        on_progress: Callable = None
    ) -> WorkflowRun:
        """Esegue workflow con progress callback."""
    
    async def pause(self, run_id: str):
        """Metti in pausa workflow."""
    
    async def resume(self, run_id: str):
        """Riprendi workflow in pausa."""
    
    async def cancel(self, run_id: str):
        """Annulla workflow."""
```

### 4. SiLAGateway (gateway.py)

Discovery automatico dispositivi SiLA2.

```python
class SiLAGateway:
    """Gateway per discovery SiLA2."""
    
    async def discover_devices(self, timeout: float = 5.0) -> List[DiscoveredDevice]:
        """Scopri dispositivi SiLA2 sulla rete."""
    
    async def connect_device(self, device: DiscoveredDevice) -> bool:
        """Connetti a dispositivo scoperto."""
    
    async def extract_features(self, device: DiscoveredDevice) -> List[SiLAFeature]:
        """Estrai feature SiLA2 da dispositivo."""
    
    def get_device_capabilities(self, device: DiscoveredDevice) -> Dict:
        """Ottieni capacità dispositivo."""
```

---

## 📝 Formato Workflow JSON

### Struttura Base

```json
{
  "workflow": {
    "id": "unique_workflow_id",
    "name": "Workflow Name",
    "version": "1.0",
    "author": "Author Name",
    "description": "Workflow description"
  },
  
  "variables": {
    "plate_id": "PLATE_001",
    "sample_volume": 100,
    "temperature": 37.0
  },
  
  "steps": [
    {
      "id": "step_1",
      "name": "Step Name",
      "instrument": "opentrons",
      "action": "run_recipe",
      "params": {
        "recipe": "path/to/recipe.json"
      },
      "depends_on": [],
      "on_error": "abort",
      "max_retries": 1,
      "timeout_seconds": 300
    }
  ],
  
  "on_complete": {
    "notify": true,
    "archive_results": true
  },
  
  "on_error": {
    "notify": true,
    "cleanup": true
  }
}
```

### Tipi di Step

#### Opentrons Step
```json
{
  "id": "liquid_handling",
  "instrument": "opentrons",
  "action": "run_recipe",
  "params": {
    "recipe": "Library/Recipes/sample_prep.json",
    "use_hal": true,
    "hal_config": "Standard_Flex_Setup"
  }
}
```

#### Tecan Step
```json
{
  "id": "plate_read",
  "instrument": "tecan",
  "action": "run_measurement",
  "params": {
    "protocol_file": "TestAbs.mdfx",
    "plate_id": "${plate_id}",
    "wait_for_temp": true
  }
}
```

#### Mobile Step
```json
{
  "id": "transport",
  "instrument": "mobile",
  "action": "transport_labware",
  "params": {
    "source": "opentrons",
    "destination": "tecan",
    "labware_type": "96_well_plate",
    "labware_id": "${plate_id}"
  }
}
```

#### System Step
```json
{
  "id": "wait",
  "instrument": "system",
  "action": "delay",
  "params": {
    "seconds": 60,
    "message": "Waiting for incubation"
  }
}
```

### Dipendenze tra Step

```json
{
  "steps": [
    {
      "id": "prepare",
      "instrument": "opentrons",
      "action": "run_recipe",
      "depends_on": []
    },
    {
      "id": "transport_1",
      "instrument": "mobile",
      "action": "transport_labware",
      "depends_on": ["prepare"]
    },
    {
      "id": "read",
      "instrument": "tecan",
      "action": "run_measurement",
      "depends_on": ["transport_1"]
    },
    {
      "id": "transport_2",
      "instrument": "mobile",
      "action": "transport_labware",
      "depends_on": ["read"]
    }
  ]
}
```

### Variabili e Sostituzione

Le variabili definite in `variables` possono essere usate negli step con sintassi `${variable_name}`:

```json
{
  "variables": {
    "plate_id": "PLATE_001",
    "volume": 100
  },
  "steps": [
    {
      "params": {
        "labware_id": "${plate_id}",
        "aspirate_volume": "${volume}"
      }
    }
  ]
}
```

---

## 🔄 Gestione Errori

### Strategie di Error Handling

| Strategia | Descrizione |
|-----------|-------------|
| `abort` | Ferma workflow immediatamente |
| `retry` | Riprova step (max_retries volte) |
| `skip` | Salta step e continua |
| `continue` | Marca come fallito ma continua |

### Esempio con Retry

```json
{
  "id": "fragile_step",
  "instrument": "tecan",
  "action": "run_measurement",
  "on_error": "retry",
  "max_retries": 3,
  "timeout_seconds": 600
}
```

---

## 📊 Stati Workflow e Step

### Workflow Status

```python
class WorkflowStatus(Enum):
    PENDING = "pending"       # In attesa di esecuzione
    RUNNING = "running"       # In esecuzione
    PAUSED = "paused"         # In pausa
    COMPLETED = "completed"   # Completato con successo
    FAILED = "failed"         # Fallito
    CANCELLED = "cancelled"   # Annullato
```

### Step Status

```python
class StepStatus(Enum):
    PENDING = "pending"       # In attesa
    WAITING = "waiting"       # Attende dipendenze
    RUNNING = "running"       # In esecuzione
    COMPLETED = "completed"   # Completato
    FAILED = "failed"         # Fallito
    SKIPPED = "skipped"       # Saltato
    CANCELLED = "cancelled"   # Annullato
```

---

## 📡 Integrazione MQTT (Opzionale)

Il sistema supporta MQTT per notifiche e monitoraggio remoto.

### Configurazione MQTT

```yaml
mqtt:
  enabled: true
  broker: "mqtt.example.com"
  port: 1883
  username: "lab_user"
  password: "secret"
  topics:
    status: "lab/orchestrator/status"
    events: "lab/orchestrator/events"
    commands: "lab/orchestrator/commands"
```

### Topic MQTT

| Topic | Direzione | Contenuto |
|-------|-----------|-----------|
| `lab/orchestrator/status` | Publish | Stato sistema |
| `lab/orchestrator/events` | Publish | Eventi workflow |
| `lab/devices/{name}/status` | Publish | Stato dispositivo |
| `lab/orchestrator/commands` | Subscribe | Comandi remoti |

---

## 🔧 Configurazione

### config.yaml

```yaml
orchestrator:
  name: "BicoccaLab Orchestrator"
  version: "2.0.0"
  log_level: "INFO"

devices:
  tecan:
    host: "localhost"
    port: 50051
    type: "platereader"
    auto_connect: true
    
  opentrons:
    host: "localhost"
    port: 50052
    type: "liquidhandler"
    auto_connect: true
    
  mobile:
    host: "localhost"
    port: 50053
    type: "mobile"
    auto_connect: true
    simulate: true

directories:
  library: "../../Library"
  workflows: "../../Library/Workflows"
  recipes: "../../Library/Recipes"
  results: "../../Results"
  queue: "../../Queue"

execution:
  default_timeout: 300
  max_parallel_steps: 2
  retry_delay: 5
```

---

## 🚀 Avvio Orchestrator

### Avvio Standard
```bash
cd SiLA2
python -m Orchestrator
```

### Con Config Custom
```bash
python -m Orchestrator --config custom_config.yaml
```

### Modalità Interattiva
```bash
python -m Orchestrator shell
```

### Esecuzione Diretta Workflow
```bash
python -m Orchestrator run Library/Workflows/ELISA_Complete.workflow.json
```

---

## 🧪 Esempio Workflow Completo

### ELISA_Complete.workflow.json

```json
{
  "workflow": {
    "id": "elisa_complete",
    "name": "ELISA Complete Workflow",
    "version": "1.0",
    "author": "BicoccaLab",
    "description": "Complete ELISA: sample prep on Opentrons + absorbance read on Tecan"
  },
  
  "variables": {
    "plate_id": "ELISA_001",
    "sample_volume": 100,
    "read_wavelength": 450
  },
  
  "steps": [
    {
      "id": "prepare_samples",
      "name": "Prepare Samples",
      "instrument": "opentrons",
      "action": "run_recipe",
      "params": {
        "recipe": "Library/Recipes/elisa_sample_prep.json",
        "use_hal": true,
        "hal_config": "ELISA_Setup"
      },
      "timeout_seconds": 1800
    },
    {
      "id": "transport_to_reader",
      "name": "Transport Plate to Reader",
      "instrument": "mobile",
      "action": "transport_labware",
      "params": {
        "source": "opentrons",
        "destination": "tecan",
        "labware_type": "96_well_plate",
        "labware_id": "${plate_id}"
      },
      "depends_on": ["prepare_samples"],
      "timeout_seconds": 120
    },
    {
      "id": "connect_reader",
      "name": "Connect Plate Reader",
      "instrument": "tecan",
      "action": "connect",
      "params": {
        "connection_string": "usb"
      },
      "depends_on": ["transport_to_reader"]
    },
    {
      "id": "load_plate",
      "name": "Load Plate into Reader",
      "instrument": "tecan",
      "action": "plate_in",
      "depends_on": ["connect_reader"]
    },
    {
      "id": "set_temperature",
      "name": "Set Reading Temperature",
      "instrument": "tecan",
      "action": "set_temperature",
      "params": {
        "temperature": 25.0
      },
      "depends_on": ["load_plate"]
    },
    {
      "id": "read_absorbance",
      "name": "Read Absorbance",
      "instrument": "tecan",
      "action": "run_measurement",
      "params": {
        "protocol_file": "ELISA_Abs450.mdfx",
        "plate_id": "${plate_id}",
        "output_format": "csv"
      },
      "depends_on": ["set_temperature"],
      "timeout_seconds": 300
    },
    {
      "id": "eject_plate",
      "name": "Eject Plate",
      "instrument": "tecan",
      "action": "plate_out",
      "depends_on": ["read_absorbance"]
    },
    {
      "id": "transport_to_storage",
      "name": "Transport Plate to Storage",
      "instrument": "mobile",
      "action": "transport_labware",
      "params": {
        "source": "tecan",
        "destination": "storage",
        "labware_type": "96_well_plate",
        "labware_id": "${plate_id}"
      },
      "depends_on": ["eject_plate"]
    }
  ],
  
  "on_complete": {
    "notify": true,
    "archive_results": true,
    "message": "ELISA workflow completed successfully"
  },
  
  "on_error": {
    "notify": true,
    "cleanup": true,
    "safe_state": true
  }
}
```

---

## 📚 Riferimenti

- [SiLA2 Standard](https://sila-standard.com/)
- [gRPC Python](https://grpc.io/docs/languages/python/)
- [AsyncIO Documentation](https://docs.python.org/3/library/asyncio.html)
- [MQTT Protocol](https://mqtt.org/)

---

*Documentazione Orchestrator - BicoccaLab v6*
