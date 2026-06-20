"""
tests/test_grade_retry.py
=========================
UI overhaul session 1: retry-rescore on a recall/quiz question.

A retry overwrites the visible result and the Leitner scheduling decision
(weakness_log upserts by objective_id) while the ORIGINAL attempt is preserved as
its own study_sessions row (is_retry=0 for the first, is_retry=1 for the retry).
These tests drive the controller's grade route (where the study_sessions write
lives -- grade_answer itself is pure) with a stubbed examiner, so no Ollama.

Run: pytest tests/test_grade_retry.py -v
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import controller  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"

# q1 has two 1-mark points. Evidence is >= 20 chars so the Stage 10 thin-evidence
# gate never downgrades an awarded point.
FIRST_ATTEMPT_JSON = (
    '{"objective_id":"POB-1.1","question_id":"q1","points":['
    '{"mark_point_id":"mp1","awarded":true,'
    '"evidence":"the student clearly named an organisation here"},'
    '{"mark_point_id":"mp2","awarded":false,'
    '"evidence":"no mention of goods or services anywhere here"}],"confidence":80}'
)  # 1 of 2 -> 50%

RETRY_JSON = (
    '{"objective_id":"POB-1.1","question_id":"q1","points":['
    '{"mark_point_id":"mp1","awarded":true,'
    '"evidence":"the student clearly named an organisation here"},'
    '{"mark_point_id":"mp2","awarded":true,'
    '"evidence":"the answer supplies goods and services clearly"}],"confidence":90}'
)  # 2 of 2 -> 100%


def _boom_embed(*a, **k):
    raise AssertionError("embedding must not be called on the mark-scheme grade path")


def _make_chat(payload):
    def fake_chat(messages, system, schema=None):
        return payload
    return fake_chat


def _grade(db, payload, is_retry):
    return controller.handle_request(
        db,
        {"route": "grade", "subject_id": "Principles_of_Business",
         "question_id": "q1", "student_answer": "my answer", "is_retry": is_retry},
        chat_fn=_make_chat(payload),
        embed_fn=_boom_embed,
    )


@pytest.fixture
def db():
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping grade-retry tests")
    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    for stmt in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) "
        "VALUES ('Principles_of_Business', 'Principles of Business', 1)"
    )
    conn.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES ('POB-SEC-1', 'Principles_of_Business', 'Nature of Business', '1')"
    )
    conn.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, content_stmt) "
        "VALUES ('POB-1.1', 'POB-SEC-1', 'Principles_of_Business', '1.1', 'Define the term business.')"
    )
    conn.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, source_file, content_hash) "
        "VALUES ('ms1', 'Principles_of_Business', 'mark_scheme', 'ms.pdf', 'h1')"
    )
    for i, mp in enumerate(("mp1", "mp2"), 1):
        conn.execute(
            "INSERT INTO mark_points (mark_point_id, objective_id, question_id, doc_id, "
            "point_text, marks_value, point_order) VALUES (?, 'POB-1.1', 'q1', 'ms1', ?, 1, ?)",
            (mp, f"point {i}", i),
        )
    conn.commit()
    yield conn
    conn.close()


def _sessions(db):
    return db.execute(
        "SELECT session_id, score_pct, is_retry FROM study_sessions "
        "WHERE objective_id = 'POB-1.1' ORDER BY session_id"
    ).fetchall()


def test_first_attempt_logs_is_retry_zero(db):
    out = _grade(db, FIRST_ATTEMPT_JSON, is_retry=False)
    assert out["score_pct"] == 50
    rows = _sessions(db)
    assert len(rows) == 1
    assert rows[0]["is_retry"] == 0
    assert rows[0]["score_pct"] == 50


def test_retry_adds_row_keeping_original(db):
    _grade(db, FIRST_ATTEMPT_JSON, is_retry=False)
    first = _sessions(db)[0]

    _grade(db, RETRY_JSON, is_retry=True)
    rows = _sessions(db)
    # Both attempts are in history -- the original is not overwritten.
    assert len(rows) == 2
    assert rows[0]["session_id"] == first["session_id"]
    assert (rows[0]["is_retry"], rows[0]["score_pct"]) == (0, 50)   # original untouched
    assert (rows[1]["is_retry"], rows[1]["score_pct"]) == (1, 100)  # retry flagged


def test_retry_overwrites_weakness_score(db):
    _grade(db, FIRST_ATTEMPT_JSON, is_retry=False)
    _grade(db, RETRY_JSON, is_retry=True)
    rows = db.execute(
        "SELECT score_pct FROM weakness_log WHERE objective_id = 'POB-1.1'"
    ).fetchall()
    # weakness_log upserts by objective_id: one row, holding the RETRY's score.
    assert len(rows) == 1
    assert rows[0]["score_pct"] == 100
