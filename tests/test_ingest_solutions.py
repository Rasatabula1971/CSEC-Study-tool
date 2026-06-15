"""
tests/test_ingest_solutions.py
==============================
Tests for backend/ingest_solutions.py -- the text-format Paper 2 solution
ingester. Exercises parsing + storage on plain strings (not files) against an
in-memory SQLite DB with the full schema and sqlite-vec loaded. A fake embedder
is injected, so these tests need neither Ollama nor PyMuPDF.

The headline guarantee: every stored question is a '-stem' chunk that the quiz
page (GET /api/questions, /api/filters) can actually see, with mark_points keyed
on question_id == chunk_id (what grade.fetch_mark_points and the quiz mark-count
subquery both rely on).

Run: pytest tests/test_ingest_solutions.py -v
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import backend.ingest_solutions as isol  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"
EMBED_DIM = 768

SUBJECT = "Principles_of_Business"

# A self-contained sample paper (mirrors the template's format) with three
# questions whose vocabulary clearly overlaps the three seeded objectives.
SAMPLE = """\
SUBJECT: Principles_of_Business
PAPER: 2
SESSION: June
YEAR: 2024

QUESTION 1
Explain THREE functions that an entrepreneur performs when organising a business.
ANSWER:
- Organising the factors of production such as land, labour and capital.
- Bearing the risk of financial loss in the business.
- Making decisions about what to produce and how to finance it.

QUESTION 2
Distinguish between the private sector and the public sector and give an example of each.
ANSWER:
- The private sector is owned and controlled by private individuals for profit.
- The public sector is owned and controlled by the government to provide services.
- A valid example of an organisation in each sector.

QUESTION 3
Outline FOUR reasons why a business should keep proper accounting records.
ANSWER:
- To monitor the profit or loss of the business.
- To support financial decisions made by the owner.
- To meet legal requirements such as paying taxes.
- To support a request for a loan or other finance.
"""


def fake_embed(text: str) -> list[float]:
    """Deterministic dummy embedding -- no Ollama required."""
    return [0.0] * EMBED_DIM


def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping solution-ingest tests")

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
    """Locked POB subject + one section + three objectives the sample maps to."""
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES (?, ?, ?, ?)",
        ("POB-SEC-1", SUBJECT, "Nature of Business", "1"),
    )
    objectives = [
        ("POB-1.1", "1.1",
         "Explain the functions of the entrepreneur and the concept of a business, "
         "including organising the factors of production and bearing risk."),
        ("POB-1.2", "1.2",
         "Distinguish between the private sector and the public sector and give "
         "examples of organisations in each."),
        ("POB-5.1", "5.1",
         "Outline the reasons a business should keep accounting records, including "
         "monitoring profit and supporting financial decisions."),
    ]
    for oid, num, stmt in objectives:
        db.execute(
            "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
            "content_stmt, skill_type) VALUES (?, ?, ?, ?, ?, ?)",
            (oid, "POB-SEC-1", SUBJECT, num, stmt, "Understanding"),
        )
    db.commit()


@pytest.fixture
def db():
    conn = open_test_db()
    seed_locked_subject(conn)
    yield conn
    conn.close()


def objectives(db):
    return isol.load_objectives(db, SUBJECT)


def ingest_sample(db, text=SAMPLE):
    counts = isol.new_counts()
    meta = isol.ingest_solution_text(
        db, text=text, subject_id=SUBJECT, source_file="POB_Paper2_June2024.txt",
        objectives=objectives(db), counts=counts, embed_fn=fake_embed,
        content_hash="hash-sample-1",
    )
    db.commit()
    return meta, counts


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------
def test_parse_header():
    meta = isol.parse_header(SAMPLE)
    assert meta is not None
    assert meta["paper_num"] == "2"
    assert meta["session"] == "June"
    assert meta["year"] == 2024
    assert meta["paper_label"] == "Paper 2 - June 2024"
    assert meta["paper_short"] == "June2024"


def test_parse_header_requires_paper_and_year():
    assert isol.parse_header("SUBJECT: POB\nSESSION: June\n") is None        # no PAPER/YEAR
    assert isol.parse_header("PAPER: 2\nYEAR: not-a-year\n") is None          # no 4-digit year


def test_parse_header_without_session():
    meta = isol.parse_header("PAPER: 1\nYEAR: 2019\n\nQUESTION 1\nx\n")
    assert meta["paper_label"] == "Paper 1 - 2019"
    assert meta["paper_short"] == "P12019"


def test_parse_questions():
    qs = isol.parse_questions(SAMPLE)
    assert [q["num"] for q in qs] == [1, 2, 3]
    assert "entrepreneur performs" in qs[0]["stem"]
    assert "ANSWER" not in qs[0]["stem"]               # the separator is not part of the stem
    assert len(qs[0]["points"]) == 3
    assert len(qs[2]["points"]) == 4
    assert qs[0]["points"][0].startswith("Organising the factors")


# ---------------------------------------------------------------------------
# Storage: chunks, mark_points, FK integrity, quiz visibility
# ---------------------------------------------------------------------------
def test_stem_chunks_created(db):
    meta, counts = ingest_sample(db)
    assert meta["paper_label"] == "Paper 2 - June 2024"
    assert counts["questions"] == 3

    stems = db.execute(
        "SELECT chunk_id, objective_id, question_num FROM chunks "
        "WHERE chunk_id LIKE '%-stem' ORDER BY chunk_id"
    ).fetchall()
    assert len(stems) == 3
    # chunk_id format: {objective_id}-{paper_short}-q{num}-stem
    ids = {r["chunk_id"] for r in stems}
    assert "POB-1.1-June2024-q1-stem" in ids
    assert "POB-1.2-June2024-q2-stem" in ids
    assert "POB-5.1-June2024-q3-stem" in ids
    # every stem chunk carries a real question_num (the quiz filter needs it)
    assert all(r["question_num"] is not None for r in stems)


def test_mark_points_fk_to_chunks(db):
    ingest_sample(db)

    # 3 + 3 + 4 bullets across the three questions
    total = db.execute("SELECT COUNT(*) FROM mark_points").fetchone()[0]
    assert total == 10

    # Every mark point's question_id must equal an existing '-stem' chunk_id, and
    # its objective_id must match that chunk's objective_id (FK + key integrity).
    orphans = db.execute(
        """
        SELECT mp.mark_point_id
        FROM   mark_points mp
        LEFT   JOIN chunks c ON c.chunk_id = mp.question_id
        WHERE  c.chunk_id IS NULL
            OR c.objective_id <> mp.objective_id
        """
    ).fetchall()
    assert orphans == []

    # mark_point_id format: {chunk_id}-mp{n}
    q1 = db.execute(
        "SELECT mark_point_id, point_order FROM mark_points "
        "WHERE question_id = 'POB-1.1-June2024-q1-stem' ORDER BY point_order"
    ).fetchall()
    assert [r["mark_point_id"] for r in q1] == [
        "POB-1.1-June2024-q1-stem-mp1",
        "POB-1.1-June2024-q1-stem-mp2",
        "POB-1.1-June2024-q1-stem-mp3",
    ]


def test_chunks_visible_to_quiz_page_query(db):
    """The chunks must satisfy the exact GET /api/questions filter (app.py)."""
    ingest_sample(db)
    rows = db.execute(
        """
        SELECT c.chunk_id AS question_id,
               c.question_num AS question_num,
               d.paper AS paper,
               d.year AS year,
               (SELECT COUNT(*) FROM mark_points mp
                  WHERE mp.question_id = c.chunk_id) AS marks_total
        FROM   chunks c
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  c.subject_id = ?
          AND  d.content_type IN ('past_paper', 'mark_scheme')
          AND  c.question_num IS NOT NULL
          AND  c.chunk_id LIKE '%-stem'
        ORDER  BY c.question_num ASC
        """,
        (SUBJECT,),
    ).fetchall()
    assert len(rows) == 3
    assert [r["paper"] for r in rows] == ["Paper 2 - June 2024"] * 3
    assert [r["year"] for r in rows] == [2024, 2024, 2024]
    # The quiz page's mark-count subquery resolves (>0) for every question.
    assert all(r["marks_total"] > 0 for r in rows)
    assert [r["marks_total"] for r in rows] == [3, 3, 4]


def test_stem_chunks_embedded_into_vec_mark_schemes(db):
    ingest_sample(db)
    chunk_ids = [r["id"] for r in db.execute(
        "SELECT id FROM chunks WHERE chunk_id LIKE '%-stem'"
    ).fetchall()]
    placeholders = ",".join("?" * len(chunk_ids))
    indexed = db.execute(
        f"SELECT COUNT(*) FROM vec_mark_schemes WHERE rowid IN ({placeholders})",
        chunk_ids,
    ).fetchone()[0]
    assert indexed == 3


def test_unmatched_question_goes_to_review_queue(db):
    text = (
        "PAPER: 2\nSESSION: June\nYEAR: 2024\n\n"
        "QUESTION 1\nXyzzy plugh frobnicate quux wibble zorp grault.\n"
        "ANSWER:\n- garply waldo fred plugh\n"
    )
    counts = isol.new_counts()
    isol.ingest_solution_text(
        db, text=text, subject_id=SUBJECT, source_file="unmatched.txt",
        objectives=objectives(db), counts=counts, embed_fn=fake_embed,
        content_hash="hash-unmatched",
    )
    db.commit()

    assert counts["questions"] == 0
    assert counts["queued_no_objective"] == 1
    assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM mark_points").fetchone()[0] == 0
    queued = db.execute("SELECT reason FROM ingest_review_queue").fetchall()
    assert [r["reason"] for r in queued] == ["no_objective_match"]


def test_question_without_bullets_is_queued_not_stored(db):
    text = (
        "PAPER: 2\nSESSION: June\nYEAR: 2024\n\n"
        "QUESTION 1\nExplain the functions of the entrepreneur in a business.\n"
        "ANSWER:\nThe entrepreneur organises production and bears risk.\n"
    )
    counts = isol.new_counts()
    isol.ingest_solution_text(
        db, text=text, subject_id=SUBJECT, source_file="prose.txt",
        objectives=objectives(db), counts=counts, embed_fn=fake_embed,
        content_hash="hash-prose",
    )
    db.commit()

    assert counts["questions"] == 0
    assert counts["queued_no_points"] == 1
    assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    row = db.execute(
        "SELECT reason, objective_id FROM ingest_review_queue"
    ).fetchone()
    assert row["reason"] == "no_mark_points"
    assert row["objective_id"] == "POB-1.1"   # objective resolved, just no bullets


def test_missing_header_is_queued(db):
    counts = isol.new_counts()
    meta = isol.ingest_solution_text(
        db, text="QUESTION 1\nno header here\nANSWER:\n- a point\n",
        subject_id=SUBJECT, source_file="bad.txt",
        objectives=objectives(db), counts=counts, embed_fn=fake_embed,
        content_hash="hash-bad",
    )
    db.commit()
    assert meta is None
    assert counts["files"] == 0
    row = db.execute("SELECT reason FROM ingest_review_queue").fetchone()
    assert row["reason"] == "no_header"


def test_locked_subject_gate_reused(db):
    isol.assert_subject_locked(db, SUBJECT)        # no SystemExit
    with pytest.raises(SystemExit):
        isol.assert_subject_locked(db, "Nonexistent_Subject")
