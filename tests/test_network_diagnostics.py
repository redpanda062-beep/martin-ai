from __future__ import annotations

import unittest
from unittest.mock import patch

from martin_ai.actions import ActionManager
from martin_ai.intent import Intent, detect_intent
from martin_ai.router import route_command
from martin_ai.tools.diagnostics import _analyze_network_performance, _ping, run_network_diagnostic


class NetworkIntentTests(unittest.TestCase):
    def test_network_requests_have_distinct_semantic_intents(self):
        cases = {
            "am i connected to the internet": Intent.NETWORK_STATUS,
            "run a full network diagnostic": Intent.NETWORK_DIAGNOSTIC,
            "check my ping": Intent.NETWORK_LATENCY,
            "do i have packet loss": Intent.NETWORK_PACKET_LOSS,
            "check my DNS": Intent.NETWORK_DNS,
            "why is my internet slow": Intent.NETWORK_PERFORMANCE_INVESTIGATION,
            "my internet feels slow. investigate why, but don't make changes": Intent.NETWORK_PERFORMANCE_INVESTIGATION,
            "my online game is lagging": Intent.NETWORK_PERFORMANCE_INVESTIGATION,
            "my game is lagging": Intent.GAMING,
            "run a full network diagnostic and don't make any changes": Intent.NETWORK_DIAGNOSTIC,
            "fix my internet but don't make any changes": Intent.NETWORK_DIAGNOSTIC,
            "check my internet connection for problems. dont make any changes yet": Intent.NETWORK_DIAGNOSTIC,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(detect_intent(text), expected)

    def test_router_selects_the_specific_read_only_tool(self):
        calls = []
        tools = {
            "network_status": lambda: calls.append("status") or {"success": True, "connected": True},
            "run_network_diagnostic": lambda: calls.append("diagnostic") or {
                "success": True, "issue": "healthy", "summary": "Measured healthy result", "changes_made": False,
                "adapter": {"state": "Up", "connection_type": "Ethernet"}, "ip": {"valid": True},
                "gateway": {"reachable": True}, "dns": {"resolved": True}, "internet": {"reachable": True},
                "latency_ms": 12.0, "packet_loss_percent": 0.0,
            },
            "measure_network_latency": lambda: calls.append("latency") or {
                "success": True, "reachable": True, "latency_ms": 12.0, "packet_loss_percent": 0.0,
            },
            "measure_network_jitter": lambda: calls.append("jitter") or {
                "success": True, "reachable": True, "latency_ms": 12.0, "jitter_ms": 2.0,
                "packet_loss_percent": 0.0, "sample_count": 8,
            },
            "investigate_network_performance": lambda: calls.append("performance") or {
                "success": True, "issue": "no_obvious_cause", "summary": "No obvious cause detected.",
                "gateway_latency": {"latency_ms": 2.0},
                "external_latency": {"latency_ms": 20.0, "jitter_ms": 2.0, "packet_loss_percent": 0.0},
                "dns": {"resolution_ms": 5.0}, "wifi": {"quality": "Strong"},
                "adapter": {"link_speed": "500 Mbps"}, "utilization": {},
                "process_bandwidth": {"supported": False}, "changes_made": False,
            },
            "check_dns": lambda: calls.append("dns") or {
                "success": True, "resolved": True, "resolution_ms": 4.0,
            },
        }
        manager = ActionManager({"danger": lambda: calls.append("repair") or {"success": True}})
        with patch.dict("martin_ai.router.READ_ONLY_TOOLS", tools, clear=False):
            route_command("am i connected to the internet", manager)
            route_command("run a full network diagnostic", manager)
            route_command("check my ping", manager)
            route_command("do i have packet loss", manager)
            route_command("check my DNS", manager)
            route_command("why is my internet slow", manager)
            last = route_command("run a full network diagnostic and don't make any changes", manager)
        self.assertEqual(calls, ["status", "diagnostic", "latency", "jitter", "dns", "performance", "diagnostic"])
        self.assertNotIn("repair", calls)
        self.assertFalse(last.raw_result["changes_made"])


class NetworkPipelineTests(unittest.TestCase):
    def test_performance_analysis_distinguishes_local_and_external_delay(self):
        config = {"success": True, "name": "Wi-Fi"}
        dns = {"resolved": True, "resolution_ms": 20.0}
        wifi = {"signal_percent": 80.0}
        utilization = {"combined_link_utilization_percent": 2.0}
        local = _analyze_network_performance(
            config, {"latency_ms": 35.0, "jitter_ms": 12.0, "packet_loss_percent": 0.0},
            {"latency_ms": 45.0, "jitter_ms": 13.0, "packet_loss_percent": 0.0}, dns, wifi, utilization)
        external = _analyze_network_performance(
            config, {"latency_ms": 2.0, "jitter_ms": 1.0, "packet_loss_percent": 0.0},
            {"latency_ms": 110.0, "jitter_ms": 25.0, "packet_loss_percent": 0.0}, dns, wifi, utilization)
        self.assertEqual(local[0], "possible_local_network_issue")
        self.assertEqual(external[0], "possible_external_issue")

    @patch("martin_ai.tools.diagnostics._run")
    def test_jitter_is_calculated_from_multiple_real_samples(self, run):
        run.return_value = {"success": True, "stdout": (
            "Reply from 1.1.1.1: time=10ms\nReply from 1.1.1.1: time=14ms\n"
            "Reply from 1.1.1.1: time=12ms\nReply from 1.1.1.1: time=18ms\n"
            "Packets: Sent = 4, Received = 4, Lost = 0 (0% loss),\n"
            "Minimum = 10ms, Maximum = 18ms, Average = 13ms")}
        result = _ping("1.1.1.1", 4)
        self.assertEqual(result["latency_ms"], 13.5)
        self.assertEqual(result["jitter_ms"], 4.0)
        self.assertEqual(result["packet_loss_percent"], 0.0)
        self.assertEqual(len(result["samples_ms"]), 4)

    @patch("martin_ai.tools.diagnostics.check_internet_connectivity")
    @patch("martin_ai.tools.diagnostics.check_dns")
    @patch("martin_ai.tools.diagnostics.check_gateway")
    @patch("martin_ai.tools.diagnostics._network_configuration")
    def test_full_pipeline_does_not_stop_at_active_adapter(self, config, gateway, dns, internet):
        config.return_value = {"success": True, "name": "Wi-Fi", "state": "Up", "media_type": "Native 802.11",
                               "ipv4": "192.168.1.20", "gateway": "192.168.1.1", "dns_servers": ["1.1.1.1"],
                               "link_speed": "866 Mbps"}
        gateway.return_value = {"success": True, "gateway": "192.168.1.1", "reachable": True, "latency_ms": 2.0, "packet_loss_percent": 0.0}
        dns.return_value = {"success": False, "resolved": False, "dns_servers": ["1.1.1.1"]}
        internet.return_value = {"success": True, "reachable": True, "latency_ms": 20.0, "packet_loss_percent": 0.0}
        result = run_network_diagnostic()
        self.assertEqual(result["issue"], "dns_failure")
        self.assertFalse(result["changes_made"])
        gateway.assert_called_once(); dns.assert_called_once(); internet.assert_called_once()

    @patch("martin_ai.tools.diagnostics.check_internet_connectivity")
    @patch("martin_ai.tools.diagnostics.check_dns")
    @patch("martin_ai.tools.diagnostics.check_gateway")
    @patch("martin_ai.tools.diagnostics._network_configuration")
    def test_pipeline_identifies_gateway_failure(self, config, gateway, dns, internet):
        config.return_value = {"success": True, "name": "Ethernet", "state": "Up", "media_type": "802.3",
                               "ipv4": "10.0.0.8", "gateway": "10.0.0.1"}
        gateway.return_value = {"success": False, "gateway": "10.0.0.1", "reachable": False}
        dns.return_value = {"success": False, "resolved": False}
        internet.return_value = {"success": False, "reachable": False}
        result = run_network_diagnostic()
        self.assertEqual(result["issue"], "gateway_unreachable")
        self.assertIn("router", result["summary"])


if __name__ == "__main__":
    unittest.main()
