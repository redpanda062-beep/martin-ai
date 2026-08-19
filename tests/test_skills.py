from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from martin_ai.skills import (Capability, DuplicateSkillError, Skill, SkillManager,
                              SkillMatch, SkillMetadata, SkillStatus, SkillValidationError)
from martin_ai.skills.builtin import BUILTIN_SKILLS


class DemoSkill(Skill):
    metadata = SkillMetadata("demo", "Demo", "Test skill", "1.0.0", "Tests",
                             (Capability("inspect", "Inspect", 1, keywords=("inspect",)),))
    def can_handle(self, request: str):
        return SkillMatch("demo", "inspect", .9) if "inspect" in request else None


class BrokenSkill(DemoSkill):
    metadata = SkillMetadata("broken", "Broken", "Broken test skill", "1.0.0", "Tests",
                             (Capability("inspect", "Inspect"),))
    def execute(self, action, parameters, *, approved=False): raise RuntimeError("boom")


class InvalidSkill(DemoSkill):
    metadata = SkillMetadata("INVALID ID", "", "", "", "", ())


class DependentSkill(DemoSkill):
    metadata = SkillMetadata("dependent", "Dependent", "Dependency test", "1.0.0", "Tests",
                             (Capability("inspect", "Inspect"),), dependencies=("missing",))


class SkillTests(unittest.TestCase):
    def manager(self, classes=(DemoSkill,), result=None):
        manager = SkillManager(lambda name, args, approved=False: result or {"success": True, "tool": name})
        manager.discover(classes); manager.load_all(); return manager

    def test_discovery_loading_and_execution(self):
        manager = self.manager(); self.assertEqual(manager.skills["demo"]._status, SkillStatus.HEALTHY)
        self.assertTrue(manager.execute(manager.route("inspect this")).get("success"))

    def test_enable_disable(self):
        manager = self.manager(); self.assertTrue(manager.set_enabled("demo", False))
        self.assertIsNone(manager.route("inspect this")); self.assertTrue(manager.set_enabled("demo", True))

    def test_failure_isolated(self):
        manager = self.manager((BrokenSkill,)); result = manager.execute(SkillMatch("broken", "inspect", .9))
        self.assertFalse(result["success"]); self.assertEqual(manager.skills["broken"]._status, SkillStatus.DEGRADED)

    def test_dependency_failure_degrades(self):
        manager = self.manager((DependentSkill,))
        self.assertEqual(manager.skills["dependent"]._status, SkillStatus.DEGRADED)

    def test_duplicate_ids(self):
        manager = SkillManager(lambda *args: {})
        with self.assertRaises(DuplicateSkillError): manager.discover((DemoSkill, DemoSkill))

    def test_invalid_metadata(self):
        manager = SkillManager(lambda *args: {})
        with self.assertRaises(SkillValidationError): manager.discover((InvalidSkill,))

    def test_permission_result_is_preserved(self):
        manager = self.manager(result={"success": False, "approval_required": True})
        self.assertTrue(manager.execute(SkillMatch("demo", "inspect", .9))["approval_required"])

    def test_natural_language_routes(self):
        manager = self.manager(BUILTIN_SKILLS)
        cases = {
            "What's my GPU temperature?": ("device", "nvidia_status"),
            "Open Steam.": ("system_control", "open_app"),
            "Why did my game crash?": ("gaming", "analyze_crash"),
            "Check my RAM usage.": ("system_control", "ram_status"),
            "What game am I playing?": ("gaming", "game_status"),
            "Why isn't my second monitor working?": ("device", "display_status"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                match = manager.route(text); self.assertIsNotNone(match)
                self.assertEqual((match.skill_id, match.capability), expected)


if __name__ == "__main__": unittest.main()
