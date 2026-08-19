from __future__ import annotations

import os
import runpy
import threading
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QProcess, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QPushButton

from martin_ai.actions import ActionManager
from martin_ai.gui_app import ICON_PATH, MartinMainWindow, create_application
from martin_ai.router import route_command


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = create_application(["test_gui"])

    def make_window(self) -> MartinMainWindow:
        with patch.object(MartinMainWindow, "refresh_live_status"), patch.object(
            MartinMainWindow, "start_monitor"
        ):
            window = MartinMainWindow()
        window.poll_timer.stop()
        return window

    def test_gui_module_constructs(self) -> None:
        window = self.make_window()
        self.assertEqual(window.windowTitle(), "Martin AI")
        self.assertEqual(window.pages.count(), 12)
        window.settings["minimize_to_tray"] = False
        window.close()

    def test_official_application_icon_is_loaded(self) -> None:
        window = self.make_window()
        self.assertTrue(ICON_PATH.is_file())
        self.assertFalse(window.windowIcon().isNull())
        self.assertFalse(self.app.windowIcon().isNull())
        window.settings["minimize_to_tray"] = False
        window.close()

    def test_memory_page_displays_database_memories(self) -> None:
        window = self.make_window()
        with patch("martin_ai.gui_app.memory_manager.list_memories", return_value=[
            {"id": 42, "category": "user_preference", "content": "Prefer short answers"}
        ]):
            window.refresh_memories()
        self.assertIn("Prefer short answers", window.memory_list.item(0).text())
        self.assertTrue(window.auto_memory_input.isChecked() in {True, False})
        window.settings["minimize_to_tray"] = False; window.close()

    def test_monitor_stats_update_widgets(self) -> None:
        window = self.make_window()
        game = {
            "game": "R.E.P.O.", "executable": "repo.exe", "elapsed_seconds": 125,
            "cpu_percent": 30.0, "cpu_temperature_c": None, "ram_percent": 60.0,
            "gpu": {"available": True, "utilization_percent": 70.0,
                    "temperature_c": 55.0, "memory_used_mb": 4096.0,
                    "memory_total_mb": 12288.0}, "disks": [],
        }
        window.apply_live_status((game, game))
        self.assertIn("R.E.P.O.", window.top_game.text())
        self.assertEqual(window.metric_gpu.bar.value(), 70)
        self.assertIn("4096", window.metric_vram.value.text())
        window.settings["minimize_to_tray"] = False
        window.close()

    def test_refresh_does_not_start_overlapping_workers(self) -> None:
        window = self.make_window()
        with patch.object(window, "run_background") as run_background:
            window.refresh_live_status()
            window.refresh_live_status()
            self.assertEqual(run_background.call_count, 3)
            self.assertTrue(window.live_refresh_running)
            self.assertTrue(window.ollama_refresh_running)
            self.assertTrue(window.crash_refresh_running)
        window.settings["minimize_to_tray"] = False
        window.close()

    def test_game_command_does_not_use_app_status(self) -> None:
        manager = ActionManager({})
        with patch("martin_ai.router.live_game_status", return_value=None), patch(
            "martin_ai.router.READ_ONLY_TOOLS"
        ) as tools:
            result = route_command("is repo running", manager)
        self.assertEqual(result.message, "No supported game is currently running.")
        tools.__getitem__.assert_not_called()

    def test_authenticated_remote_user_action_is_separate_from_ai_autonomy(self) -> None:
        window = self.make_window()
        device = {"id": "phone", "name": "Phone", "permissions": {"volume_controls": True}}
        with patch("martin_ai.gui_app.execute_remote_control", return_value={"success": True}) as execute:
            result = window.remote_action("volume_up", False, device)
        self.assertTrue(result["success"]); execute.assert_called_once_with("volume_up")
        window.settings["minimize_to_tray"] = False; window.close()

    def test_phone_remote_page_scrolls_and_permissions_reflow(self) -> None:
        window = self.make_window(); window.pages.setCurrentIndex(9); window.show()
        for width, height in ((1200, 800), (1000, 700), (960, 640)):
            with self.subTest(size=(width, height)):
                window.resize(width, height); self.app.processEvents()
                self.assertTrue(window.phone_remote_scroll.widgetResizable())
                self.assertGreater(window.phone_remote_scroll.verticalScrollBar().maximum(), 0)
                checkboxes = list(window.remote_permission_inputs.values())
                self.assertTrue(all(box.height() >= box.minimumHeight() for box in checkboxes))
                positions = [window.remote_permission_panel.grid.getItemPosition(
                    window.remote_permission_panel.grid.indexOf(box))[:2] for box in checkboxes]
                self.assertEqual(len(positions), len(set(positions)))
        window.remote_permission_panel._reflow(1)
        self.assertEqual(window.remote_permission_panel.columns, 1)
        window.remote_permission_panel._reflow(2)
        self.assertEqual(window.remote_permission_panel.columns, 2)
        window.settings["minimize_to_tray"] = False; window.close()

    def test_phone_permission_controls_remain_functional_in_scroll_layout(self) -> None:
        window = self.make_window(); window.pages.setCurrentIndex(9); window.show(); window.resize(1000, 700)
        device = {"id": "phone-1", "name": "Phone", "paired_at": "now", "last_connected": "now",
                  "permissions": {key: False for key in window.remote_permission_inputs}, "revoked": False}
        window.phone_remote.devices = [device]; window.refresh_phone_remote_page(); window.remote_devices.setCurrentRow(0)
        self.app.processEvents(); window.remote_permission_inputs["remote_chat"].setChecked(True)
        with patch.object(window.phone_remote, "update_permissions", return_value=True) as update:
            window.save_selected_phone_permissions()
        self.assertTrue(update.call_args.args[1]["remote_chat"])
        window.settings["minimize_to_tray"] = False; window.close()

    def test_gui_approval_and_denial_use_action_manager(self) -> None:
        calls: list[str] = []
        manager = ActionManager({"protected": lambda: calls.append("ran") or {"success": True}})
        manager.queue("protected", {})
        self.assertTrue(manager.approve()["success"])
        manager.queue("protected", {})
        self.assertTrue(manager.deny()["success"])
        self.assertEqual(calls, ["ran"])

    def test_close_stops_owned_monitor_and_workers(self) -> None:
        window = self.make_window()
        process = Mock()
        process.state.return_value = QProcess.ProcessState.Running
        process.waitForFinished.return_value = True
        window.monitor_process = process
        window.settings["minimize_to_tray"] = False
        with patch("martin_ai.gui_app.unload_model"):
            window.close()
        process.terminate.assert_called_once()
        self.assertFalse(window.poll_timer.isActive())

    def test_terminal_entry_still_imports(self) -> None:
        import main
        from martin_ai import app
        self.assertIs(main.run, app.run)

    def test_brain_signal_updates_investigation_page(self) -> None:
        from martin_ai.brain import BrainState, PlanStep
        window = self.make_window()
        state = BrainState("Why is RAM high?", [PlanStep("1", "Check RAM", "system_summary")])
        window.apply_brain_update("step_started", state, state.active_plan[0])
        self.assertIn("Check RAM", window.brain_status.text())
        self.assertIn("All Good", window.brain_output.toPlainText())
        self.assertNotIn("system_summary", window.brain_output.toPlainText())
        window.settings["minimize_to_tray"] = False
        window.close()

    def test_security_page_signal_handling(self) -> None:
        window = self.make_window()
        window.apply_security_status({"status": "Needs attention", "defender": {"real_time_protection_enabled": True},
                                      "firewall": {"Domain": True, "Private": True, "Public": True},
                                      "findings": [{"label": "uncertain", "evidence": ["Unsigned"]}]})
        self.assertIn("Needs attention", window.security_overall.text())
        self.assertIn("uncertain", window.security_output.toPlainText())
        window.security_action_finished({"success": True})
        self.assertEqual(window.security_scan_progress.value(), 100)
        window.settings["minimize_to_tray"] = False
        window.close()

    def test_notifications_are_added_and_deduplicated(self) -> None:
        window = self.make_window()
        with patch("martin_ai.gui_app.QSystemTrayIcon.isSystemTrayAvailable", return_value=False):
            self.assertTrue(window.add_notification("Security", "Review finding", "Medium", "same-alert"))
            self.assertFalse(window.add_notification("Security", "Review finding", "Medium", "same-alert"))
        self.assertEqual(window.notification_list.count(), 1)
        self.assertIn("Review finding", window.notification_list.item(0).text())
        window.settings["minimize_to_tray"] = False
        window.close()

    def test_chat_renders_bold_markdown_without_literal_asterisks(self) -> None:
        window = self.make_window()
        window.append_message("Martin AI", "**RAM Status**\nEverything looks normal.", False)
        rendered = window.conversation.toHtml()
        self.assertIn("RAM Status", rendered)
        self.assertNotIn("**RAM Status**", window.conversation.toPlainText())
        window.settings["minimize_to_tray"] = False
        window.close()

    def test_general_chat_failure_is_not_reported_as_pc_problem(self) -> None:
        window = self.make_window()
        window.history.append({"role": "user", "content": "general question"})
        window.conversation_failed("HTTP 500")
        text = window.conversation.toPlainText()
        self.assertIn("Local AI couldn't answer", text)
        self.assertNotIn("Problem Found", text)
        window.settings["minimize_to_tray"] = False
        window.close()

    def test_stop_generation_cancels_http_without_unloading_ollama(self) -> None:
        window = self.make_window()
        window.chat_thread = Mock()
        window.chat_cancel_event = threading.Event()
        with patch("martin_ai.gui_app.ollama_manager.cancel_generation") as cancel, \
             patch("martin_ai.gui_app.unload_model") as unload:
            window.stop_generation()
        self.assertTrue(window.chat_cancel_event.is_set())
        cancel.assert_called_once_with()
        unload.assert_not_called()
        window.chat_thread.requestInterruption.assert_called_once_with()
        window.chat_thread = None
        window.settings["minimize_to_tray"] = False
        window.close()

    def test_enter_sends_once_clears_and_keeps_focus(self) -> None:
        window = self.make_window(); window.show(); self.app.processEvents()
        window.chat_input.setFocus(); window.chat_input.setPlainText("hello")
        QTest.keyClick(window.chat_input, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(window.chat_input.toPlainText(), "")
        self.assertEqual(window.conversation.toPlainText().count("hello"), 1)
        self.assertTrue(window.chat_input.hasFocus())
        window.settings["minimize_to_tray"] = False; window.close()

    def test_shift_enter_inserts_newline_without_sending(self) -> None:
        window = self.make_window(); window.show(); self.app.processEvents()
        window.chat_input.setPlainText("hello")
        cursor = window.chat_input.textCursor(); cursor.movePosition(cursor.MoveOperation.End)
        window.chat_input.setTextCursor(cursor)
        QTest.keyClick(window.chat_input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        QTest.keyClicks(window.chat_input, "Martin AI")
        self.assertEqual(window.chat_input.toPlainText(), "hello\nMartin AI")
        self.assertEqual(window.conversation.toPlainText(), "")
        window.settings["minimize_to_tray"] = False; window.close()

    def test_empty_enter_does_nothing(self) -> None:
        window = self.make_window(); window.chat_input.setPlainText(" \n ")
        QTest.keyClick(window.chat_input, Qt.Key.Key_Return)
        self.assertEqual(window.conversation.toPlainText(), "")
        self.assertEqual(window.chat_input.toPlainText(), " \n ")
        window.settings["minimize_to_tray"] = False; window.close()

    def test_enter_during_generation_does_not_duplicate(self) -> None:
        window = self.make_window(); window.chat_thread = Mock(); window.chat_input.setPlainText("hello")
        QTest.keyClick(window.chat_input, Qt.Key.Key_Return)
        self.assertEqual(window.conversation.toPlainText(), "")
        self.assertEqual(window.chat_input.toPlainText(), "hello")
        window.chat_thread = None; window.settings["minimize_to_tray"] = False; window.close()

    def test_send_button_uses_existing_send_message(self) -> None:
        window = self.make_window(); window.chat_input.setPlainText("hello")
        send = next(button for button in window.findChildren(QPushButton) if button.text() == "Send")
        QTest.mouseClick(send, Qt.MouseButton.LeftButton)
        self.assertEqual(window.chat_input.toPlainText(), "")
        self.assertEqual(window.conversation.toPlainText().count("hello"), 1)
        window.settings["minimize_to_tray"] = False; window.close()

    def test_enter_routes_general_question_to_existing_conversation_path(self) -> None:
        window = self.make_window(); window.chat_input.setPlainText("what is the capital of japan")
        with patch.object(window, "start_conversation") as start:
            QTest.keyClick(window.chat_input, Qt.Key.Key_Return)
        start.assert_called_once_with("what is the capital of japan")
        self.assertEqual(window.chat_input.toPlainText(), "")
        window.settings["minimize_to_tray"] = False; window.close()

    def test_terminal_entry_invokes_existing_run_function(self) -> None:
        with patch("martin_ai.app.run") as terminal_run:
            runpy.run_path("main.py", run_name="__main__")
        terminal_run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
