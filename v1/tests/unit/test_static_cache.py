"""Serving a script the browser will actually re-fetch, and picking a sane
first command for a new step.

Both come out of one report: the workflow builder's Properties panel showed
"Feature Identifier" while its own command dropdown said "Set Module Latch".

Two faults, stacked:

* The shared scripts were linked by a bare path, so a browser that had seen
  them once kept its copy. A template calling a function added to
  labos-params.js an hour earlier met a cached file without it; the call threw,
  renderProperties() never re-ran, and the panel went on showing the previous
  command's parameters. Nothing on the page looked broken.
* The previous command was GetFeatureDefinition, because a new step took
  `inst.commands[0]` and every SiLA2 server answers SiLAService first. So the
  first thing an operator saw on a fresh Opentrons step was a field asking for
  a Feature Identifier.
"""
import re
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))

APP = (V1_DIR / "webapp" / "app.py").read_text(encoding="utf-8-sig")
BASE_PAGE = (V1_DIR / "webapp" / "templates" / "base.html").read_text(encoding="utf-8")
BUILDER = (V1_DIR / "webapp" / "templates" / "workflows.html").read_text(encoding="utf-8")


class TestAChangedScriptIsFetched:

    @pytest.mark.parametrize("asset", [
        "labos-core.js", "labos-ws.js", "labos-params.js", "labos-fallback.css",
    ])
    def test_every_shared_asset_carries_the_version(self, asset):
        pattern = re.escape(asset) + r"\?v=\{\{ static_version \}\}"
        assert re.search(pattern, BASE_PAGE), f"{asset} is still linked bare"

    def test_no_shared_asset_is_linked_without_it(self):
        bare = re.findall(r'(?:src|href)="/static/[^"?]+"', BASE_PAGE)
        assert bare == [], f"linked without a version: {bare}"

    def test_the_token_is_available_to_every_template(self):
        """A global, not something each route has to remember to pass."""
        assert 'templates.env.globals["static_version"]' in APP

    def test_it_is_derived_from_the_files_themselves(self):
        assert "def _static_version(" in APP
        body = APP[APP.index("def _static_version("):]
        body = body[:body.index("\ndef ", 1)]
        assert "st_mtime" in body
        assert "rglob" in body

    def test_an_unreadable_static_directory_does_not_break_startup(self):
        body = APP[APP.index("def _static_version("):]
        body = body[:body.index("\ndef ", 1)]
        assert "except OSError" in body

    def test_the_token_changes_with_the_files(self, tmp_path):
        sys.path.insert(0, str(V1_DIR))
        from webapp.app import _static_version

        (tmp_path / "a.js").write_text("one", encoding="utf-8")
        first = _static_version(tmp_path)

        import os
        later = (tmp_path / "a.js").stat().st_mtime + 120
        os.utime(tmp_path / "a.js", (later, later))
        assert _static_version(tmp_path) != first

    def test_it_is_stable_when_nothing_changed(self, tmp_path):
        from webapp.app import _static_version
        (tmp_path / "a.js").write_text("one", encoding="utf-8")
        assert _static_version(tmp_path) == _static_version(tmp_path)


class TestANewStepOpensOnARealCommand:

    def test_it_does_not_take_the_first_command_blindly(self):
        assert "command: inst.commands[0].id," not in BUILDER
        assert "command: _defaultCommand(inst)," in BUILDER

    def test_sila_service_is_skipped(self):
        body = BUILDER[BUILDER.index("function _defaultCommand("):]
        body = body[:body.index("\nfunction ", 1)]
        assert "c.feature !== 'SiLAService'" in body

    def test_it_still_falls_back_rather_than_returning_nothing(self):
        """An instrument that only exposes SiLAService is odd, not a crash."""
        body = BUILDER[BUILDER.index("function _defaultCommand("):]
        body = body[:body.index("\nfunction ", 1)]
        assert "(real || inst.commands[0]).id" in body

    def test_the_feature_name_reaches_the_builder(self):
        """It is what tells plumbing apart from lab commands, and it was being
        dropped when the API's command list was converted."""
        assert BUILDER.count("feature: cmd.feature || '',") == 2

    def test_the_empty_command_list_guard_is_still_there(self):
        """An instrument can be discovered with no commands at all."""
        body = BUILDER[BUILDER.index("function addStep(type) {"):]
        body = body[:body.index("\nfunction ", 1)]
        assert "inst.commands.length === 0" in body
        assert body.index("inst.commands.length === 0") < body.index("_defaultCommand(inst)")
