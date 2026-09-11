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
import re
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
    """Each keyword selects a real chain of SDK connection methods.

    They used to differ only in the connection *string* while every one of them
    was passed to InstrumentConnectionMethod.LastConnection — so "dialog" and
    "last" were the same strategy with a different token appended.
    """

    @property
    def source(self):
        return (V1_DIR / "SiLA2" / "TecanM200SiLA2Server" / "bridge" / "Instrument"
                / "TecanBridge.cs").read_text(encoding="utf-8")

    def _plan(self, keyword):
        """The Methods array the C# resolver builds for one keyword."""
        source = self.source
        marker = f'key == "{keyword}"'
        start = source.index(marker)
        end = source.index("ConnString =", start)
        return re.findall(r"InstrumentConnectionMethod\.(\w+)", source[start:end])

    @pytest.mark.parametrize("method", ["LastConnection", "Manually", "UserDialog"])
    def test_more_than_one_method_is_used(self, method):
        assert f"InstrumentConnectionMethod.{method}" in self.source

    def test_the_default_tries_the_stored_connection_then_a_fresh_scan(self):
        """The fresh scan is what recovers a reader moved to another USB port."""
        assert self._plan("usb") == ["LastConnection", "Manually"]

    def test_ask_ends_with_the_picker(self):
        assert self._plan("ask") == ["LastConnection", "Manually", "UserDialog"]

    def test_last_is_the_stored_connection_alone(self):
        assert self._plan("last") == ["LastConnection"]

    def test_dialog_goes_straight_to_the_picker(self):
        assert self._plan("dialog") == ["UserDialog"]

    def test_no_default_chain_can_open_a_window(self):
        """A modal dialog on the lab PC does not fail — it waits, and a
        workflow running overnight waits with it. Escalating to the picker has
        to be asked for."""
        for keyword in ("usb", "last", "sim", "any"):
            assert "UserDialog" not in self._plan(keyword), (
                f"'{keyword}' can open a window; it must not"
            )

    def test_the_picker_is_always_last_when_present(self):
        for keyword in ("ask", "dialog"):
            plan = self._plan(keyword)
            assert plan[-1] == "UserDialog"

    def test_the_failure_message_points_at_the_interactive_options(self):
        source = self.source
        assert "'ask'" in source and "'dialog'" in source


class TestAMalformedCatalogIsSurvivable:
    """The catalog arrives over the wire from another process, frequently one
    deployed on a different machine and upgraded on a different schedule. It is
    untrusted input.

    An entry missing a key used to raise KeyError out of _server_to_instrument,
    out of discover(), and out of the execute route that calls discover()
    before every command — so one stale remote server made every command on
    every instrument in the lab fail with the message 'folder'. That message is
    what an operator actually saw when trying to shake a module.
    """

    def test_an_entry_with_no_folder_costs_only_its_own_dropdown(self, core):
        core._server_to_instrument(
            _server_with_catalog([
                {"hint": "broken", "parameter_names": ["Whatever"]},
                {"hint": "recipe", "folder": "Library/Recipes",
                 "file_extension": ".json", "parameter_names": ["RecipeName"]},
            ]), {}, {})
        assert core._get_options_for_hint("broken") == []
        assert core._ui_dropdowns["recipe"]["folder"] == "Library/Recipes"

    def test_an_entry_with_no_hint_is_ignored(self):
        core = LabCore(Path("."))
        core._server_to_instrument(
            _server_with_catalog([{"folder": "Library/Recipes",
                                   "parameter_names": ["RecipeName"]}]), {}, {})

    def test_a_folder_entry_with_no_extension_still_works(self, core):
        core._server_to_instrument(
            _server_with_catalog([{"hint": "thing", "folder": "Library/Recipes",
                                   "parameter_names": ["X"]}]), {}, {})
        assert core._ui_dropdowns["thing"]["file_extension"] == ""

    def test_a_server_with_a_broken_catalog_still_becomes_an_instrument(self, core):
        """It keeps its commands; it just loses the dropdown it described."""
        server = _server_with_catalog([{"hint": "broken", "parameter_names": ["X"]}])
        server.name = "Stale Remote Server"
        instrument = core._server_to_instrument(server, {}, {})
        assert instrument.name == "Stale Remote Server"

    def test_the_discover_loop_does_not_build_them_in_one_comprehension(self):
        """A comprehension discards every instrument already built on the first
        raise; the loop has to tolerate one server failing."""
        source = (V1_DIR / "src" / "lab_core.py").read_text(encoding="utf-8")
        block = source[source.index("await self._discovery.discover_all("):]
        block = block[:block.index("self._last_discovery_time")]
        assert "try:" in block and "continue" in block
