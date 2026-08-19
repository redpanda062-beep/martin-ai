from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
import ctypes
from collections import deque
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any
from .game_detection import GameDetectionEngine


# GUI polling runs this module from pythonw.exe.  On Windows, console programs
# such as nvidia-smi and PowerShell otherwise create a brief visible window for
# every poll.
SUBPROCESS_CREATION_FLAGS = (
    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
)

import psutil

try:
    from .tools.repairs import clear_shader_caches
except ImportError:
    # Keep direct execution useful for developers; launchers use ``-m``.
    from tools.repairs import clear_shader_caches


BASE_DIR = Path(__file__).resolve().parent
GAMES_FILE = BASE_DIR / "games.json"
REPORT_DIR = Path.home() / "Documents" / "MartinAI Game Reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

POLL_SECONDS = 3
SAMPLE_SECONDS = 5
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
AUTO_REPAIR_STATE_FILE = BASE_DIR / ".auto_repair_state.json"
AUTO_REPAIR_COOLDOWN_HOURS = 24

# Only terminate known browser background processes when that browser has no
# visible window. Never infer that an open application is safe to close.
SAFE_BACKGROUND_APPS: dict[str, set[str]] = {
    "Google Chrome": {"chrome.exe"},
    "Microsoft Edge": {"msedge.exe"},
}
_GAME_DETECTOR: GameDetectionEngine | None = None
_NETWORK_GAME_CACHE: tuple[float, dict[str, Any]] = (0.0, {})


def game_detector() -> GameDetectionEngine:
    global _GAME_DETECTOR
    if _GAME_DETECTOR is None:
        from .memory import MemoryManager
        _GAME_DETECTOR = GameDetectionEngine(MemoryManager())
    return _GAME_DETECTOR


def configure_game_detector(memory: Any) -> GameDetectionEngine:
    global _GAME_DETECTOR
    _GAME_DETECTOR = GameDetectionEngine(memory)
    return _GAME_DETECTOR


def acquire_monitor_lock() -> int | None:
    """Prevent multiple Martin game monitors from running at once on Windows."""
    if os.name != "nt":
        return None
    handle = ctypes.windll.kernel32.CreateMutexW(
        None, False, "Local\\MartinAI.GameMonitor"
    )
    if ctypes.windll.kernel32.GetLastError() == 183:
        ctypes.windll.kernel32.CloseHandle(handle)
        return 0
    return int(handle)


def load_games() -> dict[str, str]:
    try:
        data = json.loads(GAMES_FILE.read_text(encoding="utf-8"))
        return {key.lower(): value for key, value in data.items()}
    except Exception as exc:
        print(f"[Game Mode] Could not read games.json: {exc}")
        return {}


def running_process_names() -> set[str]:
    names: set[str] = set()
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info["name"]
            if name:
                names.add(name.lower())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return names


def detect_game(games: dict[str, str]) -> tuple[str, str] | None:
    detection = game_detector().detect(force=True)
    return ((str(detection.executable), str(detection.game_name))
            if detection and detection.detected and detection.game_name else None)


def live_game_status(
    requested_exe: str | None = None,
    *, force_refresh: bool = False,
) -> dict[str, Any] | None:
    """Return a current snapshot for a running game, including unknown games."""
    detection = game_detector().detect(force=force_refresh)
    if not detection or not detection.detected or not detection.game_name:
        return None
    if requested_exe and detection.executable != requested_exe.lower(): return None
    exe_name, display_name = str(detection.executable), str(detection.game_name)
    started_times: list[float] = []
    for proc in psutil.process_iter(["name", "create_time"]):
        try:
            if (proc.info["name"] or "").lower() == exe_name:
                started_times.append(float(proc.info["create_time"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError, ValueError):
            continue

    sample = system_sample(detection.pids)
    elapsed_seconds = max(
        0,
        int(time.time() - min(started_times)),
    ) if started_times else 0
    return {
        "game": display_name,
        "executable": exe_name,
        "status": "running",
        "detection": detection.title_source or "Automatic",
        "confidence": detection.confidence,
        "confidence_label": detection.confidence_label,
        "foreground": detection.foreground,
        "pids": detection.pids,
        "pid": detection.pids[0] if detection.pids else None,
        "path": detection.path,
        "install_dir": detection.install_dir,
        "launcher": detection.launcher or detection.source,
        "source": detection.source,
        "app_id": detection.app_id,
        "signals": detection.signals,
        "window_title": detection.window_title,
        "window_class": detection.window_class,
        "elapsed_seconds": elapsed_seconds,
        **sample,
    }


def rescan_game_library() -> dict[str, Any]:
    detector = game_detector(); count = detector.refresh_library(force=True)
    detection = detector.detect(force=True)
    return {"success": True, "manifest_entries": count,
            "game": detection.as_dict() if detection else None}


def game_detection_debug() -> dict[str, Any]:
    return game_detector().debug_report()


def visible_window_pids() -> set[int]:
    """Return process IDs that own a visible, titled top-level window."""
    if os.name != "nt":
        return set()

    pids: set[int] = set()
    user32 = ctypes.windll.user32

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                pids.add(pid.value)
        return True

    user32.EnumWindows(callback, 0)
    return pids


def close_safe_background_apps() -> list[dict[str, Any]]:
    """Close allowlisted apps only when none of their processes has a window."""
    visible_pids = visible_window_pids()
    processes = list(psutil.process_iter(["pid", "name"]))
    closed: list[dict[str, Any]] = []

    for display_name, executable_names in SAFE_BACKGROUND_APPS.items():
        matches = []
        for proc in processes:
            try:
                if (proc.info["name"] or "").lower() in executable_names:
                    matches.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if not matches or any(proc.pid in visible_pids for proc in matches):
            continue

        terminated = 0
        for proc in matches:
            try:
                proc.terminate()
                terminated += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if terminated:
            closed.append({"app": display_name, "processes": terminated})
            print(
                f"[Game Mode] Closed {terminated} background "
                f"{display_name} process(es); no visible window was open."
            )

    return closed


def unload_ollama() -> None:
    payload = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "prompt": "",
            "keep_alive": 0,
            "stream": False,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10):
            pass
        print("[Game Mode] Ollama model unloaded from GPU memory.")
    except Exception:
        # Game monitoring should continue even if Ollama is closed.
        print("[Game Mode] Ollama was unavailable or already unloaded.")


def find_nvidia_smi() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found

    candidates = [
        Path(r"C:\Windows\System32\nvidia-smi.exe"),
        Path(os.environ.get("ProgramW6432", r"C:\Program Files"))
        / "NVIDIA Corporation"
        / "NVSMI"
        / "nvidia-smi.exe",
    ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return None


def gpu_status() -> dict[str, Any]:
    exe = find_nvidia_smi()
    if not exe:
        return {"available": False, "error": "nvidia-smi not found"}

    try:
        completed = subprocess.run(
            [
                exe,
                "--query-gpu=name,temperature.gpu,utilization.gpu,memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            errors="replace",
            creationflags=SUBPROCESS_CREATION_FLAGS,
        )

        if completed.returncode != 0:
            return {
                "available": False,
                "error": completed.stderr.strip() or "nvidia-smi failed",
            }

        line = completed.stdout.strip().splitlines()[0]
        parts = [part.strip() for part in line.split(",")]

        return {
            "available": True,
            "name": parts[0],
            "temperature_c": float(parts[1]),
            "utilization_percent": float(parts[2]),
            "memory_total_mb": float(parts[3]),
            "memory_used_mb": float(parts[4]),
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def system_sample(game_pids: list[int] | None = None) -> dict[str, Any]:
    memory = psutil.virtual_memory()
    gpu = gpu_status()

    disks = []
    for partition in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(partition.mountpoint)
            disks.append(
                {
                    "device": partition.device,
                    "free_gb": round(usage.free / 2**30, 1),
                    "percent_used": usage.percent,
                }
            )
        except (PermissionError, OSError):
            continue

    process_cpu = process_ram = 0.0
    for pid in game_pids or []:
        try:
            proc = psutil.Process(pid); process_cpu += proc.cpu_percent(interval=0.05)
            process_ram += proc.memory_info().rss / 2**20
        except (psutil.NoSuchProcess, psutil.AccessDenied): pass
    disk = psutil.disk_io_counters()
    return {
        "time": datetime.now().isoformat(timespec="seconds"),
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "cpu_temperature_c": cpu_temperature(),
        "ram_percent": memory.percent,
        "ram_available_gb": round(memory.available / 2**30, 2),
        "game_process_cpu_percent": round(process_cpu, 1),
        "game_process_ram_mb": round(process_ram, 1),
        "disk_busy_time_ms": getattr(disk, "busy_time", None) if disk else None,
        "gpu": gpu,
        "disks": disks,
    }


def cpu_temperature() -> float | None:
    """Return the hottest reported CPU sensor, when one is available."""
    try:
        temperatures = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        temperatures = {}

    values = [
        entry.current
        for group, entries in temperatures.items()
        if "cpu" in group.lower() or "core" in group.lower()
        for entry in entries
        if entry.current is not None
    ]
    return round(max(values), 1) if values else None


def warnings_for(sample: dict[str, Any]) -> list[str]:
    warnings: list[str] = []

    if sample["ram_percent"] >= 90:
        warnings.append("RAM usage is at or above 90%.")

    gpu = sample.get("gpu", {})
    if gpu.get("available"):
        if gpu.get("temperature_c", 0) >= 85:
            warnings.append("GPU temperature is at or above 85 C.")
        total = gpu.get("memory_total_mb", 0)
        used = gpu.get("memory_used_mb", 0)
        if total and used / total >= 0.95:
            warnings.append("GPU memory usage is at or above 95%.")

    for disk in sample.get("disks", []):
        if disk.get("free_gb", 999) < 10:
            warnings.append(
                f"{disk['device']} has less than 10 GB free."
            )

    return warnings


def game_error_events(exe_name: str, started: datetime) -> list[dict[str, Any]]:
    """Read Application errors mentioning this game's executable."""
    safe_exe = exe_name.replace("'", "''")
    safe_started = started.isoformat(timespec="seconds").replace("'", "''")
    script = (
        "$start=[datetime]::Parse('" + safe_started + "'); "
        "$events=Get-WinEvent -FilterHashtable "
        "@{LogName='Application'; Level=1,2; StartTime=$start} "
        "-ErrorAction SilentlyContinue | Where-Object { "
        "$_.Message -like '*" + safe_exe + "*' } | Select-Object -First 20 "
        "TimeCreated,Id,ProviderName,Message; "
        "$events | ConvertTo-Json -Depth 3 -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
            errors="replace",
            creationflags=SUBPROCESS_CREATION_FLAGS,
        )
        raw = result.stdout.strip()
        if result.returncode != 0 or not raw:
            return []
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else [parsed]
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []


def diagnose_session(summary: dict[str, Any]) -> dict[str, Any]:
    """Turn collected evidence into safe, actionable troubleshooting advice."""
    findings: list[str] = []
    recommendations: list[str] = []
    warnings = summary.get("warnings", [])
    events = summary.get("windows_error_events", [])

    if events:
        findings.append(
            f"Windows recorded {len(events)} error event(s) mentioning the game."
        )
        recommendations.append(
            "Check the recorded faulting module, then verify the game's files and update that component."
        )
    if summary.get("duration_minutes", 0) < 2 and events:
        findings.append("The game ended within two minutes and likely crashed during startup.")
    if any("RAM usage" in item for item in warnings):
        recommendations.append("Close memory-heavy apps or reduce the game's texture settings.")
    if any("GPU temperature" in item for item in warnings):
        recommendations.append("Check airflow and lower the GPU power or graphics load.")
    if any("GPU memory" in item for item in warnings):
        recommendations.append("Reduce texture quality, resolution, or ray-tracing settings.")
    if any("less than 10 GB" in item for item in warnings):
        recommendations.append("Free at least 10 GB on the affected drive.")
    if warnings:
        findings.extend(warnings)
    if not findings:
        findings.append("No crash event or resource threshold problem was detected.")
        recommendations.append("No corrective action is currently indicated.")

    return {
        "status": "issues_detected" if events or warnings else "healthy",
        "findings": list(dict.fromkeys(findings)),
        "recommendations": list(dict.fromkeys(recommendations)),
    }


def graphics_crash_detected(events: list[dict[str, Any]]) -> bool:
    """Require explicit graphics/API evidence before repairing shader caches."""
    signatures = (
        "nvlddmkm", "nvwgf2umx", "direct3d", "d3d11", "d3d12",
        "dxgi_error", "dxgi.dll", "unrealengine", "rendering device lost",
    )
    for event in events:
        evidence = " ".join(
            str(event.get(key, ""))
            for key in ("ProviderName", "Message")
        ).lower()
        if any(signature in evidence for signature in signatures):
            return True
    return False


def run_automatic_repairs(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Run only low-risk, evidence-triggered repairs with a cooldown."""
    events = summary.get("windows_error_events", [])
    if not graphics_crash_detected(events):
        return []

    state: dict[str, Any] = {}
    try:
        state = json.loads(AUTO_REPAIR_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass

    now = datetime.now()
    last_text = state.get("clear_shader_caches")
    if last_text:
        try:
            elapsed = now - datetime.fromisoformat(last_text)
            if elapsed.total_seconds() < AUTO_REPAIR_COOLDOWN_HOURS * 3600:
                return [{
                    "repair": "clear_shader_caches",
                    "success": True,
                    "skipped": True,
                    "reason": "24-hour safety cooldown is active.",
                }]
        except ValueError:
            pass

    result = clear_shader_caches()
    record = {
        "repair": "clear_shader_caches",
        "trigger": "A Windows crash event contained a graphics failure signature.",
        **result,
    }
    if result.get("success"):
        state["clear_shader_caches"] = now.isoformat(timespec="seconds")
        try:
            AUTO_REPAIR_STATE_FILE.write_text(
                json.dumps(state, indent=2), encoding="utf-8"
            )
        except OSError:
            record["state_warning"] = "Could not save the repair cooldown state."
    return [record]


def local_ai_diagnosis(summary: dict[str, Any]) -> str | None:
    """Ask the local model for a concise evidence-only post-game explanation."""
    compact = {key: summary.get(key) for key in (
        "game", "duration_minutes", "peak_cpu_percent", "peak_ram_percent",
        "gpu", "warnings", "windows_error_events", "automatic_diagnosis",
    )}
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "stream": False,
        "keep_alive": 0,
        "messages": [
            {"role": "system", "content": (
                "You are Martin AI. Explain this game-session diagnosis in under 180 words. "
                "Use only the supplied evidence, name likely causes as possibilities, and suggest "
                "safe next steps. Do not claim that a repair was performed."
            )},
            {"role": "user", "content": json.dumps(compact, default=str)},
        ],
        "options": {"temperature": 0.1},
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            data = json.loads(response.read().decode("utf-8"))
        return (data.get("message", {}).get("content") or "").strip() or None
    except Exception:
        return None


def summarize_session(
    display_name: str,
    exe_name: str,
    started: datetime,
    ended: datetime,
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    gpu_samples = [
        sample["gpu"]
        for sample in samples
        if sample.get("gpu", {}).get("available")
    ]

    summary: dict[str, Any] = {
        "game": display_name,
        "executable": exe_name,
        "started": started.isoformat(timespec="seconds"),
        "ended": ended.isoformat(timespec="seconds"),
        "duration_minutes": round((ended - started).total_seconds() / 60, 1),
        "sample_count": len(samples),
        "peak_cpu_percent": max(
            (sample["cpu_percent"] for sample in samples),
            default=0,
        ),
        "peak_ram_percent": max(
            (sample["ram_percent"] for sample in samples),
            default=0,
        ),
        "warnings": sorted(
            {
                warning
                for sample in samples
                for warning in warnings_for(sample)
            }
        ),
    }

    if gpu_samples:
        summary["gpu"] = {
            "name": gpu_samples[0].get("name"),
            "peak_temperature_c": max(
                sample.get("temperature_c", 0)
                for sample in gpu_samples
            ),
            "average_utilization_percent": round(
                sum(sample.get("utilization_percent", 0) for sample in gpu_samples)
                / len(gpu_samples),
                1,
            ),
            "peak_memory_used_mb": max(
                sample.get("memory_used_mb", 0)
                for sample in gpu_samples
            ),
        }

    return summary


def save_report(summary: dict[str, Any]) -> Path:
    safe_name = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in summary["game"]
    )
    path = REPORT_DIR / (
        f"{safe_name}-{datetime.now():%Y%m%d-%H%M%S}.json"
    )
    path.write_text(
        json.dumps(summary, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def monitor_session(exe_name: str, display_name: str) -> None:
    started = datetime.now()
    samples: deque[dict[str, Any]] = deque(maxlen=60)

    print()
    print("=" * 60)
    print(f"[Game Mode] Detected: {display_name} ({exe_name})")
    print("[Game Mode] Entering Game Mode.")
    print("=" * 60)

    # Keep chat available. The GUI shortens model retention and suppresses
    # nonessential AI work while a game is active instead of disabling Ollama.
    # Monitoring is read-only. Closing applications is a protected action and
    # must be requested through ActionManager for explicit approval.
    closed_background_apps: list[dict[str, Any]] = []

    next_sample = 0.0

    while True:
        running = running_process_names()
        if exe_name not in running:
            break

        now = time.monotonic()
        if now >= next_sample:
            try:
                sample = system_sample()
            except Exception as exc:
                # A transient counter or hardware-sensor failure must not end
                # monitoring while the game is still running.
                print(f"[Game Mode] Sample failed; retrying: {exc}", flush=True)
                next_sample = now + SAMPLE_SECONDS
                time.sleep(1)
                continue
            samples.append(sample)
            next_sample = now + SAMPLE_SECONDS

            gpu = sample.get("gpu", {})
            gpu_text = "GPU unavailable"
            if gpu.get("available"):
                gpu_text = (
                    f"GPU {gpu['utilization_percent']:.0f}% | "
                    f"{gpu['temperature_c']:.0f} C | "
                    f"VRAM {gpu['memory_used_mb']:.0f}/"
                    f"{gpu['memory_total_mb']:.0f} MB"
                )

            cpu_temp = sample.get("cpu_temperature_c")
            cpu_temp_text = (
                f"{cpu_temp:.0f} C" if cpu_temp is not None else "N/A"
            )
            print(
                f"[{datetime.now():%H:%M:%S}] "
                f"{display_name}: RUNNING | "
                f"CPU {sample['cpu_percent']:.0f}% ({cpu_temp_text}) | "
                f"RAM {sample['ram_percent']:.0f}% | "
                f"{gpu_text}"
            , flush=True)

            for warning in warnings_for(sample):
                print(f"[WARNING] {warning}")

        time.sleep(1)

    ended = datetime.now()
    summary = summarize_session(
        display_name,
        exe_name,
        started,
        ended,
        list(samples),
    )
    summary["closed_background_apps"] = closed_background_apps
    summary["windows_error_events"] = game_error_events(exe_name, started)
    summary["automatic_diagnosis"] = diagnose_session(summary)
    # This process has no approval UI, so it must not execute protected repairs.
    summary["automatic_repairs"] = []
    summary["martin_ai_diagnosis"] = None
    # The GUI's IntelligentGameMode owns optional reports, profiles, crash
    # snapshots, and notifications so user settings are honored in one place.
    report_path = None

    print()
    print("=" * 60)
    print(f"[Game Mode] {display_name} closed.")
    print(
        f"[Game Mode] Session duration: "
        f"{summary['duration_minutes']} minutes."
    )
    if report_path: print(f"[Game Mode] Report saved to: {report_path}")
    print(
        f"[Game Mode] Troubleshooting status: "
        f"{summary['automatic_diagnosis']['status']}"
    )
    for finding in summary["automatic_diagnosis"]["findings"]:
        print(f"[Diagnosis] {finding}")
    for repair in summary["automatic_repairs"]:
        state = "skipped" if repair.get("skipped") else (
            "completed" if repair.get("success") else "failed"
        )
        print(f"[Auto Repair] {repair['repair']}: {state}")
    print("=" * 60)
    print()


def main() -> None:
    monitor_lock = acquire_monitor_lock()
    if monitor_lock == 0:
        print("[Game Mode] A monitor is already running.")
        return

    games = load_games()

    print("Martin AI Game Mode - Stage 1")
    print(f"Watching {len(games)} configured game executable(s).")
    print("Press Ctrl+C to stop the monitor.")
    print()

    try:
        while True:
            detected = detect_game(games)

            if detected:
                exe_name, display_name = detected
                try:
                    monitor_session(exe_name, display_name)
                except Exception as exc:
                    print(
                        f"[Game Mode] Session monitor error: {exc}. Retrying.",
                        flush=True,
                    )
                    time.sleep(POLL_SECONDS)
            else:
                time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print()
        print("[Game Mode] Monitor stopped.")


if __name__ == "__main__":
    main()
