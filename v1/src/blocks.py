"""Blocks: named sequences of steps the Workflow Builder inserts in one go.

Moving a plate from the robot into the reader is five steps - drive there,
take the plate off the jig, open the tray, place it, close the tray - and they
have to be written in that order every time. A block is that sequence, written
once in `Library/Blocks/<id>.block.json`, which the builder expands into
ordinary steps. Nothing downstream knows blocks exist: the executor sees steps.

A block file::

    {
      "Name": "Leave the plate in the Tecan",
      "Description": "From the robot's jig into the reader, tray closed.",
      "Category": "Transport",
      "Steps": [
        {"Instrument": "mobile", "Action": "ExecuteTask",
         "Parameters": {"TaskId": {"label": "go_to_tecan"}, "ExecutionMode": "Normal"}},
        {"Instrument": "tecan", "Action": "PlateOut", "Parameters": {}},
        ...
      ]
    }

* `DependsOn`, if given, is **1-based within the block**. Without it a step
  waits for the step before it in the block, and the first step waits for
  whatever is above it in the workflow - so a block reads, and runs, in order.
* A parameter written `{"label": "go_to_tecan"}` means *the option whose shown
  name is go_to_tecan*. The robot's tasks are picked by name because their ids
  change whenever a task is re-taught; the id is looked up when the block is
  inserted, through the same option list the builder's dropdown shows.
* `Ask` lists the parameters the person must choose (the recipe, the reading
  protocol). The builder marks them until they are changed, rather than
  leaving whatever default it filled in looking like a decision.

A block file is untrusted input like a server catalog: a malformed one costs
that block and is reported, nothing raises.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

SUFFIX = ".block.json"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: (instrument, action, parameter, label) -> (value, error). The value is None
#: when the label could not be found, and the error then says why.
LabelResolver = Callable[[str, str, str, str], Tuple[Optional[Any], Optional[str]]]


@dataclass
class BlockStep:
    instrument: str
    action: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    #: 1-based within the block; None = the step before it in the block
    depends_on: Optional[List[int]] = None
    description: str = ""
    ask: List[str] = field(default_factory=list)


@dataclass
class Block:
    id: str
    name: str
    description: str = ""
    category: str = ""
    steps: List[BlockStep] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "description": self.description,
                "category": self.category, "steps": len(self.steps)}


def parse_block(block_id: str, raw: Any, problems: List[str]) -> Optional[Block]:
    """Read one block file's content. Returns None (and reports) if unusable."""
    label = f"block {block_id}"
    if not isinstance(raw, Mapping):
        problems.append(f"{label}: not a JSON object")
        return None
    raw_steps = raw.get("Steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        problems.append(f"{label}: Steps must be a non-empty list")
        return None

    steps: List[BlockStep] = []
    for index, entry in enumerate(raw_steps, start=1):
        where = f"{label} step {index}"
        if not isinstance(entry, Mapping):
            problems.append(f"{where}: not an object")
            return None
        instrument = str(entry.get("Instrument") or "").strip()
        action = str(entry.get("Action") or "").strip()
        if not instrument or not action:
            problems.append(f"{where}: needs Instrument and Action")
            return None
        parameters = entry.get("Parameters") or {}
        if not isinstance(parameters, Mapping):
            problems.append(f"{where}: Parameters must be an object")
            return None
        depends = entry.get("DependsOn")
        if depends is not None:
            if (not isinstance(depends, list)
                    or not all(isinstance(d, int) and 1 <= d < index for d in depends)):
                problems.append(
                    f"{where}: DependsOn must list earlier steps of the block "
                    f"(1 to {index - 1})")
                return None
        ask = entry.get("Ask") or []
        if not isinstance(ask, list) or not all(isinstance(a, str) for a in ask):
            problems.append(f"{where}: Ask must be a list of parameter names")
            return None
        steps.append(BlockStep(
            instrument=instrument, action=action, parameters=dict(parameters),
            depends_on=list(depends) if depends is not None else None,
            description=str(entry.get("Description") or ""), ask=list(ask)))

    return Block(id=block_id, name=str(raw.get("Name") or block_id),
                 description=str(raw.get("Description") or ""),
                 category=str(raw.get("Category") or ""), steps=steps)


def load_blocks(folder: Path) -> Tuple[List[Block], List[str]]:
    """Every block in `folder`, sorted by category then name, and the problems."""
    blocks: List[Block] = []
    problems: List[str] = []
    if not folder.is_dir():
        return blocks, problems
    for path in sorted(folder.glob(f"*{SUFFIX}")):
        block_id = path.name[: -len(SUFFIX)]
        if not _ID_RE.match(block_id):
            problems.append(f"{path.name}: the file name must be letters, digits, - or _")
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            problems.append(f"{path.name}: unreadable ({exc})")
            continue
        block = parse_block(block_id, raw, problems)
        if block is not None:
            blocks.append(block)
    blocks.sort(key=lambda b: (b.category or "~", b.name.lower()))
    return blocks, problems


def expand(block: Block, resolve_label: LabelResolver) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The block's steps as workflow steps, with labels turned into values.

    DependsOn stays block-relative (1-based); the builder offsets it to where
    the block lands. A first step written without DependsOn comes back as []
    and the builder makes it wait for the step above it. Returns the steps and
    the labels that could not be resolved; a step with an unresolved label is
    still returned, with the parameter left empty, so the person sees where.
    """
    out: List[Dict[str, Any]] = []
    errors: List[str] = []
    for index, step in enumerate(block.steps, start=1):
        parameters: Dict[str, Any] = {}
        for name, value in step.parameters.items():
            if isinstance(value, Mapping) and set(value) == {"label"}:
                resolved, error = resolve_label(step.instrument, step.action, name,
                                                str(value["label"]))
                if resolved is None:
                    errors.append(f"step {index} ({step.action}): {error}")
                    parameters[name] = ""
                else:
                    parameters[name] = resolved
            else:
                parameters[name] = value
        depends = step.depends_on if step.depends_on is not None else (
            [index - 1] if index > 1 else [])
        out.append({
            "Instrument": step.instrument,
            "Action": step.action,
            "Parameters": parameters,
            "DependsOn": depends,
            "Description": step.description,
            "Ask": list(step.ask),
        })
    return out, errors
