"""
tests/test_recover_mark_points.py
=================================
Stage 8 (Build Playbook v3.1) tests for backend/recover_mark_points.py.

Three behaviours, all offline (ollama_chat is mocked, the embedder is faked, the
DB is an in-memory SQLite with the full schema + sqlite-vec):

  1. High-confidence extraction  -> a mark_points row is written with
     source_type='recovered_extraction'.
  2. Low-confidence extraction   -> an ingest_review_queue row is written and
     NOTHING goes into mark_points.
  3. Re-running on the same chunk -> no duplicate mark_points row.

Run: pytest tests/test_recover_mark_points.py -v
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import backend.recover_mark_points as rmp  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"
EMBED_DIM = 768
SUBJECT = "Principles_of_Business"
OBJECTIVE = "POB-3.2"
CHUNK_ID = "mark_scheme-test01-p1-c0"
DOC_ID = "ms-doc-1"


# --- fakes -----------------------------------------------------------------
def fake_embed(text: str) -> list[float]:
    """Deterministic dummy embedding -- no Ollama required."""
    return [0.0] * EMBED_DIM


def make_chat(points: list[dict]):
    """Build a mock ollama_chat that always returns this points payload as JSON.

    The returned callable also records how many times it was invoked so a test can
    assert the model was (or wasn't) called.
    """
    def _chat(messages, system, schema=None):
        _chat.calls += 1
        return json.dumps({"points": points})
    _chat.calls = 0
    return _chat


HIGH_CONF_POINTS = [
    {
        "point_text": "Identifies a business opportunity / gap in the market",
        "marks_value": 1,
        "confidence": 95,
        "evidence_quote": "identifies a business opportunity",
    },
    {
        "point_text": "Organises the factors of production",
        "marks_value": 2,
        "confidence": 88,
        "evidence_quote": "organises land, labour and capital",
    },
]

LOW_CONF_POINTS = [
    {
        "point_text": "Cover page text, not a real mark point",
        "marks_value": 1,
        "confidence": 35,
        "evidence_quote": "MAY/JUNE 2015 FORM TP",
    },
]


# --- in-memory DB ----------------------------------------------------------
def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping recovery tests")
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
    """A locked subject + one objective with ZERO mark points + one mark-scheme
    chunk tagged to that objective and indexed in vec_mark_schemes."""
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES (?, ?, ?, ?)",
        ("POB-SEC-3", SUBJECT, "Forms of Business Organisation", "3"),
    )
    db.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
        "content_stmt, skill_type, command_words) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (OBJECTIVE, "POB-SEC-3", SUBJECT, "3.2",
         "Explain the functions of an entrepreneur", "Understanding",
         '["Explain"]'),
    )
    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, source_file, content_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        (DOC_ID, SUBJECT, "mark_scheme",
         r"E:\KB\Principles_of_Business\03_MARK_SCHEMES\ms2015.pdf", "hash-ms-1"),
    )
    cur = db.execute(
        "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page, "
        "question_num, chunk_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (DOC_ID, OBJECTIVE, SUBJECT,
         "The entrepreneur identifies a business opportunity, organises land, "
         "labour and capital, and bears the risk.", 1, None, CHUNK_ID),
    )
    db.execute(
        "INSERT INTO vec_mark_schemes(rowid, embedding) VALUES (?, ?)",
        (cur.lastrowid, rmp.serialize_vec(fake_embed("x"))),
    )
    db.commit()


@pytest.fixture
def db():
    conn = open_test_db()
    seed(conn)
    yield conn
    conn.close()


def mp_count(db, objective_id=OBJECTIVE) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM mark_points WHERE objective_id = ?", (objective_id,)
    ).fetchone()[0]


def queue_count(db, objective_id=OBJECTIVE) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM ingest_review_queue WHERE objective_id = ?", (objective_id,)
    ).fetchone()[0]


# --- tests -----------------------------------------------------------------
def test_high_confidence_writes_mark_points(db):
    """confidence >= min_confidence -> rows land in mark_points, not the queue."""
    chat = make_chat(HIGH_CONF_POINTS)
    summary = rmp.recover_mark_points(
        db, SUBJECT, min_confidence=70, dry_run=False,
        chat_fn=chat, embed_fn=fake_embed, verbose=False,
    )

    assert chat.calls >= 1, "the (mocked) model must be called for the candidate chunk"
    assert mp_count(db) == 2, "both high-confidence points should be written"
    assert queue_count(db) == 0, "nothing high-confidence should be queued"
    assert summary["points_recovered"] == 2
    assert summary["points_queued"] == 0
    assert summary["objectives_still_empty"] == 0

    rows = db.execute(
        "SELECT source_type, source_chunk_id, extraction_confidence, marks_value, "
        "doc_id, question_id FROM mark_points WHERE objective_id = ? ORDER BY point_order",
        (OBJECTIVE,),
    ).fetchall()
    for r in rows:
        assert r["source_type"] == "recovered_extraction"
        assert r["source_chunk_id"] == CHUNK_ID
        assert r["doc_id"] == DOC_ID
        assert r["question_id"] is None
    # weights are preserved from the extraction, not flattened to 1
    assert sorted(r["marks_value"] for r in rows) == [1, 2]
    assert sorted(r["extraction_confidence"] for r in rows) == [88, 95]


def test_low_confidence_goes_to_review_queue(db):
    """confidence < min_confidence -> ingest_review_queue, and NOT mark_points."""
    chat = make_chat(LOW_CONF_POINTS)
    summary = rmp.recover_mark_points(
        db, SUBJECT, min_confidence=70, dry_run=False,
        chat_fn=chat, embed_fn=fake_embed, verbose=False,
    )

    assert mp_count(db) == 0, "low-confidence points must never enter mark_points"
    assert queue_count(db) == 1, "the low-confidence point should be queued for review"
    assert summary["points_recovered"] == 0
    assert summary["points_queued"] == 1
    # the objective is still empty (correctly): it has no mark points yet
    assert summary["objectives_still_empty"] == 1

    row = db.execute(
        "SELECT reason, objective_id, doc_id, source_file, chunk_text "
        "FROM ingest_review_queue WHERE objective_id = ?", (OBJECTIVE,),
    ).fetchone()
    assert row["reason"] == "low_confidence_extraction"
    assert row["objective_id"] == OBJECTIVE
    assert row["doc_id"] == DOC_ID
    # chunk_text carries the candidate point text plus its evidence quote
    assert "Cover page text" in row["chunk_text"]
    assert "Evidence:" in row["chunk_text"]


def test_rerun_does_not_duplicate(db):
    """Idempotency: a second pass over the same chunk adds no new mark_points.

    After the first pass the objective HAS mark points, so it drops out of the
    zero-mark-point set entirely on the second pass -- the strongest possible
    idempotency. The playbook's requirement is that the count is unchanged.
    """
    chat = make_chat(HIGH_CONF_POINTS)
    rmp.recover_mark_points(
        db, SUBJECT, min_confidence=70, dry_run=False,
        chat_fn=chat, embed_fn=fake_embed, verbose=False,
    )
    after_first = mp_count(db)
    assert after_first == 2

    chat2 = make_chat(HIGH_CONF_POINTS)
    summary2 = rmp.recover_mark_points(
        db, SUBJECT, min_confidence=70, dry_run=False,
        chat_fn=chat2, embed_fn=fake_embed, verbose=False,
    )

    assert mp_count(db) == after_first, "re-run must not duplicate existing mark points"
    assert summary2["points_recovered"] == 0
    assert summary2["objectives_total"] == 0, "the now-covered objective is not reprocessed"


def test_dedup_guard_skips_existing_point(db):
    """The (source_chunk_id, point_text) guard skips a point already in mark_points.

    Realistic trigger: a multi-objective mark-scheme chunk surfaces an award point
    that another objective already recovered from the SAME chunk. Here POB-OTHER
    already holds the first point from CHUNK_ID, so when POB-3.2 is processed the
    identical extraction is skipped while the genuinely new second point is written.
    """
    rmp.ensure_recovery_columns(db)  # the provenance columns must exist for the pre-insert
    db.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
        "content_stmt, skill_type) VALUES (?, ?, ?, ?, ?, ?)",
        ("POB-OTHER", "POB-SEC-3", SUBJECT, "3.9",
         "A different objective sharing the chunk", "Understanding"),
    )
    db.execute(
        "INSERT INTO mark_points (mark_point_id, objective_id, question_id, doc_id, "
        "point_text, marks_value, point_order, source_type, source_chunk_id, "
        "extraction_confidence) VALUES (?, ?, NULL, ?, ?, 1, 1, ?, ?, 90)",
        ("POB-OTHER-pre", "POB-OTHER", DOC_ID, HIGH_CONF_POINTS[0]["point_text"],
         "recovered_extraction", CHUNK_ID),
    )
    db.commit()

    chat = make_chat(HIGH_CONF_POINTS)
    summary = rmp.recover_mark_points(
        db, SUBJECT, min_confidence=70, dry_run=False,
        chat_fn=chat, embed_fn=fake_embed, verbose=False,
    )

    assert summary["points_skipped_duplicate"] == 1, "the shared point is skipped"
    assert summary["points_recovered"] == 1, "only the genuinely new point is written"
    assert mp_count(db, "POB-3.2") == 1


def test_dry_run_writes_nothing(db):
    """--dry-run reports what it would do but commits no rows."""
    chat = make_chat(HIGH_CONF_POINTS)
    summary = rmp.recover_mark_points(
        db, SUBJECT, min_confidence=70, dry_run=True,
        chat_fn=chat, embed_fn=fake_embed, verbose=False,
    )
    assert mp_count(db) == 0
    assert queue_count(db) == 0
    assert summary["points_recovered"] == 2  # counted as "would recover"
    assert summary["objectives_still_empty"] == 1
