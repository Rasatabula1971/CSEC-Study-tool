"""
tests/test_ingest.py
====================
Stage 4 tests for backend/ingest.py.

Exercises the ingestion logic on plain text strings (not real PDFs) against an
in-memory SQLite DB with the full schema and sqlite-vec loaded. A fake embedder
is injected, so these tests need neither Ollama nor PyMuPDF.

Run: pytest tests/test_ingest.py -v
"""

import sqlite3
import sys
from pathlib import Path

import pytest

# Make backend/ importable (matches the layout the module expects at runtime).
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import backend.ingest as ingest  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"
EMBED_DIM = 768


def fake_embed(text: str) -> list[float]:
    """Deterministic dummy embedding -- no Ollama required."""
    return [0.0] * EMBED_DIM


def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping ingest tests")

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


def seed_locked_subject(db: sqlite3.Connection) -> None:
    """A locked subject + one section + one objective + a document to attach chunks to."""
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        ("Principles_of_Business", "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES (?, ?, ?, ?)",
        ("POB-SEC-1", "Principles_of_Business", "Nature of Business", "1"),
    )
    db.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
        "content_stmt, skill_type) VALUES (?, ?, ?, ?, ?, ?)",
        ("POB-1.1", "POB-SEC-1", "Principles_of_Business", "1.1",
         "Explain the concept of a business and the functions of an entrepreneur",
         "Understanding"),
    )
    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, source_file, content_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        ("notes-doc1", "Principles_of_Business", "notes",
         r"D:\KB\Principles_of_Business\04_NOTES\notes1.pdf", "hash-notes-1"),
    )
    db.commit()


@pytest.fixture
def db():
    conn = open_test_db()
    seed_locked_subject(conn)
    yield conn
    conn.close()


def objectives(db):
    return ingest.load_objectives(db, "Principles_of_Business")


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------
def test_chunk_page_overlaps():
    text = "x" * 1200
    chunks = ingest.chunk_page(text, size=500, overlap=100)
    # step = 400 -> windows at 0, 400, 800 ... last window reaches the end
    assert chunks[0] == text[0:500]
    assert chunks[1] == text[400:900]
    assert all(len(c) <= 500 for c in chunks)
    assert chunks[-1].endswith("x")


def test_best_objective_matches_on_shared_words(db):
    objs = objectives(db)
    chunk = "A business is an organisation; the entrepreneur performs key functions."
    obj_id, score = ingest.best_objective(chunk, objs)
    assert obj_id == "POB-1.1"
    assert score >= ingest.MIN_KEYWORD_OVERLAP


def test_best_objective_returns_none_when_unrelated(db):
    objs = objectives(db)
    obj_id, score = ingest.best_objective("Xyzzy plugh frobnicate quux wibble.", objs)
    assert obj_id is None
    assert score < ingest.MIN_KEYWORD_OVERLAP


def test_parse_mark_points():
    text = (
        "Award 1 mark for each:\n"
        "- profit motive\n"
        "* provides employment\n"
        "1. produces goods\n"
        "(a) generates revenue\n"
        "this line is prose and should be ignored\n"
    )
    points = ingest.parse_mark_points(text)
    assert "profit motive" in points
    assert "provides employment" in points
    assert "produces goods" in points
    assert "generates revenue" in points
    assert len(points) == 4


# ---------------------------------------------------------------------------
# ingest_page: indexed chunk lands in chunks + correct vec table
# ---------------------------------------------------------------------------
def test_matched_chunk_indexed_in_vec_notes(db):
    counts = ingest.new_counts()
    ingest.ingest_page(
        db, doc_id="notes-doc1", subject_id="Principles_of_Business",
        content_type="notes",
        source_file=r"D:\KB\Principles_of_Business\04_NOTES\notes1.pdf",
        page=1,
        text="A business is an organisation and the entrepreneur performs functions.",
        objectives=objectives(db), counts=counts, embed_fn=fake_embed,
    )
    db.commit()

    chunk = db.execute(
        "SELECT id, objective_id FROM chunks WHERE doc_id = 'notes-doc1'"
    ).fetchone()
    assert chunk is not None
    assert chunk["objective_id"] == "POB-1.1"          # real FK, not unmapped

    in_vec = db.execute(
        "SELECT rowid FROM vec_notes WHERE rowid = ?", (chunk["id"],)
    ).fetchone()
    assert in_vec is not None and in_vec[0] == chunk["id"]

    # not routed to the wrong table
    assert db.execute("SELECT count(*) FROM vec_past_papers").fetchone()[0] == 0
    assert counts["chunks_indexed"] == 1
    assert counts["queued"] == 0


def test_unmatched_chunk_goes_to_review_queue(db):
    counts = ingest.new_counts()
    ingest.ingest_page(
        db, doc_id="notes-doc1", subject_id="Principles_of_Business",
        content_type="notes",
        source_file=r"D:\KB\Principles_of_Business\04_NOTES\notes1.pdf",
        page=1,
        text="Xyzzy plugh frobnicate quux wibble zorp.",
        objectives=objectives(db), counts=counts, embed_fn=fake_embed,
    )
    db.commit()

    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    queued = db.execute("SELECT chunk_text, reason FROM ingest_review_queue").fetchall()
    assert len(queued) == 1
    assert queued[0]["reason"] == "no_objective_match"
    assert counts["queued"] == 1
    assert counts["chunks_indexed"] == 0


def test_mark_scheme_chunk_routes_and_extracts_points(db):
    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, source_file, content_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        ("ms-doc1", "Principles_of_Business", "mark_scheme",
         r"D:\KB\Principles_of_Business\03_MARK_SCHEMES\ms1.pdf", "hash-ms-1"),
    )
    db.commit()

    counts = ingest.new_counts()
    text = (
        "Explain the functions of an entrepreneur and the concept of a business.\n"
        "- organises resources\n"
        "- bears risk\n"
    )
    ingest.ingest_page(
        db, doc_id="ms-doc1", subject_id="Principles_of_Business",
        content_type="mark_scheme",
        source_file=r"D:\KB\Principles_of_Business\03_MARK_SCHEMES\ms1.pdf",
        page=1, text=text, objectives=objectives(db), counts=counts,
        embed_fn=fake_embed,
    )
    db.commit()

    chunk = db.execute("SELECT id FROM chunks WHERE doc_id = 'ms-doc1'").fetchone()
    assert chunk is not None
    # routed to the mark-scheme vec table
    assert db.execute(
        "SELECT rowid FROM vec_mark_schemes WHERE rowid = ?", (chunk["id"],)
    ).fetchone() is not None
    # mark points captured under the chunk's objective
    pts = db.execute(
        "SELECT point_text FROM mark_points WHERE objective_id = 'POB-1.1' ORDER BY point_order"
    ).fetchall()
    assert [p["point_text"] for p in pts] == ["organises resources", "bears risk"]
    assert counts["mark_points"] == 2


def test_locked_subject_gate(db):
    # locked subject passes the gate (no SystemExit)
    ingest.assert_subject_locked(db, "Principles_of_Business")

    # an unlocked subject must be refused
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 0)",
        ("Economics", "Economics"),
    )
    db.commit()
    with pytest.raises(SystemExit):
        ingest.assert_subject_locked(db, "Economics")

    # an unknown subject must be refused
    with pytest.raises(SystemExit):
        ingest.assert_subject_locked(db, "Nonexistent_Subject")
