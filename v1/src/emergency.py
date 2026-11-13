"""What an emergency stop sends to whom, and what it may then claim.

Kept apart from the web route so it can be tested without one, and so the
route cannot drift back into deciding these things inline.

Three decisions live here.

**Which command stops an instrument.** The server says, in its catalog
(`"emergency_stop": {"command": "CancelTask"}`). Guessing from a list of
likely names is how the mobile robot - whose stop is called `CancelTask`,
which the list did not contain - was skipped by every emergency stop. The
list is still tried for a server that says nothing, but only for a command
that can be sent with no parameters.

**What counts as stopped.** Only an instrument that acknowledged. One that
has no stop command at all was *not stopped*, and is reported that way; it
used to be counted as "skipped" and left out of the verdict, so a stop that
reached one instrument out of three read "all instruments confirmed".

**What has nothing to stop.** An operator at a bench. A server says so
(`"emergency_stop": {"not_applicable": "..."}`) and is then left out of the
verdict - explicitly, rather than by not happening to have a matching command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: Tried, in this order, for a server whose catalog names no stop command.
FALLBACK_STOP_COMMANDS: Tuple[str, ...] = (
    "EmergencyStop", "Stop", "Halt", "AbortRun", "Abort", "CancelTask", "Cancel")


@dataclass
class StopPlan:
    #: (instrument, command id) for everything a stop will be sent to
    targets: List[Tuple[Any, str]] = field(default_factory=list)
    #: instrument id -> seconds to wait for it, where its catalog asks for more
    timeouts: Dict[str, float] = field(default_factory=dict)
    #: (instrument name, why) for every instrument a stop cannot reach
    unreachable: List[Tuple[str, str]] = field(default_factory=list)
    #: (instrument name, why) for everything with nothing to stop
    not_applicable: List[Tuple[str, str]] = field(default_factory=list)


def _takes_nothing(command: Any) -> bool:
    """True if the command can be sent with an empty parameter set."""
    parameters = getattr(command, "parameters", None) or []
    return not any(getattr(p, "required", True) for p in parameters)


def stop_command_of(instrument: Any) -> Tuple[Optional[str], str]:
    """(command id, "") or (None, why not) for one instrument.

    `("", reason)` - an empty command with a reason - means there is nothing
    to stop here.
    """
    commands = {getattr(c, "id", None): c for c in (getattr(instrument, "commands", None) or [])}
    catalog = getattr(instrument, "catalog", None)
    declared = getattr(catalog, "stop_command", "") or ""
    nothing = getattr(catalog, "stop_not_applicable", "") or ""

    if declared:
        if declared in commands:
            return declared, ""
        return None, (f"its catalog names '{declared}' as the stop command, "
                      "which the server does not expose")
    if nothing:
        return "", nothing
    for name in FALLBACK_STOP_COMMANDS:
        command = commands.get(name)
        if command is not None and _takes_nothing(command):
            return name, ""
    return None, "it exposes no stop command"


def plan_stop(instruments: Iterable[Any]) -> StopPlan:
    plan = StopPlan()
    for instrument in instruments:
        name = getattr(instrument, "name", None) or getattr(instrument, "id", "?")
        command, why = stop_command_of(instrument)
        if command:
            plan.targets.append((instrument, command))
            seconds = getattr(getattr(instrument, "catalog", None), "stop_timeout", 0.0) or 0.0
            if seconds:
                plan.timeouts[getattr(instrument, "id", name)] = float(seconds)
        elif command == "":
            plan.not_applicable.append((name, why))
        else:
            plan.unreachable.append((name, why))
    return plan


def verdict(plan: StopPlan, acknowledged: int, failures: List[str]) -> Dict[str, Any]:
    """The status and the sentence the operator reads.

    `stopped` is said only when every instrument that has something to stop
    acknowledged. Anything less names what did not.
    """
    attempted = len(plan.targets)
    not_stopped = [f"{name}: {why}" for name, why in plan.unreachable]
    unconfirmed = list(failures) + not_stopped

    if not attempted and not plan.unreachable and not plan.not_applicable:
        status = "failed"
        message = ("No instrument is known to LabOS - nothing was stopped. "
                   "Use the hardware emergency stop.")
    elif not unconfirmed:
        status = "stopped"
        message = (f"All {attempted} instrument(s) acknowledged the stop."
                   if attempted else "No instrument here has anything to stop.")
    elif acknowledged:
        status = "partial"
        message = (f"{acknowledged} instrument(s) acknowledged the stop. "
                   f"NOT CONFIRMED - verify at the instrument: {'; '.join(unconfirmed)}")
    else:
        status = "failed"
        message = ("No instrument acknowledged the stop. Use the hardware "
                   f"emergency stop. ({'; '.join(unconfirmed)})")

    return {
        "status": status,
        "attempted": attempted,
        "successful": acknowledged,
        "failures": list(failures),
        # Kept under their old names for the page that already reads them:
        # "skipped" was always "could not be stopped", it just did not say so.
        "skipped": len(plan.unreachable),
        "skipped_instruments": [name for name, _ in plan.unreachable],
        "not_stopped": unconfirmed,
        "not_applicable": [name for name, _ in plan.not_applicable],
        "message": message,
    }


__all__ = ["FALLBACK_STOP_COMMANDS", "StopPlan", "plan_stop", "stop_command_of", "verdict"]
