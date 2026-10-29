"""Pickers whose values live on a server, read through a property it names.

The mobile robot's task list was the only case, and it was written into the
core by name: LabCore looked for an instrument called "mobile" or "gofa",
called a Get_AvailableTasks command the real server does not have, and caught
RefreshTasks by name to pull the list from a property instead. The mechanism is
now declared - `source_property`, `value_field`, `label_field`, `folder` - by
the server's catalog, or by lab_config.yaml for a server whose catalog cannot
be changed. These tests use an instrument with an unrelated name on purpose.
"""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.catalog import parse_catalog
from src.lab_core import ExecutionResult, Instrument, LabCore, Property

DECLARATION = {
    "hint": "stacker_slot",
    "parameter_names": ["SlotId"],
    "source_property": "Slots",
    "value_field": "SlotId",
    "label_field": "Label",
    "folder": "Library/StackerSlots",
}


def _instrument(status="online", declaration=DECLARATION):
    return Instrument(
        id="stacker", name="Plate Stacker", status=status,
        properties=[Property(id="Slots", name="Slots", feature="StackerControl")],
        catalog=parse_catalog({"library_types": [declaration]}),
        _server=MagicMock(),
    )


@pytest.fixture
def core(tmp_path):
    return LabCore(base_dir=tmp_path)


class TestReadingTheProperty:

    @pytest.mark.asyncio
    async def test_values_and_labels_come_from_the_declared_fields(self, core, tmp_path):
        core._instruments = {"stacker": _instrument()}
        core.get_property = AsyncMock(return_value=ExecutionResult(success=True, data=[
            {"SlotId": "s1", "Label": "Top", "Height": 3},
            {"SlotId": "s2", "Label": "Bottom", "Height": 1},
        ]))

        options = await core.fetch_dynamic_options("stacker_slot")

        assert [(o["id"], o["name"]) for o in options] == [("s1", "Top"), ("s2", "Bottom")]
        assert options[0]["Height"] == 3, "extra fields travel with the option"
        core.get_property.assert_awaited_once_with("stacker", "StackerControl", "Slots")

    @pytest.mark.asyncio
    async def test_a_json_string_property_is_read_too(self, core):
        core._instruments = {"stacker": _instrument()}
        core.get_property = AsyncMock(return_value=ExecutionResult(
            success=True, data=json.dumps([{"SlotId": "s1", "Label": "Top"}])))
        assert [o["id"] for o in await core.fetch_dynamic_options("stacker_slot")] == ["s1"]

    @pytest.mark.asyncio
    async def test_the_list_is_kept_on_disk(self, core, tmp_path):
        core._instruments = {"stacker": _instrument()}
        core.get_property = AsyncMock(return_value=ExecutionResult(
            success=True, data=[{"SlotId": "s1", "Label": "Top"}]))
        await core.fetch_dynamic_options("stacker_slot")
        snapshot = tmp_path / "Library" / "StackerSlots" / LabCore.OPTIONS_SNAPSHOT
        assert json.loads(snapshot.read_text(encoding="utf-8"))[0]["id"] == "s1"


class TestWhenTheServerCannotAnswer:

    @pytest.mark.asyncio
    async def test_no_instrument_declares_it(self, core):
        core._instruments = {}
        assert await core.fetch_dynamic_options("stacker_slot") == []

    @pytest.mark.asyncio
    async def test_an_offline_server_is_not_asked(self, core):
        core._instruments = {"stacker": _instrument(status="offline")}
        core.get_property = AsyncMock()
        assert await core.fetch_dynamic_options("stacker_slot") == []
        core.get_property.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failed_read_falls_back_to_the_snapshot(self, core, tmp_path):
        folder = tmp_path / "Library" / "StackerSlots"
        folder.mkdir(parents=True)
        (folder / LabCore.OPTIONS_SNAPSHOT).write_text(
            json.dumps([{"id": "s9", "name": "Kept"}]), encoding="utf-8")
        core._instruments = {"stacker": _instrument()}
        core.get_property = AsyncMock(return_value=ExecutionResult(success=False, error="boom"))
        assert [o["id"] for o in await core.fetch_dynamic_options("stacker_slot")] == ["s9"]

    @pytest.mark.asyncio
    async def test_a_raising_read_does_not_propagate(self, core):
        core._instruments = {"stacker": _instrument()}
        core.get_property = AsyncMock(side_effect=ConnectionError("PC unreachable over hotspot"))
        assert await core.fetch_dynamic_options("stacker_slot") == []

    @pytest.mark.asyncio
    async def test_malformed_json_degrades_to_no_options(self, core):
        core._instruments = {"stacker": _instrument()}
        core.get_property = AsyncMock(return_value=ExecutionResult(success=True, data="{not json"))
        assert await core.fetch_dynamic_options("stacker_slot") == []

    def test_the_older_one_file_per_option_layout_still_reads(self, core, tmp_path):
        """Library/MobileTasks was written as one JSON file per task."""
        folder = tmp_path / "Library" / "MobileTasks"
        folder.mkdir(parents=True)
        (folder / "t1.json").write_text(
            json.dumps({"id": "t1", "name": "Task One", "subtask_count": 2}), encoding="utf-8")
        options = core._read_options_snapshot("Library/MobileTasks")
        assert options == [{"id": "t1", "name": "Task One", "subtask_count": 2}]


class TestDeclaredInConfiguration:
    """For a server on another machine, whose catalog cannot be edited."""

    @pytest.mark.asyncio
    async def test_lab_config_names_the_instrument_and_the_property(self, core):
        core._instruments = {"stacker": _instrument(declaration={
            "hint": "stacker_slot", "parameter_names": ["SlotId"], "dynamic": True})}
        core._ui_dropdowns["stacker_slot"] = {
            "dynamic": True, "server": "stacker", "source_property": "Slots",
            "value_field": "SlotId", "label_field": "Label",
        }
        core.get_property = AsyncMock(return_value=ExecutionResult(
            success=True, data=[{"SlotId": "s1", "Label": "Top"}]))
        assert [o["name"] for o in await core.fetch_dynamic_options("stacker_slot")] == ["Top"]


class TestWhatThePickerShows:
    """The value is the id; what an operator reads is the label.

    The mobile robot's Execute Task dialog listed the raw task ids -
    "bsffcndfdddchyrrbefecdanbcscfddy" and nine more like it - because the
    labels were dropped on the way from the property to the form, leaving the
    operator to pick a task by a string that says nothing about it.
    """

    @pytest.mark.asyncio
    async def test_a_read_list_is_offered_by_label(self, core):
        core._instruments = {"stacker": _instrument()}
        core.get_property = AsyncMock(return_value=ExecutionResult(success=True, data=[
            {"SlotId": "s1", "Label": "Top"}, {"SlotId": "s2", "Label": "Bottom"},
        ]))
        await core.fetch_dynamic_options("stacker_slot")
        core._ui_dropdowns["stacker_slot"] = {"source_property": "Slots"}

        assert core._option_pairs_for_hint("stacker_slot") == [
            {"value": "s1", "label": "Top"}, {"value": "s2", "label": "Bottom"}]
        assert core._get_options_for_hint("stacker_slot") == ["s1", "s2"]

    def test_the_snapshot_carries_the_labels_while_the_server_is_off(self, core, tmp_path):
        folder = tmp_path / "Library" / "StackerSlots"
        folder.mkdir(parents=True)
        (folder / LabCore.OPTIONS_SNAPSHOT).write_text(
            json.dumps([{"id": "s9", "name": "Kept"}]), encoding="utf-8")
        core._ui_dropdowns["stacker_slot"] = {
            "source_property": "Slots", "folder": "Library/StackerSlots"}

        assert core._option_pairs_for_hint("stacker_slot") == [
            {"value": "s9", "label": "Kept"}]

    def test_an_option_with_no_label_falls_back_to_its_id(self, core):
        core._dynamic_options_cache["stacker_slot"] = [{"id": "s1", "name": ""}]
        core._ui_dropdowns["stacker_slot"] = {"source_property": "Slots"}

        assert core._option_pairs_for_hint("stacker_slot") == [
            {"value": "s1", "label": "s1"}]

    def test_a_hint_that_is_not_property_backed_is_left_alone(self, core):
        core._ui_dropdowns["stacker_slot"] = {"folder": "Library/StackerSlots"}
        assert core._option_pairs_for_hint("stacker_slot") == []


class TestRefreshAfterACommand:

    @pytest.mark.asyncio
    async def test_a_successful_command_re_reads_the_lists_its_server_owns(self, core):
        instrument = _instrument()
        core._instruments = {"stacker": instrument}
        core.fetch_dynamic_options = AsyncMock(return_value=[])
        assert await core._refresh_options_owned_by(instrument) == ["stacker_slot"]

    @pytest.mark.asyncio
    async def test_another_server_s_lists_are_left_alone(self, core):
        owner = _instrument()
        bystander = Instrument(id="reader", name="Reader", status="online",
                               catalog=parse_catalog({}), _server=MagicMock())
        core._instruments = {"stacker": owner, "reader": bystander}
        core.fetch_dynamic_options = AsyncMock(return_value=[])
        assert await core._refresh_options_owned_by(bystander) == []
