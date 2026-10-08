from __future__ import annotations

import unittest
from unittest.mock import patch

from martin_ai.actions import ActionManager
from martin_ai.response_formatter import (
    SIMPLE_MODE,
    format_response,
    technical_details,
    wants_technical_details,
)
from martin_ai.router import route_command
from martin_ai.intent import Intent, detect_intent
from martin_ai.alert_manager import AlertManager
from martin_ai.brain import BrainState


class ResponseFormatterTests(unittest.TestCase):
    def test_measured_assessment_is_shown_without_replacing_it_with_storage_warning(self) -> None:
        from martin_ai.brain.evaluator import Evaluator
        state = BrainState("why is my pc slow", status="completed", evidence=[
            {"tool": "system_summary", "result": {"ram_percent_used": 71.9,
                "disks": [{"device": "C:", "free_gb": 19.4}]}},
        ])
        Evaluator({}).finalize(state)
        text = format_response(state)
        for heading in ("Confirmed Findings", "Possible Causes", "Normal Readings"):
            self.assertIn(heading, text)
        self.assertIn("71.9%", text)
        self.assertNotIn("near", text)
        self.assertNotIn("likely cause", text)

    def test_completed_assessment_is_not_labeled_all_good(self) -> None:
        state = BrainState("why is my pc slow", status="completed",
                           diagnosis="Recent warnings may need investigation.",
                           evidence=[{"tool": "system_summary", "result": {"ram_percent_used": 72.9}}])
        text = format_response(state)
        self.assertIn("Investigation Complete", text)
        self.assertNotIn("All Good", text)

    def test_inconclusive_assessment_does_not_claim_healthy(self) -> None:
        state = BrainState("why is my pc slow", status="completed", diagnosis="The investigation was inconclusive.")
        self.assertIn("Inconclusive", format_response(state))
        self.assertNotIn("All Good", format_response(state))

    def test_required_intents_are_detected_before_tools(self) -> None:
        cases = {
            "hey ai": Intent.GREETING,
            "hows my ram": Intent.RAM_STATUS,
            "hows my cpu": Intent.CPU_STATUS,
            "check my storage": Intent.STORAGE_STATUS,
            "check my pc health": Intent.PC_HEALTH,
            "what are you doing?": Intent.GENERAL_CHAT,
            "whats the capital of japan": Intent.GENERAL_CHAT,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(detect_intent(text), expected)

    def test_focused_routes_execute_only_relevant_tool(self) -> None:
        calls: list[str] = []
        tools = {
            "ram_status": lambda: calls.append("ram") or {"success": True, "used_gb": 18, "total_gb": 32, "available_gb": 14, "percent_used": 56},
            "cpu_status": lambda: calls.append("cpu") or {"success": True, "cpu_percent": 12},
            "storage_status": lambda: calls.append("storage") or {"success": True, "disks": [{"device": "C:\\", "free_gb": 4, "percent_used": 98}]},
        }
        with patch.dict("martin_ai.router.READ_ONLY_TOOLS", tools, clear=False):
            ram = route_command("hows my ram", ActionManager({}))
            cpu = route_command("hows my cpu", ActionManager({}))
            storage = route_command("check my storage", ActionManager({}))
        self.assertEqual(calls, ["ram", "cpu", "storage"])
        self.assertIn("18 GB", ram.message)
        self.assertNotIn("Storage", ram.message)
        self.assertIn("12%", cpu.message)
        self.assertNotIn("Storage", cpu.message)
        self.assertIn("Storage Warning", storage.message)

    def test_greeting_and_activity_status_run_no_tools(self) -> None:
        with patch("martin_ai.router.READ_ONLY_TOOLS") as tools:
            greeting = route_command("hey ai", ActionManager({}))
            activity = route_command("what are you doing?", ActionManager({}))
        self.assertEqual(greeting.message, "Hey! What's up?")
        self.assertIn("monitoring quietly", activity.message)
        self.assertNotIn("Storage", greeting.message + activity.message)
        tools.__getitem__.assert_not_called()

    def test_alert_manager_deduplicates_until_significantly_worse(self) -> None:
        manager = AlertManager()
        sample = {"disks": [{"device": "C:\\", "free_gb": 8.0}]}
        self.assertEqual(len(manager.update_system_sample(sample)), 1)
        self.assertEqual(manager.update_system_sample(sample), [])
        self.assertEqual(manager.update_system_sample({"disks": [{"device": "C:\\", "free_gb": 7.0}]}), [])
        self.assertEqual(len(manager.update_system_sample({"disks": [{"device": "C:\\", "free_gb": 5.5}]})), 1)
    def test_simple_mode_is_enabled(self) -> None:
        self.assertTrue(SIMPLE_MODE)

    def test_healthy_system_is_short_and_hides_raw_data(self) -> None:
        raw = {
            "success": True,
            "computer_name": "PRIVATE-PC",
            "ram_percent_used": 42,
            "disks": [{"device": "C:\\", "free_gb": 120, "percent_used": 50}],
        }
        text = format_response(raw, {"tool": "system_summary"})
        self.assertIn("All Good", text)
        self.assertNotIn("PRIVATE-PC", text)
        self.assertNotIn("computer_name", text)
        self.assertNotIn("{", text)

    def test_low_disk_has_plain_warning_and_action(self) -> None:
        raw = {"success": True, "ram_percent_used": 50,
               "disks": [{"device": "C:\\", "free_gb": 4, "percent_used": 98}]}
        text = format_response(raw, {"tool": "system_summary"})
        self.assertIn("Storage Warning", text)
        self.assertIn("4 GB free", text)
        self.assertNotIn("percent_used", text)

    def test_json_text_is_not_echoed_in_simple_mode(self) -> None:
        text = format_response('{"success": true, "secret": "internal"}')
        self.assertNotIn("secret", text)
        self.assertNotIn("{", text)

    def test_technical_details_requires_explicit_command(self) -> None:
        raw = {"success": True, "private": [1, 2]}
        self.assertTrue(wants_technical_details("show technical details"))
        self.assertIn('"private"', technical_details(raw))

    def test_system_diagnostic_route_never_returns_raw_result_as_message(self) -> None:
        raw = {"success": True, "computer_name": "PRIVATE-PC", "ram_percent_used": 30,
               "disks": [{"device": "C:\\", "free_gb": 100, "percent_used": 40}]}
        with patch.dict("martin_ai.router.READ_ONLY_TOOLS", {"system_summary": lambda: raw}):
            result = route_command("check my system", ActionManager({}))
        self.assertTrue(result.handled)
        self.assertIs(result.raw_result, raw)
        self.assertIn("All Good", result.message)
        self.assertNotIn("PRIVATE-PC", result.message)


if __name__ == "__main__":
    unittest.main()
