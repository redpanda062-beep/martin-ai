from __future__ import annotations

import unittest
from unittest.mock import patch

from martin_ai.actions import ActionManager
from martin_ai.app import TOOLS
from martin_ai.router import detect_ollama_action, route_command
from martin_ai.game_monitor import (
    close_safe_background_apps,
    diagnose_session,
    graphics_crash_detected,
)
from martin_ai.tools import READ_ONLY_TOOLS, REPAIR_TOOLS


class ToolRegistrationTests(unittest.TestCase):
    def test_every_tool_is_exposed_to_ollama(self) -> None:
        schemas = {item["function"]["name"] for item in TOOLS}
        self.assertEqual(schemas, set(READ_ONLY_TOOLS) | set(REPAIR_TOOLS))


class ActionManagerTests(unittest.TestCase):
    def test_protected_action_requires_approval(self) -> None:
        calls: list[str] = []
        manager = ActionManager({"repair": lambda: calls.append("ran") or {"success": True}})
        self.assertTrue(manager.queue("repair", {})["pending"])
        self.assertEqual(calls, [])
        self.assertTrue(manager.approve()["success"])
        self.assertEqual(calls, ["ran"])

    def test_denied_action_does_not_run(self) -> None:
        calls: list[str] = []
        manager = ActionManager({"repair": lambda: calls.append("ran") or {"success": True}})
        manager.queue("repair", {})
        self.assertTrue(manager.deny()["success"])
        self.assertEqual(calls, [])


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ActionManager(REPAIR_TOOLS)

    def test_close_app_is_queued(self) -> None:
        result = route_command("close notepad", self.manager)
        self.assertTrue(result.handled)
        self.assertTrue(self.manager.has_pending())

    def test_open_app_accepts_natural_trailing_punctuation(self) -> None:
        with patch("martin_ai.router.READ_ONLY_TOOLS") as tools:
            tools.__getitem__.return_value.return_value = {"success": True, "message": "Opening notepad."}
            result = route_command("Open Notepad.", self.manager)
        self.assertTrue(result.handled)
        tools.__getitem__.return_value.assert_called_once_with(app_name="notepad")

    @patch("martin_ai.router.READ_ONLY_TOOLS")
    def test_screenshot_command(self, tools) -> None:
        tools.__getitem__.return_value.return_value = {
            "success": True,
            "message": "Screenshot saved.",
        }
        result = route_command("take a screenshot", self.manager)
        self.assertTrue(result.handled)
        self.assertEqual(result.panel_title, "Screenshot")

    def test_ollama_action_variations_are_routed_locally(self) -> None:
        expected = {
            "reconnect to ollama": "reconnect", "restart the ai backend": "restart",
            "start ollama": "start", "stop ollama": "stop", "check ollama": "status",
            "ollama status": "status", "is ollama working": "status",
            "fix ollama": "repair", "repair ollama": "repair", "reconnect model": "reconnect",
        }
        for command, action in expected.items():
            with self.subTest(command=command):
                result = route_command(command, self.manager)
                self.assertTrue(result.handled)
                self.assertEqual(result.intent, "ollama_action")
                self.assertEqual(result.raw_result["operation"], action)

    def test_ollama_discussion_is_not_a_local_action(self) -> None:
        self.assertIsNone(detect_ollama_action("How does Ollama work?"))
        self.assertFalse(route_command("How does Ollama work?", self.manager).handled)

    def test_manual_game_queries_use_local_detector(self) -> None:
        status = {"game": "Fortnite", "executable": "fortnite.exe", "elapsed_seconds": 5,
                  "cpu_percent": 1, "ram_percent": 2, "gpu": {"available": False}}
        with patch("martin_ai.router.live_game_status", return_value=status):
            self.assertEqual(route_command("what game am I playing", self.manager).message, "You're playing Fortnite.")
            self.assertIn("Game detected", route_command("detect game", self.manager).message)

    def test_rescan_and_debug_commands_are_local(self) -> None:
        with patch("martin_ai.router.rescan_game_library", return_value={"success": True, "manifest_entries": 3, "game": None}), \
             patch("martin_ai.router.game_detection_debug", return_value={"candidates": [], "active": None}):
            self.assertEqual(route_command("rescan games", self.manager).intent, "game_rescan")
            self.assertEqual(route_command("game detection debug", self.manager).intent, "game_debug")

    def test_remote_desktop_status_is_local(self) -> None:
        service = unittest.mock.Mock()
        service.remote_desktop_status.return_value = {"active": True, "device": "Phone", "mode": "CONTROL", "monitor": 1,
            "stream_fps": 12.0, "capture_latency_ms": 20.0, "quality": "Balanced", "remote_input": True}
        with patch("martin_ai.router.phone_remote_service", return_value=service):
            result = route_command("remote desktop status?", self.manager)
        self.assertEqual(result.intent, "remote_desktop"); self.assertIn("Stream FPS: 12.0", result.message)


class GameMonitorTests(unittest.TestCase):
    @patch("martin_ai.game_monitor.visible_window_pids", return_value={101})
    @patch("martin_ai.game_monitor.psutil.process_iter")
    def test_does_not_close_app_with_visible_window(self, process_iter, _visible) -> None:
        visible = unittest.mock.Mock(pid=101)
        visible.info = {"pid": 101, "name": "chrome.exe"}
        process_iter.return_value = [visible]
        self.assertEqual(close_safe_background_apps(), [])
        visible.terminate.assert_not_called()

    def test_healthy_session_has_no_corrective_action(self) -> None:
        result = diagnose_session({"warnings": [], "windows_error_events": []})
        self.assertEqual(result["status"], "healthy")

    def test_crash_event_is_flagged(self) -> None:
        result = diagnose_session({
            "duration_minutes": 0.5,
            "warnings": [],
            "windows_error_events": [{"Id": 1000}],
        })
        self.assertEqual(result["status"], "issues_detected")
        self.assertTrue(any("crashed" in item for item in result["findings"]))

    def test_graphics_repair_requires_specific_crash_evidence(self) -> None:
        self.assertFalse(graphics_crash_detected([
            {"ProviderName": "Application Error", "Message": "game.exe stopped"}
        ]))
        self.assertTrue(graphics_crash_detected([
            {"ProviderName": "Application Error", "Message": "DXGI_ERROR_DEVICE_REMOVED"}
        ]))


if __name__ == "__main__":
    unittest.main()
