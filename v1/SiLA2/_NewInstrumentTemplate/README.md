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

`src/library_provider_impl.py` and `generated/laboslibraryprovider/` are the
same in every LabOS server; do not change them here.

Test it without the web interface:

```bash
python main.py                                   # in this folder
python labos.py instruments                      # from v1/: it is listed
python labos.py folders                          # its folders exist
python labos.py call cell_imager Snap ExposureMs=20
```
