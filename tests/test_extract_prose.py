"""
tests/test_extract_prose.py
===========================
Tests for backend/db/extract_prose_markpoints.py.

An in-memory SQLite DB (full schema + sqlite-vec) is seeded with a locked
subject, an objective, a document and a chunk, plus rows in
ingest_review_queue. ollama_chat is replaced with a stub (injected via the
chat_fn parameter), so no Ollama and no network are needed.

ingest_review_queue now carries objective_id / doc_id columns populated at
queue time; the extractor reads those first and only falls back to the
chunk-text lookup when both are NULL. Both paths are covered here.

Run: pytest tests/test_extract_prose.py -v
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "db"))

import extract_prose_markpoints as ep  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"
PROSE_TEXT = (
    "The entrepreneur accepts the financial risk of the business and organises "
    "the resources of production to provide goods and services for a profit."
)


def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping prose extraction tests")
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
    """Locked subject + objective + document + a chunk whose text == PROSE_TEXT."""
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES "
        "('Principles_of_Business', 'Principles of Business', 1)"
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES ('POB-SEC-1', 'Principles_of_Business', 'Nature of Business', '1')"
    )
    db.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
        "content_stmt) VALUES ('POB-1.1', 'POB-SEC-1', 'Principles_of_Business', '1.1', "
        "'Explain the concept of a business and the functions of an entrepreneur')"
    )
    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, source_file, content_hash) "
        "VALUES ('doc1', 'Principles_of_Business', 'mark_scheme', 'D:\\sol\\x.pdf', 'hash1')"
    )
    db.execute(
        "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, chunk_id) "
        "VALUES ('doc1', 'POB-1.1', 'Principles_of_Business', ?, 'doc1-stem-1')",
        (PROSE_TEXT,),
    )
    db.commit()


def queue_row(db: sqlite3.Connection, text: str, reason: str = ep.PROSE_REASON,
              objective_id: str | None = None, doc_id: str | None = None) -> int:
    cur = db.execute(
        "INSERT INTO ingest_review_queue (source_file, chunk_text, reason, objective_id, doc_id) "
        "VALUES (?, ?, ?, ?, ?)",
        ("D:\\sol\\x.pdf", text, reason, objective_id, doc_id),
    )
    db.commit()
    return cur.lastrowid


@pytest.fixture
def db():
    conn = open_test_db()
    seed(conn)
    yield conn
    conn.close()


# --- chat stubs (signature matches ollama_chat: messages, system, schema=None) ---
def good_chat(messages, system, schema=None):
    return '["Point one.", "Point two.", "Point three."]'


def fenced_chat(messages, system, schema=None):
    return "```json\n[\"Alpha point.\", \"Beta point.\"]\n```"


def bad_chat(messages, system, schema=None):
    return "Sure! Here are the mark points you asked for: not-json {{{"


# ---------------------------------------------------------------------------
# Happy path -- objective_id / doc_id come from the queue row's own columns
# ---------------------------------------------------------------------------
def test_valid_response_inserts_markpoints_and_deletes_queue_row(db):
    qid = queue_row(db, PROSE_TEXT, objective_id="POB-1.1", doc_id="doc1")
    summary = ep.process_queue(db, chat_fn=good_chat, sleep_between=0)

    rows = db.execute(
        "SELECT mark_point_id, objective_id, question_id, doc_id, point_text, "
        "marks_value, point_order FROM mark_points ORDER BY point_order"
    ).fetchall()
    assert len(rows) == 3
    assert [r["point_text"] for r in rows] == ["Point one.", "Point two.", "Point three."]
    assert rows[0]["mark_point_id"] == f"POB-1.1-prose-{qid}-mp1"
    assert rows[2]["mark_point_id"] == f"POB-1.1-prose-{qid}-mp3"
    assert all(r["objective_id"] == "POB-1.1" for r in rows)
    assert all(r["question_id"] is None for r in rows)      # prose -> no structured key
    assert all(r["doc_id"] == "doc1" for r in rows)
    assert all(r["marks_value"] == 1 for r in rows)

    # the queue row was removed after a successful insert
    assert db.execute(
        "SELECT COUNT(1) FROM ingest_review_queue WHERE id = ?", (qid,)
    ).fetchone()[0] == 0

    assert summary["inserted"] == 3
    assert summary["failed"] == 0
    assert summary["skipped"] == 0


def test_reads_objective_from_queue_column_not_text_lookup(db):
    """objective_id/doc_id are taken from the row's columns even when the
    chunk_text matches NO chunks row (so the text lookup would have failed)."""
    qid = queue_row(
        db, "Totally unmatched prose with no corresponding chunks row.",
        objective_id="POB-1.1", doc_id="doc1",
    )
    summary = ep.process_queue(db, chat_fn=good_chat, sleep_between=0)

    rows = db.execute("SELECT objective_id, doc_id FROM mark_points").fetchall()
    assert len(rows) == 3
    assert all(r["objective_id"] == "POB-1.1" for r in rows)  # from the column
    assert all(r["doc_id"] == "doc1" for r in rows)
    assert summary["inserted"] == 3 and summary["skipped"] == 0
    assert db.execute(
        "SELECT COUNT(1) FROM ingest_review_queue WHERE id = ?", (qid,)
    ).fetchone()[0] == 0


def test_falls_back_to_text_lookup_when_columns_null(db):
    """Legacy rows with NULL objective_id/doc_id still resolve via chunk text."""
    qid = queue_row(db, PROSE_TEXT)  # columns left NULL; chunk_text matches seed chunk
    summary = ep.process_queue(db, chat_fn=good_chat, sleep_between=0)
    rows = db.execute("SELECT objective_id, doc_id FROM mark_points").fetchall()
    assert len(rows) == 3
    assert all(r["objective_id"] == "POB-1.1" for r in rows)  # found via lookup
    assert all(r["doc_id"] == "doc1" for r in rows)
    assert summary["inserted"] == 3
    assert db.execute(
        "SELECT COUNT(1) FROM ingest_review_queue WHERE id = ?", (qid,)
    ).fetchone()[0] == 0


def test_markdown_fenced_response_is_parsed(db):
    qid = queue_row(db, PROSE_TEXT, objective_id="POB-1.1", doc_id="doc1")
    summary = ep.process_queue(db, chat_fn=fenced_chat, sleep_between=0)
    texts = [r["point_text"] for r in db.execute(
        "SELECT point_text FROM mark_points ORDER BY point_order")]
    assert texts == ["Alpha point.", "Beta point."]
    assert summary["inserted"] == 2
    assert db.execute(
        "SELECT COUNT(1) FROM ingest_review_queue WHERE id = ?", (qid,)
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Failure tolerance
# ---------------------------------------------------------------------------
def test_malformed_response_does_not_crash_and_keeps_row(db):
    qid = queue_row(db, PROSE_TEXT, objective_id="POB-1.1", doc_id="doc1")
    summary = ep.process_queue(db, chat_fn=bad_chat, sleep_between=0)  # must not raise

    assert db.execute("SELECT COUNT(1) FROM mark_points").fetchone()[0] == 0
    # the row is left in the queue for a later retry
    assert db.execute(
        "SELECT COUNT(1) FROM ingest_review_queue WHERE id = ?", (qid,)
    ).fetchone()[0] == 1
    assert summary["failed"] == 1
    assert summary["inserted"] == 0


def test_no_objective_match_skips_and_keeps_row(db):
    # NULL columns AND chunk_text that matches no chunks row -> Rule 1: skip,
    # never invent an objective.
    qid = queue_row(db, "A completely unrelated chunk with no matching chunk row.")
    summary = ep.process_queue(db, chat_fn=good_chat, sleep_between=0)

    assert db.execute("SELECT COUNT(1) FROM mark_points").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(1) FROM ingest_review_queue WHERE id = ?", (qid,)
    ).fetchone()[0] == 1
    assert summary["skipped"] == 1
    assert summary["inserted"] == 0


# ---------------------------------------------------------------------------
# --dry-run writes nothing
# ---------------------------------------------------------------------------
def test_dry_run_inserts_nothing_and_keeps_rows(db):
    qid = queue_row(db, PROSE_TEXT, objective_id="POB-1.1", doc_id="doc1")
    summary = ep.process_queue(db, chat_fn=good_chat, dry_run=True, sleep_between=0)

    assert db.execute("SELECT COUNT(1) FROM mark_points").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(1) FROM ingest_review_queue WHERE id = ?", (qid,)
    ).fetchone()[0] == 1
    assert summary["inserted"] == 3  # reports what WOULD be inserted


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_strip_fences_variants():
    assert ep.strip_fences('```json\n["a"]\n```') == '["a"]'
    assert ep.strip_fences('```\n["a"]\n```') == '["a"]'
    assert ep.strip_fences('["a"]') == '["a"]'


def test_parse_points_rejects_non_array():
    with pytest.raises(Exception):
        ep.parse_points('{"not": "an array"}')
