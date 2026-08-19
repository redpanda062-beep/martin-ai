from __future__ import annotations

import re
from typing import Any

from .base import Capability, Skill, SkillMatch, SkillMetadata


class MappedSkill(Skill):
    capability_tools: dict[str, str] = {}

    def can_handle(self, request: str) -> SkillMatch | None:
        text = re.sub(r"[^a-z0-9]+", " ", request.lower()).strip()
        best: SkillMatch | None = None
        for capability in self.metadata.capabilities:
            hits = sum(1 for keyword in capability.keywords if keyword in text)
            if not hits:
                continue
            score = min(.98, .55 + hits * .12)
            parameters: dict[str, Any] = {}
            if capability.id == "open_app":
                opened = re.fullmatch(r"(?:please )?(?:open|launch|start|run) (.+)", text)
                if not opened:
                    continue
                parameters = {"app_name": opened.group(1)}
            candidate = SkillMatch(self.metadata.id, capability.id, score, parameters)
            if best is None or candidate.confidence > best.confidence:
                best = candidate
        return best

    def execute(self, action: str, parameters: dict[str, Any], *, approved: bool = False) -> dict[str, Any]:
        tool = self.capability_tools.get(action, action)
        if action not in {item.id for item in self.metadata.capabilities}:
            return {"success": False, "error": "This skill does not provide that capability."}
        return self._executor(tool, parameters, approved)


class GamingSkill(MappedSkill):
    metadata = SkillMetadata("gaming", "Gaming", "Game detection, diagnostics and optimization.", "1.0.0", "Martin AI",
        (Capability("game_status", "Detect the active game", 1, keywords=("what game", "game am i playing", "game status", "detect game")),
         Capability("analyze_crash", "Detect and analyze crash evidence", 1, keywords=("why did my game crash", "why did", "keep crashing", "what crashed")),
         Capability("get_last_crash", "Read the latest crash", 1, keywords=("last crash", "caused that crash")),
         Capability("get_crash_history", "Read crash history and repeated patterns", 1, keywords=("crash history", "been crashing a lot", "show my crashes")),
         Capability("explain_crash", "Explain the latest crash", 1, keywords=("technical details", "explain crash"))),
        ("gaming", "game_crash"), (1, 2, 3), "standard", ("system_control",))


class PCRepairSkill(MappedSkill):
    metadata = SkillMetadata("pc_repair", "PC Repair", "Safe diagnostics and approved repair entry points.", "1.0.0", "Martin AI",
        (Capability("system_summary", "Check Windows health", 1, keywords=("wrong with my pc", "pc health", "check my system")),
         Capability("recent_windows_errors", "Inspect Windows event logs", 1, keywords=("event log", "windows error", "crash")),
         Capability("run_network_diagnostic", "Diagnose network problems", 1, keywords=("check my network", "network problem", "internet problem"))),
        ("diagnose", "repair"), (1, 3), "standard", ("system_control",))


class SystemControlSkill(MappedSkill):
    metadata = SkillMetadata("system_control", "System Control", "Approved application and system information controls.", "1.0.0", "Martin AI",
        (Capability("open_app", "Open an approved application", 2, keywords=("open", "launch", "start", "run")),
         Capability("system_summary", "Read system information", 1, keywords=("system information", "system info", "pc specs")),
         Capability("ram_status", "Read memory use", 1, keywords=("ram usage", "memory usage", "using all my ram")),
         Capability("top_processes", "Find resource-heavy processes", 1, keywords=("using all my ram", "using my memory", "resource usage"))),
        ("system",), (1, 2, 3), "standard")


class SecuritySkill(MappedSkill):
    metadata = SkillMetadata("security", "Security", "Read-only protection monitoring and approved Defender actions.", "1.0.0", "Martin AI",
        (Capability("windows_security_status", "Read Windows protection state", 1, keywords=("security status", "defender status", "firewall status")),
         Capability("security_health_report", "Assess local security health", 1, keywords=("security check", "anything suspicious", "malware check"))),
        ("security",), (1, 3), "protected")


class DeviceSkill(MappedSkill):
    metadata = SkillMetadata("device", "Devices", "Read-only Windows hardware and adapter inventory.", "1.0.0", "Martin AI",
        (Capability("display_status", "Inspect connected monitors", 1, keywords=("second monitor", "monitor disconnected", "display status", "monitor working")),
         Capability("controller_status", "Inspect game controllers", 1, keywords=("controller", "gamepad")),
         Capability("usb_status", "Inspect USB devices", 1, keywords=("usb device", "usb status")),
         Capability("audio_device_status", "Inspect audio devices", 1, keywords=("audio device", "sound device")),
         Capability("network_adapter_status", "Inspect network adapters", 1, keywords=("network adapter", "wifi adapter")),
         Capability("bluetooth_status", "Inspect Bluetooth devices", 1, keywords=("bluetooth device", "bluetooth status")),
         Capability("nvidia_status", "Read GPU temperature", 1, keywords=("gpu temperature", "gpu temp", "graphics temperature"))),
        ("device", "hardware"), (1,), "read_only", ("system_control",))


class MemorySkill(MappedSkill):
    metadata = SkillMetadata("memory", "Memory", "Existing persistent memory and history interface.", "1.0.0", "Martin AI", (), ("memory",), (1,), "read_only")


class RemoteSkill(MappedSkill):
    metadata = SkillMetadata("remote", "Remote Control", "Existing paired-phone remote control interface.", "1.0.0", "Martin AI", (), ("remote",), (1, 2, 3), "protected", ("system_control",))


BUILTIN_SKILLS = (SystemControlSkill, GamingSkill, PCRepairSkill, SecuritySkill, DeviceSkill, MemorySkill, RemoteSkill)
