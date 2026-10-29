"""Where the values a parameter accepts come from, and who gets to say so.

A workflow step's `RecipeName` should offer the recipes, `PlateType` the plate
definitions this lab owns, `TaskName` the tasks the robot has been taught. None
of that is in a SiLA2 feature definition, so somebody has to declare it.

The rule is the same one `resources.py` arrives at, applied to a different kind
of fact: **in the server what the server can know, in `lab_config.yaml` what no
server can speak for.** Three layers, in order of precedence:

1. **The server**, in its `library_catalog.json` `library_types`. A recipe is
   the Opentrons' business and an analysis protocol is the Tecan's. Zero
   configuration, and the reason adding an instrument adds its pickers without
   anybody touching the orchestrator.
2. **`lab_config.yaml` `library_types.declare.<server>`**, for a server that
   *cannot* declare — a vendor SiLA2 server knows nothing of this lab, and the
   mobile robot's catalog is a file inside a container whose source is not in
   this repository.
3. **`lab_config.yaml` `ui_dropdowns`**, for hints that belong to no server at
   all. This is not a fallback and it is worth being clear about: `plate`,
   `tiprack` and `pipette` are properties of *the lab's inventory*. The
   Opentrons does not own the list of plate definitions this lab possesses, so
   the config is their rightful home rather than a workaround, and moving them
   into a server would be the mistake.

What this module replaced was four `if ui_hint == "..."` branches in
`lab_core._get_options_for_hint` with the folder walks written out in Python.
They were instrument-agnostic, so they did not break plug-and-play the way a
`"tecan" in name` check would — but they were code where the architecture
promises data, and each sat alongside a `ui_dropdowns` entry for the same hint:
two sources that could disagree, which is the shape `provides` was introduced
to kill in the resource model.

Hints that are *computed* rather than declared stay in code, and there are only
two: deck contents (read from the active hardware config) and `location`
(the discovered instruments). Neither is a list of files, so neither is data.

Like `catalog.py`, declarations arriving from a server are untrusted input: a
malformed entry costs that entry and is reported in `problems`, and nothing
raises out of here.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Trees a declaration may read from. Anything else is refused, on both sides,
#: so a server cannot make the orchestrator read arbitrary paths on its disk.
ALLOWED_ROOTS = ("Library", "Results")

_HINT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _safe_relative(value: Any) -> Optional[str]:
    """A relative, forward-slashed path inside one of ALLOWED_ROOTS."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("\\", "/").lstrip("./")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    path = PurePosixPath(text.rstrip("/"))
    if any(part == ".." for part in path.parts):
        return None
    if not path.parts or path.parts[0] not in ALLOWED_ROOTS:
        return None
    return str(path)


def _dig(data: Mapping[str, Any], dotted: str) -> Any:
    """`parameters.loadName` out of a nested dict, or None."""
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        logger.debug("options: %s is unreadable (%s)", path, exc)
        return None


# ── One source of values ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class OptionSource:
    """One place a hint's values come from. A hint may have several.

    Exactly one of the shapes below is used, in this order of checking:

    * `values`        — a fixed list, declared outright
    * `glob` + `fields` — one value per matching JSON file, read out of it
                          (`fields` are tried in order, first hit wins)
    * `file` + `list_key`  — a list inside one JSON file
    * `file` + `items_key` — a list of objects inside one JSON file, each
                             rendered through `key_template` (e.g. `{channel}/{model}`)
    * `json_keys_file`  — the top-level keys of one JSON file
    * `folder` (+ `extension`) — the file stems in a folder
    """

    values: Tuple[str, ...] = ()
    glob: str = ""
    fields: Tuple[str, ...] = ()
    file: str = ""
    list_key: str = ""
    items_key: str = ""
    key_template: str = ""
    json_keys_file: str = ""
    folder: str = ""
    extension: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {k: (list(v) if isinstance(v, tuple) else v)
                for k, v in self.__dict__.items() if v}

    def resolve(self, base_dir: Path) -> List[str]:
        """The values this source currently offers. Never raises."""
        if self.values:
            return list(self.values)

        if self.glob:
            found: List[str] = []
            for path in sorted(base_dir.glob(self.glob)):
                data = _read_json(path)
                if not isinstance(data, Mapping):
                    continue
                for dotted in self.fields or ("id",):
                    value = _dig(data, dotted)
                    if isinstance(value, str) and value:
                        found.append(value)
                        break
            return found

        if self.file:
            data = _read_json(base_dir / self.file)
            if not isinstance(data, Mapping):
                return []
            if self.list_key:
                raw = data.get(self.list_key) or []
                return [v for v in raw if isinstance(v, str) and v]
            if self.items_key:
                out: List[str] = []
                for item in data.get(self.items_key) or []:
                    if not isinstance(item, Mapping):
                        continue
                    try:
                        rendered = self.key_template.format(**item)
                    except (KeyError, IndexError, ValueError):
                        continue
                    if rendered and rendered not in out:
                        out.append(rendered)
                return out
            return []

        if self.json_keys_file:
            data = _read_json(base_dir / self.json_keys_file)
            return sorted(data) if isinstance(data, Mapping) else []

        if self.folder:
            folder = base_dir / self.folder
            if not folder.is_dir():
                return []
            return sorted(
                item.stem for item in folder.iterdir()
                if item.is_file() and (not self.extension or item.suffix == self.extension)
            )
        return []


def parse_source(raw: Any, label: str, problems: List[str]) -> Optional[OptionSource]:
    """Read one source declaration. Returns None (and reports) if unusable."""
    if not isinstance(raw, Mapping):
        problems.append(f"{label} is not an object")
        return None

    # Two spellings in the wild, and one reader has to take both: a server's
    # catalog declares `values`, and LabCore normalises that to `options` when
    # it merges a discovered server's declarations into the hint map, which is
    # also the shape lab_config.yaml has always used.
    if raw.get("values") is not None or raw.get("options") is not None:
        values = raw["values"] if raw.get("values") is not None else raw["options"]
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple)):
            problems.append(f"{label}.values must be a list")
            return None
        # The empty string is a real value — it is how the Tecan says "use the
        # configured default" — so only non-strings are dropped.
        return OptionSource(values=tuple(v for v in values if isinstance(v, str)))

    def _path(key: str) -> Optional[str]:
        safe = _safe_relative(raw.get(key))
        if safe is None:
            problems.append(
                f"{label}.{key} must be a relative path inside "
                f"{' or '.join(ALLOWED_ROOTS)}/, got {raw.get(key)!r}")
        return safe

    if raw.get("glob"):
        pattern = _path("glob")
        if pattern is None:
            return None
        fields = raw.get("fields") or raw.get("field") or "id"
        if isinstance(fields, str):
            fields = [fields]
        if not isinstance(fields, (list, tuple)):
            problems.append(f"{label}.fields must be a string or a list")
            return None
        return OptionSource(glob=pattern,
                            fields=tuple(f for f in fields if isinstance(f, str)))

    if raw.get("file"):
        path = _path("file")
        if path is None:
            return None
        if raw.get("list_key"):
            return OptionSource(file=path, list_key=str(raw["list_key"]))
        if raw.get("items_key"):
            template = raw.get("key_template")
            if not isinstance(template, str) or "{" not in template:
                problems.append(
                    f"{label}.items_key needs a key_template like '{{channel}}/{{model}}'")
                return None
            return OptionSource(file=path, items_key=str(raw["items_key"]),
                                key_template=template)
        problems.append(f"{label}.file needs either list_key or items_key")
        return None

    if raw.get("json_keys_file"):
        path = _path("json_keys_file")
        return OptionSource(json_keys_file=path) if path else None

    if raw.get("folder"):
        path = _path("folder")
        if path is None:
            return None
        extension = raw.get("file_extension") or raw.get("extension") or ""
        if extension and not str(extension).startswith("."):
            extension = f".{extension}"
        return OptionSource(folder=path, extension=str(extension))

    problems.append(
        f"{label} declares no source (expected one of: values, glob, file, "
        "json_keys_file, folder)")
    return None


# ── One hint ──────────────────────────────────────────────────────────────────

@dataclass
class HintSpec:
    """Everything known about one ui_hint, and where it was declared."""

    hint: str
    display_name: str = ""
    parameter_names: Tuple[str, ...] = ()
    sources: Tuple[OptionSource, ...] = ()
    #: "server:<id>" or "config". Shown in the Settings panel so an operator can
    #: tell a server's declaration from the deployment's.
    origin: str = "config"
    #: Read from a live server property instead of the filesystem. Resolved
    #: elsewhere (LabCore holds the last value and the on-disk snapshot); this
    #: module only records that it is that kind of hint.
    source_property: str = ""
    #: For a dynamic hint only: where the last list read is kept, for when that
    #: server is off. It is NOT a source of values — treating it as one made the
    #: snapshot folder's own file names show up as pickable robot tasks.
    snapshot_folder: str = ""
    value_field: str = ""
    label_field: str = ""
    value_labels: Mapping[str, str] = field(default_factory=dict)

    @property
    def dynamic(self) -> bool:
        return bool(self.source_property)

    def resolve(self, base_dir: Path) -> List[str]:
        """Every value this hint offers, de-duplicated, order preserved."""
        out: List[str] = []
        for source in self.sources:
            for value in source.resolve(base_dir):
                if value not in out:
                    out.append(value)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hint": self.hint,
            "display_name": self.display_name or self.hint,
            "parameter_names": list(self.parameter_names),
            "sources": [s.to_dict() for s in self.sources],
            "origin": self.origin,
            "dynamic": self.dynamic,
            "source_property": self.source_property,
            "snapshot_folder": self.snapshot_folder,
        }


def _labels(raw: Mapping[str, Any]) -> Dict[str, str]:
    """`value_labels` as a server writes it, or `option_labels` as LabCore
    stores it after merging one in."""
    for key in ("value_labels", "option_labels"):
        found = raw.get(key)
        if isinstance(found, Mapping):
            return {str(k): str(v) for k, v in found.items()}
    return {}


def parse_hint(raw: Any, origin: str, problems: List[str]) -> Optional[HintSpec]:
    """Read one `library_types` entry, in either the old or the new shape.

    The old shape put a single source inline (`folder`, `values`,
    `json_keys_file`); the new one allows a `sources` list, which is what the
    lab-wide catalogs need — `labware` is the union of three. Both are accepted,
    because every shipped catalog uses the old one and there is no reason to
    make them churn.
    """
    if not isinstance(raw, Mapping):
        problems.append(f"{origin}: a library_types entry is not an object")
        return None
    hint = str(raw.get("hint") or "").strip()
    if not _HINT_RE.match(hint):
        problems.append(f"{origin}: library_types entry has no usable hint ({raw.get('hint')!r})")
        return None

    label = f"{origin}: {hint}"
    parameter_names = raw.get("parameter_names") or []
    if not isinstance(parameter_names, (list, tuple)):
        problems.append(f"{label}.parameter_names is not a list")
        parameter_names = []

    spec = HintSpec(
        hint=hint,
        display_name=str(raw.get("display_name") or ""),
        parameter_names=tuple(str(p) for p in parameter_names),
        origin=origin,
        source_property=str(raw.get("source_property") or ""),
        value_field=str(raw.get("value_field") or ""),
        label_field=str(raw.get("label_field") or ""),
        value_labels=_labels(raw),
    )

    # For a dynamic hint, a top-level `folder` is the snapshot of the last list
    # read, not somewhere to find values. Two different roles that share a key
    # name, so the dynamic case claims it before the source parsing below.
    if spec.dynamic or raw.get("dynamic"):
        snapshot = _safe_relative(raw.get("folder"))
        if snapshot:
            spec.snapshot_folder = snapshot

    sources: List[OptionSource] = []
    declared = raw.get("sources")
    if declared is not None:
        if not isinstance(declared, (list, tuple)):
            problems.append(f"{label}.sources must be a list")
        else:
            for index, entry in enumerate(declared):
                source = parse_source(entry, f"{label}.sources[{index}]", problems)
                if source is not None:
                    sources.append(source)
    elif any(raw.get(k) is not None
             for k in ("values", "options", "glob", "file", "json_keys_file")) or (
                 raw.get("folder") and not spec.snapshot_folder):
        source = parse_source(raw, label, problems)
        if source is not None:
            sources.append(source)
    # A dynamic hint with no filesystem source is fine: its values come from the
    # server. One with a `folder` keeps it as the offline snapshot.

    spec.sources = tuple(sources)
    if not sources and not spec.dynamic and not raw.get("dynamic"):
        problems.append(
            f"{label} offers nothing: it declares no source and is not dynamic")
    return spec


# ── The three layers, resolved ────────────────────────────────────────────────

@dataclass
class HintRegistry:
    hints: Dict[str, HintSpec] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)

    def get(self, hint: str) -> Optional[HintSpec]:
        return self.hints.get(hint)

    def options(self, base_dir: Path, hint: str) -> List[str]:
        spec = self.hints.get(hint)
        return spec.resolve(base_dir) if spec is not None else []

    def hint_for_parameter(self, parameter: str) -> str:
        for spec in self.hints.values():
            if parameter in spec.parameter_names:
                return spec.hint
        return ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hints": {k: v.to_dict() for k, v in sorted(self.hints.items())},
            "problems": list(self.problems),
        }


def build_registry(
    server_library_types: Mapping[str, Sequence[Any]],
    config_declare: Optional[Mapping[str, Sequence[Any]]] = None,
    config_hints: Optional[Mapping[str, Any]] = None,
) -> HintRegistry:
    """Merge the three layers into one registry.

    Precedence is the same as for resources, and for the same reason: an
    instrument is the authority on itself, so a server's declaration is never
    overridden from central config — otherwise adding an instrument would mean
    editing the core's configuration, which is the thing this architecture
    promises it does not.

    * a server's own entry wins;
    * `config_declare[<server>]` applies only where that server declared
      nothing for the hint;
    * `config_hints` (the `ui_dropdowns` section) applies where no server
      claims the hint at all. That is where the lab-wide catalogs live, by
      right rather than as a fallback.
    """
    registry = HintRegistry()
    claimed_by: Dict[str, str] = {}

    for server_id, entries in (server_library_types or {}).items():
        for raw in entries or []:
            spec = parse_hint(raw, f"server:{server_id}", registry.problems)
            if spec is None:
                continue
            owner = claimed_by.get(spec.hint)
            if owner is not None and owner != f"server:{server_id}":
                # Two instruments offering the same picker is normal — both the
                # Opentrons and a second liquid handler read recipes — so the
                # first one wins quietly and the rest are equivalent.
                continue
            claimed_by[spec.hint] = f"server:{server_id}"
            registry.hints[spec.hint] = spec

    for server_id, entries in (config_declare or {}).items():
        for raw in entries or []:
            spec = parse_hint(raw, "config", registry.problems)
            if spec is None:
                continue
            owner = claimed_by.get(spec.hint)
            if owner is not None and owner.startswith("server:"):
                registry.problems.append(
                    f"library_types.declare.{server_id}.{spec.hint} is ignored: "
                    f"{owner.split(':', 1)[1]} declares it itself")
                continue
            claimed_by[spec.hint] = "config"
            registry.hints[spec.hint] = spec

    for hint, raw in (config_hints or {}).items():
        if hint in claimed_by and claimed_by[hint].startswith("server:"):
            # Silent on purpose: lab_config may legitimately keep an offline
            # snapshot entry for a hint a server has since started declaring.
            continue
        entry = dict(raw) if isinstance(raw, Mapping) else {}
        entry.setdefault("hint", hint)
        spec = parse_hint(entry, "config", registry.problems)
        if spec is not None:
            claimed_by[hint] = "config"
            registry.hints[hint] = spec

    return registry
