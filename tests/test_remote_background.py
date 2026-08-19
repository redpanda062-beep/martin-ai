from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from martin_ai.phone_remote import DEFAULT_DEVICE_PERMISSIONS, PhoneRemoteService, RemoteCallbacks
from martin_ai.remote_background import read_status
from martin_ai.wake_on_lan import magic_packet, normalize_mac, send_magic_packet


class WakeOnLanTests(unittest.TestCase):
    def test_normalize_and_standard_magic_packet(self):
        self.assertEqual(normalize_mac("aa-bb-cc-dd-ee-ff"), "AA:BB:CC:DD:EE:FF")
        packet = magic_packet("AA:BB:CC:DD:EE:FF")
        self.assertEqual(len(packet), 102); self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:12], bytes.fromhex("AABBCCDDEEFF"))

    def test_invalid_mac_rejected(self):
        for value in ("", "AA:BB", "not-a-mac", "AA:BB:CC:DD:EE:GG"):
            with self.assertRaises(ValueError): magic_packet(value)

    @patch("martin_ai.wake_on_lan.socket.socket")
    def test_wake_retries_are_bounded(self, socket_factory):
        sock = MagicMock(); socket_factory.return_value.__enter__.return_value = sock
        result = send_magic_packet("AA:BB:CC:DD:EE:FF", ["192.168.1.255"], repeats=99)
        self.assertEqual(result["packets_sent"], 3); self.assertEqual(sock.sendto.call_count, 3)


class RemoteServicesApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.actions = []
        callbacks = RemoteCallbacks(lambda _d: {"martin_ai": "Stopped", "session_agent": "Running", "wake": {"ready": True}},
            lambda _t, _d: {}, lambda action, confirmed, _d: self.actions.append((action, confirmed)) or {"success": True})
        self.remote = PhoneRemoteService(callbacks, state_file=Path(self.temp.name) / "devices.json")
        self.device = {"id": "phone", "permissions": dict(DEFAULT_DEVICE_PERMISSIONS)}

    def tearDown(self): self.remote.stop()

    def test_service_status_distinguishes_brain(self):
        result = self.remote.mobile_get("services", {}, self.device)
        self.assertEqual(result["remote_service"], "Running"); self.assertEqual(result["martin_ai"], "Stopped")
        self.assertTrue(result["wake"]["ready"])

    def test_background_dashboard_uses_lightweight_status_callback(self):
        result = self.remote.mobile_get("dashboard", {}, self.device)
        self.assertEqual(result["martin_ai"], "Stopped"); self.assertEqual(result["session_agent"], "Running")

    def test_only_approved_martin_component_can_start(self):
        bad = self.remote.mobile_post("services", {"operation": "start_notepad"}, self.device)
        self.assertFalse(bad["success"])
        good = self.remote.mobile_post("services", {"operation": "start_martin_ai"}, self.device)
        self.assertTrue(good["success"]); self.assertEqual(self.actions, [("start_martin_ai", True)])

    def test_start_permission_can_be_revoked(self):
        self.device["permissions"]["start_martin_ai"] = False
        result = self.remote.mobile_post("services", {"operation": "start_martin_ai"}, self.device)
        self.assertFalse(result["success"]); self.assertFalse(self.actions)


if __name__ == "__main__": unittest.main()
