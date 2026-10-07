from __future__ import annotations

import json
from datetime import datetime
from typing import Any

ACTION_TITLES = {"defender_quick_scan": "Quick Virus Scan", "defender_full_scan": "Full Virus Scan",
                 "defender_custom_scan": "File or Folder Scan",
                 "update_defender_intelligence": "Security Definitions Update"}


def _inner(result: Any) -> tuple[str, dict[str, Any]]:
    outer = result if isinstance(result, dict) else {}
    action = str(outer.get("action") or "security_action")
    data = outer.get("result") if isinstance(outer.get("result"), dict) else outer
    return action, data


def friendly_error(error: Any, action: str = "security_action") -> str:
    lower = str(error or "").lower()
    title = "Scan couldn't start" if "scan" in action else "Security action couldn't start"
    if any(word in lower for word in ("access", "permission", "administrator", "denied")):
        return f"{title}\nMartin AI wasn't able to start Windows Defender. Administrator permission may be required."
    if "cancel" in lower:
        return "Action cancelled\nThe requested security action was cancelled before it completed."
    if any(word in lower for word in ("not recognized", "not found", "unavailable")):
        return f"{title}\nWindows Defender is unavailable or isn't responding on this PC."
    return f"{title}\nMartin AI couldn't complete the request. Open Technical Details for diagnostic information."


def translate_action_result(result: Any) -> dict[str, Any]:
    action, data = _inner(result); title = ACTION_TITLES.get(action, "Security Action")
    simulated = bool(data.get("simulated")); executed = data.get("executed") is not False and not simulated
    if simulated:
        return {"title": "TEST MODE", "message": "This was a simulated security check. Windows Defender was not actually scanned.",
                "status": "test", "action_title": title, "success": False, "executed": False}
    if not bool(data.get("success")) or not executed:
        error = friendly_error(data.get("error") or data.get("output"), action).split("\n", 1)
        return {"title": error[0], "message": error[-1], "status": "error", "action_title": title,
                "success": False, "executed": executed}
    threats = data.get("threats_found")
    if action in {"defender_quick_scan", "defender_full_scan", "defender_custom_scan"}:
        if isinstance(threats, int) and threats > 0:
            return {"title": "Threats Found", "message": f"Martin AI found {threats} item{'s' if threats != 1 else ''} that need attention. Review them safely in Windows Security.",
                    "status": "warning", "action_title": title, "success": True, "executed": True}
        message = "No threats were found." if threats == 0 else "Windows Defender completed the scan."
        duration = format_duration(data.get("duration_seconds"))
        if duration: message += f"\nTime: {duration}"
        return {"title": "Scan Complete", "message": message, "status": "safe", "action_title": title,
                "success": True, "executed": True}
    return {"title": "Completed successfully", "message": f"{title} completed successfully.", "status": "safe",
            "action_title": title, "success": True, "executed": True}


def format_duration(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)) or seconds < 0: return ""
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def overall_protection(snapshot: dict[str, Any]) -> tuple[str, str]:
    defender = snapshot.get("defender") if isinstance(snapshot.get("defender"), dict) else {}
    firewall = snapshot.get("firewall") if isinstance(snapshot.get("firewall"), dict) else {}
    required = [defender.get("antivirus_enabled"), defender.get("real_time_protection_enabled"),
                firewall.get("Domain"), firewall.get("Private"), firewall.get("Public")]
    threats = snapshot.get("current_threats", snapshot.get("recent_detections"))
    if any(value is None for value in required) or threats is None:
        return "CHECKING...", "○ Required security checks are incomplete."
    if any(value is False for value in required) or bool(threats):
        return "AT RISK", "✕ A critical protection check failed or an active threat needs attention."
    if snapshot.get("definitions_outdated") or snapshot.get("scan_overdue") or snapshot.get("permission_errors"):
        return "ATTENTION NEEDED", "⚠ A non-critical security item needs review."
    return "PROTECTED", "✓ Defender and Firewall are active, and no current threats were detected."


def security_summary(snapshot: dict[str, Any]) -> str:
    status, _ = overall_protection(snapshot)
    if status == "PROTECTED": return "Your PC looks protected. Windows Defender and Firewall are active, and there are currently no detected threats."
    if status == "AT RISK":
        defender = snapshot.get("defender", {}) or {}; firewall = snapshot.get("firewall", {}) or {}
        if defender.get("real_time_protection_enabled") is False: return "Your PC is at risk because Windows Defender real-time protection appears to be disabled."
        if any(value is False for value in firewall.values()): return "Your PC needs attention. Windows Firewall appears to be disabled for one or more networks. I recommend turning it back on."
        return "Your PC is at risk because Windows Defender reports a threat that needs review."
    if status == "ATTENTION NEEDED": return "Your PC needs attention. Review the highlighted protection item and refresh after resolving it."
    return "Martin AI is still checking the required Windows security services."


def technical_json(value: Any) -> str: return json.dumps(value, indent=2, default=str)


def activity_text(events: list[dict[str, Any]]) -> str:
    if not events: return "No recent security activity yet."
    return "\n\n".join(f"{event.get('icon', '○')} {event.get('title', 'Security activity')}\n{event.get('message', '')}\n{event.get('when', 'Just now')}" for event in events[-20:][::-1])


def now_label() -> str: return datetime.now().astimezone().strftime("Today, %I:%M %p")
