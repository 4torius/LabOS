#!/usr/bin/env python3
"""One simulated instrument: a real server's interface with nothing behind it.

    python sim_lab/sim_server.py --server-dir SiLA2/TecanM200SiLA2Server \\
        --key tecan --name TecanM200Pro --type PlateReader --port 51051

What is real: the SiLA2 features (the server's own generated bindings, so the
commands, parameters and types are exactly what the orchestrator sees at the
bench), the name the server announces, and its `library_catalog.json` - served
by the server's own LabOSLibraryProvider, so what it declares it occupies is
the declaration that will be in force on the hardware.

What is not: anything a command does. A command writes a line to the timeline
when it starts, waits, and writes another when it ends. That is the whole
point - the timeline is an independent record of what was actually in flight
together, taken at the instruments and not from the orchestrator's own belief
about what it scheduled.

A scenario file can make a command slow, make it fail, or make it never
return, which is how the unhappy paths - a retry, an abort, an emergency
stop - are exercised without a robot to break.

Runs as its own process, like a real server, because every server's bindings
live in a package called `generated` and two of them cannot share one
interpreter.
"""

import argparse
import importlib
import inspect
import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger("sim")

#: Command names that stop an instrument when its catalog does not name one.
_STOP_NAMES = ("EmergencyStop", "Stop", "Halt", "AbortRun", "Abort", "CancelTask", "Cancel")

_DEFAULTS = {"str": "", "int": 0, "float": 0.0, "bool": True, "bytes": b""}


def _default_for(annotation, types_module=None, override=None):
    """A value of the right shape for a generated type annotation (a string)."""
    # A NamedTuple under `from __future__ import annotations` holds its field
    # types as ForwardRef('bool'), not as the string 'bool'.
    text = getattr(annotation, "__forward_arg__", None) or str(annotation or "str")
    text = text.strip()
    if text.startswith("Optional["):
        text = text[len("Optional["):-1]
    if text.startswith("List[") or text.startswith("list["):
        inner = text[5:-1]
        cls = getattr(types_module, inner, None) if types_module else None
        if isinstance(override, list) and cls is not None and hasattr(cls, "_fields"):
            return [cls(**item) for item in override if isinstance(item, dict)]
        return list(override) if isinstance(override, list) else []
    if override is not None:
        return override
    if text in _DEFAULTS:
        return _DEFAULTS[text]
    alias = getattr(types_module, text, None) if types_module else None
    if alias in (str, int, float, bool):
        return _DEFAULTS[alias.__name__]
    return ""


class Timeline:
    """Appends one JSON object per line; one short write per event."""

    def __init__(self, path, server_key):
        self.path = path
        self.server_key = server_key
        self._lock = threading.Lock()

    def write(self, event, command, **extra):
        record = {"t": time.time(), "server": self.server_key,
                  "event": event, "command": command}
        record.update(extra)
        line = json.dumps(record, default=str) + "\n"
        logger.info("%s %s %s", event, command, extra.get("status", ""))
        if not self.path:
            return
        # One append, one write call: four processes share this file.
        with self._lock:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(descriptor, line.encode("utf-8"))
            finally:
                os.close(descriptor)


class SimCore:
    """What every simulated command does: record, wait, maybe fail."""

    def __init__(self, key, catalog, scenario_path, timeline):
        self.key = key
        self.timeline = timeline
        self.scenario_path = scenario_path
        self._scenario_mtime = None
        self.scenario = {}
        self.default_duration = 0.3
        resources = (catalog.get("resources") or {}).get("commands") or {}
        self.control = {name for name, spec in resources.items()
                        if isinstance(spec, dict) and spec.get("control")}
        self.stop_command = ((catalog.get("emergency_stop") or {}).get("command") or "")
        self._generation = 0          # bumped by a stop; in-flight commands notice
        self._failures_left = {}
        self._lock = threading.Lock()

    def _reload(self):
        """Pick up a scenario that was rewritten while the server runs.

        Lets one set of running servers be reused across scenarios - starting
        four processes for every test would make the suite slow enough that
        nobody ran it.
        """
        if not self.scenario_path:
            return
        try:
            mtime = os.stat(self.scenario_path).st_mtime_ns
        except OSError:
            return
        if mtime == self._scenario_mtime:
            return
        try:
            scenario = json.loads(Path(self.scenario_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return        # half-written; the next call reads it whole
        self._scenario_mtime = mtime
        self.scenario = scenario.get(self.key, {}) if isinstance(scenario, dict) else {}
        self.default_duration = float(scenario.get("default_duration", 0.3))
        with self._lock:
            self._failures_left = {}

    def _spec(self, command):
        self._reload()
        spec = self.scenario.get(command)
        return spec if isinstance(spec, dict) else {}

    def is_stop(self, command):
        return command == self.stop_command or command in ("EmergencyStop", "AbortRun") or (
            not self.stop_command and command in _STOP_NAMES)

    def run(self, command, parameters):
        """Returns the scenario's response overrides, or raises."""
        spec = self._spec(command)
        if self.is_stop(command):
            with self._lock:
                self._generation += 1
            self.timeline.write("stop", command)
            return spec.get("response") or {}
        if command in self.control:
            self.timeline.write("control", command, parameters=parameters)
            return spec.get("response") or {}

        with self._lock:
            generation = self._generation
            if command not in self._failures_left:
                self._failures_left[command] = int(spec.get("fail_times", 1 if spec.get("fail") else 0))
        duration = float(spec.get("duration", self.default_duration))
        if spec.get("hang"):
            duration = float("inf")
        self.timeline.write("start", command, parameters=parameters)
        deadline = time.time() + duration
        while time.time() < deadline:
            if self._generation != generation:
                self.timeline.write("end", command, status="stopped", parameters=parameters)
                raise RuntimeError(f"{command} was stopped")
            time.sleep(0.01)
        with self._lock:
            failing = self._failures_left.get(command, 0) > 0
            if failing:
                self._failures_left[command] -= 1
        if failing:
            self.timeline.write("end", command, status="failed", parameters=parameters)
            raise RuntimeError(str(spec.get("fail") or f"{command} failed (simulated)"))
        self.timeline.write("end", command, status="ok", parameters=parameters)
        return spec.get("response") or {}

    def property_value(self, name):
        self._reload()
        return (self.scenario.get("properties") or {}).get(name)


def _plain(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def allowed_values(feature_xml):
    """(command or property, response field) -> first value its FDL allows.

    A response constrained to a set ("Absorbance", "Fluorescence", ...) is
    rejected by the SiLA2 library if the simulation answers with an empty
    string, so the first allowed value is used instead.
    """
    import xml.etree.ElementTree as ET

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    def first_of_set(element):
        for node in element.iter():
            if local(node.tag) == "Set":
                for value in node:
                    if local(value.tag) == "Value" and value.text:
                        return value.text
        return None

    found = {}
    try:
        root = ET.parse(feature_xml).getroot()
    except (OSError, ET.ParseError):
        return found
    for element in root:
        kind = local(element.tag)
        identifier = next((c.text for c in element if local(c.tag) == "Identifier"), None)
        if kind == "Property" and identifier:
            value = first_of_set(element)
            if value is not None:
                found[(identifier, "")] = value
        if kind == "Command" and identifier:
            for response in element:
                if local(response.tag) != "Response":
                    continue
                field = next((c.text for c in response if local(c.tag) == "Identifier"), None)
                value = first_of_set(response)
                if field and value is not None:
                    found[(identifier, field)] = value
    return found


def build_stub(base_class, types_module, core, allowed=None):
    """A concrete implementation of a generated feature base class."""
    members = {}
    allowed = allowed or {}

    def _command(name):
        signature = inspect.signature(getattr(base_class, name))
        names = [p for p in signature.parameters if p not in ("self", "metadata", "instance")]
        responses = getattr(types_module, f"{name}_Responses", None)
        annotations = getattr(responses, "__annotations__", {}) if responses else {}

        def method(self, *args, **kwargs):
            kwargs.pop("metadata", None)
            instance = kwargs.pop("instance", None)
            parameters = {n: _plain(v) for n, v in zip(names, args)}
            parameters.update({k: _plain(v) for k, v in kwargs.items()})
            if instance is not None and hasattr(instance, "begin_execution"):
                instance.begin_execution()
            overrides = core.run(name, parameters)
            if responses is None:
                return None
            return responses(*[
                _default_for(annotations.get(field), types_module,
                             overrides.get(field, allowed.get((name, field))))
                for field in responses._fields])
        method.__name__ = name
        return method

    def _getter(name, prop):
        annotation = inspect.signature(getattr(base_class, name)).return_annotation

        def getter(self, *args, **kwargs):
            value = core.property_value(prop)
            return _default_for(annotation, types_module,
                                allowed.get((prop, "")) if value is None else value)
        getter.__name__ = name
        return getter

    for name in sorted(getattr(base_class, "__abstractmethods__", ())):
        if name.startswith("get_"):
            members[name] = _getter(name, name[4:])
        else:
            members[name] = _command(name)

    def __init__(self, parent_server):
        base_class.__init__(self, parent_server=parent_server)
        # Observable properties need a first value, or a subscriber waits.
        for attribute in dir(base_class):
            if not attribute.startswith("update_"):
                continue
            prop = attribute[len("update_"):]
            updater = getattr(self, attribute)
            parameters = list(inspect.signature(updater).parameters.values())
            annotation = parameters[0].annotation if parameters else "str"
            value = core.property_value(prop)
            try:
                updater(_default_for(annotation, types_module,
                                     allowed.get((prop, "")) if value is None else value))
            except Exception as exc:  # a property the simulation cannot fake is not fatal
                logger.debug("no initial value for %s: %s", prop, exc)

    members["__init__"] = __init__
    return type(f"Simulated{base_class.__name__}", (base_class,), members)


def _features(server_dir):
    """(feature, base class, types module) for every generated feature package."""
    found = []
    for package in sorted((Path(server_dir) / "generated").iterdir()):
        if not package.is_dir() or package.name.startswith("_"):
            continue
        module = importlib.import_module(f"generated.{package.name}")
        feature = next((getattr(module, n) for n in dir(module) if n.endswith("Feature")), None)
        base = next((getattr(module, n) for n in dir(module)
                     if n.endswith("Base") and inspect.isclass(getattr(module, n))), None)
        try:
            types_module = importlib.import_module(f"generated.{package.name}.{package.name}_types")
        except ImportError:
            types_module = None
        xml = next(iter(sorted(package.glob("*.sila.xml"))), None)
        if feature is not None and base is not None:
            found.append((package.name, feature, base, types_module,
                          allowed_values(xml) if xml else {}))
    return found


def _resolve(spec):
    module_name, _, attribute = spec.partition(":")
    return getattr(importlib.import_module(module_name), attribute)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--server-dir", required=True)
    parser.add_argument("--key", required=True, help="the key under servers: in lab_config.yaml")
    parser.add_argument("--name", required=True, help="the name the real server announces")
    parser.add_argument("--type", default="Simulated")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--scenario", default="")
    parser.add_argument("--timeline", default=os.environ.get("LABOS_SIM_LOG", ""))
    parser.add_argument("--augment", default="",
                        help="module:function returning a catalog augmenter, "
                             "resolved inside the server directory")
    args = parser.parse_args()

    # Only the simulation's own lines: the SiLA2 library logs every request
    # and every response in full, which buries the timeline.
    logging.basicConfig(level=logging.WARNING,
                        format=f"%(asctime)s sim[{args.key}] %(message)s")
    logger.setLevel(logging.INFO)

    server_dir = str(Path(args.server_dir).resolve())
    os.chdir(server_dir)
    sys.path.insert(0, server_dir)

    from sila2.server import SilaServer  # noqa: E402
    from src.library_provider_impl import LibraryProviderImpl  # noqa: E402  (the server's own)

    catalog = json.loads((Path(server_dir) / "library_catalog.json").read_text(encoding="utf-8-sig"))
    core = SimCore(args.key, catalog, args.scenario, Timeline(args.timeline, args.key))

    server = SilaServer(
        server_name=args.name,
        server_type=args.type,
        server_description=f"SIMULATED {args.name} - no hardware behind this server",
        server_version="0.0",
        server_vendor_url="https://labos.invalid/simulated",
        server_uuid=uuid.uuid5(uuid.NAMESPACE_DNS, f"labos-sim-{args.key}"),
    )
    for package, feature, base, types_module, allowed in _features(server_dir):
        if package == "laboslibraryprovider":
            augment = _resolve(args.augment)() if args.augment else None
            implementation = LibraryProviderImpl(server, augment=augment)
        else:
            implementation = build_stub(base, types_module, core, allowed)(server)
        server.set_feature_implementation(feature, implementation)

    # Loopback only, and never announced: a simulated instrument must not be
    # found by anything looking for the real lab.
    server.start_insecure("127.0.0.1", args.port, enable_discovery=False)
    print(f"SIM-READY {args.key} {args.port}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
