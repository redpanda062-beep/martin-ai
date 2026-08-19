from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from martin_ai.core import PermissionManager, RegisteredTool, RuntimeControl, ToolRegistry
from martin_ai.memory import Database, MemoryManager
from martin_ai.models import ActionRequest


class AgentSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = MemoryManager(Database(Path(self.temp.name) / "test.db"))

    def tearDown(self) -> None:
        self.memory.db.close()
        self.temp.cleanup()

    def registry(self, level: int = 1, simulation: bool = False) -> ToolRegistry:
        return ToolRegistry(PermissionManager(level), RuntimeControl(simulation_mode=simulation), self.memory)

    def test_level_one_cannot_modify_system(self) -> None:
        calls = []
        registry = self.registry(1)
        registry.register(RegisteredTool("change", "change", "low", 2,
                                         lambda: calls.append(1) or {"success": True}, changes_system=True))
        result = registry.execute(ActionRequest("change"))
        self.assertFalse(result["success"]); self.assertEqual(calls, [])

    def test_level_two_cannot_perform_admin_action(self) -> None:
        registry = self.registry(2)
        registry.register(RegisteredTool("admin", "admin", "critical", 4, lambda: {"success": True}, changes_system=True))
        self.assertTrue(registry.execute(ActionRequest("admin"))["pending"])

    def test_levels_three_and_four_always_request_permission(self) -> None:
        for level in (3, 4):
            registry = self.registry(4)
            registry.register(RegisteredTool(f"tool{level}", "x", "high", level, lambda: {"success": True}, changes_system=True))
            self.assertTrue(registry.execute(ActionRequest(f"tool{level}"))["pending"])

    def test_unknown_and_arbitrary_shell_tools_are_rejected(self) -> None:
        registry = self.registry(4)
        self.assertTrue(registry.execute(ActionRequest("powershell", {"command": "whoami"}))["rejected"])
        registry.register(RegisteredTool("diagnose", "x", "read_only", 1, lambda **kwargs: {"success": True}))
        self.assertTrue(registry.execute(ActionRequest("diagnose", {"command": "whoami"}))["rejected"])

    def test_dangerous_file_deletion_and_defender_disable_are_rejected(self) -> None:
        registry = self.registry(4)
        for name in ("format_drive", "disable_defender", "delete_system_files"):
            self.assertTrue(registry.execute(ActionRequest(name))["rejected"])

    def test_permission_cannot_self_escalate(self) -> None:
        permissions = PermissionManager(1)
        self.assertFalse(permissions.set_level(4)); self.assertEqual(int(permissions.current_level), 1)

    def test_simulation_mode_performs_no_change(self) -> None:
        calls = []
        registry = self.registry(2, simulation=True)
        registry.register(RegisteredTool("safe", "safe", "low", 2,
                                         lambda: calls.append(1) or {"success": True}, changes_system=True))
        result = registry.execute(ActionRequest("safe", expected_result="done"))
        self.assertTrue(result["simulated"]); self.assertEqual(calls, [])

    def test_emergency_stop_blocks_action(self) -> None:
        runtime = RuntimeControl(simulation_mode=False); runtime.stop()
        registry = ToolRegistry(PermissionManager(2), runtime, self.memory)
        registry.register(RegisteredTool("safe", "safe", "low", 2, lambda: {"success": True}, changes_system=True))
        self.assertTrue(registry.execute(ActionRequest("safe"))["blocked"])

    def test_action_rate_limit(self) -> None:
        registry = ToolRegistry(PermissionManager(2), RuntimeControl(simulation_mode=False), self.memory,
                                maximum_actions_per_hour=1)
        registry.register(RegisteredTool("safe", "safe", "low", 2, lambda: {"success": True}, changes_system=True))
        self.assertTrue(registry.execute(ActionRequest("safe"))["success"])
        self.assertIn("rate limit", registry.execute(ActionRequest("safe"))["error"].lower())

    def test_rollback_data_is_persisted(self) -> None:
        registry = self.registry(2)
        registry.register(RegisteredTool("safe", "safe", "low", 2,
                                         lambda: {"success": True, "rollback": {"available": True, "id": "x"}},
                                         changes_system=True, reversible=True))
        self.assertTrue(registry.execute(ActionRequest("safe"))["success"])
        row = self.memory.db.connection().execute("SELECT rollback_json FROM actions").fetchone()
        self.assertIn('"available": true', row[0])


if __name__ == "__main__":
    unittest.main()
