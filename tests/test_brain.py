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
    def test_model_receives_threshold_and_event_causality_context(self) -> None:
        prompts = []
        state = BrainState("why is my pc slow", evidence=[
            {"tool": "system_summary", "result": {"ram_percent_used": 72.9}},
            {"tool": "recent_windows_errors", "result": {"events": [{}, {}]}},
        ])
        Evaluator({}, lambda prompt: prompts.append(prompt) or "{}").finalize(state)
        self.assertIn("high-pressure threshold: 90%", prompts[0])
        self.assertIn("relationship to this problem is unconfirmed", prompts[0])
        self.assertNotIn("High system-memory pressure", state.diagnosis)

    def test_evaluator_plan_revision(self) -> None:
        state = BrainState("RAM high", evidence=[{"tool": "system_summary", "result": {"ram_percent_used": 94}}])
        revision = Evaluator({"top_processes": policy("top_processes")}).review(state)
        self.assertEqual(revision[0].tool_name, "top_processes")

    def test_report_creation(self) -> None:
        state = BrainState("test request", diagnosis="Healthy")
        with tempfile.TemporaryDirectory() as folder, patch("martin_ai.brain.memory.REPORT_DIR", Path(folder)):
            paths = save_report(state)
            self.assertTrue(Path(paths["json"]).exists())
            self.assertTrue(Path(paths["markdown"]).exists())


if __name__ == "__main__":
    unittest.main()
