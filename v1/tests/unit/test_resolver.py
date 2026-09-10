"""Tests for src/resolver.py — the single instrument-name matcher.

The names used here are the ones that actually appear in Library/Workflows:
the same physical instruments were written four different ways across files
authored months apart, and every one of them has to keep working.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.resolver import MatchKind, normalize_identifier, resolve  # noqa: E402


LIVE = [
    ("opentrons", "Opentrons Flex", "OT"),
    ("tecan", "TecanM200Pro", "TC"),
    ("mobile", "Mobile Robot", "MB"),
    ("manual_station", "Manual Station", "MS"),
]


class TestNormalizeIdentifier:

    @pytest.mark.parametrize("value", ["OpentronsFlex", "opentrons flex", "Opentrons_Flex",
                                       "  opentrons-flex  ", "OPENTRONS.FLEX"])
    def test_punctuation_and_case_collapse(self, value):
        assert normalize_identifier(value) == "opentronsflex"

    def test_empty(self):
        assert normalize_identifier("") == ""
        assert normalize_identifier(None) == ""


class TestRealWorkflowNames:
    """Every Instrument string found in the shipped workflow files."""

    @pytest.mark.parametrize("written,expected", [
        ("opentrons", "OT"),            # canonical id
        ("OpentronsFlex", "OT"),        # O-T-D.workflow.json
        ("Opentrons Flex", "OT"),       # announced name
        ("tecan", "TC"),
        ("Tecan", "TC"),                # Simple_PlateRead.workflow.json
        ("TecanM200Pro", "TC"),         # O-T-D.workflow.json
        ("mobile", "MB"),
        ("GoFaGo Mobile Robot", "MB"),  # O-T-D.workflow.json
        ("manual_station", "MS"),
        ("Manual Station", "MS"),
    ])
    def test_resolves(self, written, expected):
        assert resolve(written, LIVE).target == expected


class TestLayering:

    def test_exact_id_wins_over_a_name_that_also_matches(self):
        candidates = [("tecan", "Something Else", "A"), ("other", "tecan", "B")]
        result = resolve("tecan", candidates)
        assert result.kind is MatchKind.EXACT_ID
        assert result.target == "A"

    def test_exact_name_beats_a_looser_substring(self):
        candidates = [("a", "Tecan", "A"), ("b", "Tecan M200 Pro Reader", "B")]
        result = resolve("Tecan", candidates)
        assert result.kind is MatchKind.EXACT_NAME
        assert result.target == "A"

    def test_normalized_match(self):
        result = resolve("OpentronsFlex", LIVE)
        assert result.kind is MatchKind.NORMALIZED
        assert result.target == "OT"

    def test_unique_substring(self):
        result = resolve("GoFaGo Mobile Robot", LIVE)
        assert result.kind is MatchKind.SUBSTRING
        assert result.target == "MB"

    def test_case_insensitive_throughout(self):
        assert resolve("OPENTRONS", LIVE).target == "OT"
        assert resolve("mObIlE rObOt", LIVE).target == "MB"


class TestAmbiguityIsAnError:
    """The point of the module: a coin flip is not an acceptable outcome when
    the result decides which machine moves."""

    def test_two_substring_matches_do_not_silently_pick_one(self):
        candidates = [
            ("manual_station", "Manual Station", "A"),
            ("manual_prep", "Manual Sample Prep", "B"),
        ]
        result = resolve("manual", candidates)
        assert result.kind is MatchKind.AMBIGUOUS
        assert result.target is None
        assert set(result.candidates) == {"manual_station", "manual_prep"}
        assert "ambiguous" in result.error.lower()

    def test_ambiguity_does_not_fall_through_to_a_looser_layer(self):
        """Two exact-name collisions must not be resolved by trying substrings."""
        candidates = [("a", "Reader", "A"), ("b", "Reader", "B")]
        assert resolve("Reader", candidates).kind is MatchKind.AMBIGUOUS

    def test_an_exact_id_disambiguates(self):
        candidates = [
            ("manual_station", "Manual Station", "A"),
            ("manual_prep", "Manual Sample Prep", "B"),
        ]
        assert resolve("manual_station", candidates).target == "A"


class TestFailures:

    def test_not_found_lists_what_is_available(self):
        result = resolve("Incubator", LIVE)
        assert result.kind is MatchKind.NOT_FOUND
        assert result.target is None
        for known in ("opentrons", "tecan", "mobile", "manual_station"):
            assert known in result.error

    def test_empty_name(self):
        result = resolve("   ", LIVE)
        assert result.kind is MatchKind.EMPTY
        assert result.error == "No instrument name given"

    def test_no_candidates_at_all(self):
        result = resolve("opentrons", [])
        assert result.kind is MatchKind.NOT_FOUND
        assert "none" in result.error

    def test_resolution_is_falsy_when_unmatched(self):
        assert not resolve("nope", LIVE)
        assert resolve("tecan", LIVE)

    def test_error_is_none_on_success(self):
        assert resolve("tecan", LIVE).error is None
