from __future__ import annotations

import threading


class RuntimeControl:
    """Thread-safe kill switch and autonomy state shared by every action path."""

    def __init__(self, *, simulation_mode: bool = False, autonomy_enabled: bool = True) -> None:
        self._lock = threading.RLock()
        self._simulation_mode = simulation_mode
        self._autonomy_enabled = autonomy_enabled
        self._emergency_stopped = False
        self._generation = 0

    @property
    def simulation_mode(self) -> bool:
        with self._lock:
            return self._simulation_mode

    @property
    def autonomy_enabled(self) -> bool:
        with self._lock:
            return self._autonomy_enabled

    @property
    def emergency_stopped(self) -> bool:
        with self._lock:
            return self._emergency_stopped

    def set_simulation(self, enabled: bool) -> None:
        with self._lock:
            self._simulation_mode = bool(enabled)

    def set_autonomy(self, enabled: bool) -> None:
        with self._lock:
            self._autonomy_enabled = bool(enabled)

    def stop(self) -> None:
        with self._lock:
            self._emergency_stopped = True
            self._generation += 1

    def resume(self) -> None:
        with self._lock:
            self._emergency_stopped = False

    def action_token(self) -> int:
        with self._lock:
            return self._generation

    def may_start_action(self, token: int | None = None) -> bool:
        with self._lock:
            return not self._emergency_stopped and (token is None or token == self._generation)
