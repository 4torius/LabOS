#!/usr/bin/env python3
"""Rewrite saved workflow files to canonical instrument ids.

Workflow files in Library/Workflows were authored over months by different
tools, so the same instrument appears as `Tecan`, `TecanM200Pro`,
`OpentronsFlex` and `GoFaGo Mobile Robot`. The executor resolves all of those
(src/resolver.py), but keeping the loose spellings in the files means every
reader has to resolve them mentally too, and a future second liquid handler
would turn several of them ambiguous.

This script rewrites each file to the canonical id from lab_config.yaml, adds a
SchemaVersion, and reports anything it could not resolve rather than guessing.

    python scripts/migrate_workflows.py            # report only (default)
    python scripts/migrate_workflows.py --apply    # write the changes

The files are tracked in git, so `git diff` shows exactly what changed and
`git checkout` undoes it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.resolver import resolve  # noqa: E402
from src.settings import get_settings  # noqa: E402
from src.workflow import SCHEMA_VERSION, is_locally_handled  # noqa: E402


def _candidates() -> List[Tuple[str, str, str]]:
    """(id, display_name, canonical_id) for every configured server."""
    settings = get_settings(BASE_DIR)
    return [(spec.key, spec.name, spec.key) for spec in settings.servers.values()]


def _chain_linear(data: dict, changes: List[str]) -> None:
    """Make an implicitly-ordered workflow explicitly ordered.

    Every workflow in Library/Workflows was authored in the Workflow Builder as
    an ordered list, and none of them declares a single dependency. That was
    harmless only because sequential execution happened to walk the file top to
    bottom. Under parallel execution the same file dispatches every step at
    once - for O-T-D.workflow.json that is fifteen simultaneous commands to a
    mobile manipulator, a liquid handler and a plate reader that are passing
    one physical plate between them.

    So where a multi-step workflow declares no dependencies at all, chain it:
    each step depends on the one before. That states the intent the author
    clearly had, and makes the file behave the same under either mode.
    Workflows that already declare dependencies are left alone.
    """
    steps = data.get("Steps", [])
    if len(steps) < 2 or any(step.get("DependsOn") for step in steps):
        return
    previous = None
    for step in steps:
        number = step.get("StepNumber")
        if previous is not None:
            step["DependsOn"] = [previous]
        previous = number
    changes.append(f"chained {len(steps)} steps sequentially (none declared a dependency)")


def migrate_file(path: Path, candidates, chain_linear: bool = False) -> Tuple[dict, List[str], List[str]]:
    """Return (new_data, changes, problems) for one workflow file."""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    changes: List[str] = []
    problems: List[str] = []

    if data.get("SchemaVersion") != SCHEMA_VERSION:
        data["SchemaVersion"] = SCHEMA_VERSION
        changes.append(f"SchemaVersion -> {SCHEMA_VERSION}")

    seen_numbers = set()
    for index, step in enumerate(data.get("Steps", [])):
        number = step.get("StepNumber", step.get("StepID"))
        if number is None:
            number = index + 1
            step["StepNumber"] = number
            changes.append(f"step {index + 1}: added missing StepNumber")
        if "StepID" in step:
            # One documented spelling. StepID was only ever used by the docs.
            step.setdefault("StepNumber", step.pop("StepID"))
            changes.append(f"step {number}: StepID -> StepNumber")
        if number in seen_numbers:
            problems.append(f"step {number}: StepNumber is used more than once")
        seen_numbers.add(number)

        step.setdefault("DependsOn", [])

        written = (step.get("Instrument") or "").strip()
        if not written:
            problems.append(f"step {number}: no Instrument")
            continue
        if is_locally_handled(written, step.get("Action", "")):
            continue

        match = resolve(written, candidates)
        if not match:
            problems.append(f"step {number}: {match.error}")
            continue
        if match.target != written:
            step["Instrument"] = match.target
            changes.append(f"step {number}: {written!r} -> {match.target!r}")

    if chain_linear:
        _chain_linear(data, changes)

    return data, changes, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--chain-linear", action="store_true",
                        help="give steps an explicit sequential DependsOn chain "
                             "in workflows that declare no dependencies at all")
    parser.add_argument("--dir", type=Path, default=BASE_DIR / "Library" / "Workflows")
    args = parser.parse_args()

    candidates = _candidates()
    if not candidates:
        print("No servers configured in lab_config.yaml — nothing to resolve against.")
        return 2
    print(f"Canonical ids: {', '.join(c[0] for c in candidates)}\n")

    files = sorted(args.dir.glob("*.json"))
    if not files:
        print(f"No workflow files in {args.dir}")
        return 0

    total_changes = total_problems = 0
    for path in files:
        try:
            data, changes, problems = migrate_file(path, candidates, args.chain_linear)
        except Exception as exc:
            print(f"  {path.name}: UNREADABLE ({exc})")
            total_problems += 1
            continue

        if not changes and not problems:
            continue

        print(f"  {path.name}")
        for change in changes:
            print(f"      {change}")
        for problem in problems:
            print(f"      ! {problem}")
        total_changes += len(changes)
        total_problems += len(problems)

        if args.apply and changes:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")

    verb = "applied" if args.apply else "would apply"
    print(f"\n{verb} {total_changes} change(s); {total_problems} unresolved problem(s).")
    if not args.apply and total_changes:
        print("Re-run with --apply to write them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
