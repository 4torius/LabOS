"""Task names in the catalog, task ids on the wire.

`library_catalog.json` declares what each robot task occupies under the task's
*name* - `place_on_OT`, `pick_from_tecan` - because that is what a person can
read and review, and it survives a task being taught again.

A workflow does not send the name. It sends `TaskId`, the opaque id the robot's
platform assigned when the task was taught, and that id changes every time the
task is re-taught. Declared by id in a file, the declaration silently stops
matching the day somebody re-teaches a pick - and a claim that matches nothing
is no exclusion at all, with nothing on screen to say so.

So the server, which is the only party that knows both, adds the id entries
itself each time it serves the catalog: for every task the robot currently has
whose name is declared, the same entry under its id.

A task whose name is *not* declared gets no entry. The catalog marks
`ExecuteTask` as `exhaustive`, so the orchestrator refuses a workflow that uses
such a task until somebody declares what it occupies - an empty entry if it
only moves the robot. That is deliberate: a task taught yesterday is not known
to be harmless because nobody has written it down yet.

Runs on the robot PC under Python 3.8: keep it to what 3.8 has.
"""

import copy
import json
import logging
import os

logger = logging.getLogger(__name__)

_warned = set()


def expand_task_names(catalog, tasks):
    """Return `catalog` with a TaskId table derived from its TaskName table.

    `tasks` is an iterable of `(task_id, task_name)`. Entries already written
    under `TaskId` in the file are kept as they are.
    """
    tasks = [(str(i), str(n)) for i, n in tasks if i]
    expanded = copy.deepcopy(catalog)
    commands = ((expanded.get("resources") or {}).get("commands") or {})
    for command, spec in commands.items():
        if not isinstance(spec, dict):
            continue
        tables = spec.get("by_parameter")
        if not isinstance(tables, dict) or not isinstance(tables.get("TaskName"), dict):
            continue
        by_name = tables["TaskName"]
        by_id = dict(tables.get("TaskId") or {})
        undeclared = []
        for task_id, task_name in tasks:
            if task_name in by_name:
                by_id.setdefault(task_id, copy.deepcopy(by_name[task_name]))
            elif task_id not in by_id:
                undeclared.append(task_name or task_id)
        if by_id:
            tables["TaskId"] = by_id
        key = (command, tuple(sorted(undeclared)))
        if undeclared and key not in _warned:
            _warned.add(key)
            logger.warning(
                "%s: no resource declaration for task(s) %s - LabOS will refuse a "
                "workflow that uses them until library_catalog.json says what they "
                "occupy (an empty entry if nothing beyond the robot)",
                command, ", ".join(sorted(set(undeclared))))
    return expanded


def bridge_tasks(bridge):
    """`(id, name)` for every task the bridge currently knows, without I/O.

    Reads what the bridge last fetched rather than asking the robot again:
    this runs inside a property read, and the orchestrator reads the catalog
    on every discovery pass.
    """
    known = list(getattr(bridge, "_available_tasks", None) or [])
    if not known and getattr(bridge, "simulate", False):
        known = list(getattr(bridge, "_simulated_tasks", None) or [])
    return [(t.root_id, t.root_name) for t in known]


def tasks_from_file(path):
    """`(id, name)` pairs from a JSON list of `{"id": ..., "name": ...}`."""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return [(str(t["id"]), str(t["name"])) for t in data if isinstance(t, dict) and t.get("id")]


def augmenter_from_tasks_file(path=None):
    """A catalog augmenter fed from a file instead of a robot.

    For the simulated lab, where there is no robot to ask: `path`, or the
    `MOBILE_SIM_TASKS` environment variable.
    """
    path = path or os.environ.get("MOBILE_SIM_TASKS", "")

    def _augment(catalog):
        return expand_task_names(catalog, tasks_from_file(path)) if path else catalog
    return _augment
