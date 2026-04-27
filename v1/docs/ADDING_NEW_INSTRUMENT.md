# Adding a New Instrument to BicoccaLab

This guide shows how to add a new scientific instrument to BicoccaLab v7.

## Quick Summary (3 Steps)

The system is **truly plug & play**:

1. **Copy the template**: `cp -r SiLA2/_NewInstrumentTemplate SiLA2/YourInstrumentSiLA2Server`
2. **Define commands**: Edit `features/YourInstrument.sila.xml`
3. **Implement servicer**: Edit `src/servicer.py`

**Done!** The system auto-discovers your server.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│         pnp_console.py / webapp                 │  <- User interfaces
├─────────────────────────────────────────────────┤
│              PnP Discovery                      │  <- Auto-discovers servers
│         (pnp_discovery.py)                      │      from SiLA2/ folder
├─────────────────────────────────────────────────┤
│           Your SiLA2 Server                     │  <- Your new server
│     (features/*.sila.xml + servicer.py)         │
├─────────────────────────────────────────────────┤
│              Physical Hardware                  │
└─────────────────────────────────────────────────┘
```

---

## Step-by-Step Guide

### Step 1: Copy the Template

```bash
cd SiLA2
cp -r _NewInstrumentTemplate CentrifugeSiLA2Server  # Example: Centrifuge
```

### Step 2: Configure the Server

Edit `config.yaml`:

```yaml
server:
  port: 50061  # Choose a unique port (50061-50099)

hardware:
  # Your hardware connection settings
  serial_port: "COM3"
  baud_rate: 9600
  # Or for network:
  # host: "192.168.1.100"
  # port: 8080

sila2:
  server_name: "Centrifuge"
  server_uuid: "centrifuge-001"
```

### Step 3: Define Your Commands

Edit `features/YourInstrument.sila.xml`:

```xml
<Feature xmlns="http://www.sila-standard.org"
         FeatureVersion="1.0.0">
    
    <Identifier>Centrifuge</Identifier>
    <DisplayName>Centrifuge</DisplayName>
    
    <!-- Define your commands -->
    <Command>
        <Identifier>Spin</Identifier>
        <DisplayName>Spin</DisplayName>
        <Description>Run centrifuge at specified RPM</Description>
        <Parameter>
            <Identifier>RPM</Identifier>
            <DataType><Basic>Integer</Basic></DataType>
        </Parameter>
        <Parameter>
            <Identifier>Duration</Identifier>
            <DataType><Basic>Integer</Basic></DataType>
        </Parameter>
    </Command>
    
    <Command>
        <Identifier>Stop</Identifier>
        <DisplayName>Emergency Stop</DisplayName>
    </Command>
    
</Feature>
```

### Step 4: Implement the Commands

Edit `src/servicer.py`:

```python
class InstrumentServicer:
    def __init__(self, config):
        self.config = config
        # Initialize your hardware connection
        self.serial = serial.Serial(
            config['hardware']['serial_port'],
            config['hardware']['baud_rate']
        )
    
    async def Spin(self, rpm: int, duration: int):
        """Run centrifuge."""
        self.serial.write(f"SPIN {rpm} {duration}\n".encode())
        response = self.serial.readline()
        return {"status": "running"}
    
    async def Stop(self):
        """Emergency stop."""
        self.serial.write(b"STOP\n")
        return {"stopped": True}
```

### Step 5: Test Your Server

```bash
cd SiLA2/CentrifugeSiLA2Server
python main.py
```

In another terminal:
```bash
python pnp_console.py
# Your server should appear in the list!
```

---

## What Gets Auto-Discovered

The PnP system automatically detects:

| What | Source |
|------|--------|
| Server name | `config.yaml` → sila2.server_name |
| Port | `config.yaml` → server.port |
| Commands | `features/*.sila.xml` |
| Parameters | From XML Parameter elements |

---

## Communication Protocols

Your servicer can use any protocol to talk to hardware:

| Protocol | Use Case | Python Library |
|----------|----------|----------------|
| Serial/RS232 | Lab instruments | `pyserial` |
| HTTP REST | Modern devices | `httpx` |
| TCP Socket | PLCs, controllers | `socket` |
| USB | Sensors, scales | `pyusb` |

---

## Tips

1. **Start simple**: Get basic commands working first
2. **Use simulation mode**: Test without hardware using `config.yaml` → `hardware.simulation: true`
3. **Add logging**: Use `logger.info()` for debugging
4. **Handle errors**: Return meaningful error messages

---

## Files Modified

When you add a new instrument, you **only modify files in your server folder**:

```
SiLA2/YourInstrumentSiLA2Server/
├── config.yaml          ← EDIT: Port and settings
├── features/*.sila.xml  ← EDIT: Command definitions
└── src/servicer.py      ← EDIT: Implementation
```

**No other files in the project need to be changed!**
