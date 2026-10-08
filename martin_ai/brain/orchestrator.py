from __future__ import annotations

from typing import Any, Callable

from martin_ai.actions import ActionManager
from martin_ai.memory import MemoryManager

from .evaluator import Evaluator
from .executor import Executor, UpdateCallback
from .memory import save_report
from .models import BrainState, BrainStatus, PlanStep
from .planner import Planner
from .policies import LIMITS, ToolPolicy


class Orchestrator:
    def __init__(self, read_only_tools: dict[str, Callable[..., dict[str, Any]]],
                 repair_tools: dict[str, Callable[..., dict[str, Any]]],
                 policies: dict[str, ToolPolicy], action_manager: ActionManager,
                 model_client: Callable[[str], str] | None = None, *,
                 memory: MemoryManager | None = None) -> None:
        self.planner = Planner(policies, model_client)
        self.evaluator = Evaluator(policies, model_client)
        self.executor = Executor(read_only_tools, repair_tools, policies, action_manager)
        self.state: BrainState | None = None
        self.memory = memory

    def start(self, request: str, context: dict[str, Any] | None = None,
              update: UpdateCallback | None = None) -> BrainState:
        self.state = BrainState(user_request=request)
        if self.memory:
            try:
                self.state.related_history = self.memory.related_problem_history(request)
            except Exception:
                self.state.uncertainty.append("Past problem history could not be loaded.")
        # Historical text is displayed as context only. It cannot supply tools,
        # arguments, approval, or fresh diagnostic evidence to the planner.
        if update:
            update("planning", self.state, None)
        self.state.active_plan = self.planner.create_plan(request, context)
        if not self.state.active_plan:
            self.state.status = BrainStatus.FAILED.value
            self.state.errors.append("No safe registered diagnostic tools matched this request.")
            return self.state
        self.state.status = BrainStatus.INVESTIGATING.value
        if update:
            update("plan_created", self.state, None)
        return self.run(update)

    def run(self, update: UpdateCallback | None = None) -> BrainState:
        state = self._require_state()
        while state.status not in {BrainStatus.CANCELLED.value, BrainStatus.FAILED.value}:
            outcome = self.executor.run_next(state, update)
            if outcome == "waiting":
                return state
            if outcome == "done":
                revisions = self.evaluator.review(state)
                if revisions:
                    state.active_plan.extend(revisions)
                    if update:
                        update("plan_revised", state, revisions[0])
                    continue
                self._complete(update)
                return state
        return state

    def approve(self, update: UpdateCallback | None = None) -> BrainState:
        state = self._require_state()
        self.executor.resolve_approval(state, True, update)
        return self.run(update)

    def deny(self, update: UpdateCallback | None = None) -> BrainState:
        state = self._require_state()
        self.executor.resolve_approval(state, False, update)
        return self.run(update)

    def cancel(self, update: UpdateCallback | None = None) -> BrainState:
        state = self._require_state()
        if state.pending_action:
            self.executor.action_manager.deny()
            state.denied_actions.append(state.pending_action)
            state.pending_action = None
        state.status = BrainStatus.CANCELLED.value
        state.touch()
        if update:
            update("cancelled", state, None)
        return state

    def retry_failed(self, update: UpdateCallback | None = None) -> BrainState:
        state = self._require_state()
        failed = next((step for step in reversed(state.active_plan) if step.status == "failed"), None)
        if failed and self.executor.retry(state, failed.step_id):
            state.status = BrainStatus.INVESTIGATING.value
            return self.run(update)
        return state

    def _complete(self, update: UpdateCallback | None) -> None:
        state = self._require_state()
        state.status = BrainStatus.EVALUATING.value
        if update:
            update("evaluating", state, None)
        self.evaluator.finalize(state)
        state.status = BrainStatus.COMPLETED.value
        state.touch()
        if self.memory:
            try:
                self.memory.save_incident("investigation", state.user_request, details={
                    "diagnosis": state.diagnosis, "confidence": state.confidence,
                    "recommendations": state.recommendations, "uncertainty": state.uncertainty,
                })
            except Exception:
                state.uncertainty.append("This investigation could not be saved to problem history.")
        try:
            state.report_paths = save_report(state)
        except OSError as exc:
            state.errors.append(f"Report could not be saved: {exc}")
        if update:
            update("completed", state, None)

    def _require_state(self) -> BrainState:
        if self.state is None:
            raise RuntimeError("No investigation has started.")
        return self.state
