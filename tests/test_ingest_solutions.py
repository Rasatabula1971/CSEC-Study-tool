"""
tests/test_ingest_solutions.py
==============================
Tests for backend/ingest_solutions.py -- the Paper 2 worked-solution ingester
that populates the grader's mark_points (the app's "answer half").

Parsing is exercised on plain text strings; ingestion runs against an in-memory
SQLite DB with the full schema + sqlite-vec. No PyMuPDF and no Ollama are
needed (PDF extraction and embeddings are not touched here).

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

# A realistic two-sub-question solution page (January 2026 Paper 2, Q1a/Q1b).
SAMPLE = """\
Page 286
January 2026 Paper 02
Question 1
1(a) Maria's career choices
Maria wants to pursue business education. List THREE careers in the field of
business that Maria could pursue. (3 marks)
Three careers in the field of business that Maria could pursue are:
•
Accountant - a person who keeps the financial records of a business and gives
financial advice to the owners.
•
Marketing manager - a person who plans and runs the activities to promote and
sell the products of a business.
•
Human resource manager - a person in charge of recruiting and training workers.
1(b) Stakeholders in business activities
List THREE stakeholders involved in business activities. (3 marks)
Three stakeholders involved in business activities are:
•
Owners or shareholders, who provide the capital used to run the business.
•
Employees, who provide their labour in exchange for wages.
•
Customers, who buy the goods and services produced by the business.
"""


def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping solutions ingest tests")
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


def seed_subject(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        ("Principles_of_Business", "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES ('POB-SEC-1', 'Principles_of_Business', 'Nature of Business', '1')",
    )
    for oid, stmt in [
        ("POB-1.14", "Describe the careers in the field of business."),
        ("POB-1.10", "Discuss the role and functions of the stakeholders involved in business activities."),
    ]:
        db.execute(
            "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
            "content_stmt) VALUES (?, 'POB-SEC-1', 'Principles_of_Business', ?, ?)",
            (oid, oid.split("-")[1], stmt),
        )
    db.commit()


@pytest.fixture
def db():
    conn = open_test_db()
    seed_subject(conn)
    yield conn
    conn.close()


def lines_of(text: str) -> list[tuple[int, str]]:
    """Mimic extract_lines() output: every line on page 1."""
    return [(1, ln) for ln in text.splitlines()]


def objectives(db):
    return isol.load_objectives(db, "Principles_of_Business")


# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------
def test_parse_session_january():
    meta = isol.parse_session("January 2026 Paper 02")
    assert meta == {"month_tag": "Jan", "year": 2026, "paper_label": "Paper 2 - January 2026"}


def test_parse_session_mayjune():
    meta = isol.parse_session("May/June 2010 Paper 02")
    assert meta["month_tag"] == "Jun"
    assert meta["year"] == 2010
    assert meta["paper_label"] == "Paper 2 - June 2010"


def test_parse_session_none_when_absent():
    assert isol.parse_session("just some prose with no header") is None


def test_split_points_drops_leadin_and_joins_multiline():
    body = [
        "Three careers ... are:",          # lead-in before first bullet -> dropped
        "•",
        "Accountant - keeps financial",
        "records of a business.",          # continuation joined to the point above
        "•",
        "Marketing manager - promotes products.",
    ]
    pts = isol.split_points(body)
    assert pts == [
        "Accountant - keeps financial records of a business.",
        "Marketing manager - promotes products.",
    ]


def test_parse_subquestions_splits_two_subparts_with_marks():
    subs = isol.parse_subquestions(lines_of(SAMPLE))
    assert [s["label"] for s in subs] == ["1(a)", "1(b)"]
    a = subs[0]
    assert a["question_num"] == 1 and a["sub"] == "a"
    assert a["marks"] == 3
    assert len(a["points"]) == 3
    assert a["points"][0].startswith("Accountant")
    # the stem holds the question prose (ends at the "(3 marks)" line)
    assert "List THREE careers" in a["stem"]
    assert "Accountant" not in a["stem"]


def test_parse_subquestion_with_no_bullets_yields_no_points():
    text = "January 2026 Paper 02\n1(c) Prose answer\nDescribe X. (2 marks)\nProduction is the area that makes goods.\n"
    subs = isol.parse_subquestions(lines_of(text))
    assert len(subs) == 1
    assert subs[0]["points"] == []
    assert subs[0]["marks"] == 2


# ---------------------------------------------------------------------------
# Ingestion against a real (in-memory) schema
# ---------------------------------------------------------------------------
def test_ingest_populates_mark_points_keyed_by_question_id(db):
    counts = isol.new_counts()
    isol.ingest_solution_lines(
        db, lines=lines_of(SAMPLE), subject_id="Principles_of_Business",
        source_file=r"D:\sol\jan2026.pdf", objectives=objectives(db),
        counts=counts, code="POB",
    )
    db.commit()

    # one document, content_type mark_scheme, session disambiguated in `paper`
    doc = db.execute("SELECT content_type, paper, year FROM documents").fetchone()
    assert doc["content_type"] == "mark_scheme"
    assert doc["paper"] == "Paper 2 - January 2026"
    assert doc["year"] == 2026

    # Q1(a) is keyed and gradeable -- exactly what grade.fetch_mark_points needs
    qid = "POB-2026Jan-P2-q1a"
    rows = db.execute(
        "SELECT point_text, objective_id, point_order FROM mark_points "
        "WHERE question_id = ? ORDER BY point_order", (qid,)
    ).fetchall()
    assert len(rows) == 3
    assert rows[0]["point_text"].startswith("Accountant")
    assert rows[0]["objective_id"] == "POB-1.14"        # careers objective
    assert counts["mark_points"] == 6                   # 3 + 3 across both parts

    # stem chunk exists so the question picker can show the prompt
    stem = db.execute(
        "SELECT chunk_text, question_num FROM chunks WHERE chunk_id = ?", (qid + "-stem",)
    ).fetchone()
    assert stem is not None
    assert stem["question_num"] == "1(a)"
    assert "List THREE careers" in stem["chunk_text"]

    # Q1(b) maps to the stakeholders objective
    b = db.execute(
        "SELECT DISTINCT objective_id FROM mark_points WHERE question_id = ?",
        ("POB-2026Jan-P2-q1b",)
    ).fetchone()
    assert b["objective_id"] == "POB-1.10"


def test_ingest_offline_writes_no_vectors(db):
    """Default (embed_fn=None) stores mark points but indexes no embeddings."""
    counts = isol.new_counts()
    isol.ingest_solution_lines(
        db, lines=lines_of(SAMPLE), subject_id="Principles_of_Business",
        source_file=r"D:\sol\jan2026.pdf", objectives=objectives(db),
        counts=counts, code="POB",
    )
    db.commit()
    assert db.execute("SELECT count(*) FROM vec_mark_schemes").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM mark_points").fetchone()[0] == 6


PROSE_SAMPLE = """\
January 2026 Paper 02
Question 1
1(c) Stakeholders in business
Describe the role of the stakeholders involved in business activities. (4 marks)
Owners provide the capital and expect a profit in return. Employees provide their
labour in exchange for wages and depend on the business for job security.
"""


def test_prose_no_bullets_retains_answer_body_and_columns(db):
    """A matched prose (no-bullet) answer is queued with reason
    'prose_answer_no_bullets', the answer body retained, and objective_id/doc_id
    populated on the row -- and produces no mark points."""
    counts = isol.new_counts()
    isol.ingest_solution_lines(
        db, lines=lines_of(PROSE_SAMPLE), subject_id="Principles_of_Business",
        source_file=r"D:\sol\jan2026.pdf", objectives=objectives(db),
        counts=counts, code="POB",
    )
    db.commit()

    assert db.execute("SELECT count(*) FROM mark_points").fetchone()[0] == 0
    q = db.execute(
        "SELECT chunk_text, reason, objective_id, doc_id FROM ingest_review_queue"
    ).fetchone()
    assert q["reason"] == "prose_answer_no_bullets"
    assert q["objective_id"] == "POB-1.10"          # stakeholders objective
    assert q["doc_id"] is not None                   # known doc recorded on the row
    assert "ANSWER:" in q["chunk_text"]
    assert "Describe the role" in q["chunk_text"]    # question stem retained
    assert "Owners provide the capital" in q["chunk_text"]  # prose answer retained
    assert counts["queued_no_points"] == 1


def test_unmatched_subquestion_queued_not_stored(db):
    text = (
        "January 2026 Paper 02\n1(a) Off-syllabus\n"
        "Xyzzy plugh frobnicate quux wibble. (2 marks)\n"
        "•\nzorp blarg wibble frobnicate.\n"
    )
    counts = isol.new_counts()
    isol.ingest_solution_lines(
        db, lines=lines_of(text), subject_id="Principles_of_Business",
        source_file=r"D:\sol\x.pdf", objectives=objectives(db),
        counts=counts, code="POB",
    )
    db.commit()
    assert db.execute("SELECT count(*) FROM mark_points").fetchone()[0] == 0
    q = db.execute("SELECT reason FROM ingest_review_queue").fetchone()
    assert q["reason"] == "no_objective_match"
    assert counts["queued_no_objective"] == 1


def test_no_session_header_queues_whole_doc(db):
    counts = isol.new_counts()
    meta = isol.ingest_solution_lines(
        db, lines=lines_of("a page with no recognizable header\nsome prose"),
        subject_id="Principles_of_Business", source_file=r"D:\sol\bad.pdf",
        objectives=objectives(db), counts=counts, code="POB",
    )
    db.commit()
    assert meta is None
    assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    assert db.execute(
        "SELECT reason FROM ingest_review_queue"
    ).fetchone()["reason"] == "no_session_header"
