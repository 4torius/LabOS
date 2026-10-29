"""
Tests for the runtime multi-well liquid-fallback branch inside
OpentronsSiLA2Server's generated protocol template (protocol_generator.py's
UNIVERSAL_TEMPLATE) - the part that runs ON THE ROBOT when a Transfer/
Distribute step's "Source" is a candidate-well LIST with "VerifyLiquid" on.

generate_content() returns the actual generated protocol source as a Python
string (settings are embedded via repr(), not read from a file at protocol
run time) - so the real, shipped dispatch logic can be exercised directly by
executing that string, with a fake `opentrons` module (opentrons isn't
installed in this test environment - it only ever runs on the robot/
simulator) and a fake ProtocolContext standing in for the real one.
"""
import json
import sys
import types

import pytest

from _pkg_loader import load_opentrons_src

_ot = load_opentrons_src()
ProtocolGenerator = _ot.ProtocolGenerator


class FakeLabware:
    """Stands in for a loaded labware/tip rack. Wells are resolved by
    __getitem__ (mirrors get_loc's `objects["Labware"][name][well]`), each
    returning a distinct sentinel string so tests can assert exactly which
    well a call was made against."""
    def __init__(self, name, load_name):
        self.name = name
        self.load_name = load_name
        self.parent = "1"

    def __getitem__(self, well_id):
        return f"{self.name}:{well_id}"

    def wells(self):
        return []


class FakePipette:
    def __init__(self):
        self.has_tip = True
        self.liquid_presence_detection = False
        self.calls = []  # (method_name, kwargs) for every high-level call
        self._liquid_by_loc = {}

    def set_liquid(self, loc, present: bool):
        self._liquid_by_loc[loc] = present

    def pick_up_tip(self, *a, **kw):
        self.calls.append(("pick_up_tip", a, kw))

    def drop_tip(self, *a, **kw):
        self.calls.append(("drop_tip", a, kw))

    def detect_liquid_presence(self, loc):
        self.calls.append(("detect_liquid_presence", loc))
        return self._liquid_by_loc.get(loc, False)

    def require_liquid_presence(self, well):
        self.calls.append(("require_liquid_presence", well))
        raise RuntimeError(f"simulated awaiting-recovery: no liquid at {well}")

    def distribute(self, **kwargs):
        self.calls.append(("distribute", kwargs))

    def transfer(self, **kwargs):
        self.calls.append(("transfer", kwargs))


class FakeCtx:
    def __init__(self):
        self.pipette = FakePipette()

    def comment(self, *a, **kw):
        pass

    def load_labware(self, load_name, slot, label=None):
        return FakeLabware(label or load_name, load_name)

    def load_instrument(self, name, mount, tip_racks=None):
        return self.pipette

    def load_trash_bin(self, slot):
        return "TRASH"


def _run_recipe(recipe: dict, ctx: FakeCtx, temp_dir):
    """Generate the real protocol source for `recipe` and execute its run()
    against a fake ctx - exercising the actual shipped dispatch code."""
    gen = ProtocolGenerator(temp_dir=str(temp_dir), enable_simulation=False)
    code = gen.generate_content(json.dumps(recipe))

    fake_protocol_api = types.ModuleType("opentrons.protocol_api")
    fake_protocol_api.ProtocolContext = object
    fake_opentrons = types.ModuleType("opentrons")
    fake_opentrons.protocol_api = fake_protocol_api
    sys.modules["opentrons"] = fake_opentrons
    sys.modules["opentrons.protocol_api"] = fake_protocol_api

    namespace: dict = {}
    exec(compile(code, "<generated_protocol>", "exec"), namespace)
    namespace["run"](ctx)


def _base_recipe(steps):
    return {
        "ProtocolName": "test",
        "Labware": {
            # DisplayName is what FakeCtx.load_labware receives as `label` -
            # matching the key here lets get_loc's "Source_1:A1"-style
            # references resolve back to a sentinel string tests can assert
            # on directly (see FakeLabware.__getitem__).
            "Source_1": {"LoadName": "nest_12_reservoir_15ml", "Slot": "A2", "DisplayName": "Source_1"},
            "TargetPlate": {"LoadName": "corning_96_wellplate_360ul_flat", "Slot": "D2", "DisplayName": "TargetPlate"},
            "MyTips": {"LoadName": "opentrons_flex_96_tiprack_1000ul", "Slot": "B2", "DisplayName": "MyTips"},
        },
        "Pipettes": {"left": "flex_1channel_1000"},
        "Steps": steps,
    }


class TestLiquidFallback:
    def test_uses_second_candidate_when_first_is_dry(self, tmp_path):
        ctx = FakeCtx()
        ctx.pipette.set_liquid("Source_1:A1", False)
        ctx.pipette.set_liquid("Source_1:A2", True)
        recipe = _base_recipe([
            {"Command": "PickUpTip", "PipetteMount": "left", "TipRack": "MyTips"},
            {"Command": "Distribute", "PipetteMount": "left", "Volume": 50,
             "Source": ["Source_1:A1", "Source_1:A2"],
             "Destinations": ["TargetPlate:A1"], "NewTip": "never", "VerifyLiquid": True},
            {"Command": "DropTip", "PipetteMount": "left", "TrashLocation": "MyTrash"},
        ])
        _run_recipe(recipe, ctx, tmp_path)

        probes = [c for c in ctx.pipette.calls if c[0] == "detect_liquid_presence"]
        assert [p[1] for p in probes] == ["Source_1:A1", "Source_1:A2"]
        distribute_calls = [c for c in ctx.pipette.calls if c[0] == "distribute"]
        assert len(distribute_calls) == 1
        assert distribute_calls[0][1]["source"] == "Source_1:A2"

    def test_remembers_progress_across_trips_and_does_not_reprobe_dry_wells(self, tmp_path):
        ctx = FakeCtx()
        ctx.pipette.set_liquid("Source_1:A1", False)
        ctx.pipette.set_liquid("Source_1:A2", True)
        candidates = ["Source_1:A1", "Source_1:A2"]
        step = {"Command": "Distribute", "PipetteMount": "left", "Volume": 50,
                "Source": candidates, "Destinations": ["TargetPlate:A1"],
                "NewTip": "never", "VerifyLiquid": True}
        recipe = _base_recipe([
            {"Command": "PickUpTip", "PipetteMount": "left", "TipRack": "MyTips"},
            dict(step, Destinations=["TargetPlate:A1"]),
            {"Command": "DropTip", "PipetteMount": "left", "TrashLocation": "MyTrash"},
            {"Command": "PickUpTip", "PipetteMount": "left", "TipRack": "MyTips"},
            dict(step, Destinations=["TargetPlate:A2"]),
            {"Command": "DropTip", "PipetteMount": "left", "TrashLocation": "MyTrash"},
        ])
        _run_recipe(recipe, ctx, tmp_path)

        probes = [c[1] for c in ctx.pipette.calls if c[0] == "detect_liquid_presence"]
        # A1 is only ever probed once (first trip) - the second trip resumes
        # straight from the index that was already known good.
        assert probes == ["Source_1:A1", "Source_1:A2", "Source_1:A2"]
        distribute_calls = [c[1]["source"] for c in ctx.pipette.calls if c[0] == "distribute"]
        assert distribute_calls == ["Source_1:A2", "Source_1:A2"]

    def test_stops_the_run_and_never_aspirates_when_every_candidate_is_dry(self, tmp_path):
        ctx = FakeCtx()  # both candidates default to "no liquid"
        recipe = _base_recipe([
            {"Command": "PickUpTip", "PipetteMount": "left", "TipRack": "MyTips"},
            {"Command": "Distribute", "PipetteMount": "left", "Volume": 50,
             "Source": ["Source_1:A1", "Source_1:A2"],
             "Destinations": ["TargetPlate:A1"], "NewTip": "never", "VerifyLiquid": True},
            {"Command": "DropTip", "PipetteMount": "left", "TrashLocation": "MyTrash"},
        ])
        with pytest.raises(RuntimeError, match="awaiting-recovery"):
            _run_recipe(recipe, ctx, tmp_path)

        require_calls = [c for c in ctx.pipette.calls if c[0] == "require_liquid_presence"]
        assert len(require_calls) == 1
        assert require_calls[0][1] == "Source_1:A2"  # last candidate
        assert not any(c[0] == "distribute" for c in ctx.pipette.calls)

    def test_single_well_source_unaffected_stays_a_plain_string(self, tmp_path):
        """No list Source (no backups configured) -> old code path, no
        detect_liquid_presence probing at all (native probe handles it)."""
        ctx = FakeCtx()
        recipe = _base_recipe([
            {"Command": "PickUpTip", "PipetteMount": "left", "TipRack": "MyTips"},
            {"Command": "Distribute", "PipetteMount": "left", "Volume": 50,
             "Source": "Source_1:A1", "Destinations": ["TargetPlate:A1"],
             "NewTip": "never", "VerifyLiquid": True},
            {"Command": "DropTip", "PipetteMount": "left", "TrashLocation": "MyTrash"},
        ])
        _run_recipe(recipe, ctx, tmp_path)
        assert not any(c[0] == "detect_liquid_presence" for c in ctx.pipette.calls)
        distribute_calls = [c[1] for c in ctx.pipette.calls if c[0] == "distribute"]
        assert distribute_calls[0]["source"] == "Source_1:A1"
