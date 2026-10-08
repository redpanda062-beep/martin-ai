from __future__ import annotations

import re
from enum import Enum


class Intent(str, Enum):
    GREETING = "greeting"
    GENERAL_CHAT = "general_chat"
    PC_HEALTH = "pc_health"
    CPU_STATUS = "cpu_status"
    RAM_STATUS = "ram_status"
    GPU_STATUS = "gpu_status"
    STORAGE_STATUS = "storage_status"
    NETWORK_STATUS = "network_status"
    NETWORK_DIAGNOSTIC = "network_diagnostic"
    NETWORK_PERFORMANCE = "network_performance"
    NETWORK_PERFORMANCE_INVESTIGATION = "network_performance_investigation"
    NETWORK_DNS = "network_dns"
    NETWORK_LATENCY = "network_latency"
    NETWORK_PACKET_LOSS = "network_packet_loss"
    GAMING = "gaming"
    SECURITY = "security"
    TROUBLESHOOTING = "troubleshooting"
    SYSTEM_ACTION = "system_action"
    UNKNOWN = "unknown"


def detect_intent(text: str) -> Intent:
    """Cheap, deterministic first-stage routing. This function never runs tools."""
    value = re.sub(r"[^a-z0-9'.]+", " ", text.lower()).strip()
    if re.fullmatch(r"(?:hey|hi|hello|yo|hiya|sup|hey ai|hello ai|hi martin|hey martin)[!. ]*", value):
        return Intent.GREETING
    if any(phrase in value for phrase in ("what are you doing", "what're you doing", "how are you", "who are you", "thank you", "thanks")):
        return Intent.GENERAL_CHAT
    if re.search(r"\b(ram|memory)\b", value):
        return Intent.RAM_STATUS
    if re.search(r"\b(cpu|processor)\b", value):
        return Intent.CPU_STATUS
    if re.search(r"\b(gpu|graphics card|vram)\b", value):
        return Intent.GPU_STATUS
    if is_storage_investigation(text):
        return Intent.TROUBLESHOOTING
    if re.search(r"\b(storage|disk|drive|free space|space left)\b", value):
        return Intent.STORAGE_STATUS
    network_topic = re.search(r"\b(network|internet|wifi|wi fi|ethernet|connection|online|dns|ping|latency|packet loss|websites?)\b", value)
    if network_topic:
        if re.search(r"\b(packet loss|packets? (?:are )?dropping|dropped packets?)\b", value):
            return Intent.NETWORK_PACKET_LOSS
        if re.search(r"\b(ping|latency|response time)\b", value):
            return Intent.NETWORK_LATENCY
        if re.search(r"\b(dns|websites? (?:won't|wont|can't|cant|do not|don't) (?:load|open)|names? (?:won't|wont|do not|don't) resolve)\b", value):
            return Intent.NETWORK_DNS
        if re.search(r"\b(slow|speed|lag(?:gy|ging)?|performance|unstable|intermittent)\b", value):
            return Intent.NETWORK_PERFORMANCE_INVESTIGATION
        if re.search(r"\b(diagnostic|diagnose|problems?|issues?|troubleshoot|why|not working|broken|checkup|full check|investigat|fix|repair)\w*\b", value):
            return Intent.NETWORK_DIAGNOSTIC
        return Intent.NETWORK_STATUS
    if re.search(r"\b(pc|computer|system)\b.*\b(health|healthy|checkup|summary|info|status)\b|\b(check|scan)\b.*\b(pc|computer|system)\b", value):
        return Intent.PC_HEALTH
    if re.search(r"\b(game|gaming|fortnite|steam)\b", value):
        return Intent.GAMING
    if re.search(r"\b(security|defender|virus|malware|firewall|threat)\b", value):
        return Intent.SECURITY
    if re.match(r"^(open|close|launch|start|stop|run|take|clear|reset|approve|deny)\b", value):
        return Intent.SYSTEM_ACTION
    if re.search(r"\b(crash|error|broken|fix|problem|issue|slow|freeze|stutter|not working|why)\b", value):
        return Intent.TROUBLESHOOTING
    if re.search(r"\b(what|what's|whats|how|when|where|who|can you|could you|tell me|explain)\b", value):
        return Intent.GENERAL_CHAT
    return Intent.UNKNOWN


def is_storage_investigation(text: str) -> bool:
    value = text.lower()
    return bool(re.search(r"\b(storage|disk|drive|space)\b", value) and
                re.search(r"\b(investigate|analyze|analyse|why|full|taking|using|cleanup|clean up|free up|running out)\b", value))
