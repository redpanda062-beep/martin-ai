from __future__ import annotations

import json
import subprocess
from typing import Any

from ..windows_process import hidden_process_kwargs


def _cim(class_name: str, properties: tuple[str, ...], *, condition: str = "") -> dict[str, Any]:
    select = ",".join(properties)
    where = f" | Where-Object {{ {condition} }}" if condition else ""
    script = f"Get-CimInstance {class_name}{where} | Select-Object {select} | ConvertTo-Json -Compress"
    try:
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                   capture_output=True, text=True, timeout=25, **hidden_process_kwargs())
        if completed.returncode != 0:
            return {"success": False, "error": completed.stderr.strip() or "Windows device query failed."}
        data = json.loads(completed.stdout or "[]")
        return {"success": True, "devices": data if isinstance(data, list) else ([data] if data else [])}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"success": False, "error": f"Device information is unavailable: {exc}"}


def display_status() -> dict[str, Any]:
    result = _cim("Win32_DesktopMonitor", ("Name", "MonitorType", "Status", "PNPDeviceID"))
    result["summary"] = f"Windows reports {len(result.get('devices', []))} monitor(s)." if result.get("success") else result.get("error")
    return result


def controller_status() -> dict[str, Any]:
    return _cim("Win32_PnPEntity", ("Name", "Status", "PNPDeviceID"), condition="$_.Name -match 'controller|gamepad|xinput'")


def usb_status() -> dict[str, Any]:
    return _cim("Win32_PnPEntity", ("Name", "Status", "PNPClass", "PNPDeviceID"), condition="$_.PNPDeviceID -like 'USB*'")


def audio_device_status() -> dict[str, Any]:
    return _cim("Win32_SoundDevice", ("Name", "Status", "Manufacturer", "PNPDeviceID"))


def network_adapter_status() -> dict[str, Any]:
    return _cim("Win32_NetworkAdapter", ("Name", "NetConnectionStatus", "NetEnabled", "AdapterType", "MACAddress"), condition="$_.PhysicalAdapter")


def bluetooth_status() -> dict[str, Any]:
    return _cim("Win32_PnPEntity", ("Name", "Status", "PNPDeviceID"), condition="$_.PNPDeviceID -like 'BTH*'")
