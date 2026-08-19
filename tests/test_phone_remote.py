from __future__ import annotations

import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import http.cookiejar
from pathlib import Path
from unittest.mock import patch

from martin_ai.phone_remote import PhoneRemoteService, RemoteCallbacks, DEFAULT_DEVICE_PERMISSIONS, MOBILE_HTML, PWA_MANIFEST, SERVICE_WORKER_JS, REMOTE_UI_VERSION, RateLimiter


class PhoneRemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.actions = []
        callbacks = RemoteCallbacks(lambda _device: {"online": True, "cpu": 12},
                                    lambda text, _device: {"success": True, "message": "local:" + text},
                                    lambda action, confirmed, _device: self.actions.append((action, confirmed)) or {"success": True, "action": action})
        self.service = PhoneRemoteService(callbacks, settings={"port": 0}, state_file=Path(self.temp.name) / "devices.json")

    def tearDown(self): self.service.stop()

    @patch.object(PhoneRemoteService, "local_ipv4", return_value="127.0.0.1")
    def test_unpaired_denied_pair_status_chat_and_revocation(self, _address):
        self.assertTrue(self.service.start()["success"]); base = self.service.address
        with self.assertRaises(urllib.error.HTTPError) as denied: urllib.request.urlopen(base + "/api/status")
        self.assertEqual(denied.exception.code, 401)
        unauthenticated_action = urllib.request.Request(base + "/api/action", data=json.dumps({"action": "volume_up"}).encode(),
                                                        headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as action_denied: urllib.request.urlopen(unauthenticated_action)
        self.assertEqual(action_denied.exception.code, 401)
        pairing = self.service.begin_pairing(); payload = json.dumps({"code": pairing["code"], "device_name": "Test Phone"}).encode()
        request = urllib.request.Request(base + "/api/pair", data=payload, headers={"Content-Type": "application/json"})
        paired = json.loads(urllib.request.urlopen(request).read()); token = paired["token"]
        auth = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        status = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/status", headers=auth)).read())
        self.assertEqual(status["cpu"], 12)
        chat = urllib.request.Request(base + "/api/chat", data=json.dumps({"message": "optimization status"}).encode(), headers=auth)
        self.assertEqual(json.loads(urllib.request.urlopen(chat).read())["message"], "local:optimization status")
        self.assertNotIn(token, self.service.state_file.read_text(encoding="utf-8"))
        self.assertTrue(self.service.revoke(paired["device_id"]))
        with self.assertRaises(urllib.error.HTTPError) as revoked:
            urllib.request.urlopen(urllib.request.Request(base + "/api/status", headers=auth))
        self.assertEqual(revoked.exception.code, 401)

    def test_pairing_code_expires_and_input_defaults_off(self):
        pairing = self.service.begin_pairing(); self.service.pairing["expires"] = time.time() - 1
        self.assertFalse(self.service.complete_pairing(pairing["code"], "Phone", "192.168.1.2")["success"])
        device = {"id": "device", "permissions": dict(DEFAULT_DEVICE_PERMISSIONS)}
        self.assertEqual(self.service.handle_input({"type": "move", "dx": 1, "dy": 1}, device)["error"], "Remote input is disabled on Martin PC.")

    def test_non_private_address_refuses_start(self):
        with patch.object(PhoneRemoteService, "local_ipv4", return_value=None):
            result = self.service.start()
        self.assertFalse(result["success"])

    def test_new_device_safe_permission_defaults(self):
        pairing = self.service.begin_pairing(); paired = self.service.complete_pairing(pairing["code"], "Phone", "192.168.1.2")
        device = next(row for row in self.service.devices if row["id"] == paired["device_id"])
        for key in ("remote_chat", "game_status", "game_controls", "media_controls", "volume_controls", "pc_lock"):
            self.assertTrue(device["permissions"][key])
        for key in ("remote_input", "sleep", "restart", "shutdown", "stop_ai"):
            self.assertFalse(device["permissions"][key])
        self.assertFalse(device["permissions"]["view_screen"]); self.assertFalse(device["permissions"]["clipboard_sync"])
        self.service.settings["allow_remote_input"] = True
        self.assertEqual(self.service.handle_input({"type": "move", "dx": 1, "dy": 1}, device)["error"],
                         "Remote input permission is disabled for this device.")

    @patch.object(PhoneRemoteService, "local_ipv4", return_value="127.0.0.1")
    def test_device_permissions_allow_media_but_block_power_until_enabled(self, _address):
        self.service.start(); pairing = self.service.begin_pairing()
        paired = self.service.complete_pairing(pairing["code"], "Phone", "127.0.0.1"); token = paired["token"]
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        def action(name, confirmed=False):
            request = urllib.request.Request(self.service.address + "/api/action",
                data=json.dumps({"action": name, "confirmed": confirmed}).encode(), headers=headers)
            return json.loads(urllib.request.urlopen(request).read())
        self.assertTrue(action("volume_up")["success"])
        self.assertTrue(action("media_play_pause")["success"])
        self.assertEqual(action("restart", True)["error"], "Restart permission is disabled.")
        permissions = dict(DEFAULT_DEVICE_PERMISSIONS); permissions["restart"] = True
        self.assertTrue(self.service.update_permissions(paired["device_id"], permissions))
        self.assertIn("requires confirmation", action("restart", False)["error"])
        self.assertTrue(action("restart", True)["success"])
        self.assertEqual(self.actions[-1], ("restart", True))

    @patch.object(PhoneRemoteService, "interactive_desktop", return_value=True)
    def test_control_requires_both_permissions_and_is_exclusive(self, _desktop):
        first = {"id": "one", "name": "One", "permissions": {**DEFAULT_DEVICE_PERMISSIONS, "view_screen": True, "remote_input": True}}
        second = {"id": "two", "name": "Two", "permissions": {**DEFAULT_DEVICE_PERMISSIONS, "view_screen": True, "remote_input": True}}
        self.assertIn("disabled on Martin PC", self.service.control_session(first, "start")["error"])
        self.service.settings["allow_remote_input"] = True
        self.assertTrue(self.service.control_session(first, "start")["success"])
        self.assertEqual(self.service.control_session(second, "start")["error"], "Another device currently controls Martin PC.")
        self.assertTrue(self.service.control_session(second, "take", True)["success"])
        self.assertEqual(self.service.control_device_id, "two")
        self.service.disable_input(); self.assertIsNone(self.service.control_device_id)

    @patch.object(PhoneRemoteService, "interactive_desktop", return_value=True)
    @patch("martin_ai.phone_remote.ctypes.windll")
    def test_validated_input_requires_session_and_supports_mouse_keyboard(self, winapi, _desktop):
        device = {"id": "one", "name": "One", "permissions": {**DEFAULT_DEVICE_PERMISSIONS, "view_screen": True, "remote_input": True}}
        self.service.settings["allow_remote_input"] = True
        self.assertIn("does not own", self.service.handle_input({"type": "move", "dx": 2, "dy": 3}, device)["error"])
        self.service.control_session(device, "start")
        for payload in ({"type": "move", "dx": 2, "dy": 3}, {"type": "click", "button": "right"},
                        {"type": "scroll", "amount": 120}, {"type": "drag_start"}, {"type": "drag_end"},
                        {"type": "key", "key": "TAB", "modifiers": ["ALT"]}, {"type": "text", "text": "safe"}):
            self.assertTrue(self.service.handle_input(payload, device)["success"])
        self.assertEqual(self.service.handle_input({"type": "key", "key": "CTRL_ALT_DEL"}, device)["error"], "Unsupported key")

    @patch.object(PhoneRemoteService, "local_ipv4", return_value="127.0.0.1")
    def test_screen_stream_requires_view_permission(self, _address):
        self.service.start(); pairing = self.service.begin_pairing(); paired = self.service.complete_pairing(pairing["code"], "Phone", "127.0.0.1")
        request = urllib.request.Request(self.service.address + "/api/desktop/stream", headers={"Authorization": "Bearer " + paired["token"]})
        with self.assertRaises(urllib.error.HTTPError) as denied: urllib.request.urlopen(request)
        self.assertEqual(denied.exception.code, 403)

    @patch.object(PhoneRemoteService, "local_ipv4", return_value="127.0.0.1")
    def test_authenticated_mjpeg_stream_is_in_memory_and_records_real_metrics(self, _address):
        from PIL import Image
        self.service.capture_provider = lambda: Image.new("RGB", (640, 360), "#731010")
        self.service.start(); pairing = self.service.begin_pairing(); paired = self.service.complete_pairing(pairing["code"], "Phone", "127.0.0.1")
        permissions = dict(DEFAULT_DEVICE_PERMISSIONS); permissions["view_screen"] = True
        self.service.update_permissions(paired["device_id"], permissions)
        request = urllib.request.Request(self.service.address + "/api/desktop/stream", headers={"Authorization": "Bearer " + paired["token"]})
        response = urllib.request.urlopen(request, timeout=5); payload = response.read(5000); response.close(); time.sleep(.2)
        self.assertIn(b"Content-Type: image/jpeg", payload)
        status = self.service.remote_desktop_status()
        self.assertGreater(status["stream_fps"], 0); self.assertGreater(status["bandwidth_mbps"], 0)

    def test_monitor_selection_defaults_primary_and_falls_back_after_disconnect(self):
        primary = {"id": "one", "number": 1, "label": "Monitor 1 — Primary", "x": 0, "y": 0,
                   "width": 1920, "height": 1080, "primary": True}
        second = {"id": "two", "number": 2, "label": "Monitor 2", "x": -1280, "y": 40,
                  "width": 1280, "height": 1024, "primary": False}
        device = {"id": "phone", "name": "Phone"}
        with patch.object(self.service, "refresh_monitors", side_effect=lambda **_kwargs: self.service.monitors):
            self.service.monitors = [primary, second]
            self.assertEqual(self.service.selected_monitor(device)["id"], "one")
            self.assertTrue(self.service.select_monitor(device, "two")["success"])
            self.assertEqual(self.service.selected_monitor(device)["id"], "two")
            self.service.monitors = [primary]
            self.assertEqual(self.service.selected_monitor(device)["id"], "one")

    @patch.object(PhoneRemoteService, "interactive_desktop", return_value=True)
    @patch("martin_ai.phone_remote.ctypes.windll")
    def test_direct_touch_maps_selected_monitor_virtual_coordinates(self, winapi, _desktop):
        device = {"id": "phone", "name": "Phone", "permissions": {**DEFAULT_DEVICE_PERMISSIONS,
                  "view_screen": True, "remote_input": True}}
        second = {"id": "two", "number": 2, "label": "Monitor 2", "x": -1920, "y": 120,
                  "width": 1920, "height": 1080, "primary": False}
        self.service.settings["allow_remote_input"] = True
        self.service.control_device_id = "phone"; self.service.control_last_input = time.monotonic()
        with patch.object(self.service, "selected_monitor", return_value=second):
            result = self.service.handle_input({"type": "direct", "x": .5, "y": .25, "sequence": 1}, device)
        self.assertTrue(result["success"])
        winapi.user32.SetCursorPos.assert_called_once_with(-960, 390)

    def test_monitor_debug_reports_arrangement_and_capture_results(self):
        self.service.monitors = [{"id": "one", "number": 1, "label": "Monitor 1 — Primary", "x": 0, "y": 0,
                                  "width": 1920, "height": 1080, "primary": True},
                                 {"id": "two", "number": 2, "label": "Monitor 2", "x": 1920, "y": -100,
                                  "width": 2560, "height": 1440, "primary": False}]
        self.service.monitor_capture_status = {"one": {"ok": True}, "two": {"ok": False, "error": "capture failed"}}
        with patch.object(self.service, "refresh_monitors", side_effect=lambda **_kwargs: self.service.monitors):
            debug = self.service.remote_desktop_debug()
        self.assertIn("Connected Monitors: 2", debug); self.assertIn("Position: 1920,-100", debug)
        self.assertIn("Capture: OK", debug); self.assertIn("FAILED — capture failed", debug)

    @patch.object(PhoneRemoteService, "local_ipv4", return_value="127.0.0.1")
    def test_authenticated_single_frame_transport_and_client_ack(self, _address):
        from PIL import Image
        self.service.capture_provider = lambda *_bbox: Image.new("RGB", (800, 450), "#7b2020")
        self.service.start(); pairing = self.service.begin_pairing()
        paired = self.service.complete_pairing(pairing["code"], "Safari Phone", "127.0.0.1")
        permissions = {**DEFAULT_DEVICE_PERMISSIONS, "view_screen": True}
        self.service.update_permissions(paired["device_id"], permissions)
        headers = {"Authorization": "Bearer " + paired["token"], "Content-Type": "application/json"}
        monitor_id = self.service.primary_monitor()["id"]
        frame = urllib.request.urlopen(urllib.request.Request(self.service.address + "/api/desktop/frame?monitor_id=" + monitor_id, headers=headers))
        payload = frame.read(); self.assertEqual(frame.headers.get_content_type(), "image/jpeg")
        self.assertGreater(len(payload), 0); self.assertTrue(frame.headers["X-Martin-Frame-Id"])
        decoded = Image.open(__import__("io").BytesIO(payload)); self.assertEqual(decoded.size, (600, 337))
        self.assertLess(self.service._near_black_ratio(decoded), .99)
        self.assertEqual(frame.headers["Cache-Control"], "no-store, no-cache, must-revalidate")
        ack = urllib.request.Request(self.service.address + "/api/desktop/frame-ack",
            data=json.dumps({"frame_id": frame.headers["X-Martin-Frame-Id"], "rendered": True}).encode(), headers=headers)
        self.assertTrue(json.loads(urllib.request.urlopen(ack).read())["success"])
        self.assertEqual(self.service.stream_stats["client_acks"], 1)
        self.assertGreater(self.service.stream_stats["frames"], 0)

    def test_safari_client_uses_authenticated_fetch_and_real_image_element(self):
        self.assertIn("const remoteImage=document.getElementById('remoteScreen')", MOBILE_HTML)
        self.assertIn("headers:H(),cache:'no-store'", MOBILE_HTML)
        self.assertIn("framesRequested++", MOBILE_HTML); self.assertIn("lastHttpStatus=r.status", MOBILE_HTML)
        self.assertIn("monitor_id='+encodeURIComponent(display.value)", MOBILE_HTML)
        self.assertNotIn("screen.clientWidth", MOBILE_HTML); self.assertNotIn("screen.onload", MOBILE_HTML)
        self.assertIn("(1000/targetFps)-(performance.now()-cycle)", MOBILE_HTML)

    @patch.object(PhoneRemoteService, "local_ipv4", return_value="127.0.0.1")
    def test_trusted_credential_exchanges_for_session_and_revocation_is_immediate(self, _address):
        self.service.start(); pairing = self.service.begin_pairing()
        paired = self.service.complete_pairing(pairing["code"], "Martin phone", "127.0.0.1")
        jar = http.cookiejar.CookieJar(); opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        session_request = urllib.request.Request(self.service.address + "/api/session", data=b"{}",
            headers={"Authorization": "Bearer " + paired["token"], "Content-Type": "application/json"})
        session = json.loads(opener.open(session_request).read()); self.assertTrue(session["trusted"])
        self.assertEqual(session["device_name"], "Martin phone")
        status = json.loads(opener.open(self.service.address + "/api/status").read())
        self.assertTrue(status["online"])
        self.assertTrue(self.service.revoke(paired["device_id"]))
        with self.assertRaises(urllib.error.HTTPError) as revoked: opener.open(self.service.address + "/api/status")
        self.assertEqual(revoked.exception.code, 401)

    @patch.object(PhoneRemoteService, "local_ipv4", return_value="127.0.0.1")
    def test_trusted_credential_survives_server_restart(self, _address):
        self.service.start(); pairing = self.service.begin_pairing()
        jar = http.cookiejar.CookieJar(); opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        request = urllib.request.Request(self.service.address + "/api/pair",
            data=json.dumps({"code": pairing["code"], "device_name": "Persistent phone", "trust_device": True}).encode(),
            headers={"Content-Type": "application/json"})
        self.assertTrue(json.loads(opener.open(request).read())["success"])
        self.assertIn("martin_trusted", {cookie.name for cookie in jar}); self.assertIn("martin_session", {cookie.name for cookie in jar})
        self.service.stop()
        callbacks = self.service.callbacks
        restarted = PhoneRemoteService(callbacks, settings={"port": 0}, state_file=self.service.state_file)
        self.addCleanup(restarted.stop); restarted.start()
        request = urllib.request.Request(restarted.address + "/api/session", data=b"{}", headers={"Content-Type": "application/json"})
        restored = json.loads(opener.open(request).read())
        self.assertTrue(restored["success"]); self.assertEqual(restored["device_name"], "Persistent phone")
        self.assertEqual(restored["permissions"], DEFAULT_DEVICE_PERMISSIONS)

    def test_mobile_page_bootstraps_trusted_session_and_can_forget_device(self):
        self.assertIn("async function boot()", MOBILE_HTML); self.assertIn("/api/session", MOBILE_HTML)
        self.assertIn("Trust this device", MOBILE_HTML); self.assertIn("FORGET THIS DEVICE", MOBILE_HTML)
        self.assertIn("localStorage.removeItem('martinToken')", MOBILE_HTML)
        self.assertIn("TRUSTED DEVICE", MOBILE_HTML)

    @patch.object(PhoneRemoteService, "local_ipv4", return_value="127.0.0.1")
    def test_pwa_manifest_service_worker_icons_and_offline_shell(self, _address):
        self.service.start()
        manifest_response = urllib.request.urlopen(self.service.address + "/manifest.webmanifest")
        manifest = json.loads(manifest_response.read()); self.assertEqual(manifest_response.headers.get_content_type(), "application/manifest+json")
        self.assertEqual(manifest["name"], "Martin AI"); self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/"); self.assertEqual(manifest["scope"], "/")
        self.assertEqual({row["sizes"] for row in manifest["icons"]}, {"192x192", "512x512"})
        for path in ("/pwa/martin-ai-180.png", "/pwa/martin-ai-192.png", "/pwa/martin-ai-512.png"):
            response = urllib.request.urlopen(self.service.address + path); self.assertEqual(response.headers.get_content_type(), "image/png")
            self.assertGreater(len(response.read()), 1000)
        worker = urllib.request.urlopen(self.service.address + "/service-worker.js")
        self.assertEqual(worker.headers["Service-Worker-Allowed"], "/")
        script = worker.read().decode(); self.assertIn("/api/", script); self.assertIn("return", script)
        self.assertNotIn("/api/status", script); self.assertNotIn("/api/desktop/frame", script)
        offline = urllib.request.urlopen(self.service.address + "/offline").read().decode()
        self.assertIn("MARTIN PC: OFFLINE", offline); self.assertIn("WAKE MARTIN PC", offline)

    def test_mobile_shell_has_ios_pwa_metadata_safe_areas_and_standalone_detection(self):
        for value in ("manifest.webmanifest", "apple-touch-icon", "apple-mobile-web-app-capable", "viewport-fit=cover",
                      "safe-area-inset-top", "safe-area-inset-bottom", "display-mode: standalone", "navigator.standalone",
                      "serviceWorker.register('/service-worker.js'", "INSTALL MARTIN AI"):
            self.assertIn(value, MOBILE_HTML)

    def test_pair_button_has_explicit_listener_visible_state_and_no_named_element_collision(self):
        self.assertIn("id=pairButton type=button", MOBILE_HTML); self.assertNotIn("onclick=pair()", MOBILE_HTML)
        self.assertIn("pairButton.addEventListener('click',submitPairing)", MOBILE_HTML)
        self.assertIn("pairButton.textContent='PAIRING...'", MOBILE_HTML); self.assertIn("pairButton.textContent='PAIR'", MOBILE_HTML)
        self.assertIn("Device paired successfully.", MOBILE_HTML); self.assertIn("Unable to contact Martin PC.", MOBILE_HTML)
        self.assertIn("new URL('/api/pair',window.location.origin)", MOBILE_HTML); self.assertIn("cache:'no-store'", MOBILE_HTML)
        self.assertIn("response.status", MOBILE_HTML); self.assertIn(f"Remote UI v{REMOTE_UI_VERSION}", MOBILE_HTML)
        self.assertNotIn("async function pair()", MOBILE_HTML); self.assertNotIn("pair.hidden", MOBILE_HTML)
        self.assertIn("boot();async function boot()", MOBILE_HTML); self.assertNotIn("boot()async function boot()", MOBILE_HTML)
        self.assertIn("martin-ai-shell-v3", SERVICE_WORKER_JS.decode())

    @patch.object(PhoneRemoteService, "local_ipv4", return_value="127.0.0.1")
    def test_pair_endpoint_reports_invalid_expired_used_and_success_states(self, _address):
        self.service.start(); pairing = self.service.begin_pairing(); base = self.service.address
        def pair(code, name="Martin iPhone App"):
            request = urllib.request.Request(base + "/api/pair", data=json.dumps({"code": code, "device_name": name, "trust_device": True}).encode(),
                                             headers={"Content-Type": "application/json"})
            try: response = urllib.request.urlopen(request); return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc: return exc.code, json.loads(exc.read())
        status, invalid = pair("000000"); self.assertEqual(status, 403); self.assertEqual(invalid["error"], "Invalid pairing code.")
        status, accepted = pair(pairing["code"]); self.assertEqual(status, 200); self.assertTrue(accepted["success"])
        self.assertEqual(len(accepted["device_id"]), 16)
        status, used = pair(pairing["code"]); self.assertEqual(status, 409); self.assertIn("already been used", used["error"])
        expired_pairing = self.service.begin_pairing(); self.service.pairing["expires"] = time.time() - 1
        status, expired = pair(expired_pairing["code"]); self.assertEqual(status, 410); self.assertIn("expired", expired["error"])
        debug = self.service.pairing_debug(); self.assertIn("Phone Remote Server: RUNNING", debug)
        self.assertIn("Pairing Requests Received: 4", debug); self.assertIn("Pairing Code: EXPIRED", debug)

    def test_token_bucket_is_smooth_and_separate_per_device_and_category(self):
        limiter = RateLimiter()
        with patch("martin_ai.phone_remote.time.monotonic", side_effect=[0, 0, 0, 0, 0, 1, 1]):
            self.assertTrue(limiter.allow("one", "normal_api", 1, 2)); self.assertTrue(limiter.allow("one", "normal_api", 1, 2))
            self.assertFalse(limiter.allow("one", "normal_api", 1, 2))
            self.assertTrue(limiter.allow("two", "normal_api", 1, 2)); self.assertTrue(limiter.allow("one", "screen_frames", 1, 2))
            self.assertTrue(limiter.allow("one", "normal_api", 1, 2)); self.assertFalse(limiter.allow("one", "normal_api", 1, 2))

    @patch.object(PhoneRemoteService, "interactive_desktop", return_value=True)
    @patch("martin_ai.phone_remote.ctypes.windll")
    def test_rapid_mouse_movement_is_silently_coalesced_not_an_error(self, _winapi, _desktop):
        device = {"id": "fast-phone", "name": "Phone", "permissions": {**DEFAULT_DEVICE_PERMISSIONS,
                  "view_screen": True, "remote_input": True}}
        self.service.settings["allow_remote_input"] = True; self.service.control_device_id = "fast-phone"; self.service.control_last_input = time.monotonic()
        results = [self.service.handle_input({"type": "move", "dx": 1, "dy": 1, "sequence": index}, device) for index in range(1, 121)]
        self.assertTrue(all(row["success"] for row in results)); self.assertTrue(any(row.get("dropped") for row in results))
        self.assertGreater(self.service.stream_stats["mouse_coalesced"], 0)
        self.assertEqual(self.service.stream_stats["last_rate_limited_endpoint"], "/api/input:move")

    @patch.object(PhoneRemoteService, "local_ipv4", return_value="127.0.0.1")
    def test_screen_frame_limit_drops_with_204_and_never_uses_normal_api_bucket(self, _address):
        from PIL import Image
        self.service.capture_provider = lambda *_bbox: Image.new("RGB", (320, 180), "#782020")
        self.service.start(); pairing = self.service.begin_pairing(); paired = self.service.complete_pairing(pairing["code"], "Phone", "127.0.0.1")
        permissions = {**DEFAULT_DEVICE_PERMISSIONS, "view_screen": True}; self.service.update_permissions(paired["device_id"], permissions)
        for _ in range(45): self.service.limiter.allow(paired["device_id"], "screen_frames", 0, 45)
        request = urllib.request.Request(self.service.address + "/api/desktop/frame",
            headers={"Authorization": "Bearer " + paired["token"]})
        response = urllib.request.urlopen(request); self.assertEqual(response.status, 204)
        self.assertEqual(response.headers["X-Martin-Frame-Dropped"], "rate-limit")
        self.assertEqual(self.service.stream_stats["frame_rate_drops"], 1)

    def test_client_polling_is_sequential_fps_aware_and_pauses_when_hidden(self):
        self.assertIn("targetFps=j.target_fps||8", MOBILE_HTML)
        self.assertIn("(1000/targetFps)-(performance.now()-cycle)", MOBILE_HTML)
        self.assertIn("await fetchFrame(false)", MOBILE_HTML); self.assertIn("if(document.hidden)", MOBILE_HTML)
        self.assertIn("r.status===204", MOBILE_HTML); self.assertIn("frameRateDrops++", MOBILE_HTML)
        self.assertIn("if(streaming)return", MOBILE_HTML)

    @patch.object(PhoneRemoteService, "local_ipv4", return_value="127.0.0.1")
    def test_forget_clears_session_and_trusted_cookies(self, _address):
        self.service.start(); pairing = self.service.begin_pairing()
        jar = http.cookiejar.CookieJar(); opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        pair = urllib.request.Request(self.service.address + "/api/pair",
            data=json.dumps({"code": pairing["code"], "device_name": "Phone", "trust_device": True}).encode(),
            headers={"Content-Type": "application/json"})
        opener.open(pair).read(); self.assertIn("martin_trusted", {cookie.name for cookie in jar})
        forget = urllib.request.Request(self.service.address + "/api/session/forget", data=b"{}", headers={"Content-Type": "application/json"})
        self.assertTrue(json.loads(opener.open(forget).read())["success"])
        self.assertNotIn("martin_trusted", {cookie.name for cookie in jar}); self.assertNotIn("martin_session", {cookie.name for cookie in jar})
        with self.assertRaises(urllib.error.HTTPError) as denied:
            opener.open(urllib.request.Request(self.service.address + "/api/session", data=b"{}", headers={"Content-Type": "application/json"}))
        self.assertEqual(denied.exception.code, 401)

    @patch.object(PhoneRemoteService, "local_ipv4", return_value="127.0.0.1")
    def test_delete_device_removes_registration_sessions_and_token_access(self, _address):
        self.service.start(); first_pairing = self.service.begin_pairing()
        first = self.service.complete_pairing(first_pairing["code"], "Martin phone", "127.0.0.1")
        device = next(row for row in self.service.devices if row["id"] == first["device_id"])
        session = self.service.create_session(first["device_id"]); self.service.control_device_id = first["device_id"]
        self.service.control_device_name = device["name"]; self.service.viewers[first["device_id"]] = time.monotonic()
        self.service.monitor_selections[first["device_id"]] = "monitor-test"; self.service.input_sequences[first["device_id"]] = 4
        self.assertTrue(self.service.delete_device(first["device_id"])); self.assertFalse(self.service.devices)
        self.assertFalse(self.service.sessions); self.assertIsNone(self.service.control_device_id)
        self.assertNotIn(first["device_id"], self.service.viewers); self.assertNotIn(first["device_id"], self.service.monitor_selections)
        denied = urllib.request.Request(self.service.address + "/api/status", headers={"Authorization": "Bearer " + first["token"]})
        with self.assertRaises(urllib.error.HTTPError) as old_token: urllib.request.urlopen(denied)
        self.assertEqual(old_token.exception.code, 401)
        second_pairing = self.service.begin_pairing()
        second = self.service.complete_pairing(second_pairing["code"], "Martin phone", "127.0.0.1")
        self.assertTrue(second["success"]); self.assertNotEqual(second["device_id"], first["device_id"])

    def test_rename_preserves_identity_permissions_and_duplicate_cleanup_keeps_newest(self):
        old_pairing = self.service.begin_pairing(); old = self.service.complete_pairing(old_pairing["code"], "Martin phone", "192.168.1.2")
        new_pairing = self.service.begin_pairing(); new = self.service.complete_pairing(new_pairing["code"], "  martin   PHONE ", "192.168.1.3")
        other_pairing = self.service.begin_pairing(); other = self.service.complete_pairing(other_pairing["code"], "Martin iPad", "192.168.1.4")
        permissions = {**DEFAULT_DEVICE_PERMISSIONS, "view_screen": True, "remote_input": True}
        self.assertTrue(self.service.update_permissions(new["device_id"], permissions))
        self.assertTrue(self.service.rename_device(new["device_id"], "Martin iPhone PWA"))
        renamed = next(row for row in self.service.devices if row["id"] == new["device_id"])
        self.assertEqual(renamed["permissions"], permissions); self.assertEqual(renamed["name"], "Martin iPhone PWA")
        self.assertEqual(new["device_id"], renamed["id"])
        self.assertFalse(self.service.duplicate_device_groups())
        self.assertTrue(self.service.rename_device(new["device_id"], "Martin phone"))
        groups = self.service.duplicate_device_groups(); self.assertEqual(len(groups), 1); self.assertEqual(len(groups[0]), 2)
        removed = self.service.remove_duplicate_devices(); self.assertEqual(removed, [old["device_id"]])
        self.assertEqual({row["id"] for row in self.service.devices}, {new["device_id"], other["device_id"]})

    def test_revoke_is_distinct_from_delete_and_clear_all_removes_every_registration(self):
        first_pairing = self.service.begin_pairing(); first = self.service.complete_pairing(first_pairing["code"], "Phone", "192.168.1.2")
        second_pairing = self.service.begin_pairing(); second = self.service.complete_pairing(second_pairing["code"], "Phone", "192.168.1.3")
        self.assertTrue(self.service.revoke(first["device_id"])); self.assertEqual(len(self.service.devices), 2)
        self.assertTrue(next(row for row in self.service.devices if row["id"] == first["device_id"])["revoked"])
        self.assertEqual(self.service.clear_devices(), 2); self.assertFalse(self.service.devices); self.assertFalse(self.service.sessions)

    @patch.object(PhoneRemoteService, "local_ipv4", return_value="127.0.0.1")
    def test_remote_desktop_self_test_reports_each_pipeline_stage(self, _address):
        from PIL import Image
        self.service.capture_provider = lambda *_bbox: Image.new("RGB", (640, 360), "#802020")
        self.service.start()
        report = self.service.remote_desktop_self_test()
        for stage in range(1, 10): self.assertIn(f"{stage}.", report)
        self.assertIn("Frame successfully encoded: PASS", report)
        self.assertIn("Saved frame verification: PASS", report)

    def test_mobile_v2_has_dashboard_navigation_reconnect_and_companion_pages(self):
        for text in ("bottomNav", "Live Dashboard" if "Live Dashboard" in MOBILE_HTML else "storageFree",
                     "remotePage", "skillsPage", "activityPage", "settingsPage", "reliablePoll",
                     "Reconnecting", "loadCrashes", "loadDevices", "notificationPreferences"):
            self.assertIn(text, MOBILE_HTML)
        self.assertIn("Math.min(30000", MOBILE_HTML)

    def test_selected_monitor_persists_per_trusted_device(self):
        device = {"id": "phone", "name": "Phone", "permissions": dict(DEFAULT_DEVICE_PERMISSIONS)}
        monitor = {"id": "two", "number": 2, "label": "Monitor 2", "x": 1920, "y": 0,
                   "width": 1920, "height": 1080, "primary": False}
        with patch.object(self.service, "refresh_monitors", side_effect=lambda **kwargs: self.service.monitors):
            self.service.monitors = [monitor]
            self.assertTrue(self.service.select_monitor(device, "two")["success"])
            self.service.monitor_selections.clear()
            self.assertEqual(self.service.selected_monitor(device)["id"], "two")

    def test_notification_preferences_throttle_read_and_clear(self):
        device = {"id": "phone", "notification_preferences": {}}
        self.assertTrue(self.service.add_notification("Crash", "Game crashed", "Graphics Driver", key="crash:game"))
        self.assertFalse(self.service.add_notification("Crash", "Game crashed", "Graphics Driver", key="crash:game"))
        payload = self.service.notification_payload(device); self.assertEqual(payload["unread"], 1)
        self.service.update_notifications(device, {"operation": "read_all"}); self.assertEqual(self.service.notification_payload(device)["unread"], 0)
        self.service.update_notifications(device, {"operation": "preferences", "preferences": {"Crash": False}})
        self.assertEqual(self.service.notification_payload(device)["notifications"], [])
        self.service.update_notifications(device, {"operation": "clear"}); self.assertFalse(self.service.notifications)

    def test_trusted_devices_payload_does_not_expose_credentials(self):
        pairing = self.service.begin_pairing(); paired = self.service.complete_pairing(pairing["code"], "Phone", "192.168.1.2")
        payload = self.service.trusted_devices_payload(); encoded = json.dumps(payload)
        self.assertIn(paired["device_id"], encoded); self.assertNotIn("token_hash", encoded); self.assertNotIn(paired["token"], encoded)

    @patch.object(PhoneRemoteService, "local_ipv4", return_value="127.0.0.1")
    def test_authenticated_mobile_dashboard_skills_crashes_and_notifications_api(self, _address):
        self.service.callbacks.mobile = lambda resource, body, device: {"success": True, "resource": resource, "body": body, "device": device["id"]}
        self.service.start(); pairing = self.service.begin_pairing(); paired = self.service.complete_pairing(pairing["code"], "Phone", "127.0.0.1")
        headers = {"Authorization": "Bearer " + paired["token"], "Content-Type": "application/json"}
        for resource in ("dashboard", "skills", "crashes", "apps"):
            response = urllib.request.urlopen(urllib.request.Request(self.service.address + "/api/mobile/" + resource, headers=headers))
            self.assertEqual(json.loads(response.read())["resource"], resource)
        self.service.add_notification("Crash", "Game crashed", "Graphics Driver")
        activity = urllib.request.urlopen(urllib.request.Request(self.service.address + "/api/mobile/notifications", headers=headers))
        self.assertEqual(len(json.loads(activity.read())["notifications"]), 1)
        devices = urllib.request.urlopen(urllib.request.Request(self.service.address + "/api/mobile/devices", headers=headers))
        self.assertEqual(json.loads(devices.read())["devices"][0]["name"], "Phone")


if __name__ == "__main__": unittest.main()
