# BicoccaLab v7 - Laboratory Automation System

**Plug & Play** laboratory automation system based on SiLA2 standard.

## Instruments (Auto-Discovered)
- **Opentrons Flex** - Robotic liquid handler
- **Tecan Infinite M200Pro** - Plate reader
- **GoFaGo** - Mobile robot (RB Kairos + ABB GoFa 15000)
- **Manual Station** - Operator task management
- **+ Any new instrument** - Just add a server folder!

---

## Quick Start

### Easiest Way (Windows)
```
Double-click START.bat
```

### From Terminal
```bash
python launcher.py              # Interactive menu with all options
python launcher.py --all        # Start everything (servers + webapp)
python launcher.py --status     # Check system status
python launcher.py --cli        # Start PnP console
```

---

## Project Structure

```
v7/
├── launcher.py                # MAIN ENTRY POINT - Start here!
├── START.bat                  # Windows quick start (double-click)
├── pnp_console.py             # Plug & Play CLI console
│
├── lab_config.yaml            # Centralized configuration
│
├── webapp/                    # Web Dashboard
│   ├── app.py                 # FastAPI application
│   └── ...
│
├── src/                       # Core Python modules (PnP)
│   ├── pnp_discovery.py       # Auto-discovery (NO hardcoded lists!)
│   ├── pnp_client.py          # Generic command executor
│   ├── pnp_workflow_executor.py # Workflow execution
│   └── grpc/                  # Generated protobuf stubs
│
├── SiLA2/                     # SiLA2 Servers
│   ├── _NewInstrumentTemplate/ # TEMPLATE for new instruments
│   ├── TecanSiLA2Server/
│   ├── OpentronsSiLA2Server/
│   ├── MobileSiLA2Server/
│   └── ManualStationSiLA2Server/
│
├── Library/                   # Shared resources
│   ├── Workflows/             # Workflow definitions
│   ├── Recipes/               # Opentrons recipes
│   └── HardwareConfig/        # HAL configurations
│
└── Results/                   # Output data
```

---

## Adding a New Instrument (3 Steps)

The system is **truly plug & play**. No code changes needed!

### 1. Copy the Template
```bash
cp -r SiLA2/_NewInstrumentTemplate SiLA2/YourInstrumentSiLA2Server
```

### 2. Define Commands
Edit `features/YourInstrument.sila.xml` to define your commands.

### 3. Implement the Servicer
Edit `src/servicer.py` with your hardware communication.

**That's it!** The system auto-discovers:
- Server folder in `SiLA2/`
- Feature definitions from `.sila.xml`
- Port from `config.yaml`
- Commands appear in console and webapp automatically

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      USER INTERFACES                            │
├──────────────────────┬──────────────────────────────────────────┤
│   pnp_console.py     │              webapp/app.py               │
│   (CLI Console)      │           (Web Dashboard)                │
└──────────┬───────────┴──────────────────┬───────────────────────┘
           │                              │
           ▼                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      PnP DISCOVERY + CLIENT                     │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  │
│  │ pnp_discovery   │  │   pnp_client    │  │ pnp_workflow    │  │
│  │ (auto-discover) │  │ (send commands) │  │   _executor     │  │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘  │
└──────────────────────────────┬──────────────────────────────────┘
                               │ gRPC
           ┌───────────────────┼───────────────────┐
           ▼                   ▼                   ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│  TecanSiLA2      │ │ OpentronsSiLA2   │ │  MobileSiLA2     │
│  Server :50051   │ │ Server :50052    │ │  Server :50053   │
└──────────────────┘ └──────────────────┘ └──────────────────┘
```

---

## SiLA2 Servers

Each server folder in `SiLA2/` is auto-discovered. Servers currently available:

| Server | Port | Description |
|--------|------|-------------|
| TecanSiLA2Server | 50051 | Tecan Infinite M200 Pro plate reader |
| OpentronsSiLA2Server | 50052 | Opentrons Flex liquid handler |
| MobileSiLA2Server | 50053 | Mobile robot (ROS1 integration) |
| ManualStationSiLA2Server | 50360 | Operator task management |

### Starting a Server

```bash
cd SiLA2/OpentronsSiLA2Server
python main.py
```

---

## Workflows

Workflows are JSON files in `Library/Workflows/` that orchestrate multiple instruments:

```json
{
  "name": "Simple Transfer",
  "steps": [
    {
      "id": "transfer",
      "instrument": "opentrons",
      "command": "Transfer",
      "params": {
        "volume": 100,
        "source": "A1",
        "destination": "B1"
      }
    },
    {
      "id": "read",
      "instrument": "tecan",
      "command": "ReadAbsorbance",
      "depends_on": ["transfer"]
    }
  ]
}
```

Run workflows via console:
```bash
python pnp_console.py
# Select "Run Workflow" from menu
```

---

## Requirements

```bash
pip install grpcio grpcio-tools httpx pyyaml fastapi uvicorn jinja2
```

---

## Documentation

See `docs/` folder for detailed documentation:
- `PLUG_AND_PLAY_ARCHITECTURE.md` - PnP system design
- `OPENTRONS_SERVER.md` - Opentrons integration
- `TECAN_SERVER.md` - Tecan integration

---

**BicoccaLab v7** - Università degli Studi di Milano-Bicocca

