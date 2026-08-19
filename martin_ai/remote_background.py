from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil

from .config import APP_DIR, load_settings
from .phone_remote import PhoneRemoteService, RemoteCallbacks
from .wake_on_lan import inspect_wake_capability
from .windows_process import hidden_process_kwargs

STATUS_FILE = APP_DIR / "remote_agent_status.json"
LOG_FILE = APP_DIR / "remote_agent.log"
STOP_FILE = APP_DIR / "remote_agent.stop"
PAIR_REQUEST_FILE = APP_DIR / "remote_pair_request"
PAIR_RESULT_FILE = APP_DIR / "remote_pair_result.json"
RUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "MartinRemoteAgent"
_WAKE_CACHE: dict[str, Any] = {"checked": 0.0, "value": {"ready": False, "adapters": []}}


def _startup_file() -> Path:
    roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Martin Remote Agent.cmd"


def _write_status(**values: Any) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps({"updated": time.time(), **values}, indent=2), encoding="utf-8")


def read_status() -> dict[str, Any]:
    try:
        row = json.loads(STATUS_FILE.read_text(encoding="utf-8")); pid = int(row.get("pid", 0))
        row["running"] = bool(pid and psutil.pid_exists(pid) and time.time() - float(row.get("updated", 0)) < 20)
        return row
    except (OSError, ValueError, json.JSONDecodeError): return {"running": False, "state": "Stopped"}


def _dashboard(_device: dict[str, Any]) -> dict[str, Any]:
    memory = psutil.virtual_memory(); disk = psutil.disk_usage(str(Path.home().anchor))
    brain_running = False
    for process in psutil.process_iter(["cmdline"]):
        try:
            if "martin_ai.gui_app" in " ".join(process.info.get("cmdline") or []): brain_running = True; break
        except (psutil.AccessDenied, psutil.NoSuchProcess): continue
    if time.time() - float(_WAKE_CACHE["checked"]) > 300:
        _WAKE_CACHE.update(checked=time.time(), value=inspect_wake_capability())
    return {"success": True, "online": True, "remote_service": "Running", "session_agent": "Running",
            "martin_ai": "Running" if brain_running else "Stopped",
            "cpu": round(psutil.cpu_percent(interval=None), 1), "ram": round(memory.percent, 1),
            "ram_total_gb": round(memory.total / 2**30, 1), "ram_available_gb": round(memory.available / 2**30, 1),
            "storage_free_gb": round(disk.free / 2**30, 1), "wake": _WAKE_CACHE["value"]}


def _chat(_text: str, _device: dict[str, Any]) -> dict[str, Any]:
    return {"success": False, "error": "Martin AI Brain is not running. Start it from the Services page."}


def _action(action: str, _confirmed: bool, _device: dict[str, Any]) -> dict[str, Any]:
    if action != "start_martin_ai": return {"success": False, "error": "This action requires the Martin AI Brain."}
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen([str(pythonw if pythonw.exists() else sys.executable), "-m", "martin_ai.gui_app"],
                     cwd=str(Path(__file__).resolve().parent.parent), **hidden_process_kwargs())
    return {"success": True, "message": "Martin AI is starting."}


def run_agent() -> int:
    mutex = None
    if os.name == "nt":
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\MartinAIRemoteAgent")
        if not mutex or ctypes.windll.kernel32.GetLastError() == 183:
            return 3
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings(); remote_settings = settings.get("phone_remote", {})
    service = PhoneRemoteService(RemoteCallbacks(_dashboard, _chat, _action), settings={**remote_settings, "enabled": True})
    result = service.start()
    if not result.get("success"):
        _write_status(pid=os.getpid(), state="Error", error=result.get("error")); return 2
    stopping = False
    def stop(*_args: Any) -> None:
        nonlocal stopping; stopping = True
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    logging.info("remote_agent_started address=%s", service.address)
    try:
        while not stopping:
            if PAIR_REQUEST_FILE.exists():
                try:
                    PAIR_REQUEST_FILE.unlink(); pairing = service.begin_pairing()
                    PAIR_RESULT_FILE.write_text(json.dumps({"code": pairing["code"], "address": pairing["address"],
                        "expires": time.time() + 300}), encoding="utf-8")
                    logging.info("pairing_window_opened")
                except OSError as exc: logging.error("pairing_window_failed error=%s", exc)
            _write_status(pid=os.getpid(), state="Running", address=service.address, session_agent="Running", service_kind="User session agent")
            time.sleep(5)
    finally:
        service.stop(); _write_status(pid=os.getpid(), state="Stopped", session_agent="Stopped"); logging.info("remote_agent_stopped")
        if mutex: ctypes.windll.kernel32.CloseHandle(mutex)
    return 0


def install_startup() -> dict[str, Any]:
    target = _startup_file(); target.parent.mkdir(parents=True, exist_ok=True)
    project = Path(__file__).resolve().parent.parent
    target.write_text(f'@echo off\r\ncd /d "{project}"\r\nstart "Martin Remote" /min "{sys.executable}" -m martin_ai.remote_background supervise\r\n', encoding="utf-8")
    return {"success": True, "message": "Martin Remote will start after user login.", "startup": str(target)}


def uninstall_startup() -> dict[str, Any]:
    try: _startup_file().unlink()
    except FileNotFoundError: pass
    return {"success": True, "message": "Startup registration removed."}


def start_detached() -> dict[str, Any]:
    current = read_status()
    if current.get("running"): return {"success": True, "message": "Martin Remote is already running.", **current}
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    try: STOP_FILE.unlink()
    except FileNotFoundError: pass
    process = subprocess.Popen([str(pythonw if pythonw.exists() else sys.executable), "-m", "martin_ai.remote_background", "supervise"],
                               cwd=str(Path(__file__).resolve().parent.parent), **hidden_process_kwargs())
    return {"success": True, "message": "Martin Remote is starting.", "pid": process.pid}


def stop_agent() -> dict[str, Any]:
    status = read_status(); pid = int(status.get("pid", 0))
    if not status.get("running"): return {"success": True, "message": "Martin Remote is already stopped."}
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True); STOP_FILE.write_text("stop", encoding="utf-8")
        psutil.Process(pid).terminate(); return {"success": True, "message": "Martin Remote stop requested."}
    except (psutil.Error, OSError) as exc: return {"success": False, "error": str(exc)}


def request_pairing() -> dict[str, Any]:
    if not read_status().get("running"): return {"success": False, "error": "Martin Remote is not running."}
    try: PAIR_RESULT_FILE.unlink()
    except FileNotFoundError: pass
    PAIR_REQUEST_FILE.write_text("pair", encoding="utf-8")
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            result = json.loads(PAIR_RESULT_FILE.read_text(encoding="utf-8"))
            if time.time() < float(result.get("expires", 0)): return {"success": True, **result}
        except (OSError, ValueError, json.JSONDecodeError): pass
        time.sleep(.25)
    return {"success": False, "error": "The background agent did not open pairing in time."}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("command", choices=("run", "supervise", "start", "stop", "restart", "status", "pair", "install", "uninstall")); args = parser.parse_args()
    if args.command == "run": return run_agent()
    if args.command == "supervise":
        try: STOP_FILE.unlink()
        except FileNotFoundError: pass
        delays = (2, 5, 15)
        for delay in delays:
            child = subprocess.Popen([sys.executable, "-m", "martin_ai.remote_background", "run"], cwd=str(Path(__file__).resolve().parent.parent), **hidden_process_kwargs())
            code = child.wait()
            if STOP_FILE.exists() or code == 0: return 0
            logging.warning("remote_agent_restart exit_code=%s delay=%s", code, delay); time.sleep(delay)
        _write_status(pid=0, state="Error", error="Recovery retry limit reached"); return 2
    if args.command == "start": result = start_detached()
    elif args.command == "stop": result = stop_agent()
    elif args.command == "restart": stop_agent(); time.sleep(1); result = start_detached()
    elif args.command == "pair": result = request_pairing()
    elif args.command == "install": result = install_startup()
    elif args.command == "uninstall": result = uninstall_startup()
    else: result = read_status()
    print(json.dumps(result, indent=2)); return 0 if result.get("success", True) else 1


if __name__ == "__main__": raise SystemExit(main())
