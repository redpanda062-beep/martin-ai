from __future__ import annotations

import sqlite3
import threading
import logging
from pathlib import Path
from typing import Iterable

from ..paths import DATABASE_PATH


DEFAULT_DATABASE_PATH = DATABASE_PATH
LOGGER = logging.getLogger("martin_ai.memory")


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS incidents (
 id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 fingerprint TEXT NOT NULL, kind TEXT NOT NULL, application TEXT,
 problem TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
 details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint ON incidents(fingerprint, created_at);
CREATE TABLE IF NOT EXISTS observations (
 id INTEGER PRIMARY KEY, incident_id INTEGER, observed_at TEXT NOT NULL,
 source TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
 data_json TEXT NOT NULL, FOREIGN KEY(incident_id) REFERENCES incidents(id)
);
CREATE TABLE IF NOT EXISTS actions (
 id INTEGER PRIMARY KEY, incident_id INTEGER, created_at TEXT NOT NULL,
 trigger_text TEXT NOT NULL, tool_name TEXT NOT NULL, arguments_json TEXT NOT NULL,
 risk_level TEXT NOT NULL, permission_level INTEGER NOT NULL,
 approval_status TEXT NOT NULL, simulation INTEGER NOT NULL DEFAULT 1,
 rollback_json TEXT, FOREIGN KEY(incident_id) REFERENCES incidents(id)
);
CREATE TABLE IF NOT EXISTS action_results (
 id INTEGER PRIMARY KEY, action_id INTEGER NOT NULL, created_at TEXT NOT NULL,
 success INTEGER NOT NULL, result_json TEXT NOT NULL, verification_json TEXT,
 error_trace TEXT, FOREIGN KEY(action_id) REFERENCES actions(id)
);
CREATE TABLE IF NOT EXISTS known_fixes (
 id INTEGER PRIMARY KEY, signature TEXT NOT NULL, application TEXT,
 tool_name TEXT NOT NULL, arguments_json TEXT NOT NULL, success_count INTEGER NOT NULL DEFAULT 0,
 failure_count INTEGER NOT NULL DEFAULT 0, last_success_at TEXT, UNIQUE(signature, application, tool_name)
);
CREATE TABLE IF NOT EXISTS game_profiles (
 game_key TEXT PRIMARY KEY, display_name TEXT NOT NULL, profile_json TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_preferences (
 key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
 id INTEGER PRIMARY KEY,
 category TEXT NOT NULL,
 memory_key TEXT NOT NULL,
 content TEXT NOT NULL,
 source TEXT NOT NULL,
 importance INTEGER NOT NULL DEFAULT 3,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 last_accessed TEXT,
 UNIQUE(category, memory_key)
);
CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category, updated_at);
CREATE INDEX IF NOT EXISTS idx_memories_content ON memories(content);
CREATE TABLE IF NOT EXISTS hardware_profile (
 key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS application_history (
 id INTEGER PRIMARY KEY, application TEXT NOT NULL, event_type TEXT NOT NULL,
 occurred_at TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS audit_log (
 id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, event_type TEXT NOT NULL,
 trigger_text TEXT, observation_json TEXT, proposed_action TEXT, risk_level TEXT,
 permission_level INTEGER, approval_status TEXT, action_executed INTEGER,
 result_json TEXT, verification_json TEXT, rollback_json TEXT
);
CREATE TABLE IF NOT EXISTS repair_history (
 id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, problem TEXT NOT NULL,
 diagnostics_json TEXT NOT NULL DEFAULT '[]', repair_id TEXT,
 permission_level INTEGER NOT NULL, user_approved INTEGER,
 repair_result_json TEXT, verification_result_json TEXT,
 rollback_result_json TEXT, status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_repair_history_time ON repair_history(timestamp);
"""


class Database:
    def __init__(self, path: Path = DEFAULT_DATABASE_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        LOGGER.info("[MEMORY] database: %s", self.path.resolve())
        self._local = threading.local()
        self._initialize()

    def connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            self._local.connection = connection
        return connection

    def _initialize(self) -> None:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.executescript(SCHEMA)
            connection.commit()
        finally:
            connection.close()

    def execute(self, sql: str, parameters: Iterable[object] = ()) -> sqlite3.Cursor:
        connection = self.connection()
        cursor = connection.execute(sql, tuple(parameters))
        connection.commit()
        return cursor

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None
