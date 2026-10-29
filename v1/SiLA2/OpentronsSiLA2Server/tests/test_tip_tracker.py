"""calculate_tips_from_recipe() must credit each PickUpTip/GetNextTip/
ConsumeTips step to the tip rack IT actually names, not lump every step
onto a single recipe-wide "default rack" - a recipe with several distinct
tip rack LoadNames (one per tip_sizes_ul entry, e.g. MyTips_50 and
MyTips_1000) must get two independent planned-usage counters.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tip_tracker import TipTracker, calculate_tips_from_recipe


class TestCalculateTipsFromRecipeMultiRack:
    def _recipe(self, steps):
        return {
            "Labware": {
                "tips_50": {"LoadName": "MyTips_50"},
                "tips_1000": {"LoadName": "MyTips_1000"},
            },
            "Steps": steps,
        }

    def test_each_pickup_credited_to_its_own_tip_rack(self):
        recipe = self._recipe([
            {"Command": "PickUpTip", "PipetteMount": "left", "TipRack": "MyTips_50"},
            {"Command": "PickUpTip", "PipetteMount": "left", "TipRack": "MyTips_1000"},
            {"Command": "PickUpTip", "PipetteMount": "left", "TipRack": "MyTips_50"},
        ])
        usage = calculate_tips_from_recipe(recipe)
        assert usage == {"MyTips_50": 2, "MyTips_1000": 1}

    def test_step_without_tiprack_falls_back_to_single_detected_rack(self):
        # Legacy single-rack recipe: PickUpTip steps carry no explicit
        # TipRack field at all - must still resolve to the one rack found
        # in Labware, exactly like before this fix.
        recipe = {
            "Labware": {"tips": {"LoadName": "MyTips"}},
            "Steps": [
                {"Command": "PickUpTip", "PipetteMount": "left"},
                {"Command": "PickUpTip", "PipetteMount": "left"},
            ],
        }
        usage = calculate_tips_from_recipe(recipe)
        assert usage == {"MyTips": 2}

    def test_consume_tips_respects_its_own_rack_and_quantity(self):
        recipe = self._recipe([
            {"Command": "ConsumeTips", "TipRack": "MyTips_1000", "Quantity": 5},
            {"Command": "ConsumeTips", "TipRack": "MyTips_50", "Quantity": 3},
        ])
        usage = calculate_tips_from_recipe(recipe)
        assert usage == {"MyTips_1000": 5, "MyTips_50": 3}


class TestTipTrackerUsageKeyedPerRack:
    """TipTracker itself was already rack-type-agnostic (usage dict keyed
    by whatever string it's given) - these lock that in so a future change
    doesn't quietly reintroduce a single-rack assumption."""

    def test_add_usage_keeps_independent_counters_per_rack(self, tmp_path):
        tracker = TipTracker(state_file=str(tmp_path / "tip_state.json"))
        tracker.add_usage("MyTips_50", 4)
        tracker.add_usage("MyTips_1000", 10)
        tracker.add_usage("MyTips_50", 1)

        assert tracker.get_usage("MyTips_50") == 5
        assert tracker.get_usage("MyTips_1000") == 10

    def test_check_availability_per_rack(self, tmp_path):
        tracker = TipTracker(state_file=str(tmp_path / "tip_state.json"))
        tracker.add_usage("MyTips_50", 90)
        assert tracker.check_availability("MyTips_50", required=6, capacity=96) is True
        assert tracker.check_availability("MyTips_50", required=10, capacity=96) is False
        # A different rack's near-empty count must not be affected.
        assert tracker.check_availability("MyTips_1000", required=90, capacity=96) is True
