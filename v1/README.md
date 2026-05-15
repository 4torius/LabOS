# LabOS v1

## Quick Start

**Windows:** double-click `START.bat`

**Terminal:**
```bash
python launcher.py          # interactive menu
python launcher.py --all    # start everything immediately
```

Web interface: **http://localhost:5000**

## Project Structure

```
v1/
├── START.bat               Windows quick start
├── launcher.py             main entry point
├── lab_config.yaml         centralized configuration
├── requirements.txt        Python dependencies
│
├── SiLA2/                  instrument servers
│   ├── TecanM200SiLA2Server/
│   ├── OpentronsSiLA2Server/
│   └── ManualStationSiLA2Server/
│
├── src/                    orchestration core
├── webapp/                 web dashboard (FastAPI)
├── Library/                workflows, recipes, HAL configs
├── Results/                measurement outputs and run logs
└── docs/                   full documentation
```

## Documentation

See [docs/README.md](docs/README.md) for the full documentation index.

## Adding a New Instrument

Copy the template, define commands in XML, implement the servicer — the system auto-discovers the rest. See [docs/ADDING_NEW_INSTRUMENT.md](docs/ADDING_NEW_INSTRUMENT.md).
