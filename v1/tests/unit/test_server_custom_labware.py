"""Unit tests for OpentronsSiLA2Server's custom-labware auto-registration
(_collect_custom_labware_definitions / _execute_protocol wiring to
robot_client.add_labware_definition).

Real-hardware bug this fixes: ctx.load_labware() only works for labware the
robot already knows (its own "opentrons" namespace catalog); a recipe
referencing genuinely custom labware (e.g. the EuroClone plate seen in this
repo's Library/Labware/Reservoirs, namespace "labos_custom") failed the run
at the very first loadLabware command with FileNotFoundError. Fix: push any
non-"opentrons"-namespace definition referenced by the recipe's Labware dict
to POST /runs/{run_id}/labware_definitions before play_run().
"""
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock

from _pkg_loader import load_opentrons_src

_ot = load_opentrons_src()


def _make_server(tmp_path):
    config = _ot.ServerConfig(config_path=str(tmp_path / "nonexistent.yaml"), use_mdns=False)
    # dir_library defaults to "../../Library", resolved (elsewhere in this
    # file, same convention) via os.path.abspath() against the process's
    # cwd - correct for the real server (which runs with cwd =
    # SiLA2/OpentronsSiLA2Server) but not for pytest's own cwd. Point it at
    # the real repo Library/ directly so these tests exercise the real
    # on-disk labware catalog instead of a fragile cwd assumption.
    config.dir_library = str(Path(__file__).resolve().parents[2] / "Library")
    srv = _ot.OpentronsSiLA2Server(config)
    srv.robot = MagicMock()
    srv.generator = MagicMock()
    return srv


class TestCollectCustomLabwareDefinitions:
    def test_none_recipe_returns_empty(self, tmp_path):
        srv = _make_server(tmp_path)
        assert srv._collect_custom_labware_definitions(None) == []

    def test_recipe_without_labware_key_returns_empty(self, tmp_path):
        srv = _make_server(tmp_path)
        assert srv._collect_custom_labware_definitions({"Steps": []}) == []

    def test_real_opentrons_labware_is_skipped(self, tmp_path):
        """corning_24_wellplate_3.4ml_flat.json declares namespace "opentrons"
        - the robot already knows it, pushing it is unnecessary and risks a
        version mismatch, so it must NOT be collected."""
        srv = _make_server(tmp_path)
        recipe = {"Labware": {"Source_1": {"LoadName": "corning_24_wellplate_3.4ml_flat", "Slot": "D1"}}}
        assert srv._collect_custom_labware_definitions(recipe) == []

    def test_real_custom_labware_is_collected(self, tmp_path):
        """primo_euroclone_48_wellplate_1000.plate.json (Library/Labware/
        Reservoirs/) declares namespace "labos_custom" - genuinely custom,
        never registered on the real robot, must be pushed."""
        srv = _make_server(tmp_path)
        recipe = {"Labware": {"Source_1": {"LoadName": "primo_euroclone_48_wellplate_1000.plate", "Slot": "A2"}}}
        defs = srv._collect_custom_labware_definitions(recipe)
        assert len(defs) == 1
        assert defs[0]["parameters"]["loadName"] == "primo_euroclone_48_wellplate_1000.plate"
        assert defs[0]["namespace"] != "opentrons"

    def test_unknown_load_name_is_silently_skipped_not_fabricated(self, tmp_path):
        """No local file for this LoadName -> skipped here, not invented.
        The run still fails at loadLabware with a clear robot-side error if
        it's genuinely unknown to the robot too - never a guessed geometry."""
        srv = _make_server(tmp_path)
        recipe = {"Labware": {"Source_1": {"LoadName": "totally_made_up_plate_xyz", "Slot": "A2"}}}
        assert srv._collect_custom_labware_definitions(recipe) == []

    def test_duplicate_load_names_collected_once(self, tmp_path):
        srv = _make_server(tmp_path)
        recipe = {"Labware": {
            "Source_1": {"LoadName": "primo_euroclone_48_wellplate_1000.plate", "Slot": "A2"},
            "Source_2": {"LoadName": "primo_euroclone_48_wellplate_1000.plate", "Slot": "B2"},
        }}
        assert len(srv._collect_custom_labware_definitions(recipe)) == 1

    def test_non_dict_labware_values_ignored(self, tmp_path):
        srv = _make_server(tmp_path)
        recipe = {"Labware": {"MyTrash": "some_string_value"}}
        assert srv._collect_custom_labware_definitions(recipe) == []


class TestExecuteProtocolPushesCustomLabware:
    @pytest.mark.asyncio
    async def test_pushes_custom_definition_before_play(self, tmp_path):
        srv = _make_server(tmp_path)
        srv.robot.upload_protocol_content = AsyncMock(return_value="proto-1")
        srv.robot.create_run = AsyncMock(return_value="run-1")
        srv.robot.add_labware_definition = AsyncMock()
        srv.robot.play_run = AsyncMock()

        recipe = {"Labware": {"Source_1": {"LoadName": "primo_euroclone_48_wellplate_1000.plate", "Slot": "A2"}}}
        run_id = await srv._execute_protocol("# fake protocol code", recipe=recipe)

        assert run_id == "run-1"
        srv.robot.add_labware_definition.assert_awaited_once()
        call_args = srv.robot.add_labware_definition.call_args
        assert call_args.args[0] == "run-1"
        assert call_args.args[1]["parameters"]["loadName"] == "primo_euroclone_48_wellplate_1000.plate"
        # Order matters: definitions must be registered before play_run().
        srv.robot.play_run.assert_awaited_once_with("run-1")

    @pytest.mark.asyncio
    async def test_no_recipe_pushes_nothing(self, tmp_path):
        srv = _make_server(tmp_path)
        srv.robot.upload_protocol_content = AsyncMock(return_value="proto-1")
        srv.robot.create_run = AsyncMock(return_value="run-1")
        srv.robot.add_labware_definition = AsyncMock()
        srv.robot.play_run = AsyncMock()

        await srv._execute_protocol("# fake protocol code")  # recipe defaults to None

        srv.robot.add_labware_definition.assert_not_awaited()
        srv.robot.play_run.assert_awaited_once_with("run-1")

    @pytest.mark.asyncio
    async def test_only_standard_opentrons_labware_pushes_nothing(self, tmp_path):
        srv = _make_server(tmp_path)
        srv.robot.upload_protocol_content = AsyncMock(return_value="proto-1")
        srv.robot.create_run = AsyncMock(return_value="run-1")
        srv.robot.add_labware_definition = AsyncMock()
        srv.robot.play_run = AsyncMock()

        recipe = {"Labware": {"TargetPlate": {"LoadName": "corning_24_wellplate_3.4ml_flat", "Slot": "D1"}}}
        await srv._execute_protocol("# fake protocol code", recipe=recipe)

        srv.robot.add_labware_definition.assert_not_awaited()
