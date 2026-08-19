from __future__ import annotations

import json
import os
import platform
import re
import socket
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
from ..windows_process import hidden_process_kwargs

REPORT_DIR = Path.home() / "Documents" / "MartinAI Reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


def _run(args: list[str], *, timeout: int = 120) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
            **hidden_process_kwargs(),
        )
        return {
            "success": completed.returncode == 0,
            "return_code": completed.returncode,
            "stdout": completed.stdout[-16000:],
            "stderr": completed.stderr[-8000:],
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Command timed out after {timeout} seconds."}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def system_summary() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    disks = []

    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append(
                {
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "filesystem": part.fstype,
                    "total_gb": round(usage.total / 2**30, 1),
                    "free_gb": round(usage.free / 2**30, 1),
                    "percent_used": usage.percent,
                }
            )
        except (PermissionError, OSError):
            continue

    return {
        "success": True,
        "computer_name": platform.node(),
        "windows": platform.platform(),
        "processor": platform.processor(),
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "ram_total_gb": round(memory.total / 2**30, 1),
        "ram_available_gb": round(memory.available / 2**30, 1),
        "ram_percent_used": memory.percent,
        "boot_time": datetime.fromtimestamp(psutil.boot_time()).isoformat(timespec="seconds"),
        "disks": disks,
    }


def cpu_status() -> dict[str, Any]:
    return {"success": True, "cpu_percent": psutil.cpu_percent(interval=0.3),
            "physical_cores": psutil.cpu_count(logical=False), "logical_cores": psutil.cpu_count(logical=True)}


def ram_status() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    return {"success": True, "total_gb": round(memory.total / 2**30, 1),
            "used_gb": round(memory.used / 2**30, 1), "available_gb": round(memory.available / 2**30, 1),
            "percent_used": memory.percent}


def storage_status() -> dict[str, Any]:
    return {"success": True, "disks": system_summary()["disks"]}


def network_status() -> dict[str, Any]:
    stats = psutil.net_if_stats()
    addresses = psutil.net_if_addrs()
    active = [name for name, status in stats.items() if status.isup and name in addresses]
    counters = psutil.net_io_counters()
    return {"success": True, "connected": bool(active), "active_adapters": active,
            "bytes_sent": counters.bytes_sent, "bytes_received": counters.bytes_recv, "changes_made": False}


def _network_configuration() -> dict[str, Any]:
    """Collect one active adapter's configuration using read-only Windows cmdlets."""
    script = r"""
$cfg = Get-NetIPConfiguration -ErrorAction SilentlyContinue |
  Where-Object { $_.NetAdapter.Status -eq 'Up' -and $_.IPv4Address } |
  Sort-Object { if ($_.IPv4DefaultGateway) { 0 } else { 1 } } |
  Select-Object -First 1
if ($cfg) {
  $adapter = Get-NetAdapter -InterfaceIndex $cfg.InterfaceIndex -ErrorAction SilentlyContinue
  [pscustomobject]@{
    name = $cfg.InterfaceAlias
    state = [string]$adapter.Status
    media_type = [string]$adapter.NdisPhysicalMedium
    ipv4 = [string]$cfg.IPv4Address.IPAddress
    prefix_length = $cfg.IPv4Address.PrefixLength
    gateway = [string]$cfg.IPv4DefaultGateway.NextHop
    dns_servers = @($cfg.DNSServer.ServerAddresses | Where-Object { $_ -match '^\d+\.' })
    link_speed = [string]$adapter.LinkSpeed
  } | ConvertTo-Json -Compress
}
"""
    result = _run(["powershell.exe", "-NoProfile", "-Command", script], timeout=15)
    raw = str(result.get("stdout", "")).strip()
    if result.get("success") and raw:
        try:
            parsed = json.loads(raw)
            parsed["success"] = True
            return parsed
        except json.JSONDecodeError:
            pass
    # psutil is a portable fallback when Windows cmdlets are unavailable.
    stats, addresses = psutil.net_if_stats(), psutil.net_if_addrs()
    for name, state in stats.items():
        ipv4 = next((a.address for a in addresses.get(name, []) if a.family == socket.AF_INET and not a.address.startswith("127.")), "")
        if state.isup and ipv4:
            speed = f"{state.speed} Mbps" if state.speed and state.speed > 0 else None
            gateway = ""
            route = _run(["route.exe", "print", "-4"], timeout=10)
            for line in str(route.get("stdout", "")).splitlines():
                fields = line.split()
                if len(fields) >= 5 and fields[0] == "0.0.0.0" and fields[1] == "0.0.0.0" and fields[3] == ipv4:
                    gateway = fields[2]; break
            dns_servers: list[str] = []
            ipconfig = _run(["ipconfig.exe", "/all"], timeout=10)
            block_match = re.search(rf"(?ims)^.*adapter\s+{re.escape(name)}:\s*(.*?)(?=^\S.*adapter\s+|\Z)",
                                    str(ipconfig.get("stdout", "")))
            if block_match:
                dns_match = re.search(r"(?im)^\s*DNS Servers[^:]*:\s*([^\r\n]+)((?:\r?\n\s{20,}[^\r\n]+)*)", block_match.group(1))
                if dns_match:
                    candidates = [dns_match.group(1), *dns_match.group(2).splitlines()]
                    dns_servers = [match.group(0) for value in candidates
                                   if (match := re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", value))]
            return {"success": True, "name": name, "state": "Up", "media_type": None,
                    "ipv4": ipv4, "prefix_length": None, "gateway": "",
                    "dns_servers": dns_servers, "link_speed": speed} | {"gateway": gateway}
    return {"success": False, "error": "No active adapter with an IPv4 address was found."}


def _connection_type(config: dict[str, Any]) -> str:
    text = f"{config.get('name', '')} {config.get('media_type', '')}".lower()
    if any(word in text for word in ("wifi", "wi-fi", "wireless", "802.11", "native 802")): return "Wi-Fi"
    if any(word in text for word in ("ethernet", "802.3")): return "Ethernet"
    return "Unavailable"


def _valid_ipv4(address: str) -> bool:
    try:
        parsed = __import__("ipaddress").ip_address(address)
        return parsed.version == 4 and not parsed.is_loopback and not parsed.is_link_local and not parsed.is_unspecified
    except ValueError:
        return False


def _ping(target: str, count: int = 4) -> dict[str, Any]:
    if not target:
        return {"success": False, "available": False, "reachable": False,
                "latency_ms": None, "packet_loss_percent": None}
    result = _run(["ping.exe", "-n", str(max(1, min(count, 10))), "-w", "1500", target], timeout=20)
    output = str(result.get("stdout", ""))
    loss_match = re.search(r"\((\d+)%\s*loss\)", output, re.I)
    latency_match = re.search(r"Average\s*=\s*(\d+)ms", output, re.I)
    samples = [float(value) for value in re.findall(r"time[=<]\s*(\d+)ms", output, re.I)]
    loss = float(loss_match.group(1)) if loss_match else None
    latency = round(sum(samples) / len(samples), 1) if samples else (float(latency_match.group(1)) if latency_match else None)
    jitter = (round(sum(abs(samples[index] - samples[index - 1]) for index in range(1, len(samples))) /
                    (len(samples) - 1), 1) if len(samples) > 1 else None)
    return {"success": bool(result.get("success")), "available": bool(loss_match),
            "target": target, "reachable": loss is not None and loss < 100,
            "latency_ms": latency, "jitter_ms": jitter, "packet_loss_percent": loss,
            "samples_ms": samples, "sample_count": count,
            "minimum_ms": min(samples) if samples else None, "maximum_ms": max(samples) if samples else None}


def check_gateway() -> dict[str, Any]:
    config = _network_configuration()
    gateway = str(config.get("gateway") or "")
    measurement = _ping(gateway)
    return {"success": bool(gateway and measurement.get("reachable")), "gateway": gateway or None, "changes_made": False,
            **{key: value for key, value in measurement.items() if key not in {"success", "target"}}}


def check_dns() -> dict[str, Any]:
    config = _network_configuration()
    hostname = "www.microsoft.com"
    started = __import__("time").perf_counter()
    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)})
        elapsed = round((__import__("time").perf_counter() - started) * 1000, 1)
        return {"success": bool(addresses), "hostname": hostname, "resolved": bool(addresses), "changes_made": False,
                "resolution_ms": elapsed, "dns_servers": config.get("dns_servers", []), "addresses": addresses}
    except OSError as exc:
        return {"success": False, "hostname": hostname, "resolved": False, "changes_made": False,
                "resolution_ms": None, "dns_servers": config.get("dns_servers", []), "error": str(exc)}


def measure_network_latency() -> dict[str, Any]:
    """Measure latency and packet loss to a fixed public diagnostic endpoint."""
    return _ping("1.1.1.1", 4) | {"changes_made": False}


def measure_gateway_latency() -> dict[str, Any]:
    config = _network_configuration()
    gateway = str(config.get("gateway") or "")
    return _ping(gateway, 8) | {"gateway": gateway or None, "context": "PC to local router", "changes_made": False}


def measure_external_latency() -> dict[str, Any]:
    return _ping("1.1.1.1", 8) | {"context": "PC to public internet endpoint", "changes_made": False}


def measure_network_jitter() -> dict[str, Any]:
    result = measure_external_latency()
    return result | {"measurement": "latency, jitter, and packet loss"}


def get_wifi_signal() -> dict[str, Any]:
    result = _run(["netsh.exe", "wlan", "show", "interfaces"], timeout=15)
    output = str(result.get("stdout", ""))
    signal = re.search(r"(?im)^\s*Signal\s*:\s*(\d+)%", output)
    receive = re.search(r"(?im)^\s*Receive rate \(Mbps\)\s*:\s*([\d.]+)", output)
    transmit = re.search(r"(?im)^\s*Transmit rate \(Mbps\)\s*:\s*([\d.]+)", output)
    radio = re.search(r"(?im)^\s*Radio type\s*:\s*(.+)$", output)
    percent = float(signal.group(1)) if signal else None
    quality = ("Strong" if percent is not None and percent >= 70 else
               "Fair" if percent is not None and percent >= 45 else
               "Weak" if percent is not None else "Unavailable")
    return {"success": percent is not None, "available": percent is not None,
            "signal_percent": percent, "quality": quality,
            "receive_link_mbps": float(receive.group(1)) if receive else None,
            "transmit_link_mbps": float(transmit.group(1)) if transmit else None,
            "radio_type": radio.group(1).strip() if radio else None, "changes_made": False}


def _link_speed_bps(text: Any) -> float | None:
    match = re.search(r"([\d.]+)\s*([GMK]?)bps", str(text or ""), re.I)
    if not match: return None
    multipliers = {"": 1.0, "K": 1e3, "M": 1e6, "G": 1e9}
    return float(match.group(1)) * multipliers[match.group(2).upper()]


def get_network_utilization() -> dict[str, Any]:
    """Measure aggregate interface traffic over one second; this is not an internet speed test."""
    config = _network_configuration(); name = str(config.get("name") or "")
    before = psutil.net_io_counters(pernic=True).get(name)
    if before is None:
        return {"success": False, "available": False, "adapter": name or None,
                "reason": "Per-interface counters are unavailable.", "changes_made": False}
    started = time.perf_counter(); time.sleep(1.0)
    after = psutil.net_io_counters(pernic=True).get(name); elapsed = max(time.perf_counter() - started, 0.001)
    if after is None:
        return {"success": False, "available": False, "adapter": name, "changes_made": False}
    receive_bps = max(0.0, (after.bytes_recv - before.bytes_recv) * 8 / elapsed)
    send_bps = max(0.0, (after.bytes_sent - before.bytes_sent) * 8 / elapsed)
    link_bps = _link_speed_bps(config.get("link_speed"))
    utilization = ((receive_bps + send_bps) / link_bps * 100) if link_bps else None
    return {"success": True, "available": True, "adapter": name,
            "receive_mbps": round(receive_bps / 1e6, 3), "send_mbps": round(send_bps / 1e6, 3),
            "combined_link_utilization_percent": round(utilization, 2) if utilization is not None else None,
            "sample_seconds": round(elapsed, 2), "note": "Interface activity, not internet throughput.",
            "changes_made": False}


def get_network_process_usage() -> dict[str, Any]:
    """Windows does not expose reliable per-process byte rates through psutil or NetTCPConnection."""
    return {"success": True, "supported": False, "processes": [],
            "reason": "Reliable per-process bandwidth rates require privileged event tracing, which Martin AI does not start automatically.",
            "changes_made": False}


def _analyze_network_performance(config: dict[str, Any], gateway: dict[str, Any], external: dict[str, Any],
                                 dns: dict[str, Any], wifi: dict[str, Any], utilization: dict[str, Any]) -> tuple[str, str]:
    if not config.get("success"):
        return "adapter_problem", "No active adapter was available for performance testing."
    signal = wifi.get("signal_percent")
    gateway_latency, gateway_jitter = gateway.get("latency_ms"), gateway.get("jitter_ms")
    external_latency, external_jitter = external.get("latency_ms"), external.get("jitter_ms")
    gateway_loss, external_loss = gateway.get("packet_loss_percent"), external.get("packet_loss_percent")
    if _connection_type(config) == "Wi-Fi" and isinstance(signal, (int, float)) and signal < 45:
        return "possible_wifi_issue", "The Wi-Fi signal is weak, which can reduce stability and performance between this PC and the router."
    if ((isinstance(gateway_latency, (int, float)) and gateway_latency > 20) or
            (isinstance(gateway_jitter, (int, float)) and gateway_jitter > 10) or
            (isinstance(gateway_loss, (int, float)) and gateway_loss > 2)):
        return "possible_local_network_issue", "Delay or packet loss is already present between this PC and the router, suggesting a local Wi-Fi or LAN issue."
    if isinstance(utilization.get("combined_link_utilization_percent"), (int, float)) and utilization["combined_link_utilization_percent"] > 70:
        return "possible_local_congestion", "The active adapter was heavily utilized during the sample, which may indicate local congestion."
    if not dns.get("resolved") or (isinstance(dns.get("resolution_ms"), (int, float)) and dns["resolution_ms"] > 500):
        return "possible_dns_delay", "DNS lookup performance is poor and may be delaying website loading."
    if ((isinstance(external_loss, (int, float)) and external_loss > 2) or
            (isinstance(external_jitter, (int, float)) and external_jitter > 20) or
            (isinstance(external_latency, (int, float)) and isinstance(gateway_latency, (int, float))
             and external_latency - gateway_latency > 75)):
        return "possible_external_issue", "The router connection looks stable, but delay increases significantly beyond the local network. This may be upstream congestion; one investigation cannot prove an ISP fault."
    return "no_obvious_cause", "No obvious cause was detected. The local router path, external latency, packet loss, jitter, DNS timing, Wi-Fi signal, and current interface activity were tested."


def investigate_network_performance() -> dict[str, Any]:
    config = _network_configuration()
    gateway = _ping(str(config.get("gateway") or ""), 8) | {"context": "PC to local router", "changes_made": False}
    external = measure_external_latency()
    dns = check_dns()
    wifi = get_wifi_signal() if _connection_type(config) == "Wi-Fi" else {"success": True, "available": False, "quality": "Not applicable"}
    utilization = get_network_utilization()
    processes = get_network_process_usage()
    issue, summary = _analyze_network_performance(config, gateway, external, dns, wifi, utilization)
    return {"success": True, "investigation_complete": True, "issue": issue, "summary": summary,
            "adapter": {"name": config.get("name"), "connection_type": _connection_type(config),
                        "link_speed": config.get("link_speed")},
            "wifi": wifi, "gateway_latency": gateway, "external_latency": external,
            "dns": dns, "utilization": utilization, "process_bandwidth": processes,
            "internet_throughput": {"tested": False, "download_mbps": None, "upload_mbps": None,
                                    "reason": "No bandwidth-heavy speed test was requested or approved."},
            "changes_made": False}


def check_internet_connectivity() -> dict[str, Any]:
    measurement = measure_network_latency()
    http_ok = False
    try:
        request = __import__("urllib.request", fromlist=["urlopen"])
        with request.urlopen("http://www.msftconnecttest.com/connecttest.txt", timeout=6) as response:
            http_ok = response.status == 200
    except OSError:
        pass
    return {"success": bool(measurement.get("reachable") or http_ok), "reachable": bool(measurement.get("reachable") or http_ok), "changes_made": False,
            "icmp_reachable": measurement.get("reachable"), "http_reachable": http_ok,
            "latency_ms": measurement.get("latency_ms"), "packet_loss_percent": measurement.get("packet_loss_percent")}


def _analyze_network(config: dict[str, Any], gateway: dict[str, Any], dns: dict[str, Any], internet: dict[str, Any]) -> tuple[str, str]:
    if not config.get("success"):
        return "adapter_disconnected", "No active network adapter with an IPv4 connection was detected."
    if not _valid_ipv4(str(config.get("ipv4") or "")):
        return "invalid_ip", f"Your PC is connected to {_connection_type(config)}, but it isn't receiving a valid IP address."
    if not gateway.get("gateway"):
        return "missing_gateway", "Your PC has a valid local address, but no default gateway is configured."
    if not gateway.get("reachable"):
        return "gateway_unreachable", "Your PC is connected to the network, but I can't reach your router."
    if not dns.get("resolved"):
        return "dns_failure", "Your internet connection appears active, but DNS isn't resolving websites correctly."
    if not internet.get("reachable"):
        return "internet_unreachable", "Your PC can reach the router, but I can't reach the internet."
    loss = internet.get("packet_loss_percent")
    latency = internet.get("latency_ms")
    if isinstance(loss, (int, float)) and loss > 5:
        return "packet_loss", f"Your connection works, but packet loss is high at {loss:g}%."
    if isinstance(latency, (int, float)) and latency > 150:
        return "high_latency", f"Your connection works, but latency is high at {latency:g} ms."
    return "healthy", "Your network looks healthy. Your adapter, IP configuration, gateway, DNS, and internet connection all passed."


def run_network_diagnostic() -> dict[str, Any]:
    config = _network_configuration()
    gateway = check_gateway() if config.get("success") else {"success": False, "reachable": False, "gateway": None}
    dns = check_dns() if config.get("success") else {"success": False, "resolved": False, "dns_servers": []}
    internet = check_internet_connectivity() if config.get("success") else {"success": False, "reachable": False}
    issue, summary = _analyze_network(config, gateway, dns, internet)
    return {"success": True, "diagnostic_complete": True, "issue": issue, "summary": summary,
            "adapter": {"name": config.get("name"), "state": config.get("state"),
                        "connection_type": _connection_type(config), "link_speed": config.get("link_speed")},
            "ip": {"address": config.get("ipv4"), "valid": _valid_ipv4(str(config.get("ipv4") or ""))},
            "gateway": gateway, "dns": dns, "internet": internet,
            "latency_ms": internet.get("latency_ms"), "packet_loss_percent": internet.get("packet_loss_percent"),
            "changes_made": False}


def top_processes(limit: int = 12) -> dict[str, Any]:
    limit = max(1, min(limit, 30))
    rows = []

    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            proc.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    psutil.cpu_percent(interval=0.8)

    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            rows.append(
                {
                    "pid": proc.pid,
                    "name": proc.info["name"],
                    "cpu_percent": proc.cpu_percent(None),
                    "memory_mb": round(proc.info["memory_info"].rss / 2**20, 1),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return {
        "success": True,
        "highest_cpu": sorted(rows, key=lambda x: x["cpu_percent"], reverse=True)[:limit],
        "highest_memory": sorted(rows, key=lambda x: x["memory_mb"], reverse=True)[:limit],
    }


def recent_windows_errors(
    log_name: str = "Application",
    hours: int = 24,
    limit: int = 30,
) -> dict[str, Any]:
    if log_name not in {"Application", "System"}:
        return {"success": False, "error": "Only Application and System logs are allowed."}

    hours = max(1, min(hours, 168))
    limit = max(1, min(limit, 100))

    script = (
        f"$events = Get-WinEvent -FilterHashtable "
        f"@{{LogName='{log_name}'; Level=1,2,3; StartTime=(Get-Date).AddHours(-{hours})}} "
        f"-ErrorAction SilentlyContinue | Select-Object -First {limit} "
        f"TimeCreated, Id, LevelDisplayName, ProviderName, Message; "
        f"$events | ConvertTo-Json -Depth 3"
    )

    result = _run(["powershell.exe", "-NoProfile", "-Command", script], timeout=45)
    if not result.get("success"):
        return result

    raw = result.get("stdout", "").strip()
    if not raw:
        return {"success": True, "events": []}

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            parsed = [parsed]
        return {"success": True, "events": parsed}
    except json.JSONDecodeError:
        return {"success": True, "raw_output": raw}


def nvidia_status() -> dict[str, Any]:
    exe = shutil.which("nvidia-smi")

    if not exe:
        candidates = [
            Path(os.environ.get("ProgramW6432", r"C:\Program Files"))
            / "NVIDIA Corporation"
            / "NVSMI"
            / "nvidia-smi.exe",
            Path(r"C:\Windows\System32\nvidia-smi.exe"),
        ]
        exe = next((str(path) for path in candidates if path.exists()), None)

    if not exe:
        return {"success": False, "error": "nvidia-smi was not found."}

    result = _run(
        [
            exe,
            "--query-gpu=name,driver_version,temperature.gpu,utilization.gpu,memory.total,memory.used",
            "--format=csv,noheader",
        ],
        timeout=20,
    )
    if not result.get("success"):
        return result
    try:
        parts = [part.strip() for part in result.get("stdout", "").splitlines()[0].split(",")]
        return {
            "success": True,
            "name": parts[0],
            "driver_version": parts[1],
            "temperature_c": float(parts[2].split()[0]),
            "utilization_percent": float(parts[3].split()[0]),
            "memory_total_mb": float(parts[4].split()[0]),
            "memory_used_mb": float(parts[5].split()[0]),
            "raw": result.get("stdout", "").strip(),
        }
    except (IndexError, ValueError):
        return {"success": False, "error": "Could not parse nvidia-smi output.", "raw": result.get("stdout", "")}


def game_status() -> dict[str, Any]:
    """Expose the existing live monitor snapshot without duplicating monitor logic."""
    from ..game_monitor import live_game_status

    status = live_game_status()
    if status is None:
        return {"success": True, "running": False, "message": "No supported game is currently running."}
    return {"success": True, "running": True, **status}


def save_diagnostic_report() -> dict[str, Any]:
    report = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "system": system_summary(),
        "processes": top_processes(),
        "nvidia": nvidia_status(),
        "application_errors": recent_windows_errors("Application", 24, 40),
        "system_errors": recent_windows_errors("System", 24, 40),
    }

    path = REPORT_DIR / f"diagnostic-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return {"success": True, "path": str(path)}
