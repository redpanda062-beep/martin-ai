from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolPolicy:
    name: str
    description: str
    risk_level: str
    approval_required: bool
    category: str
    timeout: int
    safe_arguments: dict[str, Any]


LIMITS = {
    "maximum_tool_calls": 5,
    "maximum_retries": 1,
    "maximum_plan_revisions": 2,
    "maximum_execution_seconds": 300,
    "maximum_plan_steps": 5,
}

FORBIDDEN_ARGUMENT_WORDS = (
    "powershell", "cmd.exe", "command prompt", "reg.exe", "python.exe",
    "-command", "/c", "disable antivirus", "disable firewall",
)


def validate_arguments(
    function: Callable[..., dict[str, Any]], arguments: dict[str, Any], policy: ToolPolicy
) -> tuple[bool, str]:
    if not isinstance(arguments, dict):
        return False, "Tool arguments must be an object."
    text = " ".join(str(value).lower() for value in arguments.values())
    if any(word in text for word in FORBIDDEN_ARGUMENT_WORDS):
        return False, "Arguments contain a forbidden command or security operation."
    allowed = set(inspect.signature(function).parameters)
    unknown = set(arguments) - allowed
    if unknown:
        return False, f"Unsupported arguments: {', '.join(sorted(unknown))}"
    for name, rule in policy.safe_arguments.items():
        if name not in arguments:
            continue
        value = arguments[name]
        if "choices" in rule and value not in rule["choices"]:
            return False, f"Invalid value for {name}."
        if isinstance(value, int):
            if value < rule.get("min", value) or value > rule.get("max", value):
                return False, f"{name} is outside its safe range."
    return True, ""
