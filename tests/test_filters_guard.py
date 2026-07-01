"""
tests/test_filters_guard.py
==============================
Verify that /api/filters excludes a document whose chunks are all page=NULL
from the paper/year dropdown lists.

Rule 2 of the build plan: every row served to the student must carry a
non-null source_page. A document like Economics' synthesized "Specimen
Paper - 2016" (all 21 chunks reconstructed rather than PDF-extracted, so
page=NULL on every one) must never be offered as a selectable filter option
-- doing so would let a student pick a paper whose questions the guarded
/api/questions picker can then never actually return.
"""
import sqlite3
import sys
from pathlib import Path

import sqlite_vec

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "backend"))

_SCHEMA_PATH = _REPO_ROOT / "backend" / "db" / "schema.sql"


def _make_db() -> sqlite3.Connection:
    """Minimal in-memory DB seeded for /api/filters tests."""
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

    db.execute("INSERT INTO subjects VALUES ('Economics','Economics',1)")
    db.execute("INSERT INTO syllabus_sections VALUES ('ECON-S1','Economics','Sec 1','1')")
    db.execute(
        "INSERT INTO objectives VALUES "
        "('ECON-1.6','ECON-S1','Economics','1.6','PPC',NULL,NULL,NULL,0)"
    )

    # A real past paper — every chunk has a real page
    db.execute(
        "INSERT INTO documents VALUES "
        "('doc1','Economics','past_paper','Paper 2 - 2024',2024,'src1.pdf','hash1')"
    )
    db.execute("""
        INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page,
                            question_num, chunk_id)
        VALUES ('doc1','ECON-1.6','Economics','Question stem with page',
                1, '1(a)', 'ECON-qb1(a)v1-stem')
    """)

    # A synthesized "specimen" document — every chunk has page=NULL
    db.execute(
        "INSERT INTO documents VALUES "
        "('doc2','Economics','specimen','Specimen Paper - 2016',2016,'src2.pdf','hash2')"
    )
    db.execute("""
        INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page,
                            question_num, chunk_id)
        VALUES ('doc2','ECON-1.6','Economics','Question stem no page',
                NULL, '2(a)', 'ECON-qb2(a)v1-stem')
    """)

    db.commit()
    return db


def _run_filters(db: sqlite3.Connection, subject_id: str = "Economics") -> dict:
    """Run the same SQL the /api/filters endpoint uses."""
    papers = db.execute(
        """
        SELECT DISTINCT d.paper AS paper
        FROM   chunks c
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  c.subject_id = ?
          AND  d.content_type IN ('past_paper', 'mark_scheme', 'specimen')
          AND  c.question_num IS NOT NULL
          AND  c.chunk_id LIKE '%-stem'
          AND  c.page IS NOT NULL
          AND  d.paper IS NOT NULL
        ORDER  BY d.paper ASC
        """,
        (subject_id,),
    ).fetchall()
    years = db.execute(
        """
        SELECT DISTINCT d.year AS year
        FROM   chunks c
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  c.subject_id = ?
          AND  d.content_type IN ('past_paper', 'mark_scheme', 'specimen')
          AND  c.question_num IS NOT NULL
          AND  c.chunk_id LIKE '%-stem'
          AND  c.page IS NOT NULL
          AND  d.year IS NOT NULL
        ORDER  BY d.year DESC
        """,
        (subject_id,),
    ).fetchall()
    return {
        "papers": [r["paper"] for r in papers],
        "years": [r["year"] for r in years],
    }


def test_paper_with_real_pages_is_included():
    """A document whose chunks have real pages must appear in the paper list."""
    db = _make_db()
    result = _run_filters(db)
    assert "Paper 2 - 2024" in result["papers"]
    assert 2024 in result["years"]


def test_document_with_all_null_pages_excluded_from_papers():
    """A document whose ONLY chunks have page=NULL must not appear as a
    selectable paper option."""
    db = _make_db()
    result = _run_filters(db)
    assert "Specimen Paper - 2016" not in result["papers"]


def test_document_with_all_null_pages_excluded_from_years():
    """Same guard applied to the year dropdown."""
    db = _make_db()
    result = _run_filters(db)
    assert 2016 not in result["years"]


def test_only_real_paper_and_year_returned():
    """Exactly one paper/year pair survives — the page=NULL document contributes nothing."""
    db = _make_db()
    result = _run_filters(db)
    assert result["papers"] == ["Paper 2 - 2024"]
    assert result["years"] == [2024]
