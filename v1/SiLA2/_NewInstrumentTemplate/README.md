# New Instrument Server Template

This is a **plug & play** template for creating new SiLA2 instrument servers.

## Quick Start (3 steps)

### 1. Copy and Rename This Folder
```bash
# Example: Creating a server for a Centrifuge
cp -r _NewInstrumentTemplate CentrifugeSiLA2Server
```

### 2. Define Your Commands in `features/YourInstrument.sila.xml`

Edit the XML file to define your instrument's commands:
- Each `<Command>` becomes a callable method
- Parameters and responses are auto-discovered

### 3. Implement the Servicer

Edit `src/servicer.py`:
- Implement each command defined in your .sila.xml
- Add your instrument communication code

## That's It!

The PnP system will automatically:
- Discover your server folder
- Parse your .sila.xml features
- Read port from config.yaml
- Show your instrument in the console and webapp

**NO OTHER FILES NEED TO BE MODIFIED!**

## Folder Structure

```
YourInstrumentSiLA2Server/
├── config.yaml           # Server configuration (port, etc.)
├── main.py               # Server entry point (usually unchanged)
├── features/
│   └── YourInstrument.sila.xml  # Command definitions (EDIT THIS)
├── src/
│   ├── __init__.py
│   └── servicer.py       # Command implementations (EDIT THIS)
└── README.md             # This file
```

## Testing Your Server

```bash
cd YourInstrumentSiLA2Server
python main.py
```

Then in another terminal:
```bash
python pnp_console.py
# Your server should appear automatically!
```

## Common Commands to Add

- `Initialize` - Connect to hardware
- `GetStatus` - Return current state
- `Home` - Home/reset the instrument
- `Stop` - Emergency stop
- Instrument-specific operations

## Tips

1. Keep commands simple and focused
2. Use descriptive parameter names
3. Return meaningful error messages
4. Add `<Description>` tags for better documentation
