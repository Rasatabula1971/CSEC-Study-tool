# PHASE: runtime
"""
backend/app_state.py
====================
App-level singleton state for a single-student, no-accounts offline app (UI
overhaul, session 1). Backed by the generic key-value `app_state` table -- one
row per key. Two keys live here today:

  'current_subject_id'   -> the sticky subject (e.g. 'Principles_of_Business')
  'welcome_message_seen' -> '1' once the first-launch message has been shown

A key-value table (rather than a one-row settings table) is deliberate: this app
has exactly one student, so there is no account/session dimension, and new
single-value UI preferences can be added without a migration.
"""

import sqlite3


def get_state(db: sqlite3.Connection, key: str, default=None):
    """Read a single app_state value. Returns `default` if the key is not set."""
    row = db.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(db: sqlite3.Connection, key: str, value: str) -> None:
    """Write/update a single app_state value (upsert on the key)."""
    db.execute(
        "INSERT INTO app_state (key, value, updated_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = datetime('now')",
        (key, value, value),
    )
    db.commit()


def get_current_subject(db: sqlite3.Connection) -> str | None:
    """The sticky subject_id.

    Defaults to the first LOCKED subject alphabetically when nothing has been
    chosen yet, so a fresh install lands on a real, usable subject (the pilot
    Principles_of_Business until others are signed off). Returns None only when no
    subject is locked at all.
    """
    stored = get_state(db, "current_subject_id")
    if stored:
        return stored
    row = db.execute(
        "SELECT subject_id FROM subjects WHERE syllabus_locked = 1 "
        "ORDER BY subject_id ASC LIMIT 1"
    ).fetchone()
    return row["subject_id"] if row else None


def set_current_subject(db: sqlite3.Connection, subject_id: str) -> None:
    """Persist the sticky subject after validating it is locked.

    The subject MUST exist and be locked (CLAUDE.md scope rule): an unlocked or
    unknown subject raises ValueError rather than being silently stored, so the
    UI can never strand the student on a gated subject.
    """
    row = db.execute(
        "SELECT 1 FROM subjects WHERE subject_id = ? AND syllabus_locked = 1",
        (subject_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Subject {subject_id} is not locked or does not exist")
    set_state(db, "current_subject_id", subject_id)


def has_seen_welcome_message(db: sqlite3.Connection) -> bool:
    """True once the first-launch welcome message has been marked seen."""
    return get_state(db, "welcome_message_seen") == "1"


def mark_welcome_message_seen(db: sqlite3.Connection) -> None:
    """Record that the first-launch welcome message has been shown (one-way)."""
    set_state(db, "welcome_message_seen", "1")
