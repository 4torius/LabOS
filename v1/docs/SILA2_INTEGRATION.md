# SiLA2 Integration Guide

## Come Rendere il Sistema SiLA2-Native

Questa guida spiega come sfruttare al massimo il protocollo SiLA2 nel sistema BicoccaLab.

---

## 1. Architettura Attuale vs SiLA2-Native

### Attuale (Ibrido)
```
lab_console.py
├── OpentronsFlex (HTTP REST diretto)    ❌ Non SiLA2
├── TecanM200Pro (gRPC con stubs)        ✓ SiLA2-like
└── MobileRobot (gRPC con stubs)         ✓ SiLA2-like
```

### Target (SiLA2-Native)
```
lab_console.py
└── SiLA2ServerRegistry
    ├── OpentronsSiLA2Server (gRPC)      ✓ SiLA2
    ├── TecanSiLA2Server (gRPC)          ✓ SiLA2
    └── MobileSiLA2Server (gRPC)         ✓ SiLA2
```

---

## 2. Componenti Creati

### 2.1 `sila_discovery.py` - Feature Discovery
Legge automaticamente le feature da:
- `.sila.xml` (Feature Definition Language)
- `.proto` (Protocol Buffers)

```python
from sila_discovery import SiLADiscovery

discovery = SiLADiscovery()
servers = discovery.discover_all()

# Ogni server ha features, commands, properties
for feature in servers["tecan"].features:
    for cmd in feature.commands:
        print(f"{cmd.identifier}: {cmd.parameters}")
```

### 2.2 `sila2_client.py` - Client Generico SiLA2
Client unificato che parla con qualsiasi server SiLA2:

```python
from sila2_client import SiLA2ServerRegistry

registry = SiLA2ServerRegistry()
await registry.discover_and_connect()

# Esegui comando su qualsiasi server
result = await registry.execute(
    server="tecan",
    feature="PlateReaderService",
    command="SetTemperature",
    params={"target_temperature": 37.0}
)
```

---

## 3. Piano di Migrazione SiLA2

### Fase 1: Usare i Server SiLA2 Esistenti ✅

I server esistono già:
- `OpentronsSiLA2Server/` (porta 50052)
- `TecanSiLA2Server/` (porta 50051)  
- `MobileSiLA2Server/` (porta 50053)

**Azione**: Modificare `lab_console.py` per usare gRPC invece di HTTP per Opentrons.

### Fase 2: Implementare SiLAService Standard

Ogni server SiLA2 dovrebbe implementare:

```protobuf
service SiLAService {
    rpc GetServerInfo(Empty) returns (ServerInfo);
    rpc GetImplementedFeatures(Empty) returns (FeatureList);
    rpc GetFeatureDefinition(FeatureId) returns (FeatureDefinition);
}
```

**Azione**: Aggiungere questi RPC ai server esistenti.

### Fase 3: Observable Commands & Properties

SiLA2 supporta:
1. **Observable Commands**: Streaming di progress durante esecuzione
2. **Observable Properties**: Sottoscrizione a cambiamenti di stato

```python
# Subscribe to temperature changes
async for observation in client.subscribe_property(
    "PlateReaderService", 
    "CurrentTemperature"
):
    print(f"Temp: {observation.value}°C")
```

### Fase 4: Error Handling SiLA2

SiLA2 definisce errori standardizzati:

```python
class SiLA2Error(Enum):
    VALIDATION_ERROR = "ValidationError"
    EXECUTION_ERROR = "ExecutionError" 
    FRAMEWORK_ERROR = "FrameworkError"
    UNDEFINED_EXECUTION_ERROR = "UndefinedExecutionError"
```

---

## 4. Dove è il DAG?

### Posizione
`SiLA2/Orchestrator/workflow_executor.py`

### Struttura

```
WorkflowStep
├── id: "prepare_samples"
├── depends_on: []                    # Nessuna dipendenza
├── instrument: "opentrons"
└── action: "run_recipe"

WorkflowStep  
├── id: "read_plate"
├── depends_on: ["prepare_samples"]   # Dipende da prepare_samples
├── instrument: "tecan"
└── action: "run_analysis"
```

### Algoritmo DAG (linee 425-475)

```python
while True:
    # Trova step con dipendenze soddisfatte
    ready_steps = [
        step for step in steps
        if step.status == PENDING
        and all(get_step(dep).status == COMPLETED 
                for dep in step.depends_on)
    ]
    
    # Esegui step pronti
    for step in ready_steps:
        await execute_step(step)
```

### Limitazione Attuale
L'esecuzione è **sequenziale**. Per step paralleli:

```python
# ATTUALE: sequenziale
for step in ready_steps:
    await execute_step(step)

# FUTURO: parallelo
await asyncio.gather(*[
    execute_step(step) for step in ready_steps
])
```

---

## 5. Esempio Workflow SiLA2-Native

```json
{
  "workflow": {
    "id": "elisa_full",
    "name": "Complete ELISA Protocol",
    "version": "2.0.0"
  },
  "variables": {
    "plate_id": "ELISA_001",
    "temp": 37.0
  },
  "steps": [
    {
      "id": "connect_tecan",
      "instrument": "tecan",
      "action": "sila2_command",
      "params": {
        "feature": "PlateReaderService",
        "command": "Connect",
        "args": {}
      },
      "depends_on": []
    },
    {
      "id": "prepare_samples",
      "instrument": "opentrons",
      "action": "sila2_command",
      "params": {
        "feature": "LiquidHandling",
        "command": "Transfer",
        "args": {
          "volume": 100,
          "source": "A1",
          "destination": "B1"
        }
      },
      "depends_on": []
    },
    {
      "id": "set_temperature",
      "instrument": "tecan",
      "action": "sila2_command",
      "params": {
        "feature": "PlateReaderService",
        "command": "SetTemperature",
        "args": {"target_temperature": "${temp}"}
      },
      "depends_on": ["connect_tecan"]
    },
    {
      "id": "load_plate",
      "instrument": "tecan",
      "action": "sila2_command",
      "params": {
        "feature": "PlateReaderService",
        "command": "PlateIn"
      },
      "depends_on": ["prepare_samples", "set_temperature"]
    },
    {
      "id": "measure",
      "instrument": "tecan",
      "action": "sila2_observable",
      "params": {
        "feature": "PlateReaderService",
        "command": "RunMeasurement",
        "args": {
          "protocol_file": "Absorbance_450nm.mdfx",
          "plate_id": "${plate_id}"
        }
      },
      "depends_on": ["load_plate"]
    }
  ]
}
```

---

## 6. Checklist Migrazione

### Server-side

- [ ] Aggiungere `SiLAService` a OpentronsSiLA2Server
- [ ] Aggiungere `SiLAService` a TecanSiLA2Server
- [ ] Aggiungere `SiLAService` a MobileSiLA2Server
- [ ] Implementare `GetImplementedFeatures()`
- [ ] Implementare error handling SiLA2 standard

### Client-side

- [ ] Integrare `sila2_client.py` in `lab_console.py`
- [ ] Sostituire `OpentronsFlex` HTTP con client gRPC
- [ ] Aggiornare `WorkflowExecutor` per usare `SiLA2ServerRegistry`
- [ ] Implementare property subscriptions per monitoring live

### Workflow

- [ ] Aggiungere action type `sila2_command`
- [ ] Aggiungere action type `sila2_observable`
- [ ] Implementare esecuzione parallela di step indipendenti
- [ ] Aggiungere retry con backoff esponenziale

---

## 6.5 mDNS Service Discovery

Il sistema supporta la scoperta automatica dei server SiLA2 tramite mDNS/DNS-SD, come definito nello standard SiLA2.

### Service Type
I server SiLA2 si registrano con il service type: `_sila2._tcp.local.`

### Implementazione Python (MobileSiLA2Server)
```python
from zeroconf import Zeroconf, ServiceInfo

service_info = ServiceInfo(
    "_sila2._tcp.local.",
    f"{server_name}._sila2._tcp.local.",
    addresses=[socket.inet_aton(ip)],
    port=port,
    properties={
        "name": server_name,
        "type": server_type,
        "vendor": "BicoccaLab",
        "version": version
    }
)
zeroconf.register_service(service_info)
```

### Implementazione C# (TecanSiLA2Server)
```csharp
using Makaretu.Dns;

var mdns = new MulticastService();
var sd = new ServiceDiscovery(mdns);

sd.Advertise(new ServiceProfile(
    instanceName: "TecanM200Pro",
    serviceName: "_sila2._tcp",
    port: 50051,
    txtRecords: new Dictionary<string, string> {
        ["name"] = "Tecan M200 Pro",
        ["type"] = "plate_reader"
    }
));
```

### Discovery Client
```python
from src.pnp_discovery import PnPDiscovery

discovery = PnPDiscovery(base_dir)
servers = await discovery.discover_all()  # Include mDNS discovery
```

---

## 7. Vantaggi della Migrazione

| Aspetto | Attuale | SiLA2-Native |
|---------|---------|--------------|
| Discovery | Hardcoded | Dinamica |
| Errori | Custom | Standardizzati |
| Streaming | No | Observable commands |
| Monitoring | Polling | Subscriptions |
| Interoperabilità | Limitata | Universale |
| Documentazione | Manuale | Auto-generata da FDL |

---

## 8. Risorse

- [SiLA2 Standard](https://sila-standard.com/)
- [SiLA2 Feature Definition Language](https://gitlab.com/SiLA2/sila_base)
- [gRPC Python](https://grpc.io/docs/languages/python/)
