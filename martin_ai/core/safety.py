from __future__ import annotations

from pathlib import Path
from typing import Any


FORBIDDEN_TOOL_NAMES = frozenset({
    "format_drive", "disable_defender", "disable_firewall", "disable_uac",
    "change_password", "flash_firmware", "modify_bios", "delete_system_files",
    "remove_recovery_partition", "grant_privileges", "arbitrary_shell",
})
SENSITIVE_KEYS = ("password", "token", "cookie", "secret", "authorization", "credential")
COMMAND_KEYS = ("command", "cmd", "powershell", "script", "shell", "python_code")


def validate_request(tool_name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    if tool_name.lower() in FORBIDDEN_TOOL_NAMES:
        return False, "This action is prohibited by a hard safety rule."
    if not isinstance(arguments, dict):
        return False, "Tool arguments must be an object."
    for key in arguments:
        lower = str(key).lower()
        if any(word in lower for word in COMMAND_KEYS):
            return False, "Arbitrary command or script arguments are prohibited."
        if any(word in lower for word in SENSITIVE_KEYS):
            return False, "Sensitive credentials must not be supplied to tools."
    return True, ""


def validate_safe_path(path: str, allowed_roots: tuple[Path, ...]) -> tuple[bool, str]:
    try:
        resolved = Path(path).resolve(strict=False)
    except OSError:
        return False, "The target path could not be resolved."
    if resolved.anchor == str(resolved):
        return False, "Drive roots are never valid action targets."
    for root in allowed_roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return True, ""
        except ValueError:
            continue
    return False, "The target is outside the tool's allowlisted roots."
