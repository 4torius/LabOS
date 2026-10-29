"""Tests for labos.py — the command line.

The behaviour that matters here is `new-server`: adding an instrument was a
nine-step manual procedure where every step was a chance to get something
subtly wrong, and the failure mode was a server that starts and then does not
appear. These check that the scaffold it produces is internally consistent.
"""
import json
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))

import labos  # noqa: E402


class TestParser:

    @pytest.mark.parametrize("command", [
        "status", "instruments", "validate", "run", "read",
        "new-server", "codegen", "doctor", "folders", "call",
    ])
    def test_every_documented_command_is_wired(self, command):
        parser = labos.build_parser()
        actions = [a for a in parser._subparsers._actions if hasattr(a, "choices")]
        choices = {c for a in actions for c in (a.choices or {})}
        assert command in choices

    def test_call_requires_confirmation_by_default(self):
        args = labos.build_parser().parse_args(["call", "stacker", "Home"])
        assert args.yes is False, "a single command can move hardware too"

    def test_call_reads_values_as_json_when_they_are(self):
        assert labos._parse_assignments(["Rpm=300", "Open=false", "Label=deck A", 'L=["a"]']) == {
            "Rpm": 300, "Open": False, "Label": "deck A", "L": ["a"]}

    def test_call_refuses_a_pair_without_equals(self):
        with pytest.raises(ValueError):
            labos._parse_assignments(["Rpm"])

    def test_run_requires_confirmation_by_default(self):
        args = labos.build_parser().parse_args(["run", "W"])
        assert args.yes is False, "running a workflow must confirm before moving hardware"
        assert labos.build_parser().parse_args(["run", "W", "-y"]).yes is True

    def test_a_missing_command_is_an_error_not_a_default(self):
        with pytest.raises(SystemExit):
            labos.build_parser().parse_args([])


class TestPortAllocation:

    def test_picks_the_first_free_port_in_range(self):
        assert labos._next_free_port(set()) == 50061
        assert labos._next_free_port({50061, 50062}) == 50063

    def test_raises_rather_than_colliding_when_the_range_is_full(self):
        with pytest.raises(RuntimeError):
            labos._next_free_port(set(range(50061, 50100)))


class TestWorkflowResolution:

    def test_finds_by_bare_id_file_name_and_path(self, monkeypatch, tmp_path):
        workflows = tmp_path / "Library" / "Workflows"
        workflows.mkdir(parents=True)
        target = workflows / "Demo.workflow.json"
        target.write_text("{}", encoding="utf-8")

        class _Settings:
            workflows_dir = workflows

        monkeypatch.setattr(labos, "get_settings" if hasattr(labos, "get_settings") else "BASE_DIR",
                            labos.BASE_DIR, raising=False)
        import src.settings as settings_module
        monkeypatch.setattr(settings_module, "get_settings", lambda *a, **k: _Settings())

        assert labos._resolve_workflow("Demo") == target
        assert labos._resolve_workflow("Demo.workflow.json") == target
        assert labos._resolve_workflow(str(target)) == target
        assert labos._resolve_workflow("NoSuchThing") is None


class TestNewServerScaffold:
    """Builds a real server folder in a temp tree, then removes it."""

    @pytest.fixture
    def scaffold(self, monkeypatch, tmp_path):
        # A whole project root, so nothing touches the real one.
        root = tmp_path / "v1"
        (root / "SiLA2").mkdir(parents=True)
        shutil.copytree(V1_DIR / "SiLA2" / "_NewInstrumentTemplate",
                        root / "SiLA2" / "_NewInstrumentTemplate",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(V1_DIR / "lab_config.yaml", root / "lab_config.yaml")

        monkeypatch.setattr(labos, "BASE_DIR", root)
        monkeypatch.setattr(labos, "TEMPLATE_DIR", root / "SiLA2" / "_NewInstrumentTemplate")
        # Codegen shells out and needs a working sila2 install; the scaffold's
        # consistency is what is under test here, not the generator.
        monkeypatch.setattr(labos, "_codegen", lambda *a, **k: 0)
        monkeypatch.setattr(labos, "_register_server", lambda *a, **k: None)

        import src.settings as settings_module
        monkeypatch.setattr(settings_module, "get_settings",
                            lambda *a, **k: settings_module.build_settings(
                                yaml.safe_load((root / "lab_config.yaml").read_text(encoding="utf-8")),
                                root))
        return root

    def _make(self, root, name="Centrifuge", port=None, key=None, results=None, library=None):
        import argparse
        args = argparse.Namespace(name=name, port=port, key=key, results=results, library=library)
        code = labos.cmd_new_server(args)
        return code, root / "SiLA2" / f"{name}SiLA2Server"

    def test_creates_a_coherently_renamed_server(self, scaffold):
        code, folder = self._make(scaffold)
        assert code == 0
        assert (folder / "features" / "Centrifuge.sila.xml").is_file()
        assert (folder / "src" / "centrifuge_impl.py").is_file()
        assert not (folder / "src" / "servicer.py").exists()
        assert not (folder / "features" / "NewInstrument.sila.xml").exists()

    def test_the_feature_identifier_matches_the_instrument(self, scaffold):
        _, folder = self._make(scaffold)
        fdl = (folder / "features" / "Centrifuge.sila.xml").read_text(encoding="utf-8")
        assert "<Identifier>Centrifuge</Identifier>" in fdl
        assert "NewInstrument" not in fdl

    def test_the_entry_point_imports_the_renamed_module(self, scaffold):
        _, folder = self._make(scaffold)
        main_py = (folder / "main.py").read_text(encoding="utf-8")
        assert "centrifuge_impl" in main_py
        assert "src.servicer" not in main_py
        assert "NewInstrument" not in main_py

    def test_config_gets_a_real_port_and_name(self, scaffold):
        _, folder = self._make(scaffold, port=50077)
        config = yaml.safe_load((folder / "config.yaml").read_text(encoding="utf-8"))
        assert config["server"]["port"] == 50077
        assert config["sila2"]["server_name"] == "Centrifuge"
        assert "TODO" not in config["sila2"]["server_name"]

    def test_the_catalog_declares_no_folders_unless_asked(self, scaffold):
        """The template's example entries would advertise folders this
        instrument does not use, and populate UI dropdowns with them."""
        _, folder = self._make(scaffold)
        catalog = json.loads((folder / "library_catalog.json").read_text(encoding="utf-8"))
        assert catalog["library_types"] == [] and catalog["result_types"] == []

    def test_results_and_library_folders_are_declared_from_the_command_line(self, scaffold):
        """`--results Cells` is the whole of "this instrument needs a Cells folder"."""
        from src.catalog import parse_catalog
        _, folder = self._make(scaffold, name="CellImager", results=["Cells"], library=["Library/Stains"])
        catalog = parse_catalog((folder / "library_catalog.json").read_text(encoding="utf-8"))
        assert catalog.problems == []
        assert ("results", "Results/Cells") in catalog.declared_folders()
        assert ("library", "Library/Stains") in catalog.declared_folders()

    @pytest.mark.parametrize("bad", ["../escape", "C:/Windows", "Cells;rm"])
    def test_an_unusable_folder_is_refused_before_anything_is_created(self, scaffold, bad):
        code, folder = self._make(scaffold, name="CellImager", results=[bad])
        assert code == 1
        assert not folder.exists()

    def test_the_server_type_is_one_sila2_accepts(self, scaffold):
        """The template said "instrument"; SiLA2 requires CamelCase, so every
        scaffolded server raised ValueError before it could start."""
        _, folder = self._make(scaffold)
        config = yaml.safe_load((folder / "config.yaml").read_text(encoding="utf-8"))
        assert re.fullmatch(r"[A-Z][a-zA-Z0-9]*", config["sila2"]["server_type"])
        main_py = (folder / "main.py").read_text(encoding="utf-8")
        default = re.search(r'sila2_cfg\.get\("server_type", "([^"]*)"\)', main_py).group(1)
        assert re.fullmatch(r"[A-Z][a-zA-Z0-9]*", default)

    def test_a_run_of_the_template_does_not_leak_its_identity(self, scaffold):
        (scaffold / "SiLA2" / "_NewInstrumentTemplate" / ".server_uuid").write_text(
            "0c1f8cb7-664a-4570-a015-b38e46fac549", encoding="utf-8")
        _, folder = self._make(scaffold)
        assert not (folder / ".server_uuid").exists(), "two servers would share one mDNS identity"

    def test_the_shared_library_provider_is_left_untouched(self, scaffold):
        _, folder = self._make(scaffold)
        assert (folder / "src" / "library_provider_impl.py").is_file()
        assert (folder / "generated" / "laboslibraryprovider").is_dir()

    def test_the_port_defaults_into_the_reserved_range(self, scaffold):
        _, folder = self._make(scaffold)
        config = yaml.safe_load((folder / "config.yaml").read_text(encoding="utf-8"))
        assert 50061 <= config["server"]["port"] <= 50099

    @pytest.mark.parametrize("name", ["centrifuge", "plate sealer", "2Robots", "Plate-Sealer"])
    def test_a_name_that_would_break_codegen_is_refused_up_front(self, scaffold, name):
        import argparse
        assert labos.cmd_new_server(
            argparse.Namespace(name=name, port=None, key=None)) == 1

    def test_refuses_to_overwrite_an_existing_server(self, scaffold):
        assert self._make(scaffold)[0] == 0
        assert self._make(scaffold)[0] == 1


class TestRegisterServer:

    def test_the_seed_is_appended_without_destroying_comments(self, monkeypatch, tmp_path):
        """lab_config.yaml is heavily commented on purpose; a yaml round-trip
        would silently delete every one of those comments."""
        root = tmp_path / "v1"
        root.mkdir()
        original = (V1_DIR / "lab_config.yaml").read_text(encoding="utf-8")
        (root / "lab_config.yaml").write_text(original, encoding="utf-8")
        monkeypatch.setattr(labos, "BASE_DIR", root)

        labos._register_server("centrifuge", "Centrifuge", 50061, "CentrifugeSiLA2Server")

        updated = (root / "lab_config.yaml").read_text(encoding="utf-8")
        parsed = yaml.safe_load(updated)
        assert parsed["servers"]["centrifuge"]["port"] == 50061
        assert parsed["servers"]["centrifuge"]["directory"] == "SiLA2/CentrifugeSiLA2Server"
        # every original comment line survives
        for line in original.splitlines():
            if line.strip().startswith("#"):
                assert line in updated
        # and the servers that were already there are untouched
        assert set(yaml.safe_load(original)["servers"]) <= set(parsed["servers"])
