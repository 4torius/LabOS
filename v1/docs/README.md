# 📚 Documentazione Sistema BicoccaLab v6

> Sistema di automazione di laboratorio integrato basato su SiLA2

## 📁 Indice Documentazione

### Documentazione dei Server

| Server | Descrizione | Porta | Documentazione |
|--------|-------------|-------|----------------|
| **TecanSiLA2Server** | Lettore di micropiastre Tecan M200 Pro | 50051 | [TECAN_SERVER.md](./TECAN_SERVER.md) |
| **OpentronsSiLA2Server** | Robot per liquid handling Opentrons Flex | 50052 | [OPENTRONS_SERVER.md](./OPENTRONS_SERVER.md) |
| **MobileSiLA2Server** | Robot mobile GoFaGo (RB Kairos + ABB GoFa) | 50053 | [MOBILE_SERVER.md](./MOBILE_SERVER.md) |
| **Orchestrator** | Orchestrazione multi-dispositivo | - | [ORCHESTRATOR.md](./ORCHESTRATOR.md) |

### Piani e Roadmap

| Documento | Descrizione |
|-----------|-------------|
| [NODERED_MIGRATION.md](./NODERED_MIGRATION.md) | Piano migrazione da CLI a Node-RED |
| [ARCHITECTURE_OVERVIEW.md](./ARCHITECTURE_OVERVIEW.md) | Panoramica architetturale completa |

---

## 🏗️ Architettura del Sistema

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           ORCHESTRATOR                                   │
│                     (Gateway + Workflow Executor)                        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐    │
│  │   CLI       │  │   Gateway   │  │  Workflow   │  │   Device    │    │
│  │  Interface  │  │  (Discovery)│  │  Executor   │  │  Manager    │    │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
                    ▼               ▼               ▼
        ┌───────────────┐  ┌───────────────┐  ┌───────────────┐
        │    TECAN      │  │   OPENTRONS   │  │    MOBILE     │
        │  SiLA2 Server │  │  SiLA2 Server │  │  SiLA2 Server │
        │   (C#/.NET)   │  │   (Python)    │  │   (Python)    │
        │  Port: 50051  │  │  Port: 50052  │  │  Port: 50053  │
        └───────────────┘  └───────────────┘  └───────────────┘
                │                   │                   │
                ▼                   ▼                   ▼
        ┌───────────────┐  ┌───────────────┐  ┌───────────────┐
        │  Tecan SDK    │  │ Opentrons API │  │   ROS1 Bridge │
        │  (iControl)   │  │   HTTP REST   │  │    (rospy)    │
        └───────────────┘  └───────────────┘  └───────────────┘
                │                   │                   │
                ▼                   ▼                   ▼
        ┌───────────────┐  ┌───────────────┐  ┌───────────────┐
        │ Tecan M200 Pro│  │ Opentrons Flex│  │    GoFaGo     │
        │ Plate Reader  │  │ Liquid Handler│  │ Mobile Robot  │
        └───────────────┘  └───────────────┘  └───────────────┘
```

## 🔌 Protocollo SiLA2

Tutti i server implementano il protocollo **SiLA2** (Standardization in Lab Automation):

- **Trasporto**: gRPC (HTTP/2)
- **Serializzazione**: Protocol Buffers
- **Features**: Comandi, Proprietà, Osservabili
- **Discovery**: Automatico tramite Gateway

## 📦 Componenti Principali

### 1. TecanSiLA2Server (C#/.NET)
Server per il lettore di micropiastre Tecan M200 Pro.
- Letture di assorbanza, fluorescenza, luminescenza
- Controllo temperatura
- Output in XML, CSV, AnIML

### 2. OpentronsSiLA2Server (Python)
Server per il robot Opentrons Flex con HAL (Hardware Abstraction Layer).
- 60+ comandi per liquid handling
- Supporto tutti i moduli (HeaterShaker, Thermocycler, etc.)
- Generatore di protocolli da JSON

### 3. MobileSiLA2Server (Python)
Server per il robot mobile GoFaGo.
- Navigazione autonoma
- Controllo braccio ABB GoFa
- Trasporto labware tra stazioni

### 4. Orchestrator
Coordina workflow multi-dispositivo.
- Gateway per discovery SiLA2
- Esecuzione workflow JSON
- Gestione dipendenze e errori

## 🚀 Quick Start

```bash
# Avvia tutti i server
.\Start-Lab.ps1

# Oppure singolarmente:
python start_tecan.py      # Tecan M200 Pro
python start_opentrons.py  # Opentrons Flex
python start_mobile.py     # GoFaGo Mobile
python start_orchestrator.py  # Orchestrator
```

## 📖 Documentazione Correlata

- [ARCHITECTURE.md](../ARCHITECTURE.md) - Architettura generale
- [PROTOCOLS.md](../PROTOCOLS.md) - Formati protocolli
- [QUICKSTART.md](../QUICKSTART.md) - Guida rapida
- [GUIDA_RICETTE_OPENTRONS_HAL.md](../GUIDA_RICETTE_OPENTRONS_HAL.md) - Ricette Opentrons

---

*Documentazione generata automaticamente - BicoccaLab v6*
