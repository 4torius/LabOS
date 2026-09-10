"""Tests for POST /api/config: deep-merge semantics and atomic file writes.

lab_config.yaml is the single source of truth PnP discovery bootstraps from —
a shallow .update() silently drops sibling keys of a partially-updated nested
section, and a non-atomic write can corrupt the file on a crash mid-write.
"""
import yaml
import pytest
from fastapi.testclient import TestClient

import webapp.app as webapp_app


def _base_config():
    return {
        "system": {"name": "LabOS", "version": "2.0.0", "language": "en",
                    "log_level": "INFO", "auto_start_servers": True},
        "servers": {
            "opentrons": {
                "name": "Opentrons", "host": "localhost", "port": 50052,
                "enabled": True, "command_windows": ["run.bat"],
            },
            "mobile": {
                "name": "Mobile Robot", "host": "10.16.0.113", "port": 50053,
                "enabled": True, "remote": True,
                "network": {"connection_timeout": 5},
            },
        },
        "discovery": {"enabled": True, "scan_interval": 30, "service_type": "_sila._tcp.local."},
        "workflow": {"parallel_execution": True, "default_timeout": 600},
        "error_handling": {"retry_strategy": "exponential", "max_retries": 3},
        "webapp": {"nodered_url": "http://localhost:1880", "timeouts": {"discovery_timeout": 3.0}},
    }


@pytest.fixture
def client_with_isolated_config(tmp_path, monkeypatch):
    """Point webapp.app at a throwaway lab_config.yaml instead of the real one."""
    monkeypatch.setattr(webapp_app, "BASE_DIR", tmp_path)
    config_path = tmp_path / "lab_config.yaml"
    config_path.write_text(yaml.dump(_base_config(), sort_keys=False), encoding="utf-8")
    assert webapp_app.app is not None
    client = TestClient(webapp_app.app)
    return client, config_path


class TestDeepMerge:
    def test_partial_nested_update_preserves_sibling_keys(self, client_with_isolated_config):
        client, config_path = client_with_isolated_config

        resp = client.post("/api/config", json={"servers": {"mobile": {"host": "10.16.0.200"}}})

        assert resp.status_code == 200, resp.text
        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        mobile = saved["servers"]["mobile"]
        assert mobile["host"] == "10.16.0.200"  # updated
        assert mobile["port"] == 50053  # sibling key preserved, not dropped
        assert mobile["network"]["connection_timeout"] == 5  # nested sibling preserved
        # Untouched sibling server section survives entirely
        assert saved["servers"]["opentrons"]["host"] == "localhost"

    def test_new_key_in_existing_section_is_added(self, client_with_isolated_config):
        client, config_path = client_with_isolated_config

        resp = client.post("/api/config", json={"webapp": {"timeouts": {"manual_action_timeout": 111}}})

        assert resp.status_code == 200, resp.text
        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert saved["webapp"]["timeouts"]["manual_action_timeout"] == 111
        assert saved["webapp"]["timeouts"]["discovery_timeout"] == 3.0  # sibling preserved
        assert saved["webapp"]["nodered_url"] == "http://localhost:1880"  # untouched sibling key


class TestAtomicWrite:
    def test_write_failure_leaves_original_file_intact(self, client_with_isolated_config, monkeypatch):
        client, config_path = client_with_isolated_config
        original_content = config_path.read_text(encoding="utf-8")

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated crash mid-write")

        monkeypatch.setattr(webapp_app.yaml, "dump", _boom)

        resp = client.post("/api/config", json={"system": {"name": "Changed"}})

        assert resp.status_code == 500
        assert config_path.read_text(encoding="utf-8") == original_content

    def test_write_failure_does_not_leave_a_temp_file_behind(self, client_with_isolated_config, monkeypatch):
        client, config_path = client_with_isolated_config

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated crash mid-write")

        monkeypatch.setattr(webapp_app.yaml, "dump", _boom)
        client.post("/api/config", json={"system": {"name": "Changed"}})

        leftovers = list(config_path.parent.glob(".lab_config_*"))
        assert leftovers == []

    def test_successful_write_round_trips_through_get(self, client_with_isolated_config):
        client, _ = client_with_isolated_config
        resp = client.post("/api/config", json={"system": {"name": "Renamed"}})
        assert resp.status_code == 200, resp.text

        get_resp = client.get("/api/config")
        assert get_resp.status_code == 200
        assert get_resp.json()["system"]["name"] == "Renamed"
