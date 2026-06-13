"""
tests/test_syllabus.py
======================
Stage 2 tests: PDF-text parsing (extract_syllabus) and CSV->DB loading
(syllabus_parser). Uses an in-memory SQLite database — no SSD or .env required.

Run: pytest tests/test_syllabus.py -v
"""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_DIR = REPO_ROOT / "backend" / "db"
SCHEMA_PATH = DB_DIR / "schema.sql"


def _load_module(name: str):
    """Import a backend/db/*.py module by path (the dir is not a package)."""
    spec = importlib.util.spec_from_file_location(name, DB_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


extract = _load_module("extract_syllabus")
parser = _load_module("syllabus_parser")


# ---------------------------------------------------------------------------
# In-memory DB fixture
# ---------------------------------------------------------------------------

def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed — skipping syllabus tests")
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


@pytest.fixture
def db():
    conn = open_test_db()
    yield conn
    conn.close()


SAMPLE_TEXT = """\
SECTION 1: THE NATURE OF BUSINESS

Students should be able to:

1. define the concept of a business.
2. explain the functions of a business and how
   they interrelate.
3. prepare a simple business plan.

CONTENT
- some content column text that must be ignored

SECTION 2: FORMS OF BUSINESS ORGANISATION

Students should be able to:

1. identify the types of business organisation.
2. compare sole traders and partnerships.
"""


# ---------------------------------------------------------------------------
# extract_syllabus: verb -> skill_type / exam_weight
# ---------------------------------------------------------------------------

def test_infer_skill_type_levels():
    assert extract.infer_skill_type("define a business")[0] == "Knowledge"
    assert extract.infer_skill_type("explain the functions")[0] == "Understanding"
    assert extract.infer_skill_type("prepare a plan")[0] == "Application"
    assert extract.infer_skill_type("compare two things")[0] == "Application"


def test_infer_skill_type_returns_command_word():
    skill, cmd = extract.infer_skill_type("Explain the concept")
    assert skill == "Understanding"
    assert cmd == "Explain"


def test_exam_weight_p2_for_construct_prepare_apply():
    assert extract.infer_exam_weight("construct") == "P2"
    assert extract.infer_exam_weight("Prepare") == "P2"
    assert extract.infer_exam_weight("apply") == "P2"


def test_exam_weight_p1_for_pure_recall():
    for verb in ("identify", "list", "state", "define", "name", "classify"):
        assert extract.infer_exam_weight(verb) == "P1", verb


def test_exam_weight_both_otherwise():
    assert extract.infer_exam_weight("explain") == "Both"
    assert extract.infer_exam_weight("compare") == "Both"
    assert extract.infer_exam_weight("") == "Both"


# ---------------------------------------------------------------------------
# extract_syllabus: full parse of sample text
# ---------------------------------------------------------------------------

def test_parse_counts_sections_and_objectives():
    rows = extract.parse(SAMPLE_TEXT, "POB")
    sections = {r["section_num"] for r in rows}
    assert sections == {"1", "2"}
    assert len(rows) == 5


def test_parse_builds_ids_and_titles():
    rows = extract.parse(SAMPLE_TEXT, "POB")
    first = rows[0]
    assert first["objective_id"] == "POB-1.1"
    assert first["section_id"] == "POB-SEC-1"
    # Section titles are preserved verbatim from the PDF (CXC prints them upper-case);
    # the extractor does not re-case them, to avoid mangling acronyms.
    assert first["section_title"] == "THE NATURE OF BUSINESS"
    assert first["objective_num"] == "1.1"


def test_parse_assigns_skill_and_weight():
    rows = extract.parse(SAMPLE_TEXT, "POB")
    by_id = {r["objective_id"]: r for r in rows}
    # define -> Knowledge / P1
    assert by_id["POB-1.1"]["skill_type"] == "Knowledge"
    assert by_id["POB-1.1"]["exam_weight"] == "P1"
    # prepare -> Application / P2
    assert by_id["POB-1.3"]["skill_type"] == "Application"
    assert by_id["POB-1.3"]["exam_weight"] == "P2"
    # compare -> Application / Both
    assert by_id["POB-2.2"]["skill_type"] == "Application"
    assert by_id["POB-2.2"]["exam_weight"] == "Both"


def test_parse_joins_wrapped_continuation_line():
    rows = extract.parse(SAMPLE_TEXT, "POB")
    obj2 = next(r for r in rows if r["objective_id"] == "POB-1.2")
    assert "interrelate" in obj2["content_stmt"]
    assert "\n" not in obj2["content_stmt"]


def test_parse_ignores_content_column_after_stop_heading():
    rows = extract.parse(SAMPLE_TEXT, "POB")
    # Nothing from the CONTENT block should leak in as an objective.
    assert all("content column" not in r["content_stmt"].lower() for r in rows)


# ---------------------------------------------------------------------------
# syllabus_parser: helpers
# ---------------------------------------------------------------------------

def test_command_words_to_json():
    assert parser.command_words_to_json("Describe|State") == '["Describe", "State"]'
    assert parser.command_words_to_json("Explain") == '["Explain"]'
    assert parser.command_words_to_json("") == "[]"
    assert parser.command_words_to_json("Define, Name") == '["Define", "Name"]'


def test_display_name():
    assert parser.display_name("Principles_of_Business") == "Principles of Business"


# ---------------------------------------------------------------------------
# syllabus_parser: insert into DB
# ---------------------------------------------------------------------------

def _rows_from_sample():
    return extract.parse(SAMPLE_TEXT, "POB")


def test_insert_syllabus_inserts_everything(db):
    rows = _rows_from_sample()
    subj_n, sec_n, obj_n = parser.insert_syllabus(db, "Principles_of_Business", rows)
    assert subj_n == 1
    assert sec_n == 2
    assert obj_n == 5

    assert db.execute("SELECT COUNT(*) FROM subjects").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM syllabus_sections").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM objectives").fetchone()[0] == 5


def test_insert_syllabus_is_idempotent(db):
    rows = _rows_from_sample()
    parser.insert_syllabus(db, "Principles_of_Business", rows)
    subj_n, sec_n, obj_n = parser.insert_syllabus(db, "Principles_of_Business", rows)
    # Second run inserts nothing new.
    assert (subj_n, sec_n, obj_n) == (0, 0, 0)
    assert db.execute("SELECT COUNT(*) FROM objectives").fetchone()[0] == 5


def test_insert_syllabus_stores_command_words_as_json(db):
    rows = _rows_from_sample()
    parser.insert_syllabus(db, "Principles_of_Business", rows)
    cw = db.execute(
        "SELECT command_words FROM objectives WHERE objective_id = 'POB-1.1'"
    ).fetchone()[0]
    assert json.loads(cw) == ["Define"]


def test_insert_syllabus_fk_integrity(db):
    rows = _rows_from_sample()
    parser.insert_syllabus(db, "Principles_of_Business", rows)
    # Every objective resolves to a real section and subject (the Rule 1 guarantee).
    orphans = db.execute(
        """
        SELECT COUNT(*) FROM objectives o
        LEFT JOIN syllabus_sections s ON s.section_id = o.section_id
        WHERE s.section_id IS NULL
        """
    ).fetchone()[0]
    assert orphans == 0


def test_subject_starts_unlocked(db):
    rows = _rows_from_sample()
    parser.insert_syllabus(db, "Principles_of_Business", rows)
    locked = db.execute(
        "SELECT syllabus_locked FROM subjects WHERE subject_id = 'Principles_of_Business'"
    ).fetchone()[0]
    assert locked == 0
