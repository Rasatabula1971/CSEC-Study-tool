"""
tests/test_lock_econ_specimen_stems.py
========================================
Tests for tools/lock_econ_specimen_stems.py -- the lock step that replaces
the fabricated page=NULL Economics specimen stem chunks with the reviewed,
page-tagged real question text.

Fixture CSV rows:
  - "ECON-qb1(a)(i)v1-stem" -- UPDATE case: a chunks row already exists
    under this id (page=NULL, fabricated); the CSV row is verified with a
    real page, so it must be UPDATEd in place (same row id, no delete/reinsert).
  - "ECON-qb4(a)v1-stem" -- INSERT case: no chunks row exists yet under this
    id; a mark_points row exists for it, so it must be INSERTed fresh.
  - "ECON-qb2(b)v1-stem" -- verified=0 -- must block the whole run.
  - "ECON-qb3(b)v1-stem" -- blank page -- must block the whole run.

A separate rename-specific test covers the "ECON-qb6(d)v1-stem" ->
"ECON-qb5(d)v1-stem" case documented in
tools/fix_econ_q6_block_realignment.py.
"""

import csv
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from tools.lock_econ_specimen_stems import (
    SUBJECT_ID,
    check_unverified_rows,
    check_bad_page_rows,
    classify_rows,
    apply_changes,
    run_live_verification,
)

_SCHEMA_SQL = (_REPO_ROOT / "backend" / "db" / "schema.sql").read_text()


# ── fixture helpers ────────────────────────────────────────────────────────────

def _make_test_db() -> sqlite3.Connection:
    """In-memory DB with the full schema + minimal Economics data: one
    pre-existing specimen document, one fabricated (page=NULL) chunk, and
    mark_points rows for every question_id this test file references."""
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    for stmt in _SCHEMA_SQL.split(";"):
        s = stmt.strip()
        if s and "VIRTUAL TABLE" not in s.upper():
            db.execute(s)

    try:
        db.execute("ALTER TABLE mark_points ADD COLUMN point_group_id TEXT")
    except sqlite3.OperationalError:
        pass

    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT_ID, SUBJECT_ID),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title) VALUES (?, ?, ?)",
        ("ECON-S1", SUBJECT_ID, "Test Section"),
    )
    for oid in ("ECON-1.6", "ECON-2.3", "ECON-5.2", "ECON-6.1", "ECON-4.4"):
        db.execute(
            "INSERT INTO objectives "
            "(objective_id, section_id, subject_id, objective_num, content_stmt, verified) "
            "VALUES (?, 'ECON-S1', ?, ?, 'Test objective', 1)",
            (oid, SUBJECT_ID, oid.replace("ECON-", "")),
        )

    # Pre-existing specimen document (as created by ingest_econ_specimen_stems.py)
    db.execute(
        """
        INSERT INTO documents
            (doc_id, subject_id, content_type, paper, year, source_file, content_hash)
        VALUES ('specimen-doc-1', ?, 'specimen', 'Specimen Paper - 2016', 2016,
                'fake.pdf', 'hash1')
        """,
        (SUBJECT_ID,),
    )

    # Fabricated (page=NULL) chunk for the UPDATE case
    db.execute(
        """
        INSERT INTO chunks
            (doc_id, objective_id, subject_id, chunk_text, page, question_num, chunk_id)
        VALUES ('specimen-doc-1', 'ECON-1.6', ?, 'FABRICATED Q1(a)(i) text', NULL,
                '1(a)(i)', 'ECON-qb1(a)(i)v1-stem')
        """,
        (SUBJECT_ID,),
    )

    # mark_points rows for every question_id used across the test module
    mark_points_seed = [
        ("mp1", "ECON-1.6", "ECON-qb1(a)(i)v1-stem"),
        ("mp2", "ECON-5.2", "ECON-qb4(a)v1-stem"),
        ("mp3", "ECON-2.3", "ECON-qb2(b)v1-stem"),
        ("mp4", "ECON-6.1", "ECON-qb3(b)v1-stem"),
        ("mp5", "ECON-4.4", "ECON-qb5(d)v1-stem"),
    ]
    for mpid, obj, qid in mark_points_seed:
        db.execute(
            "INSERT INTO mark_points "
            "(mark_point_id, objective_id, question_id, point_text, marks_value, point_order) "
            "VALUES (?, ?, ?, 'Test point', 1, 1)",
            (mpid, obj, qid),
        )

    db.commit()
    return db


def _base_rows() -> list[dict]:
    """Four rows: 1 UPDATE case, 1 INSERT case, 1 verified=0, 1 blank page."""
    return [
        {
            "question_id": "ECON-qb1(a)(i)v1-stem",
            "question_num": "1",
            "question_part": "(a)(i)",
            "page": "73",
            "stem_text": "REAL Q1(a)(i) text from page 73.",
            "verified": "1",
        },
        {
            "question_id": "ECON-qb4(a)v1-stem",
            "question_num": "4",
            "question_part": "(a)",
            "page": "79",
            "stem_text": "REAL Q4(a) text from page 79.",
            "verified": "1",
        },
        {
            "question_id": "ECON-qb2(b)v1-stem",
            "question_num": "2",
            "question_part": "(b)",
            "page": "74",
            "stem_text": "Unreviewed row -- should never be locked.",
            "verified": "0",
        },
        {
            "question_id": "ECON-qb3(b)v1-stem",
            "question_num": "3",
            "question_part": "(b)",
            "page": "",
            "stem_text": "Row with a missing page -- should never be locked.",
            "verified": "1",
        },
    ]


def _write_csv(tmp_path: Path, rows: list[dict]) -> Path:
    csv_path = tmp_path / "Economics_specimen_stems_review.csv"
    fieldnames = ["question_id", "question_num", "question_part", "page", "stem_text", "verified"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    return csv_path


# ── guard tests ────────────────────────────────────────────────────────────────

def test_check_unverified_rows_flags_verified_zero():
    rows = _base_rows()
    unverified = check_unverified_rows(rows)
    assert len(unverified) == 1
    assert unverified[0]["question_id"] == "ECON-qb2(b)v1-stem"


def test_check_bad_page_rows_flags_blank_page():
    rows = _base_rows()
    bad = check_bad_page_rows(rows)
    assert len(bad) == 1
    assert bad[0]["question_id"] == "ECON-qb3(b)v1-stem"


def test_csv_round_trip_matches_guard_expectations(tmp_path):
    """Sanity check that the fixture CSV on disk reproduces the same
    unverified/bad-page rows the in-memory fixtures assert on -- i.e. the
    guard functions operate correctly against a real csv.DictReader row
    shape (all-string values), not just hand-built dicts."""
    csv_path = _write_csv(tmp_path, _base_rows())
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    unverified = check_unverified_rows(rows)
    assert [r["question_id"] for r in unverified] == ["ECON-qb2(b)v1-stem"]

    bad_pages = check_bad_page_rows(rows)
    assert [r["question_id"] for r in bad_pages] == ["ECON-qb3(b)v1-stem"]


# ── classification / write tests (only the 2 valid rows) ──────────────────────

def _valid_rows() -> list[dict]:
    return [r for r in _base_rows() if r["verified"] == "1" and r["page"]]


def test_classify_rows_splits_update_and_insert():
    db = _make_test_db()
    rows = _valid_rows()

    groups = classify_rows(db, rows)

    assert len(groups["update"]) == 1
    assert groups["update"][0][1] == "ECON-qb1(a)(i)v1-stem"

    assert len(groups["insert"]) == 1
    assert groups["insert"][0][0]["question_id"] == "ECON-qb4(a)v1-stem"

    assert groups["rename"] == []
    db.close()


def test_apply_changes_updates_existing_chunk_in_place():
    db = _make_test_db()
    rows = _valid_rows()
    groups = classify_rows(db, rows)

    counts = apply_changes(db, groups)
    assert counts["updated"] == 1
    assert counts["inserted"] == 1
    assert counts["renamed"] == 0
    assert counts["skipped_no_objective"] == 0

    row = db.execute(
        "SELECT page, chunk_text, doc_id FROM chunks WHERE chunk_id = ?",
        ("ECON-qb1(a)(i)v1-stem",),
    ).fetchone()
    assert row["page"] == 73
    assert row["chunk_text"] == "REAL Q1(a)(i) text from page 73."
    # doc_id must be unchanged -- update in place, never delete/reinsert
    assert row["doc_id"] == "specimen-doc-1"

    # Row count for this chunk_id must still be exactly 1 (no duplicate/reinsert)
    count = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE chunk_id = ?", ("ECON-qb1(a)(i)v1-stem",)
    ).fetchone()[0]
    assert count == 1
    db.close()


def test_apply_changes_inserts_new_chunk():
    db = _make_test_db()
    rows = _valid_rows()
    groups = classify_rows(db, rows)
    apply_changes(db, groups)

    row = db.execute(
        "SELECT page, chunk_text, question_num, objective_id, doc_id, subject_id "
        "FROM chunks WHERE chunk_id = ?",
        ("ECON-qb4(a)v1-stem",),
    ).fetchone()
    assert row is not None
    assert row["page"] == 79
    assert row["chunk_text"] == "REAL Q4(a) text from page 79."
    assert row["question_num"] == "4(a)"
    assert row["objective_id"] == "ECON-5.2"
    assert row["doc_id"] == "specimen-doc-1"
    assert row["subject_id"] == SUBJECT_ID
    db.close()


def test_apply_changes_skips_insert_with_no_mark_points():
    """An insert-candidate row whose question_id has no mark_points row is
    skipped (not silently inserted) -- mirrors ingest_econ_specimen_stems.py's
    own WARNING/skip behaviour for the same situation."""
    db = _make_test_db()
    row = {
        "question_id": "ECON-qb9(z)v1-stem",
        "question_num": "9",
        "question_part": "(z)",
        "page": "99",
        "stem_text": "No mark_points exist for this one.",
        "verified": "1",
    }
    groups = classify_rows(db, [row])
    assert len(groups["insert"]) == 1

    counts = apply_changes(db, groups)
    assert counts["inserted"] == 0
    assert counts["skipped_no_objective"] == 1

    exists = db.execute(
        "SELECT 1 FROM chunks WHERE chunk_id = ?", ("ECON-qb9(z)v1-stem",)
    ).fetchone()
    assert exists is None
    db.close()


# ── rename case ────────────────────────────────────────────────────────────────

def test_classify_rows_detects_legacy_id_rename():
    """A chunks row exists under the stale 'ECON-qb6(d)v1-stem' id; the CSV
    row targets 'ECON-qb5(d)v1-stem' -- classify_rows must resolve this as a
    rename, not an insert."""
    db = _make_test_db()
    db.execute(
        """
        INSERT INTO chunks
            (doc_id, objective_id, subject_id, chunk_text, page, question_num, chunk_id)
        VALUES ('specimen-doc-1', 'ECON-4.4', ?, 'FABRICATED Q5(d) text (stale id)', NULL,
                '5(d)', 'ECON-qb6(d)v1-stem')
        """,
        (SUBJECT_ID,),
    )
    db.commit()

    row = {
        "question_id": "ECON-qb5(d)v1-stem",
        "question_num": "5",
        "question_part": "(d)",
        "page": "84",
        "stem_text": "REAL Q5(d) text from page 84.",
        "verified": "1",
    }
    groups = classify_rows(db, [row])
    assert groups["update"] == []
    assert groups["insert"] == []
    assert len(groups["rename"]) == 1
    assert groups["rename"][0][1] == "ECON-qb6(d)v1-stem"
    db.close()


def test_apply_changes_renames_legacy_chunk_id():
    db = _make_test_db()
    db.execute(
        """
        INSERT INTO chunks
            (doc_id, objective_id, subject_id, chunk_text, page, question_num, chunk_id)
        VALUES ('specimen-doc-1', 'ECON-4.4', ?, 'FABRICATED Q5(d) text (stale id)', NULL,
                '5(d)', 'ECON-qb6(d)v1-stem')
        """,
        (SUBJECT_ID,),
    )
    db.commit()

    row = {
        "question_id": "ECON-qb5(d)v1-stem",
        "question_num": "5",
        "question_part": "(d)",
        "page": "84",
        "stem_text": "REAL Q5(d) text from page 84.",
        "verified": "1",
    }
    groups = classify_rows(db, [row])
    counts = apply_changes(db, groups)
    assert counts["renamed"] == 1

    old = db.execute(
        "SELECT 1 FROM chunks WHERE chunk_id = ?", ("ECON-qb6(d)v1-stem",)
    ).fetchone()
    assert old is None, "legacy chunk_id must no longer exist after rename"

    new = db.execute(
        "SELECT page, chunk_text FROM chunks WHERE chunk_id = ?",
        ("ECON-qb5(d)v1-stem",),
    ).fetchone()
    assert new["page"] == 84
    assert new["chunk_text"] == "REAL Q5(d) text from page 84."
    db.close()


# ── live verification ──────────────────────────────────────────────────────────

def test_run_live_verification_reports_zero_null_pages_and_picker_count():
    db = _make_test_db()
    rows = _valid_rows()
    groups = classify_rows(db, rows)
    apply_changes(db, groups)

    report = run_live_verification(db, SUBJECT_ID)
    assert report["null_page_count"] == 0
    # Both the updated (ECON-qb1(a)(i)v1-stem) and inserted (ECON-qb4(a)v1-stem)
    # rows now have real pages and matching mark_points -- both must surface.
    assert report["picker_question_count"] == 2
    db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
