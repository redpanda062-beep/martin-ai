from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Observation:
    source: str
    kind: str
    data: dict[str, Any]
    severity: str = "info"
    observed_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ActionRequest:
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    trigger: str = "user"
    reason: str = ""
    expected_result: str = ""
    verification_method: str = ""


@dataclass
class AgentDecision:
    observation: str
    suspected_problem: str
    confidence: float
    severity: str
    proposed_action: ActionRequest | None
    permission_required: int
    expected_result: str
    verification_method: str
    explanation: str
    alternatives: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
