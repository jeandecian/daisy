"""Tests for SQLite chat persistence: init, WAL mode, save, and history retrieval."""

import sqlite3
from pathlib import Path

import pytest

from daisy.db import get_history, init_db, save_message


def test_init_db_creates_parent_directory_and_file(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "daisy.db"
    assert init_db(db_path) == db_path
    assert db_path.is_file()


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "daisy.db"
    init_db(db_path)
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(messages)")]
    finally:
        conn.close()
    assert columns == ["id", "role", "content", "model", "created_at"]


def test_database_uses_wal_mode(tmp_path: Path) -> None:
    db_path = tmp_path / "daisy.db"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode == "wal"


def test_save_and_get_history_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "daisy.db"
    first = save_message("user", "hi", model="m1", db_path=db_path)
    second = save_message("assistant", "hello", model="m1", db_path=db_path)
    third = save_message("system", "be brief", db_path=db_path)
    assert first < second < third

    history = get_history(db_path=db_path)
    assert [row["role"] for row in history] == ["user", "assistant", "system"]
    assert history[0]["content"] == "hi"
    assert history[0]["model"] == "m1"
    assert history[2]["model"] is None
    assert history[0]["created_at"].endswith("+00:00")
    assert [row["id"] for row in history] == [first, second, third]


def test_get_history_returns_most_recent_limit_in_ascending_order(tmp_path: Path) -> None:
    db_path = tmp_path / "daisy.db"
    for index in range(5):
        save_message("user", f"msg {index}", db_path=db_path)
    history = get_history(limit=2, db_path=db_path)
    assert [row["content"] for row in history] == ["msg 3", "msg 4"]


def test_save_message_rejects_unknown_role(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        save_message("robot", "hi", db_path=tmp_path / "daisy.db")


def test_get_history_rejects_non_positive_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        get_history(limit=0, db_path=tmp_path / "daisy.db")
