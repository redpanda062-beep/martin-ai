from __future__ import annotations

import asyncio
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..memory import MemoryManager
from ..monitoring import PerceptionService, PerceptionSnapshot
from .brain import AgentBrain
from .runtime import RuntimeControl
from .tool_registry import ToolRegistry


class AutonomousAgent:
    def __init__(self, perception: PerceptionService, brain: AgentBrain, tools: ToolRegistry,
                 memory: MemoryManager, runtime: RuntimeControl, *, maximum_retries: int = 2,
                 cooldown_seconds: int = 300,
                 status_callback: Callable[[str, str], None] | None = None) -> None:
        self.perception, self.brain, self.tools = perception, brain, tools
        self.memory, self.runtime = memory, runtime
        self.maximum_retries = maximum_retries
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self.status_callback = status_callback
        self._last_incident: dict[str, datetime] = {}
        self._retries: defaultdict[str, int] = defaultdict(int)

    async def run(self) -> None:
        await self.perception.run(self.handle_snapshot)

    async def handle_snapshot(self, snapshot: PerceptionSnapshot) -> None:
        self._status("ANALYZING", "Reviewing the latest system snapshot")
        decision = self.brain.analyze(snapshot)
        self.memory.save_observation("perception", "system_snapshot", snapshot.__dict__, severity=decision.severity)
        if not decision.proposed_action:
            self._status("MONITORING", decision.explanation)
            return
        signature = hashlib.sha256(json.dumps(decision.to_dict(), sort_keys=True, default=str).encode()).hexdigest()[:20]
        now = datetime.now(timezone.utc)
        if signature in self._last_incident and now - self._last_incident[signature] < self.cooldown:
            self._status("MONITORING", "Duplicate incident is in cooldown")
            return
        if self._retries[signature] > self.maximum_retries:
            self._status("WAITING FOR PERMISSION", "Retry limit reached; manual review is required")
            return
        incident_id = self.memory.save_incident("automatic", decision.suspected_problem,
                                                severity=decision.severity, details=decision.to_dict())
        self._last_incident[signature] = now
        if self.runtime.emergency_stopped or not self.runtime.autonomy_enabled:
            self._status("MONITORING", "Autonomous action is disabled; incident was recorded")
            return
        self._status("EXECUTING", decision.explanation)
        result = await asyncio.to_thread(self.tools.execute, decision.proposed_action, incident_id=incident_id)
        if result.get("pending"):
            self._status("WAITING FOR PERMISSION", decision.explanation)
        elif result.get("success"):
            self._status("VERIFYING", decision.verification_method)
        else:
            self._retries[signature] += 1
            self._status("MONITORING", f"Action failed safely: {result.get('error', 'unknown error')}")

    def stop(self) -> None:
        self.runtime.stop()
        self.perception.stop()

    def _status(self, state: str, activity: str) -> None:
        if self.status_callback:
            self.status_callback(state, activity)
