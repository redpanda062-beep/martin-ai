from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil


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
            disks.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "filesystem": part.fstype,
                "total_gb": round(usage.total / 2**30, 1),
                "free_gb": round(usage.free / 2**30, 1),
                "percent_used": usage.percent,
            })
        except (PermissionError, OSError):
            continue

    return {
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
            rows.append({
                "pid": proc.pid,
                "name": proc.info["name"],
                "cpu_percent": proc.cpu_percent(None),
                "memory_mb": round(proc.info["memory_info"].rss / 2**20, 1),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return {
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
            / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
            Path(r"C:\Windows\System32\nvidia-smi.exe"),
        ]
        exe = next((str(p) for p in candidates if p.exists()), None)

    if not exe:
        return {"success": False, "error": "nvidia-smi was not found."}

    return _run([
        exe,
        "--query-gpu=name,driver_version,temperature.gpu,utilization.gpu,memory.total,memory.used",
        "--format=csv,noheader",
    ], timeout=20)


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


def flush_dns() -> dict[str, Any]:
    return _run(["ipconfig.exe", "/flushdns"], timeout=20)


def reset_microsoft_store_cache() -> dict[str, Any]:
    try:
        subprocess.Popen(["wsreset.exe"])
        return {"success": True, "message": "Microsoft Store reset started."}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def clear_shader_caches() -> dict[str, Any]:
    targets = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "D3DSCache",
        Path(os.environ.get("LOCALAPPDATA", "")) / "NVIDIA" / "DXCache",
        Path(os.environ.get("LOCALAPPDATA", "")) / "NVIDIA" / "GLCache",
        Path(os.environ.get("LOCALAPPDATA", "")) / "NVIDIA Corporation" / "NV_Cache",
    ]

    deleted_files = 0
    failures = []
    for target in targets:
        if not target.exists():
            continue
        for child in target.rglob("*"):
            if child.is_file():
                try:
                    child.unlink()
                    deleted_files += 1
                except Exception as exc:
                    failures.append(f"{child}: {exc}")

    return {
        "success": not failures,
        "deleted_files": deleted_files,
        "failed_count": len(failures),
        "failed_examples": failures[:10],
        "note": "Games may stutter temporarily while shaders rebuild.",
    }


def run_sfc_scan() -> dict[str, Any]:
    return _run(["sfc.exe", "/scannow"], timeout=1800)


def run_dism_restorehealth() -> dict[str, Any]:
    return _run(["dism.exe", "/Online", "/Cleanup-Image", "/RestoreHealth"], timeout=1800)

# Apps Martin AI is allowed to control.
# Add more apps here later as needed.
APP_COMMANDS: dict[str, list[str]] = {
    "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe"],
    "edge": ["msedge.exe"],
    "discord": [
        str(
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Discord"
            / "Update.exe"
        ),
        "--processStart",
        "Discord.exe",
    ],
    "steam": [r"C:\Program Files (x86)\Steam\steam.exe"],
    "spotify": ["spotify.exe"],
    "notepad": ["notepad.exe"],
    "calculator": ["calc.exe"],
    "task manager": ["taskmgr.exe"],
    "settings": ["cmd.exe", "/c", "start", "", "ms-settings:"],
    "file explorer": ["explorer.exe"],
    "xbox": ["cmd.exe", "/c", "start", "", "xbox:"],
}

# Process names used when checking or closing apps.
APP_PROCESSES: dict[str, set[str]] = {
    "chrome": {"chrome.exe"},
    "edge": {"msedge.exe"},
    "discord": {"discord.exe", "update.exe"},
    "steam": {"steam.exe", "steamwebhelper.exe"},
    "spotify": {"spotify.exe"},
    "notepad": {"notepad.exe"},
    "calculator": {"calculatorapp.exe", "calculator.exe"},
    "task manager": {"taskmgr.exe"},
    "file explorer": {"explorer.exe"},
    "xbox": {"xboxpcapp.exe", "gamingservices.exe"},
}


def available_apps() -> dict[str, Any]:
    """List the apps Martin AI is allowed to control."""
    return {
        "success": True,
        "apps": sorted(APP_COMMANDS.keys()),
    }


def open_app(app_name: str) -> dict[str, Any]:
    """Open an approved application by name."""
    normalized = app_name.strip().lower()

    command = APP_COMMANDS.get(normalized)
    if command is None:
        return {
            "success": False,
            "error": f"'{app_name}' is not in the approved app list.",
            "available_apps": sorted(APP_COMMANDS.keys()),
        }

    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {
            "success": True,
            "message": f"Opening {normalized}.",
        }
    except FileNotFoundError:
        return {
            "success": False,
            "error": (
                f"{normalized} could not be found at its configured location. "
                "Its path may need to be updated."
            ),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def app_status(app_name: str) -> dict[str, Any]:
    """Check whether an approved application is currently running."""
    normalized = app_name.strip().lower()
    expected_names = APP_PROCESSES.get(normalized)

    if expected_names is None:
        return {
            "success": False,
            "error": f"'{app_name}' is not in the approved app list.",
            "available_apps": sorted(APP_PROCESSES.keys()),
        }

    matches = []

    for proc in psutil.process_iter(["pid", "name"]):
        try:
            process_name = (proc.info["name"] or "").lower()

            if process_name in expected_names:
                matches.append({
                    "pid": proc.info["pid"],
                    "name": proc.info["name"],
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return {
        "success": True,
        "app": normalized,
        "running": bool(matches),
        "processes": matches,
    }


def close_app(app_name: str) -> dict[str, Any]:
    """
    Close an approved application.

    The caller should obtain user confirmation before invoking this tool,
    because unsaved work could be lost.
    """
    normalized = app_name.strip().lower()
    expected_names = APP_PROCESSES.get(normalized)

    if expected_names is None:
        return {
            "success": False,
            "error": f"'{app_name}' is not in the approved app list.",
            "available_apps": sorted(APP_PROCESSES.keys()),
        }

    # Prevent Martin AI from closing Windows Explorer for now.
    if normalized == "file explorer":
        return {
            "success": False,
            "error": "Closing Windows Explorer is disabled for safety.",
        }

    closed = []
    failed = []

    for proc in psutil.process_iter(["pid", "name"]):
        try:
            process_name = (proc.info["name"] or "").lower()

            if process_name not in expected_names:
                continue

            proc.terminate()

            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()

            closed.append({
                "pid": proc.pid,
                "name": proc.info["name"],
            })

        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            failed.append({
                "pid": proc.pid,
                "name": proc.info.get("name"),
                "error": "Access denied.",
            })
        except Exception as exc:
            failed.append({
                "pid": proc.pid,
                "name": proc.info.get("name"),
                "error": str(exc),
            })

    if not closed and not failed:
        return {
            "success": True,
            "message": f"{normalized} was not running.",
            "closed": [],
        }

    return {
        "success": not failed,
        "message": f"Closed {len(closed)} {normalized} process(es).",
        "closed": closed,
        "failed": failed,
    }


def open_user_folder(folder_name: str) -> dict[str, Any]:
    """Open an approved folder belonging to the current Windows user."""
    folders = {
        "desktop": Path.home() / "Desktop",
        "downloads": Path.home() / "Downloads",
        "documents": Path.home() / "Documents",
        "pictures": Path.home() / "Pictures",
        "videos": Path.home() / "Videos",
        "music": Path.home() / "Music",
        "reports": REPORT_DIR,
    }

    normalized = folder_name.strip().lower()
    folder = folders.get(normalized)

    if folder is None:
        return {
            "success": False,
            "error": f"'{folder_name}' is not an approved folder.",
            "available_folders": sorted(folders.keys()),
        }

    if not folder.exists():
        return {
            "success": False,
            "error": f"The folder does not exist: {folder}",
        }

    try:
        os.startfile(folder)
        return {
            "success": True,
            "message": f"Opening {normalized}.",
            "path": str(folder),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}
READ_ONLY_TOOLS = {
    "system_summary": system_summary,
    "top_processes": top_processes,
    "recent_windows_errors": recent_windows_errors,
    "nvidia_status": nvidia_status,
    "save_diagnostic_report": save_diagnostic_report,
    "available_apps": available_apps,
    "app_status": app_status,
    "open_app": open_app,
    "open_user_folder": open_user_folder,
}

REPAIR_TOOLS = {
    "flush_dns": flush_dns,
    "reset_microsoft_store_cache": reset_microsoft_store_cache,
    "clear_shader_caches": clear_shader_caches,
    "run_sfc_scan": run_sfc_scan,
    "run_dism_restorehealth": run_dism_restorehealth,
    "close_app": close_app,
}