# LabOS — Contributing Guide

## Adding a New Instrument

The fastest path to integrating a new instrument is the server template. See [ADDING_NEW_INSTRUMENT.md](ADDING_NEW_INSTRUMENT.md) for the step-by-step guide.

## Code Organization

```
v1/src/              Core orchestration — touch carefully, changes affect everything
v1/SiLA2/            Instrument servers — each is self-contained, safe to modify
v1/webapp/           FastAPI web app — page routes in webapp/routes/
v1/Library/          User-editable assets — no Python code here
v1/docs/             This documentation
v1/tests/            pytest suite (unit/ + integration)
```

The mobile robot's SiLA2 server is **not part of this repository** — its real deployment lives entirely on the remote Linux host. See [ARCHITECTURE.md](ARCHITECTURE.md#mobile-robot-current-status).

## Submitting Changes

1. Create a branch: `git checkout -b feature/your-feature`
2. Make changes and test them (see Testing below)
3. Commit with a clear message explaining the **why**, not just the what
4. Open a pull request against `main`

## Testing

### Smoke test (no hardware required)

```bash
cd v1
python -m pytest tests/ -m "not integration" -v
```

This runs the unit suite under `tests/unit/` (config schema, discovery/port-collision, client timeout handling, bridge launcher, etc.) plus the top-level `tests/test_*.py` files (workflow/recipe parsing, discovery, stub loading). See `tests/unit/_pkg_loader.py` if you need to import server-side `src/` packages under a private module name — both `OpentronsSiLA2Server` and `TecanM200SiLA2Server` ship a package literally named `src`, which would otherwise collide with `v1`'s own top-level `src` package.

### Integration test (hardware required)

With all SiLA2 servers running:

```bash
python -m pytest tests/ -m integration -v
```

Check `tests/test_integration.py` and `tests/test_commands.py` for what's currently covered — commands that cause real physical motion are gated behind an explicit opt-in environment variable; never set it without confirming a human is present to supervise the hardware.

### Manual validation

Run the validation workflows from the web interface:
- `Opentrons_Only_Test.workflow.json` — liquid handling only
- `Manual_Intervention_Test.workflow.json` — manual station
- `example_no_robot.workflow.json` — Opentrons + Tecan

## Style Guide

- Python: PEP 8, type hints on public functions
- No bare `except:` — always catch specific exceptions
- Log with `logging` module, not `print`
- JSON keys: `CamelCase` for workflow/recipe fields (matches existing format), `snake_case` for Python dicts
- Comments: only when the **why** is non-obvious; never document what the code already says

## Adding New SiLA2 Commands

**For servers using the `sila2` library (all current servers):**

1. Add the `<Command>` block to the FDL XML file in `features/`
2. Regenerate stubs: `sila2-codegen features/YourInstrument.sila.xml --output-dir generated/`
3. Add the corresponding method to your `FeatureImplementationBase` subclass in `src/`
4. Restart the server — no orchestrator changes needed
5. The visual designer's dropdown will show the new command after the next browser refresh

**For legacy servers using `SiLA2Common` (if any):**

1. Add the `<Command>` block to the FDL XML file in `features/`
2. Add a handler in `servicer.py`'s `_dispatch()` method
3. Update `GetFeatures()` to return the new command metadata
4. Restart the server

## Modifying the Orchestrator

Changes to `v1/src/` require extra care:

- `lab_core.py` — restart required to pick up changes
- `discovery.py` — changes affect all instrument registration; test with all servers running
- `workflow.py` — changes to execution logic must be tested with fault injection
- `api/` — FastAPI routes; test all endpoint contracts before merging

## Documentation

- Update the relevant doc file in `v1/docs/` when changing behaviour
- All docs must be in English
- No references to project history, internal codenames ("settimana X"), or stale roadmaps
- Code examples must be from real files and must work with the current codebase
