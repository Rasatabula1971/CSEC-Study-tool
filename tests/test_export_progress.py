"""
tests/test_export_progress.py
=============================
Tests for backend/export_progress.py -- the parent-facing study-progress Excel
export. Exercises the generation function (fetch_progress + build_workbook)
against an in-memory SQLite DB; the FastAPI endpoint is intentionally NOT tested
here (the spec asks for the generation logic).

study_plan is a runtime-migration table (created in app.py, not schema.sql), so
the fixture creates it alongside the canonical schema.

Run: pytest tests/test_export_progress.py -v
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import backend.export_progress as ep  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"
SUBJECT = "Principles_of_Business"

# Mirrors the study_plan runtime migration in backend/app.py.
STUDY_PLAN_DDL = """
CREATE TABLE IF NOT EXISTS study_plan (
    plan_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
    objective_id  TEXT NOT NULL REFERENCES objectives(objective_id),
    status        TEXT NOT NULL DEFAULT 'unmet',
    met_count     INTEGER NOT NULL DEFAULT 0,
    last_met_at   TEXT,
    created_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(subject_id, objective_id)
)
"""


def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping export-progress tests")
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    for stmt in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        if stmt.strip():
            db.execute(stmt)
    db.execute(STUDY_PLAN_DDL)
    db.commit()
    return db


def seed(db: sqlite3.Connection) -> None:
    """3 objectives: one mastered, one weak (attempted, failing), one untouched."""
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES ('POB-SEC-1', ?, 'Nature of Business', '1')",
        (SUBJECT,),
    )
    for oid, num, stmt in [
        ("POB-1.1", "1.1", "Define the term business."),
        ("POB-1.2", "1.2", "Explain the functions of an entrepreneur."),
        ("POB-1.3", "1.3", "Distinguish between goods and services."),
    ]:
        db.execute(
            "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
            "content_stmt) VALUES (?, 'POB-SEC-1', ?, ?, ?)",
            (oid, SUBJECT, num, stmt),
        )

    # POB-1.1 -> mastered (passed on two days), with a weakness_log entry too.
    db.execute(
        "INSERT INTO study_plan (subject_id, objective_id, status, met_count, last_met_at) "
        "VALUES (?, 'POB-1.1', 'mastered', 2, '2026-06-10')",
        (SUBJECT,),
    )
    db.execute(
        "INSERT INTO weakness_log (objective_id, subject_id, score_pct, leitner_box, next_review) "
        "VALUES ('POB-1.1', ?, 90, 4, '2026-06-20')",
        (SUBJECT,),
    )

    # POB-1.2 -> weak: attempted, failing. unmet status + weakness_log entry.
    db.execute(
        "INSERT INTO study_plan (subject_id, objective_id, status, met_count) "
        "VALUES (?, 'POB-1.2', 'unmet', 0)",
        (SUBJECT,),
    )
    db.execute(
        "INSERT INTO weakness_log (objective_id, subject_id, score_pct, leitner_box, next_review) "
        "VALUES ('POB-1.2', ?, 40, 1, '2026-06-16')",
        (SUBJECT,),
    )

    # POB-1.3 -> untouched: no study_plan row, no weakness_log row.
    db.commit()


@pytest.fixture
def db():
    conn = open_test_db()
    seed(conn)
    yield conn
    conn.close()


def _status_cell(ws, label):
    """Return the Status cell (column 3) of the data row whose Status == label."""
    for row in ws.iter_rows(min_row=3):
        if row[ep.STATUS_COL - 1].value == label:
            return row[ep.STATUS_COL - 1]
    return None


def test_row_count(db):
    rows = ep.fetch_progress(db, SUBJECT)
    assert len(rows) == 3
    wb = ep.build_workbook(rows, SUBJECT)
    ws = wb.active
    # row 1 summary + row 2 header + 3 data rows = 5
    assert ws.max_row == 5


def test_status_labels_and_fills(db):
    wb = ep.build_workbook(ep.fetch_progress(db, SUBJECT), SUBJECT)
    ws = wb.active

    mastered = _status_cell(ws, "Mastered")
    weak = _status_cell(ws, "Needs work")
    untouched = _status_cell(ws, "Not started")
    assert mastered is not None and weak is not None and untouched is not None

    # Mastered row: green fill.
    assert mastered.fill.patternType == "solid"
    assert ep.GREEN in mastered.fill.fgColor.rgb

    # Weak (attempted, failing) row: orange fill.
    assert ep.ORANGE in weak.fill.fgColor.rgb

    # Untouched row: no fill.
    assert untouched.fill.patternType in (None, "none")


def test_summary_row(db):
    wb = ep.build_workbook(ep.fetch_progress(db, SUBJECT), SUBJECT)
    ws = wb.active
    summary = ws["A1"].value
    assert "1/3 (33%)" in summary          # 1 mastered of 3
    assert ws["A1"].font.bold is True


def test_headers_present(db):
    wb = ep.build_workbook(ep.fetch_progress(db, SUBJECT), SUBJECT)
    ws = wb.active
    headers = [c.value for c in ws[2]]
    assert headers == [
        "Section", "Objective", "Status", "Last Score",
        "Leitner Box", "Next Review", "Times Passed",
    ]
    assert ws.freeze_panes == "A3"


def test_export_writes_file(db, tmp_path):
    out = ep.export_progress(db, SUBJECT, str(tmp_path), today="2026-06-15")
    assert out.exists()
    assert out.name == f"{SUBJECT}_progress_2026-06-15.xlsx"
