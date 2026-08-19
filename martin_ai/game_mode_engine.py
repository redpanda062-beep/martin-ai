from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .game_monitor import REPORT_DIR, game_error_events
from .memory import MemoryManager


DEFAULT_GAME_MODE_SETTINGS = {
    "automatic_detection": True,
    "auto_enable": True,
    "performance_monitoring": True,
    "crash_detection": True,
    "session_reports": True,
    "performance_notifications": True,
    "optimization_level": "Balanced",
    "ai_resource_mode": True,
    "allow_idle_model_unload": False,
}


def ollama_keep_alive_for_game(game_active: bool) -> str:
    """Keep chat available while releasing model resources sooner during play."""
    return "30s" if game_active else "5m"


class IntelligentGameMode:
    """Stable session state, bounded telemetry, baselines, alerts, and crash snapshots."""
    def __init__(self, memory: MemoryManager, settings: dict[str, Any] | None = None, *,
                 clock: Callable[[], float] = time.time,
                 error_provider: Callable[[str, datetime], list[dict[str, Any]]] = game_error_events) -> None:
        self.memory = memory; self.settings = {**DEFAULT_GAME_MODE_SETTINGS, **(settings or {})}
        self.clock, self.error_provider = clock, error_provider
        self.active: dict[str, Any] | None = None; self.started_at = 0.0; self.misses = 0
        self.samples: deque[dict[str, Any]] = deque(maxlen=60); self.alert_times: dict[str, float] = {}
        self.last_event = "No performance problems detected"; self.status = "Inactive"

    def configure(self, settings: dict[str, Any]) -> None:
        self.settings = {**DEFAULT_GAME_MODE_SETTINGS, **settings}

    def update(self, game: dict[str, Any] | None, sample: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if not self.settings["automatic_detection"]: game = None
        if game and self.settings["auto_enable"]:
            self.misses = 0
            if not self.active or self.active.get("executable") != game.get("executable"):
                previous = self.active.get("game") if self.active else None
                if self.active: self._finish_session(unexpected=False)
                self.active = dict(game); self.started_at = self.clock(); self.samples.clear(); self.status = "Healthy"
                if previous:
                    self.last_event = f"Game switched: {previous} -> {game.get('game')}"
                    events.append({"type": "game_switched", "game": game.get("game"), "previous_game": previous, "message": self.last_event})
                else:
                    self.last_event = f"Game detected: {game.get('game')}. Game Mode activated."
                    events.append({"type": "game_started", "game": game.get("game"), "message": self.last_event})
            if self.settings["performance_monitoring"]:
                compact = self._compact_sample(game, sample); self.samples.append(compact)
                events.extend(self._performance_events(compact))
            return events
        if self.active:
            self.misses += 1
            if self.misses >= 2: events.extend(self._finish_session(unexpected=True))
        return events

    def _compact_sample(self, game: dict[str, Any], sample: dict[str, Any]) -> dict[str, Any]:
        gpu = sample.get("gpu", {})
        return {"time": datetime.fromtimestamp(self.clock()).isoformat(timespec="seconds"),
                "cpu_percent": sample.get("cpu_percent"), "game_cpu_percent": game.get("game_process_cpu_percent"),
                "ram_percent": sample.get("ram_percent"), "game_ram_mb": game.get("game_process_ram_mb"),
                "gpu_percent": gpu.get("utilization_percent") if gpu.get("available") else None,
                "gpu_temperature_c": gpu.get("temperature_c") if gpu.get("available") else None,
                "vram_used_mb": gpu.get("memory_used_mb") if gpu.get("available") else None,
                "vram_total_mb": gpu.get("memory_total_mb") if gpu.get("available") else None,
                "network": game.get("network") or {"available": False}}

    def _performance_events(self, sample: dict[str, Any]) -> list[dict[str, Any]]:
        findings = []
        gpu, cpu, ram, temp = sample.get("gpu_percent"), sample.get("cpu_percent"), sample.get("ram_percent"), sample.get("gpu_temperature_c")
        if isinstance(temp, (int, float)) and temp >= 85: findings.append(("gpu_temperature", "GPU temperature is unusually high."))
        if isinstance(ram, (int, float)) and ram >= 93: findings.append(("memory_pressure", "Memory pressure is very high and may cause stuttering."))
        if isinstance(cpu, (int, float)) and isinstance(gpu, (int, float)) and cpu >= 95 and gpu < 70:
            findings.append(("cpu_bottleneck", "CPU use is saturated while GPU use is moderate; a CPU bottleneck is possible."))
        network = sample.get("network", {}); loss = network.get("packet_loss_percent")
        if isinstance(loss, (int, float)) and loss >= 10: findings.append(("packet_loss", "Severe packet loss may affect online play."))
        if self.active:
            profile = self.memory.get_game_profile(str(self.active.get("executable") or "").lower()) or {}
            ram_baseline = (profile.get("baseline") or {}).get("peak_ram_percent", {})
            normal_max = ram_baseline.get("maximum")
            if (isinstance(ram, (int, float)) and isinstance(normal_max, (int, float))
                    and ram >= 80 and ram > normal_max + 15):
                findings.append(("ram_anomaly", f"{self.active.get('game')} is using significantly more RAM than its recent baseline."))
        events = []
        for key, message in findings:
            now = self.clock()
            if now - self.alert_times.get(key, 0) < 300: continue
            self.alert_times[key] = now; self.status = "Warning"; self.last_event = message
            events.append({"type": "performance_warning", "key": key, "message": message})
        if not findings and self.status != "Warning": self.status = "Healthy"
        return events if self.settings["performance_notifications"] else []

    def _finish_session(self, *, unexpected: bool) -> list[dict[str, Any]]:
        active = self.active or {}; ended = self.clock(); duration = max(0.0, ended - self.started_at)
        started_dt = datetime.fromtimestamp(self.started_at); errors = []
        if unexpected and self.settings["crash_detection"]:
            errors = self.error_provider(str(active.get("executable") or ""), started_dt)
        crashed = bool(errors); summary = self._summary(active, duration, crashed)
        self._update_profile(active, summary)
        report_path = None
        if crashed:
            snapshot = {**summary, "type": "crash_snapshot", "recent_samples": list(self.samples),
                        "relevant_errors": errors[:20]}
            report_path = self._save(snapshot, "crash")
        elif self.settings["session_reports"] and duration >= 120:
            report_path = self._save(summary, "session")
        event_type = "game_crashed" if crashed else "game_ended"
        message = (f"{active.get('game')} appears to have crashed. I captured the recent system state."
                   if crashed else f"{active.get('game')} closed. Game Mode returned to normal.")
        self.active = None; self.samples.clear(); self.misses = 0; self.status = "Inactive"; self.last_event = message
        return [{"type": event_type, "game": active.get("game"), "executable": active.get("executable"),
                 "message": message, "summary": summary, "relevant_errors": errors[:20],
                 "report_path": str(report_path) if report_path else None}]

    def _summary(self, game: dict[str, Any], duration: float, crashed: bool) -> dict[str, Any]:
        rows = list(self.samples); values = lambda key: [row[key] for row in rows if isinstance(row.get(key), (int, float))]
        peak = lambda key: max(values(key), default=None)
        average = lambda key: round(sum(values(key)) / len(values(key)), 1) if values(key) else None
        return {"game": game.get("game"), "executable": game.get("executable"),
                "duration_minutes": round(duration / 60, 1), "performance": self.status,
                "peak_gpu_temperature_c": peak("gpu_temperature_c"), "peak_ram_percent": peak("ram_percent"),
                "average_gpu_percent": average("gpu_percent"), "average_cpu_percent": average("cpu_percent"),
                "crashes": int(crashed), "warnings": sorted(self.alert_times), "sample_count": len(rows)}

    def _update_profile(self, game: dict[str, Any], summary: dict[str, Any]) -> None:
        key = str(game.get("executable") or "").lower()
        if not key: return
        profile = self.memory.get_game_profile(key) or {}; sessions = list(profile.get("recent_session_summaries", []))[-9:]
        sessions.append({key: summary.get(key) for key in ("duration_minutes", "peak_gpu_temperature_c", "peak_ram_percent", "average_gpu_percent", "average_cpu_percent", "crashes")})
        baseline: dict[str, Any] = {}
        for metric in ("peak_gpu_temperature_c", "peak_ram_percent", "average_gpu_percent", "average_cpu_percent"):
            values = [row[metric] for row in sessions if isinstance(row.get(metric), (int, float))]
            if values: baseline[metric] = {"minimum": min(values), "maximum": max(values), "average": round(sum(values) / len(values), 1), "sessions": len(values)}
        profile.update({"executables": sorted(set(profile.get("executables", []) + [key])),
                        "launcher": game.get("source"), "confidence": game.get("confidence"),
                        "recent_session_summaries": sessions, "baseline": baseline,
                        "baseline_note": "Advisory; game settings and updates can change behavior."})
        self.memory.update_game_profile(key, str(game.get("game") or key), profile)

    @staticmethod
    def _save(payload: dict[str, Any], kind: str) -> Path:
        safe = re_sub(str(payload.get("game") or "game")); path = REPORT_DIR / f"{safe}-{kind}-{datetime.now():%Y%m%d-%H%M%S}.json"
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8"); return path


def re_sub(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value)
