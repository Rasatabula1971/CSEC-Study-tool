"""
tests/test_lessons.py
=====================
Stage 11 (Build Playbook v3.1) tests for canonical lessons.

All offline: ollama_chat is mocked, the embedder is faked, the DB is an in-memory
SQLite with the full schema + sqlite-vec + the Stage 11 tables. Behaviours covered:

  1. Sufficient sources -> a lesson is written to objective_lessons (no queue row).
  2. Insufficient sources (zero chunks) -> queued, nothing written.
  3. --regenerate replaces the existing lesson row.
  4. teach route serves a stored canonical lesson WITHOUT any LLM call.
  5. teach route with no stored lesson falls back to runtime AND queues the objective.

Run: pytest tests/test_lessons.py -v
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
OBJECTIVE = "POB-1.1"
DOC_NOTES = "notes-doc-1"


# --- fakes -----------------------------------------------------------------
def fake_embed(text: str) -> list[float]:
    """Deterministic dummy embedding -- no Ollama required."""
    return [0.0] * EMBED_DIM


# A valid lesson payload, parameterised by confidence.
def lesson_json(confidence: int) -> str:
    return json.dumps({
        "lesson_text": "A business supplies goods and services to satisfy needs. "
                       "It uses resources to produce things people want.",
        "key_terms": [
            {"term": "business", "definition": "an organisation that supplies goods or services"},
            {"term": "resources", "definition": "the inputs used to produce goods"},
        ],
        "worked_examples": ["A bakery uses flour and labour to make bread to sell."],
        "common_mistakes": "Students often confuse a business with a charity.",
        "recall_questions": [
            "Define a business in your own words.",
            "List two resources a business uses.",
            "Explain why a business needs to make a profit.",
        ],
        "confidence": confidence,
    })


def make_chat(payload_json: str):
    """Mock ollama_chat that always returns this JSON string."""
    def _chat(messages, system, schema=None):
        _chat.calls += 1
        return payload_json
    _chat.calls = 0
    return _chat


# --- in-memory DB ----------------------------------------------------------
def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping lesson tests")
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
    il.ensure_lesson_tables(db)  # objective_lessons + lesson_generation_queue
    return db


def _add_notes_chunk(db, idx):
    cur = db.execute(
        "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page, "
        "question_num, chunk_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (DOC_NOTES, OBJECTIVE, SUBJECT,
         "A business satisfies the needs and wants of consumers and uses resources "
         "to produce goods and services for a profit.", 1, None, f"notes-c{idx}"),
    )
    db.execute(
        "INSERT INTO vec_notes(rowid, embedding) VALUES (?, ?)",
        (cur.lastrowid, il.serialize_vec(fake_embed("x"))),
    )


def seed(db: sqlite3.Connection, *, notes_chunks: int = 5) -> None:
    """Locked subject + one objective + N notes chunks linked to it."""
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
        "content_stmt, skill_type, command_words) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (OBJECTIVE, "POB-SEC-1", SUBJECT, "1.1",
         "Explain the concept of a business", "Understanding", '["Explain"]'),
    )
    if notes_chunks:
        db.execute(
            "INSERT INTO documents (doc_id, subject_id, content_type, source_file, "
            "content_hash) VALUES (?, ?, ?, ?, ?)",
            (DOC_NOTES, SUBJECT, "notes", r"E:\KB\notes.pdf", "hash-notes-1"),
        )
        for i in range(notes_chunks):
            _add_notes_chunk(db, i)
    db.commit()


def _insert_lesson(db, confidence: int, lesson_id="old-lesson-1") -> None:
    """Pre-insert an objective_lessons row (for the canonical/regenerate tests)."""
    db.execute(
        """
        INSERT INTO objective_lessons
            (lesson_id, objective_id, subject_id, lesson_text, worked_examples,
             key_terms, common_mistakes, recall_questions, source_chunk_ids, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (lesson_id, OBJECTIVE, SUBJECT, "Old lesson body.",
         json.dumps(["old example"]),
         json.dumps([{"term": "old", "definition": "old def"}]),
         "Old mistake.",
         json.dumps(["Old Q1?", "Old Q2?", "Old Q3?"]),
         json.dumps(["notes-c0"]), confidence),
    )
    db.commit()


def lesson_count(db) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM objective_lessons WHERE objective_id = ?", (OBJECTIVE,)
    ).fetchone()[0]


def queue_count(db, reason=None) -> int:
    if reason is None:
        return db.execute(
            "SELECT COUNT(*) FROM lesson_generation_queue WHERE objective_id = ?",
            (OBJECTIVE,),
        ).fetchone()[0]
    return db.execute(
        "SELECT COUNT(*) FROM lesson_generation_queue WHERE objective_id = ? AND reason = ?",
        (OBJECTIVE, reason),
    ).fetchone()[0]


@pytest.fixture
def db():
    conn = open_test_db()
    seed(conn)
    yield conn
    conn.close()


# --- tests -----------------------------------------------------------------
def test_ingest_writes_lesson_when_sources_sufficient(db):
    """5 notes chunks + model confidence 85 -> one lesson written, nothing queued."""
    chat = make_chat(lesson_json(85))
    summary = il.ingest_lessons_for_subject(
        db, SUBJECT, chat_fn=chat, embed_fn=fake_embed, verbose=False,
    )

    assert chat.calls == 1, "exactly one composition call for the one objective"
    assert lesson_count(db) == 1, "the lesson is written"
    assert queue_count(db) == 0, "sufficient sources -> nothing queued"
    assert summary["written"] == 1 and summary["queued"] == 0

    row = db.execute(
        "SELECT lesson_text, recall_questions, confidence, source_chunk_ids "
        "FROM objective_lessons WHERE objective_id = ?", (OBJECTIVE,),
    ).fetchone()
    # 5 notes -> local floor 90; the model self-report (85) is ignored, the
    # source-quality floor is stored as the confidence.
    assert row["confidence"] == 90
    assert row["lesson_text"].strip(), "lesson_text is non-empty"
    recall = json.loads(row["recall_questions"])
    assert isinstance(recall, list) and len(recall) == 3
    # source_chunk_ids cite the chunks the lesson was composed from.
    assert json.loads(row["source_chunk_ids"]), "source chunk ids recorded"


def test_zero_model_confidence_uses_source_floor(db):
    """Model returning confidence=0 should not block a lesson backed by strong
    source material. llama3.2:3b often reports 0 even when it composed a good
    lesson, so 0 is treated as 'no signal' and the source-quality floor becomes
    the final confidence (5 notes chunks -> floor 90)."""
    chat = make_chat(lesson_json(0))
    summary = il.ingest_lessons_for_subject(
        db, SUBJECT, chat_fn=chat, embed_fn=fake_embed, verbose=False,
    )

    assert chat.calls == 1, "the model was called once for the objective"
    assert lesson_count(db) == 1, "confidence=0 must NOT discard a well-sourced lesson"
    assert queue_count(db) == 0, "nothing queued -- the lesson was written"
    assert summary["written"] == 1 and summary["queued"] == 0

    stored = db.execute(
        "SELECT confidence FROM objective_lessons WHERE objective_id = ?", (OBJECTIVE,),
    ).fetchone()["confidence"]
    # model_conf 0 -> no signal -> fall back to the 5-notes floor of 90.
    assert stored == 90, "the source-quality floor becomes the stored confidence"


def test_low_model_confidence_still_uses_source_floor(db):
    """Even a non-zero LOW model confidence (e.g. 5) must not block a lesson
    backed by strong source material. POB-1.11 case: the model returned conf=5
    on a clean 1829-char lesson with 3 valid recall questions. The model
    self-report is uncalibrated noise on this task, so it is ignored entirely
    and the source-quality floor (5 notes -> 90) is the stored confidence."""
    chat = make_chat(lesson_json(5))
    summary = il.ingest_lessons_for_subject(
        db, SUBJECT, chat_fn=chat, embed_fn=fake_embed, verbose=False,
    )

    assert chat.calls == 1, "the model was called once for the objective"
    assert lesson_count(db) == 1, "conf=5 must NOT discard a well-sourced lesson"
    assert queue_count(db) == 0, "nothing queued -- the lesson was written"
    assert summary["written"] == 1 and summary["queued"] == 0

    stored = db.execute(
        "SELECT confidence FROM objective_lessons WHERE objective_id = ?", (OBJECTIVE,),
    ).fetchone()["confidence"]
    # low model conf 5 ignored -> source floor 90 stored.
    assert stored == 90, "the source-quality floor becomes the stored confidence"


def test_insufficient_sources_queue_rather_than_write():
    """Zero source chunks -> queued (insufficient_sources), nothing written."""
    conn = open_test_db()
    seed(conn, notes_chunks=0)
    try:
        chat = make_chat(lesson_json(80))
        summary = il.ingest_lessons_for_subject(
            conn, SUBJECT, chat_fn=chat, embed_fn=fake_embed, verbose=False,
        )
        assert lesson_count(conn) == 0, "no lesson written without sources"
        assert queue_count(conn, reason="insufficient_sources") == 1
        assert summary["written"] == 0 and summary["queued"] == 1
        assert chat.calls == 0, "no source material -> the model is never called"
    finally:
        conn.close()


def test_regenerate_replaces_existing(db):
    """--regenerate deletes the old row and writes a fresh one with new confidence."""
    _insert_lesson(db, confidence=40)          # pre-existing lesson
    assert lesson_count(db) == 1

    chat = make_chat(lesson_json(92))
    il.ingest_lessons_for_subject(
        db, SUBJECT, regenerate=True, chat_fn=chat, embed_fn=fake_embed, verbose=False,
    )

    assert lesson_count(db) == 1, "still exactly one row -- the old one was replaced"
    stored = db.execute(
        "SELECT confidence FROM objective_lessons WHERE objective_id = ?", (OBJECTIVE,),
    ).fetchone()["confidence"]
    # 5 notes -> floor 90; the model self-report (92) is ignored. The point is it
    # is the freshly computed floor value, NOT the pre-existing 40.
    assert stored == 90
    assert stored != 40, "confidence was updated, not left at the old value"


def test_regenerate_default_does_not_replace(db):
    """Without --regenerate an existing lesson is skipped, not recomposed."""
    _insert_lesson(db, confidence=40)
    chat = make_chat(lesson_json(92))
    summary = il.ingest_lessons_for_subject(
        db, SUBJECT, regenerate=False, chat_fn=chat, embed_fn=fake_embed, verbose=False,
    )
    assert chat.calls == 0, "existing lesson -> no model call"
    assert summary["skipped"] == 1
    stored = db.execute(
        "SELECT confidence FROM objective_lessons WHERE objective_id = ?", (OBJECTIVE,),
    ).fetchone()["confidence"]
    assert stored == 40, "the existing lesson is left untouched"


def test_teach_route_serves_canonical_without_llm(db):
    """A stored canonical lesson is returned by the teach route with no LLM call."""
    _insert_lesson(db, confidence=88)
    chat = MagicMock()  # stands in for ollama_chat; must never be called

    out = controller.handle_request(
        db,
        {"route": "teach", "subject_id": SUBJECT, "objective_id": OBJECTIVE,
         "message": "Teach me this objective"},
        chat_fn=chat, embed_fn=fake_embed,
    )

    assert out["lesson_source"] == "canonical"
    assert isinstance(out["recall_questions"], list) and len(out["recall_questions"]) == 3
    assert out["objective_id"] == OBJECTIVE
    assert out["confidence"] == 88
    chat.assert_not_called()


def test_teach_route_fallback_serves_placeholder(db):
    """No stored lesson -> an honest placeholder is served with NO LLM call (runtime
    no longer generates freeform lessons), and the objective is queued."""
    assert lesson_count(db) == 0, "precondition: no canonical lesson"
    chat = MagicMock()  # must NEVER be called -- the fix removed runtime generation

    out = controller.handle_request(
        db,
        {"route": "teach", "subject_id": SUBJECT, "objective_id": OBJECTIVE,
         "message": "Teach me this objective"},
        chat_fn=chat, embed_fn=fake_embed,
    )

    assert out["lesson_source"] == "placeholder"
    assert out["recall_questions"] == []
    assert out["objective_id"] == OBJECTIVE
    assert "being prepared" in out["lesson"]
    chat.assert_not_called()
    assert queue_count(db, reason="served_placeholder") == 1


def test_successful_write_clears_the_queue(db):
    """A successful lesson write deletes the objective's stale queue rows."""
    db.execute(
        "INSERT INTO lesson_generation_queue (objective_id, reason) VALUES (?, ?)",
        (OBJECTIVE, "insufficient_sources"),
    )
    db.commit()
    assert queue_count(db) == 1, "precondition: the objective is flagged in the queue"

    chat = make_chat(lesson_json(85))  # 5 notes -> floor 90 (model conf ignored) -> written
    summary = il.ingest_lessons_for_subject(
        db, SUBJECT, chat_fn=chat, embed_fn=fake_embed, verbose=False,
    )

    assert lesson_count(db) == 1, "the lesson is written"
    assert queue_count(db) == 0, "the stale queue row is cleared on success"
    assert summary["written"] == 1 and summary["cleared"] == 1


def test_requeuing_is_idempotent():
    """Zero chunks twice -> the queue holds ONE row, created_at refreshed not stacked."""
    conn = open_test_db()
    seed(conn, notes_chunks=0)
    try:
        chat = make_chat(lesson_json(80))  # never called -- no chunks short-circuits
        il.ingest_lessons_for_subject(
            conn, SUBJECT, chat_fn=chat, embed_fn=fake_embed, verbose=False,
        )
        il.ingest_lessons_for_subject(
            conn, SUBJECT, chat_fn=chat, embed_fn=fake_embed, verbose=False,
        )
        assert chat.calls == 0, "no source material -> the model is never called"
        assert queue_count(conn, reason="insufficient_sources") == 1, \
            "second run upserts (refreshes created_at), it does not add a second row"
    finally:
        conn.close()


# --- _validate_lesson_quality (defence-in-depth quality gate) ---------------
def _clean_questions():
    return ["What is a business?",
            "State two functions of an entrepreneur.",
            "Explain how capital is used in production."]


def test_validate_rejects_section_citation():
    ok, why = il._validate_lesson_quality(
        "According to Section 2, a business supplies goods.", _clean_questions())
    assert ok is False and "section" in why.lower()


def test_validate_rejects_boilerplate_in_lesson_text():
    ok, why = il._validate_lesson_quality(
        "A business supplies goods. Let me know if you'd like more clarification!",
        _clean_questions())
    assert ok is False and "boilerplate" in why.lower()


def test_validate_rejects_wrong_question_count():
    ok, why = il._validate_lesson_quality(
        "A clean lesson body about business.", ["Only one question here?"])
    assert ok is False and "count != 3" in why


def test_validate_rejects_too_short_question():
    ok, why = il._validate_lesson_quality(
        "A clean lesson body about business.",
        ["What is a business?", "OK?", "Explain capital use in production."])
    assert ok is False and "too short" in why.lower()


def test_validate_rejects_answer_leakage():
    # The model appended the answer to the question (the POB-10.13 pattern).
    ok, why = il._validate_lesson_quality(
        "A clean lesson body about logistics.",
        ["What is the movement of goods called? (Answer: Transportation)",
         "State two modes of transport.", "Explain why transport matters."])
    assert ok is False and "leak" in why.lower()


def test_validate_rejects_non_question_non_command():
    # No '?' and not a command-word prompt -> rejected (this is the junk case, e.g.
    # 'multiple-choice' or a leaked-answer fragment).
    ok, why = il._validate_lesson_quality(
        "A clean lesson body about business.",
        ["What is a business?", "State two functions of management.",
         "The answer here is taxation and revenue."])
    assert ok is False and "not a question or command" in why.lower()


def test_validate_accepts_clean_response():
    ok, why = il._validate_lesson_quality(
        "A business supplies goods and services to satisfy needs and wants.",
        _clean_questions())
    assert ok is True and why is None


def test_validate_accepts_imperative_command_prompt_without_question_mark():
    # CSEC recall prompts are often imperatives that do not end in '?'. These are
    # valid and must NOT be rejected.
    ok, why = il._validate_lesson_quality(
        "A business supplies goods and services.",
        ["Identify two roles of an entrepreneur.",
         "Describe the role of capital in a business.",
         "Distinguish between fixed and working capital."])
    assert ok is True and why is None


def test_validate_accepts_name_command_prompt():
    # 'Name three...' is a real CSEC command stem.
    ok, why = il._validate_lesson_quality(
        "Factors of production are land, labour, capital and enterprise.",
        ["Name three factors of production.",
         "State two functions of management.",
         "Explain how capital differs from land."])
    assert ok is True and why is None


def test_validate_accepts_give_command_prompt():
    # 'Give two...' is a real CSEC command stem.
    ok, why = il._validate_lesson_quality(
        "Capital goods are man-made resources used to produce other goods.",
        ["Give two examples of capital goods.",
         "Define the term capital good.",
         "Distinguish between capital and consumer goods."])
    assert ok is True and why is None
