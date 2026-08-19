from __future__ import annotations

import ctypes
import os
import subprocess
from typing import Any

from .windows_process import hidden_process_kwargs


MEDIA_KEYS = {"volume_up": 0xAF, "volume_down": 0xAE, "volume_mute": 0xAD,
              "media_play_pause": 0xB3, "media_next": 0xB0, "media_previous": 0xB1}
REMOTE_ACTIONS = frozenset({"lock", "sleep", "restart", "shutdown", "stop_ai", *MEDIA_KEYS})
POWER_ACTIONS = frozenset({"sleep", "restart", "shutdown"})


def execute_remote_control(action: str) -> dict[str, Any]:
    """Execute one fixed, prevalidated Windows action. No user command strings are accepted."""
    if os.name != "nt": return {"success": False, "error": "Windows controls are unavailable on this platform"}
    try:
        if action in MEDIA_KEYS:
            vk = MEDIA_KEYS[action]; ctypes.windll.user32.keybd_event(vk, 0, 0, 0); ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
            return {"success": True, "message": action.replace("_", " ").title()}
        if action == "lock": return {"success": bool(ctypes.windll.user32.LockWorkStation()), "message": "PC locked"}
        if action == "sleep":
            result = subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], capture_output=True, timeout=8, **hidden_process_kwargs())
        elif action == "restart": result = subprocess.run(["shutdown.exe", "/r", "/t", "0"], capture_output=True, timeout=8, **hidden_process_kwargs())
        elif action == "shutdown": result = subprocess.run(["shutdown.exe", "/s", "/t", "0"], capture_output=True, timeout=8, **hidden_process_kwargs())
        else: return {"success": False, "error": "Remote action is not allowlisted"}
        return {"success": result.returncode == 0, "error": result.stderr.decode(errors="replace").strip() if result.returncode else ""}
    except (OSError, subprocess.SubprocessError) as exc: return {"success": False, "error": str(exc)}
