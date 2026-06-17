# PHASE: runtime
"""
backend/weakness.py
===================
Validated weakness-log writer (CLAUDE.md: "Write weakness record | Pydantic ->
SQLite. Never parse from free text"). A grading result is validated by a Pydantic
model before it touches the DB; invalid input raises ValueError -- it never fails
silently.

Upsert rule (Stage 5 spec):
  * existing objective_id -> update score_pct/reason and advance the Leitner box
    via update_leitner() with the new score.
  * new objective_id      -> insert at box 1 with next_review = today.

weakness_log has no session column (see schema.sql), so `session_id` is accepted
for the controller's interface and traceability but is not persisted here.
"""

import sqlite3
from datetime import date

from pydantic import BaseModel, ValidationError

from schedule import update_leitner


class WeaknessInput(BaseModel):
    """The minimum a grading result must carry to record a weakness."""
    objective_id: str
    subject_id: str
    score_pct: int
    reason: str | None = None
    missed_points: list[str] | None = None


def _reason_for(data: WeaknessInput) -> str | None:
    if data.reason:
        return data.reason
    if data.missed_points:
        return "missed: " + ", ".join(data.missed_points)
    return None


def log_weakness(db: sqlite3.Connection, grading_result: dict, session_id: int) -> dict:
    """Validate and upsert a weakness record. Returns the resulting row state.

    Raises ValueError if `grading_result` is missing required fields.
    """
    try:
        data = WeaknessInput.model_validate(grading_result)
    except ValidationError as exc:
        raise ValueError(f"invalid grading_result for weakness_log: {exc}") from exc

    reason = _reason_for(data)
    existing = db.execute(
        "SELECT weakness_id, leitner_box FROM weakness_log WHERE objective_id = ?",
        (data.objective_id,),
    ).fetchone()

    if existing is not None:
        new_box, next_review = update_leitner(existing["leitner_box"], data.score_pct)
        db.execute(
            """
            UPDATE weakness_log
            SET    score_pct = ?, reason = ?, leitner_box = ?, next_review = ?,
                   updated_at = datetime('now')
            WHERE  weakness_id = ?
            """,
            (data.score_pct, reason, new_box, next_review, existing["weakness_id"]),
        )
    else:
        new_box = 1
        next_review = date.today().isoformat()
        db.execute(
            """
            INSERT INTO weakness_log
                (objective_id, subject_id, score_pct, reason, leitner_box, next_review)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (data.objective_id, data.subject_id, data.score_pct, reason, new_box, next_review),
        )
    db.commit()

    return {
        "objective_id": data.objective_id,
        "subject_id": data.subject_id,
        "score_pct": data.score_pct,
        "leitner_box": new_box,
        "next_review": next_review,
        "reason": reason,
    }
