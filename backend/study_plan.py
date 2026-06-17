# PHASE: runtime
"""
backend/study_plan.py
=====================
Deterministic Study Plan engine (CLAUDE.md "Deterministic vs LLM"). Every
objective the student studies resolves to a real objective_id (Rule 1); all
status arithmetic and the next-objective ordering are pure Python/SQLite -- the
LLM never decides whether an objective is met, mastered, or what to study next.

Status model (study_plan.status):
    unmet        -- never passed, or reset after a fail
    in_progress  -- reserved: an objective mid-batch that has not been graded yet
    met_once      -- passed on one distinct day (met_count = 1)
    mastered      -- passed on two distinct days (met_count = 2)

Mastery rule (reconciled with the test suite + manual acceptance criteria):
    "Met"      = score_pct >= 70 on an objective.
    "Mastered" = met on TWO SEPARATE days (not the same day).
A single batch grades each objective up to twice the same day (its own question
plus the synthesis question); the same-day guard below makes the second same-day
pass a no-op, so one batch leaves a passing objective at 'met_once', never
'mastered'. A pass on a later day advances met_once -> mastered. Any fail resets
to 'unmet' with met_count = 0.

This module owns study_plan writes; weakness_log is updated through the existing
log_weakness() so Leitner scheduling stays in one place.
"""

import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schedule import PASS_THRESHOLD  # noqa: E402
from weakness import log_weakness  # noqa: E402


def init_plan_for_subject(db: sqlite3.Connection, subject_id: str) -> int:
    """Seed study_plan with every objective for the subject as 'unmet'.

    Idempotent: INSERT OR IGNORE leans on UNIQUE(subject_id, objective_id), so a
    re-run inserts nothing for objectives already present. Returns the number of
    rows actually inserted this call.
    """
    cur = db.execute(
        """
        INSERT OR IGNORE INTO study_plan (subject_id, objective_id, status)
        SELECT subject_id, objective_id, 'unmet'
        FROM   objectives
        WHERE  subject_id = ?
        """,
        (subject_id,),
    )
    db.commit()
    return cur.rowcount


def get_next_batch(db: sqlite3.Connection, subject_id: str,
                   batch_size: int = 5) -> list[dict]:
    """Return the next N objectives to study, highest priority first.

    Priority order (CLAUDE.md "Build revision plan" -- a deterministic query):
      1. Leitner-due reviews (weakness_log.next_review <= today), lowest box
         first, then least-recently updated.
      2. status='unmet' objectives, in syllabus order (section_num, objective_num).
    De-duplicated by objective_id, capped at batch_size. Returns full objective
    rows enriched with section info (section_title, section_num). Fewer than
    batch_size rows are returned when fewer are available.
    """
    today = date.today().isoformat()
    picked: list[dict] = []
    seen: set[str] = set()

    due = db.execute(
        """
        SELECT o.objective_id, o.objective_num, o.content_stmt, o.skill_type,
               o.command_words, o.section_id,
               s.title       AS section_title,
               s.section_num AS section_num,
               w.leitner_box AS leitner_box,
               w.next_review AS next_review,
               'review'      AS source
        FROM   weakness_log w
        JOIN   objectives o          ON o.objective_id = w.objective_id
        JOIN   syllabus_sections s   ON s.section_id   = o.section_id
        WHERE  w.subject_id = ?
          AND  w.next_review <= ?
        ORDER  BY w.leitner_box ASC, w.updated_at ASC
        """,
        (subject_id, today),
    ).fetchall()
    for r in due:
        if r["objective_id"] in seen:
            continue
        picked.append(dict(r))
        seen.add(r["objective_id"])
        if len(picked) >= batch_size:
            return picked

    unmet = db.execute(
        """
        SELECT o.objective_id, o.objective_num, o.content_stmt, o.skill_type,
               o.command_words, o.section_id,
               s.title       AS section_title,
               s.section_num AS section_num,
               NULL          AS leitner_box,
               NULL          AS next_review,
               'new'         AS source
        FROM   study_plan p
        JOIN   objectives o        ON o.objective_id = p.objective_id
        JOIN   syllabus_sections s ON s.section_id   = o.section_id
        WHERE  p.subject_id = ?
          AND  p.status     = 'unmet'
        ORDER  BY s.section_num ASC, o.objective_num ASC
        """,
        (subject_id,),
    ).fetchall()
    for r in unmet:
        if r["objective_id"] in seen:
            continue
        picked.append(dict(r))
        seen.add(r["objective_id"])
        if len(picked) >= batch_size:
            break

    return picked


def mark_objective_outcome(db: sqlite3.Connection, subject_id: str,
                           objective_id: str, score_pct: int,
                           update_weakness: bool = True) -> dict:
    """Record one graded outcome against an objective and advance its status.

    Transitions (see module docstring for the rationale):
      * fail (score < 70)                 -> status='unmet', met_count=0, last_met cleared.
      * pass on the SAME day as last_met  -> no change (within-batch / same-day
                                             repeat cannot short-circuit mastery).
      * pass on a NEW day                 -> met_count += 1 (capped at 2),
                                             last_met_at = today; met_count 1 ->
                                             'met_once', met_count 2 -> 'mastered'.

    Always upserts weakness_log via log_weakness() unless update_weakness is False
    (the synthesis path logs weakness itself, so the controller suppresses the
    duplicate to avoid advancing the Leitner box twice for one grading).
    """
    today = date.today().isoformat()

    # A row should already exist (init seeds the whole subject); guarantee it so
    # the function is safe to call standalone.
    db.execute(
        "INSERT OR IGNORE INTO study_plan (subject_id, objective_id, status) "
        "VALUES (?, ?, 'unmet')",
        (subject_id, objective_id),
    )
    row = db.execute(
        "SELECT status, met_count, last_met_at FROM study_plan "
        "WHERE subject_id = ? AND objective_id = ?",
        (subject_id, objective_id),
    ).fetchone()
    status = row["status"]
    met_count = row["met_count"]
    last_met_at = row["last_met_at"]

    passed = score_pct >= PASS_THRESHOLD
    if not passed:
        status, met_count, last_met_at = "unmet", 0, None
    elif last_met_at == today:
        # Same-day repeat pass: leave status/met_count/last_met_at untouched.
        pass
    else:
        met_count = min(met_count + 1, 2)
        last_met_at = today
        status = "mastered" if met_count >= 2 else "met_once"

    db.execute(
        "UPDATE study_plan SET status = ?, met_count = ?, last_met_at = ? "
        "WHERE subject_id = ? AND objective_id = ?",
        (status, met_count, last_met_at, subject_id, objective_id),
    )
    db.commit()

    if update_weakness:
        log_weakness(
            db,
            {"objective_id": objective_id, "subject_id": subject_id,
             "score_pct": score_pct},
            session_id=0,
        )

    return {
        "objective_id": objective_id,
        "subject_id": subject_id,
        "status": status,
        "met_count": met_count,
        "last_met_at": last_met_at,
    }


def get_plan_progress(db: sqlite3.Connection, subject_id: str) -> dict:
    """Return mastery counts for a subject's plan. Pure aggregation, no LLM."""
    rows = db.execute(
        "SELECT status, COUNT(*) AS c FROM study_plan "
        "WHERE subject_id = ? GROUP BY status",
        (subject_id,),
    ).fetchall()
    counts = {r["status"]: r["c"] for r in rows}
    mastered = counts.get("mastered", 0)
    total = sum(counts.values())
    return {
        "total": total,
        "mastered": mastered,
        "met_once": counts.get("met_once", 0),
        "in_progress": counts.get("in_progress", 0),
        "unmet": counts.get("unmet", 0),
        "percent_mastered": round(100 * mastered / total) if total else 0,
    }
