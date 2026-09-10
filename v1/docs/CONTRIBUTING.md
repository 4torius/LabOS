# LabOS — Contributing Guide

## Adding a New Instrument

The fastest path to integrating a new instrument is the server template. See [ADDING_NEW_INSTRUMENT.md](ADDING_NEW_INSTRUMENT.md) for the step-by-step guide.

## Code Organization

```
v1/labos.py          Command line — start here for anything operational
v1/src/              Core orchestration — touch carefully, changes affect everything
v1/SiLA2/            Instrument servers — each is self-contained, safe to modify
v1/webapp/           FastAPI web app — page routes in webapp/routes/
v1/Library/          User-editable assets — no Python code here
v1/scripts/          One-off maintenance (workflow migration)
v1/docs/             This documentation
v1/tests/            pytest suite (unit/ + integration)
```

### Single-implementation rules

Four things have exactly one implementation, deliberately, because each used to
have several that disagreed. Adding a second is a regression, not a shortcut:

| Concept | The one implementation |
|---|---|
| Reading `lab_config.yaml` | `src/settings.py::get_settings()` — do not call `yaml.safe_load` on it |
| Matching an instrument name | `src/resolver.py::resolve()` — no substring matching anywhere else |
| Ordering workflow steps | `src/workflow.py::topological_waves()` |
| "0 or null means unlimited" | `src/settings.py::normalize_timeout()` |

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
python labos.py doctor                    # config, dependencies, schema versions
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

1. Add the `<Command>` (or `<Property>`) block to the FDL in `features/`
2. `python labos.py codegen YourInstrumentSiLA2Server`
3. Implement the new abstract method in your `FeatureImplementationBase`
   subclass — the server refuses to start until you do, which is the behaviour
   you want
4. Restart the server. No orchestrator change; the builder shows it on the next
   browser refresh

**Two conventions to follow:**

- **A command does something; a reading is a Property.** If the caller is only
  asking what is already true, declare a `<Property>`. Properties appear in a
  "Live readings" panel instead of as buttons an operator presses to find
  something out, and an observable one is pushed rather than polled.
- **Begin the description with `MOVES HARDWARE`** when the command physically
  moves the instrument. That description is what the operator reads in the
  command list.

There is no legacy `SiLA2Common` path any more — it was removed along with its
two extra FDL parsers and the dynamic-stub execution strategy.

## Modifying the Orchestrator

Changes to `v1/src/` require extra care:

- `settings.py` — every component reads through it; a change affects everything
- `resolver.py` — decides which physical machine a step addresses. Treat
  ambiguity as an error, always
- `lab_core.py` — restart required to pick up changes
- `discovery.py` — affects all instrument registration; test with the servers running
- `workflow.py` — execution logic; test with fault injection. `tests/unit/`
  covers ordering, abort and validation
- `run_manager.py` — owns the single run slot; a leaked slot blocks every
  later run
- `webapp/routes/` — FastAPI routes; check the endpoint contracts before merging

## Documentation

- Update the relevant doc file in `v1/docs/` when changing behaviour
- All docs must be in English
- No references to project history, internal codenames ("settimana X"), or stale roadmaps
- Code examples must be from real files and must work with the current codebase
