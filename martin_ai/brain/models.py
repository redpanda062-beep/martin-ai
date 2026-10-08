from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class BrainStatus(str, Enum):
    PLANNING = "planning"
    INVESTIGATING = "investigating"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    DENIED = "denied"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PlanStep:
    step_id: str
    goal: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    approval_required: bool = False
    expected_evidence: str = ""
    status: str = StepStatus.PENDING.value
    attempts: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None


@dataclass
class BrainState:
    user_request: str
    active_plan: list[PlanStep] = field(default_factory=list)
    current_step: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    pending_action: dict[str, Any] | None = None
    diagnosis: str = ""
    confidence: str = "low"
    uncertainty: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    approved_actions: list[dict[str, Any]] = field(default_factory=list)
    denied_actions: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    status: str = BrainStatus.PLANNING.value
    tool_calls: int = 0
    plan_revisions: int = 0
    report_paths: dict[str, str] = field(default_factory=dict)
    related_history: list[dict[str, Any]] = field(default_factory=list)
    confirmed_findings: list[str] = field(default_factory=list)
    possible_causes: list[str] = field(default_factory=list)
    normal_readings: list[str] = field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
