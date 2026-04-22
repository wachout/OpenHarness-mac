"""SQLite persistence for Web UI sessions and chat messages."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


def _default_db_path() -> Path:
    """Return default DB path: ~/.openharness/data/web_sessions.db"""
    base = Path.home() / ".openharness" / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base / "web_sessions.db"


class WebSessionDB:
    """Thin SQLite wrapper for session + message persistence."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._path = str(db_path or _default_db_path())
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    # ── Schema ───────────────────────────────────────────────────────────────

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id   TEXT PRIMARY KEY,
                name         TEXT NOT NULL DEFAULT '',
                cwd          TEXT NOT NULL DEFAULT '',
                model        TEXT NOT NULL DEFAULT '',
                active_profile TEXT DEFAULT '',
                permission_mode TEXT DEFAULT 'auto',
                api_key      TEXT DEFAULT '',
                api_format   TEXT DEFAULT '',
                base_url     TEXT DEFAULT '',
                created_at   INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                role         TEXT NOT NULL,
                content      TEXT NOT NULL DEFAULT '',
                created_at   REAL NOT NULL DEFAULT 0,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, created_at);
        """)
        self._conn.commit()

    # ── Session CRUD ─────────────────────────────────────────────────────────

    def save_session(
        self,
        *,
        session_id: str,
        name: str,
        cwd: str = "",
        model: str = "",
        active_profile: str = "",
        permission_mode: str = "auto",
        api_key: str = "",
        api_format: str = "",
        base_url: str = "",
        created_at: int | None = None,
    ) -> None:
        ts = created_at or int(time.time())
        self._conn.execute(
            """INSERT OR REPLACE INTO sessions
               (session_id, name, cwd, model, active_profile,
                permission_mode, api_key, api_format, base_url, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (session_id, name, cwd, model, active_profile,
             permission_mode, api_key, api_format, base_url, ts),
        )
        self._conn.commit()

    def delete_session(self, session_id: str) -> None:
        self._conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        self._conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        self._conn.commit()

    def list_sessions(self) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC"
        )
        return [dict(row) for row in cur.fetchall()]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ── Message CRUD ─────────────────────────────────────────────────────────

    def add_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        created_at: float | None = None,
    ) -> int:
        ts = created_at or time.time()
        cur = self._conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
            (session_id, role, content, ts),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT role, content, created_at FROM messages WHERE session_id=? ORDER BY created_at ASC",
            (session_id,),
        )
        return [dict(row) for row in cur.fetchall()]

    def message_count(self, session_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,)
        )
        return cur.fetchone()[0]

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        self._conn.close()
