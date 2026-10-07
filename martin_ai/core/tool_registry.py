from __future__ import annotations

import inspect
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..memory import MemoryManager
from ..models import ActionRequest
from .permissions import PermissionManager
from .runtime import RuntimeControl
from .safety import validate_request


Validator = Callable[[dict[str, Any]], tuple[bool, str]]


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    risk_level: str
    required_permission_level: int
    function: Callable[..., dict[str, Any]]
    timeout: int = 30
    validator: Validator | None = None
    rollback_tool: str | None = None
    reversible: bool = False
    changes_system: bool = False
    verification_tool: str | None = None
    allowed_arguments: frozenset[str] = field(default_factory=frozenset)

    @property
    def safety_level(self) -> int:
        """User-facing 0-3 safety scale; internal permissions remain 1-4."""
        return max(0, min(3, self.required_permission_level - 1))

    @property
    def confirmation_required(self) -> bool:
        return self.required_permission_level >= 3


class ToolRegistry:
    def __init__(self, permissions: PermissionManager, runtime: RuntimeControl,
                 memory: MemoryManager | None = None, *, maximum_actions_per_hour: int = 10) -> None:
        self.permissions = permissions
        self.runtime = runtime
        self.memory = memory
        self.maximum_actions_per_hour = maximum_actions_per_hour
        self._tools: dict[str, RegisteredTool] = {}
        self._action_times: deque[datetime] = deque()

    def register(self, tool: RegisteredTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        if tool.required_permission_level not in range(1, 5):
            raise ValueError("Permission level must be between 1 and 4.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def catalog(self) -> list[dict[str, Any]]:
        return [{"name": tool.name, "description": tool.description,
                 "parameters": sorted(tool.allowed_arguments or inspect.signature(tool.function).parameters),
                 "permission_level": tool.safety_level, "timeout": tool.timeout,
                 "confirmation_required": tool.confirmation_required}
                for tool in self._tools.values()]

    def execute(self, request: ActionRequest, *, explicitly_approved: bool = False,
                incident_id: int | None = None) -> dict[str, Any]:
        tool = self._tools.get(request.tool_name)
        if tool is None:
            return {"success": False, "rejected": True, "error": "Unknown tools are rejected."}
        safe, error = validate_request(request.tool_name, request.arguments)
        if not safe:
            return {"success": False, "rejected": True, "error": error}
        if self.runtime.emergency_stopped:
            return {"success": False, "blocked": True, "error": "Emergency stop is active."}
        allowed = tool.allowed_arguments or frozenset(inspect.signature(tool.function).parameters)
        unknown = set(request.arguments) - set(allowed)
        if unknown:
            return {"success": False, "rejected": True, "error": f"Unsupported arguments: {', '.join(sorted(unknown))}"}
        if tool.validator:
            valid, error = tool.validator(request.arguments)
            if not valid:
                return {"success": False, "rejected": True, "error": error}
        decision = self.permissions.evaluate(tool.required_permission_level,
                                             explicitly_approved=explicitly_approved)
        if not decision.allowed:
            return {"success": False, "pending": decision.approval_required,
                    "permission_required": tool.required_permission_level, "error": decision.reason}
        if tool.changes_system and not self.runtime.autonomy_enabled and not explicitly_approved:
            return {"success": False, "blocked": True, "error": "Autonomous actions are disabled."}
        if tool.changes_system and not self._rate_limit_available():
            return {"success": False, "blocked": True, "error": "Action rate limit reached."}

        simulated = self.runtime.simulation_mode and tool.changes_system
        approval_status = "approved" if explicitly_approved else "automatic"
        action_id = self.memory.save_action(
            tool.name, request.arguments, trigger=request.trigger, risk_level=tool.risk_level,
            permission_level=tool.required_permission_level, approval_status=approval_status,
            simulation=simulated, incident_id=incident_id,
        ) if self.memory else None
        if simulated:
            result = {"success": False, "request_handled": True, "simulated": True, "executed": False,
                      "started": False, "completed": False,
                      "message": f"Simulation: would run {tool.name}.",
                      "expected_result": request.expected_result}
            if self.memory and action_id:
                self.memory.save_result(action_id, True, result, verification={"simulated": True})
            return result

        token = self.runtime.action_token()
        if not self.runtime.may_start_action(token):
            return {"success": False, "blocked": True, "error": "Action cancelled by emergency stop."}
        try:
            result = tool.function(**request.arguments)
            if not isinstance(result, dict):
                result = {"success": False, "error": "Tool returned an invalid result."}
            error_trace = None
        except Exception as exc:
            result = {"success": False, "error": f"{tool.name} failed: {exc}"}
            error_trace = traceback.format_exc()
        if tool.changes_system:
            self._action_times.append(datetime.now(timezone.utc))
        if self.memory and action_id:
            rollback = result.get("rollback")
            if rollback:
                self.memory.db.execute("UPDATE actions SET rollback_json=? WHERE id=?",
                                       (__import__("json").dumps(rollback, default=str), action_id))
            self.memory.save_result(action_id, bool(result.get("success")), result, error_trace=error_trace)
        return result

    def _rate_limit_available(self) -> bool:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        while self._action_times and self._action_times[0] < cutoff:
            self._action_times.popleft()
        return len(self._action_times) < self.maximum_actions_per_hour
