import unittest

from martin_ai.brain import BrainState
from martin_ai.investigation_followup import answer_investigation_followup


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.state = BrainState("Investigate storage", status="completed", updated_at="2026-10-07T21:59:00", evidence=[{
            "tool": "investigate_storage", "result": {"success": True, "partial": True, "largest_files": [
                {"path": r"C:\Users\test\AppData\Roaming\Vortex\downloads\game\mod.zip", "bytes": 6 * 2**30},
                {"path": r"C:\Users\test\AppData\Local\NVIDIA\DXCache\shader.nvph", "bytes": 4 * 2**30},
            ]}}])

    def test_explains_vortex_from_retained_evidence(self):
        text = answer_investigation_followup("What is that Vortex file?", self.state)
        self.assertIn("mod.zip", text)
        self.assertIn("6.00 GB", text)
        self.assertIn("installation status was not checked", text)
        self.assertIn("No new check was run", text)
        self.assertNotIn("shader.nvph", text)

    def test_file_name_and_ambiguous_reference(self):
        self.assertIn("NVIDIA shader cache", answer_investigation_followup("Explain shader.nvph", self.state))
        self.assertIn("Which file", answer_investigation_followup("Can I delete that?", self.state))
        self.assertIn("No matching file", answer_investigation_followup("What is that Vortex file?", BrainState(
            "test", status="completed", evidence=[{"tool": "investigate_storage", "result": {"success": True}}])))

    def test_fresh_checks_actions_and_memory_questions_are_not_intercepted(self):
        for text in ("Investigate storage", "What is taking up space?", "Check my RAM", "What is my RAM usage?",
                     "Delete that Vortex file", "What do you remember about me?", "What's the capital of Japan?"):
            self.assertIsNone(answer_investigation_followup(text, self.state), text)

    def test_missing_context_is_not_invented(self):
        self.assertIn("Run an investigation first", answer_investigation_followup("What is that Vortex file?", None))

    def test_ram_followup_uses_snapshot(self):
        self.state.normal_readings = ["RAM is 70.9% used, below the 90% threshold."]
        self.assertIn("70.9%", answer_investigation_followup("Explain that RAM result", self.state))

