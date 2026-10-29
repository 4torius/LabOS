"""Unit tests for select_tip_for_volume() / FLEX_TIP_RANGES.

Real Opentrons Flex tip catalog, deliberately overlapping ranges (per the
official "Opentrons Flex Tips" brochure): 20uL (1-20), 50uL (1-50),
200uL (5-200), 1000uL (5-1000). Always pick the smallest/most precise
size among those actually available, never a coarser-but-fitting one.
"""
from src.excel_recipe_parser import select_tip_for_volume, FLEX_TIP_RANGES

ALL_SIZES = [20, 50, 200, 1000]


class TestSelectTipForVolume:
    def test_small_volume_picks_smallest_tip(self):
        assert select_tip_for_volume(1, ALL_SIZES) == 20

    def test_exact_upper_edge_of_a_range_stays_on_that_tip(self):
        assert select_tip_for_volume(20, ALL_SIZES) == 20
        assert select_tip_for_volume(50, ALL_SIZES) == 50
        assert select_tip_for_volume(200, ALL_SIZES) == 200
        assert select_tip_for_volume(1000, ALL_SIZES) == 1000

    def test_one_over_a_range_bumps_to_the_next_size(self):
        assert select_tip_for_volume(21, ALL_SIZES) == 50
        assert select_tip_for_volume(51, ALL_SIZES) == 200
        assert select_tip_for_volume(201, ALL_SIZES) == 1000

    def test_low_volume_below_a_larger_tips_minimum_still_prefers_the_smaller_tip(self):
        """4uL is below the 200uL/1000uL tips' own minimum (5uL) but within
        the 20uL tip's range (1-20) - must pick 20, not skip to 200/1000."""
        assert select_tip_for_volume(4, ALL_SIZES) == 20

    def test_volume_below_every_available_minimum_returns_none(self):
        assert select_tip_for_volume(0.5, [1000]) is None

    def test_volume_above_every_available_maximum_returns_none(self):
        assert select_tip_for_volume(1500, [1000]) is None

    def test_restricted_available_sizes_only_considers_those(self):
        """User loaded only 1000uL + 50uL tips - a volume that would prefer
        20uL or 200uL must fall back to whichever of the two loaded sizes
        actually covers it, never silently assume an unloaded size exists."""
        assert select_tip_for_volume(500, [1000, 50]) == 1000
        assert select_tip_for_volume(10, [1000, 50]) == 50
        assert select_tip_for_volume(0.5, [1000, 50]) is None  # below both minimums

    def test_flex_tip_ranges_match_the_official_brochure(self):
        assert FLEX_TIP_RANGES[20] == {"min": 1, "max": 20, "load_name": "opentrons_flex_96_tiprack_20ul"}
        assert FLEX_TIP_RANGES[50] == {"min": 1, "max": 50, "load_name": "opentrons_flex_96_tiprack_50ul"}
        assert FLEX_TIP_RANGES[200] == {"min": 5, "max": 200, "load_name": "opentrons_flex_96_tiprack_200ul"}
        assert FLEX_TIP_RANGES[1000] == {"min": 5, "max": 1000, "load_name": "opentrons_flex_96_tiprack_1000ul"}
