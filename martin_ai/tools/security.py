from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from ..windows_process import hidden_process_kwargs


REPORT_DIR = Path.home() / "Documents" / "MartinAI Security Reports"
_SCRIPT_HOSTS = {"powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe", "mshta.exe",
                 "rundll32.exe", "regsvr32.exe", "cmd.exe"}
_UNUSUAL_PARTS = ("\\temp\\", "\\appdata\\local\\temp\\", "\\downloads\\")
_SAFE_ACTIONS = {"defender_quick_scan", "defender_full_scan", "defender_custom_scan",
                 "update_defender_intelligence", "terminate_suspicious_process"}


def _ps(script: str, timeout: int = 60) -> tuple[bool, str]:
    """Run a fixed, local, read-only Windows query (never user-authored PowerShell)."""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout, **hidden_process_kwargs(),
        )
        output = (result.stdout or result.stderr).strip()
        return result.returncode == 0, output
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def _json_ps(script: str, timeout: int = 60) -> tuple[Any, str | None]:
    ok, output = _ps(f"$ErrorActionPreference='Stop'; {script} | ConvertTo-Json -Depth 6 -Compress", timeout)
    if not ok:
        return None, output or "Access denied or status unavailable."
    try:
        return json.loads(output) if output else None, None
    except json.JSONDecodeError:
        return None, "Windows returned an unreadable status."


def parse_defender_status(data: dict[str, Any] | None) -> dict[str, Any]:
    data = data or {}
    return {
        "antivirus_enabled": data.get("AntivirusEnabled"),
        "real_time_protection_enabled": data.get("RealTimeProtectionEnabled"),
        "antispyware_enabled": data.get("AntispywareEnabled"),
        "last_quick_scan": data.get("QuickScanEndTime"),
        "last_full_scan": data.get("FullScanEndTime"),
        "signature_updated": data.get("AntivirusSignatureLastUpdated"),
    }


def parse_firewall_status(data: Any) -> dict[str, bool | None]:
    rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    result: dict[str, bool | None] = {"Domain": None, "Private": None, "Public": None}
    for row in rows:
        name = str(row.get("Name", "")).title()
        if name in result:
            result[name] = bool(row.get("Enabled"))
    return result


def windows_security_status() -> dict[str, Any]:
    defender, defender_error = _json_ps("Get-MpComputerStatus | Select AntivirusEnabled,RealTimeProtectionEnabled,AntispywareEnabled,QuickScanEndTime,FullScanEndTime,AntivirusSignatureLastUpdated")
    firewall, firewall_error = _json_ps("Get-NetFirewallProfile | Select Name,Enabled")
    threats, threats_error = _json_ps("@(Get-MpThreatDetection | Select -First 20 ThreatID,ThreatStatusID,InitialDetectionTime,Resources)")
    secure_boot, secure_error = _json_ps("[pscustomobject]@{SecureBoot=(Confirm-SecureBootUEFI)}")
    tpm, tpm_error = _json_ps("Get-Tpm | Select TpmPresent,TpmReady")
    updates, update_error = _json_ps("@(Get-HotFix | Sort InstalledOn -Descending | Select -First 10 HotFixID,InstalledOn)")
    errors = {k: v for k, v in {"defender": defender_error, "firewall": firewall_error,
              "detections": threats_error, "secure_boot": secure_error, "tpm": tpm_error,
              "updates": update_error}.items() if v}
    parsed_defender = parse_defender_status(defender)
    parsed_firewall = parse_firewall_status(firewall)
    needs_attention = bool(errors) or parsed_defender["real_time_protection_enabled"] is not True or any(v is not True for v in parsed_firewall.values()) or bool(threats)
    return {"success": True, "status": "Needs attention" if needs_attention else "Protected",
            "defender": parsed_defender, "firewall": parsed_firewall,
            "recent_detections": threats or [], "secure_boot": secure_boot,
            "tpm": tpm, "recent_updates": updates or [],
            "pending_security_updates": "unavailable", "permission_errors": errors,
            "disclaimer": "This snapshot cannot prove that the PC is completely secure."}


def score_suspicious_process(process: dict[str, Any]) -> dict[str, Any]:
    name = str(process.get("name") or process.get("Name") or "").lower()
    path = str(process.get("path") or process.get("Path") or "")
    signature = str(process.get("signature_status") or process.get("SignatureStatus") or "Unknown")
    restarts = int(process.get("restart_count") or 0)
    evidence: list[str] = []
    score = 0
    lower_path = path.lower()
    if path and any(part in lower_path for part in _UNUSUAL_PARTS):
        score += 2; evidence.append("Executable is running from a user-writable or temporary location.")
    if signature.lower() not in {"valid", "signed"}:
        score += 1; evidence.append("Publisher signature is absent, invalid, or unavailable.")
    if name in _SCRIPT_HOSTS:
        score += 1; evidence.append("A command or script host is active; its name alone is not proof of malware.")
    if restarts >= 3:
        score += 2; evidence.append(f"Process restarted {restarts} times during observation.")
    label = "suspicious" if score >= 4 else "uncertain" if score >= 2 else "likely safe"
    return {"score": score, "label": label, "evidence": evidence or ["No unusual evidence was observed in the available fields."]}


def suspicious_processes(limit: int = 100) -> dict[str, Any]:
    script = "@(Get-CimInstance Win32_Process | Select -First %d Name,ProcessId,ParentProcessId,ExecutablePath,WorkingSetSize,CreationDate)" % max(1, min(limit, 300))
    rows, error = _json_ps(script)
    if error:
        return {"success": False, "error": error, "processes": [], "permission_limited": True}
    rows = rows if isinstance(rows, list) else ([rows] if rows else [])
    output = []
    for row in rows:
        path = row.get("ExecutablePath") or ""
        signature = "Unknown"
        if path:
            sig, _ = _json_ps(f"Get-AuthenticodeSignature -LiteralPath '{path.replace("'", "''")}' | Select Status,SignerCertificate")
            signature = str((sig or {}).get("Status", "Unknown"))
        item = {"name": row.get("Name"), "pid": row.get("ProcessId"), "parent_pid": row.get("ParentProcessId"),
                "path": path or None, "ram_bytes": row.get("WorkingSetSize"), "started": row.get("CreationDate"),
                "signature_status": signature, "cpu_percent": None, "network_activity": "See network_connections"}
        item.update(score_suspicious_process(item)); output.append(item)
    return {"success": True, "processes": output, "findings": [p for p in output if p["label"] != "likely safe"]}


def format_network_connection(row: dict[str, Any]) -> str:
    protocol = str(row.get("protocol", "TCP")).upper()
    local = f"{row.get('local_address', '*')}:{row.get('local_port', '*')}"
    remote = f"{row.get('remote_address', '*')}:{row.get('remote_port', '*')}"
    return f"{protocol} {local} -> {remote} [{row.get('state', 'N/A')}] PID {row.get('pid', '?')} {row.get('process_name', 'Unknown')}"


def network_connections() -> dict[str, Any]:
    tcp, tcp_error = _json_ps("@(Get-NetTCPConnection | Select LocalAddress,LocalPort,RemoteAddress,RemotePort,State,OwningProcess)")
    udp, udp_error = _json_ps("@(Get-NetUDPEndpoint | Select LocalAddress,LocalPort,OwningProcess)")
    rows = []
    for protocol, values in (("TCP", tcp), ("UDP", udp)):
        values = values if isinstance(values, list) else ([values] if values else [])
        for value in values:
            pid = value.get("OwningProcess")
            proc, _ = _json_ps(f"Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue | Select ProcessName,Path") if pid else ({}, None)
            row = {"protocol": protocol, "local_address": value.get("LocalAddress"), "local_port": value.get("LocalPort"),
                   "remote_address": value.get("RemoteAddress"), "remote_port": value.get("RemotePort"),
                   "state": value.get("State") or "Listening", "pid": pid,
                   "process_name": (proc or {}).get("ProcessName"), "executable_path": (proc or {}).get("Path")}
            row["formatted"] = format_network_connection(row); rows.append(row)
    return {"success": not (tcp_error and udp_error), "connections": rows,
            "listening_ports": [r for r in rows if str(r["state"]).lower() in {"listen", "listening"}],
            "errors": [e for e in (tcp_error, udp_error) if e],
            "note": "Read-only local connection inventory; no remote hosts were scanned or contacted."}


def suppress_duplicate_alerts(alerts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    counts: Counter[tuple[Any, ...]] = Counter()
    for alert in alerts:
        key = (alert.get("source"), alert.get("event_id"), alert.get("process"), alert.get("path"), alert.get("message"))
        counts[key] += 1; grouped.setdefault(key, dict(alert))
    for key, alert in grouped.items(): alert["occurrences"] = counts[key]
    return list(grouped.values())


def security_events(hours: int = 24, limit: int = 100) -> dict[str, Any]:
    ids = "4625,7045,4698,4719,6008"
    script = f"@(Get-WinEvent -FilterHashtable @{{LogName='System','Security'; StartTime=(Get-Date).AddHours(-{max(1,min(hours,168))})}} -ErrorAction SilentlyContinue | Where-Object {{$_.Id -in @({ids})}} | Select -First {max(1,min(limit,500))} TimeCreated,Id,ProviderName,LevelDisplayName,Message)"
    rows, error = _json_ps(script)
    if error: return {"success": False, "events": [], "error": error, "permission_limited": True}
    rows = rows if isinstance(rows, list) else ([rows] if rows else [])
    alerts = [{"timestamp": r.get("TimeCreated"), "event_id": r.get("Id"), "source": r.get("ProviderName"),
               "severity": "High" if r.get("Id") in (7045, 4719) else "Medium",
               "message": r.get("Message")} for r in rows]
    return {"success": True, "events": suppress_duplicate_alerts(alerts)}


def startup_entries() -> dict[str, Any]:
    script = "[pscustomobject]@{Run=@(Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' -ErrorAction SilentlyContinue); RunOnce=@(Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce' -ErrorAction SilentlyContinue); Tasks=@(Get-ScheduledTask -ErrorAction SilentlyContinue | Select -First 100 TaskName,TaskPath,State); Services=@(Get-CimInstance Win32_Service | Select -First 100 Name,State,StartMode,PathName)}"
    data, error = _json_ps(script)
    return {"success": error is None, "entries": data or {}, "error": error, "read_only": True}


def file_metadata(path: str) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    if not target.is_file(): return {"success": False, "error": "File does not exist."}
    stat = target.stat(); digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""): digest.update(chunk)
    sig, sig_error = _json_ps(f"Get-AuthenticodeSignature -LiteralPath '{str(target).replace("'", "''")}' | Select Status")
    return {"success": True, "path": str(target), "size": stat.st_size,
            "created": datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat(), "sha256": digest.hexdigest(),
            "extension": target.suffix.lower(), "signature_status": (sig or {}).get("Status", "Unknown"), "signature_error": sig_error}


def watched_folders() -> dict[str, Any]:
    roots = [Path.home()/"Downloads", Path.home()/"Desktop", Path(os.environ.get("TEMP", "")),
             Path(os.environ.get("APPDATA", ""))/"Microsoft/Windows/Start Menu/Programs/Startup"]
    return {"success": True, "folders": [str(p) for p in roots if str(p) and p.exists()], "monitoring": "optional_snapshot"}


def _defender(command: str, timeout: int) -> dict[str, Any]:
    ok, output = _ps(f"$ErrorActionPreference='Stop'; {command}; 'Completed'", timeout)
    return {"success": ok, "output": output, "cancelled": isinstance(output, str) and "cancel" in output.lower()}


def defender_quick_scan() -> dict[str, Any]: return _defender("Start-MpScan -ScanType QuickScan", 1800)
def defender_full_scan() -> dict[str, Any]: return _defender("Start-MpScan -ScanType FullScan", 14400)


def defender_custom_scan(path: str) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    if not target.exists(): return {"success": False, "error": "Selected file or folder does not exist."}
    escaped = str(target).replace("'", "''")
    return _defender(f"Start-MpScan -ScanType CustomScan -ScanPath '{escaped}'", 7200)


def update_defender_intelligence() -> dict[str, Any]: return _defender("Update-MpSignature", 1800)


def terminate_suspicious_process(pid: int) -> dict[str, Any]:
    if int(pid) in {0, 4, os.getpid()}: return {"success": False, "error": "Protected/system process cannot be terminated."}
    try:
        subprocess.run(["taskkill.exe", "/PID", str(int(pid))], check=True, capture_output=True, text=True, timeout=30,
                       **hidden_process_kwargs())
        return {"success": True, "pid": int(pid)}
    except (OSError, subprocess.SubprocessError) as exc: return {"success": False, "error": str(exc)}


def reject_unsafe_security_action(action: str) -> dict[str, Any]:
    if action not in _SAFE_ACTIONS:
        return {"success": False, "error": "Destructive or protection-weakening security action is forbidden."}
    return {"success": True, "allowed_only_after_approval": True}


def create_incident_report(snapshot: dict[str, Any], actions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone()
    document = {"timestamp": timestamp.isoformat(), "security_status": snapshot.get("status", "Unknown"),
                "defender_and_firewall": {"defender": snapshot.get("defender"), "firewall": snapshot.get("firewall")},
                "findings": snapshot.get("findings", []), "processes": snapshot.get("processes", []),
                "network_connections": snapshot.get("network_connections", []), "files": snapshot.get("files", []),
                "event_log_evidence": snapshot.get("events", []), "actions_approved_or_denied": actions or [],
                "scan_results": snapshot.get("scan_results", []), "final_assessment": snapshot.get("assessment", "No conclusion of malware has been made."),
                "confidence_and_uncertainty": snapshot.get("confidence", "Limited to locally accessible Windows evidence.")}
    path = REPORT_DIR / f"security-report-{timestamp:%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(document, indent=2, default=str), encoding="utf-8")
    return {"success": True, "path": str(path), "report": document}


def security_health_report() -> dict[str, Any]:
    status = windows_security_status(); processes = suspicious_processes(); network = network_connections(); startup = startup_entries(); events = security_events()
    status.update({"findings": processes.get("findings", []), "processes": processes.get("processes", []),
                   "network_connections": network.get("connections", []), "startup": startup.get("entries", {}),
                   "events": events.get("events", []), "watched_folders": watched_folders().get("folders", [])})
    return status
