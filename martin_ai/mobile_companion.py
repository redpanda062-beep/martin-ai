from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .tools.desktop import read_clipboard, write_clipboard
from .windows_process import hidden_process_kwargs

MAX_TRANSFER_BYTES = 25 * 1024 * 1024
PROTOCOL_VERSION = 2
MOBILE_VERSION = "2.1.0"
SKILL_API_VERSION = 1
SENSITIVE_CLIPBOARD = re.compile(r"(?:password|passwd|authorization|bearer\s+[a-z0-9._-]+|private key|session[_ -]?cookie|api[_ -]?key)", re.I)
SAFE_TERMINAL = {
    "ipconfig": ["ipconfig.exe"], "systeminfo": ["systeminfo.exe"], "tasklist": ["tasklist.exe"],
    "get-process": ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Get-Process | Select-Object -First 100 Name,Id,CPU"],
    "get-service": ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Get-Service | Select-Object -First 100 Name,Status"],
    "get-computerinfo": ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Get-ComputerInfo | Select-Object WindowsProductName,WindowsVersion,CsName,CsTotalPhysicalMemory"],
}


def _now() -> str: return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MobileCompanionService:
    def __init__(self, *, root: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self.root = root or Path.home() / "Documents" / "MartinAI"
        self.shared = self.root / "Martin Shared"; self.state_file = self.root / "mobile_companion.json"
        self.clock = clock; self.shared.mkdir(parents=True, exist_ok=True)
        self.state = self._load(); self.state.setdefault("transfers", []); self.state.setdefault("commands", [])
        self.state.setdefault("favorites", []); self.state.setdefault("queue", [])
        self.settings = {"clipboard_enabled": False, "clipboard_phone_to_pc": False, "clipboard_pc_to_phone": False,
            "clipboard_auto": False, "file_transfer_enabled": False, "terminal_enabled": False, "camera_enabled": False,
            "audio_enabled": False, "low_bandwidth": False, "session_timeout_minutes": 15, "require_unlock_launch": False,
            "require_unlock_remote": False, "require_unlock_terminal": True, "require_unlock_power": True,
            **self.state.get("settings", {})}

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_file.read_text(encoding="utf-8")); return value if isinstance(value, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError): return {}

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True); self.state["settings"] = self.settings
        for key, limit in (("transfers", 200), ("commands", 200), ("queue", 50)):
            self.state[key] = self.state.get(key, [])[-limit:]
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2), encoding="utf-8"); temporary.replace(self.state_file)

    @staticmethod
    def _permission(device: dict[str, Any], name: str) -> bool: return bool(device.get("permissions", {}).get(name, False))
    @staticmethod
    def _safe_name(name: str) -> str:
        clean = re.sub(r"[^a-zA-Z0-9._ -]", "_", Path(name).name).strip(" .")[:150]
        if not clean or clean in {".", ".."}: raise ValueError("Invalid filename")
        return clean

    def clipboard(self, operation: str, device: dict[str, Any], text: str = "") -> dict[str, Any]:
        if not self.settings["clipboard_enabled"] or not self._permission(device, "clipboard_sync"):
            return {"success": False, "error": "Clipboard Sync is disabled or not permitted."}
        if operation == "get":
            if not self.settings["clipboard_pc_to_phone"]: return {"success": False, "error": "PC to phone clipboard is disabled."}
            result = read_clipboard(); value = str(result.get("text", ""))
            if SENSITIVE_CLIPBOARD.search(value): return {"success": False, "error": "Clipboard looks sensitive and was not sent."}
            return {"success": True, "text": value[:20000], "truncated": len(value) > 20000}
        if operation == "set":
            if not self.settings["clipboard_phone_to_pc"]: return {"success": False, "error": "Phone to PC clipboard is disabled."}
            value = str(text)[:20000]
            if SENSITIVE_CLIPBOARD.search(value): return {"success": False, "error": "Sensitive-looking clipboard text was blocked."}
            return write_clipboard(value)
        return {"success": False, "error": "Unsupported clipboard operation"}

    def safe_roots(self) -> dict[str, Path]:
        home = Path.home()
        return {"Martin Shared": self.shared, "Desktop": home / "Desktop", "Downloads": home / "Downloads",
                "Documents": home / "Documents", "Pictures": home / "Pictures", "Videos": home / "Videos"}

    def _safe_path(self, location: str, relative: str = "") -> Path:
        root = self.safe_roots().get(location)
        if root is None: raise ValueError("Location is not approved")
        candidate = (root / relative).resolve(); resolved_root = root.resolve()
        if candidate != resolved_root and resolved_root not in candidate.parents: raise ValueError("Path traversal blocked")
        return candidate

    def list_files(self, device: dict[str, Any], location: str = "Martin Shared") -> dict[str, Any]:
        if not self.settings["file_transfer_enabled"] or not self._permission(device, "file_transfer"):
            return {"success": False, "error": "File transfer is disabled or not permitted."}
        try: root = self._safe_path(location)
        except ValueError as exc: return {"success": False, "error": str(exc)}
        rows = []
        try:
            for path in sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))[:200]:
                rows.append({"name": path.name, "directory": path.is_dir(), "size": path.stat().st_size if path.is_file() else None})
        except OSError as exc: return {"success": False, "error": str(exc)}
        return {"success": True, "location": location, "files": rows}

    def upload(self, device: dict[str, Any], filename: str, encoded: str) -> dict[str, Any]:
        if not self.settings["file_transfer_enabled"] or not self._permission(device, "file_transfer"):
            return {"success": False, "error": "File transfer is disabled or not permitted."}
        transfer = {"id": secrets.token_hex(8), "filename": "", "direction": "Phone to PC", "size": 0, "timestamp": _now(), "status": "Failed"}
        try:
            name = self._safe_name(filename); payload = base64.b64decode(encoded, validate=True)
            if len(payload) > MAX_TRANSFER_BYTES: raise ValueError("File exceeds the 25 MB transfer limit")
            target = self._safe_path("Martin Shared", name)
            if target.suffix.lower() in {".exe", ".msi", ".bat", ".cmd", ".ps1", ".scr"}: transfer["note"] = "Executable content was stored only and will not be run."
            target.write_bytes(payload); transfer.update({"filename": name, "size": len(payload), "status": "Completed"})
            result = {"success": True, "transfer": transfer}
        except (ValueError, OSError) as exc: transfer["filename"] = Path(filename).name[:150]; result = {"success": False, "error": str(exc), "transfer": transfer}
        self.state["transfers"].append(transfer); self._save(); return result

    def download(self, device: dict[str, Any], location: str, filename: str) -> dict[str, Any]:
        if not self.settings["file_transfer_enabled"] or not self._permission(device, "file_transfer"):
            return {"success": False, "error": "File transfer is disabled or not permitted."}
        try:
            name = self._safe_name(filename); path = self._safe_path(location, name)
            if not path.is_file(): raise ValueError("File not found")
            size = path.stat().st_size
            if size > MAX_TRANSFER_BYTES: raise ValueError("File exceeds the 25 MB transfer limit")
            transfer = {"id": secrets.token_hex(8), "filename": name, "direction": "PC to Phone", "size": size, "timestamp": _now(), "status": "Completed"}
            self.state["transfers"].append(transfer); self._save()
            return {"success": True, "filename": name, "size": size, "content": base64.b64encode(path.read_bytes()).decode(), "transfer": transfer}
        except (ValueError, OSError) as exc: return {"success": False, "error": str(exc)}

    def cancel_transfer(self, transfer_id: str) -> dict[str, Any]:
        row = next((item for item in self.state["transfers"] if item.get("id") == transfer_id), None)
        if not row: return {"success": False, "error": "Transfer not found"}
        if row.get("status") != "Completed": row["status"] = "Cancelled"
        self._save(); return {"success": True, "transfer": row}

    def terminal(self, device: dict[str, Any], command: str) -> dict[str, Any]:
        if not self.settings["terminal_enabled"]: return {"success": False, "error": "Remote terminal is disabled on Martin PC."}
        if not self._permission(device, "terminal"): return {"success": False, "error": "Terminal permission is denied for this device."}
        normalized = re.sub(r"\s+", " ", command.strip().lower())
        if normalized.startswith("ping ") and re.fullmatch(r"ping [a-z0-9.-]{1,253}", normalized): args = ["ping.exe", "-n", "4", normalized[5:]]
        else: args = SAFE_TERMINAL.get(normalized)
        if not args: return {"success": False, "error": "Command is not available in Terminal Safe Mode."}
        started = time.perf_counter()
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=30, errors="replace", **hidden_process_kwargs())
            return {"success": result.returncode == 0, "output": (result.stdout or result.stderr)[:20000], "exit_code": result.returncode,
                    "duration_ms": round((time.perf_counter()-started)*1000)}
        except (OSError, subprocess.TimeoutExpired) as exc: return {"success": False, "error": str(exc)}

    def queue(self, device: dict[str, Any], command: str, *, ttl_seconds: int = 600) -> dict[str, Any]:
        match = re.fullmatch(r"(?:open|launch|start) ([a-z0-9 ._-]{1,80})", command.strip(), re.I)
        if not match: return {"success": False, "error": "Only safe approved app launches can be queued offline."}
        row = {"id": secrets.token_hex(8), "command": command.strip(), "app_name": match.group(1).strip(), "created": self.clock(),
               "expires": self.clock() + max(60, min(ttl_seconds, 3600)), "device_id": device.get("id"), "status": "Pending"}
        self.state["queue"].append(row); self._save(); return {"success": True, "queued": row}

    def pending_queue(self) -> list[dict[str, Any]]:
        now = self.clock()
        for row in self.state["queue"]:
            if row.get("status") == "Pending" and now >= float(row.get("expires", 0)): row["status"] = "Expired"
        self._save(); return list(reversed(self.state["queue"][-50:]))

    def record_command(self, command: str, result: dict[str, Any], *, skill: str = "", capability: str = "",
                       duration_ms: int = 0, permission_level: int = 1) -> None:
        self.state["commands"].append({"id": secrets.token_hex(8), "command": command[:500], "timestamp": _now(),
            "success": bool(result.get("success")), "result": str(result.get("message") or result.get("error") or "Completed")[:1000],
            "skill": skill, "capability": capability, "duration_ms": duration_ms, "permission_level": permission_level})
        self._save()

    def set_pin(self, pin: str) -> dict[str, Any]:
        if not re.fullmatch(r"\d{4,8}", pin): return {"success": False, "error": "PIN must be 4 to 8 digits."}
        salt = secrets.token_bytes(16); digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200_000)
        self.state["pin"] = {"salt": salt.hex(), "digest": digest.hex()}; self._save(); return {"success": True}

    def verify_pin(self, pin: str) -> bool:
        row = self.state.get("pin") or {}
        try: actual = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(row["salt"]), 200_000).hex()
        except (KeyError, ValueError): return False
        return secrets.compare_digest(actual, row["digest"])

    def versions(self, desktop_version: str = "1.0.0") -> dict[str, Any]:
        return {"success": True, "mobile_version": MOBILE_VERSION, "desktop_version": desktop_version,
                "remote_protocol": PROTOCOL_VERSION, "skill_api": SKILL_API_VERSION, "compatibility": "Compatible"}
