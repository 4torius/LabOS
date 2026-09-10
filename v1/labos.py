#!/usr/bin/env python3
"""LabOS command line.

    python labos.py status                    what is running
    python labos.py instruments               what is on the network, and what it can do
    python labos.py validate <workflow>       check a workflow without running it
    python labos.py run <workflow>            run one, streaming progress
    python labos.py read <instrument> <prop>  read a property
    python labos.py new-server <Name>         scaffold a new instrument
    python labos.py codegen <server>          regenerate bindings after an FDL edit
    python labos.py doctor                    check the installation

Why this exists: adding an instrument was a nine-step manual procedure —
copy a folder, rename four files, edit three of them consistently, run codegen
into a temp directory and copy the inner package back out, then remember to
register it. Every one of those steps is a chance to get it subtly wrong, and
the failure mode is a server that starts and then does not appear. `new-server`
does the whole thing and tells you the one file you actually have to write.

Nothing here needs the webapp to be running. `run` drives the same executor
the web interface uses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# Box-drawing and status glyphs die on a non-UTF8 console (piped output, a
# service, CI). Same guard launcher.py and webapp/app.py use.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  Output
# ─────────────────────────────────────────────────────────────────────────────

class C:
    RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
    RED, GREEN, YELLOW, CYAN = "\033[91m", "\033[92m", "\033[93m", "\033[96m"


def ok(msg: str) -> None:
    print(f"  {C.GREEN}OK{C.RESET}   {msg}")


def bad(msg: str) -> None:
    print(f"  {C.RED}FAIL{C.RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {C.YELLOW}WARN{C.RESET} {msg}")


def info(msg: str) -> None:
    print(f"       {msg}")


def heading(msg: str) -> None:
    print(f"\n{C.BOLD}{msg}{C.RESET}")


# ─────────────────────────────────────────────────────────────────────────────
#  status
# ─────────────────────────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> int:
    import socket
    from src.settings import get_settings

    settings = get_settings(BASE_DIR)

    def reachable(host: str, port: int, timeout: float = 0.5) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                return sock.connect_ex((host, port)) == 0
        except OSError:
            return False

    heading("SiLA2 servers")
    any_down = False
    for spec in settings.servers.values():
        if not spec.enabled:
            info(f"{spec.name:<24} disabled in lab_config.yaml")
            continue
        if reachable(spec.host, spec.port):
            ok(f"{spec.name:<24} {spec.address}")
        else:
            bad(f"{spec.name:<24} {spec.address} not reachable")
            any_down = True

    heading("Web interface")
    if reachable("localhost", 5000):
        ok("http://localhost:5000")
    else:
        bad("not running  (start it with: python launcher.py --webapp)")
        any_down = True

    return 1 if any_down else 0


# ─────────────────────────────────────────────────────────────────────────────
#  instruments
# ─────────────────────────────────────────────────────────────────────────────

async def _discover():
    from src.lab_core import get_lab_core
    core = get_lab_core(BASE_DIR)
    await core.discover(timeout=3.0)
    return core


def cmd_instruments(args: argparse.Namespace) -> int:
    core = asyncio.run(_discover())
    instruments = core.list_instruments()
    if not instruments:
        bad("No instrument discovered. Are the servers running? (python labos.py status)")
        return 1

    for inst in instruments:
        state = f"{C.GREEN}online{C.RESET}" if inst.status == "online" else f"{C.RED}{inst.status}{C.RESET}"
        heading(f"{inst.name}  [{inst.id}]  {state}  {inst.host}:{inst.port}")

        if inst.properties:
            print(f"  {C.DIM}properties (read-only){C.RESET}")
            for prop in inst.properties:
                mark = " (observable)" if prop.observable else ""
                print(f"    {prop.id:<22} {prop.data_type}{mark}")

        if inst.commands:
            print(f"  {C.DIM}commands{C.RESET}")
            for cmd in inst.commands:
                params = ", ".join(
                    f"{p.name}{'' if p.required else '?'}" for p in cmd.parameters
                )
                moves = ""
                if "MOVES HARDWARE" in (cmd.description or ""):
                    moves = f"  {C.YELLOW}moves hardware{C.RESET}"
                print(f"    {cmd.id:<22} ({params}){moves}")

        if args.verbose:
            for cmd in inst.commands:
                if cmd.description:
                    print(f"\n    {C.DIM}{cmd.id}: "
                          f"{' '.join(cmd.description.split())[:300]}{C.RESET}")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    """Read one property, so a check does not require opening a browser."""
    core = asyncio.run(_discover())
    match = core.resolve_instrument(args.instrument)
    if not match:
        bad(match.error)
        return 1
    instrument = match.target

    candidates = [p for p in instrument.properties if p.id.lower() == args.property.lower()]
    if not candidates:
        bad(f"'{args.property}' is not a property of {instrument.name}. "
            f"Available: {', '.join(p.id for p in instrument.properties) or 'none'}")
        return 1
    prop = candidates[0]

    result = asyncio.run(core.get_property(instrument.id, prop.feature, prop.id))
    if not result.success:
        bad(result.error)
        return 1

    value = result.data
    if isinstance(value, str) and value.strip()[:1] in "[{":
        try:
            value = json.loads(value)
        except ValueError:
            pass
    print(json.dumps(value, indent=2) if not isinstance(value, str) else value)
    return 0


# ─────────────────────────────────────────────────────────────────────────────
#  validate / run
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_workflow(name: str) -> Optional[Path]:
    from src.settings import get_settings
    directory = get_settings(BASE_DIR).workflows_dir
    for candidate in (Path(name), directory / name,
                      directory / f"{name}.workflow.json", directory / f"{name}.json"):
        if candidate.is_file():
            return candidate
    return None


async def _build_registry(core):
    from src.client import PnPRegistry
    registry = PnPRegistry(BASE_DIR)
    for instrument in core.list_instruments():
        if getattr(instrument, "_server", None):
            registry.register(instrument.id, instrument._server)
    return registry


def cmd_validate(args: argparse.Namespace) -> int:
    from src.workflow import Workflow, PnPWorkflowExecutor

    path = _resolve_workflow(args.workflow)
    if path is None:
        bad(f"Workflow '{args.workflow}' not found")
        return 1
    info(f"{path}")

    async def _run() -> List:
        core = await _discover()
        registry = await _build_registry(core)
        workflow = Workflow.from_dict(json.loads(path.read_text(encoding="utf-8-sig")))
        return await PnPWorkflowExecutor(registry, BASE_DIR).validate_workflow(workflow)

    errors = asyncio.run(_run())
    if not errors:
        ok("valid — every instrument, command and parameter resolves")
        return 0
    for error in errors:
        bad(f"step {error.step_number} · {error.field}: {error.message}")
    return 1


def cmd_run(args: argparse.Namespace) -> int:
    from src.workflow import Workflow, PnPWorkflowExecutor, StepStatus

    path = _resolve_workflow(args.workflow)
    if path is None:
        bad(f"Workflow '{args.workflow}' not found")
        return 1

    data = json.loads(path.read_text(encoding="utf-8-sig"))
    workflow_name = data.get("WorkflowName", path.stem)

    print(f"\n{C.BOLD}{workflow_name}{C.RESET}  ({len(data.get('Steps', []))} steps)")
    print(f"{C.DIM}{path}{C.RESET}")
    if not args.yes:
        warn("This drives real instruments and can move hardware.")
        if input("  Type 'run' to continue: ").strip().lower() != "run":
            info("Cancelled.")
            return 130

    async def _run() -> int:
        core = await _discover()
        registry = await _build_registry(core)
        executor = PnPWorkflowExecutor(registry, BASE_DIR)

        def on_progress(progress) -> None:
            symbol = {
                StepStatus.RUNNING: f"{C.CYAN}>{C.RESET}",
                StepStatus.SUCCESS: f"{C.GREEN}v{C.RESET}",
                StepStatus.FAILED: f"{C.RED}x{C.RESET}",
                StepStatus.SKIPPED: f"{C.YELLOW}-{C.RESET}",
            }.get(progress.step_status, " ")
            print(f"  {symbol} [{progress.current_step}/{progress.total_steps}] "
                  f"{progress.step_instrument}.{progress.step_action}  "
                  f"{C.DIM}{progress.message}{C.RESET}")

        executor.add_progress_callback(on_progress)
        workflow = Workflow.from_dict(data)
        result = await executor.execute(workflow, validate=True,
                                        parallel=args.parallel)

        print()
        if result.success:
            ok(f"{result.steps_completed} step(s) in {result.duration_seconds:.1f}s")
            return 0
        bad(f"{result.steps_failed} failed, {result.steps_skipped} skipped")
        for error in result.errors:
            info(error)
        return 1

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        print()
        warn("Interrupted. The instruments were NOT told to stop — "
             "use the web interface's Abort, or stop them at the instrument.")
        return 130


# ─────────────────────────────────────────────────────────────────────────────
#  new-server
# ─────────────────────────────────────────────────────────────────────────────

TEMPLATE_DIR = BASE_DIR / "SiLA2" / "_NewInstrumentTemplate"


def _next_free_port(taken: set) -> int:
    for port in range(50061, 50100):
        if port not in taken:
            return port
    raise RuntimeError("No free port left in the 50061-50099 range")


def cmd_new_server(args: argparse.Namespace) -> int:
    from src.settings import get_settings

    name = args.name
    if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", name):
        bad("Name must be CamelCase, starting with a capital: Centrifuge, PlateSealer")
        return 1

    settings = get_settings(BASE_DIR)
    key = args.key or re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    port = args.port or _next_free_port({s.port for s in settings.servers.values()})
    folder = BASE_DIR / "SiLA2" / f"{name}SiLA2Server"
    lower = name.lower()

    if folder.exists():
        bad(f"{folder} already exists")
        return 1
    if key in settings.servers:
        bad(f"lab_config.yaml already has a server called '{key}' — pass --key")
        return 1

    heading(f"Creating {name}SiLA2Server  (key '{key}', port {port})")

    # 1 ── copy the template, minus its generated feature package
    shutil.copytree(TEMPLATE_DIR, folder,
                    ignore=shutil.ignore_patterns("__pycache__", "newinstrument"))
    ok(f"copied template  ->  SiLA2/{folder.name}/")

    # 2 ── rename and retarget the feature definition
    fdl = folder / "features" / f"{name}.sila.xml"
    (folder / "features" / "NewInstrument.sila.xml").rename(fdl)
    text = fdl.read_text(encoding="utf-8")
    text = text.replace("<Identifier>NewInstrument</Identifier>",
                        f"<Identifier>{name}</Identifier>")
    text = text.replace("<DisplayName>New Instrument</DisplayName>",
                        f"<DisplayName>{name}</DisplayName>")
    fdl.write_text(text, encoding="utf-8")
    ok(f"feature definition  ->  features/{fdl.name}")

    # 3 ── implementation module, renamed and retargeted
    impl = folder / "src" / f"{lower}_impl.py"
    (folder / "src" / "servicer.py").rename(impl)
    text = impl.read_text(encoding="utf-8")
    text = (text.replace("newinstrument", lower)
                .replace("NewInstrument", name)
                .replace("New Instrument", name))
    impl.write_text(text, encoding="utf-8")
    ok(f"implementation      ->  src/{impl.name}")

    # 4 ── entry point
    main_py = folder / "main.py"
    text = main_py.read_text(encoding="utf-8")
    text = (text.replace("from src.servicer import", f"from src.{lower}_impl import")
                .replace("newinstrument", lower)
                .replace("NewInstrument", name)
                .replace("New Instrument", name))
    main_py.write_text(text, encoding="utf-8")
    ok("entry point         ->  main.py")

    # 5 ── config
    config_path = folder / "config.yaml"
    text = config_path.read_text(encoding="utf-8")
    text = re.sub(r"port: \d+.*", f"port: {port}", text, count=1)
    text = text.replace('server_name: "New Instrument"', f'server_name: "{name}"')
    text = text.replace('server_uuid: "new-instrument-001"', f'server_uuid: "{key}-001"')
    text = text.replace('description: "Template server for new instruments"',
                        f'description: "SiLA2 server for {name}"')
    config_path.write_text(text, encoding="utf-8")
    ok(f"config.yaml         ->  port {port}, server_name {name}")

    # 6 ── library catalog: empty rather than the template's example, which
    #      would otherwise advertise folders this instrument does not use.
    (folder / "library_catalog.json").write_text(
        json.dumps({"library_types": []}, indent=2) + "\n", encoding="utf-8")
    ok("library_catalog.json -> empty (add entries when the instrument reads Library/ files)")

    # 7 ── codegen
    if _codegen(folder, name, lower) != 0:
        warn("Code generation failed — run `python labos.py codegen "
             f"{folder.name}` once the FDL is valid")
    else:
        ok(f"generated bindings  ->  generated/{lower}/")

    # 8 ── register it
    _register_server(key, name, port, folder.name)
    ok(f"registered in lab_config.yaml as '{key}'")

    heading("One file left to write")
    info(f"SiLA2/{folder.name}/src/{impl.name}")
    info("Add your commands to the FDL, re-run codegen, then implement every")
    info(f"abstract method in generated/{lower}/{lower}_base.py.")
    print()
    info(f"Then:  cd SiLA2/{folder.name} && python main.py")
    info(f"       python labos.py instruments        # it should appear")
    return 0


def _codegen(folder: Path, name: str, package: str) -> int:
    """Regenerate a server's bindings from its FDL.

    The documented procedure generates into a temp directory and copies the
    inner package back out, because the generator also writes a server skeleton
    nobody wants. Doing that by hand is where people get it wrong.
    """
    fdl_candidates = sorted((folder / "features").glob("*.sila.xml"))
    fdl = next((f for f in fdl_candidates if f.stem.split(".")[0] == name), None)
    if fdl is None:
        fdl = next((f for f in fdl_candidates
                    if "laboslibraryprovider" not in f.name.lower()), None)
    if fdl is None:
        bad(f"No feature definition found in {folder / 'features'}")
        return 1

    executable = shutil.which("sila2-codegen") or str(
        BASE_DIR / ".venv" / "Scripts" / "sila2-codegen.exe")
    if not Path(executable).exists() and not shutil.which("sila2-codegen"):
        bad("sila2-codegen not found — pip install 'sila2[codegen]'")
        return 1

    with tempfile.TemporaryDirectory(prefix="labos_codegen_") as tmp:
        result = subprocess.run(
            [executable, "new-package", "--package-name", package,
             "--output-directory", tmp, "--no-generate-main", str(fdl)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            bad("sila2-codegen failed:")
            for line in (result.stderr or result.stdout).splitlines()[-15:]:
                info(line)
            return result.returncode

        produced = Path(tmp) / package / "generated" / package
        if not produced.is_dir():
            bad(f"Generator produced no package at {produced}")
            return 1
        target = folder / "generated" / package
        target.mkdir(parents=True, exist_ok=True)
        for item in produced.iterdir():
            if item.is_file():
                shutil.copy2(item, target / item.name)
    return 0


def cmd_codegen(args: argparse.Namespace) -> int:
    folder = BASE_DIR / "SiLA2" / args.server
    if not folder.is_dir():
        folder = Path(args.server)
    if not folder.is_dir():
        bad(f"No server directory '{args.server}' under SiLA2/")
        return 1

    fdl = next((f for f in sorted((folder / "features").glob("*.sila.xml"))
                if "laboslibraryprovider" not in f.name.lower()), None)
    if fdl is None:
        bad(f"No feature definition in {folder / 'features'}")
        return 1

    name = fdl.name.split(".")[0]
    package = name.lower()
    heading(f"Regenerating {package} from {fdl.name}")
    code = _codegen(folder, name, package)
    if code == 0:
        ok(f"generated/{package}/ updated")
        info("Any command you added is now an abstract method your "
             "implementation must provide — the server will refuse to start until it does.")
    return code


def _register_server(key: str, name: str, port: int, directory: str) -> None:
    """Append a bootstrap seed to lab_config.yaml, preserving its comments.

    Deliberately textual rather than a yaml round-trip: PyYAML would drop every
    comment in the file, and lab_config.yaml is heavily commented on purpose.
    """
    config_path = BASE_DIR / "lab_config.yaml"
    text = config_path.read_text(encoding="utf-8")
    block = (
        f"  {key}:\n"
        f"    name: {name}\n"
        f"    enabled: true\n"
        f"    host: localhost\n"
        f"    port: {port}\n"
        f"    directory: SiLA2/{directory}\n"
        f"    command_windows:\n    - python\n    - main.py\n"
        f"    command_unix:\n    - python\n    - main.py\n"
        f"    startup_timeout: 30\n"
        f"    check_file: main.py\n"
    )
    match = re.search(r"^discovery:", text, re.M)
    if match:
        text = text[: match.start()] + block + text[match.start():]
    else:
        text = text.rstrip() + "\n" + block
    config_path.write_text(text, encoding="utf-8")

    from src.settings import reload_settings
    reload_settings(BASE_DIR)


# ─────────────────────────────────────────────────────────────────────────────
#  doctor
# ─────────────────────────────────────────────────────────────────────────────

def cmd_doctor(args: argparse.Namespace) -> int:
    problems = 0

    heading("Python")
    if sys.version_info >= (3, 10):
        ok(f"{sys.version.split()[0]}")
    else:
        bad(f"{sys.version.split()[0]} — 3.10 or newer required")
        problems += 1

    heading("Dependencies")
    for module, hint in (("sila2", "sila2[codegen]"), ("grpc", "grpcio"),
                         ("fastapi", "fastapi"), ("yaml", "pyyaml"),
                         ("zeroconf", "zeroconf"), ("httpx", "httpx"),
                         ("openpyxl", "openpyxl")):
        try:
            __import__(module)
            ok(module)
        except ImportError:
            bad(f"{module} missing — pip install {hint}")
            problems += 1

    heading("Configuration")
    try:
        from src.settings import get_settings
        settings = get_settings(BASE_DIR)
        for error in settings.validation.errors:
            bad(error)
            problems += 1
        for warning in settings.validation.warnings:
            warn(warning)
        if settings.validation.ok:
            ok(f"lab_config.yaml — {len(settings.servers)} server(s) configured")
    except Exception as exc:
        bad(f"lab_config.yaml unreadable: {exc}")
        problems += 1

    heading("Library")
    from src.settings import get_settings
    settings = get_settings(BASE_DIR)
    for label, path in (("workflows", settings.workflows_dir),
                        ("recipes", settings.recipes_dir),
                        ("HAL configs", settings.hardware_config_dir)):
        if path.is_dir():
            ok(f"{label}: {len(list(path.glob('*.json')))} file(s)")
        else:
            warn(f"{label}: {path} does not exist")

    heading("Workflows")
    from src.workflow import SCHEMA_VERSION
    unversioned = []
    for path in sorted(settings.workflows_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            bad(f"{path.name}: unreadable ({exc})")
            problems += 1
            continue
        if data.get("SchemaVersion") != SCHEMA_VERSION:
            unversioned.append(path.name)
    if unversioned:
        warn(f"{len(unversioned)} workflow(s) predate SchemaVersion {SCHEMA_VERSION} — "
             f"run: python scripts/migrate_workflows.py --apply")
    else:
        ok(f"all workflows at SchemaVersion {SCHEMA_VERSION}")

    heading("Code generation")
    if shutil.which("sila2-codegen") or (BASE_DIR / ".venv" / "Scripts" / "sila2-codegen.exe").exists():
        ok("sila2-codegen available")
    else:
        warn("sila2-codegen not found — needed only to add or change commands")

    print()
    if problems:
        bad(f"{problems} problem(s) found")
    else:
        ok("no problems found")
    return 1 if problems else 0


# ─────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="labos", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="which servers and interfaces are up").set_defaults(func=cmd_status)

    p = sub.add_parser("instruments", help="discovered instruments, their commands and properties")
    p.add_argument("-v", "--verbose", action="store_true", help="include command descriptions")
    p.set_defaults(func=cmd_instruments)

    p = sub.add_parser("read", help="read one property of one instrument")
    p.add_argument("instrument")
    p.add_argument("property")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("validate", help="check a workflow without running it")
    p.add_argument("workflow", help="file name, path, or workflow id")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("run", help="run a workflow (drives real instruments)")
    p.add_argument("workflow")
    p.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--parallel", action="store_true",
                   help="dispatch independent steps concurrently")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("new-server", help="scaffold, generate and register a new instrument")
    p.add_argument("name", help="CamelCase instrument name, e.g. Centrifuge")
    p.add_argument("--port", type=int, help="defaults to the next free port in 50061-50099")
    p.add_argument("--key", help="lab_config.yaml key; defaults to snake_case of the name")
    p.set_defaults(func=cmd_new_server)

    p = sub.add_parser("codegen", help="regenerate a server's bindings after an FDL edit")
    p.add_argument("server", help="folder under SiLA2/, e.g. OpentronsSiLA2Server")
    p.set_defaults(func=cmd_codegen)

    sub.add_parser("doctor", help="check the installation").set_defaults(func=cmd_doctor)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
