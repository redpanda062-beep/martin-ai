from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .paths import DATA_DIR, LOCAL_CONFIG_PATH

APP_DIR = DATA_DIR
CONFIG_PATH = LOCAL_CONFIG_PATH
DEFAULT_SETTINGS: dict[str, Any] = {
    "schema_version": 1,
    "simulation_mode": True,
    "autonomy_enabled": True,
    "self_repair": {"enabled": True, "auto_level_1": False},
    "game_mode": {
        "automatic_detection": True, "auto_enable": True, "performance_monitoring": True,
        "crash_detection": True, "session_reports": True, "performance_notifications": True,
        "optimization_level": "Balanced", "ai_resource_mode": True,
        "allow_idle_model_unload": False
    },
    "permission_level": 1,
    "passive_monitoring_when_stopped": True,
    "memory": {"automatic_enabled": True, "retrieval_limit": 5},
    "skills": {"enabled": {}},
    "crash_detective": {"enabled": True, "event_poll_seconds": 30, "notification_cooldown_seconds": 300},
    "phone_remote": {
        "enabled": False, "port": 8765, "allow_chat": True, "allow_pc_controls": True,
        "allow_remote_input": False, "allow_game_controls": True,
        "require_power_confirmation": True, "remote_quality": "Balanced",
        "gaming_remote_quality": "Low", "control_timeout_minutes": 15,
        "background_after_login": True, "auto_start_martin_ai": False
    },
    "monitoring": {
        "normal_interval_seconds": 15,
        "game_interval_seconds": 3,
        "event_interval_seconds": 120,
        "security_interval_seconds": 300,
        "network_probe_interval_seconds": 60,
    },
    "agent": {
        "maximum_retries": 2,
        "maximum_actions_per_hour": 10,
        "incident_cooldown_seconds": 300,
        "verification_delay_seconds": 5,
    },
}


def _merge(defaults: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(defaults)
    for key, value in supplied.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_settings(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        settings = deepcopy(DEFAULT_SETTINGS)
        save_settings(settings, path)
        return settings
    try:
        supplied = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(supplied, dict):
            raise ValueError("settings root must be an object")
    except (OSError, ValueError, json.JSONDecodeError):
        return deepcopy(DEFAULT_SETTINGS)
    return _merge(DEFAULT_SETTINGS, supplied)


def save_settings(settings: dict[str, Any], path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_merge(DEFAULT_SETTINGS, settings), indent=2), encoding="utf-8")
