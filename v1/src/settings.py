"""Typed, cached access to lab_config.yaml.

Before this module, `lab_config.yaml` was opened and parsed independently by
nine call sites, each with its own defaults, its own error handling and its
own idea of what a timeout of `0` means. `LabCore._command_timeout()` re-read
and re-parsed the whole file on *every command execution*.

This module is the single reader. `get_settings()` parses once and caches;
`reload_settings()` invalidates the cache (the config-save endpoint calls it,
so an edit in the Settings page takes effect without a restart).

Everything returned is frozen: settings are a snapshot, and code that wants a
changed value re-reads it from `get_settings()` rather than mutating a shared
object underneath other callers.

Timeout convention
------------------
`0` and `null` both mean **unlimited**, everywhere in LabOS. That is a real
requirement, not a quirk: an Opentrons protocol can legitimately run for days
(multi-day hydrogel curing with periodic reads), and a client-side deadline on
such a command reports a false failure while the instrument is still working
correctly. `normalize_timeout()` is the one implementation of that rule; every
timeout in this module is already normalized to `Optional[float]`, where
`None` means "wait indefinitely".
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from src.config_schema import ConfigValidationResult, load_lab_config

from .resources import ResourceConfig, parse_config as parse_resource_config


# ─────────────────────────────────────────────────────────────────────────────
#  Timeout normalisation — the single implementation of "0/null = unlimited"
# ─────────────────────────────────────────────────────────────────────────────

def normalize_timeout(value: Any) -> Optional[float]:
    """Return seconds as a positive float, or None for 'unlimited'.

    `None`, `0`, and any negative number all mean unlimited. A non-numeric
    value is treated as unlimited too rather than raising, because a timeout
    is a safety net: a malformed value must not take down the run that the
    net was supposed to protect.
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


# ─────────────────────────────────────────────────────────────────────────────
#  Sections
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ServerSpec:
    """One entry under `servers:` in lab_config.yaml.

    These are *bootstrap seeds*, not the source of truth about an instrument:
    they tell discovery where to look before mDNS has announced anything (and
    for servers on subnets mDNS cannot reach). Everything about what a server
    can actually do comes from the server itself at runtime.
    """

    key: str
    name: str
    enabled: bool = True
    host: str = "localhost"
    port: int = 0
    remote: bool = False
    directory: Optional[str] = None
    command_windows: Tuple[str, ...] = ()
    command_unix: Tuple[str, ...] = ()
    startup_timeout: float = 30.0
    check_file: Optional[str] = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def start_command(self, *, windows: bool) -> Tuple[str, ...]:
        """Startup command for this platform, falling back to the other one."""
        primary = self.command_windows if windows else self.command_unix
        fallback = self.command_unix if windows else self.command_windows
        return primary or fallback


@dataclass(frozen=True)
class WorkflowSettings:
    parallel_execution: bool = False
    default_timeout: Optional[float] = 600.0
    retry_on_failure: bool = False
    max_retries: int = 3


@dataclass(frozen=True)
class ErrorHandlingSettings:
    retry_strategy: str = "exponential"
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    max_retries: int = 3
    enable_human_intervention: bool = True

    def retry_delay(self, attempt: int) -> float:
        """Seconds to wait before retry number `attempt` (0-based)."""
        if self.retry_strategy == "none":
            return 0.0
        if self.retry_strategy == "linear":
            delay = self.base_delay_seconds * (attempt + 1)
        else:
            delay = self.base_delay_seconds * (2 ** attempt)
        return min(delay, self.max_delay_seconds)


@dataclass(frozen=True)
class DiscoverySettings:
    enabled: bool = True
    scan_interval: float = 30.0
    service_type: str = "_sila._tcp.local."
    bootstrap_timeout_s: float = 5.0


@dataclass(frozen=True)
class WebappTimeouts:
    intervention_timeout: Optional[float] = 3600.0
    manual_action_timeout: Optional[float] = 600.0
    tip_refill_timeout: Optional[float] = 600.0
    discovery_timeout: float = 3.0


@dataclass(frozen=True)
class WebappUISettings:
    auto_refresh_interval: float = 3.0
    toast_duration: float = 15.0
    workflow_status_auto_hide: float = 10.0


@dataclass(frozen=True)
class WebappSettings:
    nodered_url: str = "http://localhost:1880"
    timeouts: WebappTimeouts = field(default_factory=WebappTimeouts)
    ui: WebappUISettings = field(default_factory=WebappUISettings)


@dataclass(frozen=True)
class SystemSettings:
    name: str = "LabOS"
    version: str = "2.0.0"
    log_level: str = "INFO"
    auto_start_servers: bool = True


# ─────────────────────────────────────────────────────────────────────────────
#  Root
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Settings:
    base_dir: Path
    system: SystemSettings
    servers: Mapping[str, ServerSpec]
    workflow: WorkflowSettings
    error_handling: ErrorHandlingSettings
    discovery: DiscoverySettings
    webapp: WebappSettings
    paths: Mapping[str, str]
    ui_dropdowns: Mapping[str, Any]
    #: Only what no server can speak for: declarations on behalf of servers
    #: that cannot declare their own resources, and workstation aliases.
    #: See docs/RESOURCE_MODEL.md.
    resources: ResourceConfig
    validation: ConfigValidationResult
    raw: Mapping[str, Any]

    # ── Paths ───────────────────────────────────────────────────────────────

    def path(self, key: str, default: str) -> Path:
        """Absolute path for a `paths:` key, falling back to `default`."""
        return (self.base_dir / self.paths.get(key, default)).resolve()

    @property
    def library_dir(self) -> Path:
        return self.path("library", "Library")

    @property
    def results_dir(self) -> Path:
        return self.path("results", "Results")

    @property
    def workflows_dir(self) -> Path:
        return self.path("workflows", "Library/Workflows")

    @property
    def recipes_dir(self) -> Path:
        return self.path("recipes", "Library/Recipes")

    @property
    def hardware_config_dir(self) -> Path:
        return self.path("hardware_config", "Library/HardwareConfig")

    @property
    def database_path(self) -> Path:
        return self.results_dir / "labos.db"

    # ── Servers ─────────────────────────────────────────────────────────────

    def server(self, key: str) -> Optional[ServerSpec]:
        return self.servers.get(key)

    def enabled_servers(self) -> Dict[str, ServerSpec]:
        return {k: s for k, s in self.servers.items() if s.enabled}

    def local_servers(self) -> Dict[str, ServerSpec]:
        """Servers the launcher is responsible for starting."""
        return {k: s for k, s in self.servers.items() if s.enabled and not s.remote}

    def port_map(self) -> Dict[int, ServerSpec]:
        """port -> spec, used to give a discovered server a stable instrument id."""
        return {s.port: s for s in self.servers.values() if s.port}

    def endpoint(self, key: str, default_host: str = "localhost",
                 default_port: int = 0) -> Tuple[str, int]:
        spec = self.servers.get(key)
        if spec is None:
            return default_host, default_port
        return spec.host, spec.port


# ─────────────────────────────────────────────────────────────────────────────
#  Construction
# ─────────────────────────────────────────────────────────────────────────────

def _as_tuple(value: Any) -> Tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    if isinstance(value, str) and value:
        return (value,)
    return ()


_SERVER_KNOWN_KEYS = {
    "name", "enabled", "host", "port", "remote", "directory",
    "command_windows", "command_unix", "startup_timeout", "check_file",
}


def _build_server(key: str, cfg: Mapping[str, Any]) -> ServerSpec:
    return ServerSpec(
        key=key,
        name=str(cfg.get("name", key)),
        enabled=bool(cfg.get("enabled", True)),
        host=str(cfg.get("host", "localhost")),
        port=int(cfg.get("port", 0) or 0),
        remote=bool(cfg.get("remote", False)),
        directory=cfg.get("directory"),
        command_windows=_as_tuple(cfg.get("command_windows")),
        command_unix=_as_tuple(cfg.get("command_unix")),
        startup_timeout=float(cfg.get("startup_timeout", 30) or 30),
        check_file=cfg.get("check_file"),
        # Anything not in the known set is kept verbatim so an instrument can
        # carry its own metadata (the mobile robot's `network:` block, say)
        # without this module needing to know about it.
        extra={k: v for k, v in cfg.items() if k not in _SERVER_KNOWN_KEYS},
    )


def build_settings(config: Mapping[str, Any], base_dir: Path,
                   validation: Optional[ConfigValidationResult] = None) -> Settings:
    """Turn a parsed config mapping into a Settings snapshot."""
    sys_cfg = config.get("system") or {}
    wf_cfg = config.get("workflow") or {}
    eh_cfg = config.get("error_handling") or {}
    disc_cfg = config.get("discovery") or {}
    web_cfg = config.get("webapp") or {}
    web_timeouts = web_cfg.get("timeouts") or {}
    web_ui = web_cfg.get("ui") or {}

    servers = {
        key: _build_server(key, cfg or {})
        for key, cfg in (config.get("servers") or {}).items()
        if isinstance(cfg, dict)
    }

    return Settings(
        base_dir=Path(base_dir).resolve(),
        system=SystemSettings(
            name=str(sys_cfg.get("name", "LabOS")),
            version=str(sys_cfg.get("version", "2.0.0")),
            log_level=str(sys_cfg.get("log_level", "INFO")),
            auto_start_servers=bool(sys_cfg.get("auto_start_servers", True)),
        ),
        servers=servers,
        workflow=WorkflowSettings(
            parallel_execution=bool(wf_cfg.get("parallel_execution", False)),
            default_timeout=normalize_timeout(wf_cfg.get("default_timeout", 600)),
            retry_on_failure=bool(wf_cfg.get("retry_on_failure", False)),
            max_retries=int(wf_cfg.get("max_retries", 3)),
        ),
        error_handling=ErrorHandlingSettings(
            retry_strategy=str(eh_cfg.get("retry_strategy", "exponential")),
            base_delay_seconds=float(eh_cfg.get("base_delay_seconds", 1.0)),
            max_delay_seconds=float(eh_cfg.get("max_delay_seconds", 60.0)),
            max_retries=int(eh_cfg.get("max_retries", 3)),
            enable_human_intervention=bool(eh_cfg.get("enable_human_intervention", True)),
        ),
        discovery=DiscoverySettings(
            enabled=bool(disc_cfg.get("enabled", True)),
            scan_interval=float(disc_cfg.get("scan_interval", 30) or 30),
            service_type=str(disc_cfg.get("service_type", "_sila._tcp.local.")),
            bootstrap_timeout_s=float(disc_cfg.get("bootstrap_timeout_s", 5) or 5),
        ),
        webapp=WebappSettings(
            nodered_url=str(web_cfg.get("nodered_url", "http://localhost:1880")),
            timeouts=WebappTimeouts(
                intervention_timeout=normalize_timeout(web_timeouts.get("intervention_timeout", 3600)),
                manual_action_timeout=normalize_timeout(web_timeouts.get("manual_action_timeout", 600)),
                tip_refill_timeout=normalize_timeout(web_timeouts.get("tip_refill_timeout", 600)),
                discovery_timeout=float(web_timeouts.get("discovery_timeout", 3.0) or 3.0),
            ),
            ui=WebappUISettings(
                auto_refresh_interval=float(web_ui.get("auto_refresh_interval", 3) or 3),
                toast_duration=float(web_ui.get("toast_duration", 15) or 15),
                workflow_status_auto_hide=float(web_ui.get("workflow_status_auto_hide", 10) or 10),
            ),
        ),
        paths=dict(config.get("paths") or {}),
        ui_dropdowns=dict(config.get("ui_dropdowns") or {}),
        resources=parse_resource_config(config.get("resources")),
        validation=validation or ConfigValidationResult(),
        raw=dict(config),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Cache
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_BASE_DIR = Path(__file__).resolve().parent.parent
_cache: Dict[Path, Settings] = {}
_cache_lock = threading.Lock()


def get_settings(base_dir: Optional[Path] = None, *, strict: bool = False) -> Settings:
    """Return the cached Settings for `base_dir` (the v1/ project root).

    Parsing happens on the first call and after `reload_settings()`. `strict`
    only affects a fresh parse: it makes an invalid config raise instead of
    being returned with `validation.errors` populated.
    """
    root = Path(base_dir).resolve() if base_dir else _DEFAULT_BASE_DIR
    with _cache_lock:
        cached = _cache.get(root)
        if cached is not None:
            return cached

    config, validation = load_lab_config(
        root / "lab_config.yaml", apply_defaults=True, strict=strict
    )
    settings = build_settings(config, root, validation)

    with _cache_lock:
        _cache[root] = settings
    return settings


def reload_settings(base_dir: Optional[Path] = None) -> Settings:
    """Drop the cached snapshot and re-read from disk.

    Called by the config-save endpoint so an edit in the Settings page takes
    effect immediately. Passing no base_dir clears every cached root.
    """
    with _cache_lock:
        if base_dir is None:
            _cache.clear()
        else:
            _cache.pop(Path(base_dir).resolve(), None)
    return get_settings(base_dir)


__all__ = [
    "Settings", "ServerSpec", "WorkflowSettings", "ErrorHandlingSettings",
    "DiscoverySettings", "WebappSettings", "WebappTimeouts", "WebappUISettings",
    "SystemSettings", "build_settings", "get_settings", "reload_settings",
    "normalize_timeout",
]
