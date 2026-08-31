"""Tests for OpentronsSiLA2Server's TipTracker — atomic saves for tip_state.json.

A non-atomic write (open + write directly to the target file) can leave a
corrupted/truncated file behind on a crash mid-write; this is the only
persistent record of tip usage, so the write must be atomic.
"""
import json

import pytest

from _pkg_loader import load_opentrons_src

_ot = load_opentrons_src()
TipTracker = _ot.TipTracker


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "tip_state.json"


class TestAtomicSave:
    def test_save_writes_expected_content(self, state_file):
        tracker = TipTracker(state_file=str(state_file))
        tracker.add_usage("opentrons_96_tiprack_1000ul", 5)

        assert state_file.exists()
        data = json.loads(state_file.read_text(encoding="utf-8"))
        assert data["opentrons_96_tiprack_1000ul"] == 5

    def test_save_does_not_leave_temp_files_behind(self, state_file):
        tracker = TipTracker(state_file=str(state_file))
        tracker.add_usage("rack_a", 1)

        leftovers = list(state_file.parent.glob(".tip_state_*"))
        assert leftovers == []

    def test_failed_dump_leaves_original_file_intact(self, state_file, monkeypatch):
        tracker = TipTracker(state_file=str(state_file))
        tracker.add_usage("rack_a", 3)
        original_content = state_file.read_text(encoding="utf-8")

        import json as json_module
        def _boom(*args, **kwargs):
            raise RuntimeError("simulated crash mid-write")
        monkeypatch.setattr(json_module, "dump", _boom)

        tracker._usage["rack_a"] = 999  # would corrupt if write succeeded
        tracker.save()  # save() catches and logs; must not raise

        assert state_file.read_text(encoding="utf-8") == original_content
        leftovers = list(state_file.parent.glob(".tip_state_*"))
        assert leftovers == []  # temp file cleaned up on failure

    def test_reload_after_save_reflects_new_usage(self, state_file):
        tracker = TipTracker(state_file=str(state_file))
        tracker.add_usage("rack_a", 2)

        other = TipTracker(state_file=str(state_file))
        assert other.get_usage("rack_a") == 2


class TestSyncWithAllowedTypes:
    def test_a_to_b_to_a_preserves_partial_usage(self, state_file):
        """Switching HAL config away from and back to a rack type must not
        reset its real, partially-used count to 0 — the rack wasn't actually
        refilled just because a different config was loaded temporarily."""
        tracker = TipTracker(state_file=str(state_file))
        tracker.add_usage("rack_a", 42)

        tracker.sync_with_allowed_types({"rack_b"})  # switch to config B (no rack_a)
        assert tracker.get_usage("rack_a") == 42  # preserved even while inactive

        summary = tracker.sync_with_allowed_types({"rack_a"})  # switch back to A
        assert tracker.get_usage("rack_a") == 42
        assert summary["kept"] == ["rack_a"]  # not "added" (would mean reset to 0)

    def test_newly_added_type_starts_at_zero(self, state_file):
        tracker = TipTracker(state_file=str(state_file))
        summary = tracker.sync_with_allowed_types({"rack_new"})
        assert tracker.get_usage("rack_new") == 0
        assert summary["added"] == ["rack_new"]


class TestUsageAccounting:
    def test_reset_zeroes_usage(self, state_file):
        tracker = TipTracker(state_file=str(state_file))
        tracker.add_usage("rack_a", 10)
        tracker.reset("rack_a")
        assert tracker.get_usage("rack_a") == 0

    def test_check_availability_respects_capacity(self, state_file):
        tracker = TipTracker(state_file=str(state_file))
        tracker.add_usage("rack_a", 90)
        assert tracker.check_availability("rack_a", required=5, capacity=96) is True
        assert tracker.check_availability("rack_a", required=10, capacity=96) is False
