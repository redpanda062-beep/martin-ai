"""Safe macOS placeholders for capabilities that are currently Windows-native.

Keeping the same callable names lets the shared router, UI, skills, and policy
registry load without importing any Windows implementation module.
"""

from __future__ import annotations

from typing import Any


def _unavailable(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {
        "success": False,
        "error": "This system capability is not implemented for macOS yet.",
        "platform": "macos",
    }


def available_apps() -> dict[str, Any]:
    return {"success": True, "apps": [], "platform": "macos"}


app_status = close_app = list_running_apps = open_app = _unavailable
audio_device_status = bluetooth_status = controller_status = _unavailable
display_status = network_adapter_status = usb_status = _unavailable
cpu_status = game_status = network_status = run_network_diagnostic = _unavailable
check_gateway = check_dns = check_internet_connectivity = _unavailable
measure_network_latency = measure_gateway_latency = measure_external_latency = _unavailable
measure_network_jitter = get_wifi_signal = get_network_utilization = _unavailable
get_network_process_usage = investigate_network_performance = _unavailable
nvidia_status = ram_status = recent_windows_errors = save_diagnostic_report = _unavailable
system_summary = storage_status = top_processes = _unavailable
clear_shader_caches = flush_dns = reset_microsoft_store_cache = _unavailable
run_dism_restorehealth = run_sfc_scan = restore_shader_caches = _unavailable
create_incident_report = defender_custom_scan = defender_full_scan = _unavailable
defender_quick_scan = network_connections = security_events = _unavailable
security_health_report = startup_entries = suspicious_processes = _unavailable
terminate_suspicious_process = update_defender_intelligence = _unavailable
watched_folders = windows_security_status = _unavailable
analyze_crash = explain_crash = get_crash_history = get_last_crash = _unavailable
