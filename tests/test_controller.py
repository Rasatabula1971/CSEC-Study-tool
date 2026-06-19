"""
tests/test_controller.py
========================
Runtime teach-route fallback behaviour (the POB-1.11 bug fix).

When an objective has no stored canonical lesson, the teach route must NOT generate
freeform AI lesson content (that produced chat boilerplate + hallucinated "Section N"
citations, which the UI then scraped into fake recall questions). It serves an honest
placeholder instead, with no LLM call. A stored canonical lesson is still served as
before.

All offline: ollama_chat is mocked (must never be called on the fallback path), the
embedder is faked, the DB is in-memory SQLite with the full schema + sqlite-vec +
the Stage 11 lesson tables.

  1. No canonical lesson -> lesson_source='placeholder'.
  2. Placeholder response has empty recall_questions.
  3. No canonical lesson -> chat_fn is NEVER called.
  4. Stored canonical lesson -> lesson_source='canonical' (unchanged).

Run: pytest tests/test_controller.py -v
"""

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import ingest_lessons as il  # noqa: E402
import controller  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"
EMBED_DIM = 768
SUBJECT = "Principles_of_Business"
OBJECTIVE = "POB-1.11"


def fake_embed(_text: str) -> list:
    return [0.0] * EMBED_DIM


def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping controller tests")
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
    il.ensure_lesson_tables(db)
    return db


def seed(db: sqlite3.Connection) -> None:
    """Locked subject + one objective, no chunks, no canonical lesson."""
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES ('POB-SEC-1', ?, 'Nature of Business', '1')",
        (SUBJECT,),
    )
    db.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
        "content_stmt, skill_type, command_words) "
        "VALUES (?, 'POB-SEC-1', ?, '1.11', "
        "'Outline the organisational environment of a business', 'Knowledge', '[\"Outline\"]')",
        (OBJECTIVE, SUBJECT),
    )
    db.commit()


def _insert_canonical(db) -> None:
    db.execute(
        "INSERT INTO objective_lessons (lesson_id, objective_id, subject_id, lesson_text, "
        "worked_examples, key_terms, common_mistakes, recall_questions, source_chunk_ids, "
        "confidence) VALUES ('L1', ?, ?, 'A clean canonical lesson body.', '[]', '[]', "
        "'A mistake.', ?, '[]', 80)",
        (OBJECTIVE, SUBJECT,
         json.dumps(["What is the organisational environment?",
                     "State two internal stakeholders.",
                     "Explain how policy shapes conduct."])),
    )
    db.commit()


@pytest.fixture
def db():
    conn = open_test_db()
    seed(conn)
    yield conn
    conn.close()


def _teach(db, chat):
    return controller.handle_request(
        db,
        {"route": "teach", "subject_id": SUBJECT, "objective_id": OBJECTIVE,
         "message": "Teach me this objective"},
        chat_fn=chat, embed_fn=fake_embed,
    )


# --- Test 1 + 2 ------------------------------------------------------------
def test_no_canonical_returns_placeholder_with_empty_recall(db):
    out = _teach(db, MagicMock())
    assert out["lesson_source"] == "placeholder"
    assert out["recall_questions"] == []
    assert out["objective_id"] == OBJECTIVE
    # the placeholder quotes the syllabus statement so the screen is not empty
    assert "Outline the organisational environment of a business" in out["lesson"]


# --- Test 3 ----------------------------------------------------------------
def test_no_canonical_never_calls_chat_fn(db):
    chat = MagicMock()
    _teach(db, chat)
    chat.assert_not_called()
    # and the objective is flagged for the offline ingest pass
    assert db.execute(
        "SELECT COUNT(*) FROM lesson_generation_queue WHERE objective_id = ? "
        "AND reason = 'served_placeholder'", (OBJECTIVE,)
    ).fetchone()[0] == 1


# --- Test 4 ----------------------------------------------------------------
def test_canonical_lesson_still_served(db):
    _insert_canonical(db)
    chat = MagicMock()
    out = _teach(db, chat)
    assert out["lesson_source"] == "canonical"
    assert len(out["recall_questions"]) == 3
    chat.assert_not_called()
