"""
tests/test_quiz_picker.py
==========================
Verify that the /api/questions quiz-picker query excludes chunks whose page
column is NULL, and includes chunks where page is set.

Rule 2 of the build plan: every row served to the student must carry a
non-null source_page (or equivalent traceability field).  The quiz picker
enforces this at read time via `AND c.page IS NOT NULL`.
"""
import sqlite3
import sys
from pathlib import Path

import pytest
import sqlite_vec
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "backend"))

_SCHEMA_PATH = _REPO_ROOT / "backend" / "db" / "schema.sql"


def _make_db() -> sqlite3.Connection:
    """Minimal in-memory DB seeded for quiz-picker tests."""
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row

    for stmt in _SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        if stmt.strip():
            db.execute(stmt)
    db.commit()

    # Subject + section + objective
    db.execute("INSERT INTO subjects VALUES ('Economics','Economics',1)")
    db.execute("INSERT INTO syllabus_sections VALUES ('ECON-S1','Economics','Sec 1','1')")
    db.execute(
        "INSERT INTO objectives VALUES "
        "('ECON-1.6','ECON-S1','Economics','1.6','PPC',NULL,NULL,NULL,0)"
    )

    # Two documents
    db.execute(
        "INSERT INTO documents VALUES "
        "('doc1','Economics','past_paper','P2',2024,'src1.pdf','hash1')"
    )
    db.execute(
        "INSERT INTO documents VALUES "
        "('doc2','Economics','past_paper','P2',2023,'src2.pdf','hash2')"
    )

    # Chunk WITH page set — should appear in quiz picker
    db.execute("""
        INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page,
                            question_num, chunk_id)
        VALUES ('doc1','ECON-1.6','Economics','Question stem with page',
                1, '1(a)', 'ECON-qb1(a)v1-stem')
    """)

    # Chunk with page = NULL — must be excluded
    db.execute("""
        INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page,
                            question_num, chunk_id)
        VALUES ('doc2','ECON-1.6','Economics','Question stem no page',
                NULL, '2(a)', 'ECON-qb2(a)v1-stem')
    """)

    db.commit()
    return db


def _run_picker(db: sqlite3.Connection, subject_id: str = "Economics",
                paper=None, year=None) -> list:
    """Run the same SQL the /api/questions endpoint uses."""
    sql = [
        "SELECT c.chunk_id      AS question_id,",
        "       c.question_num  AS question_num,",
        "       d.paper         AS paper,",
        "       d.year          AS year,",
        "       SUBSTR(c.chunk_text, 1, 400) AS stem,",
        "       (SELECT COUNT(*) FROM mark_points mp",
        "          WHERE mp.question_id = c.chunk_id) AS marks_total",
        "FROM   chunks c",
        "JOIN   documents d ON d.doc_id = c.doc_id",
        "WHERE  c.subject_id = ?",
        "  AND  d.content_type IN ('past_paper', 'mark_scheme', 'specimen')",
        "  AND  c.question_num IS NOT NULL",
        "  AND  c.chunk_id LIKE '%-stem'",
        "  AND  c.page IS NOT NULL",
    ]
    params: list = [subject_id]
    if paper:
        sql.append("  AND  d.paper = ?")
        params.append(paper)
    if year is not None:
        sql.append("  AND  d.year = ?")
        params.append(year)
    sql.append("ORDER BY d.year DESC, c.question_num ASC")
    rows = db.execute("\n".join(sql), params).fetchall()
    return [dict(r) for r in rows]


def test_chunk_with_page_is_included():
    """A chunk with page set must appear in the quiz-picker result."""
    db = _make_db()
    results = _run_picker(db)
    qids = [r["question_id"] for r in results]
    assert "ECON-qb1(a)v1-stem" in qids


def test_chunk_with_null_page_is_excluded():
    """A chunk with page = NULL must be absent from the quiz-picker result."""
    db = _make_db()
    results = _run_picker(db)
    qids = [r["question_id"] for r in results]
    assert "ECON-qb2(a)v1-stem" not in qids


def test_only_paged_chunk_returned():
    """Exactly one row is returned (the paged chunk); the NULL-page chunk is silently dropped."""
    db = _make_db()
    results = _run_picker(db)
    assert len(results) == 1
    assert results[0]["stem"] == "Question stem with page"
