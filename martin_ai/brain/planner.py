from __future__ import annotations

import json
from typing import Any, Callable

from .models import PlanStep
from .policies import LIMITS, ToolPolicy


ModelClient = Callable[[str], str]


class Planner:
    def __init__(self, policies: dict[str, ToolPolicy], model_client: ModelClient | None = None) -> None:
        self.policies = policies
        self.model_client = model_client

    def create_plan(self, request: str, context: dict[str, Any] | None = None) -> list[PlanStep]:
        # Direct status/action requests are handled by the fast intent router.
        # Requests reaching this planner are investigations, so let the local
        # model choose a small validated plan and retain deterministic fallback.
        if self.model_client:
            try:
                prompt = self._prompt(request, context or {})
                return self._validate(json.loads(self.model_client(prompt)))
            except Exception:
                pass
        return self._fallback(request)

    @staticmethod
    def _is_common_diagnostic(request: str) -> bool:
        lower = request.lower()
        phrases = (
            "slow", "stutter", "lag", "crash", "error", "not opening",
            "not launching", "ram", "memory", "cpu", "gpu", "temperature",
            "overheat", "optimize", "system health", "check my system",
            "game", "fortnite", "r.e.p.o", "repo",
            "network", "internet", "wifi", "dns", "ping", "packet loss",
        )
        return any(phrase in lower for phrase in phrases)

    def _prompt(self, request: str, context: dict[str, Any]) -> str:
        tools = [
            {"name": p.name, "description": p.description,
             "approval_required": p.approval_required, "category": p.category}
            for p in self.policies.values() if p.risk_level in {"read_only", "protected"}
        ]
        return (
            "Return JSON only: an array of 1-5 necessary troubleshooting steps. Do not select a tool unless it helps answer the request. Each step requires "
            "step_id, goal, tool_name, arguments, expected_evidence. Use only these tools. "
            "Do not create shell commands or include approval_required; policy supplies it.\n"
            f"TOOLS={json.dumps(tools)}\nCONTEXT={json.dumps(context, default=str)}\n"
            f"USER_REQUEST={request}"
        )

    def _validate(self, raw: Any) -> list[PlanStep]:
        if not isinstance(raw, list) or not 1 <= len(raw) <= LIMITS["maximum_plan_steps"]:
            raise ValueError("Invalid plan size")
        plan = []
        seen = set()
        for index, item in enumerate(raw, 1):
            if not isinstance(item, dict) or item.get("tool_name") not in self.policies:
                raise ValueError("Unknown tool")
            tool = str(item["tool_name"])
            arguments = item.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError("Invalid arguments")
            signature = (tool, json.dumps(arguments, sort_keys=True, default=str))
            if signature in seen:
                raise ValueError("Repeated tool call")
            seen.add(signature)
            policy = self.policies[tool]
            if policy.risk_level not in {"read_only", "protected"}:
                raise ValueError("Tool is not permitted in autonomous plans")
            plan.append(PlanStep(
                step_id=str(item.get("step_id") or index),
                goal=str(item.get("goal") or policy.description),
                tool_name=tool,
                arguments=arguments,
                approval_required=policy.approval_required,
                expected_evidence=str(item.get("expected_evidence") or "Relevant diagnostic evidence"),
            ))
        return plan

    def _fallback(self, request: str) -> list[PlanStep]:
        lower = request.lower()
        choices: list[tuple[str, dict[str, Any], str, str]] = []
        if any(word in lower for word in ("network", "internet", "wifi", "wi-fi", "connection", "dns", "ping", "packet loss")):
            if "dns" in lower or "website" in lower:
                choices.append(("check_dns", {}, "Test DNS configuration and resolution", "Configured DNS servers and measured resolution"))
                choices.append(("check_internet_connectivity", {}, "Test external connectivity", "External reachability"))
            elif "ping" in lower or "latency" in lower or "packet loss" in lower:
                choices.append(("measure_network_jitter", {}, "Measure latency, jitter, and packet loss", "Multiple measured latency samples, variation, and loss"))
            elif any(word in lower for word in ("slow", "performance", "unstable", "intermittent")):
                choices.append(("investigate_network_performance", {}, "Investigate network performance", "Router versus external latency, jitter, signal, DNS timing, and utilization"))
            else:
                choices.append(("run_network_diagnostic", {}, "Run the full read-only network diagnostic", "Adapter, IP, gateway, DNS, internet, latency, and loss"))
        if any(word in lower for word in ("game", "stutter", "fortnite", "r.e.p.o", "repo", "gpu")):
            choices.append(("game_status", {}, "Check active game-monitor status", "Detected game, elapsed time, and live metrics"))
            choices.append(("nvidia_status", {}, "Check GPU health and load", "GPU temperature, utilization, driver, and VRAM"))
        choices.append(("system_summary", {}, "Check CPU, memory, and disk pressure", "RAM and disk utilization"))
        if any(word in lower for word in ("slow", "stutter", "ram", "memory", "optimize", "healthy", "gpu")):
            choices.append(("top_processes", {"limit": 10}, "Identify resource-heavy background processes", "Top CPU and memory consumers"))
        if any(word in lower for word in ("crash", "not opening", "not launching", "store", "steam", "error", "stutter")):
            choices.append(("recent_windows_errors", {"log_name": "Application", "hours": 24, "limit": 20}, "Inspect recent Windows application errors", "Relevant crash and warning events"))
        if "healthy" in lower:
            choices.append(("recent_windows_errors", {"log_name": "System", "hours": 24, "limit": 20}, "Inspect recent Windows system errors", "Hardware, driver, and service errors"))
        if len(choices) < 3:
            choices.append(("recent_windows_errors", {"log_name": "System", "hours": 24, "limit": 12}, "Check for recent system-level faults", "Driver, hardware, or service warnings"))
        unique = []
        for tool, args, goal, expected in choices:
            if tool in self.policies and (tool, json.dumps(args, sort_keys=True)) not in {(x[0], json.dumps(x[1], sort_keys=True)) for x in unique}:
                unique.append((tool, args, goal, expected))
        return [PlanStep(str(i), goal, tool, args, self.policies[tool].approval_required, expected)
                for i, (tool, args, goal, expected) in enumerate(unique[:5], 1)]
