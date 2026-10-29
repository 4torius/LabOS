# LabOS — Adding a New Instrument

## The short version

```bash
cd v1
python labos.py new-server CellImager --results Cells
```

That scaffolds the server, generates its SiLA2 bindings, registers it in
`lab_config.yaml`, and declares that it writes its data to `Results/Cells`.
Then you write **one file** — the implementation — and start it:

- the `Results/Cells` folder exists from the first start (the server creates it);
- the instrument appears in the dashboard and in the workflow builder toolbox
  of every open page, without a reload;
- its commands can be sent from the web interface, from a workflow, or from
  `python labos.py call CellImager <Command> Param=value`;
- whatever it writes into `Results/Cells` has its own tab on the Results page.

Nothing in the orchestrator, the web interface or the workflow engine changes:
everything they know about an instrument comes from what the server itself
publishes at runtime — its feature definition, and its `library_catalog.json`.

This used to be nine manual steps involving four renames, three consistent
edits and a codegen run into a temp directory. Each was a chance to get
something subtly wrong, and the failure mode was a server that starts and then
never appears.

---

## What `new-server` does

```
python labos.py new-server Centrifuge [--port 50077] [--key centrifuge]
                                      [--results DIR ...] [--library DIR ...]
```

| # | Step | Detail |
|---|------|--------|
| 1 | Copies the template | `SiLA2/CentrifugeSiLA2Server/` |
| 2 | Renames the feature definition | `features/Centrifuge.sila.xml`, with `<Identifier>Centrifuge</Identifier>` |
| 3 | Renames the implementation | `src/centrifuge_impl.py` |
| 4 | Retargets the entry point | `main.py` imports the renamed module |
| 5 | Fills in `config.yaml` | next free port in 50061–50099, real server name, a SiLA2-valid server type |
| 6 | Writes `library_catalog.json` | with exactly the folders given by `--results`/`--library`, none of the template's examples |
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

### 4. Say everything else in `library_catalog.json`

The feature definition says which commands the instrument accepts. The catalog
says the rest, and it is the only other place anything about the instrument is
written. Every section is optional.

```json
{
  "display": {"icon": "bi-camera", "color": "warning"},

  "library_types": [
    {"hint": "stain_protocol", "display_name": "Stain protocol",
     "folder": "Library/StainProtocols", "file_extension": ".json",
     "parameter_names": ["ProtocolName"]}
  ],

  "result_types": [
    {"key": "cells", "display_name": "Cell images",
     "folder": "Results/Cells", "file_extensions": [".tif", ".png"]}
  ],

  "parameter_defaults": {
    "Snap": {"ExposureMs": 20}
  },

  "traceability": {
    "Snap": {"role": "measures_plate", "plate_parameter": "PlateID"}
  }
}
```

| Section | What it does |
|---|---|
| `display` | Bootstrap icon (`bi-…`) and colour (`primary`, `success`, `warning`, `info`, `danger`, `secondary`, `dark`) for the dashboard card and the toolbox block |
| `library_types` | a parameter named in `parameter_names` becomes a picker over that folder. Instead of `folder`: `values` (+ `value_labels`) for a fixed list, `json_keys_file` for the keys of a Library JSON file, or `source_property` (+ `value_field`, `label_field`, and `folder` for the offline snapshot) for a list the server itself holds |
| `result_types` | a folder under `Results/` the instrument writes. Created when the server starts, created again by the orchestrator at discovery (which is what matters for a server on another computer), and listed as a tab on the Results page |
| `parameter_defaults` | parameters a workflow step may leave out; dispatch fills them in. The step's own value always wins |
| `traceability` | `prepares_plate` (with `recipe_parameter`) or `measures_plate` (with `plate_parameter`, `protocol_parameter`): the plate prepared earlier in a run is passed to a measuring command that did not name one, and the AnIML result is recorded against it on the Database page |

Folders must be under `Library/` or `Results/`; anything else is refused, on
both sides. They are relative to the LabOS root — the directory holding
`SiLA2/`, or `LABOS_ROOT` if the server runs from somewhere else.

`python labos.py doctor` checks every server's catalog.

### 5. Run it, and test it from the command line

```bash
cd SiLA2/CentrifugeSiLA2Server && python main.py
# in another terminal
python labos.py instruments          # listed, with its commands and result folders
python labos.py folders              # its folders exist; what the Results page will show
python labos.py call centrifuge Spin RPM=3000 DurationSeconds=10   # asks before sending
```

`call` goes through exactly the path the web interface and the workflow engine
use - parameter types, declared defaults, errors - so a command that works here
works in a workflow.

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
| Result folders, look, parameter defaults, plate roles | the same catalog |
| Which command means "stop" | the FDL — the executor looks for `AbortRun`/`CancelTask`/`Stop`/`EmergencyStop` and uses whichever you declare, provided it can be sent without arguments (or its server declared defaults for them) |

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
├── library_catalog.json         everything the orchestrator should know besides the commands
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
