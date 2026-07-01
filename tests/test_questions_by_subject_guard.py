"""
tests/test_questions_by_subject_guard.py
===========================================
Verify that the /api/questions/{subject_id} grade-mode picker query excludes
mark_points whose backing chunk has page=NULL, and includes ones where page
is set.

Rule 2 of the build plan: every row served to the student must carry a
non-null source_page (or equivalent traceability field). This endpoint powers
chat.html's grade-mode question picker and previously had no page guard at
all -- a chunk_id whose text was reconstructed rather than extracted from a
PDF (e.g. Economics' synthesized specimen stems) was reachable here even
after the same gap was closed on /api/questions (see test_quiz_picker.py).
"""
import sqlite3
import sys
from pathlib import Path

import sqlite_vec

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "backend"))

_SCHEMA_PATH = _REPO_ROOT / "backend" / "db" / "schema.sql"


def _make_db() -> sqlite3.Connection:
    """Minimal in-memory DB seeded for grade-mode-picker tests."""
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
        "('doc2','Economics','specimen','Specimen Paper - 2016',2016,'src2.pdf','hash2')"
    )

    # Chunk WITH page set — a real, PDF-extracted stem
    db.execute("""
        INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page,
                            question_num, chunk_id)
        VALUES ('doc1','ECON-1.6','Economics','Question stem with page',
                1, '1(a)', 'ECON-qb1(a)v1-stem')
    """)

    # Chunk with page = NULL — a reconstructed/synthesized stem, must be excluded
    db.execute("""
        INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page,
                            question_num, chunk_id)
        VALUES ('doc2','ECON-1.6','Economics','Question stem no page',
                NULL, '2(a)', 'ECON-qb2(a)v1-stem')
    """)

    # mark_points rows keyed on each chunk_id, so both would otherwise be
    # "gradeable questions" per the endpoint's own definition.
    db.execute(
        "INSERT INTO mark_points "
        "(mark_point_id, objective_id, question_id, point_text, marks_value, point_order) "
        "VALUES ('mp1','ECON-1.6','ECON-qb1(a)v1-stem','Point A',1,1)"
    )
    db.execute(
        "INSERT INTO mark_points "
        "(mark_point_id, objective_id, question_id, point_text, marks_value, point_order) "
        "VALUES ('mp2','ECON-1.6','ECON-qb2(a)v1-stem','Point B',1,1)"
    )

    db.commit()
    return db


def _run_grade_mode_picker(db: sqlite3.Connection, subject_id: str = "Economics") -> list:
    """Run the same SQL the /api/questions/{subject_id} endpoint uses."""
    rows = db.execute(
        """
        SELECT mp.question_id            AS question_id,
               mp.objective_id           AS objective_id,
               c.chunk_text              AS question_text,
               c.question_num            AS question_num,
               d.paper                   AS paper,
               d.year                    AS year,
               COUNT(mp.mark_point_id)   AS marks
        FROM   mark_points mp
        JOIN   chunks c ON c.chunk_id = mp.question_id
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  c.subject_id = ?
          AND  c.page IS NOT NULL
        GROUP  BY mp.question_id
        ORDER  BY d.year DESC, d.paper, mp.question_id
        """,
        (subject_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def test_chunk_with_page_is_included():
    """A mark_points row backed by a chunk with page set must appear."""
    db = _make_db()
    results = _run_grade_mode_picker(db)
    qids = [r["question_id"] for r in results]
    assert "ECON-qb1(a)v1-stem" in qids


def test_chunk_with_null_page_is_excluded():
    """A mark_points row backed by a chunk with page=NULL (a reconstructed
    stem, not PDF-extracted) must be absent from the grade-mode picker."""
    db = _make_db()
    results = _run_grade_mode_picker(db)
    qids = [r["question_id"] for r in results]
    assert "ECON-qb2(a)v1-stem" not in qids


def test_only_paged_chunk_returned():
    """Exactly one row is returned (the paged chunk); the NULL-page chunk is silently dropped."""
    db = _make_db()
    results = _run_grade_mode_picker(db)
    assert len(results) == 1
    assert results[0]["question_text"] == "Question stem with page"


def test_pob_style_all_real_pages_unaffected():
    """A subject where every chunk has a real page (the normal/POB pattern)
    must return all of its gradeable questions unchanged by the guard."""
    db = _make_db()
    db.execute("INSERT INTO subjects VALUES ('Principles_of_Business','POB',1)")
    db.execute("INSERT INTO syllabus_sections VALUES ('POB-S1','Principles_of_Business','Sec 1','1')")
    db.execute(
        "INSERT INTO objectives VALUES "
        "('POB-1.1','POB-S1','Principles_of_Business','1.1','Test',NULL,NULL,NULL,0)"
    )
    db.execute(
        "INSERT INTO documents VALUES "
        "('doc3','Principles_of_Business','past_paper','P2',2022,'src3.pdf','hash3')"
    )
    db.execute("""
        INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page,
                            question_num, chunk_id)
        VALUES ('doc3','POB-1.1','Principles_of_Business','POB question stem',
                3, '1(a)', 'POB-2022-P2-q1a-stem')
    """)
    db.execute(
        "INSERT INTO mark_points "
        "(mark_point_id, objective_id, question_id, point_text, marks_value, point_order) "
        "VALUES ('mp3','POB-1.1','POB-2022-P2-q1a-stem','Point C',1,1)"
    )
    db.commit()

    results = _run_grade_mode_picker(db, subject_id="Principles_of_Business")
    qids = [r["question_id"] for r in results]
    assert "POB-2022-P2-q1a-stem" in qids
    assert len(results) == 1
