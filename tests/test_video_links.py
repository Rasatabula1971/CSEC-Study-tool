"""
tests/test_video_links.py
=========================
Stage V1 tests for backend/load_video_links.py.

All five tests use an in-memory SQLite DB (full schema + sqlite-vec) and a
temporary directory containing a minimal fake CSV — no network, no SSD, no Ollama.

Tests:
  1. load_videos writes a row for a resolvable OK row
  2. dry_run reports the count but writes nothing
  3. idempotent re-run: loading twice leaves one row, not two
  4. unlocked subject is skipped entirely
  5. resolver strips trailing "; and" and resolves the objective
"""

import csv
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from backend.load_video_links import load_videos, resolve_objective_id  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"

SUBJECT = "Economics"
OBJECTIVE = "ECON-1.1"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed")
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    for stmt in sql.split(";"):
        if stmt.strip():
            db.execute(stmt)
    db.commit()
    return db


def seed(db: sqlite3.Connection) -> None:
    """One locked subject + one objective."""
    db.execute(
        "INSERT OR IGNORE INTO subjects (subject_id, display_name, syllabus_locked) "
        "VALUES (?, ?, 1)",
        (SUBJECT, "Economics"),
    )
    db.execute(
        "INSERT OR IGNORE INTO syllabus_sections "
        "(section_id, subject_id, title, section_num) VALUES (?, ?, ?, ?)",
        ("ECON-SEC-1", SUBJECT, "Introduction to Economics", "1"),
    )
    db.execute(
        "INSERT OR IGNORE INTO objectives "
        "(objective_id, section_id, subject_id, objective_num, content_stmt) "
        "VALUES (?, ?, ?, ?, ?)",
        (OBJECTIVE, "ECON-SEC-1", SUBJECT, "1",
         'define the term "economics"'),
    )
    db.commit()


def write_csv(directory: Path, prefix: str, rows: list[dict]) -> Path:
    """Write a minimal *_final_review.csv into directory."""
    path = directory / f"{prefix}_final_review.csv"
    fieldnames = [
        "source_objective_text", "matched_objective_id", "matched_content_stmt",
        "similarity_score", "video_title", "url", "channel", "likes", "views",
        "duration", "flag",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


GOOD_ROW = {
    "source_objective_text": 'define the term "economics"',
    "matched_objective_id": "ECO-001",
    "matched_content_stmt": 'define the term "economics"',
    "similarity_score": "1.0",
    "video_title": "Intro to Economics: Crash Course Econ #1",
    "url": "https://www.youtube.com/watch?v=3ez10ADR_gM",
    "channel": "CrashCourse",
    "likes": "123228",
    "views": "8859538",
    "duration": "12:09",
    "flag": "OK",
}


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_load_writes_row(tmp_path):
    """A resolvable OK row is inserted into objective_videos."""
    db = open_test_db()
    seed(db)
    write_csv(tmp_path, "eco", [GOOD_ROW])

    stats = load_videos(db, pipeline_dir=tmp_path, subject_filter=SUBJECT)

    assert stats[SUBJECT]["loaded"] == 1
    assert stats[SUBJECT]["unresolved"] == 0
    row = db.execute(
        "SELECT * FROM objective_videos WHERE objective_id = ?", (OBJECTIVE,)
    ).fetchone()
    assert row is not None
    assert row["url"] == GOOD_ROW["url"]
    assert row["title"] == GOOD_ROW["video_title"]
    assert row["channel"] == "CrashCourse"
    assert row["duration_str"] == "12:09"


def test_dry_run_writes_nothing(tmp_path):
    """dry_run=True counts the row but makes no DB write."""
    db = open_test_db()
    seed(db)
    write_csv(tmp_path, "eco", [GOOD_ROW])

    stats = load_videos(db, pipeline_dir=tmp_path, subject_filter=SUBJECT, dry_run=True)

    assert stats[SUBJECT]["loaded"] == 1
    count = db.execute("SELECT count(*) FROM objective_videos").fetchone()[0]
    assert count == 0


def test_idempotent_rerun(tmp_path):
    """Loading the same CSV twice produces one row, not two."""
    db = open_test_db()
    seed(db)
    write_csv(tmp_path, "eco", [GOOD_ROW])

    load_videos(db, pipeline_dir=tmp_path, subject_filter=SUBJECT)
    stats2 = load_videos(db, pipeline_dir=tmp_path, subject_filter=SUBJECT)

    assert stats2[SUBJECT]["already_present"] == 1
    assert stats2[SUBJECT]["loaded"] == 0
    count = db.execute("SELECT count(*) FROM objective_videos").fetchone()[0]
    assert count == 1


def test_unlocked_subject_skipped(tmp_path):
    """A subject with syllabus_locked=0 is skipped entirely."""
    db = open_test_db()
    # Insert subject as UNLOCKED
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 0)",
        ("Mathematics", "Mathematics"),
    )
    db.commit()

    mat_row = dict(GOOD_ROW, matched_content_stmt="distinguish among sets of numbers",
                   flag="OK", url="https://www.youtube.com/watch?v=abc")
    write_csv(tmp_path, "mat", [mat_row])

    stats = load_videos(db, pipeline_dir=tmp_path, subject_filter="Mathematics")

    # Subject is not even in stats (skipped before any CSV processing)
    assert stats.get("Mathematics", {}).get("loaded", 0) == 0
    count = db.execute("SELECT count(*) FROM objective_videos").fetchone()[0]
    assert count == 0


def test_resolver_strips_trailing_and():
    """resolve_objective_id handles CSV stmts with a trailing '; and' suffix."""
    stmt_map = {
        'compare growth patterns of males and females': 'INTSCI-1.3.7',
    }
    # CSV has a truncated form with trailing "; and"
    result = resolve_objective_id(
        "compare growth patterns of males and females; and",
        stmt_map,
    )
    assert result == "INTSCI-1.3.7"
