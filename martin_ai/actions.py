from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class PendingAction:
    name: str
    arguments: dict[str, Any]


class ActionManager:
    def __init__(self, repair_tools: dict[str, Callable[..., dict[str, Any]]],
                 executor: Callable[[str, dict[str, Any], bool], dict[str, Any]] | None = None) -> None:
        self._repair_tools = repair_tools
        self._executor = executor
        self._pending: PendingAction | None = None

    def has_pending(self) -> bool:
        return self._pending is not None

    def queue(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self._repair_tools:
            return {
                "success": False,
                "error": f"Unknown protected tool: {name}",
            }

        if self._pending is not None:
            return {
                "success": False,
                "pending": True,
                "message": "Another action is already waiting for approval.",
                "pending_action": self.describe(),
            }

        self._pending = PendingAction(name=name, arguments=arguments)
        return {
            "success": False,
            "pending": True,
            "message": "Action queued. Type approve to run it or deny to cancel.",
            "pending_action": self.describe(),
        }

    def approve(self) -> dict[str, Any]:
        if self._pending is None:
            return {
                "success": False,
                "error": "There is no pending action to approve.",
            }

        pending = self._pending
        self._pending = None

        tool = self._repair_tools.get(pending.name)
        if tool is None:
            return {
                "success": False,
                "error": f"Pending tool no longer exists: {pending.name}",
            }

        try:
            result = (self._executor(pending.name, pending.arguments, True)
                      if self._executor else tool(**pending.arguments))
        except TypeError as exc:
            return {
                "success": False,
                "error": f"Invalid arguments for {pending.name}: {exc}",
            }
        except Exception as exc:
            return {
                "success": False,
                "error": f"{pending.name} failed: {exc}",
            }

        return {
            "success": bool(result.get("success")),
            "action": pending.name,
            "arguments": pending.arguments,
            "result": result,
        }

    def deny(self) -> dict[str, Any]:
        if self._pending is None:
            return {
                "success": False,
                "error": "There is no pending action to cancel.",
            }

        cancelled = self.describe()
        self._pending = None
        return {
            "success": True,
            "cancelled": True,
            "cancelled_action": cancelled,
        }

    def describe(self) -> dict[str, Any] | None:
        if self._pending is None:
            return None

        return {
            "name": self._pending.name,
            "arguments": self._pending.arguments,
        }

    def describe_text(self) -> str:
        pending = self.describe()
        if pending is None:
            return "No action is waiting for approval."

        friendly = {
            "close_app": "Close application",
            "flush_dns": "Flush DNS cache",
            "reset_microsoft_store_cache": "Reset Microsoft Store cache",
            "clear_shader_caches": "Clear shader caches",
            "run_sfc_scan": "Run System File Checker",
            "run_dism_restorehealth": "Run DISM RestoreHealth",
            "write_clipboard": "Replace clipboard text",
            "defender_quick_scan": "Run Microsoft Defender quick scan",
            "defender_full_scan": "Run Microsoft Defender full scan (long operation)",
            "defender_custom_scan": "Scan selected file or folder with Microsoft Defender",
            "update_defender_intelligence": "Update Microsoft Defender security intelligence",
            "terminate_suspicious_process": "Terminate the identified suspicious process",
        }

        name = pending["name"]
        title = friendly.get(name, name)

        return (
            f"{title}\n"
            f"Arguments: {json.dumps(pending['arguments'])}\n\n"
            "Type approve to run it or deny to cancel."
        )
