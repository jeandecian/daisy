"""Minimal SQLite persistence for chat messages (standard library only)."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

DEFAULT_DB_PATH: Final[Path] = Path("data/daisy.db")
VALID_ROLES: Final[frozenset[str]] = frozenset({"user", "assistant", "system"})
SQLITE_TIMEOUT_SECONDS: Final[float] = 5.0

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages (created_at, id);
"""


def _resolve(db_path: Path | None) -> Path:
    return DEFAULT_DB_PATH if db_path is None else Path(db_path)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=SQLITE_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    # synchronous is per-connection, so it is set on every connection.
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: Path | None = None) -> Path:
    """Create the parent directory, WAL-mode database file, table, and indices.

    Safe to call repeatedly. Returns the resolved database path.
    """
    path = _resolve(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return path


def save_message(
    role: str,
    content: str,
    model: str | None = None,
    db_path: Path | None = None,
) -> int:
    """Insert a message and return its id. Role must be user, assistant, or system."""
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role: {role!r}")
    path = init_db(db_path)
    created_at = datetime.now(UTC).isoformat()
    conn = _connect(path)
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO messages (role, content, model, created_at) VALUES (?, ?, ?, ?)",
                (role, content, model, created_at),
            )
            message_id = cursor.lastrowid
    finally:
        conn.close()
    if message_id is None:
        raise RuntimeError("SQLite did not return an id for the inserted message")
    return message_id


def get_history(limit: int = 50, db_path: Path | None = None) -> list[dict[str, Any]]:
    """Return the most recent `limit` messages, ordered by created_at ascending."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    path = init_db(db_path)
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            SELECT id, role, content, model, created_at FROM (
                SELECT id, role, content, model, created_at
                FROM messages
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            )
            ORDER BY created_at ASC, id ASC
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]
