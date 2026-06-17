"""
tests/test_review_queue.py
==========================
Stage 9 (Build Playbook v3.1) tests for backend/review_queue.py, focused on the
Stage 9 additions:

  1. _split_candidate handles BOTH producers' evidence markers
     ("\\n\\nEvidence:" from recovery, " | EVIDENCE: " from syllabus derivation).
  2. promote_row stamps the right source_type by the queued row's reason:
       syllabus_derived_first_run -> 'syllabus_derived'
       low_confidence_extraction / anything else -> 'recovered_extraction'.

Offline + in-memory SQLite. Run: pytest tests/test_review_queue.py -v
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import backend.review_queue as rq  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"
SUBJECT = "Principles_of_Business"
OBJECTIVE = "POB-1.1"
DOC_ID = "doc-1"


def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping review-queue tests")
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


def seed(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES (?, ?, ?, ?)",
        ("POB-SEC-1", SUBJECT, "Nature of Business", "1"),
    )
    db.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
        "content_stmt, skill_type) VALUES (?, ?, ?, ?, ?, ?)",
        (OBJECTIVE, "POB-SEC-1", SUBJECT, "1.1", "Explain a business", "Understanding"),
    )
    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, source_file, "
        "content_hash) VALUES (?, ?, ?, ?, ?)",
        (DOC_ID, SUBJECT, "notes", r"E:\KB\notes.pdf", "hash-1"),
    )
    db.commit()


@pytest.fixture
def db():
    conn = open_test_db()
    rq.ensure_recovery_columns(conn)
    seed(conn)
    yield conn
    conn.close()


def _queue(db, chunk_text, reason):
    cur = db.execute(
        "INSERT INTO ingest_review_queue (source_file, chunk_text, reason, "
        "objective_id, doc_id) VALUES (?, ?, ?, ?, ?)",
        ("derive_syllabus_mark_points", chunk_text, reason, OBJECTIVE, DOC_ID),
    )
    db.commit()
    return dict(db.execute(
        "SELECT * FROM ingest_review_queue WHERE id = ?", (cur.lastrowid,)
    ).fetchone())


# --- _split_candidate ------------------------------------------------------
def test_split_handles_recovery_marker():
    pt, ev = rq._split_candidate("A point\n\nEvidence: the source phrase")
    assert pt == "A point"
    assert ev == "the source phrase"


def test_split_handles_syllabus_marker():
    pt, ev = rq._split_candidate("A point | EVIDENCE: the source phrase")
    assert pt == "A point"
    assert ev == "the source phrase"


def test_split_no_marker():
    pt, ev = rq._split_candidate("Just a point")
    assert pt == "Just a point"
    assert ev == ""


# --- promote_row source_type by reason -------------------------------------
def test_promote_syllabus_derived_row_stamps_syllabus_source_type(db):
    row = _queue(db, "A derived point | EVIDENCE: from notes",
                 "syllabus_derived_first_run")
    mp_id = rq.promote_row(db, row)
    assert mp_id is not None
    r = db.execute(
        "SELECT point_text, source_type FROM mark_points WHERE mark_point_id = ?",
        (mp_id,),
    ).fetchone()
    assert r["point_text"] == "A derived point"  # evidence stripped
    assert r["source_type"] == "syllabus_derived"
    # row removed from the queue after promotion
    assert db.execute("SELECT COUNT(*) FROM ingest_review_queue").fetchone()[0] == 0


def test_promote_recovery_row_stays_recovered(db):
    row = _queue(db, "A recovered point\n\nEvidence: verbatim",
                 "low_confidence_extraction")
    mp_id = rq.promote_row(db, row)
    r = db.execute(
        "SELECT source_type FROM mark_points WHERE mark_point_id = ?", (mp_id,)
    ).fetchone()
    assert r["source_type"] == "recovered_extraction"


def test_delete_row_removes_without_writing(db):
    row = _queue(db, "Reject me | EVIDENCE: x", "syllabus_derived_first_run")
    rq.delete_row(db, row["id"])
    assert db.execute("SELECT COUNT(*) FROM ingest_review_queue").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM mark_points").fetchone()[0] == 0
