from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import psutil

from .windows_process import hidden_process_kwargs


OPTIMIZATION_LEVELS = ("Conservative", "Balanced", "Maximum Safe")
SESSION_FILE = Path.home() / "Documents" / "MartinAI" / "active_game_optimization.json"
REPORT_DIR = Path.home() / "Documents" / "MartinAI Game Reports"
HIGH_PERFORMANCE_GUID = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
POWER_NAMES = {HIGH_PERFORMANCE_GUID: "High performance",
               "381b4222-f694-41f0-9685-ff5bb260df2e": "Balanced",
               "a1841308-3541-4fab-bc81-f71556f20b4a": "Power saver"}
OPTIONAL_BACKGROUND = {"adobearm.exe", "adobeupdateservice.exe", "ccxprocess.exe", "creative cloud.exe",
                       "onedrive.exe", "dropbox.exe", "googledrivesync.exe", "update.exe"}
PROTECTED_NAMES = {"system", "registry", "explorer.exe", "dwm.exe", "svchost.exe", "lsass.exe", "services.exe",
                   "winlogon.exe", "csrss.exe", "audiodg.exe", "discord.exe", "obs.exe", "obs64.exe",
                   "martin_ai.exe", "python.exe", "pythonw.exe", "easyanticheat.exe", "easyanticheat_eos.exe",
                   "beservice.exe", "vgc.exe", "vgtray.exe", "nvcontainer.exe", "gamingservices.exe"}
LAUNCHER_DEPENDENCIES = {
    "steam": {"steam.exe", "steamservice.exe", "steamwebhelper.exe", "gameoverlayui.exe"},
    "epic": {"epicgameslauncher.exe", "epicwebhelper.exe", "eosoverlayrenderer.exe"},
    "xbox": {"gamingservices.exe", "xboxpcapp.exe"}, "ea": {"eadesktop.exe", "eabackgroundservice.exe"},
    "ubisoft": {"ubisoftconnect.exe", "upc.exe"}, "battle.net": {"battle.net.exe", "agent.exe"},
    "gog": {"galaxyclient.exe", "goggalaxy.exe"},
}


def _priority_name(value: Any) -> str:
    pairs = ((getattr(psutil, "IDLE_PRIORITY_CLASS", 64), "Idle"),
             (getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", 16384), "Below Normal"),
             (getattr(psutil, "NORMAL_PRIORITY_CLASS", 32), "Normal"),
             (getattr(psutil, "ABOVE_NORMAL_PRIORITY_CLASS", 32768), "Above Normal"),
             (getattr(psutil, "HIGH_PRIORITY_CLASS", 128), "High"),
             (getattr(psutil, "REALTIME_PRIORITY_CLASS", 256), "Realtime"))
    return next((name for candidate, name in pairs if value == candidate), str(value))


class GameOptimizer:
    """Verified, reversible optimizer driven by the existing Game Mode polling loop."""

    def __init__(self, *, runtime: Any = None, memory: Any = None, settings: dict[str, Any] | None = None,
                 state_file: Path = SESSION_FILE, process_factory: Callable[[int], Any] = psutil.Process,
                 process_iter: Callable[..., Any] = psutil.process_iter, clock: Callable[[], float] = time.time) -> None:
        self.runtime, self.memory = runtime, memory
        self.settings = {"optimization_level": "Balanced", "ai_resource_mode": True,
                         "allow_idle_model_unload": False, **(settings or {})}
        self.state_file, self.process_factory, self.process_iter, self.clock = state_file, process_factory, process_iter, clock
        self.session: dict[str, Any] | None = None
        self.ai_resource_mode_active = False
        self.last_status = {"active": False, "status": "Inactive", "mode": self.level, "bottleneck": "None detected",
                            "ledger": [], "telemetry": {}, "verified_changes": [], "failed_changes": []}
        self._last_adaptive_check = 0.0; self._notifications: list[dict[str, str]] = []
        self.recover_incomplete_session()

    @property
    def level(self) -> str:
        value = str(self.settings.get("optimization_level", "Balanced"))
        return value if value in OPTIMIZATION_LEVELS else "Balanced"

    def configure(self, settings: dict[str, Any]) -> None:
        self.settings.update(settings); self.last_status["mode"] = self.level

    def _can_change(self) -> bool:
        return bool(self.runtime and self.runtime.autonomy_enabled and not self.runtime.simulation_mode
                    and not self.runtime.emergency_stopped)

    @staticmethod
    def _power_scheme() -> dict[str, str] | None:
        if os.name != "nt": return None
        try:
            result = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True, text=True, timeout=4,
                                    **hidden_process_kwargs())
            match = re.search(r"([0-9a-fA-F-]{36}).*?\(([^)]+)\)", result.stdout)
            return {"guid": match.group(1).lower(), "name": match.group(2).strip()} if match else None
        except (OSError, subprocess.SubprocessError): return None

    @staticmethod
    def _set_power_scheme(guid: str) -> tuple[bool, str]:
        if os.name != "nt" or not re.fullmatch(r"[0-9a-fA-F-]{36}", guid): return False, "unsupported or invalid power scheme"
        try:
            result = subprocess.run(["powercfg", "/setactive", guid], capture_output=True, text=True, timeout=4,
                                    **hidden_process_kwargs())
            return result.returncode == 0, (result.stderr or result.stdout).strip()
        except (OSError, subprocess.SubprocessError) as exc: return False, str(exc)

    def _background_snapshot(self) -> list[dict[str, Any]]:
        rows = []
        for proc in self.process_iter(["pid", "name", "memory_info"]):
            try:
                info = proc.info; memory = info.get("memory_info")
                if memory and memory.rss >= 150 * 2**20:
                    rows.append({"pid": info["pid"], "name": info.get("name"), "ram_mb": round(memory.rss / 2**20, 1)})
            except (psutil.Error, AttributeError, TypeError): continue
        return sorted(rows, key=lambda row: row["ram_mb"], reverse=True)[:15]

    def analyze_system(self, game: dict[str, Any], sample: dict[str, Any]) -> dict[str, Any]:
        pid = int(game.get("pid") or 0); priority = None
        try: priority = self.process_factory(pid).nice() if pid else None
        except (psutil.Error, OSError): pass
        vm = psutil.virtual_memory(); gpu = sample.get("gpu", {})
        return {"recorded_at": self.clock(), "game": game.get("game"), "executable": game.get("executable"), "pid": pid,
                "cpu_percent": sample.get("cpu_percent"), "game_cpu_percent": game.get("game_process_cpu_percent"),
                "gpu_percent": gpu.get("utilization_percent"), "gpu_temperature_c": gpu.get("temperature_c"),
                "ram_percent": sample.get("ram_percent", vm.percent), "available_ram_mb": round(vm.available / 2**20),
                "vram_used_mb": gpu.get("memory_used_mb"), "vram_total_mb": gpu.get("memory_total_mb"),
                "power_scheme": self._power_scheme(), "game_priority": priority, "launcher": game.get("launcher"),
                "background_applications": self._background_snapshot(), "windows_game_mode": "Not changed"}

    def _persist(self) -> None:
        if not self.session: return
        self.state_file.parent.mkdir(parents=True, exist_ok=True); temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.session, indent=2, default=str), encoding="utf-8"); temporary.replace(self.state_file)

    def _record(self, action: str, target: str, before: Any, requested: Any, after: Any, success: bool,
                *, restoration_required: bool = False, error: str = "") -> dict[str, Any]:
        row = {"action": action, "target": target, "previous_value": before, "requested_value": requested,
               "actual_value": after, "success": bool(success), "timestamp": datetime.fromtimestamp(self.clock()).isoformat(timespec="seconds"),
               "restoration_required": bool(restoration_required and success), "restoration_result": "Not required" if not restoration_required else "Pending",
               "error": error}
        self.session["ledger"].append(row); self._persist(); return row

    def _change_priority(self, pid: int, target: int, target_name: str, process_name: str | None = None) -> bool:
        action, target_label = "Process Priority", f"{process_name or self.session.get('executable')} (PID {pid})"
        try:
            proc = self.process_factory(pid); before = proc.nice()
            if before == target:
                self._record(action, target_label, _priority_name(before), target_name, _priority_name(before), False,
                             error="Already at requested value; no change was needed")
                return False
            proc.nice(target); after = proc.nice(); success = after == target
            self._record(action, target_label, _priority_name(before), target_name, _priority_name(after), success,
                         restoration_required=success, error="" if success else "Windows did not retain the requested priority")
            return success
        except (psutil.Error, OSError, ValueError) as exc:
            self._record(action, target_label, "Unavailable", target_name, "Unavailable", False, error=str(exc)); return False

    def _change_power(self) -> bool:
        before = self._power_scheme(); before_name = before["name"] if before else "Unavailable"
        if not before:
            self._record("Power Profile", "Windows active power scheme", before_name, "High performance", "Unavailable", False,
                         error="Active power scheme could not be read"); return False
        if before["guid"] == HIGH_PERFORMANCE_GUID:
            self._record("Power Profile", "Windows active power scheme", before_name, "High performance", before_name, False,
                         error="Already at requested value; no change was needed"); return False
        executed, error = self._set_power_scheme(HIGH_PERFORMANCE_GUID); after = self._power_scheme()
        success = bool(executed and after and after["guid"] == HIGH_PERFORMANCE_GUID)
        self._record("Power Profile", "Windows active power scheme", before_name, "High performance",
                     after["name"] if after else "Unavailable", success, restoration_required=success,
                     error="" if success else (error or "Power scheme verification failed"))
        return success

    def _set_ai_resource_mode(self, enabled: bool) -> bool:
        before = self.ai_resource_mode_active; self.ai_resource_mode_active = bool(enabled)
        success = self.ai_resource_mode_active == bool(enabled) and before != self.ai_resource_mode_active
        self._record("AI Gaming Resource Mode", "Martin AI background analysis", "Reduced" if before else "Normal",
                     "Reduced" if enabled else "Normal", "Reduced" if self.ai_resource_mode_active else "Normal", success,
                     restoration_required=success and enabled, error="" if success else "Already at requested value; no change was needed")
        return success

    def apply_safe_optimizations(self, game: dict[str, Any], sample: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.session: return []
        if not self._can_change():
            self._record("Optimization Permission", "Martin AI autonomy", "Blocked", "Allow reversible changes", "Blocked", False,
                         error="Autonomy, simulation mode, or emergency stop prevents system changes")
            return self.session["ledger"]
        if self.level != "Conservative" and self.session.get("pid"):
            self._change_priority(int(self.session["pid"]), getattr(psutil, "ABOVE_NORMAL_PRIORITY_CLASS", -7), "Above Normal")
            self._change_power()
        if self.settings.get("ai_resource_mode", True): self._set_ai_resource_mode(True)
        self.session["latest_telemetry"] = self.analyze_system(game, sample)
        self._persist()
        return list(self.session["ledger"])

    def start_game_session(self, game: dict[str, Any], sample: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        identity = (game.get("executable"), game.get("pid"))
        if self.session and not force and identity == (self.session.get("executable"), self.session.get("pid")):
            return self.get_optimization_status()
        if self.session: self.restore_session_changes("game switched")
        baseline = self.analyze_system(game, sample)
        self.session = {"version": 2, "active": True, "started_at": self.clock(), "game": game.get("game"),
                        "executable": game.get("executable"), "pid": game.get("pid"), "mode": self.level,
                        "baseline": baseline, "latest_telemetry": baseline, "ledger": [],
                        "bottleneck": self._bottleneck(game, sample), "restoration": []}
        self._persist(); self.apply_safe_optimizations(game, sample)
        verified = len(self._successful_actions())
        self._notify("optimization_started", (f"Applied and verified {verified} optimization(s) for {game.get('game')}."
                     if verified else f"Monitoring {game.get('game')}; no system changes were verified."))
        return self.get_optimization_status()

    @staticmethod
    def _bottleneck(game: dict[str, Any], sample: dict[str, Any]) -> str:
        gpu = sample.get("gpu", {}); gpu_use = float(gpu.get("utilization_percent") or 0)
        cpu = float(sample.get("cpu_percent") or 0); ram = float(sample.get("ram_percent") or 0); temp = float(gpu.get("temperature_c") or 0)
        if temp >= 85: return "High temperature"
        if ram >= 92: return "RAM"
        if cpu >= 92 and gpu_use < 80: return "CPU"
        if gpu_use >= 95 and cpu < 85: return "GPU"
        return "None detected"

    def _protected_for_game(self, name: str) -> bool:
        lowered = name.lower()
        if lowered in PROTECTED_NAMES or any(token in lowered for token in ("anticheat", "battleye", "vanguard")): return True
        launcher = str((self.session or {}).get("baseline", {}).get("launcher") or "").lower()
        return any(key in launcher and lowered in values for key, values in LAUNCHER_DEPENDENCIES.items())

    def _reduce_optional_background(self) -> None:
        if not self.session or not self._can_change(): return
        for proc in self.process_iter(["pid", "name"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if name in OPTIONAL_BACKGROUND and not self._protected_for_game(name):
                    self._change_priority(int(proc.info["pid"]), getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", 10), "Below Normal", name)
            except (psutil.Error, OSError, TypeError): continue

    def monitor_game_performance(self, game: dict[str, Any], sample: dict[str, Any]) -> dict[str, Any]:
        if not self.session: return self.get_optimization_status()
        self.session["bottleneck"] = self._bottleneck(game, sample); self.session["latest_telemetry"] = self.analyze_system(game, sample)
        now = time.monotonic(); temp = float(sample.get("gpu", {}).get("temperature_c") or 0)
        if temp >= 85: self._notify("high_temperature", "GPU temperature is unusually high; no additional optimization was attempted.")
        elif now - self._last_adaptive_check >= 30 and self.session["bottleneck"] in {"CPU", "RAM"}:
            self._last_adaptive_check = now
            if self.level == "Maximum Safe": self._reduce_optional_background()
        self._persist(); return self.get_optimization_status()

    def _successful_actions(self) -> list[dict[str, Any]]:
        return [row for row in (self.session or {}).get("ledger", []) if row.get("success")]

    def restore_session_changes(self, reason: str = "game closed") -> dict[str, Any]:
        session = self.session
        if not session: return self.get_optimization_status()
        restored = 0
        for row in reversed(session.get("ledger", [])):
            if not row.get("restoration_required") or row.get("restoration_result") != "Pending": continue
            success, detail = False, ""
            if row["action"] == "Process Priority":
                try:
                    pid_match = re.search(r"PID (\d+)", str(row.get("target", "")))
                    restore_pid = int(pid_match.group(1)) if pid_match else int(session.get("pid") or 0)
                    proc = self.process_factory(restore_pid); before = proc.nice()
                    # A terminated game needs no priority restoration.
                    original_name = row["previous_value"]
                    target = next((value for value in (getattr(psutil, "IDLE_PRIORITY_CLASS", 64), getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", 16384),
                                getattr(psutil, "NORMAL_PRIORITY_CLASS", 32), getattr(psutil, "ABOVE_NORMAL_PRIORITY_CLASS", 32768),
                                getattr(psutil, "HIGH_PRIORITY_CLASS", 128), getattr(psutil, "REALTIME_PRIORITY_CLASS", 256)) if _priority_name(value) == original_name), None)
                    if target is not None: proc.nice(target); success = proc.nice() == target
                    detail = f"{_priority_name(before)} -> {original_name}" if success else "priority verification failed"
                except (psutil.NoSuchProcess, ProcessLookupError): success, detail = True, "process exited; restoration not required"
                except (psutil.Error, OSError, ValueError) as exc: detail = str(exc)
            elif row["action"] == "Power Profile":
                baseline = session.get("baseline", {}).get("power_scheme") or {}; guid = baseline.get("guid")
                executed, detail = self._set_power_scheme(str(guid or "")); after = self._power_scheme()
                success = bool(executed and after and after.get("guid") == guid)
            elif row["action"] == "AI Gaming Resource Mode":
                self.ai_resource_mode_active = False; success = not self.ai_resource_mode_active; detail = "Reduced -> Normal"
            row["restoration_result"] = "SUCCESS" if success else f"FAILED: {detail or 'verification failed'}"
            if success: restored += 1
        summary = {"active": False, "status": "Restored", "game": session.get("game"), "executable": session.get("executable"),
                   "pid": session.get("pid"), "mode": session.get("mode"), "bottleneck": session.get("bottleneck"),
                   "baseline": session.get("baseline"), "telemetry": session.get("latest_telemetry", {}), "ledger": session.get("ledger", []),
                   "verified_changes": [row for row in session.get("ledger", []) if row.get("success")],
                   "failed_changes": [row for row in session.get("ledger", []) if not row.get("success")],
                   "restored_count": restored, "duration_seconds": max(0, int(self.clock() - session.get("started_at", self.clock()))), "reason": reason}
        self._record_history(summary); self._save_report(summary); self.session = None; self.last_status = summary
        try: self.state_file.unlink(missing_ok=True)
        except OSError: pass
        failed_restore = sum(1 for row in summary["ledger"] if str(row.get("restoration_result", "")).startswith("FAILED"))
        message = f"{summary.get('game')} closed. {restored} temporary change(s) restored successfully."
        if failed_restore: message += f" {failed_restore} restoration(s) failed verification."
        self._notify("optimization_restored", message); return summary

    def _save_report(self, summary: dict[str, Any]) -> None:
        try:
            REPORT_DIR.mkdir(parents=True, exist_ok=True); safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(summary.get("game") or "game"))
            (REPORT_DIR / f"{safe}-optimization-{datetime.now():%Y%m%d-%H%M%S}.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        except OSError: pass

    def _record_history(self, summary: dict[str, Any]) -> None:
        if not self.memory: return
        key = str(summary.get("executable") or "").lower()
        if not key: return
        try:
            profile = self.memory.get_game_profile(key) or {}; history = list(profile.get("optimization_history", []))[-9:]
            history.append({name: summary.get(name) for name in ("mode", "bottleneck", "verified_changes", "failed_changes", "duration_seconds")})
            profile["optimization_history"] = history; self.memory.update_game_profile(key, str(summary.get("game") or key), profile)
        except Exception: pass

    def recover_incomplete_session(self) -> dict[str, Any] | None:
        if not self.state_file.exists(): return None
        try:
            self.session = json.loads(self.state_file.read_text(encoding="utf-8"))
            # Upgrade the previous optimizer's restoration record before rollback.
            if "ledger" not in self.session:
                self.session["ledger"] = [{"action": "Process Priority", "target": f"PID {row.get('pid')}",
                    "previous_value": _priority_name(row.get("original")), "requested_value": "Above Normal",
                    "actual_value": "Above Normal", "success": True, "timestamp": "legacy session",
                    "restoration_required": True, "restoration_result": "Pending", "error": ""}
                    for row in self.session.get("priority_changes", [])]
                self.session.setdefault("latest_telemetry", self.session.get("baseline", {}))
                self.session.setdefault("bottleneck", "Unknown")
            return self.restore_session_changes("recovered after interrupted session")
        except (OSError, ValueError, json.JSONDecodeError):
            self.session = None
            try: self.state_file.unlink(missing_ok=True)
            except OSError: pass
            return None

    def stop(self) -> dict[str, Any]: return self.restore_session_changes("stopped by user")

    def get_optimization_status(self) -> dict[str, Any]:
        if not self.session: return dict(self.last_status)
        successful = self._successful_actions(); failed = [row for row in self.session["ledger"] if not row.get("success")]
        return {"active": True, "status": "Optimized" if successful else "Monitoring", "game": self.session.get("game"),
                "executable": self.session.get("executable"), "pid": self.session.get("pid"), "mode": self.session.get("mode"),
                "bottleneck": self.session.get("bottleneck"), "baseline": self.session.get("baseline"),
                "telemetry": self.session.get("latest_telemetry", {}), "ledger": list(self.session.get("ledger", [])),
                "verified_changes": successful, "failed_changes": failed,
                "duration_seconds": max(0, int(self.clock() - self.session.get("started_at", self.clock())))}

    def pop_notifications(self) -> list[dict[str, str]]:
        result, self._notifications = self._notifications, []; return result

    def _notify(self, key: str, message: str) -> None:
        if not any(item["key"] == key for item in self._notifications): self._notifications.append({"key": key, "message": message})


_OPTIMIZER: GameOptimizer | None = None
def configure_game_optimizer(**kwargs: Any) -> GameOptimizer:
    global _OPTIMIZER; _OPTIMIZER = GameOptimizer(**kwargs); return _OPTIMIZER
def game_optimizer() -> GameOptimizer:
    global _OPTIMIZER
    if _OPTIMIZER is None: _OPTIMIZER = GameOptimizer()
    return _OPTIMIZER
