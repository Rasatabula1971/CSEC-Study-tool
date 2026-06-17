"""
tests/test_source_rank.py
=========================
Stage 13 (roadmap #3) tests for mark-point source ranking.

Real in-memory SQLite (schema.sql + apply_runtime_migrations + sqlite-vec). Covers:

  1. source_rank backfill maps each source_type/content_type to the right rank.
  2. grade_answer returns source_rank + source_rank_label for real (rank-3) points.
  3. grade_answer REFUSES (returns an error, no LLM call) when a point is rank 5 --
     generated content still pending review in ingest_review_queue.

Run: pytest tests/test_source_rank.py -v
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"

import app as app_module   # noqa: E402  (apply_runtime_migrations)
import grade               # noqa: E402

SUBJECT = "Principles_of_Business"


def open_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping source_rank tests")
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
    app_module.apply_runtime_migrations(db)  # adds source_type/source_rank columns
    return db


def seed_base(db):
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES ('S1', ?, 'Nature of Business', '1')",
        (SUBJECT,),
    )
    db.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
        "content_stmt, command_words) VALUES ('POB-1.1', 'S1', ?, '1.1', "
        "'Explain the concept of a business', '[\"Explain\"]')",
        (SUBJECT,),
    )
    db.commit()


def add_doc(db, doc_id, content_type):
    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, source_file, content_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        (doc_id, SUBJECT, content_type, f"{doc_id}.pdf", f"{doc_id}-hash"),
    )
    db.commit()


def add_mp(db, mpid, qid, source_type, doc_id, marks=1):
    """Insert a mark point with an explicit source_type (NULL allowed) + NULL rank."""
    db.execute(
        "INSERT INTO mark_points (mark_point_id, objective_id, question_id, doc_id, "
        "point_text, marks_value, point_order, source_type, source_rank) "
        "VALUES (?, 'POB-1.1', ?, ?, 'a sufficiently long mark point text', ?, 1, ?, NULL)",
        (mpid, qid, doc_id, marks, source_type),
    )
    db.commit()


def rank_of(db, mpid):
    return db.execute(
        "SELECT source_rank FROM mark_points WHERE mark_point_id = ?", (mpid,)
    ).fetchone()["source_rank"]


def test_source_rank_backfill_mapping():
    """Each source_type/content_type combination backfills to the documented rank."""
    db = open_db()
    seed_base(db)
    add_doc(db, "ms", "mark_scheme")
    add_doc(db, "sp", "specimen")
    add_mp(db, "m_pp", "q_pp", "past_paper", "ms")            # past_paper + mark_scheme -> 3
    add_mp(db, "m_spec", "q_spec", "past_paper", "sp")        # past_paper + specimen   -> 2
    add_mp(db, "m_rec", "q_rec", "recovered_extraction", "ms")  # generated            -> 4
    add_mp(db, "m_syl", "q_syl", "syllabus_derived", "ms")     # generated            -> 4
    add_mp(db, "m_null", "q_null", None, "ms")               # no source_type          -> NULL

    app_module.apply_runtime_migrations(db)  # backfill the NULL-rank rows

    assert rank_of(db, "m_pp") == 3
    assert rank_of(db, "m_spec") == 2
    assert rank_of(db, "m_rec") == 4
    assert rank_of(db, "m_syl") == 4
    assert rank_of(db, "m_null") is None
    db.close()


def test_grade_answer_returns_source_rank_and_label():
    """Two real past-paper points -> source_rank 3, the official-past-paper label."""
    db = open_db()
    seed_base(db)
    add_doc(db, "ms", "mark_scheme")
    add_mp(db, "m1", "q1-stem", "past_paper", "ms", marks=1)
    add_mp(db, "m2", "q1-stem", "past_paper", "ms", marks=2)
    app_module.apply_runtime_migrations(db)  # both -> rank 3

    student_answer = "A business sells goods to make a profit because customers have wants."
    grading_json = json.dumps({
        "objective_id": "POB-1.1", "question_id": "q1", "confidence": 80,
        "points": [
            {"mark_point_id": "m1", "awarded": True,
             "evidence": "sells goods to make a profit because customers have wants",
             "confidence": 80},
            {"mark_point_id": "m2", "awarded": True,
             "evidence": "sells goods to make a profit because customers have wants",
             "confidence": 80},
        ],
    })
    result = grade.grade_answer(db, "q1-stem", student_answer, chat_fn=lambda *a, **k: grading_json)
    assert "error" not in result
    assert result["source_rank"] == 3
    assert result["source_rank_label"] == "Official past paper mark scheme"
    db.close()


def test_grade_answer_refuses_rank5_unreviewed():
    """A generated point (rank 4) whose objective is still queued = rank 5 -> refuse,
    with NO LLM call."""
    db = open_db()
    seed_base(db)
    add_doc(db, "ms", "mark_scheme")
    add_mp(db, "m1", "q1-stem", "syllabus_derived", "ms")  # rank 4 after backfill
    app_module.apply_runtime_migrations(db)
    db.execute(
        "INSERT INTO ingest_review_queue (source_file, chunk_text, reason, objective_id) "
        "VALUES ('derive', 'a point', 'syllabus_derived_first_run', 'POB-1.1')"
    )
    db.commit()

    calls = {"n": 0}

    def chat(*a, **k):
        calls["n"] += 1
        return "{}"

    result = grade.grade_answer(db, "q1-stem", "an answer", chat_fn=chat)
    assert result.get("error") == "mark_points pending review"
    assert result["objective_id"] == "POB-1.1"
    assert result["source_rank"] == 5
    assert calls["n"] == 0, "refused before spending the LLM call"
    db.close()
