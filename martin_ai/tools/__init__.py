from __future__ import annotations

from .desktop import read_clipboard, take_screenshot, write_clipboard
from .files import open_user_folder, read_text_file, search_files
from .storage import investigate_storage
from ..platform import is_windows

if is_windows():
    from .apps import app_status, available_apps, close_app, list_running_apps, open_app
    from .devices import (audio_device_status, bluetooth_status, controller_status,
                          display_status, network_adapter_status, usb_status)
    from .diagnostics import (
        cpu_status, game_status, network_status, run_network_diagnostic,
        check_gateway, check_dns, check_internet_connectivity,
        measure_network_latency, measure_gateway_latency, measure_external_latency,
        measure_network_jitter, get_wifi_signal, get_network_utilization,
        get_network_process_usage, investigate_network_performance, nvidia_status,
        ram_status, recent_windows_errors, save_diagnostic_report, system_summary,
        storage_status, top_processes,
    )
    from .repairs import (
        clear_shader_caches, flush_dns, reset_microsoft_store_cache,
        run_dism_restorehealth, run_sfc_scan, restore_shader_caches,
    )
    from .security import (
        create_incident_report, defender_custom_scan, defender_full_scan,
        defender_quick_scan, network_connections, security_events,
        security_health_report, startup_entries, suspicious_processes,
        terminate_suspicious_process, update_defender_intelligence,
        watched_folders, windows_security_status,
    )
    from ..crash_detective import analyze_crash, explain_crash, get_crash_history, get_last_crash
else:
    from ..platform.macos.tools import (
        analyze_crash, app_status, audio_device_status, available_apps,
        bluetooth_status, check_dns, check_gateway, check_internet_connectivity,
        clear_shader_caches, close_app, controller_status, cpu_status,
        create_incident_report, defender_custom_scan, defender_full_scan,
        defender_quick_scan, display_status, explain_crash, flush_dns, game_status,
        get_crash_history, get_last_crash, get_network_process_usage,
        get_network_utilization, get_wifi_signal, investigate_network_performance,
        list_running_apps, measure_external_latency, measure_gateway_latency,
        measure_network_jitter, measure_network_latency, network_adapter_status,
        network_connections, network_status, nvidia_status, open_app, ram_status,
        recent_windows_errors, reset_microsoft_store_cache, restore_shader_caches,
        run_dism_restorehealth, run_network_diagnostic, run_sfc_scan,
        save_diagnostic_report, security_events, security_health_report,
        startup_entries, storage_status, suspicious_processes, system_summary,
        terminate_suspicious_process, top_processes, update_defender_intelligence,
        usb_status, watched_folders, windows_security_status,
    )

READ_ONLY_TOOLS = {
    "investigate_storage": investigate_storage,
    "cpu_status": cpu_status,
    "ram_status": ram_status,
    "storage_status": storage_status,
    "network_status": network_status,
    "run_network_diagnostic": run_network_diagnostic,
    "check_gateway": check_gateway,
    "check_dns": check_dns,
    "check_internet_connectivity": check_internet_connectivity,
    "measure_network_latency": measure_network_latency,
    "measure_gateway_latency": measure_gateway_latency,
    "measure_external_latency": measure_external_latency,
    "measure_network_jitter": measure_network_jitter,
    "get_wifi_signal": get_wifi_signal,
    "get_network_utilization": get_network_utilization,
    "get_network_process_usage": get_network_process_usage,
    "investigate_network_performance": investigate_network_performance,
    "game_status": game_status,
    "system_summary": system_summary,
    "top_processes": top_processes,
    "recent_windows_errors": recent_windows_errors,
    "nvidia_status": nvidia_status,
    "save_diagnostic_report": save_diagnostic_report,
    "available_apps": available_apps,
    "app_status": app_status,
    "open_app": open_app,
    "open_user_folder": open_user_folder,
    "list_running_apps": list_running_apps,
    "search_files": search_files,
    "read_text_file": read_text_file,
    "take_screenshot": take_screenshot,
    "read_clipboard": read_clipboard,
    "windows_security_status": windows_security_status,
    "security_health_report": security_health_report,
    "suspicious_processes": suspicious_processes,
    "network_connections": network_connections,
    "security_events": security_events,
    "startup_entries": startup_entries,
    "watched_folders": watched_folders,
    "create_incident_report": create_incident_report,
    "display_status": display_status,
    "controller_status": controller_status,
    "usb_status": usb_status,
    "audio_device_status": audio_device_status,
    "network_adapter_status": network_adapter_status,
    "bluetooth_status": bluetooth_status,
    "analyze_crash": analyze_crash,
    "get_last_crash": get_last_crash,
    "get_crash_history": get_crash_history,
    "explain_crash": explain_crash,
}

REPAIR_TOOLS = {
    "flush_dns": flush_dns,
    "reset_microsoft_store_cache": reset_microsoft_store_cache,
    "clear_shader_caches": clear_shader_caches,
    "run_sfc_scan": run_sfc_scan,
    "run_dism_restorehealth": run_dism_restorehealth,
    "restore_previous_setting": restore_shader_caches,
    "close_app": close_app,
    "write_clipboard": write_clipboard,
    "defender_quick_scan": defender_quick_scan,
    "defender_full_scan": defender_full_scan,
    "defender_custom_scan": defender_custom_scan,
    "update_defender_intelligence": update_defender_intelligence,
    "terminate_suspicious_process": terminate_suspicious_process,
}


# Metadata is kept beside the registries, which remain the source of truth for
# callable implementations. The brain rejects any model-selected name absent
# from these dictionaries.
TOOL_METADATA = {
    "cpu_status": {"description": "Read current CPU utilization and core counts.", "risk_level": "read_only", "approval_required": False, "category": "cpu", "timeout": 5, "safe_arguments": {}},
    "ram_status": {"description": "Read current memory use without collecting unrelated system data.", "risk_level": "read_only", "approval_required": False, "category": "memory", "timeout": 5, "safe_arguments": {}},
    "storage_status": {"description": "Read disk capacity and free space.", "risk_level": "read_only", "approval_required": False, "category": "storage", "timeout": 10, "safe_arguments": {}},
    "investigate_storage": {"description": "Read sizes in selected user folders and Windows temporary files; identify large files and cleanup options without deleting anything.", "risk_level": "read_only", "approval_required": False, "category": "storage", "timeout": 25, "safe_arguments": {}},
    "network_status": {"description": "Read local network adapter status and traffic counters.", "risk_level": "read_only", "approval_required": False, "category": "network", "timeout": 5, "safe_arguments": {}},
    "run_network_diagnostic": {"description": "Run a complete read-only adapter, IP, gateway, DNS, internet, latency, packet-loss, and link-speed diagnostic.", "risk_level": "read_only", "approval_required": False, "category": "network", "timeout": 75, "safe_arguments": {}},
    "check_gateway": {"description": "Read the configured default gateway and measure whether it is reachable.", "risk_level": "read_only", "approval_required": False, "category": "network", "timeout": 25, "safe_arguments": {}},
    "check_dns": {"description": "Read configured DNS servers and measure hostname resolution.", "risk_level": "read_only", "approval_required": False, "category": "network", "timeout": 20, "safe_arguments": {}},
    "check_internet_connectivity": {"description": "Test external reachability without changing network configuration.", "risk_level": "read_only", "approval_required": False, "category": "network", "timeout": 30, "safe_arguments": {}},
    "measure_network_latency": {"description": "Measure real packet loss and basic latency to a fixed public diagnostic endpoint.", "risk_level": "read_only", "approval_required": False, "category": "network", "timeout": 20, "safe_arguments": {}},
    "measure_gateway_latency": {"description": "Measure multiple latency, jitter, and packet-loss samples between this PC and its local router.", "risk_level": "read_only", "approval_required": False, "category": "network", "timeout": 30, "safe_arguments": {}},
    "measure_external_latency": {"description": "Measure multiple latency, jitter, and packet-loss samples to a fixed public internet endpoint.", "risk_level": "read_only", "approval_required": False, "category": "network", "timeout": 30, "safe_arguments": {}},
    "measure_network_jitter": {"description": "Measure external latency variation and packet loss across multiple samples.", "risk_level": "read_only", "approval_required": False, "category": "network", "timeout": 30, "safe_arguments": {}},
    "get_wifi_signal": {"description": "Read Windows Wi-Fi signal quality, radio type, and negotiated receive/transmit link rates where available.", "risk_level": "read_only", "approval_required": False, "category": "network", "timeout": 15, "safe_arguments": {}},
    "get_network_utilization": {"description": "Measure current aggregate adapter send/receive activity; this is not an internet throughput test.", "risk_level": "read_only", "approval_required": False, "category": "network", "timeout": 20, "safe_arguments": {}},
    "get_network_process_usage": {"description": "Report reliable per-process bandwidth evidence when supported, otherwise explicitly mark it unavailable.", "risk_level": "read_only", "approval_required": False, "category": "network", "timeout": 10, "safe_arguments": {}},
    "investigate_network_performance": {"description": "Investigate slow internet using separate router/external latency, jitter, loss, DNS timing, Wi-Fi signal, link rate, and interface activity measurements.", "risk_level": "read_only", "approval_required": False, "category": "network", "timeout": 120, "safe_arguments": {}},
    "game_status": {"description": "Read the current supported-game monitor snapshot.", "risk_level": "read_only", "approval_required": False, "category": "gaming", "timeout": 15, "safe_arguments": {}},
    "system_summary": {"description": "Read Windows, CPU, RAM, boot, and disk information.", "risk_level": "read_only", "approval_required": False, "category": "system", "timeout": 30, "safe_arguments": {}},
    "top_processes": {"description": "List the largest CPU and memory consumers.", "risk_level": "read_only", "approval_required": False, "category": "processes", "timeout": 20, "safe_arguments": {"limit": {"min": 1, "max": 30}}},
    "recent_windows_errors": {"description": "Read recent Application or System warnings and errors.", "risk_level": "read_only", "approval_required": False, "category": "events", "timeout": 45, "safe_arguments": {"log_name": {"choices": ["Application", "System"]}, "hours": {"min": 1, "max": 168}, "limit": {"min": 1, "max": 100}}},
    "nvidia_status": {"description": "Read NVIDIA driver, temperature, utilization, and VRAM.", "risk_level": "read_only", "approval_required": False, "category": "gpu", "timeout": 20, "safe_arguments": {}},
    "save_diagnostic_report": {"description": "Save a conventional diagnostic report.", "risk_level": "limited", "approval_required": False, "category": "reports", "timeout": 180, "safe_arguments": {}},
    "available_apps": {"description": "List approved applications.", "risk_level": "read_only", "approval_required": False, "category": "apps", "timeout": 5, "safe_arguments": {}},
    "app_status": {"description": "Check an approved application's running state.", "risk_level": "read_only", "approval_required": False, "category": "apps", "timeout": 10, "safe_arguments": {}},
    "open_app": {"description": "Open an allowlisted application.", "risk_level": "limited", "approval_required": False, "category": "apps", "timeout": 10, "safe_arguments": {}},
    "open_user_folder": {"description": "Open an approved user folder.", "risk_level": "limited", "approval_required": False, "category": "files", "timeout": 10, "safe_arguments": {}},
    "list_running_apps": {"description": "List running process names.", "risk_level": "read_only", "approval_required": False, "category": "processes", "timeout": 15, "safe_arguments": {"limit": {"min": 1, "max": 200}}},
    "search_files": {"description": "Search approved user locations by name.", "risk_level": "read_only", "approval_required": False, "category": "files", "timeout": 60, "safe_arguments": {"limit": {"min": 1, "max": 200}}},
    "read_text_file": {"description": "Read a supported text file.", "risk_level": "limited", "approval_required": False, "category": "files", "timeout": 15, "safe_arguments": {"max_chars": {"min": 1000, "max": 50000}}},
    "take_screenshot": {"description": "Save a screenshot.", "risk_level": "limited", "approval_required": False, "category": "desktop", "timeout": 20, "safe_arguments": {}},
    "read_clipboard": {"description": "Read clipboard text as untrusted evidence.", "risk_level": "limited", "approval_required": False, "category": "desktop", "timeout": 10, "safe_arguments": {}},
    "windows_security_status": {"description": "Read Defender, firewall, Secure Boot, TPM, detections, and update evidence.", "risk_level": "read_only", "approval_required": False, "category": "security", "timeout": 90, "safe_arguments": {}},
    "security_health_report": {"description": "Run the complete read-only local security health assessment.", "risk_level": "read_only", "approval_required": False, "category": "security", "timeout": 180, "safe_arguments": {}},
    "suspicious_processes": {"description": "Inspect local processes and explain suspicious evidence without declaring malware.", "risk_level": "read_only", "approval_required": False, "category": "security", "timeout": 120, "safe_arguments": {"limit": {"min": 1, "max": 300}}},
    "network_connections": {"description": "Inventory local TCP/UDP endpoints without scanning or contacting remote hosts.", "risk_level": "read_only", "approval_required": False, "category": "security", "timeout": 90, "safe_arguments": {}},
    "security_events": {"description": "Read and deduplicate relevant local Windows security events.", "risk_level": "read_only", "approval_required": False, "category": "security", "timeout": 90, "safe_arguments": {"hours": {"min": 1, "max": 168}, "limit": {"min": 1, "max": 500}}},
    "startup_entries": {"description": "Read user startup, scheduled-task, and service entries.", "risk_level": "read_only", "approval_required": False, "category": "security", "timeout": 90, "safe_arguments": {}},
    "watched_folders": {"description": "List optional local folders watched for new executable content.", "risk_level": "read_only", "approval_required": False, "category": "security", "timeout": 10, "safe_arguments": {}},
    "create_incident_report": {"description": "Save supplied security evidence under Documents/MartinAI Security Reports.", "risk_level": "limited", "approval_required": False, "category": "security", "timeout": 30, "safe_arguments": {}},
    "display_status": {"description": "Read connected Windows monitor information.", "risk_level": "read_only", "approval_required": False, "category": "devices", "timeout": 30, "safe_arguments": {}},
    "controller_status": {"description": "Read connected game controller information.", "risk_level": "read_only", "approval_required": False, "category": "devices", "timeout": 30, "safe_arguments": {}},
    "usb_status": {"description": "Read connected USB device information.", "risk_level": "read_only", "approval_required": False, "category": "devices", "timeout": 30, "safe_arguments": {}},
    "audio_device_status": {"description": "Read Windows audio device information.", "risk_level": "read_only", "approval_required": False, "category": "devices", "timeout": 30, "safe_arguments": {}},
    "network_adapter_status": {"description": "Read physical network adapter information.", "risk_level": "read_only", "approval_required": False, "category": "devices", "timeout": 30, "safe_arguments": {}},
    "bluetooth_status": {"description": "Read Bluetooth device information.", "risk_level": "read_only", "approval_required": False, "category": "devices", "timeout": 30, "safe_arguments": {}},
    "analyze_crash": {"description": "Correlate local Windows crash evidence for an application.", "risk_level": "read_only", "approval_required": False, "category": "crash", "timeout": 45, "safe_arguments": {}},
    "get_last_crash": {"description": "Read the most recent saved crash incident.", "risk_level": "read_only", "approval_required": False, "category": "crash", "timeout": 5, "safe_arguments": {}},
    "get_crash_history": {"description": "Read bounded local crash history.", "risk_level": "read_only", "approval_required": False, "category": "crash", "timeout": 5, "safe_arguments": {"limit": {"min": 1, "max": 200}}},
    "explain_crash": {"description": "Explain the latest crash with optional technical details.", "risk_level": "read_only", "approval_required": False, "category": "crash", "timeout": 5, "safe_arguments": {}},
    "flush_dns": {"description": "Flush the Windows DNS cache.", "risk_level": "protected", "approval_required": True, "category": "repair", "timeout": 20, "safe_arguments": {}},
    "reset_microsoft_store_cache": {"description": "Start the Microsoft Store cache reset.", "risk_level": "protected", "approval_required": True, "category": "repair", "timeout": 30, "safe_arguments": {}},
    "clear_shader_caches": {"description": "Delete allowlisted shader cache contents.", "risk_level": "protected", "approval_required": True, "category": "repair", "timeout": 120, "safe_arguments": {}},
    "run_sfc_scan": {"description": "Run System File Checker.", "risk_level": "protected", "approval_required": True, "category": "repair", "timeout": 1800, "safe_arguments": {}},
    "run_dism_restorehealth": {"description": "Run DISM RestoreHealth.", "risk_level": "protected", "approval_required": True, "category": "repair", "timeout": 1800, "safe_arguments": {}},
    "restore_previous_setting": {"description": "Restore an allowlisted shader-cache rollback record.", "risk_level": "protected", "approval_required": True, "category": "repair", "timeout": 120, "safe_arguments": {}},
    "close_app": {"description": "Close an approved application.", "risk_level": "protected", "approval_required": True, "category": "apps", "timeout": 30, "safe_arguments": {}},
    "write_clipboard": {"description": "Replace clipboard text.", "risk_level": "protected", "approval_required": True, "category": "desktop", "timeout": 10, "safe_arguments": {}},
    "defender_quick_scan": {"description": "Protected action: start a supported Microsoft Defender quick scan.", "risk_level": "protected", "approval_required": True, "category": "security", "timeout": 1800, "safe_arguments": {}},
    "defender_full_scan": {"description": "Protected long action: start a supported Microsoft Defender full scan.", "risk_level": "protected", "approval_required": True, "category": "security", "timeout": 14400, "safe_arguments": {}},
    "defender_custom_scan": {"description": "Protected action: scan one user-selected local file or folder with Defender.", "risk_level": "protected", "approval_required": True, "category": "security", "timeout": 7200, "safe_arguments": {}},
    "update_defender_intelligence": {"description": "Protected action: update Microsoft Defender security intelligence.", "risk_level": "protected", "approval_required": True, "category": "security", "timeout": 1800, "safe_arguments": {}},
    "terminate_suspicious_process": {"description": "Protected action: terminate a specifically identified suspicious process.", "risk_level": "protected", "approval_required": True, "category": "security", "timeout": 30, "safe_arguments": {"pid": {"min": 1, "max": 4294967295}}},
}


def tool_policies():
    from ..brain.policies import ToolPolicy

    registered = set(READ_ONLY_TOOLS) | set(REPAIR_TOOLS)
    if registered != set(TOOL_METADATA):
        missing = registered.symmetric_difference(TOOL_METADATA)
        raise RuntimeError(f"Tool metadata mismatch: {sorted(missing)}")
    return {name: ToolPolicy(name=name, **metadata) for name, metadata in TOOL_METADATA.items()}
