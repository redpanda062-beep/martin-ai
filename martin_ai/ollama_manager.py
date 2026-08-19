from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import psutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from .windows_process import hidden_process_kwargs


LOGGER = logging.getLogger(__name__)


class OllamaCancelled(Exception):
    pass


@dataclass(frozen=True)
class OllamaConfig:
    host: str = "127.0.0.1"
    port: int = 11434
    model: str = ""
    timeout: float = 5.0

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @classmethod
    def from_environment(cls) -> "OllamaConfig":
        legacy = os.getenv("OLLAMA_URL", "").strip()
        parsed = urlparse(legacy if "://" in legacy else f"http://{legacy}") if legacy else None
        host = os.getenv("OLLAMA_HOST", "").strip() or (parsed.hostname if parsed else None) or "127.0.0.1"
        if host.lower() == "localhost": host = "127.0.0.1"
        port = int(os.getenv("OLLAMA_PORT", "") or (parsed.port if parsed else 0) or 11434)
        return cls(host, port, os.getenv("OLLAMA_MODEL", "").strip(), float(os.getenv("OLLAMA_TIMEOUT", "5")))


@dataclass
class OllamaState:
    status: str = "offline"
    installed: bool = False
    executable: str | None = None
    models: list[str] = field(default_factory=list)
    selected_model: str | None = None
    message: str = "Local AI is unavailable."


class OllamaManager:
    def __init__(self, config: OllamaConfig | None = None) -> None:
        self.config = config or OllamaConfig.from_environment()
        self.state = OllamaState()
        self._lock = threading.Lock()
        self._last_check = 0.0
        self._last_start = 0.0
        self._failures = 0
        self._last_repair = 0.0
        self._repair_attempts = 0
        self._last_logged_event = ""
        self._active_response: Any = None
        self._response_lock = threading.Lock()

    def configure(self, *, base_url: str | None = None, model: str | None = None) -> None:
        parsed = urlparse(base_url) if base_url else None
        host = (parsed.hostname if parsed else None) or self.config.host
        if host.lower() == "localhost": host = "127.0.0.1"
        self.config = OllamaConfig(host, (parsed.port if parsed else None) or self.config.port,
                                   self.config.model if model is None else model, self.config.timeout)

    def find_executable(self) -> str | None:
        candidates = [shutil.which("ollama.exe"), shutil.which("ollama"),
                      str(Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"),
                      str(Path(os.getenv("LOCALAPPDATA", "")) / "Ollama" / "ollama.exe"),
                      str(Path(os.getenv("ProgramFiles", "C:\\Program Files")) / "Ollama" / "ollama.exe")]
        return next((path for path in candidates if path and Path(path).is_file()), None)

    def is_ollama_installed(self) -> bool:
        return self.find_executable() is not None

    def is_ollama_running(self) -> bool:
        """Process evidence only; API health is checked separately."""
        for process in psutil.process_iter(["name"]):
            try:
                if (process.info.get("name") or "").lower() == "ollama.exe":
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    def check_ollama_connection(self) -> bool:
        try:
            self._tags()
            return True
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            return False

    def get_loaded_models(self) -> list[str]:
        try:
            return self._tags()
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            return []

    def get_ollama_status(self) -> OllamaState:
        return self.check(auto_start=False, force=True)

    def _log_once(self, event: str, level: int = logging.INFO) -> None:
        if event != self._last_logged_event:
            LOGGER.log(level, event)
            self._last_logged_event = event

    def _tags(self) -> list[str]:
        with urllib.request.urlopen(self.config.base_url + "/api/tags", timeout=self.config.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return [str(item.get("name") or item.get("model")) for item in payload.get("models", []) if item.get("name") or item.get("model")]

    @staticmethod
    def choose_model(configured: str, models: list[str]) -> str | None:
        if not models: return None
        if configured in models: return configured
        base = configured.removesuffix(":latest")
        return next((item for item in models if item.removesuffix(":latest") == base), models[0])

    def _connected(self, models: list[str]) -> OllamaState:
        selected = self.choose_model(self.config.model, models)
        status = "connected" if selected else "no_models"
        message = f"Connected using {selected}." if selected else "Ollama is connected, but no models are installed."
        return OllamaState(status, True, self.find_executable(), models, selected, message)

    def _start(self, executable: str, *, force: bool = False) -> bool:
        if not Path(executable).is_file():
            return False
        if not force and time.monotonic() - self._last_start < 30: return False
        self._last_start = time.monotonic()
        self._cleanup_orphaned_runners(executable)
        try:
            subprocess.Popen([executable, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, **hidden_process_kwargs())
            self._log_once("Ollama server start attempted")
            return True
        except OSError as exc:
            LOGGER.warning("Ollama server start failed: %s", exc)
            return False

    def wait_for_ollama_ready(self, timeout: float = 30.0) -> OllamaState:
        deadline = time.monotonic() + max(timeout, 0.0)
        delay = 0.25
        while time.monotonic() <= deadline:
            try:
                self.state = self._connected(self._tags())
                self._failures = 0
                self._log_once("Ollama API available")
                return self.state
            except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
                time.sleep(delay)
                delay = min(delay * 1.6, 2.0)
        executable = self.find_executable()
        self.state = OllamaState("error", bool(executable), executable,
                                 message="Ollama is running, but its local API isn't responding."
                                 if self.is_ollama_running() else "Ollama did not become ready.")
        return self.state

    def start_ollama(self) -> OllamaState:
        try:
            return self._connected(self._tags())
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            pass
        executable = self.find_executable()
        if not executable:
            self.state = OllamaState("offline", False, message="I couldn't find Ollama installed on this PC.")
            return self.state
        self.state = OllamaState("starting", True, executable, message="Starting Ollama...")
        if not self._start(executable, force=True):
            self.state = OllamaState("error", True, executable, message="Ollama is installed, but I couldn't start the server.")
            return self.state
        return self.wait_for_ollama_ready()

    def stop_ollama(self, timeout: float = 10.0) -> OllamaState:
        stopped = 0
        for process in psutil.process_iter(["name", "exe"]):
            try:
                if (process.info.get("name") or "").lower() != "ollama.exe":
                    continue
                process.terminate(); stopped += 1
                try: process.wait(timeout=max(timeout / 2, 1))
                except psutil.TimeoutExpired: process.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        self.state = OllamaState("offline", self.is_ollama_installed(), self.find_executable(),
                                 message="Ollama stopped." if stopped or not self.check_ollama_connection()
                                 else "I couldn't stop the Ollama server.")
        self._log_once("Ollama server stopped")
        return self.state

    def restart_ollama(self) -> OllamaState:
        self._log_once("Ollama server restart attempted")
        self.stop_ollama()
        return self.start_ollama()

    def reconnect(self) -> OllamaState:
        """Reset Martin AI's connection state without restarting a healthy server."""
        self.state = OllamaState("reconnecting", self.is_ollama_installed(), self.find_executable(),
                                 message="Reconnecting to Ollama...")
        try:
            self.state = self._connected(self._tags())
            self._failures = 0
            self._log_once("Ollama reconnect succeeded")
            return self.state
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            self._log_once("Ollama reconnect failed", logging.WARNING)
            return self.start_ollama()

    @staticmethod
    def _cleanup_orphaned_runners(executable: str) -> int:
        """Remove only stale Ollama-owned runners whose recorded parent no longer exists."""
        ollama_root = Path(executable).resolve().parent
        removed = 0
        for process in psutil.process_iter(["name", "exe", "ppid"]):
            try:
                if (process.info.get("name") or "").lower() != "llama-server.exe": continue
                runner = Path(process.info.get("exe") or "").resolve()
                try: runner.relative_to(ollama_root)
                except ValueError: continue
                parent_pid = int(process.info.get("ppid") or 0)
                if psutil.pid_exists(parent_pid):
                    try:
                        if psutil.Process(parent_pid).name().lower() == "ollama.exe": continue
                    except (psutil.NoSuchProcess, psutil.AccessDenied): pass
                process.terminate()
                try: process.wait(timeout=3)
                except psutil.TimeoutExpired: process.kill()
                removed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return removed

    def check(self, *, auto_start: bool = True, force: bool = False) -> OllamaState:
        """Call from a background worker; checks are throttled and startup is single-flight."""
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_check < 10: return self.state
            self._last_check = now
            executable = self.find_executable()
            try:
                self.state = self._connected(self._tags()); self._failures = 0
                return self.state
            except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
                self._failures += 1
            if auto_start and executable and self._start(executable):
                self.state = OllamaState("starting", True, executable, message="Local AI is starting.")
                for delay in (0.5, 1.0, 1.5, 2.0, 3.0):
                    time.sleep(delay)
                    try:
                        self.state = self._connected(self._tags()); self._failures = 0
                        return self.state
                    except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError): pass
            message = "Ollama is not installed." if not executable else "Local AI is unavailable. Trying to reconnect."
            self.state = OllamaState("offline", bool(executable), executable, message=message)
            self._log_once("Ollama connection lost", logging.WARNING)
            return self.state

    def require_model(self, base_url: str, configured: str) -> tuple[str, str]:
        self.configure(base_url=base_url, model=configured)
        state = self.check(auto_start=True, force=True)
        if state.status == "no_models": raise RuntimeError("Ollama is connected, but no models are installed.")
        if state.status != "connected" or not state.selected_model:
            raise RuntimeError("Local AI isn't available right now. I'm trying to reconnect.")
        return self.config.base_url, state.selected_model

    def cancel_generation(self) -> None:
        with self._response_lock:
            response = self._active_response
        if response is not None:
            try: response.close()
            except OSError: pass

    def chat(self, messages: list[dict[str, str]], *, base_url: str, model: str,
             keep_alive: str = "5m", cancel_event: threading.Event | None = None) -> str:
        """Send chat directly over HTTP and stream it so cancellation stays responsive."""
        base_url, model = self.require_model(base_url, model)
        payload_data = {"model": model, "messages": messages, "stream": True,
                        "think": False, "keep_alive": keep_alive,
                        "options": {"temperature": 0.4}}
        last_error: Exception | None = None
        for attempt in range(2):
            payload = json.dumps(payload_data).encode("utf-8")
            request = urllib.request.Request(base_url + "/api/chat", data=payload,
                                             headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    with self._response_lock: self._active_response = response
                    parts: list[str] = []
                    while True:
                        if cancel_event is not None and cancel_event.is_set(): raise OllamaCancelled()
                        line = response.readline()
                        if not line: break
                        chunk = json.loads(line.decode("utf-8"))
                        content = (chunk.get("message") or {}).get("content")
                        if content: parts.append(str(content))
                        if chunk.get("done"): break
                    if cancel_event is not None and cancel_event.is_set(): raise OllamaCancelled()
                    answer = "".join(parts).strip()
                    if not answer: raise RuntimeError("Ollama returned an empty response.")
                    return answer
            except OllamaCancelled:
                raise
            except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
                if cancel_event is not None and cancel_event.is_set(): raise OllamaCancelled()
                last_error = exc
                if isinstance(exc, urllib.error.HTTPError):
                    try: detail = exc.read().decode("utf-8", errors="replace").lower()
                    except OSError: detail = ""
                    if attempt == 0 and ("cudamalloc" in detail or "out of memory" in detail):
                        # Preserve the selected model but fall back to system RAM when VRAM is busy.
                        payload_data["options"] = {"temperature": 0.4, "num_gpu": 0}
                if attempt == 0: time.sleep(0.5)
            finally:
                with self._response_lock: self._active_response = None
        raise RuntimeError("Local AI couldn't generate a response. Please try again.") from last_error


ollama_manager = OllamaManager()
