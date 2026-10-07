from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .memory import MemoryManager

LOGGER = logging.getLogger("martin_ai.memory")


@dataclass
class MemoryCommandResult:
    handled: bool
    message: str = ""
    changed: bool = False


def _category_and_key(content: str) -> tuple[str, str | None]:
    lower = content.lower()
    if "favorite game" in lower: return "user_fact", "favorite-game"
    if "discord" in lower and any(word in lower for word in ("gaming", "game", "closed", "close")):
        return "user_preference", "discord-gaming"
    if any(phrase in lower for phrase in ("short answers", "concise answers", "prefer short", "prefer concise")):
        return "user_preference", "answer-length"
    if "game mode" in lower and any(word in lower for word in ("automatic", "automatically", "activate")):
        return "ai_configuration", "game-mode-automatic"
    if any(word in lower for word in ("prefer", "don't want", "do not want", "always", "never")):
        return "user_preference", None
    if any(word in lower for word in ("setting", "configure", "mode", "autonomy")):
        return "ai_configuration", None
    return "user_fact", None


def _is_memory_recall(lower: str) -> bool:
    return bool(
        "what do you remember" in lower or "what did i tell you" in lower
        or re.match(r"^how do i like (?:you to (?:respond|reply|answer)|(?:your|my) (?:answers?|responses?|replies))\b", lower)
        or re.match(r"^do i prefer .+ answers?", lower)
        or re.match(r"^what are my (?:response|answer) preferences?", lower)
    )


def is_memory_command(text: str, memory: MemoryManager) -> bool:
    lower = re.sub(r"\s+", " ", text.strip().lower())
    detected = bool(memory.pending_clear or re.match(r"^(remember|forget|clear my memor)", lower)
                or _is_memory_recall(lower))
    if detected:
        LOGGER.info("[MEMORY] intent detected: %s", "pending_clear" if memory.pending_clear else lower.split()[0])
    return detected


def _normalize_memory(content: str, category: str, key: str | None) -> str:
    lower = content.lower()
    if key == "answer-length" or (category == "user_preference" and "short" in lower and "answer" in lower):
        return "User prefers short answers."
    if key == "discord-gaming":
        return "User does not want Discord closed while gaming."
    return content[0].upper() + content[1:] + ("" if content.endswith((".", "!", "?")) else ".")


def handle_memory_command(text: str, memory: MemoryManager) -> MemoryCommandResult:
    lower = re.sub(r"\s+", " ", text.strip().lower())
    if memory.pending_clear:
        if lower in {"yes", "yes clear them", "confirm", "clear them", "i'm sure", "im sure"}:
            count = memory.clear_memories()
            return MemoryCommandResult(True, f"**Memory Cleared**\n\nI permanently removed {count} saved memories.", True)
        if lower in {"no", "cancel", "never mind", "nevermind"}:
            memory.pending_clear = False
            return MemoryCommandResult(True, "**All Good**\n\nI kept your saved memories.")
        return MemoryCommandResult(True, "**Action Needed**\n\nPlease say **yes** to clear every saved memory, or **no** to keep them.")
    if re.match(r"^clear my memor(?:y|ies)", lower):
        memory.pending_clear = True
        return MemoryCommandResult(True, "**Action Needed**\n\nThat will permanently delete everything I've remembered about you. Are you sure?")
    remember = re.match(r"^remember(?: that| this)?\s+(.+)$", text.strip(), re.I | re.S)
    if remember:
        content = re.sub(r"\s+", " ", remember.group(1)).strip().rstrip(".")
        category, key = _category_and_key(content)
        normalized = _normalize_memory(content, category, key)
        memory.save_memory(category, normalized, source="explicit_user", importance=5, key=key)
        acknowledgement = "you prefer short answers" if key == "answer-length" else content
        return MemoryCommandResult(True, f"**Remembered**\n\nGot it. I'll remember that {acknowledgement}.", True)
    forget = re.match(r"^forget(?: that| what i said about)?\s*(.+)$", text.strip(), re.I | re.S)
    if forget:
        subject = forget.group(1).strip()
        count = memory.forget_matching(subject)
        message = f"I removed {count} matching memor{'y' if count == 1 else 'ies'}." if count else "I couldn't find a matching saved memory."
        return MemoryCommandResult(True, f"**Memory Updated**\n\n{message}", bool(count))
    if _is_memory_recall(lower):
        category = "pc_information" if "pc" in lower or "computer" in lower else None
        search = ""
        about = re.search(r"(?:about|tell you about)\s+(.+?)[?.]*$", lower)
        if about and about.group(1) not in {"me", "my pc", "my computer"}: search = about.group(1)
        broad_request = ("what do you remember" in lower or "what did i tell you" in lower) and not search
        if broad_request:
            rows = memory.list_memories(category=category, limit=12)
            LOGGER.info('[MEMORY] retrieval query="%s" matches=%s', lower, len(rows))
        else:
            rows = memory.relevant_memories(search or lower, limit=12)
            if category: rows = [row for row in rows if row["category"] == category]
        if not rows: return MemoryCommandResult(True, "**Memory**\n\nI don't have any matching saved memories yet.")
        lines = [f"• {row['content']}" for row in rows]
        return MemoryCommandResult(True, "**Memory**\n\n" + "\n".join(lines))
    return MemoryCommandResult(False)
