"""A server declaring the values one of its parameters accepts.

Some parameters are neither a file in Library/ nor deck state, but still have a
small fixed set of sensible values — the Tecan's "how to connect" is the
motivating case. A server declares them in library_catalog.json and the
orchestrator offers them, without knowing what they mean.

Why not a SiLA2 Constrained/Set, which is what the protocol has for exactly
this? Because it is enforced before the server sees the call, and it rejects
values the server still accepts. Constraining ConnectionString to five
keywords broke the empty string — the documented way to say "use your
configured default" — which is what the shipped Simple_PlateRead workflow
sends. A dropdown is a UI affordance; narrowing the protocol to get one costs
more than it gives.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))

from src.lab_core import LabCore  # noqa: E402


def _server_with_catalog(catalog):
    server = MagicMock()
    server.library_catalog = catalog
    server.features = []
    server.get_all_commands.return_value = []
    server.name = "TestInstrument"
    server.server_type = "instrument"
    server.server_online = True
    server.host, server.port = "localhost", 50051
    return server


@pytest.fixture
def core(tmp_path):
    return LabCore(tmp_path)


class TestDeclaredValues:

    def test_a_declared_list_becomes_the_parameter_options(self, core):
        core._server_to_instrument(
            _server_with_catalog([{
                "hint": "connect_mode",
                "values": ["", "usb", "dialog"],
                "parameter_names": ["ConnectionString"],
            }]), {}, {})
        assert core._get_options_for_hint("connect_mode") == ["", "usb", "dialog"]

    def test_labels_are_offered_when_declared(self, core):
        core._server_to_instrument(
            _server_with_catalog([{
                "hint": "connect_mode",
                "values": ["usb", "dialog"],
                "value_labels": {"usb": "usb - the usual one",
                                 "dialog": "dialog - pick it by hand"},
                "parameter_names": ["ConnectionString"],
            }]), {}, {})
        pairs = core._option_pairs_for_hint("connect_mode")
        assert {p["value"] for p in pairs} == {"usb", "dialog"}
        assert any("the usual one" in p["label"] for p in pairs)

    def test_the_empty_string_survives_as_an_offered_value(self, core):
        """It is the documented "use your configured default"."""
        core._server_to_instrument(
            _server_with_catalog([{
                "hint": "connect_mode",
                "values": ["", "usb"],
                "parameter_names": ["ConnectionString"],
            }]), {}, {})
        assert "" in core._get_options_for_hint("connect_mode")

    def test_a_folder_backed_entry_still_works(self, core):
        """The original mechanism is untouched."""
        core._server_to_instrument(
            _server_with_catalog([{
                "hint": "recipe", "folder": "Library/Recipes",
                "file_extension": ".json", "parameter_names": ["RecipeName"],
            }]), {}, {})
        assert core._ui_dropdowns["recipe"]["folder"] == "Library/Recipes"

    def test_a_dynamic_entry_is_not_turned_into_a_folder(self, core):
        core._server_to_instrument(
            _server_with_catalog([{
                "hint": "mobile_task", "dynamic": True, "folder": "",
                "file_extension": "", "parameter_names": ["TaskId"],
            }]), {}, {})
        assert "mobile_task" not in core._ui_dropdowns or \
               not core._ui_dropdowns["mobile_task"].get("folder")


class TestTecanConnectionModes:
    """The shipped declaration, and the protocol contract it must not break."""

    @property
    def catalog(self):
        path = V1_DIR / "SiLA2" / "TecanM200SiLA2Server" / "library_catalog.json"
        return json.loads(path.read_text(encoding="utf-8-sig"))

    @property
    def fdl(self):
        path = (V1_DIR / "SiLA2" / "TecanM200SiLA2Server" / "features"
                / "PlateReaderService.sila.xml")
        return path.read_text(encoding="utf-8")

    def test_the_connection_modes_are_declared(self):
        entry = next(e for e in self.catalog["library_types"]
                     if e["hint"] == "tecan_connect_mode")
        assert "ConnectionString" in entry["parameter_names"]
        assert {"usb", "dialog", "last", "sim", "any"} <= set(entry["values"])

    def test_the_default_empty_string_is_offered(self):
        entry = next(e for e in self.catalog["library_types"]
                     if e["hint"] == "tecan_connect_mode")
        assert "" in entry["values"], (
            "the empty string means 'use instrument.connection_string' and is "
            "what Simple_PlateRead sends"
        )

    def test_connection_string_is_not_constrained_in_the_protocol(self):
        """A Set here rejects the empty string before the server sees it."""
        block = self.fdl[self.fdl.index("<Identifier>ConnectionString</Identifier>"):]
        block = block[:block.index("</Parameter>")]
        assert "<Constrained>" not in block
        assert "<Set>" not in block

    def test_the_shipped_workflow_still_sends_a_value_the_server_accepts(self):
        workflow = json.loads(
            (V1_DIR / "Library" / "Workflows" / "Simple_PlateRead.workflow.json")
            .read_text(encoding="utf-8-sig"))
        connect = next(s for s in workflow["Steps"] if s["Action"] == "Connect")
        entry = next(e for e in self.catalog["library_types"]
                     if e["hint"] == "tecan_connect_mode")
        assert connect["Parameters"]["ConnectionString"] in entry["values"]


class TestConnectionMethodsAreDistinct:
    """Each keyword selects a real SDK connection method.

    They used to differ only in the connection *string* while every one of them
    was passed to InstrumentConnectionMethod.LastConnection — so "dialog" and
    "last" were the same strategy with a different token appended.
    """

    @property
    def source(self):
        return (V1_DIR / "SiLA2" / "TecanM200SiLA2Server" / "bridge" / "Instrument"
                / "TecanBridge.cs").read_text(encoding="utf-8")

    @pytest.mark.parametrize("method", ["LastConnection", "Manually", "UserDialog"])
    def test_more_than_one_method_is_used(self, method):
        assert f"InstrumentConnectionMethod.{method}" in self.source

    def test_the_default_falls_back_to_a_fresh_enumeration(self):
        """This is what recovers a reader moved to another USB port."""
        assert "AllowEnumerationFallback = true" in self.source

    def test_the_fallback_only_runs_after_the_fast_path_failed(self):
        source = self.source
        first = source.index("TryConnect(plan.Method, plan.ConnString)")
        fallback = source.index("TryConnect(InstrumentConnectionMethod.Manually")
        assert first < fallback, "the fallback must not pre-empt the stored connection"
