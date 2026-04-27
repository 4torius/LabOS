# SiLA2 Servers - Plug & Play Architecture

This folder contains all SiLA2 server implementations for the lab automation system.

## Folder Structure

```
SiLA2/
├── SiLA2Common.proto           # Shared Plug & Play interface definition
├── SiLA2Common_pb2.py          # Generated Python stubs
├── SiLA2Common_pb2_grpc.py     # Generated gRPC stubs
├── sila2_common_servicer.py    # Reusable servicer helper class
├── sila2_mdns_registry.py      # mDNS discovery utilities
├── 000_EMERGENCY_RESET.json    # Emergency configuration
│
├── ManualStationSiLA2Server/   # Manual operator station
├── MobileSiLA2Server/          # GoFaGo mobile robot (ROS bridge)
├── OpentronsSiLA2Server/       # Opentrons Flex liquid handler
├── TecanSiLA2Server/           # Tecan M200 Pro plate reader (C#)
├── Orchestrator/               # Central workflow orchestrator
│
└── _NewInstrumentTemplate/     # Template for new servers
```

## Plug & Play Architecture

Every server implements **SiLA2Common** interface that enables:
- **GetServerInfo()** - Server metadata and capabilities
- **GetFeatures()** - Available commands and parameters
- **GetStatus()** - Real-time hardware status
- **ExecuteCommand()** - Generic command execution
- **GetProperty()** - Property value retrieval

This allows the Orchestrator to discover and control any server without prior knowledge.

## Adding a New Instrument

1. Copy `_NewInstrumentTemplate/` folder
2. Implement your hardware-specific servicer
3. Register SiLA2Common adapter in main.py
4. The system will auto-discover the new server

See `docs/ADDING_NEW_INSTRUMENT.md` for detailed guide.

## Server Ports (Default)

| Server         | Port  | Protocol |
|----------------|-------|----------|
| Tecan M200     | 50051 | gRPC     |
| Opentrons Flex | 50052 | gRPC     |
| Mobile Robot   | 50053 | gRPC     |
| Manual Station | 50360 | gRPC     |
| Orchestrator   | 50100 | gRPC     |

## For Ubuntu Deployment

Copy these files to the parent of your server folder:
```bash
scp SiLA2Common.proto SiLA2Common_pb2.py SiLA2Common_pb2_grpc.py sila2_common_servicer.py user@host:~/SiLA2/
```

Expected structure on Ubuntu:
```
~/SiLA2/
├── SiLA2Common_pb2.py
├── SiLA2Common_pb2_grpc.py
├── sila2_common_servicer.py
└── MobileSiLA2Server/
    ├── main.py
    └── ...
```
