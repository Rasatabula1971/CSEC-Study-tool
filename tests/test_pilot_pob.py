"""
tests/test_pilot_pob.py
=======================
Stage 7 pilot integration tests for Principles_of_Business (POB).

These exercise the *full study loop* end-to-end through controller.handle_request,
against a REAL (in-memory) SQLite database — the DB is never mocked. Only Ollama
is mocked: `ollama_chat` and `ollama_embed` are replaced by clearly-labelled
stubs in every test, and Ollama is NEVER contacted for real anywhere in this file.

The database is opened with the production `init_db.open_db()` pointed at the
`:memory:` path (sqlite-vec loaded, FKs on), seeded with a small slice of the
POB syllabus, real chunk + vec rows, and a mark scheme, then driven through the
six Stage 7 test groups:

  1. Full teach loop
  2. Grading loop (Python scoring + weakness_log Leitner update)
  3. Scope gate (unknown subject)
  4. Revision plan ordering
  5. Traceability VAL-08 (objective_id + source_file + page)
  6. Lock gate (known subject, syllabus_locked = 0)

Run: pytest tests/test_pilot_pob.py -v
"""

import json
import struct
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "db"))

import controller  # noqa: E402
import init_db      # noqa: E402  (production open_db / init_schema)

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"
SUBJECT = "Principles_of_Business"
EMBED_DIM = 768


# ---------------------------------------------------------------------------
# Test DB: real SQLite (:memory:) via the production open_db, sqlite-vec loaded
# ---------------------------------------------------------------------------
def open_pilot_db():
    """Real in-memory DB through production open_db() + schema. Not a mock."""
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping pilot tests")
    db = init_db.open_db(":memory:")          # production opener, :memory: path
    init_db.init_schema(db, SCHEMA_PATH)       # production schema loader
    return db


def _vec(seed: float) -> list[float]:
    """A deterministic 768-d embedding; distinct per `seed` so MATCH is stable."""
    return [seed] * EMBED_DIM


def _serialize(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def seed_pob(db) -> None:
    """Seed a real slice of the POB pilot: subject, 3 objectives, 5 mark points,
    2 notes chunks (+ vec_notes), 2 mark-scheme chunks (+ vec_mark_schemes), and
    one past-paper chunk used by the structured-lookup traceability test."""

    # --- subject (locked) + section ---------------------------------------
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES (?, ?, ?, ?)",
        ("POB-SEC-1", SUBJECT, "Nature of Business", "1"),
    )

    # --- 3 objectives from the Nature of Business section ------------------
    objectives = [
        ("POB-1.1", "1.1", "Explain the nature and functions of a business"),
        ("POB-1.2", "1.2", "Describe the types of economic activity"),
        ("POB-1.3", "1.3", "Outline the factors of production"),
    ]
    for oid, num, stmt in objectives:
        db.execute(
            "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
            "content_stmt) VALUES (?, ?, ?, ?, ?)",
            (oid, "POB-SEC-1", SUBJECT, num, stmt),
        )

    # --- documents ---------------------------------------------------------
    db.executemany(
        "INSERT INTO documents (doc_id, subject_id, content_type, paper, year, "
        "source_file, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("doc_notes", SUBJECT, "notes",      None, None, "pob_notes.pdf",      "hash_notes"),
            ("doc_ms",    SUBJECT, "mark_scheme", None, None, "pob_markscheme.pdf", "hash_ms"),
            ("doc_pp",    SUBJECT, "past_paper", "P2", 2019, "pob_p2_2019.pdf",     "hash_pp"),
        ],
    )

    # --- chunks (+ vec rows; vec rowid MUST equal chunks.id) ---------------
    # 2 notes chunks -> vec_notes
    notes_chunks = [
        ("POB-1.1", "A business is an organisation that supplies goods and services.", 3, "c_notes_1", _vec(0.11)),
        ("POB-1.2", "Economic activity is classified as primary, secondary or tertiary.", 5, "c_notes_2", _vec(0.22)),
    ]
    for oid, text, page, cid, emb in notes_chunks:
        cur = db.execute(
            "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page, chunk_id) "
            "VALUES ('doc_notes', ?, ?, ?, ?, ?)",
            (oid, SUBJECT, text, page, cid),
        )
        db.execute("INSERT INTO vec_notes (rowid, embedding) VALUES (?, ?)",
                   (cur.lastrowid, _serialize(emb)))

    # 2 mark-scheme chunks -> vec_mark_schemes
    ms_chunks = [
        ("POB-1.2", "Mark scheme: award 1 mark per correct type of economic activity.", 7, "c_ms_1", _vec(0.33)),
        ("POB-1.3", "Mark scheme: land, labour, capital, enterprise.", 9, "c_ms_2", _vec(0.44)),
    ]
    for oid, text, page, cid, emb in ms_chunks:
        cur = db.execute(
            "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page, chunk_id) "
            "VALUES ('doc_ms', ?, ?, ?, ?, ?)",
            (oid, SUBJECT, text, page, cid),
        )
        db.execute("INSERT INTO vec_mark_schemes (rowid, embedding) VALUES (?, ?)",
                   (cur.lastrowid, _serialize(emb)))

    # past-paper chunk keyed by question_num -> exercised by structured lookup (VAL-08)
    db.execute(
        "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page, question_num, chunk_id) "
        "VALUES ('doc_pp', ?, ?, ?, ?, ?, ?)",
        ("POB-1.1", SUBJECT, "Define the term 'business'. (2 marks)", 12, "2b", "c_pp_1"),
    )

    # --- 5 mark points across the 3 objectives ----------------------------
    # q1 (POB-1.1): mp1, mp2, mp3   |   q2 (POB-1.2): mp4, mp5
    mark_points = [
        ("mp1", "POB-1.1", "q1", "an organisation/entity", 1, 1),
        ("mp2", "POB-1.1", "q1", "supplies goods and services", 1, 2),
        ("mp3", "POB-1.1", "q1", "to satisfy needs/wants (for profit)", 1, 3),
        ("mp4", "POB-1.2", "q2", "primary economic activity", 1, 1),
        ("mp5", "POB-1.2", "q2", "secondary economic activity", 1, 2),
    ]
    for mp_id, oid, qid, text, marks, order in mark_points:
        db.execute(
            "INSERT INTO mark_points (mark_point_id, objective_id, question_id, doc_id, "
            "point_text, marks_value, point_order) VALUES (?, ?, ?, 'doc_ms', ?, ?, ?)",
            (mp_id, oid, qid, text, marks, order),
        )

    db.commit()


@pytest.fixture
def db():
    conn = open_pilot_db()
    seed_pob(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Mock helpers — every Ollama touch point is replaced by a labelled stub.
# ---------------------------------------------------------------------------
def boom_chat(*args, **kwargs):
    """MOCK ollama_chat: must NEVER be called in this path."""
    raise AssertionError("ollama_chat was called when no LLM call was expected")


def boom_embed(*args, **kwargs):
    """MOCK ollama_embed: must NEVER be called in this path."""
    raise AssertionError("ollama_embed was called when no embedding was expected")


# ===========================================================================
# Group 1 — Full teach loop
# ===========================================================================
def test_group1_full_teach_loop(db):
    """teach route for a known objective returns lesson text + exactly one question."""

    # MOCK ollama_embed: return the exact stored vector for the POB-1.1 notes
    # chunk so the real sqlite-vec MATCH deterministically resolves to it.
    def fake_embed(text):
        return _vec(0.11)

    # MOCK ollama_chat (Tutor role): a lesson body with exactly ONE question.
    lesson_text = (
        "A business is an organisation that supplies goods and services to satisfy needs.\n"
        "Example: a bakery sells bread to customers.\n"
        "Q: What is the primary function of a business?"
    )

    def fake_chat(messages, system):
        return lesson_text

    out = controller.handle_request(
        db,
        {"route": "teach", "subject_id": SUBJECT,
         "objective_id": "POB-1.1", "query": "nature of a business"},
        chat_fn=fake_chat, embed_fn=fake_embed,
    )

    assert out["route"] == "teach"
    assert out["objective_id"] == "POB-1.1"           # known, in-scope objective
    assert out["lesson"].strip()                       # lesson text present
    assert out["lesson"].count("?") == 1               # exactly one question
    assert out["lesson"].count("Q:") == 1


# ===========================================================================
# Group 2 — Grading loop (Python scores; weakness_log Leitner update)
# ===========================================================================
def test_group2_grading_loop_python_scoring_and_leitner(db):
    """Score is computed in Python (LLM's number ignored) and weakness_log is
    advanced by the deterministic Leitner box."""

    # Pre-seed a prior weakness for POB-1.1 at box 2 so a PASS should move to box 3.
    db.execute(
        "INSERT INTO weakness_log (objective_id, subject_id, score_pct, leitner_box, next_review) "
        "VALUES ('POB-1.1', ?, 40, 2, ?)",
        (SUBJECT, date.today().isoformat()),
    )
    db.commit()

    # MOCK ollama_chat (Examiner role): all 3 points awarded. The JSON carries a
    # deliberately WRONG "score_pct": 999 to prove Python recomputes it, not the LLM.
    grading_json = json.dumps({
        "objective_id": "POB-1.1",
        "question_id": "q1",
        "score_pct": 999,          # bogus — must be overwritten by compute_score()
        "points": [
            {"mark_point_id": "mp1", "awarded": True,  "evidence": "said organisation"},
            {"mark_point_id": "mp2", "awarded": True,  "evidence": "said goods/services"},
            {"mark_point_id": "mp3", "awarded": True,  "evidence": "said to satisfy wants"},
        ],
    })

    def fake_chat(messages, system, schema=None):
        return grading_json

    out = controller.handle_request(
        db,
        {"route": "grade", "subject_id": SUBJECT, "question_id": "q1",
         "student_answer": "A business is an organisation that supplies goods to satisfy wants."},
        chat_fn=fake_chat, embed_fn=boom_embed,   # grading needs no embedding
    )

    # Python scoring: 3/3 awarded = 100% (NOT the LLM's 999)
    assert out["awarded"] == 3
    assert out["total"] == 3
    assert out["score_pct"] == 100
    assert out["missed_points"] == []

    # weakness_log advanced by Leitner: box 2 + pass -> box 3
    assert out["weakness"]["leitner_box"] == 3
    row = db.execute(
        "SELECT score_pct, leitner_box FROM weakness_log WHERE objective_id = 'POB-1.1'"
    ).fetchone()
    assert row["score_pct"] == 100
    assert row["leitner_box"] == 3
    # upsert, not a new row
    assert db.execute("SELECT COUNT(*) FROM weakness_log").fetchone()[0] == 1


def test_group2_grading_loop_fail_resets_box_to_one(db):
    """A failing grade on a NEW objective records box 1 (deterministic, not LLM)."""

    # MOCK ollama_chat (Examiner): 1 of 3 awarded -> 33% -> fail.
    grading_json = json.dumps({
        "objective_id": "POB-1.1",
        "question_id": "q1",
        "points": [
            {"mark_point_id": "mp1", "awarded": True,  "evidence": "organisation"},
            {"mark_point_id": "mp2", "awarded": False, "evidence": "no goods mentioned"},
            {"mark_point_id": "mp3", "awarded": False, "evidence": "no purpose mentioned"},
        ],
    })

    def fake_chat(messages, system, schema=None):
        return grading_json

    out = controller.handle_request(
        db,
        {"route": "grade", "subject_id": SUBJECT, "question_id": "q1",
         "student_answer": "It is an organisation."},
        chat_fn=fake_chat, embed_fn=boom_embed,
    )

    assert out["score_pct"] == 33          # round(100 * 1/3)
    assert out["weakness"]["leitner_box"] == 1   # new objective, fail -> box 1


# ===========================================================================
# Group 3 — Scope gate (subject not present in the DB)
# ===========================================================================
def test_group3_scope_gate_unknown_subject_no_llm(db):
    """An unknown subject is refused with no LLM and no embedding call."""
    out = controller.handle_request(
        db,
        {"route": "teach", "subject_id": "Underwater_Basket_Weaving", "query": "anything"},
        chat_fn=boom_chat, embed_fn=boom_embed,   # both raise if reached
    )
    assert out == {"error": "out_of_scope"}


# ===========================================================================
# Group 4 — Revision plan ordering (deterministic, no LLM)
# ===========================================================================
def test_group4_revision_plan_orders_by_leitner_box(db):
    """3 objectives due today (boxes 1, 1, 2) come back ordered by box ASC."""
    today = date.today().isoformat()
    db.executemany(
        "INSERT INTO weakness_log (objective_id, subject_id, score_pct, leitner_box, next_review) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("POB-1.3", SUBJECT, 60, 2, today),   # box 2
            ("POB-1.1", SUBJECT, 30, 1, today),   # box 1
            ("POB-1.2", SUBJECT, 45, 1, today),   # box 1
        ],
    )
    db.commit()

    out = controller.handle_request(
        db,
        {"route": "plan", "subject_id": SUBJECT},
        chat_fn=boom_chat, embed_fn=boom_embed,   # plan is fully deterministic
    )

    assert out["route"] == "plan"
    tasks = out["tasks"]
    assert out["due_count"] >= 3
    assert len(tasks) >= 3
    # ordered by leitner_box ASC
    boxes = [t["leitner_box"] for t in tasks]
    assert boxes == sorted(boxes)
    assert boxes[:3] == [1, 1, 2]
    # all three seeded objectives are present
    assert {"POB-1.1", "POB-1.2", "POB-1.3"}.issubset({t["objective_id"] for t in tasks})


# ===========================================================================
# Group 5 — Traceability VAL-08 (objective_id + source_file + page)
# ===========================================================================
def test_group5_traceability_structured_lookup(db):
    """A structured (paper/year/question) request returns objective_id, source_file,
    and page from a real FK join — and makes NO embedding call."""

    # MOCK ollama_chat (Tutor): structured path still produces a lesson.
    def fake_chat(messages, system):
        return "Lesson grounded in the past-paper extract.\nQ: Define a business."

    out = controller.handle_request(
        db,
        {"route": "teach", "subject_id": SUBJECT,
         "paper": "P2", "year": 2019, "question_num": "2b"},
        chat_fn=fake_chat, embed_fn=boom_embed,   # structured lookup -> no embedding
    )

    assert out["objective_id"] == "POB-1.1"
    assert out["source_file"] == "pob_p2_2019.pdf"
    assert out["page"] == 12


# ===========================================================================
# Group 6 — Lock gate (subject exists but syllabus_locked = 0)
# ===========================================================================
def test_group6_lock_gate_unlocked_subject_no_llm(db):
    """A known-but-unlocked subject is refused with no LLM/embedding call."""
    # Economics exists in the DB but is NOT locked.
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 0)",
        ("Economics", "Economics"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES ('ECO-SEC-1', 'Economics', 'Intro', '1')",
    )
    db.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, content_stmt) "
        "VALUES ('ECO-1.1', 'ECO-SEC-1', 'Economics', '1.1', 'Define scarcity')",
    )
    db.commit()

    out = controller.handle_request(
        db,
        {"route": "teach", "subject_id": "Economics",
         "objective_id": "ECO-1.1", "query": "scarcity"},
        chat_fn=boom_chat, embed_fn=boom_embed,   # gate must trip before either
    )
    assert out == {"error": "out_of_scope"}
