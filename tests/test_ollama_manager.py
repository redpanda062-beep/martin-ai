from __future__ import annotations

import unittest
import threading
import json
import io
import urllib.error
from unittest.mock import MagicMock, patch

from martin_ai.ollama_manager import OllamaCancelled, OllamaConfig, OllamaManager, OllamaState


class OllamaManagerTests(unittest.TestCase):
    def test_localhost_is_normalized_to_ipv4(self) -> None:
        manager = OllamaManager(OllamaConfig())
        manager.configure(base_url="http://localhost:11434", model="wanted:latest")
        self.assertEqual(manager.config.base_url, "http://127.0.0.1:11434")

    def test_configured_model_or_installed_fallback_is_selected(self) -> None:
        self.assertEqual(OllamaManager.choose_model("qwen3:8b", ["qwen3:8b", "other:latest"]), "qwen3:8b")
        self.assertEqual(OllamaManager.choose_model("missing", ["available:latest"]), "available:latest")
        self.assertIsNone(OllamaManager.choose_model("missing", []))

    def test_connected_no_models_and_api_failure_states(self) -> None:
        manager = OllamaManager(OllamaConfig(model="wanted"))
        with patch.object(manager, "_tags", return_value=[]), patch.object(manager, "find_executable", return_value="ollama.exe"):
            self.assertEqual(manager.check(force=True).status, "no_models")
        manager.state = OllamaState("connected", True, "ollama.exe", ["wanted"], "wanted", "Connected")
        manager._failures = 0
        manager._last_start = 10**20
        with patch.object(manager, "_tags", side_effect=OSError("temporary")), patch.object(manager, "find_executable", return_value="ollama.exe"):
            state = manager.check(force=True)
        self.assertEqual(state.status, "offline")

    def test_start_is_single_flight(self) -> None:
        manager = OllamaManager(OllamaConfig())
        with patch("martin_ai.ollama_manager.Path.is_file", return_value=True), \
             patch("martin_ai.ollama_manager.subprocess.Popen") as popen:
            self.assertTrue(manager._start("ollama.exe"))
            self.assertFalse(manager._start("ollama.exe"))
        popen.assert_called_once()

    def test_chat_uses_streaming_http_without_spawning_process(self) -> None:
        manager = OllamaManager(OllamaConfig(model="qwen3:8b"))
        response = MagicMock()
        response.__enter__.return_value = response
        response.readline.side_effect = [
            json.dumps({"message": {"content": "Tokyo"}, "done": False}).encode() + b"\n",
            json.dumps({"message": {"content": "."}, "done": True}).encode() + b"\n",
        ]
        with patch.object(manager, "require_model", return_value=("http://127.0.0.1:11434", "qwen3:8b")), \
             patch("martin_ai.ollama_manager.urllib.request.urlopen", return_value=response), \
             patch("martin_ai.ollama_manager.subprocess.Popen") as popen:
            answer = manager.chat([{"role": "user", "content": "capital?"}],
                                  base_url="http://127.0.0.1:11434", model="qwen3:8b")
        self.assertEqual(answer, "Tokyo.")
        popen.assert_not_called()

    def test_cancelled_chat_does_not_stop_server(self) -> None:
        manager = OllamaManager(OllamaConfig(model="qwen3:8b"))
        cancelled = threading.Event(); cancelled.set()
        response = MagicMock(); response.__enter__.return_value = response
        with patch.object(manager, "require_model", return_value=("http://127.0.0.1:11434", "qwen3:8b")), \
             patch("martin_ai.ollama_manager.urllib.request.urlopen", return_value=response), \
             patch.object(manager, "_start") as start:
            with self.assertRaises(OllamaCancelled):
                manager.chat([{"role": "user", "content": "hello"}],
                             base_url="http://127.0.0.1:11434", model="qwen3:8b", cancel_event=cancelled)
        start.assert_not_called()

    def test_gpu_out_of_memory_retries_on_cpu(self) -> None:
        manager = OllamaManager(OllamaConfig(model="qwen3:8b"))
        error = urllib.error.HTTPError("http://local/api/chat", 500, "error", {},
                                      io.BytesIO(b'{"error":"cudaMalloc failed: out of memory"}'))
        response = MagicMock(); response.__enter__.return_value = response
        response.readline.side_effect = [json.dumps({"message": {"content": "Tokyo"}, "done": True}).encode() + b"\n"]
        requests = []
        def open_request(request, timeout):
            requests.append(json.loads(request.data.decode()))
            if len(requests) == 1: raise error
            return response
        with patch.object(manager, "require_model", return_value=("http://127.0.0.1:11434", "qwen3:8b")), \
             patch("martin_ai.ollama_manager.urllib.request.urlopen", side_effect=open_request):
            answer = manager.chat([{"role": "user", "content": "capital?"}],
                                  base_url="http://127.0.0.1:11434", model="qwen3:8b")
        self.assertEqual(answer, "Tokyo")
        self.assertNotIn("num_gpu", requests[0]["options"])
        self.assertEqual(requests[1]["options"]["num_gpu"], 0)

    def test_start_cleans_orphaned_ollama_runners(self) -> None:
        manager = OllamaManager(OllamaConfig())
        with patch("martin_ai.ollama_manager.Path.is_file", return_value=True), \
             patch.object(manager, "_cleanup_orphaned_runners", return_value=1) as cleanup, \
             patch("martin_ai.ollama_manager.subprocess.Popen"):
            self.assertTrue(manager._start(r"C:\Ollama\ollama.exe"))
        cleanup.assert_called_once_with(r"C:\Ollama\ollama.exe")

    def test_reconnect_does_not_restart_a_healthy_api(self) -> None:
        manager = OllamaManager(OllamaConfig(model="qwen3:8b"))
        with patch.object(manager, "_tags", return_value=["qwen3:8b"]), \
             patch.object(manager, "start_ollama") as start:
            state = manager.reconnect()
        self.assertEqual(state.status, "connected")
        start.assert_not_called()

    def test_restart_stops_starts_and_verifies_api(self) -> None:
        manager = OllamaManager(OllamaConfig(model="qwen3:8b"))
        with patch.object(manager, "stop_ollama") as stop, \
             patch.object(manager, "start_ollama", return_value=OllamaState("connected", True, "ollama.exe", ["qwen3:8b"], "qwen3:8b")) as start:
            state = manager.restart_ollama()
        stop.assert_called_once_with(); start.assert_called_once_with()
        self.assertEqual(state.status, "connected")

    def test_command_reports_configured_model_missing_without_claiming_success(self) -> None:
        from martin_ai.router import execute_ollama_action
        manager = OllamaManager(OllamaConfig(model="qwen3:8b"))
        with patch.object(manager, "get_ollama_status", return_value=OllamaState(
                "connected", True, "ollama.exe", ["other:latest"], "other:latest")):
            _state, message = execute_ollama_action("status", manager)
        self.assertEqual(message, "Ollama is running, but qwen3:8b is not currently available.")

    def test_start_uses_argument_array_and_hidden_process_settings(self) -> None:
        manager = OllamaManager(OllamaConfig())
        with patch("martin_ai.ollama_manager.Path.is_file", return_value=True), \
             patch("martin_ai.ollama_manager.hidden_process_kwargs", return_value={"creationflags": 8}), \
             patch("martin_ai.ollama_manager.subprocess.Popen") as popen:
            self.assertTrue(manager._start(r"C:\Ollama\ollama.exe", force=True))
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [r"C:\Ollama\ollama.exe", "serve"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["creationflags"], 8)


if __name__ == "__main__":
    unittest.main()
