from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from martin_ai.actions import ActionManager
from martin_ai.tools import security
from martin_ai.security_presenter import overall_protection, translate_action_result


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
        failed = {"success": False, "started": True, "exit_code": 1, "stdout": "", "stderr": "Access is denied",
                  "timed_out": False, "unavailable": False, "message": "Access is denied"}
        with patch.object(security, "run_powershell", return_value=failed):
            result = security.windows_security_status()
        self.assertFalse(result["success"]); self.assertEqual(result["status"], "error")
        self.assertIn("powershell", result["permission_errors"])

    def test_status_contract_uses_real_completed_checks(self) -> None:
        payload = {"defender": {"AntivirusEnabled": True, "AntispywareEnabled": True,
                                "RealTimeProtectionEnabled": True, "AntivirusSignatureAge": 0,
                                "AntivirusSignatureVersion": "1.2.3", "QuickScanAge": 0, "FullScanAge": 5},
                   "firewall": [{"Name": "Domain", "Enabled": True}, {"Name": "Private", "Enabled": True},
                                {"Name": "Public", "Enabled": True}],
                   "current_threats": [], "recent_detections": [],
                   "service": {"Status": 4, "StartType": 2}, "errors": {}}
        completed = {"success": True, "started": True, "exit_code": 0, "stdout": __import__("json").dumps(payload),
                     "stderr": "", "timed_out": False, "unavailable": False, "message": ""}
        with patch.object(security, "run_powershell", return_value=completed): result = security.windows_security_status()
        self.assertTrue(result["success"]); self.assertEqual(result["overall_status"], "protected")
        self.assertEqual(result["threats"]["count"], 0); self.assertEqual(result["defender"]["service_state"], "Running")


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
        cancelled = {"success": False, "started": True, "exit_code": 1, "stdout": "", "stderr": "Operation cancelled",
                     "timed_out": False, "unavailable": False, "message": "Operation cancelled"}
        with patch.object(security, "run_powershell", return_value=cancelled):
            result = security.defender_quick_scan()
            self.assertFalse(result["success"]); self.assertFalse(result["executed"])

    def test_scan_execution_states_and_admin_detection(self) -> None:
        successful = {"success": True, "started": True, "exit_code": 0, "stdout": "", "stderr": "",
                      "timed_out": False, "unavailable": False, "message": ""}
        with patch.object(security, "run_powershell", return_value=successful): result = security.defender_quick_scan()
        self.assertTrue(result["executed"]); self.assertTrue(result["completed"]); self.assertFalse(result["simulated"])
        denied = {**successful, "success": False, "exit_code": 1, "stderr": "Access is denied"}
        with patch.object(security, "run_powershell", return_value=denied): result = security.defender_full_scan()
        self.assertFalse(result["executed"]); self.assertTrue(result["requires_admin"])

    def test_custom_scan_passes_path_outside_script(self) -> None:
        successful = {"success": True, "started": True, "exit_code": 0, "stdout": "", "stderr": "",
                      "timed_out": False, "unavailable": False, "message": ""}
        with tempfile.TemporaryDirectory(prefix="scan ' folder ") as folder, patch.object(security, "run_powershell", return_value=successful) as run:
            result = security.defender_custom_scan(folder)
        self.assertTrue(result["executed"])
        self.assertNotIn(folder, run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["parameters"]["SCAN_PATH"], str(Path(folder).resolve()))

    def test_destructive_action_is_rejected(self) -> None:
        self.assertFalse(security.reject_unsafe_security_action("disable_defender")["success"])

    def test_incident_report_creation(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.object(security, "REPORT_DIR", Path(folder)):
            result = security.create_incident_report({"status": "Needs attention", "findings": [{"evidence": "test"}]})
            self.assertTrue(Path(result["path"]).exists())
            self.assertIn("confidence_and_uncertainty", result["report"])

    def test_simulated_scan_is_never_presented_as_completed(self) -> None:
        result = translate_action_result({"success": True, "action": "defender_quick_scan",
                                          "result": {"success": True, "simulated": True, "executed": False}})
        self.assertEqual(result["title"], "TEST MODE")
        self.assertFalse(result["success"]); self.assertFalse(result["executed"])
        self.assertIn("not actually scanned", result["message"])

    def test_real_scan_and_permission_error_are_human_readable(self) -> None:
        completed = translate_action_result({"action": "defender_quick_scan", "result": {
            "success": True, "executed": True, "threats_found": 0, "duration_seconds": 84}})
        self.assertEqual(completed["title"], "Scan Complete")
        self.assertIn("No threats", completed["message"]); self.assertIn("1m 24s", completed["message"])
        failed = translate_action_result({"action": "defender_full_scan", "result": {
            "success": False, "executed": True, "error": "Access is denied"}})
        self.assertIn("Administrator permission", failed["message"])
        self.assertNotIn("Access is denied", failed["message"])

    def test_overall_protection_waits_for_required_checks(self) -> None:
        self.assertEqual(overall_protection({})[0], "CHECKING...")
        protected = {"defender": {"antivirus_enabled": True, "real_time_protection_enabled": True},
                     "firewall": {"Domain": True, "Private": True, "Public": True}, "current_threats": []}
        self.assertEqual(overall_protection(protected)[0], "PROTECTED")
        protected["firewall"]["Public"] = False
        self.assertEqual(overall_protection(protected)[0], "AT RISK")


if __name__ == "__main__":
    unittest.main()
