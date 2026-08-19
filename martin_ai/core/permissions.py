from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class PermissionLevel(IntEnum):
    OBSERVE = 1
    SAFE_AUTOMATION = 2
    ASK_FIRST = 3
    ADMINISTRATOR = 4


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    approval_required: bool
    reason: str


class PermissionManager:
    def __init__(self, current_level: int = PermissionLevel.OBSERVE) -> None:
        self._level = PermissionLevel(max(1, min(int(current_level), 4)))

    @property
    def current_level(self) -> PermissionLevel:
        return self._level

    def set_level(self, level: int, *, user_authorized: bool = False) -> bool:
        requested = PermissionLevel(max(1, min(int(level), 4)))
        if requested > self._level and not user_authorized:
            return False
        self._level = requested
        return True

    def evaluate(self, required: int, *, explicitly_approved: bool = False) -> PermissionDecision:
        required_level = PermissionLevel(max(1, min(int(required), 4)))
        if required_level == PermissionLevel.ADMINISTRATOR:
            return PermissionDecision(explicitly_approved, not explicitly_approved,
                                      "Administrator actions always require explicit approval.")
        if required_level == PermissionLevel.ASK_FIRST:
            return PermissionDecision(explicitly_approved, not explicitly_approved,
                                      "This change requires confirmation.")
        if required_level > self._level:
            return PermissionDecision(False, True, "The current autonomy level does not allow this action.")
        return PermissionDecision(True, False, "Allowed by the current permission level.")
