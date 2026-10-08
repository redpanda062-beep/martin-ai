from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from typing import Any


SIMPLE_MODE = True
TECHNICAL_DETAILS_COMMANDS = {
    "show technical details",
    "show diagnostics",
    "technical details",
}


def wants_technical_details(text: str) -> bool:
    return re.sub(r"\s+", " ", text.strip().lower()) in TECHNICAL_DETAILS_COMMANDS


def technical_details(raw_result: Any) -> str:
    """Serialize retained evidence only after an explicit user request."""
    if raw_result is None:
        return "**Action Needed**\n\nThere are no saved technical details from a previous response."
    if is_dataclass(raw_result):
        raw_result = asdict(raw_result)
    try:
        return "**Technical Details**\n\n```json\n" + json.dumps(raw_result, indent=2, default=str) + "\n```"
    except (TypeError, ValueError):
        return f"**Technical Details**\n\n```text\n{raw_result}\n```"


def _plain_error(raw: Any) -> str:
    if isinstance(raw, dict):
        error = str(raw.get("error") or raw.get("message") or "The operation could not be completed.")
    else:
        error = str(raw or "The operation could not be completed.")
    lowered = error.lower()
    if "timed out" in lowered:
        return "The check took too long to finish. Please try again."
    if "permission" in lowered or "access is denied" in lowered:
        return "Windows did not allow this check. No changes were made."
    if "urlopen" in lowered or "connection" in lowered or "ollama" in lowered:
        return "The local AI service is unavailable. Please make sure Ollama is running and try again."
    return "The operation could not be completed. No changes were made."


def _system_summary(result: dict[str, Any]) -> str:
    if not result.get("success", True):
        return f"**Problem Found**\n\n{_plain_error(result)}"
    ram = result.get("ram_percent_used")
    disks = [disk for disk in result.get("disks", []) if isinstance(disk, dict)]
    critical = [disk for disk in disks if float(disk.get("free_gb", 9999)) < 10]
    low = [disk for disk in disks if disk not in critical and (float(disk.get("free_gb", 9999)) < 30 or float(disk.get("percent_used", 0)) >= 90)]
    if critical or low:
        disk = (critical or low)[0]
        drive = str(disk.get("device") or disk.get("mountpoint") or "A drive").rstrip("\\")
        free = float(disk.get("free_gb", 0))
        seriousness = "critically low" if critical else "running low"
        action = "Free up space as soon as practical" if critical else "Consider freeing 20–30 GB"
        return f"**⚠️ Storage Warning**\n\n{drive} is {seriousness} on space, with about **{free:g} GB free**. {action}."
    if isinstance(ram, (int, float)) and ram >= 90:
        return "**⚠️ Warning**\n\nMemory use is very high right now. Close an app you are not using, especially before starting a game."
    return "**All Good**\n\nYour PC looks healthy in this check. No action is needed."


def _focused_status(result: dict[str, Any], tool: str) -> str:
    if result.get("success") is False:
        return f"**Problem Found**\n\n{_plain_error(result)}"
    if tool == "ram_status":
        used, total, available = result.get("used_gb", 0), result.get("total_gb", 0), result.get("available_gb", 0)
        percent = float(result.get("percent_used", 0))
        condition = "Memory use is high; closing an unused app may help." if percent >= 90 else "Everything looks normal."
        return f"**RAM Status**\n\nYou're using about **{used:g} GB** of {total:g} GB, with {available:g} GB available. {condition}"
    if tool == "cpu_status":
        percent = float(result.get("cpu_percent", 0))
        condition = "CPU use is high right now." if percent >= 90 else "CPU use looks normal."
        return f"**CPU Status**\n\nYour CPU is currently at about **{percent:g}%** usage. {condition}"
    if tool == "storage_status":
        return _system_summary({"success": True, "ram_percent_used": 0, "disks": result.get("disks", [])})
    if tool == "network_status":
        return "**Network Status**\n\nYour PC has an active network connection." if result.get("connected") else "**Problem Found**\n\nNo active network connection was detected."
    if tool == "nvidia_status":
        if not result.get("available", True):
            return "**GPU Status**\n\nGPU sensor information is unavailable right now."
        usage = result.get("utilization_percent")
        temp = result.get("temperature_c")
        details = []
        if isinstance(usage, (int, float)): details.append(f"usage is about **{usage:g}%**")
        if isinstance(temp, (int, float)): details.append(f"temperature is **{temp:g}°C**")
        return "**GPU Status**\n\nYour GPU " + (" and ".join(details) if details else "is available") + "."
    return _generic_dict(result, {"tool": tool})


def _value(value: Any, suffix: str = "") -> str:
    return f"{value:g}{suffix}" if isinstance(value, (int, float)) else "Unavailable"


def _network_diagnostic(result: dict[str, Any]) -> str:
    issue = result.get("issue")
    healthy = issue == "healthy"
    title = "Network Healthy" if healthy else "Network Problem Found"
    adapter = result.get("adapter", {})
    ip = result.get("ip", {})
    gateway = result.get("gateway", {})
    dns = result.get("dns", {})
    internet = result.get("internet", {})
    lines = [
        f"**{title}**", "", str(result.get("summary") or "The diagnostic completed."), "",
        f"* Adapter: {'Connected' if adapter.get('state') in {'Up', 'Connected'} else 'Disconnected'} ({adapter.get('connection_type') or 'Unavailable'})",
        f"* IP: {'Valid' if ip.get('valid') else 'Invalid or unavailable'}",
        f"* Gateway: {'Reachable' if gateway.get('reachable') else 'Unreachable'}",
        f"* DNS: {'Working' if dns.get('resolved') else 'Not working'}",
        f"* Internet: {'Working' if internet.get('reachable') else 'Not reachable'}",
        f"* Latency: {_value(result.get('latency_ms'), ' ms')}",
        f"* Packet loss: {_value(result.get('packet_loss_percent'), '%')}",
        f"* Link speed: {adapter.get('link_speed') or 'Unavailable'}",
    ]
    return "\n".join(lines)


def _network_measurement(result: dict[str, Any], *, dns: bool = False) -> str:
    if dns:
        status = "working" if result.get("resolved") else "not resolving websites correctly"
        return f"**DNS Check**\n\nDNS is {status}. Resolution time: **{_value(result.get('resolution_ms'), ' ms')}**."
    reachable = result.get("reachable")
    summary = "The test endpoint responded." if reachable else "The test endpoint did not respond."
    context = str(result.get("context") or f"PC to {result.get('target') or 'public test endpoint'}")
    return (f"**Network Performance**\n\n{summary} Target: {context}.\n\n"
            f"* Latency: {_value(result.get('latency_ms'), ' ms')}\n"
            f"* Jitter: {_value(result.get('jitter_ms'), ' ms')}\n"
            f"* Packet loss: {_value(result.get('packet_loss_percent'), '%')}\n"
            f"* Samples: {_value(result.get('sample_count'))}")


def _network_performance_investigation(result: dict[str, Any]) -> str:
    issue = str(result.get("issue") or "no_obvious_cause")
    titles = {
        "possible_wifi_issue": "Possible Wi-Fi Issue",
        "possible_local_network_issue": "Possible Local Network Issue",
        "possible_local_congestion": "Possible Local Congestion",
        "possible_dns_delay": "Possible DNS Delay",
        "possible_external_issue": "Possible Internet/Upstream Issue",
        "adapter_problem": "Network Adapter Problem",
        "no_obvious_cause": "No Obvious Network Problem Found",
    }
    gateway = result.get("gateway_latency", {}); external = result.get("external_latency", {})
    wifi = result.get("wifi", {}); adapter = result.get("adapter", {})
    utilization = result.get("utilization", {}); processes = result.get("process_bandwidth", {})
    process_text = ("Unavailable" if not processes.get("supported") else
                    ", ".join(str(item.get("name")) for item in processes.get("processes", [])[:3]) or "No heavy users detected")
    return "\n".join([
        f"**{titles.get(issue, 'Network Performance Investigation')}**", "",
        str(result.get("summary") or "The investigation completed."), "",
        f"* Router latency: {_value(gateway.get('latency_ms'), ' ms')}",
        f"* Internet latency: {_value(external.get('latency_ms'), ' ms')}",
        f"* Internet jitter: {_value(external.get('jitter_ms'), ' ms')}",
        f"* Packet loss: {_value(external.get('packet_loss_percent'), '%')}",
        f"* DNS lookup: {_value((result.get('dns') or {}).get('resolution_ms'), ' ms')}",
        f"* Wi-Fi signal: {wifi.get('quality') or 'Unavailable'}",
        f"* Wi-Fi/link rate: {adapter.get('link_speed') or 'Unavailable'} (PC-to-router link, not internet speed)",
        f"* Current adapter receive/send: {_value(utilization.get('receive_mbps'), ' Mbps')} / {_value(utilization.get('send_mbps'), ' Mbps')}",
        "* Internet throughput: Not tested",
        f"* Per-process bandwidth: {process_text}",
    ])


def _generic_dict(result: dict[str, Any], context: dict[str, Any]) -> str:
    if result.get("success") is False:
        return f"**Problem Found**\n\n{_plain_error(result)}"
    if result.get("pending"):
        name = str(result.get("name") or context.get("action") or "the requested action").replace("_", " ")
        return f"**Action Needed**\n\nApproval is required before I can {name}."
    message = result.get("message")
    if message and not any(token in str(message) for token in ("{", "[", "Traceback")):
        return f"**Fixed**\n\n{message}"
    return "**All Good**\n\nThe requested check completed successfully."


def investigation_assessment(state: Any) -> str:
    sections = []
    for field, title in (("confirmed_findings", "Confirmed Findings"),
                         ("possible_causes", "Possible Causes — Unconfirmed"),
                         ("normal_readings", "Normal Readings")):
        lines = getattr(state, field, [])
        if lines:
            sections.append(f"**{title}**\n\n" + "\n".join(f"- {line}" for line in lines))
    return "\n\n".join(sections)


def _investigation(state: Any) -> str:
    status = getattr(state, "status", "completed")
    if status == "waiting_for_approval":
        return "**Approval Required**\n\nThe investigation is paused until you approve or deny the proposed action."
    if status in {"planning", "investigating", "evaluating"}:
        return "**Investigation in Progress**\n\nMartin is collecting and reviewing evidence. No conclusion is ready yet."
    if status in {"cancelled", "failed"}:
        return f"**Investigation {status.capitalize()}**\n\nThe investigation did not complete. Review the action log and collected evidence."
    diagnosis = str(getattr(state, "diagnosis", "") or "").strip()
    recommendations = [str(item) for item in getattr(state, "recommendations", []) if item]
    errors = list(getattr(state, "errors", []))
    evidence = list(getattr(state, "evidence", []))
    assessment = investigation_assessment(state)
    if assessment:
        action = f"\n\n**Next check:** {recommendations[0]}" if recommendations else ""
        return f"**Investigation Complete**\n\n{diagnosis}\n\n{assessment}{action}"
    for item in evidence:
        if item.get("tool") == "system_summary" and isinstance(item.get("result"), dict):
            summary = _system_summary(item["result"])
            if "All Good" not in summary:
                return summary
    serious = "alert:" in diagnosis.lower() or any(word in diagnosis.lower() for word in ("critical", "high pressure", "likely cause"))
    if serious:
        action = recommendations[0] if recommendations else "Review the issue before continuing."
        clean = re.sub(r"^alert:\s*", "", diagnosis, flags=re.I)
        return f"**Problem Found**\n\n{clean}\n\n**Recommended action:** {action}"
    if errors and not evidence:
        return "**Problem Found**\n\nI could not complete the diagnostic check. Please try again."
    if diagnosis and "inconclusive" not in diagnosis.lower():
        action = f" {recommendations[0]}" if recommendations else " No action is needed."
        return f"**Investigation Complete**\n\n{diagnosis}{action}"
    return "**Inconclusive**\n\nThe collected evidence did not establish a cause."


def format_response(raw_result: Any, context: dict[str, Any] | None = None, simple_mode: bool = SIMPLE_MODE) -> str:
    """Convert internal tool/state data into the only text shown in normal chat."""
    context = context or {}
    if not simple_mode:
        return technical_details(raw_result)
    if hasattr(raw_result, "evidence") and hasattr(raw_result, "diagnosis"):
        return _investigation(raw_result)
    if isinstance(raw_result, dict):
        tool = str(context.get("tool") or "")
        if tool in {"ram_status", "cpu_status", "storage_status", "network_status", "nvidia_status"}:
            return _focused_status(raw_result, tool)
        if tool == "run_network_diagnostic": return _network_diagnostic(raw_result)
        if tool == "investigate_network_performance": return _network_performance_investigation(raw_result)
        if tool == "check_dns": return _network_measurement(raw_result, dns=True)
        if tool in {"measure_network_latency", "measure_gateway_latency", "measure_external_latency",
                    "measure_network_jitter", "check_internet_connectivity", "check_gateway"}:
            return _network_measurement(raw_result)
        if tool == "system_summary" or {"ram_percent_used", "disks"}.issubset(raw_result):
            return _system_summary(raw_result)
        return _generic_dict(raw_result, context)
    if isinstance(raw_result, BaseException):
        return f"**Problem Found**\n\n{_plain_error(raw_result)}"
    text = str(raw_result or "").strip()
    if not text:
        return "**Problem Found**\n\nI did not receive a usable result. Please try again."
    try:
        structured = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        structured = None
    if isinstance(structured, (dict, list)):
        return format_response(structured, context, simple_mode=True)
    if any(token in text for token in ("Traceback", "system_summary:", "{'", "ProcessId", "EventRecordID")):
        return "**Problem Found**\n\nThe check returned an unreadable result. Ask me to try it again."
    return text
