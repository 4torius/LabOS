# New Instrument Server Template

Do not copy this folder by hand. From `v1/`:

```bash
python labos.py new-server CellImager --results Cells
```

That copies this template, renames everything consistently, generates the
SiLA2 bindings, declares the result folder and registers the server. The full
guide is [docs/ADDING_NEW_INSTRUMENT.md](../../docs/ADDING_NEW_INSTRUMENT.md).

What you then edit:

| File | What goes in it |
|---|---|
| `features/<Name>.sila.xml` | the commands and properties - your API. Re-run `python labos.py codegen <Name>SiLA2Server` after each change |
| `src/<name>_impl.py` | the implementation of every command |
| `library_catalog.json` | everything else the orchestrator should know: Library files it reads, Results folders it writes (created when the server starts), parameters a workflow may omit, which commands prepare or measure a plate, its icon |
| `config.yaml` | port, server name and type, hardware connection |

## Pointing a parameter at the Library

Each entry of `library_types` turns the parameters in `parameter_names` into a
picker in the Workflow Builder and the dashboard. The value the command
receives is the name the picker shows (or `value_field`). Pick one source:

```jsonc
// one option per file in a folder (the file name without extension)
{"hint": "stain_protocol", "display_name": "Stain protocol",
 "folder": "Library/StainProtocols", "file_extension": ".json",
 "parameter_names": ["ProtocolFile"]}

// one option per Library JSON file matching a pattern, that has a field -
// e.g. the shared plates this instrument can handle (see docs/NUOVA_PIASTRA.md)
{"hint": "imager_plate", "display_name": "Plate",
 "json_files": "Library/Labware/Plates/*.plate.json",
 "require_field": "imager_plate_name", "value_field": "id", "label_field": "display_name",
 "parameter_names": ["PlateType"]}

// the keys of one Library JSON file
{"hint": "filter_set", "json_keys_file": "Library/Imager/filters.json",
 "parameter_names": ["FilterSet"]}

// a fixed list
{"hint": "speed", "values": ["slow", "fast"], "value_labels": {"slow": "Slow (quiet)"},
 "parameter_names": ["Speed"]}

// a list the server itself holds in a property; `folder` keeps the last list
// for when the server is off, `refreshed_by` is the command that re-reads it
// (the dashboard log then shows what it found)
{"hint": "imager_slot", "source_property": "Slots", "value_field": "SlotId",
 "label_field": "Label", "folder": "Library/ImagerSlots", "refreshed_by": "RescanSlots",
 "parameter_names": ["SlotId"]}
```

Folders must be under `Library/` or `Results/`. The server creates the folders it
declares when it starts. Reuse the shared folders (`Library/Labware/...`) for
things another instrument also uses, so a plate has one name everywhere.

`src/library_provider_impl.py` and `generated/laboslibraryprovider/` are the
same in every LabOS server; do not change them here.

Test it without the web interface:

```bash
python main.py                                   # in this folder
python labos.py instruments                      # from v1/: it is listed
python labos.py folders                          # its folders exist
python labos.py call cell_imager Snap ExposureMs=20
```
