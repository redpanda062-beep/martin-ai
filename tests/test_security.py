from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from martin_ai.actions import ActionManager
from martin_ai.tools import security


class SecurityParsingTests(unittest.TestCase):
    def test_defender_status_parsing(self) -> None:
        parsed = security.parse_defender_status({"AntivirusEnabled": True, "RealTimeProtectionEnabled": False})
        self.assertTrue(parsed["antivirus_enabled"])
        self.assertFalse(parsed["real_time_protection_enabled"])

    def test_firewall_status_parsing(self) -> None:
        parsed = security.parse_firewall_status([{"Name": "Domain", "Enabled": True}, {"Name": "Public", "Enabled": False}])
        self.assertEqual(parsed, {"Domain": True, "Private": None, "Public": False})

    def test_suspicious_process_scoring_explains_evidence(self) -> None:
        result = security.score_suspicious_process({"name": "powershell.exe", "path": r"C:\Users\me\AppData\Local\Temp\x.exe", "signature_status": "NotSigned", "restart_count": 3})
        self.assertEqual(result["label"], "suspicious")
        self.assertGreaterEqual(len(result["evidence"]), 3)

    def test_network_connection_formatting(self) -> None:
        text = security.format_network_connection({"protocol": "tcp", "local_address": "127.0.0.1", "local_port": 10, "remote_address": "1.2.3.4", "remote_port": 443, "state": "Established", "pid": 7, "process_name": "app"})
        self.assertIn("1.2.3.4:443", text); self.assertIn("PID 7", text)

    def test_duplicate_alert_suppression(self) -> None:
        alerts = [{"source": "x", "event_id": 1, "message": "same"}] * 3
        result = security.suppress_duplicate_alerts(alerts)
        self.assertEqual(len(result), 1); self.assertEqual(result[0]["occurrences"], 3)

    def test_status_is_graceful_without_admin_permissions(self) -> None:
        with patch.object(security, "_json_ps", return_value=(None, "Access is denied")):
            result = security.windows_security_status()
        self.assertTrue(result["success"]); self.assertIn("defender", result["permission_errors"])


class SecurityActionTests(unittest.TestCase):
    def test_scan_requires_approval(self) -> None:
        called = []
        manager = ActionManager({"defender_quick_scan": lambda: called.append(1) or {"success": True}})
        self.assertTrue(manager.queue("defender_quick_scan", {})["pending"])
        self.assertEqual(called, []); manager.approve(); self.assertEqual(called, [1])

    def test_scan_denial_and_cancellation(self) -> None:
        called = []
        manager = ActionManager({"defender_full_scan": lambda: called.append(1) or {"success": True}})
        manager.queue("defender_full_scan", {}); manager.deny()
        self.assertEqual(called, [])
        with patch.object(security, "_ps", return_value=(False, "Operation cancelled")):
            self.assertTrue(security.defender_quick_scan()["cancelled"])

    def test_destructive_action_is_rejected(self) -> None:
        self.assertFalse(security.reject_unsafe_security_action("disable_defender")["success"])

    def test_incident_report_creation(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.object(security, "REPORT_DIR", Path(folder)):
            result = security.create_incident_report({"status": "Needs attention", "findings": [{"evidence": "test"}]})
            self.assertTrue(Path(result["path"]).exists())
            self.assertIn("confidence_and_uncertainty", result["report"])


if __name__ == "__main__":
    unittest.main()
