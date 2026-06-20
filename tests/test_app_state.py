"""
tests/test_app_state.py
=======================
UI overhaul session 1: the app_state key-value store helpers (backend/app_state.py).

DB is built from schema.sql (now including the app_state table); no Ollama, no SSD.
Run: pytest tests/test_app_state.py -v
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import app_state  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"


def open_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping app_state tests")
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    for stmt in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        if stmt.strip():
            db.execute(stmt)
    db.commit()
    return db


def _subject(db: sqlite3.Connection, subject_id: str, locked: int) -> None:
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, ?)",
        (subject_id, subject_id.replace("_", " "), locked),
    )
    db.commit()


def test_default_is_first_locked_subject_alphabetically():
    """Nothing set yet -> the first LOCKED subject alphabetically; unlocked excluded."""
    db = open_db()
    try:
        _subject(db, "Principles_of_Business", 1)
        _subject(db, "Economics", 1)
        _subject(db, "Mathematics", 0)  # unlocked -> never the default
        assert app_state.get_current_subject(db) == "Economics"
    finally:
        db.close()


def test_set_then_get_current_subject_persists():
    db = open_db()
    try:
        _subject(db, "Economics", 1)
        _subject(db, "Principles_of_Business", 1)
        app_state.set_current_subject(db, "Principles_of_Business")
        assert app_state.get_current_subject(db) == "Principles_of_Business"
    finally:
        db.close()


def test_set_current_subject_rejects_unlocked_or_unknown():
    db = open_db()
    try:
        _subject(db, "Principles_of_Business", 1)
        _subject(db, "Economics", 0)  # unlocked
        with pytest.raises(ValueError):
            app_state.set_current_subject(db, "Economics")     # unlocked
        with pytest.raises(ValueError):
            app_state.set_current_subject(db, "Mathematics")   # nonexistent
        # The store was never written.
        assert app_state.get_state(db, "current_subject_id") is None
    finally:
        db.close()


def test_welcome_message_flag_false_then_true():
    db = open_db()
    try:
        _subject(db, "Principles_of_Business", 1)
        assert app_state.has_seen_welcome_message(db) is False
        app_state.mark_welcome_message_seen(db)
        assert app_state.has_seen_welcome_message(db) is True
    finally:
        db.close()
