from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ..game_monitor import live_game_status
from .process_monitor import foreground_application, running_processes
from .system_monitor import SystemMonitor


LOGGER = logging.getLogger(__name__)


@dataclass
class PerceptionSnapshot:
    observed_at: str
    system: dict[str, Any] = field(default_factory=dict)
    connectivity: dict[str, Any] = field(default_factory=dict)
    foreground: dict[str, Any] | None = None
    processes: list[dict[str, Any]] = field(default_factory=list)
    game: dict[str, Any] | None = None
    errors: dict[str, str] = field(default_factory=dict)


class PerceptionService:
    """Isolates sensor failures and switches to faster polling only during games."""

    def __init__(self, *, normal_interval: float = 15, game_interval: float = 3) -> None:
        self.normal_interval = max(normal_interval, 2)
        self.game_interval = max(game_interval, 1)
        self.system_monitor = SystemMonitor()
        self._stopping = asyncio.Event()

    async def snapshot(self) -> PerceptionSnapshot:
        result = PerceptionSnapshot(datetime.now(timezone.utc).isoformat(timespec="seconds"))
        collectors: list[tuple[str, Callable[[], Any]]] = [
            ("system", self.system_monitor.collect),
            ("connectivity", self.system_monitor.connectivity),
            ("foreground", foreground_application),
            ("processes", running_processes),
            ("game", live_game_status),
        ]
        for name, collector in collectors:
            try:
                value = await asyncio.wait_for(asyncio.to_thread(collector), timeout=12)
                setattr(result, name, value)
            except Exception as exc:
                result.errors[name] = str(exc)
                LOGGER.exception("Perception component failed: %s", name)
        return result

    async def run(self, callback: Callable[[PerceptionSnapshot], Awaitable[None]]) -> None:
        self._stopping.clear()
        while not self._stopping.is_set():
            snapshot = await self.snapshot()
            await callback(snapshot)
            interval = self.game_interval if snapshot.game else self.normal_interval
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stopping.set()
