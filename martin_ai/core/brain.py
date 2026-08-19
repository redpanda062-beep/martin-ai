from __future__ import annotations

from ..models import ActionRequest, AgentDecision
from ..monitoring import PerceptionSnapshot


class AgentBrain:
    """Deterministic safety-first analysis; an LLM may explain but never selects commands."""

    def analyze(self, snapshot: PerceptionSnapshot) -> AgentDecision:
        system = snapshot.system
        game = snapshot.game
        if not snapshot.connectivity.get("connected", True):
            action = ActionRequest("check_network", trigger="connectivity_lost",
                                   reason="The connectivity probe failed.",
                                   expected_result="A diagnostic identifies whether DNS or the route failed.",
                                   verification_method="Repeat the connectivity probe.")
            return AgentDecision("Network connectivity failed", "Network interruption", .75, "medium",
                                 action, 1, action.expected_result, action.verification_method,
                                 "The PC could not reach the configured connectivity endpoint.")
        gpu = system.get("gpu") or {}
        if game and isinstance(gpu.get("temperature_c"), (int, float)) and gpu["temperature_c"] >= 88:
            return AgentDecision(
                f"{game.get('game')} is running and GPU temperature is {gpu['temperature_c']} C",
                "GPU thermal pressure", .9, "high", None, 1,
                "Temperature falls below the warning threshold", "Continue passive temperature monitoring",
                "GPU temperature is above the safety threshold. No automatic system change is appropriate.",
                ["Reduce graphics load", "Check fans and airflow"],
            )
        if system.get("ram_percent", 0) >= 92:
            action = ActionRequest("top_processes", trigger="high_memory", reason="RAM pressure is high",
                                   expected_result="Identify the largest memory consumers.",
                                   verification_method="Collect another memory snapshot.")
            return AgentDecision("RAM use exceeds 92%", "Memory pressure", .85, "high", action, 1,
                                 action.expected_result, action.verification_method,
                                 "Memory pressure may cause stuttering or application failures.")
        return AgentDecision("No critical threshold exceeded", "No active incident", .8, "info", None, 1,
                             "Continue stable operation", "Observe the next snapshot",
                             "System telemetry is within the configured critical thresholds.")
