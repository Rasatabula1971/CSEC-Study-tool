"""
tests/test_lesson_flow_unified.py
=================================
UI overhaul session 3: the single shared lesson-render path + retry wiring.

(1) All three Study entry points (batch step, jump-to-objective, objective-map
    click) render the SAME objective through one path. The frontend funnels them
    into renderObjectiveLesson(); the backing data is identical because both the
    batch teach call (/api/chat route=teach) and the jump/map call
    (/api/objective/{id}) route through controller.handle_request(route='teach').
    This test asserts those two endpoints return the same lesson for one objective.

(2) Submitting then retrying records is_retry=0 then is_retry=1 in study_sessions
    (the wiring confirms session 1's parameter reaches the Study grade path), with
    the original attempt preserved.

Run: pytest tests/test_lesson_flow_unified.py -v
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import app as app_module      # noqa: E402
import controller             # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"
SUBJECT = "Principles_of_Business"


def make_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping unified-flow tests")
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    for stmt in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        if stmt.strip():
            db.execute(stmt)
    db.commit()
    app_module.apply_runtime_migrations(db)
    db.execute("INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
               (SUBJECT, "Principles of Business"))
    db.execute("INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
               "VALUES ('SEC-1', ?, 'Nature of Business', '1')", (SUBJECT,))
    db.execute("INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
               "content_stmt) VALUES ('POB-1.1', 'SEC-1', ?, '1.1', 'Define a business')", (SUBJECT,))
    db.execute("INSERT INTO objective_lessons (lesson_id, objective_id, subject_id, lesson_text, "
               "recall_questions, source_chunk_ids, confidence) "
               "VALUES ('L11', 'POB-1.1', ?, 'A business supplies goods or services to satisfy needs.', "
               "'[\"Define the term business.\"]', '[\"c1\"]', 90)", (SUBJECT,))
    db.commit()
    return db


# --- (1) shared lesson path -------------------------------------------------
def test_batch_and_jump_entry_points_return_same_lesson():
    db = make_db()
    app_module.app.state.db = db
    client = TestClient(app_module.app)

    # Jump / map click backing endpoint.
    jump = client.get("/api/objective/POB-1.1").json()
    # Batch step backing endpoint (same controller teach path).
    batch = client.post("/api/chat", json={
        "message": "Teach me this objective", "subject_id": SUBJECT,
        "route": "teach", "objective_id": "POB-1.1",
    }).json()

    assert jump["objective_id"] == batch["objective_id"] == "POB-1.1"
    assert jump["lesson_source"] == batch["lesson_source"] == "canonical"
    # Identical lesson body + recall question -> one render path, one source.
    assert jump["lesson_text"] == batch["lesson_text"]
    assert jump["recall_questions"] == batch["recall_questions"] == ["Define the term business."]


# --- (2) retry records is_retry 0 then 1 ------------------------------------
SYLLABUS_GRADING_JSON = json.dumps({
    "objective_id": "POB-1.1", "question_id": "q",
    "points": [
        {"mark_point_id": "POB-1.1-syn-1", "awarded": True,
         "evidence": "the student defined a business clearly here"},
        {"mark_point_id": "POB-1.1-syn-2", "awarded": True,
         "evidence": "they described supplying goods and services"},
        {"mark_point_id": "POB-1.1-syn-3", "awarded": False,
         "evidence": "no mention of satisfying needs anywhere here"},
    ],
})


def _stub_chat(messages, system, schema=None):
    return SYLLABUS_GRADING_JSON


def test_retry_records_is_retry_sequence_in_study_sessions():
    db = make_db()
    # A batch is only a subject/scope carrier for a per-objective grade.
    db.execute("INSERT INTO study_batches (subject_id, objective_ids, status) "
               "VALUES (?, '[\"POB-1.1\"]', 'active')", (SUBJECT,))
    batch_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()

    base = {"route": "grade_batch_question", "batch_id": batch_id,
            "objective_id": "POB-1.1", "question_text": "Define a business.",
            "answer": "A business supplies goods and services to people."}

    # First attempt (fresh): is_retry=0.
    out1 = controller.handle_request(db, {**base, "is_retry": False}, chat_fn=_stub_chat)
    assert "error" not in out1
    # Retry: is_retry=1, original row preserved.
    out2 = controller.handle_request(db, {**base, "is_retry": True}, chat_fn=_stub_chat)
    assert "error" not in out2

    rows = db.execute(
        "SELECT is_retry FROM study_sessions WHERE objective_id = 'POB-1.1' ORDER BY session_id"
    ).fetchall()
    assert [r["is_retry"] for r in rows] == [0, 1]   # both attempts kept, flagged correctly
