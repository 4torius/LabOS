# LabOS — Adding a New Instrument

## The short version

```bash
cd v1
python labos.py new-server Centrifuge
```

That scaffolds the server, generates its SiLA2 bindings, and registers it in
`lab_config.yaml`. Then you write **one file** — the implementation — and start
it. Nothing in the orchestrator, the web interface or the workflow engine needs
to change: everything they show about an instrument comes from what the server
itself publishes at runtime.

This used to be nine manual steps involving four renames, three consistent
edits and a codegen run into a temp directory. Each was a chance to get
something subtly wrong, and the failure mode was a server that starts and then
never appears.

---

## What `new-server` does

```
python labos.py new-server Centrifuge [--port 50077] [--key centrifuge]
```

| # | Step | Detail |
|---|------|--------|
| 1 | Copies the template | `SiLA2/CentrifugeSiLA2Server/` |
| 2 | Renames the feature definition | `features/Centrifuge.sila.xml`, with `<Identifier>Centrifuge</Identifier>` |
| 3 | Renames the implementation | `src/centrifuge_impl.py` |
| 4 | Retargets the entry point | `main.py` imports the renamed module |
| 5 | Fills in `config.yaml` | next free port in 50061–50099, real server name and uuid |
| 6 | Writes an empty `library_catalog.json` | the template's example entry would advertise `Library/` folders this instrument does not use |
| 7 | Runs `sila2-codegen` | into `generated/centrifuge/`, without the server skeleton nobody wants |
| 8 | Registers it in `lab_config.yaml` | as a bootstrap seed, comments preserved |

The name must be CamelCase starting with a capital (`Centrifuge`,
`PlateSealer`) — that is a SiLA2 requirement, and the command refuses anything
else up front rather than letting codegen fail later.

---

## Then: define what it does

### 1. Describe the commands (FDL)

`features/Centrifuge.sila.xml` **is** your API. Anything you declare here
appears automatically in the dashboard, the workflow builder and the CLI.

```xml
<Command>
    <Identifier>Spin</Identifier>
    <DisplayName>Spin</DisplayName>
    <Description>MOVES HARDWARE. Spin at the given speed for the given time.</Description>
    <Observable>Yes</Observable>
    <Parameter>
        <Identifier>RPM</Identifier>
        <DisplayName>RPM</DisplayName>
        <Description>Rotation speed.</Description>
        <DataType><Basic>Integer</Basic></DataType>
    </Parameter>
    <Response>
        <Identifier>FinalRPM</Identifier>
        <DisplayName>Final RPM</DisplayName>
        <Description>Speed reached.</Description>
        <DataType><Basic>Integer</Basic></DataType>
    </Response>
</Command>
```

**Two conventions worth following**, because the rest of LabOS assumes them:

- **A command does something; a reading is a Property.** If the client is only
  asking what is already true — status, current configuration, how many tips
  are left — declare a `<Property>`, not a `Get*` command. Properties show up
  in a "Live readings" panel rather than as buttons an operator has to press to
  find something out, and an `<Observable>Yes</Observable>` property is pushed
  to subscribers instead of polled.
- **Say so when a command moves hardware.** Begin its `<Description>` with
  `MOVES HARDWARE`. The operator reads that description in the command list,
  and it is the only thing standing between them and a surprise.

**Schema rules that bite:**
- `FeatureVersion` is `"1.0"`, never `"1.0.0"`
- for an observable command, `<Response>` comes *before* `<IntermediateResponse>`
- `<Parameter>` elements come before both

### 2. Regenerate the bindings

```bash
python labos.py codegen CentrifugeSiLA2Server
```

Every command you added is now an abstract method in
`generated/centrifuge/centrifuge_base.py`. The server refuses to start until
your implementation provides all of them — which is the behaviour you want:
a missing method is caught at startup, not on the first call.

### 3. Implement them

In `src/centrifuge_impl.py`:

```python
class CentrifugeImpl(CentrifugeBase):
    def __init__(self, parent_server, config: dict):
        super().__init__(parent_server=parent_server)
        self._simulation = config.get("hardware", {}).get("simulation", True)

    def Spin(self, RPM: int, DurationSeconds: int, *, metadata, instance) -> Spin_Responses:
        instance.begin_execution()
        for elapsed in range(DurationSeconds):
            time.sleep(1)
            instance.progress = (elapsed + 1) / DurationSeconds
        return Spin_Responses(FinalRPM=RPM)
```

These methods run on a thread pool, so use `time.sleep`, not `asyncio.sleep`.
If your hardware library is async, bridge it with a dedicated event loop —
`SiLA2/OpentronsSiLA2Server/src/workflow_api_impl.py` shows the pattern.

### 4. Declare the `Library/` files it reads, if any

Only if your instrument consumes files from `Library/` (methods, protocols).
In `library_catalog.json`:

```json
{
  "library_types": [
    {
      "hint": "centrifuge_method",
      "display_name": "Centrifuge Method",
      "folder": "Library/CentrifugeMethods",
      "file_extension": ".json",
      "parameter_names": ["MethodName"]
    }
  ]
}
```

The orchestrator reads this at discovery time and turns any parameter named
`MethodName` into a file picker showing that folder. No orchestrator change,
no `lab_config.yaml` change.

### 5. Run it

```bash
cd SiLA2/CentrifugeSiLA2Server && python main.py
# in another terminal
python labos.py instruments        # it should be listed, with its commands
```

Develop against `hardware.simulation: true` in `config.yaml` until the logic is
right, then point it at the real device.

---

## What the orchestrator discovers by itself

| What | Where it comes from |
|------|---------------------|
| Server name and type | the server's `config.yaml` → `sila2.server_name` / `server_type` |
| Commands, parameters, responses | the FDL, fetched live via `SiLAService.GetFeatureDefinition()` |
| Properties | the same |
| File-picker dropdowns | `LabOSLibraryProvider.LibraryCatalog` (reads `library_catalog.json`) |
| Which command means "stop" | the FDL — the executor looks for `AbortRun`/`CancelTask`/`Stop`/`EmergencyStop` and uses whichever you declare |

That last one matters: expose one of those and your instrument becomes
abortable from the web interface's Abort button, with no orchestrator change.

---

## Files in your server folder

```
SiLA2/CentrifugeSiLA2Server/
├── config.yaml                  port, hardware settings, server metadata
├── features/
│   └── Centrifuge.sila.xml      YOUR API — commands, properties, parameters
├── generated/
│   ├── centrifuge/              codegen output (commit it)
│   └── laboslibraryprovider/    shared, do not modify
├── library_catalog.json         which Library/ file types this instrument uses
├── main.py                      entry point (already wired)
└── src/
    ├── centrifuge_impl.py       YOUR IMPLEMENTATION
    └── library_provider_impl.py shared, do not modify
```

**Re-run codegen** every time you change the FDL.

---

## Communication protocols

Your implementation can use any Python library:

| Protocol | Library |
|----------|---------|
| Serial / RS232 | `pyserial` |
| HTTP REST | `httpx` |
| TCP socket | `socket` |
| USB / HID | `pyusb`, `hid` |
| MODBUS | `pymodbus` |

---

## Doing it by hand

`new-server` is a convenience, not a requirement — the template is a normal
folder and every step above is an ordinary edit. If you scaffold manually, run
`python labos.py doctor` afterwards: it checks the configuration parses, the
ports do not collide and codegen is available.
