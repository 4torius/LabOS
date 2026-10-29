"""What a server says about itself beyond its commands.

A SiLA2 feature definition says which commands an instrument accepts. It does
not say where the files it reads live, where the data it writes goes, what it
should look like in the workflow builder, which parameters may be left out, or
which of its commands prepare or measure a plate. Without those, every one of
them had to be written into the orchestrator by instrument name - `"tecan" in
name`, `"opentrons" in name`, `"manual" in name` - so adding an instrument meant
editing the core, which is exactly what the architecture promises it does not.

Each server now declares them in its `library_catalog.json`, served through the
`LabOSLibraryProvider.LibraryCatalog` property, next to the library types it
already declared. This module is the one reader of that document:

    {
      "display":            {"icon": "bi-droplet-fill", "color": "primary"},
      "library_types":      [...],   # inputs: files in Library/ -> pickers
      "result_types":       [...],   # outputs: folders in Results/ -> Results page
      "parameter_defaults": {"Command": {"Parameter": value}},
      "traceability":       {"Command": {"role": "prepares_plate" | "measures_plate", ...}}
    }

Every section is optional, so a catalog written before this module existed
(`{"library_types": [...]}`) still reads exactly as it did.

The catalog arrives over the network, frequently from another machine running
another version, so it is untrusted input. A malformed entry costs that entry
and is reported in `problems`; it never raises out of here. Folders are
confined to the Library and Results trees, so a server cannot make the
orchestrator create directories anywhere else on its disk, and the icon and
colour are checked against a fixed shape because pages render them as markup.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

#: Roles a command can play in a plate's history.
PREPARES_PLATE = "prepares_plate"
MEASURES_PLATE = "measures_plate"
TRACEABILITY_ROLES = frozenset({PREPARES_PLATE, MEASURES_PLATE})

#: Bootstrap contextual colours the pages know how to render.
DISPLAY_COLORS = frozenset({
    "primary", "secondary", "success", "danger", "warning", "info", "dark",
})
_ICON_RE = re.compile(r"^bi-[a-z0-9-]{1,40}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Features that are infrastructure, not instrument capability. Every SiLA2
#: server implements SiLAService, and every LabOS server the catalog provider;
#: neither is something an operator should find in a toolbox.
INFRASTRUCTURE_FEATURES = frozenset({"SiLAService", "LabOSLibraryProvider"})


def feature_name(identifier: str) -> str:
    """Bare feature name from a fully qualified SiLA2 feature identifier.

    `org.silastandard/core/SiLAService/v1` -> `SiLAService`; a bare name is
    returned unchanged.
    """
    parts = [p for p in (identifier or "").split("/") if p]
    if len(parts) >= 2 and re.fullmatch(r"v\d+", parts[-1]):
        return parts[-2]
    return parts[-1] if parts else ""


def is_infrastructure_feature(identifier: str) -> bool:
    return feature_name(identifier) in INFRASTRUCTURE_FEATURES


@dataclass(frozen=True)
class ResultType:
    """A folder under Results/ that an instrument writes its data into."""

    key: str
    display_name: str
    folder: str
    file_extensions: Tuple[str, ...] = ()
    description: str = ""

    @property
    def name(self) -> str:
        """The folder's own name, which is how the Results page addresses it."""
        return PurePosixPath(self.folder).name

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "folder": self.folder,
            "name": self.name,
            "file_extensions": list(self.file_extensions),
            "description": self.description,
        }


@dataclass(frozen=True)
class Traceability:
    """What a command does to a plate's record."""

    role: str
    plate_parameter: str = ""
    recipe_parameter: str = ""
    protocol_parameter: str = ""


@dataclass
class ServerCatalog:
    library_types: List[Dict[str, Any]] = field(default_factory=list)
    result_types: List[ResultType] = field(default_factory=list)
    display: Dict[str, str] = field(default_factory=dict)
    parameter_defaults: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    traceability: Dict[str, Traceability] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)

    def defaults_for(self, command: str) -> Dict[str, Any]:
        return dict(self.parameter_defaults.get(command, {}))

    def role_of(self, command: str) -> Optional[Traceability]:
        return self.traceability.get(command)

    def hint_for_parameter(self, parameter: str) -> str:
        for entry in self.library_types:
            if parameter in (entry.get("parameter_names") or []):
                return str(entry.get("hint") or "")
        return ""

    def declared_folders(self) -> List[Tuple[str, str]]:
        """(kind, folder) for every concrete folder this server declares."""
        folders: List[Tuple[str, str]] = []
        for entry in self.library_types:
            folder = entry.get("folder")
            if folder and not entry.get("values") and not entry.get("json_keys_file"):
                folders.append(("library", folder))
        for result in self.result_types:
            folders.append(("results", result.folder))
        return folders

    def to_dict(self) -> Dict[str, Any]:
        return {
            "display": dict(self.display),
            "library_types": list(self.library_types),
            "result_types": [r.to_dict() for r in self.result_types],
            "parameter_defaults": {k: dict(v) for k, v in self.parameter_defaults.items()},
            "traceability": {
                k: {"role": t.role, "plate_parameter": t.plate_parameter,
                    "recipe_parameter": t.recipe_parameter,
                    "protocol_parameter": t.protocol_parameter}
                for k, t in self.traceability.items()
            },
        }


def _normal_folder(value: Any) -> Optional[str]:
    """A relative, forward-slashed folder with no way out of its tree."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    text = text.rstrip("/")
    path = PurePosixPath(text)
    if any(part in ("..", ".") for part in path.parts):
        return None
    return str(path)


def parse_catalog(raw: Union[str, bytes, Mapping[str, Any], List[Any], None],
                  source: str = "") -> ServerCatalog:
    """Read a catalog document. Never raises; problems are collected."""
    catalog = ServerCatalog()
    label = f"{source}: " if source else ""

    if raw is None:
        return catalog
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            catalog.problems.append(f"{label}catalog is not valid JSON ({exc})")
            return catalog
    if isinstance(raw, list):
        # The pre-catalog shape: a bare list of library types.
        raw = {"library_types": raw}
    if not isinstance(raw, Mapping):
        catalog.problems.append(f"{label}catalog must be a JSON object")
        return catalog

    # ── library types: kept as dicts, the shape LabCore already consumes ────
    for index, entry in enumerate(raw.get("library_types") or []):
        if not isinstance(entry, Mapping):
            catalog.problems.append(f"{label}library_types[{index}] is not an object")
            continue
        if not entry.get("hint"):
            catalog.problems.append(f"{label}library_types[{index}] has no hint")
            continue
        clean = dict(entry)
        if clean.get("folder"):
            folder = _normal_folder(clean["folder"])
            if folder is None:
                catalog.problems.append(
                    f"{label}library type '{entry['hint']}' has an unusable folder "
                    f"{entry['folder']!r} (must be relative, without '..')")
                clean["folder"] = ""
            else:
                clean["folder"] = folder
        if not isinstance(clean.get("parameter_names") or [], list):
            catalog.problems.append(
                f"{label}library type '{entry['hint']}' parameter_names is not a list")
            clean["parameter_names"] = []
        catalog.library_types.append(clean)

    # ── result types ─────────────────────────────────────────────────────────
    seen_keys = set()
    for index, entry in enumerate(raw.get("result_types") or []):
        if not isinstance(entry, Mapping):
            catalog.problems.append(f"{label}result_types[{index}] is not an object")
            continue
        folder = _normal_folder(entry.get("folder"))
        if folder is None:
            catalog.problems.append(
                f"{label}result_types[{index}] needs a relative folder, got {entry.get('folder')!r}")
            continue
        key = str(entry.get("key") or PurePosixPath(folder).name)
        if not _KEY_RE.match(key) or key in seen_keys:
            catalog.problems.append(f"{label}result_types[{index}] has an unusable or repeated key {key!r}")
            continue
        seen_keys.add(key)
        extensions = entry.get("file_extensions") or []
        if isinstance(extensions, str):
            extensions = [extensions]
        extensions = tuple(
            (e if e.startswith(".") else f".{e}").lower()
            for e in extensions if isinstance(e, str) and e.strip()
        )
        catalog.result_types.append(ResultType(
            key=key,
            display_name=str(entry.get("display_name") or PurePosixPath(folder).name),
            folder=folder,
            file_extensions=extensions,
            description=str(entry.get("description") or ""),
        ))

    # ── display ──────────────────────────────────────────────────────────────
    display = raw.get("display") or {}
    if isinstance(display, Mapping):
        icon, color = display.get("icon"), display.get("color")
        if icon is not None:
            if isinstance(icon, str) and _ICON_RE.match(icon):
                catalog.display["icon"] = icon
            else:
                catalog.problems.append(f"{label}display.icon {icon!r} is not a Bootstrap icon name")
        if color is not None:
            if color in DISPLAY_COLORS:
                catalog.display["color"] = color
            else:
                catalog.problems.append(
                    f"{label}display.color {color!r} is not one of {sorted(DISPLAY_COLORS)}")
    else:
        catalog.problems.append(f"{label}display is not an object")

    # ── parameter defaults ───────────────────────────────────────────────────
    defaults = raw.get("parameter_defaults") or {}
    if isinstance(defaults, Mapping):
        for command, values in defaults.items():
            if isinstance(values, Mapping):
                catalog.parameter_defaults[str(command)] = {
                    str(k): v for k, v in values.items()
                    if isinstance(v, (str, int, float, bool))
                }
            else:
                catalog.problems.append(f"{label}parameter_defaults.{command} is not an object")
    else:
        catalog.problems.append(f"{label}parameter_defaults is not an object")

    # ── traceability ─────────────────────────────────────────────────────────
    trace = raw.get("traceability") or {}
    if isinstance(trace, Mapping):
        for command, spec in trace.items():
            if not isinstance(spec, Mapping) or spec.get("role") not in TRACEABILITY_ROLES:
                catalog.problems.append(
                    f"{label}traceability.{command} needs a role in {sorted(TRACEABILITY_ROLES)}")
                continue
            catalog.traceability[str(command)] = Traceability(
                role=spec["role"],
                plate_parameter=str(spec.get("plate_parameter") or ""),
                recipe_parameter=str(spec.get("recipe_parameter") or ""),
                protocol_parameter=str(spec.get("protocol_parameter") or ""),
            )
    else:
        catalog.problems.append(f"{label}traceability is not an object")

    return catalog


def resolve_folder(root: Path, folder: str, allowed: Tuple[Path, ...]) -> Optional[Path]:
    """Absolute path of a declared folder, or None if it leaves the allowed trees."""
    normal = _normal_folder(folder)
    if normal is None:
        return None
    target = (root / normal).resolve()
    for base in allowed:
        try:
            target.relative_to(base.resolve())
            return target
        except ValueError:
            continue
    return None


def ensure_folders(catalog: ServerCatalog, root: Path, *, library_dir: Path,
                   results_dir: Path) -> Tuple[List[Path], List[str]]:
    """Create every folder a catalog declares. Returns (created, refused)."""
    created: List[Path] = []
    refused: List[str] = []
    for kind, folder in catalog.declared_folders():
        allowed = (library_dir,) if kind == "library" else (results_dir,)
        target = resolve_folder(root, folder, allowed)
        if target is None:
            refused.append(folder)
            continue
        if not target.is_dir():
            try:
                target.mkdir(parents=True, exist_ok=True)
                created.append(target)
            except OSError:
                refused.append(folder)
    return created, refused


__all__ = [
    "DISPLAY_COLORS", "INFRASTRUCTURE_FEATURES", "MEASURES_PLATE", "PREPARES_PLATE",
    "ResultType", "ServerCatalog", "Traceability", "ensure_folders", "feature_name",
    "is_infrastructure_feature", "parse_catalog", "resolve_folder",
]
