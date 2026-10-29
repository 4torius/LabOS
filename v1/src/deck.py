"""What is on the deck right now, according to the active hardware config.

The Workflow Builder and the Dashboard need to offer real choices for two
questions: *which* piece of labware do you want to move, and *where* to. Both
were answered with fiction — the labware picker listed six invented load names
(`tiprack_1000ul`, `wellplate_96`, …) that match nothing Opentrons knows, and
the destination picker offered the discovered *instruments* (`tecan`,
`manual_station`), because the parameter happened to be called `NewLocation`
and a name-based rule guessed "location" meant an instrument.

The real answers are in `Library/HardwareConfig/.active`: the labware it
declares, and the deck slots it leaves free.

This module reads that file. It is deliberately read-only and stateless — the
Opentrons server owns writing the HAL (`hardware_manager`), because it is the
process that knows a move actually happened.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Addressable deck slots on an Opentrons Flex. Column 4 (A4–D4) is the staging
#: area: the gripper can place labware there, but a pipette cannot reach it.
FLEX_SLOTS: tuple = ("A1", "A2", "A3", "B1", "B2", "B3",
                     "C1", "C2", "C3", "D1", "D2", "D3")
FLEX_STAGING_SLOTS: tuple = ("A4", "B4", "C4", "D4")


@dataclass(frozen=True)
class DeckItem:
    """One thing occupying a slot."""
    name: str                  # logical name used by recipes, e.g. "MyPlate"
    slot: str
    kind: str                  # "labware" | "module" | "trash"
    load_name: str = ""
    display_name: str = ""
    on_module: Optional[str] = None

    @property
    def label(self) -> str:
        """What a human should see in a dropdown."""
        detail = self.display_name or self.load_name
        if detail and len(detail) > 42:
            # Keep it short enough for a select; the full text is the tooltip.
            detail = detail[:39] + "…"

        if self.kind == "slot":
            # The name *is* the slot; repeating it reads as a stutter.
            return f"{self.name} — {detail}" if detail else self.name

        where = f"on {self.on_module}" if self.on_module else f"slot {self.slot}"
        if detail and detail != self.name:
            return f"{self.name} — {detail} ({where})"
        return f"{self.name} ({where})"


@dataclass(frozen=True)
class DeckLayout:
    """The deck as the active hardware config describes it."""
    config_name: str
    labware: List[DeckItem]
    modules: List[DeckItem]
    trash: List[DeckItem]

    @property
    def everything(self) -> List[DeckItem]:
        return [*self.labware, *self.modules, *self.trash]

    @property
    def occupied_slots(self) -> List[str]:
        return sorted({item.slot for item in self.everything if item.slot})

    def free_slots(self, *, include_staging: bool = True) -> List[str]:
        """Slots nothing is standing in.

        Staging slots are included by default: the gripper can reach them, and
        parking a plate there is exactly what a move is often for.
        """
        taken = set(self.occupied_slots)
        candidates = FLEX_SLOTS + (FLEX_STAGING_SLOTS if include_staging else ())
        return [slot for slot in candidates if slot not in taken]

    def movable(self) -> List[DeckItem]:
        """Labware the gripper could pick up.

        Modules are bolted to the deck and the trash bin is a fixture; neither
        can be moved by a command, so neither belongs in the picker.
        """
        return list(self.labware)

    def move_targets(self) -> List[DeckItem]:
        """Where a piece of labware can be put.

        A free slot, or a module — `protocol.move_labware(new_location=...)`
        accepts either, and the generated protocol resolves a module by its
        logical name.
        """
        targets = [
            DeckItem(name=slot, slot=slot, kind="slot",
                     display_name="staging area" if slot in FLEX_STAGING_SLOTS else "")
            for slot in self.free_slots()
        ]
        targets.extend(self.modules)
        return targets

    def find(self, name: str) -> Optional[DeckItem]:
        lowered = (name or "").lower()
        return next((i for i in self.everything if i.name.lower() == lowered), None)


def _items(section: Dict[str, Any], kind: str) -> List[DeckItem]:
    items = []
    for name, entry in (section or {}).items():
        if not isinstance(entry, dict):
            continue
        items.append(DeckItem(
            name=name,
            slot=str(entry.get("Slot", "") or ""),
            kind=kind,
            load_name=str(entry.get("LoadName", entry.get("Type", "")) or ""),
            display_name=str(entry.get("DisplayName", "") or ""),
            on_module=entry.get("OnModule"),
        ))
    return items


def active_config_name(hal_dir: Path) -> Optional[str]:
    """Whatever `.active` points at, or None."""
    marker = hal_dir / ".active"
    try:
        name = marker.read_text(encoding="utf-8-sig").strip()
        return name or None
    except OSError:
        return None


def load_layout(hal_dir: Path, config_name: Optional[str] = None) -> Optional[DeckLayout]:
    """Read a hardware config into a DeckLayout. None if there isn't one.

    `config_name` defaults to the active config, which is the one recipes
    actually run against.
    """
    name = config_name or active_config_name(hal_dir)
    if not name:
        return None
    if not name.endswith(".json"):
        name += ".json"
    path = hal_dir / name
    try:
        # Reject a name that escapes the config directory before reading it.
        path.resolve().relative_to(hal_dir.resolve())
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read hardware config %s: %s", name, exc)
        return None

    return DeckLayout(
        config_name=data.get("ConfigName", path.stem),
        labware=_items(data.get("Labware", {}), "labware"),
        modules=_items(data.get("Modules", {}), "module"),
        trash=_items(data.get("Trash", {}), "trash"),
    )


def options(hal_dir: Path, hint: str) -> List[Dict[str, str]]:
    """`{value, label}` pairs for a deck-related dropdown hint.

    Returns an empty list when no config is active, which the UI shows as an
    empty picker with an explanation — better than offering choices that do not
    exist on the deck in front of the operator.
    """
    layout = load_layout(hal_dir)
    if layout is None:
        return []
    if hint == "hal_labware":
        return [{"value": i.name, "label": i.label} for i in layout.movable()]
    if hint == "deck_target":
        return [{"value": i.name, "label": i.label} for i in layout.move_targets()]
    if hint == "deck_slot":
        return [{"value": s, "label": s} for s in layout.free_slots()]
    if hint == "hal_module":
        return [{"value": i.name, "label": i.label} for i in layout.modules]
    return []


DECK_HINTS = ("hal_labware", "deck_target", "deck_slot", "hal_module")

__all__ = [
    "DECK_HINTS", "FLEX_SLOTS", "FLEX_STAGING_SLOTS",
    "DeckItem", "DeckLayout", "active_config_name", "load_layout", "options",
]
