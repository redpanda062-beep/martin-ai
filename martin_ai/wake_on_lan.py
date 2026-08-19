from __future__ import annotations

import json
import csv
import io
import re
import socket
import subprocess
from dataclasses import dataclass, asdict
from typing import Any

from .windows_process import hidden_process_kwargs

MAC_PATTERN = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def normalize_mac(value: str) -> str:
    value = value.strip().replace("-", ":").upper()
    if not MAC_PATTERN.fullmatch(value):
        raise ValueError("Invalid MAC address")
    return value


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(normalize_mac(mac).replace(":", ""))
    return b"\xff" * 6 + raw * 16


def send_magic_packet(mac: str, broadcasts: list[str] | None = None, *, repeats: int = 3) -> dict[str, Any]:
    packet = magic_packet(mac); targets = broadcasts or ["255.255.255.255"]
    sent = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(2)
        for _ in range(max(1, min(repeats, 3))):
            for target in targets:
                sock.sendto(packet, (target, 9)); sent += 1
    return {"success": True, "packets_sent": sent, "message": "Wake signal sent. Authentication is still required after startup."}


@dataclass
class WakeAdapter:
    name: str
    description: str
    mac: str
    media: str
    status: str
    magic_packet_setting: str
    wake_armed: bool
    support: str


def _powershell_json(script: str) -> Any:
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, timeout=20, errors="replace", **hidden_process_kwargs())
    if result.returncode != 0 or not result.stdout.strip(): return []
    try: return json.loads(result.stdout)
    except json.JSONDecodeError: return []


def inspect_wake_capability() -> dict[str, Any]:
    if not hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"success": True, "platform": "Unsupported", "adapters": [], "ready": False, "bios": "Unknown"}
    rows = _powershell_json("Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Select-Object Name,InterfaceDescription,MacAddress,MediaType,Status | ConvertTo-Json -Compress")
    if isinstance(rows, dict): rows = [rows]
    if not rows:
        try:
            fallback = subprocess.run(["getmac.exe", "/fo", "csv", "/v", "/nh"], capture_output=True, text=True,
                                      timeout=10, errors="replace", **hidden_process_kwargs())
            rows = [{"Name": item[0], "InterfaceDescription": item[1], "MacAddress": item[2], "MediaType": "Unknown",
                     "Status": "Connected" if "disconnected" not in item[3].lower() else "Disconnected"}
                    for item in csv.reader(io.StringIO(fallback.stdout)) if len(item) >= 4 and MAC_PATTERN.fullmatch(item[2])]
        except (OSError, subprocess.TimeoutExpired): rows = []
    if not rows:
        try:
            fallback = subprocess.run(["ipconfig.exe", "/all"], capture_output=True, text=True, timeout=10,
                                      errors="replace", **hidden_process_kwargs())
            current: dict[str, str] | None = None; rows = []
            for line in fallback.stdout.splitlines():
                header = re.match(r"^([^ ].* adapter .+):$", line.strip())
                if header:
                    current = {"Name": header.group(1), "InterfaceDescription": "", "MacAddress": "", "MediaType": header.group(1), "Status": "Connected"}; rows.append(current); continue
                if current and "Description" in line and ":" in line: current["InterfaceDescription"] = line.split(":", 1)[1].strip()
                if current and "Physical Address" in line and ":" in line: current["MacAddress"] = line.split(":", 1)[1].strip()
                if current and "Media disconnected" in line: current["Status"] = "Disconnected"
            rows = [row for row in rows if MAC_PATTERN.fullmatch(row["MacAddress"])]
        except (OSError, subprocess.TimeoutExpired): rows = []
    try:
        armed_result = subprocess.run(["powercfg.exe", "/devicequery", "wake_armed"], capture_output=True, text=True,
                                      timeout=10, errors="replace", **hidden_process_kwargs())
        armed = {line.strip().lower() for line in armed_result.stdout.splitlines() if line.strip()}
    except (OSError, subprocess.TimeoutExpired): armed = set()
    adapters = []
    for row in rows if isinstance(rows, list) else []:
        name, description = str(row.get("Name", "")), str(row.get("InterfaceDescription", ""))
        props = _powershell_json("Get-NetAdapterAdvancedProperty -Name '" + name.replace("'", "''") + "' -ErrorAction SilentlyContinue | Where-Object {$_.DisplayName -match 'Wake.*Magic|Magic.*Packet'} | Select-Object -First 1 DisplayValue | ConvertTo-Json -Compress")
        setting = str(props.get("DisplayValue", "Unknown")) if isinstance(props, dict) else "Unknown"
        is_armed = description.lower() in armed or name.lower() in armed
        ethernet = ("802.3" in str(row.get("MediaType", "")) or "ethernet" in description.lower()
                    or any(word in description.lower() for word in ("gbe", "pcie", "realtek", "ethernet")))
        explicit = setting.lower() in {"enabled", "on"}
        support = "Supported" if ethernet and (explicit or is_armed) else ("Unknown" if ethernet else "Unknown/Unsupported")
        adapters.append(asdict(WakeAdapter(name, description, str(row.get("MacAddress", "")),
            "Ethernet" if ethernet else str(row.get("MediaType", "Unknown")), str(row.get("Status", "Unknown")), setting, is_armed, support)))
    ready = any(row["support"] == "Supported" and row["mac"] for row in adapters)
    return {"success": True, "platform": "Windows", "adapters": adapters, "ready": ready, "bios": "Unknown",
            "sleep_support": "Supported" if ready else "Unknown", "hibernate_support": "Unknown", "shutdown_support": "Unknown",
            "note": "BIOS/UEFI PCIe wake cannot be verified from Windows."}
