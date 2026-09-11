"""A recipe that uses the heater-shaker, executed against a stand-in robot.

The generator emits a Python protocol that the robot runs; what it does is
therefore invisible to any test that only reads the generated text. So this
runs it: `opentrons.protocol_api` is replaced with a recorder, `run(ctx)` is
called, and the sequence of calls landing on the heater-shaker is compared
against what the recipe asked for.

The flow being protected is the one recipes actually use — open the latch,
put a plate on the module, close the latch, heat, shake — and it must keep
working exactly as it did.
"""
import json
import sys
import types
from pathlib import Path

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))
sys.path.insert(0, str(V1_DIR / "tests" / "unit"))

from _pkg_loader import load_opentrons_src  # noqa: E402

_ot = load_opentrons_src()
from _ot_src.protocol_generator import ProtocolGenerator  # noqa: E402
from _ot_src.hardware_manager import HardwareManager  # noqa: E402


class Recorder:
    """Stands in for anything the protocol touches, and remembers what it was
    asked to do. Returns another Recorder for any attribute, so the protocol
    can chain as deeply as it likes without this needing to model the API."""

    def __init__(self, name, log):
        self._name = name
        self._log = log

    def __call__(self, *args, **kwargs):
        self._log.append((self._name, args, kwargs))
        return Recorder(f"{self._name}()", self._log)

    def __getattr__(self, attr):
        if attr.startswith("_"):
            raise AttributeError(attr)
        return Recorder(f"{self._name}.{attr}", self._log)

    def __getitem__(self, key):
        return Recorder(f"{self._name}[{key!r}]", self._log)

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return True

    def __repr__(self):
        return f"<{self._name}>"


class FakeContext(Recorder):
    """A protocol context that hands back a distinct recorder per module, so a
    module's own call sequence can be read back on its own."""

    def __init__(self, log):
        super().__init__("ctx", log)
        self.modules = {}
        self.deck = {}

    def load_module(self, model, location=None, *a, **kw):
        self._log.append(("ctx.load_module", (model, location), {}))
        module = Recorder(f"module[{model}]", self._log)
        self.modules[model] = module
        return module


CONFIG = {
    "ConfigName": "HS_Test",
    "Modules": {"HeaterShaker": {"Type": "heaterShakerModuleV1", "Slot": "A1"}},
    "Trash": {"MyTrash": {"Type": "TrashBin", "Slot": "A3"}},
    "Labware": {
        "MyTips": {"LoadName": "opentrons_flex_96_tiprack_1000ul", "Slot": "B3"},
        "ShakePlate": {"LoadName": "corning_96_wellplate_360ul_flat", "Slot": "D2"},
    },
    "Pipettes": {"left": "flex_1channel_1000"},
}

#: The flow recipes use: open the latch, move the plate on, close it, heat, shake.
RECIPE_STEPS = [
    {"Command": "HeaterShakerOpenLatch", "ModuleID": "HeaterShaker"},
    {"Command": "MoveLabware", "LabwareID": "ShakePlate",
     "NewLocation": "HeaterShaker", "UseGripper": True},
    {"Command": "HeaterShakerCloseLatch", "ModuleID": "HeaterShaker"},
    {"Command": "HeaterShakerSetTemperature", "ModuleID": "HeaterShaker",
     "Temperature": 37},
    {"Command": "HeaterShakerWaitForTemperature", "ModuleID": "HeaterShaker"},
    {"Command": "HeaterShakerSetShake", "ModuleID": "HeaterShaker", "RPM": 500},
    {"Command": "Delay", "Seconds": 1},
    {"Command": "HeaterShakerDeactivateShaker", "ModuleID": "HeaterShaker"},
    {"Command": "HeaterShakerDeactivateHeater", "ModuleID": "HeaterShaker"},
    {"Command": "HeaterShakerOpenLatch", "ModuleID": "HeaterShaker"},
]


@pytest.fixture
def hardware(tmp_path):
    path = tmp_path / "HS_Test.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    return HardwareManager(config_path=str(path))


@pytest.fixture
def generator(tmp_path):
    return ProtocolGenerator(temp_dir=str(tmp_path / "gen"), enable_simulation=False)


def _execute(hardware, generator, steps, requirements=None):
    """Generate the protocol for these steps and run it against a recorder."""
    recipe = hardware.apply_hardware_to_recipe({
        "ProtocolName": "hs",
        "Requirements": dict(requirements or {}),
        "Steps": list(steps),
    })
    code = generator.generate_content(json.dumps(recipe))

    log = []
    fake_api = types.ModuleType("opentrons.protocol_api")
    fake_api.ProtocolContext = object
    fake_opentrons = types.ModuleType("opentrons")
    fake_opentrons.protocol_api = fake_api

    saved = {k: sys.modules.get(k) for k in ("opentrons", "opentrons.protocol_api")}
    sys.modules["opentrons"] = fake_opentrons
    sys.modules["opentrons.protocol_api"] = fake_api
    try:
        namespace = {"__name__": "generated_protocol"}
        exec(compile(code, "generated_protocol.py", "exec"), namespace)
        namespace["run"](FakeContext(log))
    finally:
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
    return log


def _module_calls(log):
    """Just the calls made on the heater-shaker, in order, by method name."""
    return [name.split(".")[-1] for name, _a, _kw in log
            if name.startswith("module[heaterShakerModuleV1].")]


class TestTheRecipeFlowIsIntact:

    def test_the_protocol_runs_end_to_end(self, hardware, generator):
        log = _execute(hardware, generator, RECIPE_STEPS,
                       {"ShakePlate": "ShakePlate", "HeaterShaker": "HeaterShaker"})
        assert any(name == "ctx.load_module" for name, _a, _kw in log)

    def test_the_module_is_loaded_at_the_slot_the_config_gives(self, hardware, generator):
        log = _execute(hardware, generator, RECIPE_STEPS,
                       {"ShakePlate": "ShakePlate", "HeaterShaker": "HeaterShaker"})
        loads = [(a[0], a[1]) for name, a, _kw in log if name == "ctx.load_module"]
        assert ("heaterShakerModuleV1", "A1") in loads

    def test_loading_the_module_opens_the_latch(self, hardware, generator):
        """The step recipes rely on to put a plate on the module."""
        calls = _module_calls(_execute(hardware, generator, RECIPE_STEPS,
                                       {"ShakePlate": "ShakePlate",
                                        "HeaterShaker": "HeaterShaker"}))
        assert calls[0] == "open_labware_latch"

    def test_every_step_the_recipe_asked_for_happened_in_order(self, hardware, generator):
        calls = _module_calls(_execute(hardware, generator, RECIPE_STEPS,
                                       {"ShakePlate": "ShakePlate",
                                        "HeaterShaker": "HeaterShaker"}))
        expected = [
            "open_labware_latch",        # on load
            "open_labware_latch",        # HeaterShakerOpenLatch
            "open_labware_latch",        # MoveLabware onto the module
            "close_labware_latch",       # HeaterShakerCloseLatch
            "set_target_temperature",
            "wait_for_temperature",
            "close_labware_latch",       # precondition of shaking
            "set_and_wait_for_shake_speed",
            "deactivate_shaker",
            "deactivate_heater",
            "open_labware_latch",
        ]
        assert calls == expected

    def test_the_temperature_reaches_the_module(self, hardware, generator):
        log = _execute(hardware, generator, RECIPE_STEPS,
                       {"ShakePlate": "ShakePlate", "HeaterShaker": "HeaterShaker"})
        temps = [a for name, a, _kw in log
                 if name.endswith("set_target_temperature")]
        assert temps == [(37,)]

    def test_the_speed_reaches_the_module(self, hardware, generator):
        log = _execute(hardware, generator, RECIPE_STEPS,
                       {"ShakePlate": "ShakePlate", "HeaterShaker": "HeaterShaker"})
        speeds = [a for name, a, _kw in log
                  if name.endswith("set_and_wait_for_shake_speed")]
        assert speeds == [(500,)]


class TestShakingWithoutAnExplicitClose:
    """The case that failed on hardware: a recipe that shakes while the latch
    is open, because loading the module opened it and nothing closed it."""

    def test_the_latch_is_closed_before_the_shaker_starts(self, hardware, generator):
        calls = _module_calls(_execute(hardware, generator, [
            {"Command": "HeaterShakerSetShake", "ModuleID": "HeaterShaker", "RPM": 300},
        ], {"HeaterShaker": "HeaterShaker"}))
        assert calls == ["open_labware_latch", "close_labware_latch",
                         "set_and_wait_for_shake_speed"]


class TestTheCompositeStepIsUnchanged:
    """The single `HeaterShaker` step, which does several things at once, is
    the older form and a lot of recipes use it."""

    def test_it_still_honours_its_own_flags(self, hardware, generator):
        calls = _module_calls(_execute(hardware, generator, [
            {"Command": "HeaterShaker", "ModuleID": "HeaterShaker",
             "CloseLatch": True, "Temperature": 42, "WaitForTemp": True,
             "RPM": 400, "Duration": 1, "DeactivateHeater": True, "OpenLatch": True},
        ], {"HeaterShaker": "HeaterShaker"}))
        assert calls == [
            "open_labware_latch",        # on load
            "close_labware_latch",       # CloseLatch
            "set_target_temperature",
            "wait_for_temperature",
            "set_and_wait_for_shake_speed",
            "deactivate_shaker",
            "deactivate_heater",
            "open_labware_latch",        # OpenLatch
        ]

    def test_keep_shaking_still_leaves_the_shaker_running(self, hardware, generator):
        calls = _module_calls(_execute(hardware, generator, [
            {"Command": "HeaterShaker", "ModuleID": "HeaterShaker",
             "RPM": 400, "KeepShaking": True},
        ], {"HeaterShaker": "HeaterShaker"}))
        assert "deactivate_shaker" not in calls
