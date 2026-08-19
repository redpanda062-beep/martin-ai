from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import urllib.request
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import psutil

from .core.runtime import RuntimeControl
from .memory import MemoryManager
from .ollama_manager import OllamaManager, ollama_manager
from .tools.apps import APP_COMMANDS, APP_PROCESSES, app_status, close_app, open_app
from .tools.diagnostics import _run, run_network_diagnostic, storage_status


Result = dict[str, Any]
Handler = Callable[[dict[str, Any]], Result]


@dataclass(frozen=True)
class RepairDefinition:
    repair_id: str
    name: str
    description: str
    problems: tuple[str, ...]
    diagnostic_prerequisites: tuple[str, ...]
    risk_level: int
    permission_required: bool
    repair: Handler
    verify: Handler
    timeout: int
    rollback: Handler | None = None
    platforms: tuple[str, ...] = ("Windows",)
    impact: str = ""
    rollback_description: str = "No automatic rollback is available."


class RepairRegistry:
    def __init__(self) -> None:
        self._repairs: dict[str, RepairDefinition] = {}

    def register(self, repair: RepairDefinition) -> None:
        if repair.repair_id in self._repairs:
            raise ValueError(f"Duplicate repair ID: {repair.repair_id}")
        if repair.risk_level not in range(1, 4):
            raise ValueError("Repair risk must be Level 1, 2, or 3.")
        if repair.risk_level >= 2 and not repair.permission_required:
            raise ValueError("Level 2 and Level 3 repairs always require confirmation.")
        self._repairs[repair.repair_id] = repair

    def get(self, repair_id: str) -> RepairDefinition | None:
        return self._repairs.get(repair_id)

    def catalog(self) -> list[dict[str, Any]]:
        return [{
            "repair_id": item.repair_id, "name": item.name, "description": item.description,
            "problems": list(item.problems), "diagnostic_prerequisites": list(item.diagnostic_prerequisites),
            "risk_level": item.risk_level, "permission_required": item.permission_required,
            "timeout": item.timeout, "rollback_available": item.rollback is not None,
            "platforms": list(item.platforms),
        } for item in self._repairs.values()]


@dataclass
class PendingRepair:
    repair_id: str
    problem: str
    context: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)


def diagnose_network(_: dict[str, Any] | None = None) -> Result:
    return run_network_diagnostic()


def _flush_dns(_: dict[str, Any]) -> Result:
    return _run(["ipconfig.exe", "/flushdns"], timeout=20)


def _verify_dns(_: dict[str, Any]) -> Result:
    from .tools.diagnostics import check_dns
    return check_dns()


def _renew_network(_: dict[str, Any]) -> Result:
    return _run(["ipconfig.exe", "/renew"], timeout=90)


def _reset_adapter(context: dict[str, Any]) -> Result:
    name = str(context.get("adapter", "")).strip()
    if not name or name not in psutil.net_if_stats():
        return {"success": False, "error": "The selected adapter is not present."}
    safe_name = name.replace("'", "''")
    script = (f"Disable-NetAdapter -Name '{safe_name}' -Confirm:$false -ErrorAction Stop; "
              f"Start-Sleep -Seconds 2; Enable-NetAdapter -Name '{safe_name}' -Confirm:$false -ErrorAction Stop")
    return _run(["powershell.exe", "-NoProfile", "-Command", script], timeout=60)


def _reset_network_stack(_: dict[str, Any]) -> Result:
    first = _run(["netsh.exe", "winsock", "reset"], timeout=45)
    if not first.get("success"): return first
    second = _run(["netsh.exe", "int", "ip", "reset"], timeout=60)
    second["restart_required"] = True
    return second


def _verify_network(_: dict[str, Any]) -> Result:
    result = diagnose_network()
    result["success"] = result.get("issue") == "healthy"
    return result


def _ollama_reconnect(context: dict[str, Any]) -> Result:
    manager = context.get("ollama_manager") or ollama_manager
    state = manager.reconnect()
    if state.status not in {"connected", "no_models"}:
        state = manager.restart_ollama()
    return {"success": state.status == "connected", "status": state.status,
            "model": state.selected_model, "message": state.message}


def _verify_ollama(context: dict[str, Any]) -> Result:
    manager: OllamaManager = context.get("ollama_manager") or ollama_manager
    state = manager.check(auto_start=False, force=True)
    if state.status != "connected" or not state.selected_model:
        return {"success": False, "api": False, "generation": False, "status": state.status}
    configured = manager.config.model
    if configured and configured not in state.models:
        return {"success": False, "api": True, "generation": False, "status": "model_missing",
                "model": configured}
    try:
        answer = manager.chat([{"role": "user", "content": "Reply with OK."}],
                              base_url=manager.config.base_url, model=state.selected_model, keep_alive="30s")
        return {"success": bool(answer.strip()), "api": True, "generation": bool(answer.strip())}
    except Exception as exc:
        return {"success": False, "api": True, "generation": False, "error": str(exc)}


def _restart_app(context: dict[str, Any]) -> Result:
    app = str(context.get("app_name", "")).strip().lower()
    if app not in APP_COMMANDS or app not in APP_PROCESSES:
        return {"success": False, "error": "The application is not allowlisted for restart."}
    closed = close_app(app)
    if not closed.get("success"): return closed
    opened = open_app(app)
    return {"success": bool(opened.get("success")), "closed": closed, "opened": opened}


def _verify_app(context: dict[str, Any]) -> Result:
    result = app_status(str(context.get("app_name", "")))
    result["success"] = bool(result.get("success") and result.get("running"))
    return result


def _clear_martin_temp(_: dict[str, Any]) -> Result:
    root = Path.home() / "Documents" / "MartinAI" / "temp"
    if not root.exists(): return {"success": True, "bytes_removed": 0, "files_removed": 0}
    quarantine = Path.home() / "Documents" / "MartinAI" / "rollback" / f"martin-temp-{datetime.now():%Y%m%d-%H%M%S-%f}"
    removed, count = 0, 0
    for item in root.iterdir():
        try:
            removed += item.stat().st_size if item.is_file() else sum(p.stat().st_size for p in item.rglob("*") if p.is_file())
            quarantine.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item), str(quarantine / item.name)); count += 1
        except OSError: continue
    return {"success": True, "bytes_removed": removed, "files_removed": count, "target": str(root),
            "rollback": {"quarantine_path": str(quarantine), "temp_path": str(root)}}


def _verify_martin_temp(_: dict[str, Any]) -> Result:
    root = Path.home() / "Documents" / "MartinAI" / "temp"
    remaining = sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) if root.exists() else 0
    return {"success": remaining == 0, "remaining_bytes": remaining}


def _restore_martin_temp(context: dict[str, Any]) -> Result:
    allowed = (Path.home() / "Documents" / "MartinAI" / "rollback").resolve(strict=False)
    source = Path(str(context.get("quarantine_path", ""))).resolve(strict=False)
    target = (Path.home() / "Documents" / "MartinAI" / "temp").resolve(strict=False)
    try: source.relative_to(allowed)
    except ValueError: return {"success": False, "error": "Rollback source is outside Martin AI's rollback folder."}
    if not source.is_dir(): return {"success": False, "error": "Rollback data is unavailable."}
    target.mkdir(parents=True, exist_ok=True); restored = 0
    for item in source.iterdir():
        if (target / item.name).exists():
            return {"success": False, "error": "Rollback would overwrite newer temporary data."}
        shutil.move(str(item), str(target / item.name)); restored += 1
    return {"success": True, "restored_items": restored}


def _sfc(_: dict[str, Any]) -> Result: return _run(["sfc.exe", "/scannow"], timeout=1800)
def _dism(_: dict[str, Any]) -> Result: return _run(["dism.exe", "/Online", "/Cleanup-Image", "/RestoreHealth"], timeout=1800)
def _verify_windows(_: dict[str, Any]) -> Result:
    result = _run(["dism.exe", "/Online", "/Cleanup-Image", "/CheckHealth"], timeout=120)
    result["success"] = bool(result.get("success") and "repairable" not in str(result.get("stdout", "")).lower())
    return result


def build_registry() -> RepairRegistry:
    registry = RepairRegistry()
    add = registry.register
    add(RepairDefinition("ollama.reconnect", "Reconnect or start Ollama", "Reconnects Martin AI's managed local model service.",
        ("Ollama unavailable", "API unavailable", "model unavailable"), ("Ollama API and model status",), 1, False,
        _ollama_reconnect, _verify_ollama, 120, impact="The local model service may restart."))
    add(RepairDefinition("network.flush_dns", "Flush DNS cache", "Clears only the Windows DNS resolver cache.",
        ("DNS resolution failure",), ("Adapter, IP, gateway, DNS, and internet tests",), 1, False,
        _flush_dns, _verify_dns, 20, impact="Cached hostname entries are cleared."))
    add(RepairDefinition("network.renew_ip", "Renew network configuration", "Requests a fresh DHCP lease.",
        ("Missing or invalid IP assignment",), ("Adapter and IP assignment",), 2, True,
        _renew_network, _verify_network, 90, impact="The connection may disconnect briefly."))
    add(RepairDefinition("network.restart_adapter", "Restart network adapter", "Disables and re-enables one named adapter.",
        ("Network adapter problem",), ("Adapter state and exact adapter name",), 2, True,
        _reset_adapter, _verify_network, 60, impact="The connection will disconnect briefly."))
    add(RepairDefinition("network.reset_stack", "Reset Windows network stack", "Resets Winsock and TCP/IP configuration.",
        ("Damaged network stack",), ("Adapter, gateway, DNS, and internet tests",), 2, True,
        _reset_network_stack, _verify_network, 120, impact="A restart may be required; network configuration changes.",
        rollback_description="Windows does not provide a reliable automatic rollback for this reset."))
    add(RepairDefinition("application.restart", "Restart approved application", "Closes and reopens an allowlisted application.",
        ("Frozen application", "failed application launch"), ("Process state and application allowlist",), 2, True,
        _restart_app, _verify_app, 45, impact="Unsaved work in the application may be lost."))
    add(RepairDefinition("storage.clear_martin_temp", "Clear Martin AI temporary files", "Deletes only Martin AI's disposable temp folder.",
        ("Martin AI temporary storage usage",), ("Martin AI temp folder size",), 1, False,
        _clear_martin_temp, _verify_martin_temp, 30, rollback=_restore_martin_temp,
        impact="Only disposable Martin AI temporary data is moved out of the active temp folder.",
        rollback_description="Moved temporary data can be restored from Martin AI's rollback folder."))
    add(RepairDefinition("windows.sfc", "Run System File Checker", "Checks and repairs protected Windows system files.",
        ("Damaged Windows system files",), ("Relevant Windows errors",), 2, True, _sfc, _verify_windows, 1800,
        impact="Windows system files may be repaired; this can take a long time."))
    add(RepairDefinition("windows.dism_restorehealth", "Run DISM RestoreHealth", "Repairs the Windows component store.",
        ("Windows component store damage",), ("Relevant Windows errors and DISM health",), 2, True,
        _dism, _verify_windows, 1800, impact="Windows components may change; this can take a long time."))
    return registry


class SelfRepairEngine:
    def __init__(self, registry: RepairRegistry, runtime: RuntimeControl, memory: MemoryManager,
                 *, enabled: bool = True, auto_level_1: bool = False) -> None:
        self.registry, self.runtime, self.memory = registry, runtime, memory
        self.enabled, self.auto_level_1 = bool(enabled), bool(auto_level_1)
        self._lock = threading.RLock()
        self._pending: PendingRepair | None = None

    @property
    def pending(self) -> PendingRepair | None:
        with self._lock: return self._pending

    def set_options(self, *, enabled: bool, auto_level_1: bool) -> None:
        self.enabled, self.auto_level_1 = bool(enabled), bool(auto_level_1)

    def prepare(self, repair_id: str, problem: str, context: dict[str, Any] | None = None,
                diagnostics: list[str] | None = None, *, autonomous: bool = False) -> Result:
        repair = self.registry.get(repair_id)
        if not repair: return {"success": False, "rejected": True, "message": "No approved repair exists for this problem."}
        if not self.enabled: return {"success": False, "blocked": True, "message": "Self Repair is turned off."}
        if self.runtime.emergency_stopped: return {"success": False, "blocked": True, "message": "STOP AI is active."}
        if autonomous and not self.runtime.autonomy_enabled:
            return {"success": False, "blocked": True, "message": "Autonomous repairs are turned off."}
        pending = PendingRepair(repair_id, problem, dict(context or {}), list(diagnostics or repair.diagnostic_prerequisites))
        must_ask = repair.risk_level >= 2 or repair.permission_required or (autonomous and not self.auto_level_1)
        if must_ask:
            with self._lock:
                if self._pending: return {"success": False, "pending": True, "message": "Another repair is awaiting approval."}
                self._pending = pending
            self.memory.save_repair_history(problem, pending.diagnostics, repair_id, repair.risk_level,
                                            None, None, None, None, "pending")
            return {"success": False, "pending": True, "repair_id": repair_id, "risk_level": repair.risk_level,
                    "plan": self.plan_text(pending)}
        return self._execute(pending, approved=False)

    def approve(self) -> Result:
        with self._lock: pending, self._pending = self._pending, None
        if not pending: return {"success": False, "message": "No repair is awaiting approval."}
        return self._execute(pending, approved=True)

    def deny(self) -> Result:
        with self._lock: pending, self._pending = self._pending, None
        if not pending: return {"success": False, "message": "No repair is awaiting approval."}
        repair = self.registry.get(pending.repair_id)
        self.memory.save_repair_history(pending.problem, pending.diagnostics, pending.repair_id,
                                        repair.risk_level if repair else 3, False, None, None, None, "denied")
        return {"success": True, "cancelled": True, "message": "I cancelled the repair. No change was made."}

    def cancel_pending(self) -> None:
        with self._lock: self._pending = None

    def plan_text(self, pending: PendingRepair | None = None) -> str:
        pending = pending or self.pending
        if not pending: return "No repair is awaiting approval."
        repair = self.registry.get(pending.repair_id)
        if not repair: return "The pending repair is no longer available."
        rollback = repair.rollback_description
        return (f"I found: {pending.problem}\n\nProposed repair: {repair.name}.\n"
                f"{repair.impact}\nRollback: {rollback}\n\nDo you want me to continue?")

    def _execute(self, pending: PendingRepair, *, approved: bool) -> Result:
        repair = self.registry.get(pending.repair_id)
        if not repair: return {"success": False, "rejected": True, "message": "The approved repair is unavailable."}
        if repair.risk_level >= 2 and not approved:
            return {"success": False, "blocked": True, "message": "Explicit confirmation is required."}
        token = self.runtime.action_token()
        if not self.runtime.may_start_action(token):
            return {"success": False, "blocked": True, "message": "STOP AI prevented the repair from starting."}
        if self.runtime.simulation_mode:
            result, verification, status = {"success": True, "simulated": True}, {"success": False, "simulated": True}, "simulated"
        else:
            result = repair.repair(pending.context)
            if not self.runtime.may_start_action(token):
                verification = {"success": False, "cancelled": True}; status = "cancelled"
            elif result.get("success"):
                verification = repair.verify(pending.context)
                status = "verified" if verification.get("success") else "verification_failed"
            else:
                verification = {"success": False, "not_run": True}; status = "repair_failed"
        rollback_result = None
        if status == "verification_failed" and repair.rollback and result.get("rollback"):
            rollback_context = dict(pending.context); rollback_context.update(result["rollback"])
            rollback_result = repair.rollback(rollback_context)
            if rollback_result.get("success"): status = "rolled_back"
        self.memory.save_repair_history(pending.problem, pending.diagnostics, repair.repair_id,
                                        repair.risk_level, approved, result, verification, rollback_result, status)
        if status == "verified": message = f"I completed {repair.name.lower()} and verified that the original problem is resolved."
        elif status == "verification_failed": message = "The repair completed, but the problem is still present."
        elif status == "rolled_back": message = "The repair did not solve the problem, so I restored the previous state."
        elif status == "simulated": message = f"Simulation only: I would perform {repair.name.lower()} and then verify the original problem."
        else: message = "The repair did not complete, so I could not verify a fix."
        return {"success": status == "verified", "executed": status != "simulated", "status": status,
                "repair_id": repair.repair_id, "repair_result": result, "verification": verification,
                "rollback": rollback_result, "message": message}


repair_registry = build_registry()
