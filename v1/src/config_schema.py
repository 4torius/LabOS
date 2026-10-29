from __future__ import annotations

import copy
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
    # Physical resource claims — only for servers that cannot declare their own,
    # plus workstation aliases. Normally absent: an instrument states what it
    # occupies itself. See docs/RESOURCE_MODEL.md and src/resources.py.
    "resources",
}

KNOWN_TOP_LEVEL_KEYS = REQUIRED_TOP_LEVEL_KEYS | OPTIONAL_TOP_LEVEL_KEYS

DEFAULT_CONFIG: Dict[str, Any] = {
    "system": {
        "name": "LabOS",
        "version": "2.0.0",
        "log_level": "INFO",
        "auto_start_servers": True,
    },
    "servers": {},
    "discovery": {
        "enabled": True,
        "scan_interval": 30,
        "service_type": "_sila._tcp.local.",
    },
    "workflow": {
        # Safe default. Every workflow shipped in Library/Workflows declares
        # DependsOn: [], so with parallel execution on, a file whose steps are
        # merely written in order would dispatch all of them at once — for a
        # mobile robot and a liquid handler sharing a deck, that is a collision.
        # lab_config.yaml has always set this to false; the default here
        # disagreed, so deleting the key silently armed it.
        "parallel_execution": False,
        # Fallback timeout (seconds) for any step that doesn't set its own
        # TimeoutSeconds. 0 or null means unlimited for ALL such steps — prefer
        # keeping this finite (it's the safety net that detects a hung/crashed
        # instrument) and instead set TimeoutSeconds: 0 on the one step that's
        # genuinely long-running (e.g. a multi-day incubation).
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
            "discovery_timeout": 3.0,
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
    # Deep copy, not dict(base): a shallow copy would leave sections absent
    # from `override` sharing the exact same nested dict object as `base`
    # (typically the module-level DEFAULT_CONFIG) — any later in-place mutation
    # of the merged result would silently corrupt the shared default too.
    result: Dict[str, Any] = copy.deepcopy(base)
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
            has_win = isinstance(cmd_win, list) and bool(cmd_win)
            has_unix = isinstance(cmd_unix, list) and bool(cmd_unix)
            if not has_win and not has_unix:
                result.errors.append(
                    f"servers.{server_key}: at least one of command_windows or command_unix "
                    f"must be a non-empty list for local servers"
                )
        else:
            if (cmd_win and isinstance(cmd_win, list)) or (cmd_unix and isinstance(cmd_unix, list)):
                result.warnings.append(
                    f"servers.{server_key} is remote: local startup commands will be ignored"
                )

    workflow_cfg = config.get("workflow")
    default_timeout = workflow_cfg.get("default_timeout") if isinstance(workflow_cfg, dict) else None
    if not default_timeout:
        result.warnings.append(
            "workflow.default_timeout is unlimited (0/null): steps that hang or "
            "crash will not be auto-detected unless they set their own "
            "TimeoutSeconds. Prefer a finite global default and an explicit "
            "TimeoutSeconds: 0 override on individual long-running steps."
        )

    return result


def load_lab_config(
    config_path: Path,
    *,
    apply_defaults: bool = True,
    strict: bool = False,
) -> Tuple[Dict[str, Any], ConfigValidationResult]:
    if not config_path.exists():
        # deepcopy, not dict(): same identity-sharing bug as _deep_merge (see
        # its comment) — a shallow copy here would let a caller's mutation of
        # the returned fallback corrupt the shared module-level DEFAULT_CONFIG.
        fallback = copy.deepcopy(DEFAULT_CONFIG) if apply_defaults else {}
        validation = validate_lab_config(fallback)
        if strict and not validation.ok:
            raise ValueError("lab_config.yaml missing and fallback config is invalid")
        return fallback, validation

    with open(config_path, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ValueError("lab_config.yaml must define a YAML mapping at root")

    merged = _deep_merge(DEFAULT_CONFIG, raw) if apply_defaults else raw
    validation = validate_lab_config(merged)

    if strict and not validation.ok:
        raise ValueError("; ".join(validation.errors))

    return merged, validation
