# PHASE: runtime
"""
backend/schedule.py
===================
Leitner spaced-repetition scheduler (CLAUDE.md "Leitner Scheduler"). The next
review date is always computed by this deterministic function -- never by the
model. Five boxes; pass (>= 70%) moves up one (max 5), fail resets to box 1.
"""

import sqlite3
from datetime import date, timedelta

LEITNER_INTERVALS = {1: 1, 2: 2, 3: 4, 4: 7, 5: 15}
PASS_THRESHOLD = 70


def update_leitner(current_box: int, score_pct: int) -> tuple[int, str]:
    """Return (new_box, next_review_iso) for a graded objective.

    Pass (score >= 70) -> up one box (capped at 5). Fail -> reset to box 1.
    next_review = today + the new box's interval, as an ISO date string.
    """
    passed = score_pct >= PASS_THRESHOLD
    new_box = min(current_box + 1, 5) if passed else 1
    days = LEITNER_INTERVALS[new_box]
    return new_box, (date.today() + timedelta(days=days)).isoformat()


def get_due_objectives(db: sqlite3.Connection, subject_id: str) -> list[dict]:
    """Weak objectives due for review today or earlier, lowest box first.

    Deterministic query of weakness_log (CLAUDE.md "Build revision plan").
    """
    today = date.today().isoformat()
    rows = db.execute(
        """
        SELECT objective_id, subject_id, score_pct, reason, leitner_box, next_review
        FROM   weakness_log
        WHERE  subject_id = ?
          AND  next_review <= ?
        ORDER  BY leitner_box ASC, next_review ASC
        """,
        (subject_id, today),
    ).fetchall()
    return [dict(r) for r in rows]
