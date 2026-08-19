from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class SkillStatus(str, Enum):
    STARTING = "Starting"
    HEALTHY = "Healthy"
    DISABLED = "Disabled"
    DEGRADED = "Degraded"
    ERROR = "Error"
    UNAVAILABLE = "Unavailable"


@dataclass(frozen=True)
class Capability:
    id: str
    description: str
    permission_level: int = 1
    intents: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillMetadata:
    id: str
    name: str
    description: str
    version: str
    author: str
    capabilities: tuple[Capability, ...]
    supported_intents: tuple[str, ...] = ()
    permissions: tuple[int, ...] = (1,)
    risk_level: str = "read_only"
    dependencies: tuple[str, ...] = ()


@dataclass
class SkillMatch:
    skill_id: str
    capability: str
    confidence: float
    parameters: dict[str, Any] = field(default_factory=dict)


class Skill(ABC):
    """Narrow plugin contract. Skills can call only injected capabilities."""

    metadata: SkillMetadata

    def __init__(self, executor: Callable[[str, dict[str, Any], bool], dict[str, Any]]) -> None:
        self._executor = executor
        self._status = SkillStatus.STARTING
        self._status_detail = ""

    def initialize(self) -> None:
        self._status = SkillStatus.HEALTHY

    @abstractmethod
    def can_handle(self, request: str) -> SkillMatch | None:
        raise NotImplementedError

    def get_capabilities(self) -> tuple[Capability, ...]:
        return self.metadata.capabilities

    def execute(self, action: str, parameters: dict[str, Any], *, approved: bool = False) -> dict[str, Any]:
        if action not in {item.id for item in self.metadata.capabilities}:
            return {"success": False, "error": "This skill does not provide that capability."}
        return self._executor(action, parameters, approved)

    def get_status(self) -> dict[str, str]:
        return {"status": self._status.value, "detail": self._status_detail}

    def shutdown(self) -> None:
        self._status = SkillStatus.DISABLED

