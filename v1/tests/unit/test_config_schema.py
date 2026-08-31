"""Tests for src/config_schema.py — validation, deep-merge, strict-mode loading."""
import yaml

from src.config_schema import (
    DEFAULT_CONFIG,
    _deep_merge,
    load_lab_config,
    validate_lab_config,
)


def _minimal_valid_config():
    return {
        "system": {},
        "servers": {
            "opentrons": {"name": "Opentrons", "host": "localhost", "port": 50052, "enabled": True,
                           "command_windows": ["run.bat"]},
        },
        "discovery": {},
        "workflow": {},
        "error_handling": {},
        "webapp": {},
    }


class TestValidateLabConfig:
    def test_valid_config_has_no_errors(self):
        result = validate_lab_config(_minimal_valid_config())
        assert result.ok
        assert result.errors == []

    def test_non_mapping_root_is_rejected(self):
        result = validate_lab_config(["not", "a", "mapping"])
        assert not result.ok
        assert "mapping" in result.errors[0]

    def test_missing_required_top_level_keys_reported(self):
        result = validate_lab_config({"servers": {"x": {}}})
        assert not result.ok
        assert any("Missing required top-level keys" in e for e in result.errors)

    def test_unknown_top_level_key_is_a_warning_not_an_error(self):
        cfg = _minimal_valid_config()
        cfg["something_unexpected"] = {}
        result = validate_lab_config(cfg)
        assert result.ok
        assert any("Unknown top-level keys" in w for w in result.warnings)

    def test_empty_servers_mapping_is_an_error(self):
        cfg = _minimal_valid_config()
        cfg["servers"] = {}
        result = validate_lab_config(cfg)
        assert not result.ok
        assert any("non-empty mapping" in e for e in result.errors)

    def test_server_missing_required_field_reported(self):
        cfg = _minimal_valid_config()
        del cfg["servers"]["opentrons"]["port"]
        result = validate_lab_config(cfg)
        assert not result.ok
        assert any("missing required key 'port'" in e for e in result.errors)

    def test_server_port_out_of_range_reported(self):
        cfg = _minimal_valid_config()
        cfg["servers"]["opentrons"]["port"] = 70000
        result = validate_lab_config(cfg)
        assert not result.ok
        assert any("port must be in range" in e for e in result.errors)

    def test_server_port_must_be_int_not_bool(self):
        cfg = _minimal_valid_config()
        cfg["servers"]["opentrons"]["port"] = True
        result = validate_lab_config(cfg)
        assert not result.ok
        assert any("must be an integer" in e for e in result.errors)

    def test_local_server_without_startup_commands_is_an_error(self):
        cfg = _minimal_valid_config()
        del cfg["servers"]["opentrons"]["command_windows"]
        result = validate_lab_config(cfg)
        assert not result.ok
        assert any("command_windows or command_unix" in e for e in result.errors)

    def test_remote_server_does_not_need_startup_commands(self):
        cfg = _minimal_valid_config()
        del cfg["servers"]["opentrons"]["command_windows"]
        cfg["servers"]["opentrons"]["remote"] = True
        result = validate_lab_config(cfg)
        assert result.ok

    def test_remote_server_with_startup_commands_warns(self):
        cfg = _minimal_valid_config()
        cfg["servers"]["opentrons"]["remote"] = True
        result = validate_lab_config(cfg)
        assert result.ok
        assert any("will be ignored" in w for w in result.warnings)


class TestDeepMerge:
    def test_nested_dict_is_merged_not_replaced(self):
        base = {"a": {"x": 1, "y": 2}}
        override = {"a": {"y": 99}}
        merged = _deep_merge(base, override)
        assert merged == {"a": {"x": 1, "y": 99}}

    def test_non_dict_override_replaces_value(self):
        base = {"a": {"x": 1}}
        override = {"a": [1, 2, 3]}
        merged = _deep_merge(base, override)
        assert merged["a"] == [1, 2, 3]

    def test_new_keys_are_added(self):
        base = {"a": 1}
        override = {"b": 2}
        merged = _deep_merge(base, override)
        assert merged == {"a": 1, "b": 2}

    def test_does_not_mutate_inputs(self):
        base = {"a": {"x": 1}}
        override = {"a": {"y": 2}}
        _deep_merge(base, override)
        assert base == {"a": {"x": 1}}
        assert override == {"a": {"y": 2}}

    def test_untouched_section_does_not_share_identity_with_base(self):
        """A section absent from `override` must be a real copy, not the same
        dict object as in `base` — otherwise mutating the merged result (a
        normal thing to do with a loaded config) silently corrupts `base`
        (in practice, the module-level DEFAULT_CONFIG)."""
        base = {"untouched": {"x": 1}, "touched": {"y": 1}}
        override = {"touched": {"y": 2}}
        merged = _deep_merge(base, override)

        merged["untouched"]["x"] = 999
        assert base["untouched"]["x"] == 1


class TestLoadLabConfig:
    def test_missing_file_with_defaults_returns_default_config(self, tmp_path):
        config, validation = load_lab_config(tmp_path / "missing.yaml", apply_defaults=True, strict=False)
        assert config == DEFAULT_CONFIG
        # DEFAULT_CONFIG ships with servers={} (no instrument is default-configured),
        # so it is expected to fail validation on its own — a real lab_config.yaml
        # always supplies at least one server.
        assert not validation.ok
        assert "servers" in validation.errors[0]

    def test_missing_file_fallback_does_not_share_identity_with_default_config(self, tmp_path):
        config, _ = load_lab_config(tmp_path / "missing.yaml", apply_defaults=True, strict=False)
        config["workflow"]["default_timeout"] = 0
        assert DEFAULT_CONFIG["workflow"]["default_timeout"] != 0

    def test_missing_file_without_defaults_returns_empty_and_invalid(self, tmp_path):
        config, validation = load_lab_config(tmp_path / "missing.yaml", apply_defaults=False, strict=False)
        assert config == {}
        assert not validation.ok

    def test_missing_file_strict_raises(self, tmp_path):
        import pytest
        with pytest.raises(ValueError):
            load_lab_config(tmp_path / "missing.yaml", apply_defaults=False, strict=True)

    def test_present_file_is_deep_merged_with_defaults(self, tmp_path):
        cfg_path = tmp_path / "lab_config.yaml"
        cfg_path.write_text(yaml.dump({
            "servers": {"opentrons": {"name": "O", "host": "h", "port": 1, "enabled": True,
                                       "command_windows": ["x"]}},
            "discovery": {"scan_interval": 999},
        }))
        config, validation = load_lab_config(cfg_path, apply_defaults=True, strict=False)
        assert config["discovery"]["scan_interval"] == 999
        # Untouched defaults (e.g. service_type) survive the merge
        assert config["discovery"]["service_type"] == DEFAULT_CONFIG["discovery"]["service_type"]
        assert validation.ok

    def test_non_mapping_yaml_root_raises(self, tmp_path):
        import pytest
        cfg_path = tmp_path / "lab_config.yaml"
        cfg_path.write_text(yaml.dump(["not", "a", "mapping"]))
        with pytest.raises(ValueError):
            load_lab_config(cfg_path, apply_defaults=True, strict=False)

    def test_strict_mode_raises_on_invalid_merged_config(self, tmp_path):
        import pytest
        cfg_path = tmp_path / "lab_config.yaml"
        cfg_path.write_text(yaml.dump({"servers": {}}))  # empty servers -> invalid
        with pytest.raises(ValueError):
            load_lab_config(cfg_path, apply_defaults=True, strict=True)
