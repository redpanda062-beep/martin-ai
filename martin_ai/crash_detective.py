from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .memory import MemoryManager
from .windows_process import hidden_process_kwargs

LOGGER = logging.getLogger(__name__)
CORRELATION_SECONDS = 90
CRASH_EVENT_IDS = {1000, 1001, 1002, 4101}


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value: return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError: return None


def sanitize(value: Any) -> str:
    text = str(value or "")
    home = str(Path.home())
    return re.sub(re.escape(home), "%USERPROFILE%", text, flags=re.I)[:4000]


@dataclass
class CrashIncident:
    application: str
    executable: str
    timestamp: str
    cause: str = "Unknown"
    confidence: str = "Low"
    score: int = 0
    explanation: str = "The application crashed, but I couldn't find enough evidence to identify the cause."
    faulting_module: str | None = None
    exception_code: str | None = None
    exit_code: str | None = None
    event_ids: list[int] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    relevant_logs: list[str] = field(default_factory=list)
    repeated_count: int = 1
    suggestions: list[str] = field(default_factory=list)


def correlate_events(events: list[dict[str, Any]], crash_time: datetime, executable: str = "") -> list[dict[str, Any]]:
    result = []
    exe = executable.lower().removesuffix(".exe")
    for raw in events:
        if not isinstance(raw, dict): continue
        stamp = _utc(raw.get("timestamp") or raw.get("TimeCreated") or raw.get("time"))
        if stamp is None or abs((stamp - crash_time).total_seconds()) > CORRELATION_SECONDS: continue
        message = sanitize(raw.get("message") or raw.get("Message"))
        event_id = raw.get("event_id", raw.get("Id"))
        try: event_id = int(event_id)
        except (TypeError, ValueError): event_id = 0
        provider = sanitize(raw.get("provider") or raw.get("ProviderName"))
        relevance = 1
        if exe and exe in message.lower(): relevance += 4
        if event_id in CRASH_EVENT_IDS: relevance += 3
        if any(term in (provider + " " + message).lower() for term in ("nvlddmkm", "display", "windows error reporting")): relevance += 2
        result.append({"timestamp": stamp.isoformat(), "event_id": event_id, "provider": provider,
                       "message": message, "relevance": relevance})
    return sorted(result, key=lambda row: (-row["relevance"], row["timestamp"]))[:20]


def classify_crash(events: list[dict[str, Any]], telemetry: dict[str, Any] | None = None) -> tuple[str, int, list[str]]:
    text = " ".join(str(row.get("provider", "")) + " " + str(row.get("message", "")) for row in events).lower()
    telemetry = telemetry or {}; scores: dict[str, int] = {}
    def add(category: str, points: int) -> None: scores[category] = scores.get(category, 0) + points
    if any(x in text for x in ("nvlddmkm", "display driver", "device removed", "device hung", "dxgi_error", "tdr", "4101")): add("Graphics Driver", 9)
    if any(x in text for x in ("outofmemory", "out of memory", "0xc0000017", "not enough memory", "allocation failed")): add("Out of Memory", 10)
    if any(x in text for x in ("easyanticheat", "battleye", "anti-cheat", "anticheat")): add("Anti-Cheat", 8)
    if any(x in text for x in ("vcruntime", "msvcp", "d3dcompiler", "dll not found", "missing dependency")): add("Missing Dependency", 8)
    if any(x in text for x in ("access denied", "0xc0000022", "permission")): add("Permission Problem", 8)
    if any(x in text for x in ("mod", "plugin", "bepinex", "fabric", "forge")): add("Mod/Plugin Related", 6)
    if any(x in text for x in ("disk", "ntfs", "bad block", "storage")): add("Storage Problem", 6)
    if any(x in text for x in ("network", "connection", "timeout")): add("Network Related", 4)
    if any(x in text for x in ("kernelbase", "ntdll", "windows component")): add("Windows Component", 4)
    if any(int(row.get("event_id", 0)) in (1000, 1001) for row in events): add("Game/Application Bug", 3)
    peak_ram = telemetry.get("peak_ram_percent")
    if isinstance(peak_ram, (int, float)) and peak_ram >= 95: add("Out of Memory", 6)
    used, total = telemetry.get("peak_vram_used_mb"), telemetry.get("vram_total_mb")
    if isinstance(used, (int, float)) and isinstance(total, (int, float)) and total and used / total >= .95: add("VRAM Pressure", 7)
    if not scores: return "Unknown", 0, []
    category, score = max(scores.items(), key=lambda item: item[1])
    suggestions = {
        "Graphics Driver": ["Restart the PC", "Check for a stable graphics-driver update", "Verify game files"],
        "Out of Memory": ["Close memory-heavy applications", "Lower memory-heavy game settings"],
        "VRAM Pressure": ["Lower texture quality", "Close GPU-heavy applications"],
        "Anti-Cheat": ["Verify game files", "Repair the game's anti-cheat component"],
        "Missing Dependency": ["Repair the named runtime or dependency", "Verify application files"],
        "Mod/Plugin Related": ["Test once with recently added mods disabled"],
    }.get(category, ["Restart the application", "Verify application files"])
    return category, min(score, 15), suggestions


class CrashDetective:
    def __init__(self, memory: MemoryManager, *, event_provider: Callable[..., list[dict[str, Any]]] | None = None,
                 clock: Callable[[], float] = time.time, enabled: bool = True) -> None:
        self.memory, self.event_provider, self.clock, self.enabled = memory, event_provider or self.windows_events, clock, enabled
        self.last_poll = 0.0; self.last_seen_event = ""; self.latest: CrashIncident | None = None

    @staticmethod
    def windows_events(*, since: datetime | None = None, executable: str = "") -> list[dict[str, Any]]:
        since = since or datetime.now(timezone.utc) - timedelta(minutes=3)
        stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        script = (f"Get-WinEvent -FilterHashtable @{{LogName=@('Application','System');StartTime=[datetime]'{stamp}'}} -ErrorAction SilentlyContinue | "
                  "Where-Object {$_.Id -in 1000,1001,1002,4101 -or $_.ProviderName -match 'Display|nvlddmkm|Windows Error Reporting|Application Error'} | "
                  "Select-Object -First 80 Id,ProviderName,TimeCreated,Message | ConvertTo-Json -Depth 3 -Compress")
        try:
            done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                                  text=True, timeout=25, **hidden_process_kwargs())
            if done.returncode != 0 or not done.stdout.strip(): return []
            data = json.loads(done.stdout); return data if isinstance(data, list) else [data]
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            LOGGER.exception("crash_event_collection_failed"); return []

    def investigate(self, application: str, executable: str, *, timestamp: datetime | None = None,
                    events: list[dict[str, Any]] | None = None, telemetry: dict[str, Any] | None = None,
                    exit_code: str | None = None, logs: list[str] | None = None) -> dict[str, Any]:
        if not self.enabled: return {"success": False, "disabled": True, "error": "Crash Detective is disabled."}
        crash_time = timestamp or datetime.now(timezone.utc)
        correlated = correlate_events(events if events is not None else self.event_provider(since=crash_time-timedelta(minutes=2), executable=executable), crash_time, executable)
        cause, score, suggestions = classify_crash(correlated, telemetry)
        confidence = "High" if score >= 9 else ("Medium" if score >= 5 else "Low")
        explanation = (f"{application} crashed because the evidence points to {cause.lower()}." if cause != "Unknown" else
                       f"{application} crashed, but I couldn't find enough evidence to identify the cause.")
        combined = " ".join(row["message"] for row in correlated)
        module = next((match.group(1) for match in re.finditer(r"faulting module(?: name)?:?\s*([^,\s]+)", combined, re.I)), None)
        exception = next((match.group(1) for match in re.finditer(r"exception code:?\s*(0x[0-9a-f]+)", combined, re.I)), None)
        incident = CrashIncident(application, executable, crash_time.astimezone(timezone.utc).isoformat(), cause, confidence, score,
                                 explanation, module, exception, exit_code,
                                 sorted({row["event_id"] for row in correlated if row["event_id"]}), correlated,
                                 [sanitize(path) for path in (logs or [])[:10]], 1, suggestions)
        similar = self.memory.find_similar_incidents("crash", cause, application=application, limit=20)
        incident.repeated_count = len(similar) + 1
        self.memory.save_incident("crash", cause, application=application,
                                  severity="high" if confidence == "High" else "medium", details=asdict(incident))
        self.latest = incident
        return {"success": True, "incident": asdict(incident), "message": self.explain(incident)}

    def explain(self, incident: CrashIncident, *, technical: bool = False) -> str:
        repeat = f"\n{incident.application} has crashed {incident.repeated_count} times with this likely cause." if incident.repeated_count > 1 else ""
        simple = f"{incident.explanation}\n\nConfidence: {incident.confidence}{repeat}"
        if not technical: return simple
        details = [f"Faulting application: {incident.application}", f"Executable: {incident.executable}",
                   f"Timestamp: {incident.timestamp}", f"Faulting module: {incident.faulting_module or 'Unavailable'}",
                   f"Exception code: {incident.exception_code or 'Unavailable'}", f"Exit code: {incident.exit_code or 'Unavailable'}",
                   "Event IDs: " + (", ".join(map(str, incident.event_ids)) or "None")]
        return simple + "\n\nTechnical Details\n" + "\n".join(details)

    def history(self, limit: int = 50, application: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT id,created_at,application,problem,severity,details_json FROM incidents WHERE kind='crash'"
        values: list[Any] = []
        if application: query += " AND LOWER(application)=?"; values.append(application.lower())
        query += " ORDER BY id DESC LIMIT ?"; values.append(max(1, min(limit, 200)))
        rows = self.memory.db.connection().execute(query, values).fetchall(); result = []
        for row in rows:
            item = dict(row)
            try: details = json.loads(item.pop("details_json") or "{}")
            except (TypeError, json.JSONDecodeError): details = {}
            result.append({**item, **details})
        return result

    def poll(self) -> list[dict[str, Any]]:
        """Cheap throttle; Windows is queried at most once every 30 seconds."""
        now = self.clock()
        if not self.enabled or now - self.last_poll < 30: return []
        self.last_poll = now; raw = self.event_provider(since=datetime.now(timezone.utc)-timedelta(seconds=45), executable="")
        created = []
        for event in raw:
            if not isinstance(event, dict): continue
            event_id = str(event.get("Id", event.get("event_id", "")))
            stamp = str(event.get("TimeCreated", event.get("timestamp", "")))
            key = event_id + stamp + sanitize(event.get("Message", event.get("message", "")))[:80]
            if not key or key == self.last_seen_event: continue
            self.last_seen_event = key
            message = sanitize(event.get("Message", event.get("message", "")))
            match = re.search(r"faulting application name:\s*([^,]+)", message, re.I)
            try: numeric_id = int(event_id or 0)
            except (TypeError, ValueError): numeric_id = 0
            if numeric_id in (1000, 1001) and match:
                executable = match.group(1).strip(); created.append(self.investigate(executable.removesuffix(".exe"), executable,
                    timestamp=_utc(stamp) or datetime.now(timezone.utc), events=raw))
        return created


_DETECTIVE: CrashDetective | None = None


def configure_crash_detective(memory: MemoryManager, *, enabled: bool = True) -> CrashDetective:
    global _DETECTIVE
    _DETECTIVE = CrashDetective(memory, enabled=enabled)
    return _DETECTIVE


def crash_detective() -> CrashDetective:
    if _DETECTIVE is None: raise RuntimeError("Crash Detective is not initialized")
    return _DETECTIVE


def get_last_crash() -> dict[str, Any]:
    rows = crash_detective().history(1)
    return {"success": True, "incident": rows[0] if rows else None,
            "message": rows[0].get("explanation") if rows else "I haven't recorded a crash yet."}


def get_crash_history(limit: int = 20) -> dict[str, Any]:
    return {"success": True, "incidents": crash_detective().history(limit)}


def analyze_crash(application: str = "", executable: str = "") -> dict[str, Any]:
    detective = crash_detective()
    if application or executable:
        return detective.investigate(application or executable or "Application", executable or application)
    rows = detective.history(1)
    return {"success": True, "incident": rows[0] if rows else None,
            "message": rows[0].get("explanation") if rows else "I couldn't find a recorded crash to analyze."}


def explain_crash(technical: bool = False) -> dict[str, Any]:
    rows = crash_detective().history(1)
    if not rows: return {"success": True, "message": "I haven't recorded a crash yet."}
    row = rows[0]
    incident = CrashIncident(**{key: row.get(key, field.default if field.default is not None else None)
                                for key, field in CrashIncident.__dataclass_fields__.items()})
    return {"success": True, "incident": row, "message": crash_detective().explain(incident, technical=technical)}
