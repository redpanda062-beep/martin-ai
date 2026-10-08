from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from martin_ai.core.runtime import RuntimeControl
from martin_ai.memory import Database, MemoryManager
from martin_ai.self_repair import RepairDefinition, RepairRegistry, SelfRepairEngine, build_registry


class SelfRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.memory = MemoryManager(Database(Path(self.temp.name) / "test.db"))
        self.runtime = RuntimeControl(simulation_mode=False, autonomy_enabled=True)

    def tearDown(self):
        self.memory.db.close()
        self.temp.cleanup()

    def engine_with(self, definition, *, auto=True):
        registry = RepairRegistry()
        registry.register(definition)
        return SelfRepairEngine(registry, self.runtime, self.memory, enabled=True, auto_level_1=auto)

    def test_registry_has_controlled_initial_capabilities(self):
        catalog = {item["repair_id"]: item for item in build_registry().catalog()}
        self.assertIn("ollama.reconnect", catalog)
        self.assertIn("network.flush_dns", catalog)
        self.assertIn("application.restart", catalog)
        self.assertTrue(catalog["network.restart_adapter"]["permission_required"])

    def test_ollama_reconnect_verifies_api_and_generation(self):
        class FakeOllama:
            config = SimpleNamespace(base_url="http://127.0.0.1:11434", model="qwen3:8b")
            def reconnect(self):
                return SimpleNamespace(status="connected", selected_model="qwen3:8b", models=["qwen3:8b"], message="connected")
            def restart_ollama(self):
                return self.reconnect()
            def check(self, *, auto_start, force):
                return SimpleNamespace(status="connected", selected_model="qwen3:8b", models=["qwen3:8b"], message="connected")
            def chat(self, messages, *, base_url, model, keep_alive):
                return "OK"
        definition = build_registry().get("ollama.reconnect")
        registry = RepairRegistry(); registry.register(definition)
        engine = SelfRepairEngine(registry, self.runtime, self.memory, enabled=True, auto_level_1=True)
        result = engine.prepare("ollama.reconnect", "simulated unavailable Ollama",
                                {"ollama_manager": FakeOllama()}, autonomous=True)
        self.assertTrue(result["success"])
        self.assertTrue(result["verification"]["api"])
        self.assertTrue(result["verification"]["generation"])

    def test_level_two_rejection_makes_zero_change(self):
        calls = []
        repair = RepairDefinition("test.level2", "Test", "Test", ("test",), ("check",), 2, True,
                                  lambda _: calls.append("changed") or {"success": True},
                                  lambda _: {"success": True}, 5)
        engine = self.engine_with(repair)
        queued = engine.prepare("test.level2", "simulated system problem")
        self.assertTrue(queued["pending"])
        engine.deny()
        self.assertEqual(calls, [])

    def test_level_three_always_requires_explicit_confirmation(self):
        calls = []
        repair = RepairDefinition("test.level3", "High risk test", "Test only", ("test",), ("check",), 3, True,
                                  lambda _: calls.append("changed") or {"success": True},
                                  lambda _: {"success": True}, 5)
        engine = self.engine_with(repair, auto=True)
        result = engine.prepare("test.level3", "simulated high risk problem", autonomous=True)
        self.assertTrue(result["pending"])
        self.assertEqual(calls, [])

    def test_verification_failure_never_claims_fixed(self):
        repair = RepairDefinition("test.verify", "No-op repair", "Test", ("test",), ("check",), 1, False,
                                  lambda _: {"success": True}, lambda _: {"success": False}, 5)
        result = self.engine_with(repair).prepare("test.verify", "persistent issue")
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "verification_failed")
        self.assertEqual(result["message"], "The repair completed, but the problem is still present.")

    def test_simulated_verification_never_claims_fixed(self):
        repair = RepairDefinition("test.simulated_verify", "Test", "Test", ("test",), ("check",), 1, False,
                                  lambda _: {"success": True}, lambda _: {"success": True, "simulated": True}, 5)
        result = self.engine_with(repair).prepare("test.simulated_verify", "persistent issue")
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "verification_failed")

    def test_stop_ai_prevents_new_repairs(self):
        calls = []
        repair = RepairDefinition("test.stop", "Test", "Test", ("test",), ("check",), 1, False,
                                  lambda _: calls.append(1) or {"success": True}, lambda _: {"success": True}, 5)
        engine = self.engine_with(repair)
        self.runtime.stop()
        result = engine.prepare("test.stop", "test")
        self.assertTrue(result["blocked"])
        self.assertEqual(calls, [])

    def test_autonomy_off_prevents_background_repair(self):
        repair = RepairDefinition("test.auto", "Test", "Test", ("test",), ("check",), 1, False,
                                  lambda _: {"success": True}, lambda _: {"success": True}, 5)
        engine = self.engine_with(repair)
        self.runtime.set_autonomy(False)
        self.assertTrue(engine.prepare("test.auto", "test", autonomous=True)["blocked"])

    def test_storage_repair_scope_is_martin_temp_only(self):
        definition = build_registry().get("storage.clear_martin_temp")
        self.assertIsNotNone(definition)
        self.assertIn("Martin AI", definition.description)
        self.assertNotIn("Downloads", definition.description)

    def test_history_is_compact_and_persistent(self):
        repair = RepairDefinition("test.history", "Test", "Test", ("test",), ("check",), 1, False,
                                  lambda _: {"success": True, "detail": "small"}, lambda _: {"success": True}, 5)
        self.engine_with(repair).prepare("test.history", "history issue")
        history = self.memory.repair_history()
        self.assertEqual(history[0]["status"], "verified")
        self.assertTrue(history[0]["verification"]["success"])


if __name__ == "__main__":
    unittest.main()
