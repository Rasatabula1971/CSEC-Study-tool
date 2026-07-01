"""
tests/test_econ_specimen_stems.py
==================================
Tests for the Specimen 1 Economics -stem chunk ingestion (Stage 4).

Verifies:
  1. ingest_specimen_stems() creates 24 chunk rows and 1 document row.
  2. Requesting an Economics quiz question returns a -stem chunk whose
     chunk_id joins to at least one mark_points row.
  3. The /api/questions (filter-based) endpoint returns Economics questions
     after stem ingestion.
  4. The /api/questions/{subject_id} endpoint returns Economics questions
     after stem ingestion (via chunk join, not mp.doc_id).
  5. Idempotency: running ingest_specimen_stems() twice leaves exactly 24 chunk
     rows for the specimen document (no duplicates).
  6. Dry-run writes nothing to the DB.

STEM_TEXTS grew from 21 to 24 entries after the Question 6 block realignment
(see tools/ingest_econ_specimen_stems.py's block-assignment comment): 3 new
entries for the real Q6(a)/(b)(i)/(b)(ii), plus the existing qb6(c) id
corrected to resolve to Q6(c) instead of a stale "5(c)-2" duplicate. The
counts below are hardcoded (not len(STEM_TEXTS)) so a future accidental
change in STEM_TEXTS's size is still caught as a real regression.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# ── fixture helpers ────────────────────────────────────────────────────────────

_SCHEMA_SQL = (_REPO_ROOT / "backend" / "db" / "schema.sql").read_text()

# Import under test (no DB_PATH needed at import time)
from tools.ingest_econ_specimen_stems import (
    STEM_TEXTS,
    CONTENT_TYPE,
    PAPER,
    YEAR,
    SUBJECT_ID,
    ingest_specimen_stems,
    _doc_id_for_specimen,
    _content_hash_for_specimen,
)


def _make_test_db() -> sqlite3.Connection:
    """Return an in-memory DB with the full schema + minimal ECON data."""
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    # Apply schema (strip virtual tables — sqlite-vec not available in tests)
    for stmt in _SCHEMA_SQL.split(";"):
        s = stmt.strip()
        if s and "VIRTUAL TABLE" not in s.upper():
            db.execute(s)

    # Add point_group_id column (m020 migration not in schema.sql yet)
    try:
        db.execute("ALTER TABLE mark_points ADD COLUMN point_group_id TEXT")
    except sqlite3.OperationalError:
        pass  # already present

    # Add source_family column to chunks (ingest_v2 adds this)
    try:
        db.execute("ALTER TABLE chunks ADD COLUMN source_family TEXT")
    except sqlite3.OperationalError:
        pass

    # Seed subject
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        ("Economics", "Economics"),
    )
    # Seed section + objectives (minimal — one objective covers all 24 q_ids)
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title) VALUES (?, ?, ?)",
        ("ECON-S1", "Economics", "Test Section"),
    )
    for oid in ("ECON-1.6", "ECON-2.3", "ECON-4.4", "ECON-5.2",
                "ECON-6.1", "ECON-6.10", "ECON-6.11", "ECON-6.3",
                "ECON-1.1", "ECON-1.5", "ECON-1.7", "ECON-2.1",
                "ECON-2.2", "ECON-2.10", "ECON-3.2", "ECON-3.3",
                "ECON-3.7", "ECON-6.4", "ECON-6.9", "ECON-7.3", "ECON-8.2"):
        db.execute(
            "INSERT OR IGNORE INTO objectives "
            "(objective_id, section_id, subject_id, objective_num, content_stmt, verified) "
            "VALUES (?, 'ECON-S1', 'Economics', ?, 'Test objective', 1)",
            (oid, oid.replace("ECON-", "")),
        )

    # Seed mark_points for each STEM_TEXTS question_id so FK validation passes.
    # Simulate the post-backfill state (question_id already ends in -stem).
    for i, qid in enumerate(sorted(STEM_TEXTS.keys())):
        # Pick a sensible primary objective per question_id block
        if qid.startswith("ECON-qb1"):
            obj = "ECON-1.6"
        elif qid.startswith("ECON-qb2"):
            obj = "ECON-2.3"
        elif qid.startswith("ECON-qb3"):
            obj = "ECON-6.1"
        elif qid.startswith("ECON-qb4"):
            obj = "ECON-5.2"
        else:  # qb5 + qb6
            obj = "ECON-4.4"

        mpid = f"TEST-mp-{i}"
        db.execute(
            "INSERT INTO mark_points "
            "(mark_point_id, objective_id, question_id, point_text, marks_value, point_order) "
            "VALUES (?, ?, ?, 'Test point', 1, 1)",
            (mpid, obj, qid),
        )

    db.commit()
    return db


# ── tests ──────────────────────────────────────────────────────────────────────

def test_ingest_creates_document_and_24_chunks():
    """ingest_specimen_stems() writes exactly 1 document and 24 chunks."""
    db = _make_test_db()
    written = ingest_specimen_stems(db, dry_run=False)

    assert written == 24, f"Expected 24 chunks written, got {written}"

    doc_count = db.execute(
        "SELECT COUNT(*) FROM documents WHERE content_type = 'specimen' "
        "AND subject_id = 'Economics'"
    ).fetchone()[0]
    assert doc_count == 1

    chunk_count = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE chunk_id LIKE 'ECON-qb%-stem' "
        "AND chunk_id IN (SELECT chunk_id FROM chunks WHERE doc_id IN "
        "   (SELECT doc_id FROM documents WHERE content_type='specimen'))"
    ).fetchone()[0]
    assert chunk_count == 24
    db.close()


def test_each_stem_chunk_joins_to_mark_points():
    """Every -stem chunk_id created by the ingest joins to a mark_points row."""
    db = _make_test_db()
    ingest_specimen_stems(db, dry_run=False)

    # All chunk_ids ending in -stem that we created
    rows = db.execute(
        "SELECT chunk_id FROM chunks WHERE subject_id = 'Economics' "
        "AND chunk_id LIKE 'ECON-qb%-stem'"
    ).fetchall()
    assert len(rows) == 24

    for row in rows:
        cid = row["chunk_id"]
        mp_row = db.execute(
            "SELECT 1 FROM mark_points WHERE question_id = ? LIMIT 1", (cid,)
        ).fetchone()
        assert mp_row is not None, f"No mark_points row for chunk_id={cid!r}"
    db.close()


def test_quiz_picker_query_returns_econ_questions():
    """The /api/questions SQL (subject_id filter) returns ECON questions via chunk join."""
    db = _make_test_db()
    ingest_specimen_stems(db, dry_run=False)

    # Mirror the /api/questions/{subject_id} query AFTER the fix (join through chunks)
    rows = db.execute(
        """
        SELECT mp.question_id, c.chunk_text, c.question_num, d.paper, d.year,
               COUNT(mp.mark_point_id) AS marks
        FROM   mark_points mp
        JOIN   chunks c ON c.chunk_id = mp.question_id
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  c.subject_id = 'Economics'
        GROUP  BY mp.question_id
        ORDER  BY d.year DESC, d.paper, mp.question_id
        """
    ).fetchall()

    assert len(rows) == 24, f"Expected 24 quiz rows, got {len(rows)}"
    # All question_ids should end in -stem
    for r in rows:
        assert r["question_id"].endswith("-stem"), (
            f"question_id {r['question_id']!r} doesn't end in -stem"
        )
    db.close()


def test_filter_query_returns_econ_specimen_paper():
    """The /api/filters-style query finds the specimen paper when content_type='specimen'."""
    db = _make_test_db()
    ingest_specimen_stems(db, dry_run=False)

    papers = db.execute(
        """
        SELECT DISTINCT d.paper
        FROM   chunks c
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  c.subject_id = 'Economics'
          AND  d.content_type IN ('past_paper', 'mark_scheme', 'specimen')
          AND  c.question_num IS NOT NULL
          AND  c.chunk_id LIKE '%-stem'
          AND  d.paper IS NOT NULL
        ORDER BY d.paper ASC
        """
    ).fetchall()

    paper_names = [r["paper"] for r in papers]
    assert PAPER in paper_names, (
        f"{PAPER!r} not found in quiz filter papers: {paper_names}"
    )
    db.close()


def test_idempotent_second_run_no_duplicates():
    """Running ingest_specimen_stems() twice leaves exactly 24 stem chunks."""
    db = _make_test_db()
    ingest_specimen_stems(db, dry_run=False)
    written2 = ingest_specimen_stems(db, dry_run=False)

    assert written2 == 0, f"Second run should write 0 rows; got {written2}"

    count = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE subject_id = 'Economics' "
        "AND chunk_id LIKE 'ECON-qb%-stem'"
    ).fetchone()[0]
    assert count == 24
    db.close()


def test_dry_run_writes_nothing():
    """--dry-run reports what would be done but writes nothing."""
    db = _make_test_db()
    written = ingest_specimen_stems(db, dry_run=True)

    assert written == 0
    doc_count = db.execute(
        "SELECT COUNT(*) FROM documents WHERE content_type = 'specimen'"
    ).fetchone()[0]
    assert doc_count == 0

    chunk_count = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE subject_id = 'Economics' "
        "AND chunk_id LIKE 'ECON-qb%-stem'"
    ).fetchone()[0]
    assert chunk_count == 0
    db.close()
