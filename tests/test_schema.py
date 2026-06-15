"""
tests/test_schema.py
====================
Stage 1 tests: verify schema.sql creates all expected tables,
vec virtual tables load correctly, and basic FK behaviour works.

Run: pytest tests/test_schema.py -v
Does NOT require the SSD or .env — uses an in-memory database.
"""

import sqlite3
import struct
import sys
from pathlib import Path

import pytest

# Make sure backend/ is importable from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "backend" / "db" / "schema.sql"

EXPECTED_REGULAR_TABLES = {
    "subjects",
    "syllabus_sections",
    "objectives",
    "documents",
    "chunks",
    "mark_points",
    "study_sessions",
    "weakness_log",
    "revision_schedule",
    "ingest_review_queue",
    "practice_questions",
    "study_plan",
    "study_batches",
}

EXPECTED_VEC_TABLES = {
    "vec_notes",
    "vec_past_papers",
    "vec_mark_schemes",
}


def open_test_db() -> sqlite3.Connection:
    """Open an in-memory SQLite database with sqlite-vec loaded."""
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed — skipping schema tests")

    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    return db


def apply_schema(db: sqlite3.Connection) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    for stmt in sql.split(";"):
        stmt = stmt.strip()
        if stmt:
            db.execute(stmt)
    db.commit()


@pytest.fixture
def db():
    conn = open_test_db()
    apply_schema(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Table existence tests
# ---------------------------------------------------------------------------

def test_regular_tables_exist(db):
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {r["name"] for r in rows}
    for table in EXPECTED_REGULAR_TABLES:
        assert table in names, f"Missing table: {table}"


def test_vec_tables_exist(db):
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'vec_%'"
    ).fetchall()
    names = {r["name"] for r in rows}
    for table in EXPECTED_VEC_TABLES:
        assert table in names, f"Missing vec table: {table}"


def test_ingest_review_queue_has_objective_and_doc_columns(db):
    """The objective_id / doc_id columns (formerly added via ensure_queue_columns'
    ALTER TABLE) are now part of the canonical schema."""
    cols = {r["name"] for r in db.execute("PRAGMA table_info(ingest_review_queue)")}
    assert "objective_id" in cols
    assert "doc_id" in cols


# ---------------------------------------------------------------------------
# Basic insert and readback
# ---------------------------------------------------------------------------

def test_subject_insert_and_readback(db):
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) "
        "VALUES (?, ?, ?)",
        ("Principles_of_Business", "Principles of Business", 0),
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM subjects WHERE subject_id = ?",
        ("Principles_of_Business",),
    ).fetchone()
    assert row is not None
    assert row["display_name"] == "Principles of Business"
    assert row["syllabus_locked"] == 0


def test_syllabus_locked_default_is_zero(db):
    db.execute(
        "INSERT INTO subjects (subject_id, display_name) VALUES (?, ?)",
        ("Economics", "Economics"),
    )
    db.commit()
    row = db.execute(
        "SELECT syllabus_locked FROM subjects WHERE subject_id = 'Economics'"
    ).fetchone()
    assert row["syllabus_locked"] == 0


# ---------------------------------------------------------------------------
# Foreign key enforcement
# ---------------------------------------------------------------------------

def test_fk_section_requires_valid_subject(db):
    """Inserting a section with a non-existent subject_id must raise."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO syllabus_sections (section_id, subject_id, title) "
            "VALUES (?, ?, ?)",
            ("SEC-1", "NONEXISTENT_SUBJECT", "Nature of Business"),
        )
        db.commit()


def test_fk_objective_requires_valid_section(db):
    """Inserting an objective with a non-existent section_id must raise."""
    db.execute(
        "INSERT INTO subjects (subject_id, display_name) VALUES (?, ?)",
        ("Principles_of_Business", "Principles of Business"),
    )
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO objectives "
            "(objective_id, section_id, subject_id, objective_num, content_stmt) "
            "VALUES (?, ?, ?, ?, ?)",
            ("POB-1.1", "NONEXISTENT_SECTION", "Principles_of_Business", "1.1", "Test"),
        )
        db.commit()


# ---------------------------------------------------------------------------
# Full chain insert (subject → section → objective)
# ---------------------------------------------------------------------------

def test_full_chain_insert(db):
    db.execute(
        "INSERT INTO subjects (subject_id, display_name) VALUES (?, ?)",
        ("Principles_of_Business", "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES (?, ?, ?, ?)",
        ("POB-SEC-1", "Principles_of_Business", "Nature of Business", "1"),
    )
    db.execute(
        "INSERT INTO objectives "
        "(objective_id, section_id, subject_id, objective_num, content_stmt, skill_type) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "POB-1.1",
            "POB-SEC-1",
            "Principles_of_Business",
            "1.1",
            "Define the concept of a business",
            "Knowledge",
        ),
    )
    db.commit()

    row = db.execute(
        "SELECT o.objective_id, o.content_stmt, s.display_name "
        "FROM objectives o JOIN subjects s ON s.subject_id = o.subject_id "
        "WHERE o.objective_id = 'POB-1.1'"
    ).fetchone()
    assert row is not None
    assert row["objective_id"] == "POB-1.1"
    assert "business" in row["content_stmt"].lower()


# ---------------------------------------------------------------------------
# vec table basic smoke test
# ---------------------------------------------------------------------------

def test_vec_insert_and_search(db):
    """Insert a dummy 768-dim vector and confirm it is searchable."""
    # First need a valid chain for the chunk FK
    db.execute(
        "INSERT INTO subjects (subject_id, display_name) VALUES (?, ?)",
        ("Principles_of_Business", "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title) VALUES (?, ?, ?)",
        ("POB-SEC-1", "Principles_of_Business", "Nature of Business"),
    )
    db.execute(
        "INSERT INTO objectives "
        "(objective_id, section_id, subject_id, objective_num, content_stmt) "
        "VALUES (?, ?, ?, ?, ?)",
        ("POB-1.1", "POB-SEC-1", "Principles_of_Business", "1.1", "Define business"),
    )
    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, source_file, content_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        ("doc-001", "Principles_of_Business", "notes", "test.pdf", "abc123"),
    )
    db.execute(
        "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, chunk_id) "
        "VALUES (?, ?, ?, ?, ?)",
        ("doc-001", "POB-1.1", "Principles_of_Business", "A business is an organisation.", "chunk-001"),
    )
    db.commit()

    # Get the chunk rowid
    row = db.execute("SELECT id FROM chunks WHERE chunk_id = 'chunk-001'").fetchone()
    chunk_rowid = row["id"]

    # Insert a dummy 768-dim embedding (all zeros)
    vec = struct.pack("768f", *([0.0] * 768))
    db.execute(
        "INSERT OR REPLACE INTO vec_notes (rowid, embedding) VALUES (?, ?)",
        (chunk_rowid, vec),
    )
    db.commit()

    # Confirm the rowid appears in vec_notes
    result = db.execute(
        "SELECT rowid FROM vec_notes WHERE rowid = ?", (chunk_rowid,)
    ).fetchone()
    assert result is not None
    assert result[0] == chunk_rowid
