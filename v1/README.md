# LabOS v1

Laboratory orchestration on the [SiLA2](https://sila-standard.org) standard.
Instruments describe their own capabilities at runtime, so the orchestrator,
the web interface and the workflow engine contain no instrument-specific code.

## Quick Start

**Windows:** double-click `LabOS_launcher.bat`

**Terminal:**
```bash
python launcher.py          # interactive menu
python launcher.py --all    # start everything immediately
```

Web interface: **http://localhost:5000**

## Command line

```bash
python labos.py doctor                # check the installation
python labos.py status                # which servers and interfaces are up
python labos.py instruments           # what was discovered, and what it can do
python labos.py read opentrons Status # read one property
python labos.py validate ELISA        # check a workflow without running it
python labos.py run ELISA             # run it, streaming per-step progress
python labos.py new-server Centrifuge # scaffold and register a new instrument
```

## Project Structure

```
v1/
├── LabOS_launcher.bat      Windows quick start
├── launcher.py             starts the servers and the web app
├── labos.py                command line
├── lab_config.yaml         centralized configuration
│
├── SiLA2/                  instrument servers
│   ├── TecanM200SiLA2Server/
│   ├── OpentronsSiLA2Server/
│   ├── ManualStationSiLA2Server/
│   └── _NewInstrumentTemplate/
│
├── src/                    orchestration core
├── webapp/                 web dashboard (FastAPI)
├── Library/                workflows, recipes, HAL configs, labware
├── Results/                measurements, run history, deck photos
└── docs/                   full documentation
```

## Documentation

Start with [docs/README.md](docs/README.md), or
[docs/TRAINING_GUIDE.md](docs/TRAINING_GUIDE.md) if you are onboarding a new
operator. [docs/REFACTOR_PLAN.md](docs/REFACTOR_PLAN.md) records the structural
work done in September 2026 and why.

## Adding a New Instrument

```bash
python labos.py new-server Centrifuge
```

That scaffolds the server, generates its SiLA2 bindings and registers it. Then
you write one file: the implementation. See
[docs/ADDING_NEW_INSTRUMENT.md](docs/ADDING_NEW_INSTRUMENT.md).

## Testing

```bash
python -m pytest tests -m "not integration"   # no hardware needed
python -m pytest tests -m integration         # requires the servers running
```
