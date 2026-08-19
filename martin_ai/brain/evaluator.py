from __future__ import annotations

import json
from typing import Any, Callable

from .models import BrainState, PlanStep
from .policies import LIMITS, ToolPolicy


class Evaluator:
    def __init__(self, policies: dict[str, ToolPolicy], model_client: Callable[[str], str] | None = None) -> None:
        self.policies = policies
        self.model_client = model_client

    def review(self, state: BrainState) -> list[PlanStep]:
        revision = self._rule_revision(state)
        if revision and state.plan_revisions < LIMITS["maximum_plan_revisions"]:
            state.plan_revisions += 1
            return revision
        return []

    def finalize(self, state: BrainState) -> None:
        observations, recommendations = self._observations(state)
        if self.model_client and observations:
            try:
                raw = json.loads(self.model_client(
                    "Return JSON only with diagnosis, confidence (low/medium/high), uncertainty array, "
                    "and recommendations array. Use only this evidence: " + json.dumps(observations, default=str)
                ))
                if isinstance(raw, dict) and raw.get("diagnosis"):
                    state.diagnosis = str(raw["diagnosis"])
                    state.confidence = str(raw.get("confidence", "medium")).lower()
                    if state.confidence not in {"low", "medium", "high"}:
                        state.confidence = "low"
                    state.uncertainty = [str(x) for x in raw.get("uncertainty", [])][:5]
                    state.recommendations = [str(x) for x in raw.get("recommendations", [])][:6]
                    return
            except Exception:
                pass
        alerts = [item for item in observations if item.startswith("ALERT:")]
        if alerts:
            state.diagnosis = alerts[0].removeprefix("ALERT: ")
            state.confidence = "high" if len(alerts) > 1 else "medium"
        elif observations:
            state.diagnosis = "No clear resource or sensor threshold problem was found in the collected snapshot."
            state.confidence = "medium"
        else:
            state.diagnosis = "The investigation was inconclusive because no usable evidence was available."
            state.confidence = "low"
        state.recommendations = recommendations or ["Reproduce the issue while Martin's game monitor is active, then investigate again."]
        if state.errors:
            state.uncertainty.append("Some tools or sensors were unavailable; see errors.")

    def _observations(self, state: BrainState) -> tuple[list[str], list[str]]:
        found: list[str] = []
        recs: list[str] = []
        for item in state.evidence:
            tool, result = item.get("tool"), item.get("result", {})
            if tool == "system_summary":
                ram = result.get("ram_percent_used")
                if isinstance(ram, (int, float)):
                    found.append(f"RAM usage was {ram}%.")
                    if ram >= 90:
                        found.append("ALERT: High system-memory pressure is the likely cause.")
                        recs.append("Close an approved memory-heavy app before launching the game.")
                for disk in result.get("disks", []):
                    if disk.get("free_gb", 999) < 10:
                        found.append(f"ALERT: {disk.get('device')} has critically low free space ({disk.get('free_gb')} GB).")
                        recs.append("Free at least 10 GB on the affected drive.")
            elif tool == "game_status" and result.get("game"):
                found.append(f"{result.get('game')} was detected with {result.get('elapsed_seconds', 0)} seconds elapsed.")
            elif tool == "nvidia_status":
                temp = result.get("temperature_c")
                used, total = result.get("memory_used_mb"), result.get("memory_total_mb")
                if isinstance(temp, (int, float)):
                    found.append(f"GPU temperature was {temp} C.")
                    if temp >= 85:
                        found.append("ALERT: GPU thermal pressure is the likely cause.")
                        recs.append("Check airflow and reduce GPU load.")
                if all(isinstance(x, (int, float)) for x in (used, total)) and total and used / total >= .95:
                    found.append("ALERT: GPU memory pressure is the likely cause.")
                    recs.append("Reduce texture quality or resolution.")
            elif tool == "recent_windows_errors":
                events = result.get("events", [])
                if events:
                    found.append(f"Windows reported {len(events)} recent relevant error or warning events.")
        return found, list(dict.fromkeys(recs))

    def _rule_revision(self, state: BrainState) -> list[PlanStep]:
        has_top = any(step.tool_name == "top_processes" for step in state.active_plan)
        high_ram = any(
            item.get("tool") == "system_summary" and item.get("result", {}).get("ram_percent_used", 0) >= 90
            for item in state.evidence
        )
        if high_ram and not has_top and "top_processes" in self.policies:
            return [PlanStep(
                step_id=f"r{state.plan_revisions + 1}-1",
                goal="Identify the processes causing memory pressure",
                tool_name="top_processes", arguments={"limit": 10},
                approval_required=False, expected_evidence="Highest memory-consuming processes",
            )]
        return []
