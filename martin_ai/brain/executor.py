from __future__ import annotations

import json
import time
from typing import Any, Callable

from martin_ai.actions import ActionManager

from .models import BrainState, BrainStatus, PlanStep, StepStatus, now_iso
from .policies import LIMITS, ToolPolicy, validate_arguments


UpdateCallback = Callable[[str, BrainState, PlanStep | None], None]


class Executor:
    def __init__(self, read_only_tools: dict[str, Callable[..., dict[str, Any]]],
                 repair_tools: dict[str, Callable[..., dict[str, Any]]],
                 policies: dict[str, ToolPolicy], action_manager: ActionManager) -> None:
        self.read_only_tools = read_only_tools
        self.repair_tools = repair_tools
        self.policies = policies
        self.action_manager = action_manager
        self._started = time.monotonic()
        self._signatures: dict[str, int] = {}

    def run_next(self, state: BrainState, update: UpdateCallback | None = None) -> str:
        if state.status == BrainStatus.CANCELLED.value:
            return "cancelled"
        if state.tool_calls >= LIMITS["maximum_tool_calls"]:
            state.errors.append("Maximum tool-call limit reached.")
            state.status = BrainStatus.FAILED.value
            return "failed"
        if time.monotonic() - self._started > LIMITS["maximum_execution_seconds"]:
            state.errors.append("Maximum investigation time reached.")
            state.status = BrainStatus.FAILED.value
            return "failed"
        step = next((item for item in state.active_plan if item.status == StepStatus.PENDING.value), None)
        if not step:
            return "done"
        state.current_step = step.step_id
        policy = self.policies.get(step.tool_name)
        tool = self.repair_tools.get(step.tool_name) if policy and policy.approval_required else self.read_only_tools.get(step.tool_name)
        if not policy or not tool:
            return self._fail_step(state, step, f"Unknown or unavailable tool: {step.tool_name}", update)
        if not policy.approval_required and policy.risk_level != "read_only":
            return self._fail_step(state, step, "Only read-only diagnostics may run automatically.", update)
        valid, error = validate_arguments(tool, step.arguments, policy)
        if not valid:
            return self._fail_step(state, step, error, update)
        signature = f"{step.tool_name}:{json.dumps(step.arguments, sort_keys=True, default=str)}"
        self._signatures[signature] = self._signatures.get(signature, 0) + 1
        if self._signatures[signature] > LIMITS["maximum_retries"] + 1:
            return self._fail_step(state, step, "Repeated-tool loop prevented.", update)
        step.attempts += 1
        step.started_at = now_iso()
        if policy.approval_required:
            queued = self.action_manager.queue(step.tool_name, step.arguments)
            if not queued.get("pending"):
                return self._fail_step(state, step, str(queued.get("error") or queued), update)
            step.status = StepStatus.WAITING_FOR_APPROVAL.value
            state.pending_action = {"step_id": step.step_id, "name": step.tool_name, "arguments": step.arguments}
            state.status = BrainStatus.WAITING_FOR_APPROVAL.value
            state.touch()
            if update:
                update("approval_required", state, step)
            return "waiting"
        step.status = StepStatus.RUNNING.value
        state.status = BrainStatus.INVESTIGATING.value
        if update:
            update("step_started", state, step)
        started = time.monotonic()
        try:
            result = tool(**step.arguments)
            if not isinstance(result, dict):
                result = {"success": False, "error": "Tool returned an invalid result."}
        except Exception as exc:
            result = {"success": False, "error": f"{step.tool_name} failed: {exc}"}
        state.tool_calls += 1
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        step.result = result
        step.finished_at = now_iso()
        if result.get("success"):
            step.status = StepStatus.COMPLETED.value
            state.completed_steps.append(step.step_id)
            state.evidence.append({"step_id": step.step_id, "tool": step.tool_name, "result": result})
            event = "step_completed"
        else:
            step.status = StepStatus.FAILED.value
            state.errors.append(str(result.get("error") or f"{step.tool_name} failed"))
            event = "step_failed"
        state.touch()
        if update:
            update(event, state, step)
        return event

    def resolve_approval(self, state: BrainState, approved: bool,
                         update: UpdateCallback | None = None) -> str:
        pending = state.pending_action
        if not pending:
            return "no_pending_action"
        step = next(item for item in state.active_plan if item.step_id == pending["step_id"])
        result = self.action_manager.approve() if approved else self.action_manager.deny()
        state.pending_action = None
        step.finished_at = now_iso()
        if approved:
            state.tool_calls += 1
            step.result = result
            state.approved_actions.append({**pending, "result": result})
            if result.get("success"):
                step.status = StepStatus.COMPLETED.value
                state.completed_steps.append(step.step_id)
                state.evidence.append({"step_id": step.step_id, "tool": step.tool_name, "result": result})
            else:
                step.status = StepStatus.FAILED.value
                state.errors.append(str(result.get("error") or result))
        else:
            step.status = StepStatus.DENIED.value
            step.result = result
            state.denied_actions.append(pending)
        state.status = BrainStatus.INVESTIGATING.value
        state.touch()
        if update:
            update("action_approved" if approved else "action_denied", state, step)
        return "resolved"

    def retry(self, state: BrainState, step_id: str) -> bool:
        step = next((item for item in state.active_plan if item.step_id == step_id), None)
        if not step or step.status != StepStatus.FAILED.value or step.attempts > LIMITS["maximum_retries"]:
            return False
        step.status = StepStatus.PENDING.value
        step.result = None
        return True

    def _fail_step(self, state: BrainState, step: PlanStep, error: str,
                   update: UpdateCallback | None) -> str:
        step.status = StepStatus.FAILED.value
        step.result = {"success": False, "error": error}
        step.finished_at = now_iso()
        state.errors.append(error)
        state.touch()
        if update:
            update("step_failed", state, step)
        return "step_failed"
