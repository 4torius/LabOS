"""Where each plate is, step by step, as the Workflow Builder shows it.

A step may say which plate it concerns (`"Plate": "A"`, ignored by the
executor). Following the steps in the order they are written, this module
works out where every labelled plate is and warns when a step cannot make
sense there: reading plate A while A is still on the Opentrons, placing B on
the deck while A is on it, picking A from the reader when it was never put
there, or the robot carrying two plates at once.

Nothing here knows the instruments. It reads what their catalogs already
declare:

* a step that **acquires** `slot:X` puts its plate into the instrument that
  provides `slot:X` (the robot's `place_on_Tecan` acquires `slot:tecan`);
* a step that **releases** it takes the plate out, onto the instrument that
  ran the step (`pick_from_tecan`: the plate is now on the robot);
* a step that **needs** `slot:X` works on a plate that must be there
  (`RunMeasurement` needs `slot:tecan`).

One move no catalog can declare is a person's: the operator loading a plate
onto the robot or taking it away. A step says it with `"PlateMove"`:
`"onto:mobile"` (the plate is now on that instrument) or `"off"` (it has left;
nothing is known about it any more). The operator blocks carry it.

It is advice, never a refusal: the written order is not always the run order
(steps on different instruments may overlap), and a plate loaded by hand is
somewhere no step said. A plate nobody has moved yet is "not known" and draws
no warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional


@dataclass
class PlateStep:
    number: int
    plate: str                 # "" when the step names no plate
    carrier: str               # the instrument key that runs the step ("" if none)
    acquires: FrozenSet[str] = frozenset()
    releases: FrozenSet[str] = frozenset()
    needs: FrozenSet[str] = frozenset()
    #: "onto:<instrument>", "off" or "" - a move only a person makes
    move: str = ""


@dataclass
class PlateReport:
    number: int
    plate: str
    #: plate -> where it is after this step ("tecan", "on mobile"), for every
    #: plate whose place is known by then
    after: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


#: A placeholder holder for a plate no step labelled.
UNLABELLED = ""


def track(steps: Iterable[PlateStep],
          owner_of: Callable[[str], Optional[str]]) -> List[PlateReport]:
    """Follow the plates through `steps`, in order. `owner_of(slot)` names the
    instrument that provides a slot, or None."""
    where: Dict[str, str] = {}     # plate -> "slot:x" or "on:<carrier>"
    holder: Dict[str, str] = {}    # slot -> plate in it
    reports: List[PlateReport] = []

    def place_name(location: str) -> str:
        if location.startswith("on:"):
            return f"on {location[3:]}"
        return f"in {owner_of(location) or location}"

    def who(plate: str) -> str:
        return f"plate {plate}" if plate else "a plate with no label"

    for step in steps:
        report = PlateReport(number=step.number, plate=step.plate)
        plate = step.plate

        for slot in sorted(step.releases):
            inside = holder.get(slot)
            target = owner_of(slot) or slot
            if plate and inside is not None and inside != plate:
                report.warnings.append(
                    f"takes plate {plate} out of {target}, but {who(inside)} is in it")
            elif plate and plate in where and where[plate] != slot:
                report.warnings.append(
                    f"takes plate {plate} out of {target}, but at this point it is "
                    f"{place_name(where[plate])}")
            holder.pop(slot, None)
            if plate:
                where[plate] = f"on:{step.carrier}"

        for slot in sorted(step.acquires):
            inside = holder.get(slot)
            target = owner_of(slot) or slot
            if inside is not None and inside != plate:
                report.warnings.append(
                    f"puts {who(plate)} into {target}, but {who(inside)} is still in it")
            elif plate and plate in where and not where[plate].startswith("on:"):
                report.warnings.append(
                    f"puts plate {plate} into {target}, but at this point it is "
                    f"{place_name(where[plate])}")
            holder[slot] = plate or UNLABELLED
            if plate:
                where[plate] = slot

        if plate and step.move == "off":
            for slot in [s for s, p in holder.items() if p == plate]:
                del holder[slot]
            where.pop(plate, None)
        elif plate and step.move.startswith("onto:"):
            target = step.move[5:]
            if plate in where and not where[plate].startswith("on:"):
                report.warnings.append(
                    f"puts plate {plate} onto {target}, but at this point it is "
                    f"{place_name(where[plate])}")
            where[plate] = f"on:{target}"

        if plate:
            for slot in sorted(step.needs):
                inside = holder.get(slot)
                target = owner_of(slot) or slot
                if inside is not None and inside != plate:
                    report.warnings.append(
                        f"works on plate {plate} in {target}, but {who(inside)} is in it")
                elif plate in where and where[plate] != slot:
                    report.warnings.append(
                        f"works on plate {plate} in {target}, but at this point it is "
                        f"{place_name(where[plate])}")

        carrier = step.move[5:] if step.move.startswith("onto:") else step.carrier
        if carrier and (step.releases or step.acquires or step.move.startswith("onto:")):
            carried = sorted(p for p, w in where.items() if w == f"on:{carrier}")
            if len(carried) > 1:
                report.warnings.append(
                    f"{carrier} would be carrying plates {', '.join(carried)} at once")

        report.after = {p: place_name(w) for p, w in sorted(where.items())}
        reports.append(report)
    return reports


def report_workflow(raw_steps: Iterable[dict], key_of: Callable[[str, str], str],
                    topology) -> Dict[str, dict]:
    """`track` over a workflow's steps as written in its file.

    `key_of(instrument, action)` is the registry key of the instrument that
    runs a step, or "" for a step no instrument runs (a Delay). `topology` is
    a `resources.ResourceTopology`. Only a workflow that labels at least one
    plate gets a report.
    """
    raw_steps = [s for s in raw_steps if isinstance(s, dict)]
    if not any(s.get("Plate") for s in raw_steps):
        return {}
    steps = []
    for raw in raw_steps:
        action = str(raw.get("Action") or "")
        params = raw.get("Parameters") if isinstance(raw.get("Parameters"), dict) else {}
        key = key_of(str(raw.get("Instrument") or ""), action)
        steps.append(PlateStep(
            number=int(raw.get("StepNumber") or len(steps) + 1),
            plate=str(raw.get("Plate") or ""),
            carrier=key,
            acquires=topology.acquires_for(key, action, params) if key else frozenset(),
            releases=topology.releases_for(key, action, params) if key else frozenset(),
            needs=topology.needs_for(key, action) if key else frozenset(),
            move=str(raw.get("PlateMove") or ""),
        ))
    return {str(r.number): {"plate": r.plate, "after": r.after, "warnings": r.warnings}
            for r in track(steps, topology.providers.get)}
