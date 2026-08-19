from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from martin_ai.mobile_companion import MAX_TRANSFER_BYTES, MobileCompanionService
from martin_ai.phone_remote import DEFAULT_DEVICE_PERMISSIONS, PhoneRemoteService, RemoteCallbacks


class MobileCompanionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.service = MobileCompanionService(root=Path(self.temp.name), clock=lambda: self.now)
        self.device = {"id": "phone", "permissions": {**DEFAULT_DEVICE_PERMISSIONS, "clipboard_sync": True,
            "file_transfer": True, "terminal": True, "screenshots": True, "camera": True, "audio": True}}

    @patch("martin_ai.mobile_companion.write_clipboard", return_value={"success": True})
    @patch("martin_ai.mobile_companion.read_clipboard", return_value={"success": True, "text": "hello"})
    def test_clipboard_both_directions_and_sensitive_block(self, read, write):
        self.service.settings.update(clipboard_enabled=True, clipboard_phone_to_pc=True, clipboard_pc_to_phone=True)
        self.assertEqual(self.service.clipboard("get", self.device)["text"], "hello")
        self.assertTrue(self.service.clipboard("set", self.device, "https://example.test")["success"])
        write.assert_called_once_with("https://example.test")
        self.assertFalse(self.service.clipboard("set", self.device, "Bearer secret.token")["success"])

    def test_clipboard_permission_denied(self):
        self.service.settings["clipboard_enabled"] = True
        denied = {"permissions": {"clipboard_sync": False}}
        self.assertFalse(self.service.clipboard("get", denied)["success"])

    def test_file_upload_download_history_and_cancel(self):
        self.service.settings["file_transfer_enabled"] = True
        uploaded = self.service.upload(self.device, "shot.png", base64.b64encode(b"image").decode())
        self.assertTrue(uploaded["success"])
        downloaded = self.service.download(self.device, "Martin Shared", "shot.png")
        self.assertEqual(base64.b64decode(downloaded["content"]), b"image")
        self.assertEqual(len(self.service.state["transfers"]), 2)
        self.service.state["transfers"].append({"id": "pending", "status": "Pending"})
        self.assertEqual(self.service.cancel_transfer("pending")["transfer"]["status"], "Cancelled")

    def test_invalid_location_traversal_and_large_transfer(self):
        self.service.settings["file_transfer_enabled"] = True
        self.assertFalse(self.service.download(self.device, "System", "secret.txt")["success"])
        self.assertFalse(self.service.download(self.device, "Martin Shared", "../secret.txt")["success"])
        huge = base64.b64encode(b"x" * (MAX_TRANSFER_BYTES + 1)).decode()
        self.assertIn("25 MB", self.service.upload(self.device, "large.zip", huge)["error"])

    def test_terminal_disabled_permission_and_safe_allowlist(self):
        self.assertIn("disabled", self.service.terminal(self.device, "ipconfig")["error"])
        self.service.settings["terminal_enabled"] = True
        denied = {"permissions": {"terminal": False}}
        self.assertIn("denied", self.service.terminal(denied, "ipconfig")["error"])
        self.assertIn("Safe Mode", self.service.terminal(self.device, "Remove-Item C:\\Windows")["error"])
        with patch("martin_ai.mobile_companion.subprocess.run") as run:
            run.return_value.returncode = 0; run.return_value.stdout = "ok"; run.return_value.stderr = ""
            self.assertTrue(self.service.terminal(self.device, "Get-Process")["success"])

    def test_safe_queue_rejects_dangerous_and_expires(self):
        queued = self.service.queue(self.device, "Open Discord", ttl_seconds=60)
        self.assertTrue(queued["success"])
        self.assertFalse(self.service.queue(self.device, "Shutdown")["success"])
        self.assertFalse(self.service.queue(self.device, "Run Terminal Command")["success"])
        self.now += 61
        self.assertEqual(self.service.pending_queue()[0]["status"], "Expired")

    def test_pin_is_hashed_and_verified(self):
        self.assertTrue(self.service.set_pin("123456")["success"])
        self.assertTrue(self.service.verify_pin("123456")); self.assertFalse(self.service.verify_pin("654321"))
        self.assertNotIn("123456", self.service.state_file.read_text(encoding="utf-8"))


class PhoneCompanionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        callbacks = RemoteCallbacks(lambda _d: {}, lambda _t, _d: {"success": True}, lambda _a, _c, _d: {"success": True})
        self.remote = PhoneRemoteService(callbacks, state_file=Path(self.temp.name) / "devices.json")
        self.device = {"id": "phone", "permissions": {**DEFAULT_DEVICE_PERMISSIONS, "camera": True}}

    def tearDown(self): self.remote.stop()

    def test_phone_cannot_enable_pc_privileged_features(self):
        result = self.remote.mobile_post("settings", {"settings": {"terminal_enabled": True, "camera_enabled": True,
            "file_transfer_enabled": True, "low_bandwidth": True}}, self.device)
        self.assertTrue(result["settings"]["low_bandwidth"])
        self.assertFalse(result["settings"]["terminal_enabled"]); self.assertFalse(result["settings"]["camera_enabled"])

    def test_camera_disabled_and_provider_unavailable(self):
        self.assertFalse(self.remote.mobile_post("camera", {"operation": "start"}, self.device)["success"])
        self.remote.companion.settings["camera_enabled"] = True
        result = self.remote.mobile_post("camera", {"operation": "start"}, self.device)
        self.assertFalse(result["success"]); self.assertIsNone(self.remote.camera_device_id)

    def test_emergency_disconnect_ends_every_session_but_preserves_device(self):
        self.remote.devices.append({"id": "phone", "name": "Phone", "revoked": False})
        self.remote.control_device_id = "phone"; self.remote.camera_device_id = "phone"
        self.remote.audio_device_id = "phone"; self.remote.terminal_device_id = "phone"; self.remote.sessions["x"] = {}
        self.assertTrue(self.remote.emergency_disconnect()["success"])
        self.assertIsNone(self.remote.control_device_id); self.assertFalse(self.remote.sessions)
        self.assertFalse(self.remote.devices[0]["revoked"])

    def test_low_bandwidth_profile_and_versions_diagnostics(self):
        self.remote.companion.settings["low_bandwidth"] = True
        name, profile = self.remote._quality(self.device)
        self.assertEqual(name, "Low bandwidth"); self.assertLessEqual(profile["fps"], 8)
        self.assertEqual(self.remote.mobile_get("versions", {}, self.device)["compatibility"], "Compatible")
        self.assertTrue(self.remote.mobile_get("diagnostics", {}, self.device)["sanitized"])


if __name__ == "__main__": unittest.main()
