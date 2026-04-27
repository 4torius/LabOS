from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml


@dataclass
class ConfigValidationResult:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


REQUIRED_TOP_LEVEL_KEYS = {
    "system",
    "servers",
    "discovery",
    "workflow",
    "error_handling",
    "webapp",
}

OPTIONAL_TOP_LEVEL_KEYS = {
    "paths",
    "ui_dropdowns",
}

KNOWN_TOP_LEVEL_KEYS = REQUIRED_TOP_LEVEL_KEYS | OPTIONAL_TOP_LEVEL_KEYS

DEFAULT_CONFIG: Dict[str, Any] = {
    "system": {
        "name": "LabOS",
        "version": "2.0.0",
        "language": "en",
        "log_level": "INFO",
        "auto_start_servers": True,
    },
    "servers": {},
    "discovery": {
        "enabled": True,
        "scan_interval": 30,
        "service_type": "_sila2._tcp.local.",
    },
    "workflow": {
        "parallel_execution": True,
        "default_timeout": 600,
        "retry_on_failure": False,
        "max_retries": 3,
    },
    "error_handling": {
        "retry_strategy": "exponential",
        "base_delay_seconds": 1.0,
        "max_delay_seconds": 60.0,
        "max_retries": 3,
        "enable_human_intervention": True,
    },
    "webapp": {
        "nodered_url": "http://localhost:1880",
        "timeouts": {
            "intervention_timeout": 3600,
            "manual_action_timeout": 300,
            "tip_refill_timeout": 600,
            "discovery_timeout": 1.0,
        },
    },
    "paths": {
        "library": "Library",
        "labware": "Library/Labware",
        "plates": "Library/Labware/Plates",
        "templates": "Library/Templates",
        "tipracks": "Library/Labware/TipRacks",
        "pipettes": "Library/Labware/Pipettes",
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def validate_lab_config(config: Dict[str, Any]) -> ConfigValidationResult:
    result = ConfigValidationResult()

    if not isinstance(config, dict):
        result.errors.append("Configuration root must be a mapping")
        return result

    missing = [key for key in REQUIRED_TOP_LEVEL_KEYS if key not in config]
    if missing:
        result.errors.append(f"Missing required top-level keys: {', '.join(sorted(missing))}")

    unknown = [key for key in config.keys() if key not in KNOWN_TOP_LEVEL_KEYS]
    if unknown:
        result.warnings.append(f"Unknown top-level keys: {', '.join(sorted(unknown))}")

    servers = config.get("servers")
    if not isinstance(servers, dict) or not servers:
        result.errors.append("'servers' must be a non-empty mapping")
        return result

    for server_key, server_cfg in servers.items():
        if not isinstance(server_cfg, dict):
            result.errors.append(f"servers.{server_key} must be a mapping")
            continue

        for required in ("name", "host", "port", "enabled"):
            if required not in server_cfg:
                result.errors.append(f"servers.{server_key} is missing required key '{required}'")

        port = server_cfg.get("port")
        if isinstance(port, bool) or not isinstance(port, int):
            result.errors.append(f"servers.{server_key}.port must be an integer")
        elif not (1 <= port <= 65535):
            result.errors.append(f"servers.{server_key}.port must be in range 1..65535")

        enabled = server_cfg.get("enabled")
        if not isinstance(enabled, bool):
            result.errors.append(f"servers.{server_key}.enabled must be a boolean")

        remote = bool(server_cfg.get("remote", False))
        cmd_win = server_cfg.get("command_windows")
        cmd_unix = server_cfg.get("command_unix")

        if not remote:
            if not isinstance(cmd_win, list) or not cmd_win:
                result.errors.append(
                    f"servers.{server_key}.command_windows must be a non-empty list for local servers"
                )
            if not isinstance(cmd_unix, list) or not cmd_unix:
                result.errors.append(
                    f"servers.{server_key}.command_unix must be a non-empty list for local servers"
                )
        else:
            if (cmd_win and isinstance(cmd_win, list)) or (cmd_unix and isinstance(cmd_unix, list)):
                result.warnings.append(
                    f"servers.{server_key} is remote: local startup commands will be ignored"
                )

    return result


def load_lab_config(
    config_path: Path,
    *,
    apply_defaults: bool = True,
    strict: bool = False,
) -> Tuple[Dict[str, Any], ConfigValidationResult]:
    if not config_path.exists():
        fallback = dict(DEFAULT_CONFIG) if apply_defaults else {}
        validation = validate_lab_config(fallback)
        if strict and not validation.ok:
            raise ValueError("lab_config.yaml missing and fallback config is invalid")
        return fallback, validation

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ValueError("lab_config.yaml must define a YAML mapping at root")

    merged = _deep_merge(DEFAULT_CONFIG, raw) if apply_defaults else raw
    validation = validate_lab_config(merged)

    if strict and not validation.ok:
        raise ValueError("; ".join(validation.errors))

    return merged, validation
