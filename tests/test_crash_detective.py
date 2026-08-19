from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from martin_ai.crash_detective import CrashDetective, classify_crash, correlate_events
from martin_ai.memory import MemoryManager
from martin_ai.memory.database import Database
from martin_ai.skills.builtin import BUILTIN_SKILLS
from martin_ai.skills import SkillManager


NOW = datetime(2026, 8, 18, 4, 0, tzinfo=timezone.utc)


def event(seconds=0, event_id=1000, message="Faulting application name: game.exe", provider="Application Error"):
    return {"timestamp": (NOW + timedelta(seconds=seconds)).isoformat(), "event_id": event_id,
            "message": message, "provider": provider}


class CrashDetectiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.memory = MemoryManager(Database(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.memory.db.close(); self.temp.cleanup()

    def detective(self, events=(), enabled=True):
        return CrashDetective(self.memory, event_provider=lambda **kwargs: list(events), enabled=enabled)

    def test_normal_exit_creates_no_incident(self):
        detective = self.detective([]); detective.poll()
        self.assertEqual(detective.history(), [])

    def test_application_crash_is_saved(self):
        result = self.detective().investigate("Game", "game.exe", timestamp=NOW, events=[event()])
        self.assertTrue(result["success"]); self.assertEqual(len(self.memory.incident_history()), 1)

    def test_gpu_crash_is_high_confidence(self):
        events = [event(-2, 4101, "Display driver nvlddmkm stopped responding and recovered", "Display"),
                  event(0, 1000, "game.exe DXGI_ERROR_DEVICE_HUNG")]
        incident = self.detective().investigate("Game", "game.exe", timestamp=NOW, events=events)["incident"]
        self.assertEqual((incident["cause"], incident["confidence"]), ("Graphics Driver", "High"))

    def test_out_of_memory(self):
        incident = self.detective().investigate("Minecraft", "javaw.exe", timestamp=NOW,
            events=[event(0, message="javaw.exe OutOfMemory allocation failed")])["incident"]
        self.assertEqual((incident["cause"], incident["confidence"]), ("Out of Memory", "High"))

    def test_correlates_near_events_and_excludes_unrelated_old_event(self):
        rows = correlate_events([event(-2), event(1, 1001), event(-3600, 4101, "nvlddmkm")], NOW, "game.exe")
        self.assertEqual(len(rows), 2); self.assertNotIn(4101, [row["event_id"] for row in rows])

    def test_unknown_crash_does_not_fabricate_cause(self):
        incident = self.detective().investigate("App", "app.exe", timestamp=NOW, events=[])["incident"]
        self.assertEqual(incident["cause"], "Unknown"); self.assertEqual(incident["confidence"], "Low")

    def test_repeated_crashes_are_grouped(self):
        detective = self.detective(); events = [event(message="game.exe DXGI_ERROR_DEVICE_REMOVED")]
        detective.investigate("Game", "game.exe", timestamp=NOW, events=events)
        second = detective.investigate("Game", "game.exe", timestamp=NOW, events=events)["incident"]
        self.assertEqual(second["repeated_count"], 2)

    def test_disabled_detective(self):
        result = self.detective(enabled=False).investigate("Game", "game.exe", timestamp=NOW, events=[])
        self.assertTrue(result["disabled"]); self.assertEqual(self.memory.incident_history(), [])

    def test_malformed_events_are_ignored(self):
        rows = correlate_events([None, {}, {"timestamp": "bad", "event_id": "nope"}], NOW, "game.exe")
        self.assertEqual(rows, [])

    def test_level_one_capabilities_and_router_requests(self):
        manager = SkillManager(lambda name, args, approved=False: {"success": True, "tool": name})
        manager.discover(BUILTIN_SKILLS); manager.load_all()
        cases = {
            "Why did my game crash?": "analyze_crash",
            "What caused my last crash?": "get_last_crash",
            "Has Fortnite been crashing a lot?": "get_crash_history",
            "Show my crash history.": "get_crash_history",
            "Give me the technical details.": "explain_crash",
        }
        for request, capability in cases.items():
            with self.subTest(request=request):
                match = manager.route(request); self.assertIsNotNone(match); self.assertEqual(match.capability, capability)
                declared = next(c for c in manager.skills[match.skill_id].metadata.capabilities if c.id == capability)
                self.assertEqual(declared.permission_level, 1)

    def test_poll_detects_normal_application_crash_once(self):
        raw = {"Id": 1000, "TimeCreated": NOW.isoformat(), "Message": "Faulting application name: editor.exe", "ProviderName": "Application Error"}
        detective = CrashDetective(self.memory, event_provider=lambda **kwargs: [raw], clock=lambda: 100.0)
        first = detective.poll(); second = detective.poll()
        self.assertEqual(len(first), 1); self.assertEqual(second, [])


if __name__ == "__main__": unittest.main()
