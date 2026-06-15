"""
tests/smoke_test.py
===================
Whole-system smoke test. Run this after every Claude Code session to confirm the
deterministic core + controller still wire together end-to-end -- WITHOUT Ollama,
the SSD, or any network.

It builds a fresh in-memory DB with the full schema (+ the runtime-migration
tables app.py adds at boot), seeds a small but realistic POB dataset, and drives
handle_request / the deterministic helpers with a mock chat + mock embed.

The mock chat returns GRADING_SCHEMA-shaped JSON when the system prompt is an
Examiner prompt, and plain lesson text otherwise -- so teach, mark-scheme grading,
and syllabus-fallback grading all exercise real control flow.

Run: pytest tests/smoke_test.py -v   (all 10 tests must pass)
"""

import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from controller import handle_request  # noqa: E402
from schedule import update_leitner  # noqa: E402
from scope import is_in_scope  # noqa: E402
from study_plan import mark_objective_outcome  # noqa: E402
from weakness import log_weakness  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"
SUBJECT = "Principles_of_Business"
TODAY = date.today().isoformat()
EMBED_DIM = 768

# Tables app.py creates as runtime migrations (not in schema.sql). The grade
# fallback + study-plan paths need them present.
RUNTIME_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS practice_questions (
        question_id   TEXT PRIMARY KEY,
        objective_id  TEXT NOT NULL REFERENCES objectives(objective_id),
        subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
        stem          TEXT NOT NULL,
        created_at    TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS study_plan (
        plan_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
        objective_id  TEXT NOT NULL REFERENCES objectives(objective_id),
        status        TEXT NOT NULL DEFAULT 'unmet',
        met_count     INTEGER NOT NULL DEFAULT 0,
        last_met_at   TEXT,
        created_at    TEXT DEFAULT (datetime('now')),
        UNIQUE(subject_id, objective_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS study_batches (
        batch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id      TEXT NOT NULL REFERENCES subjects(subject_id),
        objective_ids   TEXT NOT NULL,
        synthesis_qid   TEXT,
        status          TEXT NOT NULL DEFAULT 'active',
        created_at      TEXT DEFAULT (datetime('now')),
        completed_at    TEXT
    )
    """,
)


# ---------------------------------------------------------------------------
# Mocks (no Ollama / network)
# ---------------------------------------------------------------------------
_OBJ_RE = re.compile(r"[A-Z]{2,6}-\d+\.\d+")  # e.g. POB-1.1


def mock_chat(messages, system, schema=None):
    """Examiner prompt -> GRADING_SCHEMA JSON; anything else -> plain lesson text."""
    text = " ".join(m.get("content", "") for m in messages)
    if "examiner" in system.lower():
        obj = _OBJ_RE.search(text)
        objective_id = obj.group(0) if obj else "POB-1.1"
        qid = re.search(r"QUESTION ID:\s*(\S+)", text)
        question_id = qid.group(1) if qid else "q"
        # Mark-scheme grading hands the model explicit mark_point_id="..." tokens;
        # judge exactly those. The syllabus fallback has none, so emit synthetic ones.
        mp_ids = re.findall(r'mark_point_id="([^"]+)"', text)
        if not mp_ids:
            mp_ids = [f"{objective_id}-syn-{i}" for i in (1, 2, 3)]
        points = [{"mark_point_id": mp, "awarded": True, "evidence": "student covered this"}
                  for mp in mp_ids]
        return json.dumps({"objective_id": objective_id,
                           "question_id": question_id, "points": points})
    return "A business supplies goods and services.\nExample: a bakery.\nQ: What is a business?"


def mock_embed(text):
    """Fixed 768-dim vector -- deterministic, no Ollama."""
    return [0.1] * EMBED_DIM


# ---------------------------------------------------------------------------
# DB + seed
# ---------------------------------------------------------------------------
def open_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping smoke test")
    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    for stmt in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        if stmt.strip():
            conn.execute(stmt)
    for stmt in RUNTIME_MIGRATIONS:
        conn.execute(stmt)
    conn.commit()
    return conn


def seed(conn: sqlite3.Connection) -> None:
    # 1 locked subject
    conn.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Principles of Business"),
    )
    # 2 sections
    conn.executemany(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) VALUES (?, ?, ?, ?)",
        [
            ("POB-SEC-1", SUBJECT, "Nature of Business", "1"),
            ("POB-SEC-2", SUBJECT, "The Entrepreneur", "2"),
        ],
    )
    # 5 objectives across the two sections
    objectives = [
        ("POB-1.1", "POB-SEC-1", "1.1", "Define the term business."),
        ("POB-1.2", "POB-SEC-1", "1.2", "Explain the concept of production."),
        ("POB-2.1", "POB-SEC-2", "2.1", "Describe the functions of an entrepreneur."),
        ("POB-2.2", "POB-SEC-2", "2.2", "Outline the characteristics of an entrepreneur."),
        ("POB-2.3", "POB-SEC-2", "2.3", "Discuss the role of stakeholders in business."),
    ]
    conn.executemany(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, content_stmt) "
        "VALUES (?, ?, ?, ?, ?)",
        [(oid, sec, SUBJECT, num, stmt) for oid, sec, num, stmt in objectives],
    )
    # 1 document (mock past paper)
    conn.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, paper, year, source_file, content_hash) "
        "VALUES ('pp-doc1', ?, 'past_paper', 'Paper 2 - June 2024', 2024, 'june2024_p2.pdf', 'hash-pp1')",
        (SUBJECT,),
    )
    # 3 chunks, each FK'd to an objective. The first has a mark scheme.
    chunks = [
        ("POB-1.1-pp-q1-stem", "POB-1.1", "Define the term 'business' and give one example."),
        ("POB-1.2-pp-q2-stem", "POB-1.2", "Explain what is meant by production in business."),
        ("POB-2.1-pp-q3-stem", "POB-2.1", "State THREE functions of an entrepreneur."),
    ]
    conn.executemany(
        "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, question_num, chunk_id) "
        "VALUES ('pp-doc1', ?, ?, ?, ?, ?)",
        [(oid, SUBJECT, txt, cid.split("-q")[1][0], cid) for cid, oid, txt in chunks],
    )
    # 2 mark_points for the first chunk (question_id == that chunk_id)
    conn.executemany(
        "INSERT INTO mark_points (mark_point_id, objective_id, question_id, doc_id, point_text, "
        "marks_value, point_order) VALUES (?, 'POB-1.1', 'POB-1.1-pp-q1-stem', 'pp-doc1', ?, 1, ?)",
        [
            ("POB-1.1-pp-q1-stem-mp1", "An organisation that supplies goods or services.", 1),
            ("POB-1.1-pp-q1-stem-mp2", "A valid example, e.g. a bakery or a bank.", 2),
        ],
    )
    # 1 practice question with NO mark scheme (drives the syllabus-fallback grade path)
    conn.execute(
        "INSERT INTO practice_questions (question_id, objective_id, subject_id, stem) "
        "VALUES ('practice-POB-1.2-1', 'POB-1.2', ?, 'Explain what production means.')",
        (SUBJECT,),
    )
    # 1 study_plan row (unmet)
    conn.execute(
        "INSERT INTO study_plan (subject_id, objective_id, status, met_count) "
        "VALUES (?, 'POB-2.2', 'unmet', 0)",
        (SUBJECT,),
    )
    # 1 weakness_log row (box 1, due today)
    conn.execute(
        "INSERT INTO weakness_log (objective_id, subject_id, score_pct, reason, leitner_box, next_review) "
        "VALUES ('POB-2.3', ?, 40, 'needs review', 1, ?)",
        (SUBJECT, TODAY),
    )
    conn.commit()


@pytest.fixture
def db():
    conn = open_db()
    seed(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_teach_known_objective(db):
    out = handle_request(
        db,
        {"route": "teach", "subject_id": SUBJECT, "objective_id": "POB-1.1", "query": "teach me"},
        chat_fn=mock_chat, embed_fn=mock_embed,
    )
    assert "lesson" in out
    assert out["objective_id"] == "POB-1.1"


def test_teach_unknown_subject(db):
    out = handle_request(
        db,
        {"route": "teach", "subject_id": "Fake_Subject", "query": "anything"},
        chat_fn=mock_chat, embed_fn=mock_embed,
    )
    assert out == {"error": "out_of_scope"}


def test_grade_with_mark_scheme(db):
    out = handle_request(
        db,
        {"route": "grade", "subject_id": SUBJECT,
         "question_id": "POB-1.1-pp-q1-stem", "student_answer": "A business sells goods, e.g. a bakery."},
        chat_fn=mock_chat, embed_fn=mock_embed,
    )
    assert "error" not in out
    assert {"score_pct", "awarded", "total"} <= set(out)

    # weakness_log was updated for the graded objective.
    row = db.execute(
        "SELECT score_pct FROM weakness_log WHERE objective_id = 'POB-1.1'"
    ).fetchone()
    assert row is not None


def test_grade_without_mark_scheme(db):
    out = handle_request(
        db,
        {"route": "grade", "subject_id": SUBJECT,
         "question_id": "practice-POB-1.2-1", "student_answer": "Production is making goods and services."},
        chat_fn=mock_chat, embed_fn=mock_embed,
    )
    # Fell through to grade_against_syllabus (mock returned valid JSON, Python scored).
    assert "error" not in out
    assert "score_pct" in out
    assert out["objective_id"] == "POB-1.2"


def test_plan_returns_due(db):
    out = handle_request(
        db, {"route": "plan", "subject_id": SUBJECT},
        chat_fn=mock_chat, embed_fn=mock_embed,
    )
    assert "tasks" in out
    objective_ids = [t["objective_id"] for t in out["tasks"]]
    assert "POB-2.3" in objective_ids  # the seeded weak objective, due today


def test_leitner_pass_advances_box():
    new_box, _next = update_leitner(1, 80)
    assert new_box == 2


def test_leitner_fail_resets():
    new_box, _next = update_leitner(3, 40)
    assert new_box == 1


def test_scope_blocks_unlocked_subject(db):
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES ('Economics', 'Economics', 0)"
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES ('ECON-SEC-1', 'Economics', 'Intro', '1')"
    )
    db.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, content_stmt) "
        "VALUES ('ECON-1.1', 'ECON-SEC-1', 'Economics', '1.1', 'Define scarcity.')"
    )
    db.commit()
    assert is_in_scope(db, "Economics", "ECON-1.1") is False


def test_study_plan_mastery(db):
    # A single pass on day 1 reaches 'met_once'. It CANNOT reach 'mastered' in one
    # call: mastery requires passes on two distinct days, and the same-day guard in
    # mark_objective_outcome makes a second same-day pass a no-op.
    result = mark_objective_outcome(db, SUBJECT, "POB-2.2", 80)
    assert result["status"] == "met_once"
    assert result["met_count"] == 1


def test_weakness_log_validates(db):
    with pytest.raises(ValueError):
        log_weakness(db, {"subject_id": SUBJECT, "score_pct": 50}, session_id=0)  # no objective_id
