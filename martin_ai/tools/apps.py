from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import psutil
from ..windows_process import hidden_process_kwargs

APP_COMMANDS: dict[str, list[str]] = {
    "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe"],
    "edge": ["msedge.exe"],
    "discord": [
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Discord" / "Update.exe"),
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
    return {"success": True, "apps": sorted(APP_COMMANDS)}


def open_app(app_name: str) -> dict[str, Any]:
    normalized = app_name.strip().lower()
    command = APP_COMMANDS.get(normalized)

    if command is None:
        return {
            "success": False,
            "error": f"'{app_name}' is not in the approved app list.",
            "available_apps": sorted(APP_COMMANDS),
        }

    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **hidden_process_kwargs())
        return {"success": True, "message": f"Opening {normalized}."}
    except FileNotFoundError:
        return {"success": False, "error": f"{normalized} was not found."}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def app_status(app_name: str) -> dict[str, Any]:
    normalized = app_name.strip().lower()
    expected_names = APP_PROCESSES.get(normalized)

    if expected_names is None:
        return {"success": False, "error": f"'{app_name}' is not approved."}

    matches = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info["name"] or "").lower()
            if name in expected_names:
                matches.append({"pid": proc.info["pid"], "name": proc.info["name"]})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return {"success": True, "app": normalized, "running": bool(matches), "processes": matches}


def close_app(app_name: str) -> dict[str, Any]:
    normalized = app_name.strip().lower()
    expected_names = APP_PROCESSES.get(normalized)

    if expected_names is None:
        return {"success": False, "error": f"'{app_name}' is not approved."}

    if normalized == "file explorer":
        return {"success": False, "error": "Closing Windows Explorer is disabled for safety."}

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

            closed.append({"pid": proc.pid, "name": proc.info["name"]})
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except Exception as exc:
            failed.append({"pid": proc.pid, "error": str(exc)})

    if not closed and not failed:
        return {"success": True, "message": f"{normalized} was not running.", "closed": []}

    return {
        "success": not failed,
        "message": f"Closed {len(closed)} {normalized} process(es).",
        "closed": closed,
        "failed": failed,
    }


def list_running_apps(limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    names: dict[str, int] = {}

    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info["name"] or "Unknown"
            names[name] = names.get(name, 0) + 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    rows = [
        {"name": name, "instances": count}
        for name, count in sorted(names.items(), key=lambda item: item[0].lower())
    ]
    return {"success": True, "apps": rows[:limit], "total_unique": len(rows)}
