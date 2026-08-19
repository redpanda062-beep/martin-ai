from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from martin_ai.game_detection import GameDetectionEngine, ProcessEvidence
from martin_ai.game_mode_engine import IntelligentGameMode, ollama_keep_alive_for_game
from martin_ai.memory import Database, MemoryManager


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        self.games = root / "games.json"
        self.games.write_text(json.dumps({"fortniteclient-win64-shipping.exe": "Fortnite"}), encoding="utf-8")
        self.memory = MemoryManager(Database(root / "memory.db"))

    def tearDown(self):
        self.memory.db.close(); self.temp.cleanup()

    def detector(self, processes, metadata=None):
        return GameDetectionEngine(self.memory, games_file=self.games, process_provider=lambda: processes,
                                   metadata_provider=lambda _path: metadata or {})

    def test_known_game_is_human_named_and_high_confidence(self):
        process = ProcessEvidence(10, "fortniteclient-win64-shipping.exe",
                                  r"C:\Program Files\Epic Games\Fortnite\FortniteClient-Win64-Shipping.exe",
                                  "Fortnite", True, memory_mb=1500)
        result = self.detector([process]).detect()
        self.assertTrue(result.detected); self.assertEqual(result.game_name, "Fortnite")
        self.assertEqual(result.confidence_label, "High")

    def test_launchers_and_desktop_apps_are_not_games(self):
        processes = [ProcessEvidence(1, "steam.exe", title="Steam", foreground=True),
                     ProcessEvidence(2, "epicgameslauncher.exe", title="Epic Games", foreground=True),
                     ProcessEvidence(3, "chrome.exe", title="Browser", foreground=True),
                     ProcessEvidence(4, "discord.exe", title="Discord", foreground=True)]
        self.assertIsNone(self.detector(processes).detect())

    def test_overlay_and_crash_helpers_are_not_games(self):
        processes = [ProcessEvidence(1, "eosoverlayrenderer-win64-shipping.exe", r"C:\Epic Games\EOS\overlay.exe", "", False),
                     ProcessEvidence(2, "crashreportclient.exe", r"C:\SteamApps\common\Game\crashreportclient.exe", "", False)]
        self.assertIsNone(self.detector(processes).detect())

    def test_game_filename_alone_does_not_activate(self):
        result = self.detector([ProcessEvidence(10, "fortniteclient-win64-shipping.exe")]).detect()
        self.assertIsNotNone(result); self.assertFalse(result.detected)

    def test_foreground_game_wins_and_helpers_do_not_duplicate_session(self):
        rows = [
            ProcessEvidence(10, "fortniteclient-win64-shipping.exe", r"C:\Epic Games\Fortnite\game.exe", "Fortnite", True, memory_mb=1000),
            ProcessEvidence(11, "fortniteclient-win64-shipping.exe", r"C:\Epic Games\Fortnite\helper.exe", "", False),
        ]
        result = self.detector(rows).detect()
        self.assertEqual(result.pids, [10, 11]); self.assertTrue(result.foreground)

    def test_unknown_game_can_be_learned_and_persists(self):
        row = ProcessEvidence(22, "mystery-win64-shipping.exe", r"C:\Games\steamapps\common\Mystery\mystery-win64-shipping.exe",
                              "Mystery", True, memory_mb=900)
        detector = self.detector([row]); possible = detector.detect()
        self.assertTrue(possible.detected); self.assertEqual(possible.game_name, "Unknown Game")
        self.assertTrue(detector.learn_pending("Ready or Not"))
        restarted = self.detector([row]).detect()
        self.assertTrue(restarted.detected); self.assertEqual(restarted.game_name, "Ready or Not")

    def test_steam_and_epic_metadata_are_discovered(self):
        root = Path(self.temp.name); steamapps = root / "pf86" / "Steam" / "steamapps"; steamapps.mkdir(parents=True)
        (steamapps / "appmanifest_1.acf").write_text('"name" "Test Steam Game"\n"installdir" "SteamGame"', encoding="utf-8")
        epic = root / "programdata" / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"; epic.mkdir(parents=True)
        (epic / "one.item").write_text(json.dumps({"InstallLocation": str(root / "EpicGame"), "DisplayName": "Test Epic Game"}), encoding="utf-8")
        with patch.dict(os.environ, {"ProgramFiles(x86)": str(root / "pf86"), "ProgramData": str(root / "programdata")}):
            metadata = GameDetectionEngine._launcher_metadata()
        self.assertEqual({item["launcher"] for item in metadata.values()}, {"Steam", "Epic Games"})

    def test_generic_executable_uses_metadata_instead_of_filename(self):
        row = ProcessEvidence(30, "game.exe", r"C:\Games\ReadyOrNot\game.exe", "Ready or Not", True,
                              memory_mb=1200, cpu_percent=20)
        result = self.detector([row], {"product_name": "Ready or Not", "company_name": "VOID Interactive"}).detect()
        self.assertTrue(result.detected); self.assertEqual(result.game_name, "Ready or Not")
        self.assertEqual(result.title_source, "executable metadata")

    def test_high_confidence_unknown_game_activates_without_invented_title(self):
        row = ProcessEvidence(31, "odd-win64-shipping.exe", r"C:\Games\Odd\odd-win64-shipping.exe",
                              "Untitled", True, memory_mb=1400, cpu_percent=25)
        result = self.detector([row]).detect()
        self.assertTrue(result.detected); self.assertEqual(result.game_name, "Unknown Game")

    def test_emulator_prefers_loaded_game_title(self):
        row = ProcessEvidence(32, "pcsx2-qt.exe", r"C:\Emulators\PCSX2\pcsx2-qt.exe",
                              "Def Jam: Fight for NY — PCSX2", True, memory_mb=900)
        result = self.detector([row]).detect()
        self.assertTrue(result.detected); self.assertEqual(result.game_name, "Def Jam: Fight for NY")
        self.assertEqual(result.launcher, "PCSX2")

    def test_minimized_manifest_game_stays_detected(self):
        root = str(Path(r"C:\Steam\steamapps\common\TestGame")).lower()
        detector = self.detector([ProcessEvidence(33, "client.exe", root + r"\client.exe", memory_mb=800)])
        detector.installed = {root: {"name": "Test Game", "launcher": "Steam", "app_id": "42"}}
        detector._manifest_at = float("inf")
        result = detector.detect(force=True)
        self.assertTrue(result.detected); self.assertEqual(result.game_name, "Test Game")

    def test_debug_report_is_bounded_and_explains_rejections(self):
        rows = [ProcessEvidence(i, f"candidate{i}.exe", rf"C:\Games\G{i}\candidate{i}.exe",
                                f"Candidate {i}", i == 1, memory_mb=350) for i in range(1, 20)]
        report = self.detector(rows).debug_report()
        self.assertLessEqual(len(report["candidates"]), 8)
        self.assertTrue(all("signals" in item and "accepted" in item for item in report["candidates"]))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        self.memory = MemoryManager(Database(root / "memory.db")); self.now = 1000.0
        self.clock = lambda: self.now
        self.sample = {"cpu_percent": 40, "ram_percent": 60, "gpu": {"available": True,
                       "utilization_percent": 95, "temperature_c": 72, "memory_used_mb": 4000, "memory_total_mb": 8000}}
        self.game = {"game": "Fortnite", "executable": "fortnite.exe", "confidence": 90,
                     "confidence_label": "High", "game_process_cpu_percent": 30, "game_process_ram_mb": 2000}

    def tearDown(self): self.memory.db.close(); self.temp.cleanup()

    def test_stable_start_multiple_samples_and_two_miss_end(self):
        engine = IntelligentGameMode(self.memory, {"session_reports": False}, clock=self.clock, error_provider=lambda *_: [])
        self.assertEqual(engine.update(self.game, self.sample)[0]["type"], "game_started")
        engine.update(self.game, self.sample); self.assertEqual(len(engine.samples), 2)
        self.assertEqual(engine.update(None, self.sample), []); self.assertIsNotNone(engine.active)
        events = engine.update(None, self.sample); self.assertEqual(events[0]["type"], "game_ended")
        profile = self.memory.get_game_profile("fortnite.exe")
        self.assertEqual(profile["executables"], ["fortnite.exe"])
        self.assertIn("peak_ram_percent", profile["baseline"])

    def test_high_gpu_alone_is_normal_but_multi_signal_cpu_pressure_warns(self):
        engine = IntelligentGameMode(self.memory, {}, clock=self.clock, error_provider=lambda *_: [])
        events = engine.update(self.game, self.sample)
        self.assertFalse(any(item["type"] == "performance_warning" for item in events))
        pressured = {**self.sample, "cpu_percent": 99, "gpu": {**self.sample["gpu"], "utilization_percent": 45}}
        events = engine.update(self.game, pressured)
        self.assertTrue(any(item.get("key") == "cpu_bottleneck" for item in events))

    def test_crash_evidence_saves_bounded_snapshot(self):
        engine = IntelligentGameMode(self.memory, {}, clock=self.clock,
                                     error_provider=lambda *_: [{"Id": 1000, "Message": "fortnite.exe fault"}])
        with patch("martin_ai.game_mode_engine.REPORT_DIR", Path(self.temp.name)):
            engine.update(self.game, self.sample)
            for _ in range(80): engine.update(self.game, self.sample)
            engine.update(None, self.sample); events = engine.update(None, self.sample)
            self.assertEqual(events[0]["type"], "game_crashed")
            payload = json.loads(Path(events[0]["report_path"]).read_text(encoding="utf-8"))
        self.assertLessEqual(len(payload["recent_samples"]), 60)

    def test_game_aware_ollama_retention_keeps_chat_available(self):
        self.assertEqual(ollama_keep_alive_for_game(True), "30s")
        self.assertEqual(ollama_keep_alive_for_game(False), "5m")

    def test_switch_emits_one_switch_event(self):
        engine = IntelligentGameMode(self.memory, {"session_reports": False}, clock=self.clock, error_provider=lambda *_: [])
        engine.update(self.game, self.sample)
        other = {**self.game, "game": "Other Game", "executable": "other.exe"}
        events = engine.update(other, self.sample)
        self.assertEqual([event["type"] for event in events], ["game_switched"])
        self.assertIn("Fortnite -> Other Game", events[0]["message"])


if __name__ == "__main__": unittest.main()
