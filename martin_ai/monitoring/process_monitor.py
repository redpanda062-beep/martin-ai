from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any

import psutil


def foreground_application() -> dict[str, Any] | None:
    if not hasattr(ctypes, "windll"):
        return None
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        process = psutil.Process(pid.value)
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
        return {"pid": pid.value, "name": process.name(), "title": buffer.value[:300]}
    except (psutil.Error, OSError):
        return None


def running_processes(limit: int = 100) -> list[dict[str, Any]]:
    rows = []
    for process in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            rows.append({"pid": process.pid, "name": process.info["name"],
                         "memory_mb": round(process.info["memory_info"].rss / 2**20, 1)})
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return sorted(rows, key=lambda row: row["memory_mb"], reverse=True)[:max(1, min(limit, 300))]
