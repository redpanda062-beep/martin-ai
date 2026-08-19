from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .actions import ActionManager
from .game_monitor import game_detection_debug, game_detector, live_game_status, load_games, rescan_game_library
from .tools import READ_ONLY_TOOLS
from .response_formatter import format_response
from .intent import Intent, detect_intent
from .memory import MemoryManager
from .memory_commands import handle_memory_command, is_memory_command
from .self_repair import SelfRepairEngine, diagnose_network
from .ollama_manager import OllamaManager, OllamaState, ollama_manager
from .game_optimizer import game_optimizer
from .phone_remote import phone_remote_service
from .bootstrap import skill_manager


ALIASES = {
    "notebook": "notepad",
    "note book": "notepad",
    "note pad": "notepad",
    "notes": "notepad",
    "ms edge": "edge",
    "microsoft edge": "edge",
    "explorer": "file explorer",
    "file manager": "file explorer",
    "steam client": "steam",
    "taskmanager": "task manager",
    "task mgr": "task manager",
}


FOLDERS = {
    "desktop",
    "downloads",
    "documents",
    "pictures",
    "videos",
    "music",
    "reports",
}


@dataclass
class RouteResult:
    handled: bool
    message: str = ""
    panel_title: str = "Martin AI"
    panel_style: str = "green"
    raw_result: Any = None
    intent: str = Intent.UNKNOWN.value


def detect_ollama_action(text: str) -> str | None:
    """Return a trusted Ollama operation only for imperative/status-like requests."""
    command = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    target = r"(?:ollama|(?:the )?(?:ai|model) (?:backend|service)|(?:the )?ai model|model)"
    patterns = (
        ("restart", rf"^(?:please )?restart {target}$"),
        ("reconnect", rf"^(?:please )?reconnect(?: to)? {target}$"),
        ("start", rf"^(?:please )?start {target}$"),
        ("stop", rf"^(?:please )?stop {target}$"),
        ("status", rf"^(?:(?:please )?(?:check|test) {target}|{target} status|is {target} (?:running|working|online|connected))$"),
        ("repair", rf"^(?:please )?(?:fix|repair) {target}$"),
    )
    return next((action for action, pattern in patterns if re.fullmatch(pattern, command)), None)


def execute_ollama_action(action: str, manager: OllamaManager = ollama_manager) -> tuple[OllamaState, str]:
    if action == "restart":
        state = manager.restart_ollama()
    elif action == "reconnect":
        state = manager.reconnect()
    elif action == "start":
        state = manager.start_ollama()
    elif action == "stop":
        state = manager.stop_ollama()
    elif action == "status":
        state = manager.get_ollama_status()
    elif action == "repair":
        state = manager.reconnect()
        if state.status not in {"connected", "no_models"}:
            state = manager.restart_ollama()
    else:
        raise ValueError("Unsupported Ollama action")

    configured = manager.config.model
    configured_found = bool(configured and configured in state.models)
    if state.status == "connected" and configured and not configured_found:
        message = f"Ollama is running, but {configured} is not currently available."
    elif state.status == "connected":
        verbs = {"restart": "restarted and reconnected", "reconnect": "reconnected",
                 "start": "started and connected", "repair": "recovered"}
        message = (f"Ollama {verbs[action]} successfully." if action in verbs
                   else f"Ollama is connected{f' and {state.selected_model} is available' if state.selected_model else ''}.")
    elif action == "stop" and state.status == "offline":
        message = "Ollama stopped successfully."
    else:
        message = state.message
    return state, message


def _normalize_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value.strip().lower()).strip(" .!?;,:")
    return ALIASES.get(value, value)


def _format_result(result: dict[str, Any]) -> str:
    return format_response(result)


def _game_aliases() -> dict[str, str]:
    """Map natural game names and executable stems to games.json entries."""
    aliases: dict[str, str] = {}
    for exe_name, display_name in load_games().items():
        values = {
            exe_name,
            exe_name.removesuffix(".exe"),
            display_name.lower(),
            re.sub(r"[^a-z0-9]+", " ", display_name.lower()).strip(),
        }
        for value in values:
            if value:
                aliases[value] = exe_name
    return aliases


def _referenced_game(command: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9.]+", " ", command.lower()).strip()
    padded = f" {normalized} "
    for alias, exe_name in sorted(
        _game_aliases().items(), key=lambda item: len(item[0]), reverse=True
    ):
        normalized_alias = re.sub(r"[^a-z0-9.]+", " ", alias).strip()
        if normalized_alias and f" {normalized_alias} " in padded:
            return exe_name
    return None


def _format_game_status(status: dict[str, Any] | None) -> str:
    if not status:
        return "No supported game is currently running."

    elapsed = int(status.get("elapsed_seconds", 0))
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    elapsed_text = (
        f"{hours:d}:{minutes:02d}:{seconds:02d}"
        if hours else f"{minutes:d}:{seconds:02d}"
    )
    cpu_temp = status.get("cpu_temperature_c")
    cpu_temp_text = f"{cpu_temp:.0f} C" if cpu_temp is not None else "N/A"
    gpu = status.get("gpu", {})
    if gpu.get("available"):
        gpu_text = (
            f"{gpu.get('utilization_percent', 0):.0f}% | "
            f"{gpu.get('temperature_c', 0):.0f} C | "
            f"VRAM {gpu.get('memory_used_mb', 0):.0f}/"
            f"{gpu.get('memory_total_mb', 0):.0f} MB"
        )
    else:
        gpu_text = f"Unavailable ({gpu.get('error', 'no sensor data')})"

    return (
        f"{status['game']} is RUNNING\n"
        f"Executable: {status['executable']}\n"
        f"Elapsed: {elapsed_text}\n"
        f"CPU: {status.get('cpu_percent', 0):.0f}% | Temperature: {cpu_temp_text}\n"
        f"RAM: {status.get('ram_percent', 0):.0f}%\n"
        f"GPU: {gpu_text}"
    )


def _format_detection_debug(report: dict[str, Any]) -> str:
    candidates = report.get("candidates", [])
    rejected = report.get("rejected", [])
    active = report.get("active")
    foreground = report.get("foreground")
    lines = ["GAME DETECTION DEBUG"]
    if foreground:
        lines.extend([f"Foreground process: {foreground.get('executable')}",
                      f"Foreground window: {foreground.get('window_title') or 'none'}",
                      f"Path: {foreground.get('path') or 'unavailable'}"])
    if active:
        lines.extend([f"Selected: {active.get('game') or 'Unknown Game'}",
                      f"Candidate score: {active.get('confidence')}",
                      "Game evidence: " + (", ".join(active.get("signals", [])[:6]) or "none")])
    lines.append("Top candidates:")
    for item in candidates[:5]:
        verdict = "accepted" if item.get("accepted") else "below threshold"
        signals = ", ".join(item.get("signals", [])[:4]) or "no strong game signals"
        lines.append(f"- {item.get('game') or item.get('executable')} | {item.get('confidence')}% | {verdict}\n"
                     f"  {item.get('executable')} | {item.get('path') or 'path unavailable'}\n"
                     f"  Window: {item.get('window_title') or 'none'} | Launcher: {item.get('launcher') or 'none'} | {signals}")
    if rejected:
        lines.append("Important rejected candidates:")
        lines.extend(f"- {item.get('executable')} — {item.get('rejection_reason')}" for item in rejected[:4])
    if not candidates and not rejected: lines.append("No likely game candidates are currently running.")
    return "\n".join(lines)


def route_command(
    text: str,
    action_manager: ActionManager,
    memory: MemoryManager | None = None,
    automatic_memory: bool = True,
    self_repair: SelfRepairEngine | None = None,
) -> RouteResult:
    command = re.sub(r"\s+", " ", text.strip().lower())
    local_command = re.sub(r"[^a-z0-9 ]+", "", command).strip()
    if local_command == "pairing debug":
        service = phone_remote_service(); message = service.pairing_debug() if service else "PAIRING DEBUG\n\nPhone Remote Server: STOPPED\nPair Endpoint: UNAVAILABLE"
        return RouteResult(True, message, "Pairing Debug", "green" if service and service.server else "yellow", {}, "phone_remote")
    if local_command == "remote desktop self test":
        service = phone_remote_service()
        message = service.remote_desktop_self_test() if service else "REMOTE DESKTOP SELF TEST\n\nRemote Desktop service: FAIL — unavailable"
        return RouteResult(True, message, "Remote Desktop Self Test", "green" if service else "red", {}, "remote_desktop")
    if local_command == "remote desktop debug":
        service = phone_remote_service()
        message = service.remote_desktop_debug() if service else "Connected Monitors: 0\n\nRemote Desktop service is unavailable."
        return RouteResult(True, message, "Remote Desktop Debug", "green" if service else "yellow", {}, "remote_desktop")
    if local_command == "remote desktop status":
        service = phone_remote_service(); status = service.remote_desktop_status() if service else {
            "active": False, "mode": "INACTIVE", "device": None, "monitor": 1, "stream_fps": 0,
            "capture_latency_ms": 0, "quality": "Unavailable", "remote_input": False}
        message = (f"Remote Desktop: {'ACTIVE' if status['active'] else 'INACTIVE'}\nDevice: {status.get('device') or 'None'}\n"
                   f"Mode: {status['mode']}\nMonitor: {status.get('monitor_label', status.get('monitor'))}\nStream FPS: {status['stream_fps']}\n"
                   f"Capture Latency: {status['capture_latency_ms']} ms\nQuality: {status['quality']}\n"
                   f"Remote Input: {'Enabled' if status['remote_input'] else 'Disabled'}")
        return RouteResult(True, message, "Remote Desktop", "green" if status["active"] else "yellow", status, "remote_desktop")
    if local_command == "what did you optimize":
        status = game_optimizer().get_optimization_status()
        verified = status.get("verified_changes", []); game_name = status.get("game") or "the current game"
        if verified:
            lines = [f"For {game_name} I applied {len(verified)} verified optimization(s):"]
            lines.extend(f"• {row['action']} — {row['target']}: {row['previous_value']} -> {row['actual_value']}" for row in verified)
            message = "\n".join(lines)
        else: message = f"I haven't applied any verified system changes for {game_name} yet."
        return RouteResult(True, message, "Game Optimization", "green" if verified else "yellow", status, "game_optimization")
    if local_command in {"optimization status", "game performance"}:
        status = game_optimizer().get_optimization_status(); telemetry = status.get("telemetry", {})
        priority = telemetry.get("game_priority"); priority_text = str(priority) if priority is not None else "Unavailable"
        from .game_optimizer import _priority_name
        if priority is not None: priority_text = _priority_name(priority)
        message = (f"Game: {status.get('game') or 'None'}\nStatus: {str(status.get('status', 'Inactive')).upper()}\n"
                   f"Profile: {status.get('mode', 'Balanced')}\nGame PID: {status.get('pid') or 'Unavailable'}\n"
                   f"Game Priority: {priority_text}\nCPU: {telemetry.get('cpu_percent', 'Unavailable')}%\n"
                   f"GPU: {telemetry.get('gpu_percent', 'Unavailable')}%\nRAM: {telemetry.get('ram_percent', 'Unavailable')}%\n"
                   f"GPU Temperature: {telemetry.get('gpu_temperature_c', 'Unavailable')} C\n"
                   f"Bottleneck: {status.get('bottleneck', 'None detected')}\n"
                   f"Verified Changes: {len(status.get('verified_changes', []))}\nFailed Changes: {len(status.get('failed_changes', []))}")
        return RouteResult(True, message, "Game Optimization", "green" if status.get("active") else "yellow", status, "game_optimization")
    if local_command in {"stop game optimization", "restore game settings"}:
        status = game_optimizer().stop()
        restored, failed = status.get("restored_count", 0), sum(1 for row in status.get("ledger", []) if str(row.get("restoration_result", "")).startswith("FAILED"))
        message = f"Game optimization stopped. {restored} temporary change(s) restored successfully."
        if failed: message += f" {failed} restoration(s) failed verification."
        return RouteResult(True, message, "Game Optimization", "green" if not failed else "yellow", status, "game_optimization")
    if local_command in {"optimize game", "reoptimize game"}:
        game = live_game_status(force_refresh=True)
        if not game:
            return RouteResult(True, "No game is currently running to optimize.", "Game Optimization", "yellow", None, "game_optimization")
        from .game_monitor import system_sample
        status = game_optimizer().start_game_session(game, system_sample(game.get("pids", [])), force=local_command == "reoptimize game")
        count = len(status.get("verified_changes", []))
        message = (f"Applied and verified {count} optimization(s) for {game.get('game')}." if count else
                   f"Monitoring {game.get('game')}. No system changes were successfully verified.")
        return RouteResult(True, message, "Game Optimization", "green" if count else "yellow", status, "game_optimization")
    ollama_action = detect_ollama_action(text)
    if ollama_action:
        progress = {"restart": "Restarting Ollama...", "reconnect": "Reconnecting to Ollama...",
                    "start": "Starting Ollama...", "stop": "Stopping Ollama...",
                    "status": "Checking Ollama...", "repair": "Repairing the Ollama connection..."}[ollama_action]
        return RouteResult(True, progress, "Ollama", "yellow", {"operation": ollama_action}, "ollama_action")
    if command in {"game detection debug", "debug game detection"}:
        report = game_detection_debug()
        return RouteResult(True, _format_detection_debug(report), "Game Detection Debug", "yellow", report, "game_debug")
    if command in {"rescan games", "rescan game library", "refresh game library"}:
        result = rescan_game_library(); game = result.get("game")
        message = f"Game library refreshed. Detected {game.get('game')}." if game else "Game library refreshed. No game is active."
        return RouteResult(True, message, "Game Detection", "green", result, "game_rescan")
    correction = re.fullmatch(r"(?:that is|that's|thats) (.+)", text.strip(), re.I)
    if correction and game_detector().pending_unknown:
        name = correction.group(1).strip(" .!?")
        learned = game_detector().learn_pending(name)
        return RouteResult(True, f"Got it. I'll recognize {name} automatically next time." if learned else
                           "I couldn't save that game mapping.", "Game Detection", "green" if learned else "red",
                           {"learned": learned, "game": name}, "game_learning")
    if self_repair is not None:
        if command in {"approve", "approved", "yes", "y"} and self_repair.pending:
            result = self_repair.approve()
            return RouteResult(True, result["message"], "Self Repair", "green" if result.get("success") else "red", result, "self_repair")
        if command in {"deny", "denied", "no", "n", "cancel"} and self_repair.pending:
            result = self_repair.deny()
            return RouteResult(True, result["message"], "Self Repair", "yellow", result, "self_repair")
        if command in {"pending repair", "repair plan"}:
            return RouteResult(True, self_repair.plan_text(), "Self Repair", "yellow", None, "self_repair")
        if re.search(r"\b(fix|repair)\b.*\b(internet|network|wifi|wi fi|connection)\b", command):
            diagnostics = diagnose_network()
            if re.search(r"\b(?:do not|don't|dont|no)\b.{0,20}\b(?:change|changes|modify|reset|repair)\w*\b", command):
                return RouteResult(True, format_response(diagnostics, {"tool": "run_network_diagnostic"}),
                                   "Network Diagnostic", "green" if diagnostics.get("issue") == "healthy" else "yellow",
                                   diagnostics, "network_diagnostic")
            issue = diagnostics.get("issue")
            if issue == "healthy":
                return RouteResult(True, "Your internet connection is working in my tests, so I didn't make any changes.", "Self Repair", "green", diagnostics, "self_repair")
            if issue == "dns_failure":
                result = self_repair.prepare("network.flush_dns", "DNS resolution is failing", diagnostics=["Adapter state", "IP assignment", "Gateway", "DNS resolution", "Internet connectivity"])
            elif issue in {"invalid_ip", "missing_gateway"}:
                result = self_repair.prepare("network.renew_ip", diagnostics.get("summary", "The IP configuration is invalid"), diagnostics=["Adapter state", "IP assignment", "Default gateway"])
            elif issue == "gateway_unreachable":
                active = str((diagnostics.get("adapter") or {}).get("name") or "")
                result = self_repair.prepare("network.restart_adapter", "The network adapter has connectivity but internet access is unavailable",
                                             {"adapter": active}, ["Adapter state", "IP assignment", "Gateway", "DNS resolution", "Internet connectivity"])
            else:
                return RouteResult(True, diagnostics.get("summary", "I found a network problem, but no approved repair safely matches it."),
                                   "Self Repair", "yellow", diagnostics, "self_repair")
            return RouteResult(True, result.get("plan") or result.get("message", "No approved repair matched."),
                               "Self Repair", "yellow" if result.get("pending") else ("green" if result.get("success") else "red"), result, "self_repair")
    if memory is not None and is_memory_command(text, memory):
        result = handle_memory_command(text, memory)
        if result.handled:
            return RouteResult(True, result.message, "Memory", "green", {"memory_changed": result.changed}, "memory")
    crash_match = skill_manager.route(text)
    if crash_match and crash_match.capability in {"analyze_crash", "get_last_crash", "get_crash_history", "explain_crash"}:
        result = skill_manager.execute(crash_match)
        return RouteResult(True, result.get("message") or format_response(result), "Crash Detective",
                           "green" if result.get("success") else "yellow", result,
                           f"skill:{crash_match.skill_id}:{crash_match.capability}")
    intent = detect_intent(text)

    if intent == Intent.GREETING:
        return RouteResult(True, "Hey! What's up?", "Martin AI", "green", None, intent.value)
    if intent == Intent.GENERAL_CHAT and "doing" in command:
        return RouteResult(True, "I'm here and monitoring quietly in the background. What can I help you with?", "Martin AI", "green", None, intent.value)
    focused_tools = {
        Intent.RAM_STATUS: ("ram_status", "RAM Status"),
        Intent.CPU_STATUS: ("cpu_status", "CPU Status"),
        Intent.STORAGE_STATUS: ("storage_status", "Storage Status"),
        Intent.NETWORK_STATUS: ("network_status", "Network Status"),
        Intent.NETWORK_DIAGNOSTIC: ("run_network_diagnostic", "Network Diagnostic"),
        Intent.NETWORK_PERFORMANCE: ("investigate_network_performance", "Network Performance"),
        Intent.NETWORK_PERFORMANCE_INVESTIGATION: ("investigate_network_performance", "Network Performance Investigation"),
        Intent.NETWORK_DNS: ("check_dns", "DNS Check"),
        Intent.NETWORK_LATENCY: ("measure_network_latency", "Network Latency"),
        Intent.NETWORK_PACKET_LOSS: ("measure_network_jitter", "Packet Loss and Jitter"),
        Intent.GPU_STATUS: ("nvidia_status", "GPU Status"),
    }
    if intent in focused_tools:
        tool, title = focused_tools[intent]
        result = READ_ONLY_TOOLS[tool]()
        return RouteResult(True, format_response(result, {"tool": tool}), title, "green", result, intent.value)

    # A supported game always wins over generic app matching. Also support
    # natural queries such as "game status" when no title was named.
    referenced_game = _referenced_game(command)
    generic_game_status = command in {
        "game", "game status", "gaming status", "is a game running",
        "is any game running", "what game is running", "monitor status", "detect game",
        "what game am i playing", "current game",
    }
    if referenced_game or generic_game_status:
        try:
            status = live_game_status(referenced_game, force_refresh=command == "what game am i playing")
            if command == "what game am i playing" and status:
                message = ("You're currently running a game, but I haven't identified its title yet."
                           if status.get("game") == "Unknown Game" else f"You're playing {status['game']}.")
            elif command in {"detect game", "current game"} and status:
                message = ("A game appears to be running, but I couldn't identify the title yet."
                           if status.get("game") == "Unknown Game" else f"Game detected: {status['game']}.")
            else: message = _format_game_status(status)
            return RouteResult(
                True, message,
                "Game Monitor",
                "green" if status else "yellow",
            )
        except Exception as exc:
            return RouteResult(
                True,
                f"Game monitor status is unavailable: {exc}",
                "Game Monitor",
                "red",
            )

    if command in {"approve", "approved", "yes", "y"}:
        result = action_manager.approve()
        title = "Action Completed" if result.get("success") else "Action Failed"
        style = "green" if result.get("success") else "red"
        return RouteResult(True, _format_result(result.get("result", result)), title, style)

    if command in {"deny", "denied", "no", "n", "cancel"}:
        result = action_manager.deny()
        title = "Action Cancelled" if result.get("success") else "Nothing to Cancel"
        style = "yellow"
        return RouteResult(True, _format_result(result), title, style)

    if command in {"pending", "pending action", "what is pending"}:
        return RouteResult(
            True,
            action_manager.describe_text(),
            "Pending Action",
            "yellow",
        )

    if command in {"what apps can you open", "list apps", "available apps", "apps"}:
        result = READ_ONLY_TOOLS["available_apps"]()
        apps = result.get("apps", [])
        return RouteResult(
            True,
            "\n".join(f"- {app.title()}" for app in apps),
            "Approved Apps",
            "green",
        )

    if command in {"gpu", "gpu status", "check gpu", "check my gpu", "temps", "temperature"}:
        result = READ_ONLY_TOOLS["nvidia_status"]()
        return RouteResult(True, _format_result(result), "GPU Status", "green")

    if intent == Intent.PC_HEALTH or command in {"system summary", "check my system", "system info", "pc info"}:
        result = READ_ONLY_TOOLS["system_summary"]()
        if memory is not None and automatic_memory:
            memory.remember_pc_profile(result)
        return RouteResult(True, format_response(result, {"tool": "system_summary"}), "System Summary", "green", result)

    if command in {"running apps", "list running apps", "show running apps"}:
        result = READ_ONLY_TOOLS["list_running_apps"]()
        apps = result.get("apps", [])
        message = "\n".join(
            f"- {item['name']} ({item['instances']})" for item in apps
        ) or "No running applications were found."
        return RouteResult(True, message, "Running Apps", "green")

    if command in {"take screenshot", "take a screenshot", "screenshot"}:
        result = READ_ONLY_TOOLS["take_screenshot"]()
        return RouteResult(
            True,
            _format_result(result),
            "Screenshot",
            "green" if result.get("success") else "red",
        )

    folder_match = re.fullmatch(r"(?:open|show)(?: my)? (desktop|downloads|documents|pictures|videos|music|reports)(?: folder)?", command)
    if folder_match:
        folder = folder_match.group(1)
        result = READ_ONLY_TOOLS["open_user_folder"](folder_name=folder)
        return RouteResult(True, _format_result(result), "Folder", "green")

    open_match = re.fullmatch(r"(?:open|launch|start|run) (.+)", command)
    if open_match:
        app_name = _normalize_name(open_match.group(1))
        if app_name in FOLDERS:
            result = READ_ONLY_TOOLS["open_user_folder"](folder_name=app_name)
            return RouteResult(True, _format_result(result), "Folder", "green")

        result = READ_ONLY_TOOLS["open_app"](app_name=app_name)
        return RouteResult(
            True,
            _format_result(result),
            "App Launcher",
            "green" if result.get("success") else "red",
        )

    close_match = re.fullmatch(r"(?:close|quit|exit|stop) (.+)", command)
    if close_match:
        app_name = _normalize_name(close_match.group(1))
        result = action_manager.queue("close_app", {"app_name": app_name})
        return RouteResult(
            True,
            action_manager.describe_text() if result.get("pending") else _format_result(result),
            "Approval Required" if result.get("pending") else "Action Failed",
            "yellow" if result.get("pending") else "red",
        )

    status_match = re.fullmatch(r"(?:is|check if) (.+?) (?:running|open)", command)
    if status_match:
        app_name = _normalize_name(status_match.group(1))
        result = READ_ONLY_TOOLS["app_status"](app_name=app_name)
        if result.get("success"):
            state = "running" if result.get("running") else "not running"
            return RouteResult(True, f"{app_name.title()} is {state}.", "App Status", "green")
        return RouteResult(True, _format_result(result), "App Status", "red")

    skill_match = skill_manager.route(text)
    if skill_match and skill_match.confidence >= .60:
        result = skill_manager.execute(skill_match)
        skill = skill_manager.skills[skill_match.skill_id]
        message = format_response(result, {"tool": skill_match.capability})
        if not result.get("success") and result.get("approval_required"):
            message = "That action needs your confirmation first."
        return RouteResult(True, message, skill.metadata.name,
                           "green" if result.get("success") else "yellow", result,
                           f"skill:{skill_match.skill_id}:{skill_match.capability}")
    return RouteResult(False)
