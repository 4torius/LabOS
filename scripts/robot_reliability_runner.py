"""External reliability test runner for LabOS mobile workflows.

This script deliberately talks to LabOS only through its public REST API. The
base/controller move is an operator action (or an optional command template),
because the controller protocol is installation-specific.

Three things it is careful about, because the reliability map is only as good
as its bookkeeping:

*The robot's word is not evidence.* `ExecuteTask` reports that the controller
executed its trajectory, not that the plate arrived where it should. A gripper
that closes on nothing and completes the motion reports success. So after every
run the operator gives the verdict, and both answers are kept: `robot_status`
and `operator_status`. Where they disagree the operator wins, and the
disagreement is itself counted - how blind the controller's self-report is, is
worth knowing.

*Per-task, not per-workflow.* A run that ends "completed" says nothing about
which gripper move was marginal. The step timeline is pulled from
`GET /api/runs/{id}/steps`, so each task gets its own rate, retry count and
duration distribution. A task the run never reached - because an earlier one
stopped the loop - is recorded `not_reached`, never as a failure: dividing by
the number of runs instead of the number of attempts would blame the last task
in the loop for the first one's failures.

*The journal is the map.* The runner only ever appends to a single JSONL file.
The CSVs and the summary are rebuilt by re-reading it from the start, so a map
assembled over many sessions - one process per base position, which is how the
measurement is actually taken - stays whole, survives a crash, and can be
regenerated offline with --report-only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shlex
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx


#: Run states LabOS treats as terminal (src/run_manager.py FINISHED_STATES).
TERMINAL = {"completed", "failed", "aborted", "error"}

#: Step states that mean the controller believes the attempt did its job.
STEP_OK = {"success", "completed"}

REPO_ROOT = Path(__file__).resolve().parent.parent
MOBILE_TASK_OPTIONS = REPO_ROOT / "Library" / "MobileTasks" / "_options.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def wilson(successes: int, trials: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Return the 95% Wilson interval for a binomial proportion."""
    if trials <= 0:
        return 0.0, 0.0
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    radius = z * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials) / denominator
    return max(0.0, centre - radius), min(1.0, centre + radius)


def percentile(values: list[float], fraction: float) -> Optional[float]:
    """Nearest-rank percentile. Small samples make interpolation pretend."""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index], 3)


def load_task_names() -> dict[str, str]:
    """TaskId -> TaskName, so the report reads like the bench, not like hashes."""
    try:
        options = json.loads(MOBILE_TASK_OPTIONS.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return {t["TaskId"]: t.get("TaskName", t["TaskId"]) for t in options if t.get("TaskId")}


def parse_offset(text: str) -> tuple[float, float, float]:
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 3:
        raise ValueError(f"servono dx,dy,dtheta - ricevuto {text!r}")
    return tuple(float(p) for p in parts)  # type: ignore[return-value]


def position_id(dx: float, dy: float, dtheta: float) -> str:
    return f"dx{dx:g}_dy{dy:g}_dt{dtheta:g}"


# ── The workflow under test ──────────────────────────────────────────────────


@dataclass
class Slot:
    """One task position in the loop. The unit the map characterises."""

    step_number: int
    instrument: str
    action: str
    task_id: Optional[str]
    label: str

    @property
    def key(self) -> str:
        return f"{self.step_number:02d}_{self.label}"


@dataclass
class WorkflowSpec:
    name: str
    path: Path
    sha1: str
    body: dict[str, Any]
    slots: list[Slot]

    @classmethod
    def load(cls, path: Path, override_name: Optional[str] = None) -> "WorkflowSpec":
        raw = path.read_bytes()
        body = json.loads(raw.decode("utf-8-sig"))
        body["WorkflowName"] = override_name or body.get("WorkflowName") or path.stem
        names = load_task_names()

        slots: list[Slot] = []
        for index, step in enumerate(body.get("Steps", []), start=1):
            number = step.get("StepNumber") or index
            instrument = str(step.get("Instrument", "?"))
            action = str(step.get("Action", "?"))
            task_id = (step.get("Parameters") or {}).get("TaskId")
            label = names.get(task_id, task_id) if task_id else f"{instrument}.{action}"
            slots.append(Slot(int(number), instrument, action, task_id, str(label)))

        return cls(body["WorkflowName"], path, hashlib.sha1(raw).hexdigest()[:12], body, slots)


# ── Where the base is ────────────────────────────────────────────────────────


@dataclass
class Position:
    dx_cm: float
    dy_cm: float
    dtheta_deg: float
    measured: Optional[tuple[float, float, float]] = None
    note: str = ""

    @property
    def id(self) -> str:
        return position_id(self.dx_cm, self.dy_cm, self.dtheta_deg)

    def as_record(self) -> dict[str, Any]:
        record = {"position_id": self.id, "dx_cm": self.dx_cm, "dy_cm": self.dy_cm,
                  "dtheta_deg": self.dtheta_deg, "note": self.note}
        # Planned and measured are kept apart on purpose. Both are optional;
        # what is not optional is knowing which of the two a number is.
        measured = self.measured or (None, None, None)
        record["measured_dx_cm"], record["measured_dy_cm"], record["measured_dtheta_deg"] = measured
        return record


# ── Running ──────────────────────────────────────────────────────────────────


class Runner:
    def __init__(self, args: argparse.Namespace, workflow: WorkflowSpec):
        self.args = args
        self.workflow = workflow
        self.client = httpx.Client(base_url=args.labos_url.rstrip("/"), timeout=args.http_timeout)
        self.experiment_id = args.experiment_id
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.journal = Path(args.output) / f"{self.experiment_id}_journal.jsonl"
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.warned_about_empty_timeline = False

    # ── HTTP ────────────────────────────────────────────────────────────────

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def wait_for_run(self, run_id: str) -> dict[str, Any]:
        """Poll until the run reaches a terminal state.

        An E-stop is invisible here: the controller simply stops answering, so
        the run hangs until the operator aborts it from the LabOS page, or
        until --run-timeout expires and this asks for the abort itself.
        """
        deadline = None if self.args.run_timeout <= 0 else time.monotonic() + self.args.run_timeout
        while True:
            state = self.request("GET", f"/api/runs/{run_id}")
            if state.get("status") in TERMINAL:
                return state
            if deadline is not None and time.monotonic() >= deadline:
                print("  timeout del runner: richiedo l'abort.")
                self.request("POST", f"/api/runs/{run_id}/abort")
                return {**state, "status": "error", "error": "runner_timeout"}
            time.sleep(self.args.poll_seconds)

    def fetch_timeline(self, run_id: str) -> list[dict[str, Any]]:
        try:
            payload = self.request("GET", f"/api/runs/{run_id}/steps")
        except Exception as exc:
            print(f"  ATTENZIONE: timeline non recuperata ({exc}); niente dati per-task.")
            return []
        steps = payload.get("steps") or []
        if not steps and not self.warned_about_empty_timeline:
            self.warned_about_empty_timeline = True
            print("  ATTENZIONE: timeline vuota. Senza database LabOS non c'e' analisi per-task.")
        return steps

    # ── The base ────────────────────────────────────────────────────────────

    def place_base(self, position: Position) -> None:
        """Bring the base to an offset *from nominal* - an absolute target.

        The wording matters more than it looks: told to "move by -5" after a
        +5 group, an operator makes a 5 cm move and the whole group is logged
        at the wrong place.
        """
        dx, dy, dtheta = position.dx_cm, position.dy_cm, position.dtheta_deg
        if self.args.move_command:
            command = self.args.move_command.format(dx=dx, dy=dy, dtheta=dtheta)
            print(f"Comando controllore: {command}")
            subprocess.run(shlex.split(command), check=True)
            return
        if self.args.unattended:
            return
        print(f"\nBase dichiarata a dx={dx:g} cm, dy={dy:g} cm, dtheta={dtheta:g} deg "
              f"RISPETTO AL NOMINALE.")
        input("Invio per iniziare, con la base gia' in posizione e l'area sgombra. ")

    # ── Reading the run ─────────────────────────────────────────────────────

    def classify_tasks(self, timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collapse the step timeline into one record per task slot."""
        by_step: dict[int, list[dict[str, Any]]] = {}
        for row in timeline:
            number = row.get("step_number")
            if number is not None:
                by_step.setdefault(int(number), []).append(row)

        records = []
        for slot in self.workflow.slots:
            attempts = sorted(by_step.get(slot.step_number, []),
                              key=lambda r: r.get("attempt") or 1)
            record: dict[str, Any] = {
                "slot": slot.key, "step_number": slot.step_number,
                "instrument": slot.instrument, "action": slot.action,
                "task_id": slot.task_id, "task_name": slot.label,
                "attempts": len(attempts),
                "operator_status": None, "operator_comment": None,
            }
            if not attempts:
                # Never started: an earlier task stopped the loop. Not a failure
                # of this one, and it must not enter this one's denominator.
                record.update(robot_status="not_reached", status="not_reached",
                              duration_s=None, error=None)
                records.append(record)
                continue

            final = attempts[-1]
            status = str(final.get("status") or "unknown").lower()
            result = final.get("result") or {}
            if isinstance(result, dict) and result.get("skipped"):
                status = "skipped"
            elif status == "running":
                # The row never got its completion write: the run died mid-step,
                # which is what an E-stop looks like from here.
                status = "interrupted"
            elif status in STEP_OK:
                status = "success"

            record.update(robot_status=status, status=status,
                          duration_s=final.get("duration_seconds"),
                          error=final.get("error"))
            records.append(record)
        return records

    # ── The operator's verdict ──────────────────────────────────────────────

    def ask_verdict(self, record: dict[str, Any]) -> None:
        """Let the operator correct the controller's account of the run.

        One keystroke for the common case. Anything more is a prompt the
        operator learns to dismiss, and a dismissed prompt manufactures data.
        """
        tasks = record["tasks"]
        reached = [t for t in tasks if t["robot_status"] != "not_reached"]
        print("\n  Il robot riporta:")
        for task in tasks:
            mark = {"success": "ok", "not_reached": "mai raggiunto"}.get(
                task["robot_status"], task["robot_status"].upper())
            print(f"    {task['step_number']}. {task['task_name']:<22} {mark}"
                  + (f"  ({task['error']})" if task.get("error") else ""))

        options = "Invio=tutto ok | numeri dei task falliti (es. 2 o 1,3) | x=prova da scartare"
        while True:
            answer = input(f"  Verdetto? [{options}]\n  > ").strip().lower()
            if answer in ("x", "scarta"):
                record["voided"] = True
                record["outcome"] = "voided"
                record["failure_class"] = "voided_by_operator"
                record["failure_message"] = input("  Motivo dello scarto: ").strip()
                return
            if not answer:
                failed_numbers: set[int] = set()
                break
            try:
                failed_numbers = {int(part) for part in answer.replace(" ", ",").split(",") if part}
            except ValueError:
                print("  Non ho capito. Usa Invio, dei numeri, oppure x.")
                continue
            known = {t["step_number"] for t in tasks}
            unknown = failed_numbers - known
            if unknown:
                print(f"  Task inesistenti: {sorted(unknown)}. Validi: {sorted(known)}")
                continue
            break

        comment = ""
        if failed_numbers:
            comment = input("  Cosa e' successo (facoltativo): ").strip()

        for task in tasks:
            if task["robot_status"] == "not_reached":
                continue
            failed = task["step_number"] in failed_numbers
            task["operator_status"] = "failed" if failed else "ok"
            task["operator_comment"] = comment if failed else None
            # The operator saw the deck; the controller saw its own trajectory.
            task["status"] = "failed" if failed else "success"

        record["verdict_source"] = "operator"
        blind = [t["task_name"] for t in reached
                 if t["robot_status"] == "success" and t["operator_status"] == "failed"]
        record["robot_blind_tasks"] = blind
        if blind:
            print(f"  Nota: il robot dava per riuscito {', '.join(blind)}.")

        if failed_numbers:
            record["outcome"] = "failure"
            record["failure_class"] = "task_failed"
            record["failure_message"] = comment or record.get("failure_message")
        elif record["outcome"] != "success":
            # The operator overrode a failure LabOS reported. Rare, but if the
            # loop really did finish correctly the trial is a success.
            record["outcome"] = "success"
            record["failure_class"] = None

    # ── One trial ───────────────────────────────────────────────────────────

    def run_trial(self, position: Position, trial_number: int) -> tuple[bool, dict[str, Any]]:
        """Execute the workflow once. Returns (continue_session, record)."""
        started = time.monotonic()
        record: dict[str, Any] = {
            "kind": "trial",
            "experiment_id": self.experiment_id,
            "session_id": self.session_id,
            "trial": trial_number,
            "started_at": now(),
            "workflow_name": self.workflow.name,
            "workflow_file": str(self.workflow.path),
            "workflow_sha1": self.workflow.sha1,
            **position.as_record(),
            "labos_run_id": None, "run_status": None,
            "outcome": "not_started", "failure_class": None, "failure_message": None,
            "steps_failed": None, "steps_skipped": None,
            "verdict_source": "robot", "voided": False, "robot_blind_tasks": [],
            "tasks": [],
        }

        try:
            response = self.request("POST", "/api/workflows/execute", json=self.workflow.body)
            if response.get("status") == "busy":
                raise RuntimeError(response.get("error", "LabOS is busy"))
            run_id = response.get("run_id")
            if not run_id:
                raise RuntimeError(f"LabOS non ha restituito run_id: {response}")
            record["labos_run_id"] = run_id

            state = self.wait_for_run(run_id)
            status = str(state.get("status"))
            record["run_status"] = status
            record["tasks"] = self.classify_tasks(self.fetch_timeline(run_id))

            result = state.get("result") or {}
            failed, skipped = result.get("steps_failed"), result.get("steps_skipped")
            record["steps_failed"], record["steps_skipped"] = failed, skipped

            if status == "aborted":
                record.update(outcome="failure", failure_class="aborted",
                              failure_message=state.get("message") or state.get("error"))
            elif status != "completed":
                record.update(outcome="failure", failure_class=status,
                              failure_message=state.get("message") or state.get("error"))
            elif skipped:
                # "completed" with a skipped step means a failure was waved
                # through by hand. Counting it as a success would quietly
                # inflate every rate on the map.
                record.update(outcome="failure", failure_class="operator_skip",
                              failure_message="; ".join(result.get("notes") or []) or "step saltato")
            elif failed:
                record.update(outcome="failure", failure_class="steps_failed",
                              failure_message="; ".join(result.get("notes") or []))
            else:
                record["outcome"] = "success"

            if not self.args.unattended:
                self.ask_verdict(record)

        except Exception as exc:
            # An HTTP or runner fault says nothing about the robot's geometry.
            # Kept in the journal, excluded from the rates by failure_class.
            record.update(outcome="runner_error",
                          failure_class="communication_or_runner_error",
                          failure_message=str(exc))

        record["ended_at"] = now()
        record["duration_s"] = round(time.monotonic() - started, 3)
        self.append(record)
        return self.decide_whether_to_continue(record), record

    def decide_whether_to_continue(self, record: dict[str, Any]) -> bool:
        if record["outcome"] in ("success", "voided"):
            return True
        if self.args.unattended:
            return True
        print(f"\n  Prova registrata come FALLITA ({record['failure_class']}).")
        print("  La cella puo' essere in uno stato non noto: verifica piastra, pinza e deck,")
        print("  e riporta tutto nella condizione di partenza prima di proseguire.")
        return input("  Continuare? [y/N] ").strip().lower() == "y"

    def append(self, record: dict[str, Any]) -> None:
        with self.journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")

    # ── Session ─────────────────────────────────────────────────────────────

    def run(self, positions: list[Position]) -> None:
        for position in positions:
            self.place_base(position)
            for trial_number in range(1, self.args.repeats + 1):
                print(f"\n[{position.id}] prova {trial_number}/{self.args.repeats}", flush=True)
                keep_going, record = self.run_trial(position, trial_number)
                if record["outcome"] == "success":
                    print(f"  ok ({record['duration_s']:.0f}s)")
                if not keep_going:
                    print("\nSessione interrotta. Il journal e' completo fino a qui.")
                    return


# ── Report: rebuilt from the journal, never from memory ──────────────────────


@dataclass
class TaskStats:
    reached: int = 0
    not_reached: int = 0
    ok: int = 0
    first_pass_ok: int = 0
    failed: int = 0
    retried: int = 0
    robot_blind: int = 0
    durations: list[float] = field(default_factory=list)


def read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("kind") == "trial":
            records.append(record)
    return records


TRIAL_FIELDS = ["experiment_id", "session_id", "trial", "position_id", "dx_cm", "dy_cm",
                "dtheta_deg", "measured_dx_cm", "measured_dy_cm", "measured_dtheta_deg",
                "note", "workflow_name", "workflow_sha1", "labos_run_id", "run_status",
                "outcome", "failure_class", "failure_message", "verdict_source", "voided",
                "steps_failed", "steps_skipped", "started_at", "ended_at", "duration_s"]

TASK_FIELDS = ["experiment_id", "session_id", "trial", "position_id", "dx_cm", "dy_cm",
               "dtheta_deg", "measured_dx_cm", "measured_dy_cm", "measured_dtheta_deg",
               "slot", "step_number", "task_name", "task_id", "instrument", "action",
               "status", "robot_status", "operator_status", "operator_comment",
               "attempts", "duration_s", "error"]

SHARED = ("experiment_id", "session_id", "trial", "position_id", "dx_cm", "dy_cm",
          "dtheta_deg", "measured_dx_cm", "measured_dy_cm", "measured_dtheta_deg")


def build_report(journal: Path, output: Path, experiment_id: str, threshold: float) -> dict[str, Any]:
    trials = read_journal(journal)
    if not trials:
        return {}

    with (output / f"{experiment_id}_trials.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRIAL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(trials)

    # Every task row carries its own position, so this file is a standalone map.
    task_rows = []
    for trial in trials:
        shared = {key: trial.get(key) for key in SHARED}
        for task in trial.get("tasks", []):
            task_rows.append({**shared, **task})
    with (output / f"{experiment_id}_tasks.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TASK_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(task_rows)

    summary: dict[str, Any] = {"experiment_id": experiment_id, "generated_at": now(),
                               "safe_threshold": threshold, "positions": []}

    by_position: dict[str, list[dict[str, Any]]] = {}
    for trial in trials:
        by_position.setdefault(trial["position_id"], []).append(trial)

    for pid, group in sorted(by_position.items()):
        # A comms fault is not a geometric outcome, and a voided trial is one
        # the operator disowned. Both stay in the journal, out of the rates.
        counted = [t for t in group
                   if t["outcome"] not in ("runner_error", "voided", "not_started")]
        successes = sum(t["outcome"] == "success" for t in counted)
        lower, upper = wilson(successes, len(counted))
        first = group[0]

        tasks: dict[str, TaskStats] = {}
        order: dict[str, dict[str, Any]] = {}
        for trial in counted:
            for task in trial.get("tasks", []):
                stats = tasks.setdefault(task["slot"], TaskStats())
                order.setdefault(task["slot"], task)
                if task["status"] == "not_reached":
                    stats.not_reached += 1
                    continue
                stats.reached += 1
                attempts = task.get("attempts") or 1
                if attempts > 1:
                    stats.retried += 1
                if task["robot_status"] == "success" and task.get("operator_status") == "failed":
                    stats.robot_blind += 1
                if task["status"] == "success":
                    stats.ok += 1
                    if attempts == 1:
                        stats.first_pass_ok += 1
                    if task.get("duration_s") is not None:
                        stats.durations.append(float(task["duration_s"]))
                else:
                    stats.failed += 1

        task_report = []
        for slot in sorted(tasks, key=lambda s: order[s]["step_number"]):
            stats = tasks[slot]
            f_lower, f_upper = wilson(stats.first_pass_ok, stats.reached)
            task_report.append({
                "slot": slot,
                "task_name": order[slot]["task_name"],
                "step_number": order[slot]["step_number"],
                "attempted": stats.reached,
                "not_reached": stats.not_reached,
                # The headline number: recovering on a retry is not reliability.
                "first_pass_ok": stats.first_pass_ok,
                "ok_including_retries": stats.ok,
                "needed_retry": stats.retried,
                "failed": stats.failed,
                # How often the controller called a failure a success. A high
                # number here means the map cannot be built unattended.
                "robot_reported_ok_but_failed": stats.robot_blind,
                "first_pass_rate": (round(stats.first_pass_ok / stats.reached, 5)
                                    if stats.reached else None),
                "rate_including_retries": (round(stats.ok / stats.reached, 5)
                                           if stats.reached else None),
                "wilson95_lower": round(f_lower, 5),
                "wilson95_upper": round(f_upper, 5),
                "classification": (
                    "undetermined" if not stats.reached else
                    "safe_confirmed" if f_lower > threshold else
                    "unsafe_confirmed" if f_upper < threshold else "undetermined"),
                # Duration drifts before the rate does: at the edge of the map a
                # task usually gets slower and more variable first.
                "duration_median_s": (round(statistics.median(stats.durations), 3)
                                      if stats.durations else None),
                "duration_p95_s": percentile(stats.durations, 0.95),
            })

        summary["positions"].append({
            "position_id": pid,
            "dx_cm": first["dx_cm"], "dy_cm": first["dy_cm"], "dtheta_deg": first["dtheta_deg"],
            "measured_dx_cm": first.get("measured_dx_cm"),
            "measured_dy_cm": first.get("measured_dy_cm"),
            "measured_dtheta_deg": first.get("measured_dtheta_deg"),
            "note": first.get("note", ""),
            "sessions": sorted({t["session_id"] for t in group}),
            "trials": len(counted),
            "excluded": len(group) - len(counted),
            "successes": successes,
            "failures": len(counted) - successes,
            "success_rate": round(successes / len(counted), 5) if counted else None,
            "wilson95_lower": round(lower, 5),
            "wilson95_upper": round(upper, 5),
            "classification": ("safe_confirmed" if lower > threshold else
                               "unsafe_confirmed" if upper < threshold else "undetermined"),
            "tasks": task_report,
        })

    (output / f"{experiment_id}_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def print_report(summary: dict[str, Any]) -> None:
    if not summary:
        print("Nessun dato nel journal.")
        return
    for position in summary["positions"]:
        print(f"\n{position['position_id']}  (dx={position['dx_cm']:g} "
              f"dy={position['dy_cm']:g} dtheta={position['dtheta_deg']:g})")
        print(f"  ciclo completo: {position['successes']}/{position['trials']} "
              f"[{position['wilson95_lower']:.3f}-{position['wilson95_upper']:.3f}] "
              f"{position['classification']}")
        for task in position["tasks"]:
            rate = task["first_pass_rate"]
            rate_text = "n/d" if rate is None else f"{rate:.3f}"
            median = task["duration_median_s"]
            median_text = "n/d" if median is None else f"{median:g}s"
            blind = (f"  robot-cieco={task['robot_reported_ok_but_failed']}"
                     if task["robot_reported_ok_but_failed"] else "")
            print(f"    {task['task_name']:<22} "
                  f"1o colpo {task['first_pass_ok']}/{task['attempted']} ({rate_text})"
                  f"  retry={task['needed_retry']}"
                  f"  mai raggiunto={task['not_reached']}"
                  f"  mediana={median_text}{blind}")


# ── CLI ──────────────────────────────────────────────────────────────────────


def positions_from_args(args: argparse.Namespace) -> list[Position]:
    measured = parse_offset(args.measured) if args.measured else None
    if args.positions:
        if measured:
            raise SystemExit("--measured descrive una posizione sola; usalo con --position.")
        return [Position(*parse_offset(row), note=args.note)
                for row in args.positions.split(";") if row.strip()]

    text = args.position
    while not text:
        # Never default to the nominal. A forgotten --position would file a
        # whole session under dx0_dy0_dt0 and corrupt the map in silence.
        if args.unattended:
            raise SystemExit("--position e' obbligatorio con --unattended.")
        text = input("Posizione della base rispetto al nominale (dx,dy,dtheta in cm,cm,deg): ").strip()
        try:
            parse_offset(text)
        except ValueError as exc:
            print(f"  {exc}")
            text = ""
    return [Position(*parse_offset(text), measured=measured, note=args.note)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mappa di affidabilita per-task di un workflow a ciclo chiuso, "
                    "in funzione della posizione della base.")
    parser.add_argument("workflow", nargs="?", help="percorso del workflow JSON di LabOS")
    parser.add_argument("--position", help="offset dal nominale: dx,dy,dtheta (cm,cm,deg)")
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--measured", help="offset realmente misurato, se diverso da --position")
    parser.add_argument("--note", default="", help="testo libero salvato su ogni riga della sessione")
    parser.add_argument("--experiment-id", default="reliability_map",
                        help="le sessioni con lo stesso id formano una sola mappa")
    parser.add_argument("--output", default="Results/robot_reliability")
    parser.add_argument("--unattended", action="store_true",
                        help="nessuna domanda: si fida di quello che riporta il robot")
    parser.add_argument("--report-only", action="store_true",
                        help="ricostruisce CSV e summary dal journal, senza eseguire nulla")
    parser.add_argument("--positions", help="multi-posizione automatico: dx,dy,dt;dx,dy,dt")
    parser.add_argument("--move-command", help="es. 'ssh pc python move.py {dx} {dy} {dtheta}'")
    parser.add_argument("--labos-url", default="http://127.0.0.1:5000")
    parser.add_argument("--workflow-name")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--run-timeout", type=float, default=1800.0)
    parser.add_argument("--http-timeout", type=float, default=30.0)
    parser.add_argument("--safe-threshold", type=float, default=0.90)
    args = parser.parse_args()

    if not args.report_only and not args.workflow:
        parser.error("serve il workflow, a meno di --report-only")
    if args.repeats < 1 or not 0 < args.safe_threshold < 1:
        parser.error("repeats >= 1 e safe-threshold fra 0 e 1")
    if args.position and args.positions:
        parser.error("usa --position (una sessione) oppure --positions (automatico), non entrambi")
    return args


def main() -> None:
    args = parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    journal = output / f"{args.experiment_id}_journal.jsonl"

    if not args.report_only:
        workflow = WorkflowSpec.load(Path(args.workflow), args.workflow_name)
        print(f"Workflow '{workflow.name}' ({workflow.sha1}), {len(workflow.slots)} task:")
        for slot in workflow.slots:
            print(f"  {slot.step_number}. {slot.label}")
        Runner(args, workflow).run(positions_from_args(args))

    summary = build_report(journal, output, args.experiment_id, args.safe_threshold)
    print_report(summary)
    print(f"\nJournal:  {journal.resolve()}")
    print(f"Report:   {output.resolve()}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrotto dall'operatore.", file=sys.stderr)
        raise SystemExit(130)
