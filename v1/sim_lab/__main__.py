"""Run the simulated lab from a terminal.

    python -m sim_lab up                      start the instruments and leave them running
    python -m sim_lab run Test_Bicocca        start them, run one workflow, show what happened
    python -m sim_lab run my.workflow.json --max-concurrency 0

See docs/SIMULATED_LAB.md.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from sim_lab.lab import SIM_DIR, V1_DIR, SimLab


def _lab(args) -> SimLab:
    scenario = None
    if args.scenario:
        scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    return SimLab(scenario=scenario, max_concurrency=args.max_concurrency,
                  retry_on_failure=args.retry, workdir=SIM_DIR / ".run")


def _print_timeline(lab: SimLab) -> None:
    intervals = lab.intervals()
    if not intervals:
        print("  (no command reached an instrument)")
        return
    origin = intervals[0].start
    for interval in intervals:
        end = "  ...  " if interval.end == float("inf") else f"{interval.end - origin:6.2f}"
        print(f"  {interval.start - origin:6.2f} -> {end}  {interval.status:<8} {interval}")


def cmd_up(args) -> int:
    with _lab(args) as lab:
        print("Simulated lab running (no hardware behind any of these):")
        for server in lab.servers:
            print(f"  {server.key:<15} {server.name:<22} 127.0.0.1:{server.port}")
        print()
        print("Point LabOS at it in ANOTHER terminal, then use it as usual:")
        print(f"  PowerShell   $env:LABOS_CONFIG = \"{lab.config_path}\"")
        print(f"  cmd          set LABOS_CONFIG={lab.config_path}")
        print(f"  bash         export LABOS_CONFIG=\"{lab.config_path}\"")
        print("  then         python labos.py instruments")
        print("               python labos.py resources")
        print("               python labos.py run Test_Bicocca -y")
        print("               python webapp/app.py --port 5050      (not 5000: that is the real one)")
        print()
        print(f"Timeline of what the instruments did: {lab.timeline_path}")
        print("Ctrl+C to stop.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\nStopping.")
    return 0


def _report(lab: SimLab, result) -> dict:
    """What happened, as data: for the workflow editor's "try it" button."""
    intervals = lab.intervals()
    origin = intervals[0].start if intervals else 0.0
    return {
        "success": bool(result.success),
        "completed": result.steps_completed,
        "failed": result.steps_failed,
        "skipped": result.steps_skipped,
        "duration_seconds": round(result.duration_seconds, 2),
        "errors": list(result.errors),
        "timeline": [{
            "instrument": i.server, "command": i.command, "label": str(i),
            "start": round(i.start - origin, 3),
            "end": None if i.end == float("inf") else round(i.end - origin, 3),
            "status": i.status,
        } for i in intervals],
        "together": len(lab.ran_together()),
        "clashes": [{"a": str(a), "b": str(b), "resource": r}
                    for a, b, r in lab.any_overlap()],
    }


def cmd_run(args) -> int:
    with _lab(args) as lab:
        result = asyncio.run(lab.run_workflow(args.workflow))
        if args.json:
            report = _report(lab, result)
            print(json.dumps(report))
            return 2 if report["clashes"] else (0 if result.success else 1)
        print()
        print(f"{'COMPLETED' if result.success else 'FAILED'}: "
              f"{result.steps_completed} completed, {result.steps_failed} failed, "
              f"{result.steps_skipped} skipped, {result.duration_seconds:.1f}s")
        for error in result.errors:
            print(f"  - {error}")
        print("\nWhat the instruments did (seconds from the first command):")
        _print_timeline(lab)
        together = lab.ran_together()
        print(f"\nCommands on different instruments in flight together: {len(together)}")
        for a, b in together:
            print(f"  {a}  with  {b}")
        clashes = lab.any_overlap()
        if clashes:
            print("\nOVERLAP ON A SHARED RESOURCE - this must never happen:")
            for a, b, resource in clashes:
                print(f"  {resource}: {a}  with  {b}")
            return 2
        print("No two commands overlapped on a resource they both occupy.")
        return 0 if result.success else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m sim_lab", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default="",
                        help="a scenario file (default: sim_lab/scenario.default.json)")
    parser.add_argument("--max-concurrency", type=int, default=0, metavar="N",
                        help="steps at once in the simulated configuration "
                             "(default 0 = no cap; 1 = one at a time, like the bench today)")
    parser.add_argument("--retry", action="store_true",
                        help="simulate with workflow.retry_on_failure: true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("up", help="start the simulated instruments and wait")
    run = commands.add_parser("run", help="run one workflow against them")
    run.add_argument("workflow", help="a name in Library/Workflows, or a path")
    run.add_argument("--json", action="store_true",
                     help="print one JSON object (result, timeline, overlaps) instead of text")
    args = parser.parse_args()
    if str(V1_DIR) not in sys.path:
        sys.path.insert(0, str(V1_DIR))
    return {"up": cmd_up, "run": cmd_run}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
