"""
backend/scope.py
================
Deterministic scope gate (CLAUDE.md, Rule 1 + "Scope Check"). A request is in
scope only when the subject's syllabus is locked AND the objective belongs to
that subject. This is a pure SQLite boolean -- no LLM, no embedding.
"""

import sqlite3


def is_in_scope(db: sqlite3.Connection, subject_id: str, objective_id: str) -> bool:
    """True iff `objective_id` belongs to `subject_id` and that subject is locked."""
    row = db.execute(
        """
        SELECT 1 FROM objectives o
        JOIN   subjects s ON s.subject_id = o.subject_id
        WHERE  o.objective_id = ?
          AND  o.subject_id   = ?
          AND  s.syllabus_locked = 1
        """,
        (objective_id, subject_id),
    ).fetchone()
    return row is not None


def subject_is_locked(db: sqlite3.Connection, subject_id: str) -> bool:
    """True iff the subject exists and its syllabus is locked.

    Used as the subject-level gate before retrieval, so an unlocked/unknown
    subject is refused with no embedding call (CLAUDE.md scope rule).
    """
    row = db.execute(
        "SELECT 1 FROM subjects WHERE subject_id = ? AND syllabus_locked = 1",
        (subject_id,),
    ).fetchone()
    return row is not None


def get_objective(db: sqlite3.Connection, objective_id: str) -> dict | None:
    """Return the full objectives row as a dict, or None if it does not exist."""
    row = db.execute(
        "SELECT * FROM objectives WHERE objective_id = ?", (objective_id,)
    ).fetchone()
    return dict(row) if row is not None else None
