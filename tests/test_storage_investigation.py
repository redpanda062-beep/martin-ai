from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from martin_ai.brain.evaluator import Evaluator
from martin_ai.brain.models import BrainState
from martin_ai.brain.planner import Planner
from martin_ai.intent import Intent, detect_intent
from martin_ai.response_formatter import format_response
from martin_ai.tools import tool_policies
from martin_ai.tools.storage import _excluded, _scan_locations


class StorageInvestigationTests(unittest.TestCase):
    def test_counts_metadata_and_leaves_files_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "nested").mkdir()
            (root / "small.txt").write_bytes(b"small")
            (root / "nested" / "large.bin").write_bytes(b"x" * 100)
            result = _scan_locations([("Downloads", root)])
            self.assertFalse(result["partial"])
            self.assertEqual(result["locations"][0]["bytes"], 105)
            self.assertEqual(result["locations"][0]["files"], 2)
            self.assertTrue(result["largest_files"][0]["path"].endswith("large.bin"))
            self.assertEqual((root / "small.txt").read_bytes(), b"small")
            self.assertEqual((root / "nested" / "large.bin").stat().st_size, 100)
            self.assertFalse(result["changes_made"])

    def test_scan_limit_returns_partial_lower_bounds(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index in range(5):
                (root / str(index)).write_bytes(b"x")
            result = _scan_locations([("Downloads", root)], max_entries=2)
            self.assertTrue(result["partial"])
            self.assertEqual(result["entries_examined"], 2)
            self.assertEqual(result["locations"][0]["bytes"], 2)

    def test_cloud_placeholders_and_junctions_are_excluded(self):
        for flag in (0x400, 0x1000, 0x40000, 0x400000):
            self.assertTrue(_excluded(SimpleNamespace(st_mode=0, st_file_attributes=flag)))

    def test_requests_route_to_read_only_storage_plan(self):
        policies = tool_policies()
        for request in ("Investigate storage", "What is taking up space?", "Why is my C drive full?", "Help me free up disk space"):
            self.assertEqual(detect_intent(request), Intent.TROUBLESHOOTING)
            plan = Planner(policies, lambda _: self.fail("Storage plan should not need the model")).create_plan(request)
            self.assertEqual([step.tool_name for step in plan], ["storage_status", "investigate_storage"])
            self.assertTrue(all(not step.approval_required for step in plan))
        self.assertEqual(detect_intent("check my storage"), Intent.STORAGE_STATUS)

    def test_partial_results_explain_scope_and_preserve_cleanup_choices(self):
        result = {"success": True, "partial": True, "scope": "Selected folders; not a whole-drive scan.",
                  "size_basis": "Logical sizes only.",
                  "locations": [{"name": "Downloads", "bytes": 2**30, "complete": False}],
                  "largest_files": [{"path": "example.zip", "bytes": 2**29}],
                  "suggestions": ["Review files before moving them."]}
        state = BrainState("Investigate storage", status="completed", evidence=[{"tool": "investigate_storage", "result": result}])
        Evaluator({}).finalize(state)
        text = format_response(state)
        self.assertIn("1.00 GB observed (partial count)", text)
        self.assertIn("not a whole-drive", text)
        self.assertIn("lower bounds", text)
        self.assertIn("example.zip", text)
        self.assertIn("Review files before moving", text)


if __name__ == "__main__":
    unittest.main()
