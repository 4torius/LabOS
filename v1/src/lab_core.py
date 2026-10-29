#!/usr/bin/env python3
"""
================================================================================
                            LabCore - Shared Core Module
================================================================================

This is the SINGLE SOURCE OF TRUTH for lab automation logic.
Both CLI and WebApp use this module for all operations.

Architecture:
    ┌─────────────┐   ┌─────────────┐
    │    CLI      │   │   WebApp    │
    └──────┬──────┘   └──────┬──────┘
           │                 │
           └────────┬────────┘
                    │
             ┌──────▼──────┐
             │  LabCore    │  ← You are here
             │  - discovery │
             │  - commands  │
             │  - files     │
             │  - workflows │
             └─────────────┘

Usage:
    from src.lab_core import LabCore
    
    core = LabCore()
    await core.discover()
    
    # List available instruments
    instruments = core.list_instruments()
    
    # Execute a command
    result = await core.execute_command("opentrons_flex", "ExecuteRecipe", {"RecipeName": "test.json"})
    
    # Get available files
    recipes = core.list_files("recipes")
    analyses = core.list_files("analyses")
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Awaitable, Tuple

# Local imports
try:
    from src.settings import get_settings
    from src.catalog import (ServerCatalog, ensure_folders, is_infrastructure_feature,
                             parse_catalog, feature_name as _feature_name)
    from src.options import HintRegistry, build_registry as build_hint_registry
    from src.deck import DECK_HINTS, options as deck_options
    from src.resolver import Resolution, resolve
    from src.discovery import PnPDiscovery, PnPServer, PnPCommand
    from src.client import PnPClient, PnPRegistry, CommandResult as PnPResult
    from src.workflow import (
        PnPWorkflowExecutor,
        InterventionRequest,
        InterventionAction,
        InterventionCallback,
        WorkflowStep,
        WorkflowProgress,
        Workflow,
        StepStatus
    )
except ImportError:
    # Running from different directory
    from settings import get_settings
    from catalog import (ServerCatalog, ensure_folders, is_infrastructure_feature,
                         parse_catalog, feature_name as _feature_name)
    from options import HintRegistry, build_registry as build_hint_registry
    from deck import DECK_HINTS, options as deck_options
    from resolver import Resolution, resolve
    from discovery import PnPDiscovery, PnPServer, PnPCommand
    from client import PnPClient, PnPRegistry, CommandResult as PnPResult
    from workflow import (
        PnPWorkflowExecutor,
        InterventionRequest,
        InterventionAction,
        InterventionCallback,
        WorkflowStep,
        WorkflowProgress,
        Workflow,
        StepStatus
    )

logger = logging.getLogger(__name__)


#                           DATA CLASSES

@dataclass
class CommandParameter:
    """Parameter for a command with UI hints."""
    name: str
    display_name: str = ""
    description: str = ""
    data_type: str = "String"
    required: bool = True
    default_value: str = ""
    ui_hint: str = ""           # "recipe", "analysis", "hal_config", "liquid_class", "location", "labware"
    options: List[str] = field(default_factory=list)          # values the UI may offer
    options_detail: List[Dict[str, str]] = field(default_factory=list)  # {value,label} pairs
    
    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "displayName": self.display_name or self.name,
            "description": self.description,
            "type": self.data_type,
            "required": self.required,
            "default": self.default_value,
            "ui_hint": self.ui_hint,
            "options": self.options,
            # {value,label} pairs when the values are not self-explanatory: a
            # deck slot is, "Source_1" is not. The form falls back to `options`
            # when this is empty, so nothing has to special-case it.
            "options_detail": self.options_detail,
        }


@dataclass
class Command:
    """A command that can be executed on an instrument."""
    id: str
    name: str
    description: str = ""
    feature: str = ""
    important: bool = False
    observable: bool = False  # SiLA2 observable command - see execute_command()'s timeout handling
    parameters: List[CommandParameter] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "feature": self.feature,
            "important": self.important,
            "parameters": [p.to_dict() for p in self.parameters]
        }


@dataclass
class Property:
    """A readable value a server publishes.

    Properties were parsed out of the FDL and then dropped: an Instrument
    carried only commands, so nothing downstream could see them and the UI had
    no way to show a reading that is not an operation. That is a large part of
    why instruments grew GetStatus-style commands in the first place.
    """
    id: str
    name: str
    description: str = ""
    feature: str = ""
    data_type: str = "String"
    observable: bool = False

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "feature": self.feature,
            "type": self.data_type,
            "observable": self.observable,
        }


@dataclass
class Instrument:
    """A discovered instrument with its capabilities."""
    id: str
    name: str
    type: str = "instrument"
    status: str = "offline"
    host: str = "localhost"
    port: int = 0
    commands: List[Command] = field(default_factory=list)
    properties: List[Property] = field(default_factory=list)
    # How the server asks to be drawn ({"icon", "color"}) and the Results
    # folders it writes into - both declared by the server, see src/catalog.py.
    display: Dict[str, str] = field(default_factory=dict)
    results: List[Dict[str, Any]] = field(default_factory=list)
    catalog: ServerCatalog = field(default_factory=ServerCatalog, repr=False)

    # Internal reference to PnP server
    _server: Optional[PnPServer] = field(default=None, repr=False)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "status": self.status,
            "address": f"{self.host}:{self.port}",
            "display": dict(self.display),
            "results": list(self.results),
            "commands": [c.to_dict() for c in self.commands],
            "properties": [p.to_dict() for p in self.properties],
        }


@dataclass
class ExecutionResult:
    """Result of a command execution."""
    success: bool
    message: str = ""
    data: Any = None
    error: str = ""
    #: Picker lists re-read after this command, so the UI can refresh them.
    options_changed: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "success": self.success,
            "message": self.message,
            "data": self.data,
            "error": self.error,
            "options_changed": list(self.options_changed),
        }


#                           LAB CORE

def coerce_parameters(
    parameters: Dict[str, Any], declared: List["CommandParameter"]
) -> Dict[str, Any]:
    """Parameter values as the types the command declared them.

    Everything that reaches a command has been through something that only
    carries text - an HTML form, a JSON workflow file somebody typed, a curl.
    A Boolean written as "false" is a non-empty string, which is emphatically
    not false, and SiLA2 refuses it with "Expected a bool value".

    Two real runs died on exactly that, four attempts each, after the plate
    reader had already moved the plate:

        SetModuleLatch  {"ModuleName": "HeaterShaker", "Open": "false"}
        MoveLabware     {"LabwareName": "TargetPlate", "UseGripper": "UseGripper"}

    The second one is the parameter's own name, typed into a text box whose
    placeholder said "Enter UseGripper" - the workflow builder had no Boolean
    field, so both fell through to free text. The Dashboard, which renders a
    checkbox, was fine with the same command on the same server.

    Fixing the form was necessary and not sufficient: a workflow saved before
    that fix still has "false" in its JSON, and the orchestrator is the one
    place every client passes through. Only declared parameters are touched;
    anything else (hal_config and the other workflow-only keys) goes by
    untouched. A value that is not the declared type at all raises
    ParameterTypeError, naming the parameter and what it accepts, rather than
    being quietly reinterpreted.
    """
    if not declared:
        return parameters
    types = {p.name: (p.data_type or "").lower() for p in declared}
    out: Dict[str, Any] = {}
    for key, value in (parameters or {}).items():
        kind = types.get(key)
        if kind == "boolean":
            out[key] = _as_bool(key, value)
        elif kind == "integer":
            out[key] = _as_number(key, value, int)
        elif kind in ("real", "double", "float"):
            out[key] = _as_number(key, value, float)
        else:
            out[key] = value
    return out


#: What a boolean may be written as. Anything outside both sets is not a
#: mistyped boolean, it is not a boolean at all.
_TRUTHY = frozenset({"true", "1", "yes", "on", "si", "sì", "y", "t"})
_FALSY = frozenset({"false", "0", "no", "off", "n", "f", ""})


class ParameterTypeError(ValueError):
    """A parameter value that cannot be the type the command declared."""


def _as_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    # Deliberately not "anything unrecognised is false". MoveLabware arrived
    # once with UseGripper="UseGripper" - the parameter's own name, typed into
    # a text box - and reading that as false means a manual move: the robot
    # loads the deck, then stops and waits at its touchscreen for somebody to
    # carry the plate by hand. Confirmed on hardware. A nonsense value should
    # say so immediately, not turn into a quiet instruction to a human.
    raise ParameterTypeError(
        f"'{name}' is a true/false parameter and cannot be {value!r}. "
        f"Write true or false."
    )


def _as_number(name: str, value: Any, cast) -> Any:
    if isinstance(value, bool):          # bool is an int; that is not intended here
        return cast(int(value))
    if isinstance(value, (int, float)):
        return cast(value)
    text = str(value).strip()
    if not text:
        return value
    try:
        return cast(float(text)) if cast is int else cast(text)
    except (TypeError, ValueError):
        raise ParameterTypeError(
            f"'{name}' is a {'whole number' if cast is int else 'number'} "
            f"parameter and cannot be {value!r}."
        ) from None


class LabCore:
    """
    Core module for lab automation.
    
    This is the shared logic layer that both CLI and WebApp use.
    """
    
    def __init__(self, base_dir: Optional[Path] = None):
        """
        Initialize LabCore.
        
        Args:
            base_dir: Base directory of the project (defaults to parent of src/)
        """
        if base_dir is None:
            base_dir = Path(__file__).parent.parent
        
        self.base_dir = Path(base_dir)
        self.library_dir = self.base_dir / "Library"
        
        # State
        self._discovery: Optional[PnPDiscovery] = None
        self._instruments: Dict[str, Instrument] = {}
        # important_commands removed (Week 3): all commands come from GetFeatures
        self._client: Optional[PnPClient] = None  # For command execution
        self._workflow_executor: Optional[PnPWorkflowExecutor] = None
        self._intervention_callback: Optional[InterventionCallback] = None
        
        # Callbacks for UI updates
        self._on_discovery_update: Optional[Callable] = None
        self._on_command_start: Optional[Callable] = None
        self._on_command_complete: Optional[Callable] = None
        self._on_log: Optional[Callable] = None
        self._on_workflow_step: Optional[Callable] = None  # Step progress callback
        
        # Last option list read from a server, per hint (e.g. mobile_task from
        # the mobile robot's AvailableTasks). Whole options, not just their
        # ids: the picker has to show "pick_from_tecan" while submitting the
        # task id, so the label has to survive the trip.
        self._dynamic_options_cache: Dict[str, List[Dict[str, Any]]] = {}

        # Discovery cache: avoid re-running gRPC discovery on every API call
        self._last_discovery_time: float = 0.0
        self._discovery_cache_ttl: float = 30.0  # overwritten by _load_config()

        # Load config
        self._load_config()
    
    #                           CONFIGURATION
    
    def _load_config(self):
        """Take a settings snapshot (parsed once, shared process-wide)."""
        settings = get_settings(self.base_dir)
        # A mutable copy: _server_to_instrument() merges each server's
        # LabOSLibraryProvider catalog into this map as instruments are
        # discovered, and must not write into the shared frozen snapshot.
        self._ui_dropdowns = dict(settings.ui_dropdowns)
        self._hint_registry_cache: Optional["HintRegistry"] = None
        self._hint_registry_signature: Optional[tuple] = None
        self._discovery_cache_ttl = settings.discovery.scan_interval

    def _command_timeout(self) -> Optional[float]:
        """Max seconds to wait for a single command to finish.

        Reuses workflow.default_timeout — the same "how long do we wait for one
        hardware operation" knob the workflow executor uses — rather than a
        second, easy-to-desync value. Already normalized: None means unlimited.

        This used to re-open and re-parse lab_config.yaml on every single
        command execution, and it turned an explicit null (documented as
        "unlimited") into a hard 300 s.
        """
        return get_settings(self.base_dir).workflow.default_timeout

    def _get_options_for_hint(self, ui_hint: str) -> List[str]:
        """
        Get dynamic options based on UI hint from lab_config.yaml.
        
        The ui_dropdowns section in lab_config.yaml defines:
        - folder: Read files from this folder
        - file_extension: Filter by file extension
        - options: Static list of options
        - dynamic: If true, options are fetched from SiLA2 server at runtime
        
        Args:
            ui_hint: Type of parameter ("recipe", "analysis", "hal_config", etc.)
            
        Returns:
            List of available options
        """
        # Deck hints: what is actually on the deck, per the active hardware
        # config. See src/deck.py for why these exist.
        if ui_hint in DECK_HINTS:
            return [o["value"] for o in deck_options(self._hal_dir(), ui_hint)]

        # Everything declarative goes through one reader, layered exactly as
        # resources are: the server wins for what it declares, the config
        # speaks for servers that cannot, and the config owns the hints that
        # belong to no instrument. See src/options.py — this used to be four
        # `if ui_hint == "..."` branches with the folder walks written out
        # here, each sitting next to a config entry for the same hint.
        dropdown_config = self._ui_dropdowns.get(ui_hint) or {}
        if dropdown_config.get("dynamic") or dropdown_config.get("source_property"):
            # Property-backed: the values are on a server, not on disk. The
            # entry's `folder` is the offline snapshot, not a source.
            return [o["id"] for o in self._dynamic_option_items(ui_hint)]

        spec = self._hint_registry().get(ui_hint)
        if spec is not None:
            if spec.dynamic:
                return [o["id"] for o in self._dynamic_option_items(ui_hint)]
            return spec.resolve(self.base_dir)

        # Location: discovered instruments
        if ui_hint == "location":
            return self._get_available_locations()

        return []
    
    def _hint_registry(self) -> "HintRegistry":
        """Which hint comes from where, rebuilt when the instrument set changes.

        Not cached across discovery: an instrument appearing brings its pickers
        with it, which is the whole point of a server declaring them.
        """
        signature = tuple(sorted(self._instruments))
        if self._hint_registry_cache is None or self._hint_registry_signature != signature:
            self._hint_registry_cache = build_hint_registry(
                {i.id: i.catalog.library_types for i in self._instruments.values()},
                (get_settings(self.base_dir).raw.get("library_types") or {}).get("declare"),
                self._ui_dropdowns,
            )
            self._hint_registry_signature = signature
            for problem in self._hint_registry_cache.problems:
                logger.warning("dropdown declaration: %s", problem)
        return self._hint_registry_cache

    def _hal_dir(self) -> Path:
        return get_settings(self.base_dir).hardware_config_dir

    def _option_pairs_for_hint(self, ui_hint: str) -> List[Dict[str, str]]:
        """{value,label} pairs for hints whose values need explaining.

        A deck slot speaks for itself; "Source_1" does not, and an operator
        choosing what to move needs to see which plate that is and where it
        currently sits.
        """
        if ui_hint in DECK_HINTS:
            return deck_options(self._hal_dir(), ui_hint)

        declared = self._ui_dropdowns.get(ui_hint) or {}
        labels = declared.get("option_labels") or {}
        if declared.get("options") is not None and labels:
            return [{"value": v, "label": labels.get(v, v) or v}
                    for v in declared["options"]]

        # A property-backed list arrives as {id, name}: the mobile robot's
        # tasks are ids like "bsffcndfdddchyrrbefecdanbcscfddy", and an
        # operator choosing what the robot should do has to read
        # "pick_from_tecan". The command still receives the id.
        if declared.get("dynamic") or declared.get("source_property"):
            return [{"value": o["id"], "label": str(o.get("name") or o["id"])}
                    for o in self._dynamic_option_items(ui_hint)]
        return []

    def _dynamic_option_items(self, ui_hint: str) -> List[Dict[str, Any]]:
        """The last options read for a property-backed picker, without asking.

        Reading the property is async; building a command's parameters is not.
        So this offers what the last read left behind, falling back to the
        snapshot kept on disk for when that server has not been reached yet
        (or is off).
        """
        cached = self._dynamic_options_cache.get(ui_hint)
        if cached:
            return cached
        folder = (self._ui_dropdowns.get(ui_hint) or {}).get("folder")
        return self._read_options_snapshot(folder) if folder else []

    def get_dropdown_config(self) -> Dict[str, Any]:
        """
        Get the full dropdown configuration for frontend use.
        
        Returns:
            Dict mapping ui_hint to dropdown configuration
        """
        return self._ui_dropdowns.copy()
    
    def _get_available_locations(self) -> List[str]:
        """Return discovered instrument IDs as available locations."""
        return sorted(self._instruments.keys())
    
    def _json_keys(self, relative: str) -> List[str]:
        """Top-level keys of a Library JSON file, skipping `_comment`-style ones."""
        path = (self.base_dir / relative).resolve()
        try:
            path.relative_to(get_settings(self.base_dir).library_dir.resolve())
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            return []
        if not isinstance(data, dict):
            return []
        return [k for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)]

    #: File a property-backed option list is kept in, inside its declared folder.
    OPTIONS_SNAPSHOT = "_options.json"

    def _read_options_snapshot(self, folder: str) -> List[Dict[str, Any]]:
        """The last option list read from a server, as kept on disk.

        Also reads the older one-file-per-option layout Library/MobileTasks
        was written in, so an existing snapshot keeps working.
        """
        directory = self.base_dir / folder
        snapshot = directory / self.OPTIONS_SNAPSHOT
        try:
            if snapshot.is_file():
                items = json.loads(snapshot.read_text(encoding="utf-8-sig"))
                return [i for i in items if isinstance(i, dict) and i.get("id")]
        except (OSError, ValueError) as exc:
            logger.warning("Unreadable option snapshot %s: %s", snapshot, exc)
        options = []
        if directory.is_dir():
            for item in sorted(directory.glob("*.json")):
                try:
                    data = json.loads(item.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    continue
                if isinstance(data, dict):
                    options.append({**data, "id": str(data.get("id", item.stem)),
                                    "name": str(data.get("name", item.stem))})
        return options

    def _write_options_snapshot(self, folder: str, options: List[Dict[str, Any]]) -> None:
        directory = self.base_dir / folder
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / self.OPTIONS_SNAPSHOT).write_text(
                json.dumps(options, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not keep option snapshot in %s: %s", directory, exc)

    def _option_source(self, ui_hint: str) -> Optional[Tuple[Instrument, Dict[str, Any]]]:
        """The instrument, and the declaration, behind a property-backed picker.

        A server declares it in its catalog:
            {"hint": "mobile_task", "parameter_names": ["TaskId"],
             "source_property": "AvailableTasks", "value_field": "TaskId",
             "label_field": "TaskName", "folder": "Library/MobileTasks"}
        lab_config.yaml's ui_dropdowns may supply or override the same keys,
        with `server` naming the instrument, for a server whose catalog cannot
        be changed (one deployed on another machine).
        """
        configured = dict(self._ui_dropdowns.get(ui_hint) or {})
        owner = None
        if configured.get("server"):
            owner = (self.get_instrument(str(configured["server"]))
                     or self.find_instrument(str(configured["server"])))
        declaration: Dict[str, Any] = {}
        for instrument in self._instruments.values():
            if owner is not None and instrument is not owner:
                continue
            entry = next((e for e in instrument.catalog.library_types
                          if e.get("hint") == ui_hint), None)
            if entry:
                declaration = dict(entry)
                owner = owner or instrument
                break
        declaration.update({k: v for k, v in configured.items() if v not in (None, "")})
        if owner is None or not declaration.get("source_property"):
            return None
        return owner, declaration

    async def fetch_dynamic_options(self, ui_hint: str) -> List[Dict[str, Any]]:
        """Options for a picker whose values live on a server, not in a folder.

        Read from the property the server names, and kept on disk so the picker
        still offers something while that server is off. Returns
        [{"id", "name", ...}] - the remaining fields of each item come along,
        so a page can show more than the name if the server provides it.
        """
        # Locations are the instruments discovery actually found. The dashboard
        # used to offer a hardcoded list here that included an "incubator" this
        # lab does not have and three invented storage racks — every one of them
        # a dead end the operator could pick.
        if ui_hint == "location":
            return [
                {"id": inst.id, "name": inst.name}
                for inst in sorted(self._instruments.values(), key=lambda i: i.name)
            ]

        source = self._option_source(ui_hint)
        if source is None:
            configured = self._ui_dropdowns.get(ui_hint) or {}
            folder = configured.get("folder") if isinstance(configured, dict) else None
            return self._read_options_snapshot(folder) if folder else []

        instrument, declaration = source
        folder = declaration.get("folder") or ""
        prop_name = declaration["source_property"]
        prop = next((p for p in instrument.properties if p.id == prop_name), None)

        options: Optional[List[Dict[str, Any]]] = None
        if instrument.status == "online" and prop is not None:
            try:
                result = await self.get_property(instrument.id, prop.feature, prop_name)
                if result.success:
                    options = self._options_from_value(
                        result.data,
                        str(declaration.get("value_field") or "id"),
                        str(declaration.get("label_field") or "name"),
                    )
                else:
                    logger.warning("Reading %s.%s for '%s' failed: %s",
                                   instrument.name, prop_name, ui_hint, result.error)
            except Exception as exc:
                logger.warning("Reading %s.%s for '%s' failed: %s",
                               instrument.name, prop_name, ui_hint, exc)

        if options is None:
            options = self._read_options_snapshot(folder) if folder else []
        elif folder:
            self._write_options_snapshot(folder, options)

        self._dynamic_options_cache[ui_hint] = options
        return options

    @staticmethod
    def _options_from_value(value: Any, value_field: str, label_field: str) -> List[Dict[str, Any]]:
        """Turn a property value - a list of structures, or JSON of one - into options."""
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return []
        if isinstance(value, dict):
            value = next((v for v in value.values() if isinstance(v, list)), [])
        options = []
        for item in value or []:
            if isinstance(item, dict):
                ident = item.get(value_field)
                if ident in (None, ""):
                    continue
                options.append({**item, "id": str(ident),
                                "name": str(item.get(label_field) or ident)})
            elif isinstance(item, (str, int, float)):
                options.append({"id": str(item), "name": str(item)})
        return options

    async def _refresh_options_owned_by(self, instrument: "Instrument") -> List[str]:
        """Re-read every property-backed picker this instrument is the source of.

        Called after each command that succeeds on it, since the orchestrator
        cannot know which command changed the list - on the mobile robot it is
        RefreshTasks, and that name used to be written into this file.
        """
        refreshed = []
        hints = {e.get("hint") for e in instrument.catalog.library_types if e.get("hint")}
        hints.update(h for h, c in self._ui_dropdowns.items()
                     if isinstance(c, dict) and c.get("source_property"))
        for hint in sorted(h for h in hints if h):
            source = self._option_source(hint)
            if source is None or source[0] is not instrument:
                continue
            await self.fetch_dynamic_options(hint)
            refreshed.append(hint)
        return refreshed

    # ── Folders and results: what the servers declared ───────────────────────

    def ensure_declared_folders(self, instrument: "Instrument") -> List[Path]:
        """Create the Library and Results folders an instrument declares.

        The server creates them on its own machine when it starts; this covers
        the orchestrator's side, which for a server on another computer is the
        only side the web interface can see.
        """
        settings = get_settings(self.base_dir)
        created, refused = ensure_folders(
            instrument.catalog, self.base_dir,
            library_dir=settings.library_dir, results_dir=settings.results_dir,
        )
        for path in created:
            logger.info("Created %s, declared by %s", path, instrument.name)
        for folder in refused:
            logger.warning("%s declares folder %r outside Library/ or Results/; not created",
                           instrument.name, folder)
        return created

    #: Results subfolders that are LabOS's own bookkeeping, not instrument data.
    INTERNAL_RESULT_FOLDERS = frozenset({"runs", "__pycache__"})

    def list_result_folders(self) -> List[Dict[str, Any]]:
        """Every folder under Results/ an instrument writes into, and who said so.

        Declared folders come first, labelled by the instruments that declared
        them. Any other folder that exists is listed too, marked undeclared, so
        data an instrument wrote without declaring it is still visible rather
        than silently missing from the page.
        """
        results_dir = get_settings(self.base_dir).results_dir
        folders: Dict[str, Dict[str, Any]] = {}
        for instrument in sorted(self._instruments.values(), key=lambda i: i.name):
            for result in instrument.catalog.result_types:
                path = (self.base_dir / result.folder).resolve()
                try:
                    relative = path.relative_to(results_dir.resolve())
                except ValueError:
                    continue
                name = relative.as_posix()
                entry = folders.setdefault(name, {
                    "name": name,
                    "display_name": result.display_name,
                    "description": result.description,
                    "file_extensions": list(result.file_extensions),
                    "declared_by": [],
                    "declared": True,
                })
                if instrument.name not in entry["declared_by"]:
                    entry["declared_by"].append(instrument.name)
        if results_dir.is_dir():
            for child in sorted(results_dir.iterdir()):
                if (child.is_dir() and child.name not in folders
                        and child.name not in self.INTERNAL_RESULT_FOLDERS
                        and not child.name.startswith(".")):
                    folders[child.name] = {
                        "name": child.name, "display_name": child.name,
                        "description": "", "file_extensions": [],
                        "declared_by": [], "declared": False,
                    }
        for entry in folders.values():
            path = results_dir / entry["name"]
            entry["exists"] = path.is_dir()
            entry["file_count"] = (sum(1 for f in path.iterdir() if f.is_file())
                                   if path.is_dir() else 0)
        return list(folders.values())

    #                           DISCOVERY

    def _build_port_to_key(self) -> Tuple[Dict[int, str], Dict[int, str]]:
        """Build port → lab_config key and port → configured host maps for stable instrument IDs."""
        port_map = get_settings(self.base_dir).port_map()
        port_to_key = {port: spec.key for port, spec in port_map.items()}
        port_to_host = {port: spec.host for port, spec in port_map.items() if spec.host}
        return port_to_key, port_to_host

    def _server_to_instrument(
        self,
        server: PnPServer,
        port_to_key: Dict[int, str],
        port_to_host: Dict[int, str],
    ) -> Instrument:
        """Convert a PnPServer (with populated features) to an Instrument."""
        candidate_id = port_to_key.get(server.port)
        if candidate_id:
            # A configured port can be shared by a stale/foreign server (e.g. an
            # old instance left running on a different host after a redeploy).
            # Only claim the configured instrument ID if the announcing host
            # matches the configured host, or the host of whatever instance is
            # already registered under that ID — otherwise treat it as a
            # distinct instrument instead of clobbering the existing slot.
            configured_host = port_to_host.get(server.port)
            existing = self._instruments.get(candidate_id)
            existing_host = existing.host if existing else None
            host_ok = (
                not configured_host
                or configured_host in ("localhost", "127.0.0.1")
                or server.host == configured_host
                or (existing_host is not None and server.host == existing_host)
            )
            inst_id = candidate_id if host_ok else server.name.lower().replace(" ", "_")
        else:
            inst_id = server.name.lower().replace(" ", "_")

        # Build param→hint lookup from server's LabOSLibraryProvider catalog.
        # Takes priority over keyword inference in infer_ui_hint().
        #
        # Everything below reads the catalog with .get(). The catalog arrives
        # over the wire from another process - frequently one deployed on a
        # different machine and upgraded on a different schedule - so it is
        # untrusted input, not a local literal. A single entry missing a key
        # used to raise KeyError out of here, out of discover(), and out of the
        # execute route that calls discover() before every command: one stale
        # remote server made every command on every instrument in the lab fail
        # with the message 'folder'. A malformed entry now costs exactly the
        # dropdown it describes.
        catalog = getattr(server, "catalog", None)
        if not isinstance(catalog, ServerCatalog):
            # A server object built without discovery (tests, a hand-registered
            # server) may carry only the bare list of library types.
            raw_types = getattr(server, "library_catalog", None)
            catalog = parse_catalog(
                {"library_types": list(raw_types) if isinstance(raw_types, list) else []},
                source=str(server.name))

        param_hint_lookup: Dict[str, str] = {}
        for lib_type in catalog.library_types:
            hint = lib_type.get("hint")
            if not hint:
                logger.warning(
                    "%s declares a library type with no hint; ignoring it", server.name
                )
                continue
            for pname in lib_type.get("parameter_names", []):
                param_hint_lookup[pname] = hint

        # Merge catalog folder/extension into _ui_dropdowns for _get_options_for_hint().
        # Only adds new entries; static lab_config.yaml entries take precedence.
        for lib_type in catalog.library_types:
            hint = lib_type.get("hint")
            if not hint:
                continue
            # A server may declare the values a parameter accepts outright,
            # rather than pointing at a Library/ folder. That is how an
            # instrument offers a dropdown for something that is neither a file
            # nor deck state - without the orchestrator having to know what the
            # values mean, and without narrowing the SiLA2 type itself (a
            # Constrained/Set would reject values the server still accepts,
            # such as the empty string meaning "use your configured default").
            if lib_type.get("values") is not None:
                self._ui_dropdowns.setdefault(hint, {
                    "options": list(lib_type["values"]),
                    "option_labels": dict(lib_type.get("value_labels") or {}),
                })
                continue
            if hint in self._ui_dropdowns:
                continue
            if lib_type.get("json_keys_file"):
                self._ui_dropdowns[hint] = {"json_keys_file": lib_type["json_keys_file"]}
                continue
            if lib_type.get("source_property"):
                self._ui_dropdowns[hint] = {
                    k: lib_type[k] for k in ("source_property", "value_field", "label_field", "folder")
                    if lib_type.get(k)
                }
                continue
            if lib_type.get("dynamic"):
                continue
            folder = lib_type.get("folder")
            if not folder:
                # Neither a folder, nor a declared value set, nor dynamic:
                # there is nothing to offer. The parameter still works, it is
                # just a free-text field.
                logger.warning(
                    "%s declares library type '%s' with no folder, no values and "
                    "not dynamic - no dropdown for it", server.name, hint
                )
                continue
            self._ui_dropdowns[hint] = {
                "folder": folder,
                "file_extension": lib_type.get("file_extension", ""),
            }

        commands = []
        for feature_id, cmd_id, cmd in server.get_all_commands():
            # SiLAService's GetFeatureDefinition/SetServerName are on every
            # server; they are how the orchestrator reads an instrument, not
            # something an operator would ever put in a workflow.
            if is_infrastructure_feature(feature_id):
                continue
            params = []
            for p in cmd.parameters:
                ui_hint = param_hint_lookup.get(p.identifier) or p.infer_ui_hint()
                options = p.constraints if p.constraints else []
                detail: List[Dict[str, str]] = []
                if not options and ui_hint:
                    detail = self._option_pairs_for_hint(ui_hint)
                    options = ([d["value"] for d in detail] if detail
                               else self._get_options_for_hint(ui_hint))
                params.append(CommandParameter(
                    name=p.identifier,
                    display_name=p.display_name,
                    description=p.description,
                    data_type=p.data_type,
                    required=p.required,
                    ui_hint=ui_hint,
                    options=options,
                    options_detail=detail,
                ))
            commands.append(Command(
                id=cmd_id,
                name=cmd.display_name or cmd_id,
                description=cmd.description,
                feature=feature_id,
                important=True,
                observable=cmd.observable,
                parameters=params,
            ))
        # Properties: readable state the server publishes. Feature identifiers
        # arrive fully qualified (org.silastandard/.../WorkflowAPI/v1); the
        # client addresses a property by the bare feature name.
        properties = []
        for feature in server.features:
            fid = feature.identifier
            if is_infrastructure_feature(fid):
                continue
            feature_name = _feature_name(fid)
            for prop in feature.properties:
                properties.append(Property(
                    id=prop.identifier,
                    name=prop.display_name or prop.identifier,
                    description=prop.description,
                    feature=feature_name,
                    data_type=prop.data_type,
                    observable=prop.observable,
                ))

        return Instrument(
            id=inst_id,
            name=server.name,
            type=server.server_type or "instrument",
            status="online" if server.server_online else "offline",
            host=server.host,
            port=server.port,
            commands=commands,
            properties=properties,
            display=dict(catalog.display),
            results=[r.to_dict() for r in catalog.result_types],
            catalog=catalog,
            _server=server,
        )

    def _on_mdns_server_discovered(self, server: PnPServer):
        """Sync callback fired by the mDNS listener when a new server appears."""
        try:
            asyncio.get_event_loop().create_task(self._handle_new_server_async(server))
        except RuntimeError:
            pass  # No running loop (shouldn't happen during server operation)

    async def _handle_new_server_async(self, server: PnPServer):
        """Query metadata for a freshly-announced mDNS server and register it."""
        if self._discovery is None:
            return
        loop = asyncio.get_running_loop()
        # Fetch SiLAService metadata + feature definitions in thread
        await loop.run_in_executor(
            None, self._discovery._try_sila2_client_query, server.host, server.port, server
        )
        port_to_key, port_to_host = self._build_port_to_key()
        instrument = self._server_to_instrument(server, port_to_key, port_to_host)
        self._instruments[instrument.id] = instrument
        self.ensure_declared_folders(instrument)
        logger.info(f"mDNS push: registered new instrument '{instrument.name}'")
        if self._on_discovery_update:
            self._on_discovery_update(list(self._instruments.values()))

    async def discover(self, timeout: float = 2.0) -> List[Instrument]:
        """
        Discover all available SiLA2 instruments.

        On the first call (or after the cache TTL expires) this runs a full
        bootstrap + mDNS scan and then starts a continuous mDNS listener so
        that new instruments are registered immediately when they come online
        (push, not polling).

        Returns:
            List of discovered instruments
        """
        now = time.monotonic()
        if self._instruments and (now - self._last_discovery_time) < self._discovery_cache_ttl:
            return list(self._instruments.values())

        # Stop old continuous listener before resetting the discovery instance
        if self._discovery:
            await self._discovery.stop_continuous_discovery()

        self._discovery = PnPDiscovery(self.base_dir)
        self._discovery.set_discovery_callback(self._on_mdns_server_discovered)
        await self._discovery.discover_all(timeout=timeout)

        port_to_key, port_to_host = self._build_port_to_key()
        # Per-server, not one comprehension over all of them: a server whose
        # metadata cannot be turned into an Instrument is one missing
        # instrument, not an empty lab. Building the dict in a single
        # comprehension meant the first raise discarded every instrument
        # already built and propagated out of discover() - and the command
        # route calls discover() before every command, so one bad server took
        # the whole lab offline.
        instruments: Dict[str, Instrument] = {}
        for server in self._discovery.list_servers():
            try:
                inst = self._server_to_instrument(server, port_to_key, port_to_host)
            except Exception as exc:
                logger.error(
                    "Skipping %s at %s:%s - its metadata could not be read: %s",
                    server.name, server.host, server.port, exc, exc_info=True
                )
                continue
            instruments[inst.id] = inst
            try:
                self.ensure_declared_folders(inst)
            except Exception as exc:
                logger.warning("Could not create folders declared by %s: %s", inst.name, exc)
        self._instruments = instruments

        self._last_discovery_time = time.monotonic()

        # Start continuous mDNS listener — new servers now arrive via push callback
        await self._discovery.start_continuous_discovery()

        if self._on_discovery_update:
            self._on_discovery_update(list(self._instruments.values()))

        return list(self._instruments.values())
    
    def list_instruments(self) -> List[Instrument]:
        """Get list of discovered instruments."""
        return list(self._instruments.values())
    
    def resolve_instrument(self, name: str) -> "Resolution":
        """Resolve a written instrument name to a discovered Instrument.

        The single matcher for the whole stack — see src/resolver.py for why
        the old per-call-site substring rules had to go. Returns a Resolution:
        truthy when it matched, and carrying a ready-made `error` naming the
        available instruments (or the colliding ones) when it did not.
        """
        return resolve(
            name,
            [(inst.id, inst.name, inst) for inst in self._instruments.values()],
        )

    def find_instrument(self, name: str) -> Optional[Instrument]:
        """resolve_instrument() when the caller only wants the instrument."""
        return self.resolve_instrument(name).target

    def instruments_with_command(self, command_id: str) -> List[Instrument]:
        """Every discovered instrument exposing `command_id`.

        Capability lookup, for code that wants "whoever can refill tips"
        rather than "the instrument named opentrons".
        """
        return [
            inst for inst in self._instruments.values()
            if any(cmd.id == command_id for cmd in inst.commands)
        ]

    def role_of(self, instrument_id: str, command_id: str):
        """The plate-traceability role a server declared for one of its commands."""
        instrument = self.get_instrument(instrument_id) or self.find_instrument(instrument_id)
        return instrument.catalog.role_of(command_id) if instrument else None

    def get_instrument(self, instrument_id: str) -> Optional[Instrument]:
        """Get instrument by ID."""
        # Normalize ID
        normalized = instrument_id.lower().replace("-", "_")
        
        # Try exact match first
        if normalized in self._instruments:
            return self._instruments[normalized]
        if instrument_id in self._instruments:
            return self._instruments[instrument_id]
        
        # Try without underscores (handles OpentronsFlex vs opentrons_flex)
        no_underscore = normalized.replace("_", "")
        for inst_id, inst in self._instruments.items():
            if inst_id.replace("_", "") == no_underscore:
                return inst
        
        return None
    
    #                           PROPERTY ACCESS

    async def get_property(
        self,
        instrument_id: str,
        feature: str,
        property_name: str,
    ) -> ExecutionResult:
        """Read a SiLA2 (non-observable) property on an instrument."""
        instrument = self.get_instrument(instrument_id)
        if not instrument:
            return ExecutionResult(success=False, error=f"Instrument '{instrument_id}' not found")

        if instrument.status != "online":
            return ExecutionResult(success=False, error=f"Instrument '{instrument.name}' is offline")

        server = instrument._server
        if not server:
            return ExecutionResult(success=False, error=f"No server reference for {instrument.name}")

        if self._client is None:
            self._client = PnPClient()
        await self._client.connect(server)

        result = await self._client.get_property(server, feature, property_name)
        if not result.success:
            return ExecutionResult(success=False, error=result.error or "Unknown error")
        return ExecutionResult(success=True, data=result.data)

    #                           COMMAND EXECUTION

    async def execute_command(
        self, 
        instrument_id: str, 
        command_id: str, 
        parameters: Dict[str, Any]
    ) -> ExecutionResult:
        """
        Execute a command on an instrument.
        
        Args:
            instrument_id: ID of the instrument
            command_id: ID of the command to execute
            parameters: Command parameters
            
        Returns:
            ExecutionResult with success/failure info
        """
        # Get instrument
        instrument = self.get_instrument(instrument_id)
        if not instrument:
            return ExecutionResult(
                success=False, 
                error=f"Instrument '{instrument_id}' not found"
            )
        
        # Check if online
        if instrument.status != "online":
            return ExecutionResult(
                success=False,
                error=f"Instrument '{instrument.name}' is offline"
            )
        
        # Notify start
        if self._on_command_start:
            self._on_command_start(instrument_id, command_id, parameters)
        
        self._log("info", f"Executing {command_id} on {instrument.name}...")
        
        try:
            # Use PnPClient for command execution (Strategy 0: SilaClient, Strategy 1: dynamic stubs)
            if self._client is None:
                self._client = PnPClient()
            
            # Get the underlying PnPServer for execution
            server = instrument._server
            if not server:
                return ExecutionResult(
                    success=False,
                    error=f"No server reference for {instrument.name}"
                )
            
            # Connect if needed
            await self._client.connect(server)
            
            # Find the command to get feature info
            cmd = None
            for c in instrument.commands:
                if c.id == command_id:
                    cmd = c
                    break
            
            if not cmd:
                return ExecutionResult(
                    success=False,
                    error=f"Command '{command_id}' not found on {instrument.name}"
                )

            # Whatever sent these - a form, a saved workflow, a curl - carried
            # them as text. SiLA2 wants the declared types.
            try:
                parameters = coerce_parameters(parameters, cmd.parameters)
            except ParameterTypeError as exc:
                self._log("error", f"{command_id}: {exc}")
                return ExecutionResult(success=False, error=str(exc))
            
            # Execute via PnPClient (Strategy 0: sila2 SilaClient).
            # Observable SiLA2 commands (ExecuteRecipe, RunMeasurement, ...)
            # are the ones genuinely long-running by design and already have
            # their own server-side real-status tracking (e.g.
            # robot_client.wait_for_run_completion polling the actual robot
            # run state) - a client-side cutoff on these doesn't catch a real
            # hang, it just gives up on a command that's still working and
            # reports a false failure. Confirmed on real hardware: an
            # ExecuteRecipe genuinely still running past 600s got reported
            # "failed" here, which then skipped the plate-tracking DB write
            # gated on result.success downstream (webapp/routes/instruments.py)
            # even though the run finished successfully moments later.
            # Non-observable (unary) commands are expected to return quickly
            # by SiLA2 design, so they keep the bounded default - a genuine
            # hang there is exactly the anomaly the timeout exists to catch.
            result = await self._client.execute(
                server,
                command_id,
                parameters,
                feature=cmd.feature,
                timeout=None if cmd.observable else self._command_timeout()
            )
            
            # Check result
            if not result.success:
                self._log("error", f"{command_id} failed: {result.error}")
                return ExecutionResult(
                    success=False,
                    error=result.error or "Unknown error"
                )
            
            # A command may change a list a picker offers (the mobile robot's
            # RefreshTasks changes its task list). Which one is not knowable
            # from here, so every list this instrument is the source of is
            # re-read after any command that succeeds on it.
            options_changed: List[str] = []
            try:
                options_changed = await self._refresh_options_owned_by(instrument)
            except Exception as e:
                self._log("warning", f"Could not refresh option lists from {instrument.name}: {e}")

            # Notify complete
            if self._on_command_complete:
                self._on_command_complete(instrument_id, command_id, True, result.data)
            
            self._log("success", f"{command_id} completed successfully")
            
            return ExecutionResult(
                success=True,
                message=f"{command_id} completed",
                data=result.data,
                options_changed=options_changed,
            )
            
        except Exception as e:
            error_msg = str(e)
            
            # Notify complete (with error)
            if self._on_command_complete:
                self._on_command_complete(instrument_id, command_id, False, error_msg)
            
            self._log("error", f"{command_id} failed: {error_msg}")
            
            return ExecutionResult(
                success=False,
                error=error_msg
            )
    
    #                           FILE MANAGEMENT
    
    def list_files(self, file_type: str) -> List[str]:
        """
        List available files of a given type.
        
        Args:
            file_type: One of "recipes", "analyses", "hal", "liquidclasses", "workflows", "protocols"
            
        Returns:
            List of filenames
        """
        type_map = {
            "recipes": ("Recipes", "*.json"),
            "analyses": ("Analysis", "*.mdfx"),
            "protocols": ("Analysis", "*.mdfx"),  # Alias
            "hal": ("HardwareConfig", "*.json"),
            "hal_config": ("HardwareConfig", "*.json"),  # Alias
            "liquidclasses": ("LiquidClasses", "*.json"),
            "liquid_class": ("LiquidClasses", "*.json"),  # Alias
            "workflows": ("Workflows", "*.workflow.json"),
        }
        
        if file_type not in type_map:
            logger.warning(f"Unknown file type: {file_type}")
            return []
        
        folder, pattern = type_map[file_type]
        target_dir = self.library_dir / folder
        
        if not target_dir.exists():
            return []
        
        return [f.name for f in target_dir.glob(pattern)]
    
    def get_file_path(self, file_type: str, filename: str) -> Optional[Path]:
        """Get full path to a file."""
        type_map = {
            "recipes": "Recipes",
            "analyses": "Analysis",
            "protocols": "Analysis",
            "hal": "HardwareConfig",
            "hal_config": "HardwareConfig",
            "liquidclasses": "LiquidClasses",
            "liquid_class": "LiquidClasses",
            "workflows": "Workflows",
        }
        
        if file_type not in type_map:
            return None
        
        folder = type_map[file_type]
        path = self.library_dir / folder / filename
        
        return path if path.exists() else None
    
    def load_file(self, file_type: str, filename: str) -> Optional[Dict]:
        """Load and parse a JSON file."""
        path = self.get_file_path(file_type, filename)
        
        if not path:
            return None
        
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading {filename}: {e}")
            return None
    
    #                           WORKFLOW EXECUTION
    
    async def execute_workflow(
        self,
        workflow_name: str,
        on_step_start: Optional[Callable] = None,
        on_step_complete: Optional[Callable] = None,
        on_intervention_needed: Optional[Callable] = None,
        parallel: Optional[bool] = None,
    ) -> ExecutionResult:
        """
        Execute a workflow using PnPWorkflowExecutor with retry and intervention support.
        
        Args:
            workflow_name: Name of the workflow file
            on_step_start: Callback when a step starts
            on_step_complete: Callback when a step completes
            on_intervention_needed: Callback when human intervention is needed
            
        Returns:
            ExecutionResult
        """
        # Load workflow
        workflow = self.load_file("workflows", workflow_name)
        
        if not workflow:
            return ExecutionResult(
                success=False,
                error=f"Workflow '{workflow_name}' not found"
            )
        
        steps = workflow.get("Steps", [])
        total_steps = len(steps)
        
        self._log("info", f"Starting workflow: {workflow.get('WorkflowName', workflow_name)}")
        
        # Initialize workflow executor if not already done
        if self._workflow_executor is None:
            registry = PnPRegistry()
            for inst_id, inst in self._instruments.items():
                if inst._server:
                    registry.register(inst_id, inst._server)
            self._workflow_executor = PnPWorkflowExecutor(registry)

        # Set intervention callback if available
        if self._intervention_callback:
            self._workflow_executor.set_intervention_callback(self._intervention_callback)

        # Build Workflow object directly from the loaded file dict
        workflow_obj = Workflow.from_dict(workflow)

        # Progress callback bridges WorkflowProgress to the caller's step callbacks
        def on_step_progress(progress: WorkflowProgress):
            if on_step_start and progress.step_status == StepStatus.RUNNING:
                on_step_start(progress.current_step, total_steps, {
                    "instrument": progress.step_instrument,
                    "action": progress.step_action
                })
            if on_step_complete and progress.step_status in (StepStatus.SUCCESS, StepStatus.FAILED, StepStatus.SKIPPED):
                on_step_complete(progress.current_step, total_steps, {
                    "success": progress.step_status == StepStatus.SUCCESS,
                    "skipped": progress.step_status == StepStatus.SKIPPED,
                    "error": progress.message if progress.step_status == StepStatus.FAILED else None
                })
            if self._on_workflow_step:
                self._on_workflow_step(
                    progress.current_step, total_steps,
                    progress.step_status.value,
                    progress.step_instrument,
                    progress.step_action
                )

        self._workflow_executor.add_progress_callback(on_step_progress)

        # Determine parallel flag: caller overrides config; config overrides default False
        if parallel is None:
            parallel = get_settings(self.base_dir).workflow.parallel_execution

        try:
            result = await self._workflow_executor.execute(workflow_obj, validate=False, parallel=parallel)

            self._workflow_executor.remove_progress_callback(on_step_progress)

            if result.success:
                self._log("success", f"Workflow completed: {result.steps_completed}/{total_steps} steps")
                return ExecutionResult(
                    success=True,
                    message=f"Workflow completed ({result.steps_completed} steps in {result.duration_seconds:.1f}s)"
                )
            else:
                return ExecutionResult(
                    success=False,
                    error="; ".join(result.errors) if result.errors else "Workflow failed"
                )
        except Exception as e:
            self._workflow_executor.remove_progress_callback(on_step_progress)
            self._log("error", f"Workflow execution error: {e}")
            return ExecutionResult(success=False, error=str(e))
    
    #                           CALLBACKS & LOGGING
    
    def set_callbacks(
        self,
        on_discovery_update: Optional[Callable] = None,
        on_command_start: Optional[Callable] = None,
        on_command_complete: Optional[Callable] = None,
        on_log: Optional[Callable] = None,
        on_workflow_step: Optional[Callable] = None
    ):
        """Set UI callbacks for status updates."""
        self._on_discovery_update = on_discovery_update
        self._on_command_start = on_command_start
        self._on_command_complete = on_command_complete
        self._on_log = on_log
        self._on_workflow_step = on_workflow_step
    
    def set_intervention_callback(self, callback: Optional[InterventionCallback]):
        """
        Set the human intervention callback for workflow execution.
        
        The callback is invoked when a workflow step fails after all retries.
        It should return an InterventionAction (RETRY, SKIP, or ABORT).
        
        For CLI: Implement interactive prompt
        For WebApp: Implement WebSocket-based UI notification
        
        Args:
            callback: Async function taking InterventionRequest, returning InterventionAction
        """
        self._intervention_callback = callback
    
    def _log(self, level: str, message: str):
        """Log a message and notify callback."""
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        elif level == "success":
            logger.info(f"✓ {message}")
        else:
            logger.info(message)
        
        if self._on_log:
            self._on_log(level, message)
    
    #                           UTILITY METHODS
    
    def get_all_commands_dict(self) -> Dict[str, Any]:
        """
        Get all instruments and commands in API format.
        
        This is the same format used by /api/instruments/commands.
        """
        instruments = {}
        
        for inst_id, instrument in self._instruments.items():
            instruments[inst_id] = instrument.to_dict()
        
        return {
            "source": "lab_core",
            "instruments": instruments
        }


#                           SINGLETON INSTANCE

_core_instance: Optional[LabCore] = None

def get_lab_core(base_dir: Optional[Path] = None) -> LabCore:
    """
    Get the singleton LabCore instance.
    
    This ensures CLI and WebApp share the same state.
    """
    global _core_instance
    
    if _core_instance is None:
        _core_instance = LabCore(base_dir)
    
    return _core_instance


#                           TEST / DEMO

async def _demo():
    """Demo usage of LabCore."""
    core = LabCore()
    instruments = await core.discover()
    logger.info(f"Found {len(instruments)} instruments:")
    for inst in instruments:
        logger.info(f"  {inst.name} ({inst.status}) — {len(inst.commands)} commands")
    logger.info(f"Recipes: {core.list_files('recipes')}")
    logger.info(f"Analyses: {core.list_files('analyses')}")
    logger.info(f"HAL configs: {core.list_files('hal')}")


if __name__ == "__main__":
    asyncio.run(_demo())
