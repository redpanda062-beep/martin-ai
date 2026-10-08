from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from martin_ai.actions import ActionManager
from martin_ai.brain import BrainState, BrainStatus, Orchestrator, PlanStep
from martin_ai.brain.evaluator import Evaluator
from martin_ai.brain.executor import Executor
from martin_ai.brain.memory import save_report
from martin_ai.brain.planner import Planner
from martin_ai.brain.policies import ToolPolicy
from martin_ai.memory import Database, MemoryManager


def policy(name: str, protected: bool = False) -> ToolPolicy:
    return ToolPolicy(name, name, "protected" if protected else "read_only", protected,
                      "test", 5, {"limit": {"min": 1, "max": 30}} if name == "top_processes" else {})


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policies = {name: policy(name) for name in (
            "game_status", "nvidia_status", "system_summary", "top_processes", "recent_windows_errors"
        )}

    def test_valid_plan_creation(self) -> None:
        raw = json.dumps([{"step_id": "1", "goal": "Check memory", "tool_name": "system_summary",
                           "arguments": {}, "expected_evidence": "RAM"}])
        plan = Planner(self.policies, lambda _: raw).create_plan("Why is RAM high?")
        self.assertEqual(plan[0].tool_name, "system_summary")

    def test_malformed_model_plan_uses_fallback(self) -> None:
        plan = Planner(self.policies, lambda _: "not-json").create_plan("Why is R.E.P.O. stuttering?")
        self.assertGreaterEqual(len(plan), 3)
        self.assertTrue(all(step.tool_name in self.policies for step in plan))

    def test_unknown_tool_is_rejected_and_falls_back(self) -> None:
        raw = json.dumps([{"tool_name": "powershell", "arguments": {"command": "whoami"}}])
        plan = Planner(self.policies, lambda _: raw).create_plan("check pc")
        self.assertNotIn("powershell", [step.tool_name for step in plan])

    def test_common_diagnostic_uses_model_then_safe_fallback(self) -> None:
        calls = []
        plan = Planner(self.policies, lambda prompt: calls.append(prompt) or "[]").create_plan(
            "Optimize my PC because games stutter"
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("system_summary", [step.tool_name for step in plan])
        self.assertIn("top_processes", [step.tool_name for step in plan])


class ExecutionTests(unittest.TestCase):
    def test_read_only_execution(self) -> None:
        calls = []
        tools = {"check": lambda: calls.append(1) or {"success": True, "value": 4}}
        state = BrainState("test", [PlanStep("1", "check", "check")])
        executor = Executor(tools, {}, {"check": policy("check")}, ActionManager({}))
        self.assertEqual(executor.run_next(state), "step_completed")
        self.assertEqual(calls, [1])

    def test_protected_action_queue_approval_and_resume(self) -> None:
        calls = []
        repairs = {"repair": lambda: calls.append(1) or {"success": True}}
        manager = ActionManager(repairs)
        policies = {"repair": policy("repair", True)}
        brain = Orchestrator({}, repairs, policies, manager, lambda _: json.dumps([
            {"step_id": "1", "goal": "repair", "tool_name": "repair", "arguments": {}, "expected_evidence": "result"}
        ]))
        self.assertEqual(brain.start("repair").status, BrainStatus.WAITING_FOR_APPROVAL.value)
        self.assertEqual(calls, [])
        self.assertEqual(brain.approve().status, BrainStatus.COMPLETED.value)
        self.assertEqual(calls, [1])

    def test_denial_resumes_without_running(self) -> None:
        calls = []
        repairs = {"repair": lambda: calls.append(1) or {"success": True}}
        brain = Orchestrator({}, repairs, {"repair": policy("repair", True)}, ActionManager(repairs),
                             lambda _: json.dumps([{"tool_name": "repair", "arguments": {}}]))
        brain.start("repair")
        state = brain.deny()
        self.assertEqual(state.status, BrainStatus.COMPLETED.value)
        self.assertEqual(calls, [])
        self.assertTrue(state.denied_actions)

    def test_cancellation(self) -> None:
        repairs = {"repair": lambda: {"success": True}}
        brain = Orchestrator({}, repairs, {"repair": policy("repair", True)}, ActionManager(repairs),
                             lambda _: json.dumps([{"tool_name": "repair", "arguments": {}}]))
        brain.start("repair")
        self.assertEqual(brain.cancel().status, BrainStatus.CANCELLED.value)

    def test_maximum_tool_calls(self) -> None:
        state = BrainState("test", [PlanStep("1", "check", "check")], tool_calls=12)
        executor = Executor({"check": lambda: {"success": True}}, {}, {"check": policy("check")}, ActionManager({}))
        self.assertEqual(executor.run_next(state), "failed")

    def test_repeated_tool_loop_protection(self) -> None:
        steps = [PlanStep(str(i), "check", "check") for i in range(3)]
        state = BrainState("test", steps)
        executor = Executor({"check": lambda: {"success": False, "error": "no"}}, {},
                            {"check": policy("check")}, ActionManager({}))
        executor.run_next(state)
        executor.run_next(state)
        executor.run_next(state)
        self.assertIn("Repeated-tool loop prevented.", state.errors)


class EvaluatorAndReportTests(unittest.TestCase):
    def test_investigations_recall_history_without_executing_historical_actions(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch("martin_ai.brain.memory.REPORT_DIR", Path(folder) / "reports"):
            memory = MemoryManager(Database(Path(folder) / "memory.db"))
            changes = []
            memory.save_repair_history("Why is my PC slow?", [], "dangerous_old_action", 3, True,
                                       {"success": True}, {"success": True}, None, "verified")
            tools = {"system_summary": lambda: {"success": True, "ram_percent_used": 60}}
            policies = {"system_summary": policy("system_summary"), "repair": policy("repair", True)}
            brain = Orchestrator(tools, {"repair": lambda: changes.append(1)}, policies,
                                 ActionManager({}), memory=memory)
            first = brain.start("Why is my PC slow?")
            self.assertEqual(changes, [])
            self.assertFalse(first.approved_actions)
            self.assertEqual(first.related_history[0]["repair_id"], "dangerous_old_action")
            memory.db.close()
            restarted = MemoryManager(Database(Path(folder) / "memory.db"))
            second = Orchestrator(tools, {}, policies, ActionManager({}), memory=restarted).start("My computer is sluggish")
            self.assertTrue(any(case["kind"] == "investigation" for case in second.related_history))
            self.assertEqual(changes, [])
            restarted.db.close()

    def test_sensor_assessment_does_not_accept_generated_causal_claims(self) -> None:
        prompts = []
        state = BrainState("why is my pc slow", evidence=[
            {"tool": "system_summary", "result": {"ram_percent_used": 71.9,
                "disks": [{"device": "C:", "free_gb": 19.4}]}},
            {"tool": "recent_windows_errors", "result": {"events": [{}, {}]}},
        ])
        Evaluator({}, lambda prompt: prompts.append(prompt) or '{"diagnosis":"RAM near threshold caused slowness"}').finalize(state)
        self.assertEqual(prompts, [])
        self.assertIn("71.9%", state.normal_readings[0])
        self.assertTrue(any("19.4 GB" in line for line in state.confirmed_findings))
        self.assertTrue(any("does not prove" in line for line in state.possible_causes))
        self.assertTrue(any("count alone" in line for line in state.possible_causes))
        self.assertNotIn("High system-memory pressure", state.diagnosis)

    def test_threshold_crossing_confirms_condition_not_cause(self) -> None:
        state = BrainState("why is my pc slow", evidence=[
            {"tool": "system_summary", "result": {"ram_percent_used": 90}},
            {"tool": "nvidia_status", "result": {"temperature_c": 85, "memory_used_mb": 950, "memory_total_mb": 1000}},
        ])
        Evaluator({}).finalize(state)
        self.assertEqual(len(state.confirmed_findings), 3)
        self.assertEqual(state.normal_readings, [])
        self.assertNotIn("likely cause", state.diagnosis)
        self.assertTrue(state.possible_causes)

    def test_failed_sensor_does_not_create_normal_readings(self) -> None:
        state = BrainState("why slow", evidence=[
            {"tool": "system_summary", "result": {"success": False, "ram_percent_used": 20}},
        ])
        Evaluator({}).finalize(state)
        self.assertEqual(state.normal_readings, [])
        self.assertIn("inconclusive", state.diagnosis)

    def test_evaluator_plan_revision(self) -> None:
        state = BrainState("RAM high", evidence=[{"tool": "system_summary", "result": {"ram_percent_used": 94}}])
        revision = Evaluator({"top_processes": policy("top_processes")}).review(state)
        self.assertEqual(revision[0].tool_name, "top_processes")

    def test_report_creation(self) -> None:
        state = BrainState("test request", diagnosis="Snapshot assessed",
                           normal_readings=["RAM below threshold."],
                           possible_causes=["Cause remains unconfirmed."])
        with tempfile.TemporaryDirectory() as folder, patch("martin_ai.brain.memory.REPORT_DIR", Path(folder)):
            paths = save_report(state)
            self.assertTrue(Path(paths["json"]).exists())
            self.assertTrue(Path(paths["markdown"]).exists())
            report = Path(paths["markdown"]).read_text(encoding="utf-8")
            self.assertIn("Normal Readings", report)
            self.assertIn("Possible Causes", report)


if __name__ == "__main__":
    unittest.main()
