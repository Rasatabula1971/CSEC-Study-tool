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
import sqlite3
import struct
import sys
from datetime import date
from pathlib import Path

import pytest
from unittest.mock import patch, MagicMock
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "db"))

import controller  # noqa: E402
import init_db      # noqa: E402  (production open_db / init_schema)
import app as app_module        # noqa: E402  (apply_runtime_migrations)
from weakness import log_weakness  # noqa: E402

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
        # Evidence >= 20 chars so the Stage 10 thin-evidence gate keeps these awarded.
        "points": [
            {"mark_point_id": "mp1", "awarded": True,  "evidence": "the answer said an organisation"},
            {"mark_point_id": "mp2", "awarded": True,  "evidence": "the answer said goods and services"},
            {"mark_point_id": "mp3", "awarded": True,  "evidence": "the answer said to satisfy wants"},
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
        # mp1 evidence >= 20 chars so the thin-evidence gate keeps it awarded (1/3).
        "points": [
            {"mark_point_id": "mp1", "awarded": True,  "evidence": "the answer said an organisation"},
            {"mark_point_id": "mp2", "awarded": False, "evidence": "no goods were mentioned at all"},
            {"mark_point_id": "mp3", "awarded": False, "evidence": "no purpose was mentioned at all"},
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


# ===========================================================================
# Stage 7 integration class — the six-part full study loop, driven through
# controller.handle_request against a real (in-memory) DB that includes the
# runtime migrations. Ollama is mocked with unittest.mock.patch in every test
# that reaches the controller; the real Ollama is never contacted.
#
# Setup differs from the module-level fixture above in two deliberate ways the
# Stage 7 spec calls for: the DB has apply_runtime_migrations() applied, and the
# mark_points use the canonical '-stem' question_id convention.
# ===========================================================================
ZERO_VEC = [0.0] * EMBED_DIM   # fixed test embedding — never calls Ollama


def open_loop_db():
    """Real :memory: DB: production open_db + schema.sql + runtime migrations."""
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping pilot loop tests")
    db = init_db.open_db(":memory:")
    init_db.init_schema(db, SCHEMA_PATH)
    app_module.apply_runtime_migrations(db)   # the three runtime tables + question_id fix
    return db


def seed_loop(db) -> None:
    """POB slice for the full-loop class: 3 objectives, 5 '-stem' mark points,
    2 vec_notes + 2 vec_mark_schemes chunks (zero vectors), and a past-paper
    chunk (POB_2024_P2.pdf, page 4) linked to POB-1.1 for traceability.

    POB-1.1 deliberately has NO notes chunk, so its teach context resolves to the
    past-paper chunk -- exercising both the teach loop and traceability.
    """
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES ('POB-SEC-1', ?, 'Nature of Business', '1')",
        (SUBJECT,),
    )
    for oid, num, stmt in [
        ("POB-1.1", "1.1", "Explain the nature and functions of a business"),
        ("POB-1.2", "1.2", "Describe the types of economic activity"),
        ("POB-1.3", "1.3", "Outline the factors of production"),
    ]:
        db.execute(
            "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, content_stmt) "
            "VALUES (?, 'POB-SEC-1', ?, ?, ?)",
            (oid, SUBJECT, num, stmt),
        )

    db.executemany(
        "INSERT INTO documents (doc_id, subject_id, content_type, paper, year, source_file, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("doc_notes", SUBJECT, "notes",       None, None, "pob_notes.pdf",      "hash_notes"),
            ("doc_ms",    SUBJECT, "mark_scheme",  None, None, "pob_markscheme.pdf", "hash_ms"),
            ("doc_trace", SUBJECT, "past_paper",   "P2", 2024, "POB_2024_P2.pdf",    "hash_trace"),
        ],
    )

    # 2 notes chunks -> vec_notes (POB-1.2, POB-1.3 -- NOT POB-1.1)
    for oid, text, page, cid in [
        ("POB-1.2", "Economic activity is primary, secondary or tertiary.", 5, "c_notes_1"),
        ("POB-1.3", "The factors of production are land, labour, capital and enterprise.", 6, "c_notes_2"),
    ]:
        cur = db.execute(
            "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page, chunk_id) "
            "VALUES ('doc_notes', ?, ?, ?, ?, ?)",
            (oid, SUBJECT, text, page, cid),
        )
        db.execute("INSERT INTO vec_notes (rowid, embedding) VALUES (?, ?)",
                   (cur.lastrowid, _serialize(ZERO_VEC)))

    # 2 mark-scheme chunks -> vec_mark_schemes
    for oid, text, page, cid in [
        ("POB-1.2", "Mark scheme: 1 mark per correct type of economic activity.", 7, "c_ms_1"),
        ("POB-1.3", "Mark scheme: land, labour, capital, enterprise.", 9, "c_ms_2"),
    ]:
        cur = db.execute(
            "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page, chunk_id) "
            "VALUES ('doc_ms', ?, ?, ?, ?, ?)",
            (oid, SUBJECT, text, page, cid),
        )
        db.execute("INSERT INTO vec_mark_schemes (rowid, embedding) VALUES (?, ?)",
                   (cur.lastrowid, _serialize(ZERO_VEC)))

    # Past-paper chunk for POB-1.1 -> drives traceability (source_file + page).
    db.execute(
        "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page, question_num, chunk_id) "
        "VALUES ('doc_trace', 'POB-1.1', ?, ?, 4, '1a', 'c_trace_1')",
        (SUBJECT, "Define the term 'business' and state one function. (2 marks)"),
    )

    # 5 mark points across POB-1.1 (3) and POB-1.2 (2); question_id ends in '-stem'.
    mark_points = [
        ("POB-1.1-q1-stem-mp1", "POB-1.1", "POB-1.1-q1-stem", "an organisation/entity", 1),
        ("POB-1.1-q1-stem-mp2", "POB-1.1", "POB-1.1-q1-stem", "supplies goods and services", 2),
        ("POB-1.1-q1-stem-mp3", "POB-1.1", "POB-1.1-q1-stem", "to satisfy needs/wants", 3),
        ("POB-1.2-q2-stem-mp1", "POB-1.2", "POB-1.2-q2-stem", "primary economic activity", 1),
        ("POB-1.2-q2-stem-mp2", "POB-1.2", "POB-1.2-q2-stem", "secondary economic activity", 2),
    ]
    for mp_id, oid, qid, text, order in mark_points:
        db.execute(
            "INSERT INTO mark_points (mark_point_id, objective_id, question_id, doc_id, "
            "point_text, marks_value, point_order) VALUES (?, ?, ?, 'doc_ms', ?, 1, ?)",
            (mp_id, oid, qid, text, order),
        )

    db.commit()


@pytest.fixture
def loop_db():
    conn = open_loop_db()
    seed_loop(conn)
    yield conn
    conn.close()


class TestPOBStudyLoop:
    """The six Stage 7 acceptance tests for the full POB study loop."""

    def test_teach_loop(self, loop_db):
        """teach for POB-1.1 returns a non-empty lesson containing a question."""
        # MOCK ollama_chat (Tutor role): lesson body with one embedded question.
        def fake_chat(messages, system, schema=None):
            return ("A business is an organisation that supplies goods and services.\n"
                    "Example: a bakery sells bread to customers.\n"
                    "Q: What is the main purpose of a business?")

        with patch("controller.ollama_chat", fake_chat):
            out = controller.handle_request(
                loop_db,
                {"route": "teach", "subject_id": SUBJECT,
                 "objective_id": "POB-1.1", "query": "nature of a business"},
            )
        assert out["route"] == "teach"
        assert out["objective_id"] == "POB-1.1"
        # The controller returns the lesson (with the question embedded) under the
        # single "lesson" key -- there is no separate lesson_text/question field.
        lesson = out["lesson"]
        assert isinstance(lesson, str) and lesson.strip()   # lesson_text: non-empty
        assert "?" in lesson                                 # a question is present

    def test_grading_loop(self, loop_db):
        """2 of 3 mark points awarded -> Python score 67; fail (<70) -> Leitner box 1."""
        # MOCK ollama_chat (Examiner role): valid GRADING_SCHEMA JSON, 2/3 awarded.
        grading_json = json.dumps({
            "objective_id": "POB-1.1",
            "question_id": "POB-1.1-q1-stem",
            # Evidence >= 20 chars so the thin-evidence gate keeps the two awarded (2/3).
            "points": [
                {"mark_point_id": "POB-1.1-q1-stem-mp1", "awarded": True,  "evidence": "the answer named an organisation"},
                {"mark_point_id": "POB-1.1-q1-stem-mp2", "awarded": True,  "evidence": "the answer gave goods and services"},
                {"mark_point_id": "POB-1.1-q1-stem-mp3", "awarded": False, "evidence": "the purpose was not stated"},
            ],
        })

        def fake_chat(messages, system, schema=None):
            return grading_json

        with patch("controller.ollama_chat", fake_chat):
            out = controller.handle_request(
                loop_db,
                {"route": "grade", "subject_id": SUBJECT, "question_id": "POB-1.1-q1-stem",
                 "student_answer": "A business is an organisation that supplies goods."},
            )
        assert out["score_pct"] == 67          # round(100 * 2/3), computed in Python
        row = loop_db.execute(
            "SELECT leitner_box FROM weakness_log WHERE objective_id = 'POB-1.1'"
        ).fetchone()
        assert row is not None
        assert row["leitner_box"] == 1         # score < 70 -> reset to box 1

    def test_scope_gate(self, loop_db):
        """An unknown subject is refused with no LLM call."""
        # MOCK ollama_chat: must NOT be called for an out-of-scope request.
        mock_chat = MagicMock(name="ollama_chat")
        with patch("controller.ollama_chat", mock_chat):
            out = controller.handle_request(
                loop_db,
                {"route": "teach", "subject_id": "Fake_Subject",
                 "objective_id": "FAKE-1.1", "query": "anything"},
            )
        assert out == {"error": "out_of_scope"}
        mock_chat.assert_not_called()

    def test_revision_plan(self, loop_db):
        """3 objectives due today (boxes 1, 2, 1) come back ordered by box ascending."""
        today = date.today().isoformat()
        loop_db.executemany(
            "INSERT INTO weakness_log (objective_id, subject_id, score_pct, leitner_box, next_review) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("POB-1.1", SUBJECT, 30, 1, today),
                ("POB-1.2", SUBJECT, 50, 2, today),
                ("POB-1.3", SUBJECT, 40, 1, today),
            ],
        )
        loop_db.commit()

        # MOCK ollama_chat: the plan route is fully deterministic -> no LLM call.
        mock_chat = MagicMock(name="ollama_chat")
        with patch("controller.ollama_chat", mock_chat):
            out = controller.handle_request(loop_db, {"route": "plan", "subject_id": SUBJECT})

        tasks = out["tasks"]
        ids = [t["objective_id"] for t in tasks]
        assert {"POB-1.1", "POB-1.2", "POB-1.3"}.issubset(set(ids))
        boxes = [t["leitner_box"] for t in tasks]
        assert boxes == sorted(boxes)                       # ascending by leitner_box
        # the box-2 objective comes after both box-1 objectives
        assert ids.index("POB-1.2") > ids.index("POB-1.1")
        assert ids.index("POB-1.2") > ids.index("POB-1.3")
        mock_chat.assert_not_called()

    def test_traceability(self, loop_db):
        """teach for POB-1.1 surfaces objective_id + source_file + page (VAL-08)."""
        # MOCK ollama_chat (Tutor role): traceability is about the source metadata.
        def fake_chat(messages, system, schema=None):
            return "Lesson grounded in the 2024 P2 extract.\nQ: Define a business."

        with patch("controller.ollama_chat", fake_chat):
            out = controller.handle_request(
                loop_db,
                {"route": "teach", "subject_id": SUBJECT,
                 "objective_id": "POB-1.1", "query": "define business"},
            )
        assert out["objective_id"] == "POB-1.1"
        assert out["source_file"] == "POB_2024_P2.pdf"
        assert out["page"] == 4

    def test_weakness_validation(self, loop_db):
        """log_weakness raises ValueError on a missing required field (never silent)."""
        # No controller / no Ollama: a malformed grading_result must be rejected.
        with pytest.raises(ValueError):
            log_weakness(loop_db, {"subject_id": SUBJECT, "score_pct": 50}, session_id=0)  # no objective_id


# ===========================================================================
# Stage 10 — confidence-aware grading: verify-with-teacher payload fields
# ===========================================================================
def open_stage10_db():
    """Real :memory: DB with check_same_thread=False so the TestClient worker thread
    can use it (Starlette runs the sync endpoint off-thread). Schema + migrations."""
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping Stage 10 API tests")
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
    return db


def _seed_stage10_subject(db) -> None:
    """Locked POB subject + one objective (POB-1.1) for the Stage 10 API tests."""
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
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, content_stmt) "
        "VALUES ('POB-1.1', 'POB-SEC-1', ?, '1.1', 'Define the term business.')",
        (SUBJECT,),
    )
    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, source_file, content_hash) "
        "VALUES ('doc_s10', ?, 'mark_scheme', 's10.pdf', 'hash_s10')",
        (SUBJECT,),
    )


def _grade_via_api(db, question_id):
    """POST /api/chat route='grade' against `db`, mocking the examiner LLM.

    The examiner returns one awarded point with thick evidence + confidence, so the
    score and provenance fields are exercised without touching real Ollama/Gemini.
    """
    graded_json = json.dumps({
        "objective_id": "POB-1.1",
        "question_id": question_id,
        "confidence": 75,
        "points": [
            {"mark_point_id": f"{question_id}-mp1", "awarded": True,
             "evidence": "the answer clearly defined a business as an organisation",
             "confidence": 75},
        ],
    })
    app_module.app.state.db = db
    with patch("controller.ollama_chat", lambda *a, **k: graded_json):
        client = TestClient(app_module.app)
        res = client.post("/api/chat", json={
            "message": "A business is an organisation that supplies goods and services.",
            "subject_id": SUBJECT,
            "route": "grade",
            "question_id": question_id,
        })
    return res


def test_E_syllabus_derived_triggers_verify_with_teacher():
    """syllabus_derived mark points + an open review-queue row -> badge fields set."""
    db = open_stage10_db()
    try:
        _seed_stage10_subject(db)
        qid = "POB-1.1-qE-stem"   # ends in '-stem' so the startup migration leaves it
        db.execute(
            "INSERT INTO mark_points (mark_point_id, objective_id, question_id, doc_id, "
            "point_text, marks_value, point_order, source_type) "
            "VALUES (?, 'POB-1.1', ?, 'doc_s10', 'define a business', 1, 1, 'syllabus_derived')",
            (f"{qid}-mp1", qid),
        )
        # An OPEN review-queue row for this objective -> pending_review must be True.
        db.execute(
            "INSERT INTO ingest_review_queue (source_file, chunk_text, reason, objective_id) "
            "VALUES ('s10.pdf', 'define a business', 'syllabus_derived_first_run', 'POB-1.1')",
        )
        db.commit()

        body = _grade_via_api(db, qid).json()
        assert body["grading_basis"] == "syllabus_derived"
        assert body["pending_review"] is True
        assert isinstance(body["overall_confidence"], int)
    finally:
        db.close()


def test_F_past_paper_basis_does_not_trigger_badge():
    """past_paper mark points + no review-queue row -> grading_basis past_paper, no pending."""
    db = open_stage10_db()
    try:
        _seed_stage10_subject(db)
        qid = "POB-1.1-qF-stem"
        db.execute(
            "INSERT INTO mark_points (mark_point_id, objective_id, question_id, doc_id, "
            "point_text, marks_value, point_order, source_type) "
            "VALUES (?, 'POB-1.1', ?, 'doc_s10', 'define a business', 1, 1, 'past_paper')",
            (f"{qid}-mp1", qid),
        )
        db.commit()

        body = _grade_via_api(db, qid).json()
        assert body["grading_basis"] == "past_paper"
        assert body["pending_review"] is False
    finally:
        db.close()
