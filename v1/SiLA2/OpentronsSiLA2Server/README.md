# OpentronsSiLA2Server

**SiLA2 Server for Opentrons Flex Robot Control**

A robust Python implementation of a SiLA2 server for controlling Opentrons Flex liquid handling robots.

## 📁 Project Structure

```
OpentronsSiLA2Server/
├── src/                      # Core modules
│   ├── __init__.py           # Package exports
│   ├── config.py             # Configuration management
│   ├── server.py             # Main SiLA2 server
│   ├── robot_client.py       # HTTP client for Opentrons API
│   ├── protocol_generator.py # JSON to Python protocol converter
│   ├── hardware_manager.py   # Hardware Abstraction Layer (HAL)
│   └── tip_tracker.py        # Tip consumption tracking
├── tests/                    # Test suite
│   ├── test_connection.py    # Robot connectivity tests
│   └── test_server.py        # Server integration tests
├── features/                 # SiLA2 feature definitions (XML)
├── input/                    # Protocol input queue
├── output/                   # Protocol outputs
├── logs/                     # Server logs
├── main.py                   # Entry point
├── config.yaml               # Configuration
└── requirements.txt          # Python dependencies
```

## 🚀 Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

Edit `config.yaml`:

```yaml
server:
  host: "0.0.0.0"
  port: 50052

robot:
  ip: "169.254.161.83"    # Your robot's IP
  port: 31950
  local_address: "169.254.161.1"  # For USB link-local
```

### 3. Test Connection

```bash
python main.py --test
```

### 4. Start Server

```bash
python main.py
```

## ✨ Features

### SiLA2 Features Implemented

| Feature | Description |
|---------|-------------|
| **OpentronsFlex** | Robot initialization, home, emergency stop, protocol execution |
| **LiquidHandling** | Transfer, distribute, consolidate, mix operations |
| **ModuleControl** | Heater-Shaker control, gripper operations |

### Core Capabilities

- **Hardware Abstraction Layer (HAL)**: Map logical requirements to physical hardware
- **JSON Recipe System**: Define protocols in JSON, converted to Python at runtime
- **Tip Tracking**: Persistent tracking with crash recovery
- **Multi-Config Support**: Switch between deck configurations at runtime
- **Image Extraction**: Extract images from run logs (TakeSnapshot command)

## 📝 JSON Recipe Example

```json
{
  "ProtocolName": "Sample Transfer",
  "Requirements": {
    "SourcePlate": "Plate96_Slot_C2",
    "DestPlate": "Plate96_Slot_D2"
  },
  "Steps": [
    {"Command": "PickUpTip", "PipetteMount": "left"},
    {
      "Command": "Transfer",
      "Volume": 100,
      "Source": "SourcePlate:A1",
      "Dest": "DestPlate:A1",
      "PipetteMount": "left",
      "NewTip": "never"
    },
    {"Command": "DropTip", "PipetteMount": "left"}
  ]
}
```

## 🔧 API Reference

### ServerConfig

```python
from src import ServerConfig

config = ServerConfig("config.yaml")
print(config.robot_ip)  # "169.254.161.83"
```

### OpentronsSiLA2Server

```python
from src import OpentronsSiLA2Server, ServerConfig

config = ServerConfig("config.yaml")
server = OpentronsSiLA2Server(config)

await server.initialize()
await server.cmd_home()
result = await server.cmd_run_protocol(json_content, "json")
```

### RobotClient

```python
from src import RobotClient

async with RobotClient(host="169.254.161.83") as robot:
    health = await robot.get_health()
    print(health["name"])
```

## 🧪 Testing

```bash
# Connection test
python main.py --test

# Server tests
python -m pytest tests/

# Interactive test
python tests/test_server.py
```

## 📋 Commands Reference

| Command | Description |
|---------|-------------|
| `Transfer` | Move liquid from source to destination |
| `Distribute` | One source to multiple destinations |
| `Consolidate` | Multiple sources to one destination |
| `PickUpTip` | Pick up a tip |
| `DropTip` | Drop current tip |
| `MoveLabware` | Move labware with gripper |
| `HeaterShaker` | Control Heater-Shaker module |
| `Home` | Home all axes |
| `Delay` | Wait for seconds |
| `Comment` | Add comment to log |
| `TakeSnapshot` | Capture camera image |

## 🔒 Safety Features

- **Emergency Stop**: Abort, drop tip, home sequence
- **Auto Home on Error**: Configurable automatic homing after failures
- **Tip Tracking Recovery**: Analyze partial runs for actual tip usage
- **Zombie Run Cleanup**: Automatic handling of orphaned runs

## 📄 License

MIT License

## 👥 Authors

ChemicalLab - Bicocca University
