from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import psutil

from martin_ai.game_optimizer import GameOptimizer
from martin_ai.actions import ActionManager
from martin_ai.router import route_command


class Runtime:
    autonomy_enabled = True
    simulation_mode = False
    emergency_stopped = False


class FakeProcess:
    def __init__(self, pid, priorities): self.pid, self.priorities = pid, priorities
    def nice(self, value=None):
        if value is None: return self.priorities.get(self.pid, 32)
        self.priorities[self.pid] = value


class GameOptimizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.priorities = {10: 32}; self.now = [1000.0]
        self.optimizer = GameOptimizer(runtime=Runtime(), state_file=Path(self.temp.name) / "session.json",
            process_factory=lambda pid: FakeProcess(pid, self.priorities), process_iter=lambda _attrs: [],
            clock=lambda: self.now[0], settings={"optimization_level": "Balanced"})
        self.game = {"game": "Test Game", "executable": "test.exe", "pid": 10, "pids": [10], "launcher": "Steam"}
        self.sample = {"cpu_percent": 45, "ram_percent": 60, "gpu": {"available": True, "utilization_percent": 80, "temperature_c": 65}}

    @patch.object(GameOptimizer, "_power_scheme", return_value=None)
    def test_starts_once_and_restores_priority(self, _power):
        status = self.optimizer.start_game_session(self.game, self.sample)
        changed = self.priorities[10]
        self.assertTrue(status["active"]); self.assertNotEqual(changed, 32)
        self.optimizer.start_game_session(self.game, self.sample)
        self.assertEqual(len([row for row in self.optimizer.session["ledger"] if row["action"] == "Process Priority"]), 1)
        restored = self.optimizer.restore_session_changes()
        self.assertEqual(self.priorities[10], 32); self.assertFalse(restored["active"])
        self.assertFalse(self.optimizer.state_file.exists())

    @patch.object(GameOptimizer, "_power_scheme", return_value=None)
    def test_simulation_mode_only_monitors(self, _power):
        runtime = Runtime(); runtime.simulation_mode = True
        optimizer = GameOptimizer(runtime=runtime, state_file=Path(self.temp.name) / "sim.json",
            process_factory=lambda pid: FakeProcess(pid, self.priorities), process_iter=lambda _attrs: [], settings={})
        status = optimizer.start_game_session(self.game, self.sample)
        self.assertEqual(self.priorities[10], 32)
        self.assertEqual(status["verified_changes"], [])
        self.assertIn("prevents system changes", status["failed_changes"][0]["error"])

    def test_adaptive_bottlenecks_and_temperature(self):
        self.assertEqual(self.optimizer._bottleneck(self.game, {"cpu_percent": 95, "ram_percent": 50, "gpu": {"utilization_percent": 50}}), "CPU")
        self.assertEqual(self.optimizer._bottleneck(self.game, {"cpu_percent": 20, "ram_percent": 95, "gpu": {}}), "RAM")
        self.assertEqual(self.optimizer._bottleneck(self.game, {"cpu_percent": 20, "ram_percent": 50, "gpu": {"utilization_percent": 99}}), "GPU")
        self.assertEqual(self.optimizer._bottleneck(self.game, {"cpu_percent": 20, "ram_percent": 50, "gpu": {"temperature_c": 90}}), "High temperature")

    def test_protects_launchers_anticheat_and_discord(self):
        self.optimizer.session = {"baseline": {"launcher": "Steam"}}
        for name in ("steam.exe", "EasyAntiCheat.exe", "Discord.exe", "audiodg.exe"):
            self.assertTrue(self.optimizer._protected_for_game(name))

    def test_recovers_interrupted_session(self):
        state = Path(self.temp.name) / "recovery.json"
        state.write_text('{"active":true,"game":"Test","executable":"test.exe","pid":10,"started_at":1,"baseline":{"power_scheme":null},"priority_changes":[{"pid":10,"original":32}],"power_changed":false,"applied":[],"errors":[]}', encoding="utf-8")
        self.priorities[10] = getattr(psutil, "ABOVE_NORMAL_PRIORITY_CLASS", -7)
        recovered = GameOptimizer(runtime=Runtime(), state_file=state,
            process_factory=lambda pid: FakeProcess(pid, self.priorities), process_iter=lambda _attrs: [])
        self.assertEqual(self.priorities[10], 32); self.assertFalse(state.exists()); self.assertIsNone(recovered.session)

    @patch.object(GameOptimizer, "_power_scheme", return_value=None)
    def test_local_command_with_punctuation_reports_only_verified_ledger(self, _power):
        import martin_ai.game_optimizer as module
        self.optimizer.start_game_session(self.game, self.sample)
        old = module._OPTIMIZER; module._OPTIMIZER = self.optimizer
        self.addCleanup(setattr, module, "_OPTIMIZER", old)
        result = route_command("what did you optimize?", ActionManager({}))
        self.assertTrue(result.handled); self.assertEqual(result.intent, "game_optimization")
        self.assertIn("Process Priority", result.message)
        self.assertNotIn("Power Profile", result.message)
        self.assertNotIn("visual settings", result.message.lower())

    @patch.object(GameOptimizer, "_power_scheme", return_value=None)
    def test_status_uses_measured_telemetry_and_counts_failures(self, _power):
        import martin_ai.game_optimizer as module
        self.optimizer.start_game_session(self.game, self.sample)
        old = module._OPTIMIZER; module._OPTIMIZER = self.optimizer
        self.addCleanup(setattr, module, "_OPTIMIZER", old)
        result = route_command("optimization status!", ActionManager({}))
        self.assertIn("CPU: 45%", result.message)
        self.assertIn("Verified Changes: 2", result.message)
        self.assertIn("Failed Changes: 1", result.message)

    @patch.object(GameOptimizer, "_power_scheme", return_value=None)
    def test_unverified_priority_is_failed_and_never_reported_as_applied(self, _power):
        class RefusesPriority(FakeProcess):
            def nice(self, value=None):
                if value is None: return 32
                return None
        optimizer = GameOptimizer(runtime=Runtime(), state_file=Path(self.temp.name) / "refused.json",
            process_factory=lambda pid: RefusesPriority(pid, self.priorities), process_iter=lambda _attrs: [],
            settings={"ai_resource_mode": False})
        status = optimizer.start_game_session(self.game, self.sample)
        self.assertEqual(status["verified_changes"], [])
        priority = next(row for row in status["failed_changes"] if row["action"] == "Process Priority")
        self.assertEqual(priority["actual_value"], "Normal")
        self.assertIn("did not retain", priority["error"])


if __name__ == "__main__": unittest.main()
