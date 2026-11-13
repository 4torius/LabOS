"""Start, drive and read back a simulated lab.

    with SimLab() as lab:
        result = asyncio.run(lab.run_workflow("Test_Bicocca"))
        assert not lab.overlapping("deck:opentrons")

`SimLab` starts one `sim_server.py` per instrument on loopback ports of its
own, writes a copy of `lab_config.yaml` that points at them (and at nothing
else), and sets `LABOS_CONFIG` so the orchestrator in this process uses it.

The real configuration file is never written to, mDNS discovery is switched
off in the copy, and every simulated server is bound to 127.0.0.1 and not
announced - a run against the simulated lab cannot reach an instrument.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

SIM_DIR = Path(__file__).resolve().parent
V1_DIR = SIM_DIR.parent


@dataclass(frozen=True)
class SimServer:
    key: str            # the key under servers: in lab_config.yaml
    directory: str      # the real server's folder, relative to v1/
    name: str           # the name the real server announces
    type: str
    port: int
    augment: str = ""


#: The lab as it stands at Bicocca. Names are the ones the real servers
#: announce - which differ from the keys, and that difference is exactly what
#: an earlier bug hid behind test doubles that used one string for both.
BICOCCA: Tuple[SimServer, ...] = (
    SimServer("tecan", "SiLA2/TecanM200SiLA2Server", "TecanM200Pro", "PlateReader", 51051),
    SimServer("opentrons", "SiLA2/OpentronsSiLA2Server", "OpentronsFlex", "LiquidHandler", 51302),
    SimServer("mobile", "SiLA2/MobileSiLA2Server", "GoFaGo Mobile Robot", "MobileManipulator",
              51053, augment="src.task_resources:augmenter_from_tasks_file"),
    SimServer("manual_station", "SiLA2/ManualStationSiLA2Server", "ManualStation",
              "OperatorStation", 51500),
)


@dataclass(frozen=True)
class Interval:
    server: str
    command: str
    start: float
    end: float
    status: str
    parameters: Dict[str, Any]

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end

    def __str__(self) -> str:
        detail = self.parameters.get("TaskId") or self.parameters.get("RecipeName") or ""
        detail = _task_names().get(str(detail), detail)
        return f"{self.server}.{self.command}{'(' + str(detail) + ')' if detail else ''}"


class SimLab:
    """The simulated instruments, as a context manager."""

    def __init__(self, servers: Iterable[SimServer] = BICOCCA, *,
                 scenario: Optional[Dict[str, Any]] = None,
                 max_concurrency: Optional[int] = None,
                 retry_on_failure: bool = False,
                 workdir: Optional[Path] = None):
        self.servers = tuple(servers)
        self.scenario = scenario
        self.max_concurrency = max_concurrency
        self.retry_on_failure = retry_on_failure
        self._owned_tmp = None
        if workdir is None:
            self._owned_tmp = tempfile.TemporaryDirectory(prefix="labos-sim-")
            workdir = Path(self._owned_tmp.name)
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.timeline_path = self.workdir / "timeline.jsonl"
        self.config_path = self.workdir / "lab_config.sim.yaml"
        self.scenario_path = self.workdir / "scenario.json"
        self._processes: List[subprocess.Popen] = []
        self._previous_config: Optional[str] = None

    # ── configuration ────────────────────────────────────────────────────────

    def write_config(self) -> Path:
        """A copy of the real configuration, pointed at the simulated servers."""
        config = yaml.safe_load((V1_DIR / "lab_config.yaml").read_text(encoding="utf-8")) or {}
        simulated = {s.key: s for s in self.servers}
        for key, spec in (config.get("servers") or {}).items():
            spec = spec if isinstance(spec, dict) else {}
            config["servers"][key] = spec
            if key in simulated:
                spec.update(host="127.0.0.1", port=simulated[key].port,
                            enabled=True, remote=True)
            else:
                spec["enabled"] = False       # never fall through to a real one
        config.setdefault("discovery", {})["enabled"] = False
        workflow = config.setdefault("workflow", {})
        workflow.pop("parallel_execution", None)
        workflow.pop("max_concurrency", None)
        # 0 is "no cap". Leaving the key out would not be: a configuration
        # that says nothing runs one step at a time, on purpose.
        workflow["max_concurrency"] = int(self.max_concurrency or 0)
        workflow["retry_on_failure"] = bool(self.retry_on_failure)
        config.setdefault("error_handling", {})["enable_human_intervention"] = False
        # Whatever a simulated run records is kept out of the real database.
        config.setdefault("paths", {})["database"] = "Results/labos.sim.db"
        self.config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return self.config_path

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, timeout: float = 30.0) -> "SimLab":
        scenario = self.scenario
        if scenario is None:
            scenario = json.loads((SIM_DIR / "scenario.default.json").read_text(encoding="utf-8"))
        self.scenario_path.write_text(json.dumps(_with_robot_tasks(scenario)), encoding="utf-8")
        self.timeline_path.write_text("", encoding="utf-8")
        self.write_config()

        env = dict(os.environ)
        env["MOBILE_SIM_TASKS"] = str(SIM_DIR / "mobile_tasks.json")
        env["LABOS_ROOT"] = str(V1_DIR)
        env.pop("LABOS_CONFIG", None)
        for server in self.servers:
            if _port_open(server.port):
                self.stop()
                raise RuntimeError(
                    f"port {server.port} is already in use - is another simulated "
                    f"lab running? ({server.key})")
            command = [sys.executable, str(SIM_DIR / "sim_server.py"),
                       "--server-dir", str(V1_DIR / server.directory),
                       "--key", server.key, "--name", server.name, "--type", server.type,
                       "--port", str(server.port),
                       "--scenario", str(self.scenario_path),
                       "--timeline", str(self.timeline_path)]
            if server.augment:
                command += ["--augment", server.augment]
            log = open(self.workdir / f"{server.key}.log", "w", encoding="utf-8")
            self._processes.append(subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=str(V1_DIR)))

        deadline = time.time() + timeout
        waiting = {s.key: s for s in self.servers}
        while waiting and time.time() < deadline:
            for key, server in list(waiting.items()):
                if _port_open(server.port):
                    del waiting[key]
            for process in self._processes:
                if process.poll() is not None:
                    self.stop()
                    raise RuntimeError(
                        "a simulated server exited while starting - see the logs in "
                        f"{self.workdir}")
            time.sleep(0.1)
        if waiting:
            self.stop()
            raise RuntimeError(f"simulated server(s) did not start: {', '.join(waiting)}")

        self._previous_config = os.environ.get("LABOS_CONFIG")
        os.environ["LABOS_CONFIG"] = str(self.config_path)
        _reload_settings()
        return self

    def reset(self, scenario: Optional[Dict[str, Any]] = None) -> None:
        """Empty the timeline and, optionally, change what the instruments do.

        The running servers pick the new scenario up on their next command.
        """
        if scenario is None:
            scenario = json.loads((SIM_DIR / "scenario.default.json").read_text(encoding="utf-8"))
        temporary = self.scenario_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(_with_robot_tasks(scenario)), encoding="utf-8")
        os.replace(temporary, self.scenario_path)
        self.timeline_path.write_text("", encoding="utf-8")

    def stop(self) -> None:
        for process in self._processes:
            if process.poll() is None:
                process.terminate()
        for process in self._processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        self._processes = []
        if os.environ.get("LABOS_CONFIG") == str(self.config_path):
            if self._previous_config is None:
                os.environ.pop("LABOS_CONFIG", None)
            else:
                os.environ["LABOS_CONFIG"] = self._previous_config
            _reload_settings()
        if self._owned_tmp is not None:
            self._owned_tmp.cleanup()
            self._owned_tmp = None

    def __enter__(self) -> "SimLab":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ── the orchestrator, in this process ────────────────────────────────────

    async def orchestrator(self):
        """(LabCore, registry, executor) wired exactly as `labos.py run` does."""
        from src.client import PnPRegistry
        from src.lab_core import LabCore
        from src.workflow import PnPWorkflowExecutor

        core = LabCore(V1_DIR)
        await core.discover(timeout=3.0)
        registry = PnPRegistry(V1_DIR)
        for instrument in core.list_instruments():
            if getattr(instrument, "_server", None):
                registry.register(instrument.id, instrument._server)
        return core, registry, PnPWorkflowExecutor(registry, V1_DIR)

    async def run_workflow(self, workflow, *, validate: bool = True, executor=None, **kwargs):
        """Run a workflow by name, path, dict or object. Returns the result."""
        from src.workflow import Workflow

        if executor is None:
            _core, _registry, executor = await self.orchestrator()
        if isinstance(workflow, (str, Path)):
            path = Path(workflow)
            if not path.is_file():
                path = V1_DIR / "Library" / "Workflows" / f"{workflow}.workflow.json"
            workflow = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(workflow, dict):
            workflow = Workflow.from_dict(workflow)
        return await executor.execute(workflow, validate=validate, **kwargs)

    # ── what actually happened ───────────────────────────────────────────────

    def events(self) -> List[Dict[str, Any]]:
        if not self.timeline_path.exists():
            return []
        lines = self.timeline_path.read_text(encoding="utf-8").splitlines()
        return sorted((json.loads(line) for line in lines if line.strip()),
                      key=lambda e: e["t"])

    def intervals(self) -> List[Interval]:
        """Every command that occupied an instrument, start to end."""
        open_: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        out: List[Interval] = []
        for event in self.events():
            key = (event["server"], event["command"])
            if event["event"] == "start":
                open_.setdefault(key, []).append(event)
            elif event["event"] == "end" and open_.get(key):
                begun = open_[key].pop(0)
                out.append(Interval(event["server"], event["command"], begun["t"], event["t"],
                                    event.get("status", "ok"), begun.get("parameters") or {}))
        for (server, command), pending in open_.items():
            for begun in pending:      # still running when the timeline was read
                out.append(Interval(server, command, begun["t"], float("inf"),
                                    "running", begun.get("parameters") or {}))
        return sorted(out, key=lambda i: i.start)

    def commands(self, server: Optional[str] = None) -> List[str]:
        """`server.Command` for every command started, in start order."""
        return [f"{e['server']}.{e['command']}" for e in self.events()
                if e["event"] == "start" and server in (None, e["server"])]

    def overlapping(self, resource: str) -> List[Tuple[Interval, Interval]]:
        """Pairs of commands that were in flight together while both occupied `resource`.

        Decided from the declarations the simulated servers actually served,
        resolved the way the orchestrator resolves them - but measured on the
        instruments' own clocks, so it does not take the scheduler's word for
        what it did.
        """
        topology, keys = self._topology()
        occupying = []
        for interval in self.intervals():
            claims = topology.claims_for(interval.server, interval.command, interval.parameters)
            if resource in claims:
                occupying.append(interval)
        return [(a, b) for i, a in enumerate(occupying) for b in occupying[i + 1:]
                if a.overlaps(b)]

    def any_overlap(self) -> List[Tuple[Interval, Interval, str]]:
        """Every pair that overlapped on any resource both of them claim."""
        topology, _ = self._topology()
        intervals = self.intervals()
        found = []
        for i, a in enumerate(intervals):
            claims_a = topology.claims_for(a.server, a.command, a.parameters)
            for b in intervals[i + 1:]:
                if not a.overlaps(b):
                    continue
                shared = claims_a & topology.claims_for(b.server, b.command, b.parameters)
                for resource in sorted(shared):
                    found.append((a, b, resource))
        return found

    def ran_together(self) -> List[Tuple[Interval, Interval]]:
        """Pairs of commands on different instruments that were in flight together."""
        intervals = self.intervals()
        return [(a, b) for i, a in enumerate(intervals) for b in intervals[i + 1:]
                if a.server != b.server and a.overlaps(b)]

    def _topology(self):
        from src.catalog import parse_catalog
        from src.resources import topology_of
        from sila2.client import SilaClient

        entries = []
        for server in self.servers:
            client = SilaClient("127.0.0.1", server.port, insecure=True)
            try:
                raw = str(client.LabOSLibraryProvider.LibraryCatalog.get())
            finally:
                client.close()
            entries.append((server.key, server.name, parse_catalog(raw).resources))
        return topology_of(entries), [s.key for s in self.servers]


def _with_robot_tasks(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """Give the simulated robot the task list the real one last reported.

    LabOS keeps the robot's task list in Library/MobileTasks/_options.json and
    rewrites it whenever the robot answers. A simulated robot answering with an
    empty list would overwrite the real lab's snapshot with nothing; answering
    with the snapshot itself leaves it as it was.
    """
    scenario = json.loads(json.dumps(scenario))
    properties = scenario.setdefault("mobile", {}).setdefault("properties", {})
    if "AvailableTasks" not in properties:
        try:
            snapshot = json.loads((V1_DIR / "Library" / "MobileTasks" / "_options.json")
                                  .read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            snapshot = []
        properties["AvailableTasks"] = [
            {"TaskId": item["TaskId"], "TaskName": item["TaskName"],
             "SubtaskCount": int(item.get("SubtaskCount", 1))}
            for item in snapshot if isinstance(item, dict) and item.get("TaskId")]
    return scenario


_TASK_NAMES: Optional[Dict[str, str]] = None


def _task_names() -> Dict[str, str]:
    """Robot task id -> its name, so a timeline reads `place_on_OT`."""
    global _TASK_NAMES
    if _TASK_NAMES is None:
        try:
            tasks = json.loads((SIM_DIR / "mobile_tasks.json").read_text(encoding="utf-8"))
            _TASK_NAMES = {str(t["id"]): str(t["name"]) for t in tasks}
        except (OSError, ValueError, KeyError):
            _TASK_NAMES = {}
    return _TASK_NAMES


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _reload_settings() -> None:
    if str(V1_DIR) not in sys.path:
        sys.path.insert(0, str(V1_DIR))
    from src.settings import reload_settings
    reload_settings()
