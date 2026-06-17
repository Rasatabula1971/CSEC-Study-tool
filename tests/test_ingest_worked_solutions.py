"""
tests/test_ingest_worked_solutions.py
=====================================
Tests for backend/ingest_worked_solutions.py -- the Macmillan POB Worked
Solutions ingester.

The real 144-page book is too large for CI. The page text is supplied via a tiny
FakeDoc that mirrors fitz's doc[i].get_text() / .page_count -- this is used
INSTEAD of a rendered fitz PDF on purpose: PyMuPDF's base-14 fonts cannot encode
the book's bullet character U+2022 ('•' extracts back as '?'), so a synthetic
rendered PDF would drop the exact character the parser keys on. The FakeDoc
reproduces real get_text() output (bullets intact), keeping the test offline and
deterministic. One fake Paper 02 "year" section (pages 1-2) is built in the same
shape as the real book (bare-integer question headers, '•' bullets, a
'one mark each (N marks)' rule).

The topic table is supplied to ingest_book() directly as ref_to_topic
(parse_topic_table/find_tables is validated against the real book in the dry-run
stage); the pure table-decoding helpers are unit-tested here.

A deterministic bag-of-words embedder stands in for ollama_embed, so no Ollama
and no network are needed.

Run: pytest tests/test_ingest_worked_solutions.py -v
"""

import hashlib
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import backend.ingest_worked_solutions as iws  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"
EMBED_DIM = 768


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------
def fake_embed(text: str) -> list[float]:
    """Deterministic binary bag-of-words embedding (no Ollama, seed-independent).

    Each content token (len > 2) sets one dimension via a fixed md5 hash, so two
    texts that share tokens have a positive cosine and identical token sets give
    cosine 1.0 -- enough to make the right objective win within a section.
    """
    v = [0.0] * EMBED_DIM
    for tok in {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2}:
        v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % EMBED_DIM] = 1.0
    return v


def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping worked-solutions tests")
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


def seed_subject(db: sqlite3.Connection, locked: int = 1) -> None:
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, ?)",
        ("Principles_of_Business", "Principles of Business", locked),
    )
    sections = [
        ("POB-SEC-5", "PRODUCTION", "5"),
        ("POB-SEC-6", "MARKETING", "6"),
    ]
    for sid, title, num in sections:
        db.execute(
            "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
            "VALUES (?, ?, ?, ?)",
            (sid, "Principles_of_Business", title, num),
        )
    objectives = [
        ("POB-5.1", "POB-SEC-5", "5.1",
         "Outline the functions of the production department manufacturing goods"),
        ("POB-5.2", "POB-SEC-5", "5.2",
         "Describe controlling inventories of raw materials and stock"),
        ("POB-6.1", "POB-SEC-6", "6.1",
         "Explain the functions of the marketing department advertising promotion"),
    ]
    for oid, sid, num, stmt in objectives:
        db.execute(
            "INSERT INTO objectives (objective_id, section_id, subject_id, "
            "objective_num, content_stmt, skill_type) VALUES (?, ?, ?, ?, ?, ?)",
            (oid, sid, "Principles_of_Business", num, stmt, "Understanding"),
        )
    db.commit()


# Page text for the synthetic 2-page "2099" Paper 02 section.
PAGE1_LINES = [
    " 1 ",
    "(a) The functions of the production department manufacturing goods are:",
    "• manufacturing goods from design to completed product",
    "• organising production within a certain timeframe",
    "TWO points, one mark each",
    "(2 marks)",
    "(b) The functions of the marketing department advertising promotion are:",
    "• advertising and sales promotion",
    "• market research and sales forecasting",
    "TWO points, two marks each",
    "(4 marks)",
]
PAGE2_LINES = [
    " 2 ",
    "(a) Discuss zzzqqq wibble entirely unrelated content here:",
    "• wibble alpha",
    "• wibble beta",
    "TWO points, one mark each",
    "(2 marks)",
]


class FakePage:
    def __init__(self, text): self._text = text
    def get_text(self, *a, **k): return self._text


class FakeDoc:
    """Mirrors the slice of the fitz API ingest_book/parse_year_section use."""
    def __init__(self, pages_lines):
        self.pages = ["\n".join(lines) for lines in pages_lines]
        self.page_count = len(self.pages)

    def __getitem__(self, i): return FakePage(self.pages[i])


def make_doc(pages_lines=(PAGE1_LINES, PAGE2_LINES)) -> FakeDoc:
    return FakeDoc(pages_lines)


# Topic mapping for the synthetic section. (2099,2,'a') is intentionally absent.
REF_TO_TOPIC = {
    (2099, 1, "a"): "Production",
    (2099, 1, "b"): "Marketing",
}
LAYOUT = {2099: (1, 2)}


def run_ingest(db, doc, *, dry_run=False, min_similarity=0.05):
    return iws.ingest_book(
        db, doc, subject_id="Principles_of_Business",
        source_file="03_MARK_SCHEMES/ws.pdf", ref_to_topic=REF_TO_TOPIC,
        layout=LAYOUT, embed_fn=fake_embed, dry_run=dry_run,
        min_similarity=min_similarity,
    )


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------
def test_normalize_ligatures():
    # fi (U+FB01) and fl (U+FB02) must fold to ASCII before parsing.
    assert iws.normalize("ﬁnance") == "finance"
    assert iws.normalize("workﬂow") == "workflow"


def test_expand_paper02_cell_multi_group():
    assert iws.expand_paper02_cell("4a, b, c, d 5b, c, d, e, f") == [
        (4, "a"), (4, "b"), (4, "c"), (4, "d"),
        (5, "b"), (5, "c"), (5, "d"), (5, "e"), (5, "f"),
    ]
    assert iws.expand_paper02_cell("1c, 2a") == [(1, "c"), (2, "a")]
    assert iws.expand_paper02_cell("8d, e") == [(8, "d"), (8, "e")]
    assert iws.expand_paper02_cell("") == []
    assert iws.expand_paper02_cell("5a") == [(5, "a")]


def test_match_topic_to_section_and_old_syllabus_drift():
    sections = [
        {"section_id": "POB-SEC-9", "title": "ROLE OF GOVERNMENT IN AN ECONOMY"},
        {"section_id": "POB-SEC-5", "title": "PRODUCTION"},
        {"section_id": "POB-SEC-10", "title": "TECHNOLOGY AND ENVIRONMENT"},
    ]
    # 'in the Economy' vs 'IN AN ECONOMY' both reduce to {role, government, economy}.
    assert iws.match_topic_to_section(
        "Role of Government in the Economy", sections)["section_id"] == "POB-SEC-9"
    assert iws.match_topic_to_section("Production", sections)["section_id"] == "POB-SEC-5"
    # Topics that left the syllabus map to nothing.
    assert iws.match_topic_to_section("Social Accounting and Global Trade", sections) is None
    assert iws.match_topic_to_section(
        "Regional and Global Business Environment", sections) is None


def test_parse_marks_value():
    assert iws._parse_marks_value("TWO points, one mark award for each point") == 1
    assert iws._parse_marks_value("Any TWO points explained, two marks each") == 2
    assert iws._parse_marks_value("no number here") == 1


def test_parse_year_section_finds_leaves_and_labels():
    leaves = iws.parse_year_section(make_doc(), 2099, 1, 2)
    labels = {lf["label"] for lf in leaves}
    assert labels == {"1(a)", "1(b)", "2(a)"}
    by_label = {lf["label"]: lf for lf in leaves}
    assert by_label["1(a)"]["bullets"] == [
        "manufacturing goods from design to completed product",
        "organising production within a certain timeframe",
    ]
    # Mark-rule parsing: 1(b) says "two marks each".
    assert by_label["1(b)"]["marks_value"] == 2
    assert by_label["1(a)"]["marks_value"] == 1


def test_parse_year_section_sub_part_labels():
    """Leaf inheritance + roman sub labelling, via a fake doc (no glyph round-trip)."""
    class FakePage:
        def __init__(self, t): self._t = t
        def get_text(self, *a, **k): return self._t

    class FakeDoc:
        def __init__(self, pages): self.pages = pages; self.page_count = len(pages)
        def __getitem__(self, i): return FakePage(self.pages[i])

    text = "\n".join([
        " 1 ",
        "(a) Characteristics of leaders:",
        "    (i) Democratic leaders:",
        "• take the initiative",
        "• ensure fair treatment",
        "   (ii) Autocratic leaders:",
        "• see authority as the right to manage",
        "TWO points each style, two marks each",
        "(8 marks)",
    ])
    leaves = iws.parse_year_section(FakeDoc([text]), 2099, 1, 1)
    labels = [lf["label"] for lf in leaves]
    assert labels == ["1(a)(i)", "1(a)(ii)"]
    assert leaves[0]["part"] == "a" and leaves[0]["sub"] == "i"


# ---------------------------------------------------------------------------
# Integration tests (full ingest into an in-memory DB)
# ---------------------------------------------------------------------------
def test_documents_row_written_as_mark_scheme():
    db = open_test_db()
    seed_subject(db)
    run_ingest(db, make_doc())
    row = db.execute(
        "SELECT doc_id, content_type, paper, year FROM documents WHERE doc_id = 'POB-WS-2099'"
    ).fetchone()
    assert row is not None
    assert row["content_type"] == "mark_scheme"
    assert row["paper"] == "Paper_02"
    assert row["year"] == 2099


def test_mark_points_and_objective_fk():
    db = open_test_db()
    seed_subject(db)
    run_ingest(db, make_doc())

    # 1(a) -> Production section -> best objective POB-5.1; ids follow the convention.
    mp = db.execute(
        "SELECT mark_point_id, objective_id, question_id, point_text, point_order "
        "FROM mark_points WHERE mark_point_id = 'POB-WS-2099-q1a-mp1'"
    ).fetchone()
    assert mp is not None
    assert mp["objective_id"] == "POB-5.1"
    assert mp["question_id"] == "POB-WS-2099-q1a-stem"
    assert mp["point_order"] == 1

    # 1(b) -> Marketing section -> POB-6.1.
    mp_b = db.execute(
        "SELECT objective_id FROM mark_points WHERE mark_point_id = 'POB-WS-2099-q1b-mp1'"
    ).fetchone()
    assert mp_b["objective_id"] == "POB-6.1"

    # Every mark_point FK resolves to a real objective (Rule 1).
    orphans = db.execute(
        "SELECT COUNT(*) n FROM mark_points m "
        "LEFT JOIN objectives o ON o.objective_id = m.objective_id "
        "WHERE o.objective_id IS NULL"
    ).fetchone()["n"]
    assert orphans == 0
    # Two bullets per matched leaf -> four mark_points for 1(a)+1(b).
    assert db.execute("SELECT COUNT(*) n FROM mark_points").fetchone()["n"] == 4


def test_stem_chunk_indexed_in_vec():
    db = open_test_db()
    seed_subject(db)
    run_ingest(db, make_doc())
    chunk = db.execute(
        "SELECT id, objective_id, question_num FROM chunks "
        "WHERE chunk_id = 'POB-WS-2099-q1a-stem'"
    ).fetchone()
    assert chunk is not None
    assert chunk["objective_id"] == "POB-5.1"
    assert chunk["question_num"] == "1(a)"
    # The stem rowid is present in vec_mark_schemes.
    indexed = db.execute(
        "SELECT COUNT(*) n FROM vec_mark_schemes WHERE rowid = ?", (chunk["id"],)
    ).fetchone()["n"]
    assert indexed == 1


def test_unmapped_question_goes_to_review_queue():
    db = open_test_db()
    seed_subject(db)
    run_ingest(db, make_doc())
    # 2(a) has no topic-table entry -> queued, never written as a mark_point.
    q = db.execute(
        "SELECT reason FROM ingest_review_queue WHERE reason = 'topic_mapping_failed'"
    ).fetchall()
    assert len(q) == 1
    assert db.execute(
        "SELECT COUNT(*) n FROM mark_points WHERE mark_point_id LIKE 'POB-WS-2099-q2a%'"
    ).fetchone()["n"] == 0


def test_idempotent_second_run_writes_nothing_new():
    db = open_test_db()
    seed_subject(db)
    doc = make_doc()
    run_ingest(db, doc)
    before = {
        "docs": db.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"],
        "chunks": db.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"],
        "mp": db.execute("SELECT COUNT(*) n FROM mark_points").fetchone()["n"],
        "vec": db.execute("SELECT COUNT(*) n FROM vec_mark_schemes").fetchone()["n"],
        "queue": db.execute("SELECT COUNT(*) n FROM ingest_review_queue").fetchone()["n"],
    }
    counts2 = run_ingest(db, doc)
    after = {
        "docs": db.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"],
        "chunks": db.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"],
        "mp": db.execute("SELECT COUNT(*) n FROM mark_points").fetchone()["n"],
        "vec": db.execute("SELECT COUNT(*) n FROM vec_mark_schemes").fetchone()["n"],
    }
    assert before["docs"] == after["docs"]
    assert before["chunks"] == after["chunks"]
    assert before["mp"] == after["mp"]
    assert before["vec"] == after["vec"]
    # The doc already exists, so the second run reports it skipped.
    assert counts2["skipped_existing"] == 1


def test_dry_run_writes_nothing():
    db = open_test_db()
    seed_subject(db)
    counts = run_ingest(db, make_doc(), dry_run=True)
    assert db.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) n FROM mark_points").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) n FROM vec_mark_schemes").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) n FROM ingest_review_queue").fetchone()["n"] == 0
    # But it still reports what it WOULD do.
    assert counts["leaf_parts"] == 3
    assert counts["mark_points"] == 4
    assert counts["matched"] == 2


def test_low_confidence_match_queued_at_threshold():
    """A valid section but low objective overlap -> low_confidence_match queue."""
    db = open_test_db()
    seed_subject(db)
    # Map 1(a) to a real section (Marketing) but its production text shares too
    # few tokens with the marketing objective to clear the default 0.60 threshold.
    ref = {(2099, 1, "a"): "Marketing"}
    counts = iws.ingest_book(
        db, make_doc(), subject_id="Principles_of_Business",
        source_file="03_MARK_SCHEMES/ws.pdf", ref_to_topic=ref,
        layout={2099: (1, 1)}, embed_fn=fake_embed, dry_run=False,
    )
    # 1(a) production text vs marketing objective: best sim < 0.60 -> queued.
    row = db.execute(
        "SELECT chunk_text FROM ingest_review_queue WHERE reason = 'low_confidence_match'"
    ).fetchone()
    assert row is not None
    assert "Top candidates" in row["chunk_text"]
    assert counts["queued_low_conf"] >= 1
