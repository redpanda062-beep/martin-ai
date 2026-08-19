from __future__ import annotations

import socket
import time
from typing import Any

import psutil

from ..game_monitor import cpu_temperature, gpu_status


class SystemMonitor:
    def __init__(self) -> None:
        self._last_network = psutil.net_io_counters()
        self._last_network_at = time.monotonic()

    def collect(self) -> dict[str, Any]:
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("C:\\")
        current = psutil.net_io_counters()
        now = time.monotonic()
        elapsed = max(now - self._last_network_at, 0.001)
        network = {
            "sent_mbps": round((current.bytes_sent - self._last_network.bytes_sent) * 8 / elapsed / 1_000_000, 2),
            "received_mbps": round((current.bytes_recv - self._last_network.bytes_recv) * 8 / elapsed / 1_000_000, 2),
        }
        self._last_network, self._last_network_at = current, now
        gpu = gpu_status()
        return {
            "cpu_percent": psutil.cpu_percent(None),
            "cpu_temperature_c": cpu_temperature(),
            "ram_percent": memory.percent,
            "ram_available_gb": round(memory.available / 2**30, 2),
            "disk_percent": disk.percent,
            "disk_free_gb": round(disk.free / 2**30, 2),
            "network": network,
            "gpu": gpu,
        }

    @staticmethod
    def connectivity(host: str = "1.1.1.1", port: int = 53, timeout: float = 1.5) -> dict[str, Any]:
        started = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return {"connected": True, "latency_ms": round((time.monotonic() - started) * 1000, 1)}
        except OSError as exc:
            return {"connected": False, "error": str(exc)}
