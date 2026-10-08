from __future__ import annotations

import re
from typing import Any

from .intent import is_storage_investigation
from .tools.storage import _cleanup_suggestions, _file_category


def answer_investigation_followup(text: str, state: Any) -> str | None:
    """Explain retained evidence without model calls, new tools, or actions."""
    lower = text.lower().strip()
    if is_storage_investigation(text) or not re.match(
        r"^(what\b|what's\b|whats\b|explain\b|tell me\b|which\b|can i\b|is (?:it|that|this)\b)", lower
    ):
        return None
    if not re.search(r"\b(vortex|nvidia|cache|files?|ram|memory|storage|disk|events?|findings?|results?|that|this|it)\b|\.(zip|7z|rar|nvph)\b", lower):
        return None
    file_topic = bool(re.search(r"\b(vortex|cache|files?)\b|\.(zip|7z|rar|nvph)\b|^(can i (delete|remove|move)|is it safe)", lower))
    reference = bool(re.search(r"\b(that|this|those|these|latest|last|results?|findings?)\b", lower))
    if not file_topic and not reference:
        return None
    if state is None or getattr(state, "status", "") != "completed":
        return "I don't have a completed investigation in this session yet. Run an investigation first so I can explain its findings."
    prefix = f"**From the latest completed investigation** ({getattr(state, 'updated_at', 'saved snapshot')}). No new check was run.\n\n"
    scans = [item.get("result", {}) for item in getattr(state, "evidence", [])
             if item.get("tool") == "investigate_storage" and isinstance(item.get("result"), dict)
             and item["result"].get("success") is not False]
    if file_topic:
        if not scans:
            return prefix + "That investigation has no storage file list. Run 'Investigate storage' to collect one."
        files = scans[-1].get("largest_files", [])
        category = "Vortex mod archives" if "vortex" in lower else "NVIDIA shader cache" if "nvidia" in lower or "cache" in lower else None
        named = [file for file in files if str(file.get("path", "")).replace("\\", "/").split("/")[-1].lower() in lower]
        selected = named or [file for file in files if category and _file_category(file) == category]
        if not selected and category:
            return prefix + "No matching file was retained in this scan. A partial scan does not prove that the file is absent."
        if not selected:
            choices = "\n".join(f"- {file.get('path')} ({file.get('bytes', 0) / 2**30:.2f} GB)" for file in files[:5])
            return prefix + ("Which file do you mean? You can name Vortex, NVIDIA, or a filename from this list:\n\n" + choices if choices else "No large files were retained in this scan.")
        descriptions = "\n".join(f"- {file.get('path')} ({file.get('bytes', 0) / 2**30:.2f} GB observed)" for file in selected[:10])
        options = "\n\n".join(_cleanup_suggestions(selected))
        return prefix + descriptions + "\n\n" + options + "\n\nNo files were changed. File contents and installation status were not checked."
    lines = list(getattr(state, "confirmed_findings", [])) + list(getattr(state, "normal_readings", []))
    topic = "ram" if re.search(r"\b(ram|memory)\b", lower) else "windows" if "event" in lower else None
    if topic:
        lines = [line for line in lines if topic in line.lower()]
    if not lines:
        return prefix + "There is no matching measurement in the retained findings. The saved snapshot cannot establish the current condition."
    return prefix + "\n".join(f"- {line}" for line in lines) + "\n\n" + str(getattr(state, "diagnosis", "")) + "\n\nThese readings do not establish the cause of the reported problem."
