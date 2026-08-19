from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from .database import Database

LOGGER = logging.getLogger("martin_ai.memory")
LOGGER.setLevel(logging.INFO)
_CONCEPTS = {
    "answer": {"answer", "answers", "response", "responses", "respond", "reply", "replies"},
    "preference": {"prefer", "prefers", "preference", "preferences", "like", "likes", "want", "wants"},
    "short": {"short", "brief", "concise", "compact", "succinct"},
    "gaming": {"game", "games", "gaming", "gamer"},
    "computer": {"pc", "computer", "system", "machine"},
}
_TOKEN_CONCEPT = {word: concept for concept, words in _CONCEPTS.items() for word in words}
_QUERY_STOP_WORDS = {"what", "do", "did", "you", "remember", "about", "how", "i", "me", "my", "the", "a", "an", "that", "to", "of", "is", "are"}


def semantic_terms(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {_TOKEN_CONCEPT.get(word, word) for word in words if word not in _QUERY_STOP_WORDS and len(word) > 1}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_json(value: Any) -> str:
    text = json.dumps(value, default=str)
    # Values named like credentials are never persisted.
    blocked = ("password", "token", "cookie", "secret", "authorization")
    if isinstance(value, dict):
        value = {key: val for key, val in value.items() if not any(word in key.lower() for word in blocked)}
        text = json.dumps(value, default=str)
    return text


class MemoryManager:
    def __init__(self, database: Database | None = None) -> None:
        self.db = database or Database()
        self.pending_clear = False

    @staticmethod
    def fingerprint(kind: str, application: str | None, problem: str) -> str:
        canonical = "|".join((kind.lower(), (application or "").lower(), problem.lower().strip()))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]

    def save_incident(self, kind: str, problem: str, *, application: str | None = None,
                      severity: str = "medium", details: dict[str, Any] | None = None) -> int:
        stamp = now()
        cursor = self.db.execute(
            "INSERT INTO incidents(created_at,updated_at,fingerprint,kind,application,problem,severity,details_json) VALUES(?,?,?,?,?,?,?,?)",
            (stamp, stamp, self.fingerprint(kind, application, problem), kind, application, problem, severity, clean_json(details or {})),
        )
        return int(cursor.lastrowid)

    def save_observation(self, source: str, kind: str, data: dict[str, Any], *,
                         incident_id: int | None = None, severity: str = "info") -> int:
        cursor = self.db.execute(
            "INSERT INTO observations(incident_id,observed_at,source,kind,severity,data_json) VALUES(?,?,?,?,?,?)",
            (incident_id, now(), source, kind, severity, clean_json(data)),
        )
        return int(cursor.lastrowid)

    def save_action(self, tool_name: str, arguments: dict[str, Any], *, trigger: str,
                    risk_level: str, permission_level: int, approval_status: str,
                    simulation: bool, incident_id: int | None = None,
                    rollback: dict[str, Any] | None = None) -> int:
        cursor = self.db.execute(
            "INSERT INTO actions(incident_id,created_at,trigger_text,tool_name,arguments_json,risk_level,permission_level,approval_status,simulation,rollback_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (incident_id, now(), trigger, tool_name, clean_json(arguments), risk_level, permission_level,
             approval_status, int(simulation), clean_json(rollback) if rollback else None),
        )
        return int(cursor.lastrowid)

    def save_result(self, action_id: int, success: bool, result: dict[str, Any], *,
                    verification: dict[str, Any] | None = None, error_trace: str | None = None) -> int:
        cursor = self.db.execute(
            "INSERT INTO action_results(action_id,created_at,success,result_json,verification_json,error_trace) VALUES(?,?,?,?,?,?)",
            (action_id, now(), int(success), clean_json(result), clean_json(verification) if verification else None, error_trace),
        )
        return int(cursor.lastrowid)

    def find_similar_incidents(self, kind: str, problem: str, *, application: str | None = None,
                               limit: int = 5) -> list[dict[str, Any]]:
        rows = self.db.connection().execute(
            "SELECT * FROM incidents WHERE fingerprint=? ORDER BY created_at DESC LIMIT ?",
            (self.fingerprint(kind, application, problem), max(1, min(limit, 50))),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_known_fix(self, signature: str, application: str | None = None) -> dict[str, Any] | None:
        row = self.db.connection().execute(
            "SELECT * FROM known_fixes WHERE signature=? AND application IS ? ORDER BY success_count DESC,failure_count ASC LIMIT 1",
            (signature, application),
        ).fetchone()
        return dict(row) if row else None

    def get_game_profile(self, game_key: str) -> dict[str, Any] | None:
        row = self.db.connection().execute("SELECT profile_json FROM game_profiles WHERE game_key=?", (game_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def update_game_profile(self, game_key: str, display_name: str, profile: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO game_profiles(game_key,display_name,profile_json,updated_at) VALUES(?,?,?,?) ON CONFLICT(game_key) DO UPDATE SET display_name=excluded.display_name,profile_json=excluded.profile_json,updated_at=excluded.updated_at",
            (game_key, display_name, clean_json(profile), now()),
        )

    def list_game_profiles(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.db.connection().execute(
            "SELECT game_key,display_name,profile_json,updated_at FROM game_profiles ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try: item["profile"] = json.loads(item.pop("profile_json"))
            except json.JSONDecodeError: item["profile"] = {}
            result.append(item)
        return result

    def incident_history(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.connection().execute(
            "SELECT created_at,application,problem,status,severity FROM incidents ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def save_repair_history(self, problem: str, diagnostics: list[str], repair_id: str | None,
                            permission_level: int, user_approved: bool | None,
                            repair_result: dict[str, Any] | None,
                            verification_result: dict[str, Any] | None,
                            rollback_result: dict[str, Any] | None, status: str) -> int:
        cursor = self.db.execute(
            "INSERT INTO repair_history(timestamp,problem,diagnostics_json,repair_id,permission_level,user_approved,"
            "repair_result_json,verification_result_json,rollback_result_json,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (now(), problem[:500], clean_json(diagnostics), repair_id, int(permission_level),
             None if user_approved is None else int(user_approved), clean_json(repair_result) if repair_result else None,
             clean_json(verification_result) if verification_result else None,
             clean_json(rollback_result) if rollback_result else None, status),
        )
        return int(cursor.lastrowid)

    def repair_history(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.connection().execute(
            "SELECT id,timestamp,problem,diagnostics_json,repair_id,permission_level,user_approved,"
            "repair_result_json,verification_result_json,rollback_result_json,status "
            "FROM repair_history ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 200)),)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            names = {"diagnostics_json": "diagnostics", "repair_result_json": "repair_result",
                     "verification_result_json": "verification", "rollback_result_json": "rollback"}
            for key, output_key in names.items():
                if item.get(key):
                    try: item[output_key] = json.loads(item[key])
                    except json.JSONDecodeError: item[output_key] = None
                item.pop(key, None)
            result.append(item)
        return result

    @staticmethod
    def memory_key(content: str) -> str:
        words = re.findall(r"[a-z0-9]+", content.lower())
        stop = {"i", "me", "my", "that", "the", "a", "an", "is", "are", "to", "of", "while", "want"}
        useful = [word for word in words if word not in stop][:8]
        return "-".join(useful) or hashlib.sha256(content.encode()).hexdigest()[:16]

    def save_memory(self, category: str, content: str, *, source: str = "explicit_user",
                    importance: int = 3, key: str | None = None) -> dict[str, Any]:
        category = category.strip().lower()
        if category not in {"user_preference", "pc_information", "user_fact", "ai_configuration"}:
            raise ValueError("Unsupported memory category")
        content = re.sub(r"\s+", " ", content).strip()[:1000]
        if not content: raise ValueError("Memory content is empty")
        memory_key = (key or self.memory_key(content))[:120]
        stamp = now()
        self.db.execute(
            "INSERT INTO memories(category,memory_key,content,source,importance,created_at,updated_at,last_accessed) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(category,memory_key) DO UPDATE SET "
            "content=excluded.content,source=excluded.source,importance=excluded.importance,updated_at=excluded.updated_at",
            (category, memory_key, content, source, max(1, min(int(importance), 5)), stamp, stamp, stamp),
        )
        row = self.db.connection().execute(
            "SELECT * FROM memories WHERE category=? AND memory_key=?", (category, memory_key)
        ).fetchone()
        if row is None:
            raise RuntimeError("Saved memory could not be verified")
        saved = dict(row)
        LOGGER.info("[MEMORY] saved id=%s category=%s key=%s", saved["id"], category, memory_key)
        return saved

    def list_memories(self, *, category: str | None = None, search: str = "", limit: int = 100) -> list[dict[str, Any]]:
        clauses, values = [], []
        if category:
            clauses.append("category=?"); values.append(category)
        if search.strip():
            clauses.append("LOWER(content) LIKE ?"); values.append(f"%{search.strip().lower()}%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(int(limit), 500)))
        rows = self.db.connection().execute(
            f"SELECT * FROM memories{where} ORDER BY importance DESC,updated_at DESC LIMIT ?", values
        ).fetchall()
        return [dict(row) for row in rows]

    def relevant_memories(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        LOGGER.info('[MEMORY] retrieval query="%s"', re.sub(r"\s+", " ", query).strip())
        terms = semantic_terms(query)
        rows = self.list_memories(limit=200)
        scored = []
        for row in rows:
            content_terms = semantic_terms(row["content"])
            overlap = len(terms & content_terms)
            coverage = overlap / max(1, min(len(terms), len(content_terms)))
            if overlap:
                scored.append((overlap * 10 + coverage * 5 + int(row["importance"]), row))
        selected = [row for _, row in sorted(scored, key=lambda item: item[0], reverse=True)[:max(1, min(limit, 10))]]
        if selected:
            stamp = now()
            self.db.connection().executemany("UPDATE memories SET last_accessed=? WHERE id=?", [(stamp, row["id"]) for row in selected])
            self.db.connection().commit()
        LOGGER.info("[MEMORY] matches=%s", len(selected))
        return selected

    def delete_memory(self, memory_id: int) -> bool:
        cursor = self.db.execute("DELETE FROM memories WHERE id=?", (int(memory_id),))
        LOGGER.info("[MEMORY] deleted id=%s found=%s", memory_id, cursor.rowcount > 0)
        return cursor.rowcount > 0

    def forget_matching(self, text: str) -> int:
        terms = semantic_terms(text)
        if not terms: return 0
        rows = self.list_memories(limit=500)
        ids = [row["id"] for row in rows if terms & semantic_terms(row["content"])]
        if ids:
            self.db.connection().executemany("DELETE FROM memories WHERE id=?", [(item,) for item in ids])
            self.db.connection().commit()
        LOGGER.info('[MEMORY] forget query="%s" matches=%s', text, len(ids))
        return len(ids)

    def clear_memories(self) -> int:
        count = int(self.db.connection().execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        self.db.execute("DELETE FROM memories")
        self.pending_clear = False
        return count

    def remember_pc_profile(self, result: dict[str, Any]) -> None:
        if not result.get("success"): return
        stable = {
            "windows": result.get("windows"), "processor": result.get("processor"),
            "physical_cores": result.get("physical_cores"), "logical_cores": result.get("logical_cores"),
            "ram_total_gb": result.get("ram_total_gb"),
            "drives": [{"device": d.get("device"), "total_gb": d.get("total_gb")} for d in result.get("disks", [])],
        }
        self.save_memory("pc_information", clean_json(stable), source="automatic_tool", importance=4, key="system-profile")
