from __future__ import annotations

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
        # Sensor classifications come from measurements, not generated wording.
        # A threshold crossing confirms a condition, not the cause of the request.
        alerts = [item for item in observations if item.startswith("ALERT:")]
        if alerts:
            state.diagnosis = alerts[0].removeprefix("ALERT: ")
            state.confidence = "medium"
        elif observations:
            state.diagnosis = "No clear resource or sensor threshold problem was found in the collected snapshot."
            state.confidence = "medium"
        else:
            state.diagnosis = "The investigation was inconclusive because no usable evidence was available."
            state.confidence = "low"
        state.recommendations = recommendations or ["Reproduce the issue while Martin's game monitor is active, then investigate again."]
        if observations:
            state.uncertainty.append("These snapshot readings do not establish the cause of the reported problem.")
        if state.errors:
            state.uncertainty.append("Some tools or sensors were unavailable; see errors.")

    def _observations(self, state: BrainState) -> tuple[list[str], list[str]]:
        found: list[str] = []
        recs: list[str] = []
        state.confirmed_findings = []
        state.possible_causes = []
        state.normal_readings = []
        for item in state.evidence:
            tool, result = item.get("tool"), item.get("result", {})
            if not isinstance(result, dict) or result.get("success") is False:
                continue
            if tool in {"system_summary", "storage_status"}:
                ram = result.get("ram_percent_used")
                if isinstance(ram, (int, float)):
                    found.append(f"RAM usage was {ram}% (high-pressure threshold: 90%).")
                    if ram >= 90:
                        finding = f"High memory pressure: RAM is {ram:g}% used (threshold: 90%)."
                        state.confirmed_findings.append(finding)
                        state.possible_causes.append("Memory pressure may contribute to slowness; check while the issue occurs.")
                        found.append("ALERT: " + finding)
                        recs.append("Close an approved memory-heavy app before launching the game.")
                    else:
                        state.normal_readings.append(f"RAM is {ram:g}% used, below the 90% high-pressure threshold.")
                for disk in result.get("disks", []):
                    if not isinstance(disk, dict):
                        continue
                    free, used = disk.get("free_gb"), disk.get("percent_used")
                    if not isinstance(free, (int, float)):
                        continue
                    low = free < 30 or (isinstance(used, (int, float)) and used >= 90)
                    drive = disk.get("device") or disk.get("mountpoint") or "A drive"
                    if low:
                        finding = f"{drive} has {'critically low' if free < 10 else 'low'} free space ({free:g} GB)."
                        state.confirmed_findings.append(finding)
                        state.possible_causes.append("Low storage may affect performance, but this snapshot does not prove it caused the slowdown.")
                        found.append("ALERT: " + finding)
                        recs.append("Review storage usage and free space on the affected drive.")
                    else:
                        reading = f"{drive} has {free:g} GB free, above the low-space thresholds."
                        state.normal_readings.append(reading)
                        found.append(reading)
            elif tool == "investigate_storage":
                scope = str(result.get("scope", "Selected folders only."))
                state.uncertainty.append(scope)
                state.uncertainty.append(str(result.get("size_basis", "Measured file sizes only.")))
                if result.get("partial"):
                    state.uncertainty.append("The storage scan is partial: time/file limits or inaccessible entries prevented a complete count. Sizes are lower bounds for scanned locations.")
                for location in result.get("locations", [])[:5]:
                    size = location.get("bytes", 0) / 2**30
                    finding = f"{location.get('name')}: {size:.2f} GB observed" + (" (partial count)." if not location.get("complete") else ".")
                    state.confirmed_findings.append(finding)
                    found.append(finding)
                for file in result.get("largest_files", [])[:10]:
                    state.confirmed_findings.append(f"Large file observed: {file.get('path')} ({file.get('bytes', 0) / 2**30:.2f} GB).")
                recs.extend(str(line) for line in result.get("suggestions", []))
            elif tool == "game_status" and result.get("game"):
                found.append(f"{result.get('game')} was detected with {result.get('elapsed_seconds', 0)} seconds elapsed.")
            elif tool == "nvidia_status":
                temp = result.get("temperature_c")
                used, total = result.get("memory_used_mb"), result.get("memory_total_mb")
                if isinstance(temp, (int, float)):
                    found.append(f"GPU temperature was {temp} C.")
                    if temp >= 85:
                        finding = f"GPU temperature is high ({temp:g} C; threshold: 85 C)."
                        state.confirmed_findings.append(finding)
                        state.possible_causes.append("Heat may affect GPU performance; check load and clock speeds during the issue.")
                        found.append("ALERT: " + finding)
                        recs.append("Check airflow and reduce GPU load.")
                    else:
                        state.normal_readings.append(f"GPU temperature is {temp:g} C, below the 85 C warning threshold.")
                if all(isinstance(x, (int, float)) for x in (used, total)) and total and used / total >= .95:
                    finding = "GPU memory usage is at or above the 95% warning threshold."
                    state.confirmed_findings.append(finding)
                    state.possible_causes.append("GPU memory pressure may affect performance; compare with the same workload.")
                    found.append("ALERT: " + finding)
                    recs.append("Reduce texture quality or resolution.")
            elif tool == "recent_windows_errors":
                events = result.get("events", [])
                if events:
                    state.confirmed_findings.append(f"Windows recorded {len(events)} recent warning/error events.")
                    state.possible_causes.append("Recent Windows events need inspection; their count alone does not establish a cause.")
                    found.append(f"Windows reported {len(events)} recent relevant error or warning events. "
                                 "Their relationship to this problem is unconfirmed.")
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
