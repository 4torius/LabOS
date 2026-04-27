"""
Unit tests for sila2_xml_parser.py — no running servers required.
"""
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).parent.parent
SILA2_DIR = V1_DIR / "SiLA2"
sys.path.insert(0, str(V1_DIR))
sys.path.insert(0, str(SILA2_DIR))

from sila2_xml_parser import parse_sila_xml, features_from_xml_dir


# Opentrons feature XML (always present, not hardware-dependent)

OPENTRONS_XML = V1_DIR / "SiLA2" / "OpentronsSiLA2Server" / "features" / "WorkflowAPI.sila.xml"
MANUAL_XML    = V1_DIR / "SiLA2" / "ManualStationSiLA2Server" / "features" / "ManualStation.sila.xml"


@pytest.mark.parametrize("xml_path", [OPENTRONS_XML, MANUAL_XML])
def test_parse_xml_returns_dict(xml_path):
    if not xml_path.exists():
        pytest.skip(f"{xml_path.name} not found")
    result = parse_sila_xml(str(xml_path))
    assert isinstance(result, dict)


@pytest.mark.parametrize("xml_path", [OPENTRONS_XML, MANUAL_XML])
def test_parse_xml_required_keys(xml_path):
    if not xml_path.exists():
        pytest.skip(f"{xml_path.name} not found")
    feat = parse_sila_xml(str(xml_path))
    for key in ("identifier", "display_name", "description", "commands", "properties"):
        assert key in feat, f"Missing key: {key}"


def test_opentrons_xml_identifier():
    if not OPENTRONS_XML.exists():
        pytest.skip("WorkflowAPI.sila.xml not found")
    feat = parse_sila_xml(str(OPENTRONS_XML))
    assert feat["identifier"] == "WorkflowAPI"


def test_opentrons_xml_has_commands():
    if not OPENTRONS_XML.exists():
        pytest.skip("WorkflowAPI.sila.xml not found")
    feat = parse_sila_xml(str(OPENTRONS_XML))
    assert len(feat["commands"]) > 0, "No commands found in WorkflowAPI"


def test_opentrons_xml_command_fields():
    if not OPENTRONS_XML.exists():
        pytest.skip("WorkflowAPI.sila.xml not found")
    feat = parse_sila_xml(str(OPENTRONS_XML))
    for cmd in feat["commands"]:
        assert "identifier" in cmd
        assert "display_name" in cmd
        assert "parameters" in cmd
        assert "observable" in cmd


def test_manual_xml_has_properties():
    if not MANUAL_XML.exists():
        pytest.skip("ManualStation.sila.xml not found")
    feat = parse_sila_xml(str(MANUAL_XML))
    assert len(feat["properties"]) > 0, "ManualStation should have properties"


def test_features_from_xml_dir_opentrons():
    features_dir = V1_DIR / "SiLA2" / "OpentronsSiLA2Server" / "features"
    if not features_dir.exists():
        pytest.skip("Opentrons features dir not found")
    features = features_from_xml_dir(str(features_dir))
    assert len(features) > 0
    assert all("identifier" in f for f in features)


def test_features_from_xml_dir_manual():
    features_dir = V1_DIR / "SiLA2" / "ManualStationSiLA2Server" / "features"
    if not features_dir.exists():
        pytest.skip("ManualStation features dir not found")
    features = features_from_xml_dir(str(features_dir))
    assert len(features) > 0


def test_parameter_constraint_parsing():
    """Commands with Constrained parameters should populate constraints list."""
    if not OPENTRONS_XML.exists():
        pytest.skip("WorkflowAPI.sila.xml not found")
    feat = parse_sila_xml(str(OPENTRONS_XML))
    # Look for any command with parameters that have constraints
    constrained = [
        p for cmd in feat["commands"]
        for p in cmd.get("parameters", [])
        if p.get("constraints")
    ]
    # If none, test is informational (not all features have constrained params)
    if constrained:
        assert all(isinstance(p["constraints"], list) for p in constrained)
