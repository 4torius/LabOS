"""Tests for src/settings.py — the single typed reader of lab_config.yaml."""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.settings import (  # noqa: E402
    ErrorHandlingSettings,
    ServerSpec,
    build_settings,
    get_settings,
    normalize_timeout,
    reload_settings,
)


MINIMAL_CONFIG = {
    "system": {"name": "LabOS", "version": "9.9.9", "log_level": "DEBUG"},
    "servers": {
        "opentrons": {
            "name": "Opentrons Flex", "enabled": True, "host": "localhost",
            "port": 50302, "directory": "SiLA2/OpentronsSiLA2Server",
            "command_windows": ["python", "main.py"],
            "startup_timeout": 45, "check_file": "main.py",
        },
        "mobile": {
            "name": "Mobile Robot", "enabled": True, "host": "192.168.11.22",
            "port": 50053, "remote": True,
            "network": {"connection_timeout": 5},
        },
        "offline_thing": {
            "name": "Disabled", "enabled": False, "host": "localhost", "port": 50999,
            "command_unix": ["python", "main.py"],
        },
    },
    "discovery": {"enabled": True, "scan_interval": 15, "service_type": "_sila._tcp.local."},
    "workflow": {"parallel_execution": False, "default_timeout": 600, "max_retries": 2},
    "error_handling": {
        "retry_strategy": "linear", "base_delay_seconds": 2.0,
        "max_delay_seconds": 10.0, "max_retries": 4, "enable_human_intervention": False,
    },
    "webapp": {
        "nodered_url": "http://nodered.local:1880",
        "timeouts": {"intervention_timeout": 60, "manual_action_timeout": 0,
                     "tip_refill_timeout": 120, "discovery_timeout": 2.5},
        "ui": {"auto_refresh_interval": 5},
    },
    "paths": {"library": "Library", "results": "Results"},
    "ui_dropdowns": {"plate": {"folder": "Library/Labware/Plates"}},
}


def _write_config(tmp_path: Path, config=None) -> Path:
    (tmp_path / "lab_config.yaml").write_text(
        yaml.safe_dump(config if config is not None else MINIMAL_CONFIG),
        encoding="utf-8",
    )
    return tmp_path


# ── normalize_timeout: the one implementation of "0/null = unlimited" ─────────

class TestNormalizeTimeout:

    @pytest.mark.parametrize("value", [0, 0.0, None, -1, -99.5])
    def test_unlimited_values_become_none(self, value):
        assert normalize_timeout(value) is None

    @pytest.mark.parametrize("value,expected", [(1, 1.0), (600, 600.0), ("30", 30.0), (0.5, 0.5)])
    def test_positive_values_become_float_seconds(self, value, expected):
        assert normalize_timeout(value) == expected

    def test_garbage_is_unlimited_not_an_exception(self):
        """A timeout is a safety net: a malformed value must not take down the
        run the net exists to protect."""
        assert normalize_timeout("not-a-number") is None
        assert normalize_timeout({"nope": 1}) is None


# ── Section construction ─────────────────────────────────────────────────────

class TestBuildSettings:

    def test_sections_are_typed_and_populated(self, tmp_path):
        s = build_settings(MINIMAL_CONFIG, tmp_path)
        assert s.system.version == "9.9.9"
        assert s.discovery.scan_interval == 15
        assert s.workflow.parallel_execution is False
        assert s.workflow.max_retries == 2
        assert s.error_handling.retry_strategy == "linear"
        assert s.error_handling.enable_human_intervention is False
        assert s.webapp.nodered_url == "http://nodered.local:1880"
        assert s.webapp.ui.auto_refresh_interval == 5

    def test_timeouts_are_normalized_at_construction(self, tmp_path):
        s = build_settings(MINIMAL_CONFIG, tmp_path)
        assert s.workflow.default_timeout == 600.0
        assert s.webapp.timeouts.intervention_timeout == 60.0
        # manual_action_timeout: 0 -> unlimited
        assert s.webapp.timeouts.manual_action_timeout is None

    def test_null_default_timeout_is_unlimited(self, tmp_path):
        cfg = {**MINIMAL_CONFIG, "workflow": {"default_timeout": None}}
        assert build_settings(cfg, tmp_path).workflow.default_timeout is None

    def test_missing_sections_fall_back_to_defaults(self, tmp_path):
        s = build_settings({"servers": {}}, tmp_path)
        assert s.system.name == "LabOS"
        assert s.workflow.default_timeout == 600.0
        assert s.discovery.enabled is True
        assert s.servers == {}

    def test_settings_are_frozen(self, tmp_path):
        s = build_settings(MINIMAL_CONFIG, tmp_path)
        with pytest.raises(Exception):
            s.workflow.default_timeout = 1  # type: ignore[misc]


# ── Servers ──────────────────────────────────────────────────────────────────

class TestServers:

    def test_enabled_and_local_are_distinct(self, tmp_path):
        s = build_settings(MINIMAL_CONFIG, tmp_path)
        assert set(s.enabled_servers()) == {"opentrons", "mobile"}
        # the mobile robot is enabled but hosted elsewhere: the launcher must
        # not try to start it
        assert set(s.local_servers()) == {"opentrons"}

    def test_port_map_gives_stable_instrument_ids(self, tmp_path):
        s = build_settings(MINIMAL_CONFIG, tmp_path)
        assert s.port_map()[50302].key == "opentrons"
        assert s.port_map()[50053].host == "192.168.11.22"

    def test_endpoint_falls_back_when_key_is_unknown(self, tmp_path):
        s = build_settings(MINIMAL_CONFIG, tmp_path)
        assert s.endpoint("opentrons") == ("localhost", 50302)
        assert s.endpoint("nope", "h", 1) == ("h", 1)

    def test_unknown_server_keys_are_preserved_verbatim(self, tmp_path):
        """An instrument may carry its own metadata (the mobile robot's
        `network:` block) without settings.py knowing about it."""
        s = build_settings(MINIMAL_CONFIG, tmp_path)
        assert s.servers["mobile"].extra["network"]["connection_timeout"] == 5

    def test_start_command_falls_back_to_the_other_platform(self):
        win_only = ServerSpec(key="k", name="n", command_windows=("python", "main.py"))
        assert win_only.start_command(windows=True) == ("python", "main.py")
        assert win_only.start_command(windows=False) == ("python", "main.py")
        assert ServerSpec(key="k", name="n").start_command(windows=True) == ()

    def test_address(self):
        assert ServerSpec(key="k", name="n", host="h", port=1).address == "h:1"


# ── Retry policy ─────────────────────────────────────────────────────────────

class TestRetryDelay:

    def test_exponential(self):
        eh = ErrorHandlingSettings(retry_strategy="exponential", base_delay_seconds=1.0,
                                   max_delay_seconds=60.0)
        assert [eh.retry_delay(i) for i in range(4)] == [1.0, 2.0, 4.0, 8.0]

    def test_linear(self):
        eh = ErrorHandlingSettings(retry_strategy="linear", base_delay_seconds=2.0,
                                   max_delay_seconds=60.0)
        assert [eh.retry_delay(i) for i in range(3)] == [2.0, 4.0, 6.0]

    def test_none_strategy_does_not_wait(self):
        assert ErrorHandlingSettings(retry_strategy="none").retry_delay(5) == 0.0

    def test_capped_at_max_delay(self):
        eh = ErrorHandlingSettings(base_delay_seconds=1.0, max_delay_seconds=5.0)
        assert eh.retry_delay(10) == 5.0


# ── Paths ────────────────────────────────────────────────────────────────────

class TestPaths:

    def test_configured_and_default_paths_resolve_under_base_dir(self, tmp_path):
        s = build_settings(MINIMAL_CONFIG, tmp_path)
        assert s.library_dir == (tmp_path / "Library").resolve()
        assert s.results_dir == (tmp_path / "Results").resolve()
        # not in `paths:` — falls back to the supplied default
        assert s.workflows_dir == (tmp_path / "Library/Workflows").resolve()
        assert s.database_path == (tmp_path / "Results" / "labos.db").resolve()


# ── Cache ────────────────────────────────────────────────────────────────────

class TestCache:

    def test_repeated_reads_return_the_same_snapshot(self, tmp_path):
        _write_config(tmp_path)
        reload_settings(tmp_path)
        assert get_settings(tmp_path) is get_settings(tmp_path)

    def test_reload_picks_up_an_edit(self, tmp_path):
        _write_config(tmp_path)
        reload_settings(tmp_path)
        assert get_settings(tmp_path).workflow.default_timeout == 600.0

        edited = {**MINIMAL_CONFIG, "workflow": {"default_timeout": 42}}
        _write_config(tmp_path, edited)
        # still cached until explicitly invalidated
        assert get_settings(tmp_path).workflow.default_timeout == 600.0

        assert reload_settings(tmp_path).workflow.default_timeout == 42.0

    def test_separate_roots_do_not_share_a_snapshot(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(); b.mkdir()
        _write_config(a)
        _write_config(b, {**MINIMAL_CONFIG, "workflow": {"default_timeout": 7}})
        reload_settings(a); reload_settings(b)
        assert get_settings(a).workflow.default_timeout == 600.0
        assert get_settings(b).workflow.default_timeout == 7.0

    def test_missing_config_file_yields_usable_defaults(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        s = reload_settings(empty)
        assert s.workflow.default_timeout == 600.0
        assert s.servers == {}
