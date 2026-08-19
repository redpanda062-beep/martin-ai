from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from martin_ai.game_detection import GameDetectionEngine, ProcessEvidence


def process(pid: int, exe: str, path: str, *, foreground: bool = False,
            title: str = "", memory: float = 0, cpu: float = 0, ppid: int = 0) -> ProcessEvidence:
    return ProcessEvidence(pid, exe, path, title, foreground, time.time() - 30,
                           memory, cpu, ppid, [], "GameWindow", bool(title))


class UniversalGameDetectionTests(unittest.TestCase):
    def engine(self, rows, metadata=None):
        temporary = TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        games = Path(temporary.name) / "games.json"; games.write_text("{}", encoding="utf-8")
        detector = GameDetectionEngine(None, games_file=games,
            process_provider=lambda: rows,
            metadata_provider=lambda path: (metadata or {}).get(path, {}))
        detector.installed = {}; detector._manifest_at = time.monotonic()
        return detector

    def test_windows_operating_system_metadata_can_never_be_a_game(self):
        path = r"C:\Windows\System32\SystemSettings.exe"
        rows = [process(10, "systemsettings.exe", path, foreground=True,
                        title="Settings", memory=900, cpu=30)]
        detector = self.engine(rows, {path: {
            "product_name": "Microsoft® Windows® Operating System",
            "company_name": "Microsoft Corporation",
        }})
        self.assertIsNone(detector.detect(force=True))
        report = detector.debug_report()
        self.assertNotIn("Microsoft® Windows® Operating System", str(report.get("active")))
        self.assertTrue(any("Windows" in row["rejection_reason"] for row in report["rejected"]))

    def test_unknown_resource_intensive_foreground_app_is_detected_without_database(self):
        rows = [process(20, "mystery.exe", r"D:\Indie\mystery.exe", foreground=True,
                        title="A New Adventure", memory=1200, cpu=15)]
        detection = self.engine(rows).detect(force=True)
        self.assertIsNotNone(detection)
        self.assertTrue(detection.detected)
        self.assertEqual(detection.game_name, "A New Adventure")

    def test_steam_manifest_identifies_arbitrary_game_and_child_process(self):
        rows = [
            process(30, "steam.exe", r"C:\Steam\steam.exe"),
            process(31, "game.exe", r"D:\SteamLibrary\steamapps\common\Any Game\game.exe",
                    foreground=True, title="Any Game", memory=800, cpu=12, ppid=30),
        ]
        detector = self.engine(rows)
        detector.installed = {r"d:\steamlibrary\steamapps\common\any game": {
            "name": "Manifest Title", "launcher": "Steam", "app_id": "123"}}
        detection = detector.detect(force=True)
        self.assertEqual(detection.game_name, "Manifest Title")
        self.assertEqual(detection.app_id, "123")
        self.assertIn(31, detection.pids)

    def test_alt_tab_retains_game_but_process_exit_clears_it(self):
        rows = [process(40, "unknown.exe", r"D:\Indie\unknown.exe", foreground=True,
                        title="Unknown Hero", memory=1000, cpu=15)]
        detector = self.engine(rows)
        self.assertTrue(detector.detect(force=True).detected)
        rows[0].foreground = False; rows[0].visible_window = True; rows[0].cpu_percent = 0
        self.assertTrue(detector.detect(force=True).detected)
        rows.clear()
        self.assertIsNone(detector.detect(force=True))


if __name__ == "__main__":
    unittest.main()
