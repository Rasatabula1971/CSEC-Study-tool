"""
tests/test_lock_mark_scheme.py
================================
Tests for tools/lock_mark_scheme.py — Stage 3 of the mark scheme pipeline.
"""
import sqlite3
import sys
from pathlib import Path

import pytest
import sqlite_vec

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tools"))
sys.path.insert(0, str(_REPO_ROOT / "backend"))

_SCHEMA_PATH = _REPO_ROOT / "backend" / "db" / "schema.sql"

from lock_mark_scheme import (
    _all_objs,
    _obj_num_from_id,
    build_mark_group_id,
    build_mark_point_id,
    build_point_group_id,
    build_question_id,
    check_blocking_rows,
    check_collisions,
    check_null_source_pages,
    check_overlapping_classification,
    check_unmapped_eligible_rows,
    lock_subject,
    partition_rows,
    resolve_paper_pages,
)


# ── fixture helpers ────────────────────────────────────────────────────────────

def _make_db() -> sqlite3.Connection:
    """In-memory DB with the tables lock_subject needs, plus sqlite-vec loaded."""
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
        CREATE TABLE subjects (
            subject_id      TEXT PRIMARY KEY,
            display_name    TEXT NOT NULL,
            syllabus_locked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE syllabus_sections (
            section_id  TEXT PRIMARY KEY,
            subject_id  TEXT NOT NULL REFERENCES subjects(subject_id),
            title       TEXT NOT NULL,
            section_num TEXT
        );
        CREATE TABLE objectives (
            objective_id  TEXT PRIMARY KEY,
            section_id    TEXT NOT NULL REFERENCES syllabus_sections(section_id),
            subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
            objective_num TEXT NOT NULL,
            content_stmt  TEXT NOT NULL
        );
        CREATE TABLE mark_points (
            mark_point_id TEXT PRIMARY KEY,
            objective_id  TEXT NOT NULL REFERENCES objectives(objective_id),
            question_id   TEXT,
            doc_id        TEXT,
            point_text    TEXT NOT NULL,
            marks_value   INTEGER NOT NULL DEFAULT 1,
            point_order   INTEGER,
            point_group_id TEXT,
            mark_group_id TEXT,
            group_max_marks INTEGER
        );
    """)
    # Seed subject + section + objectives used in tests
    db.execute("INSERT INTO subjects VALUES ('Economics','Economics',1)")
    db.execute("INSERT INTO syllabus_sections VALUES ('ECON-S1','Economics','Section 1','1')")
    db.execute("INSERT INTO objectives VALUES ('ECON-1.6','ECON-S1','Economics','1.6','PPC')")
    db.execute("INSERT INTO objectives VALUES ('ECON-1.8','ECON-S1','Economics','1.8','Comparative advantage')")
    db.execute("INSERT INTO objectives VALUES ('ECON-2.2','ECON-S1','Economics','2.2','GDP')")
    db.execute("INSERT INTO objectives VALUES ('ECON-6.9', 'ECON-S1','Economics','6.9','Growth vs development')")
    db.execute("INSERT INTO objectives VALUES ('ECON-6.11','ECON-S1','Economics','6.11','HDI')")
    db.execute("INSERT INTO objectives VALUES ('ECON-6.12','ECON-S1','Economics','6.12','Sustainability')")
    db.commit()
    return db


def _row(block_id, part, occ, order, obj_id,
         point_text="Test point.", marks_value="1",
         verified="1", parser_artifact="0",
         excluded_reason="", needs_manual_entry="0",
         source_page="90", mark_group_id="", group_max_marks="") -> dict:
    """Build a minimal CSV-row dict.

    mark_group_id/group_max_marks default to "" — equivalent to _fld()'s
    behaviour for a column that is entirely ABSENT from the CSV (Economics'
    already-locked review CSVs predate the m021 grouping work and have
    neither column), not just present-but-empty.
    """
    return {
        "question_block_id":  str(block_id),
        "question_part":      part,
        "part_occurrence":    str(occ),
        "point_order":        str(order),
        "mapped_objective_id": obj_id,
        "point_text":         point_text,
        "marks_value":        str(marks_value),
        "verified":           verified,
        "parser_artifact":    parser_artifact,
        "excluded_reason":    excluded_reason,
        "needs_manual_entry": needs_manual_entry,
        "source_page":        source_page,
        "mark_group_id":      mark_group_id,
        "group_max_marks":    group_max_marks,
    }


# ── _all_objs deduplication ───────────────────────────────────────────────────

def test_all_objs_deduplicates_preserving_order():
    """A CSV value with a repeated objective_id must produce exactly one entry for it."""
    result = _all_objs("ECON-4.4,ECON-4.6,ECON-4.6")
    assert result == ["ECON-4.4", "ECON-4.6"]
    assert len(result) == 2


def test_all_objs_single_no_change():
    assert _all_objs("ECON-1.6") == ["ECON-1.6"]


def test_all_objs_multiple_no_dupes():
    assert _all_objs("ECON-6.9,ECON-6.11,ECON-6.12") == ["ECON-6.9", "ECON-6.11", "ECON-6.12"]


# ── formula unit tests ─────────────────────────────────────────────────────────

def test_mark_point_id_formula():
    mpid = build_mark_point_id("ECON", "1.6", "1", "(a)(i)", "1", "2")
    assert mpid == "ECON-1.6-qb1(a)(i)v1-mp2"


def test_question_id_formula():
    qid = build_question_id("ECON", "3", "(b)", "2")
    assert qid == "ECON-qb3(b)v2"


def test_obj_num_strips_prefix():
    assert _obj_num_from_id("ECON-6.11", "ECON") == "6.11"


def test_obj_num_raises_on_wrong_prefix():
    with pytest.raises(ValueError, match="does not start with"):
        _obj_num_from_id("POB-1.2", "ECON")


# ── partition / block checks ───────────────────────────────────────────────────

def test_partition_separates_all_categories():
    rows = [
        _row(1, "(a)", 1, 1, "ECON-1.6"),                              # eligible
        _row(2, "(a)", 1, 1, "ECON-1.6", parser_artifact="1"),         # artifact
        _row(3, "(a)", 1, 1, "",         excluded_reason="out_of_scope"),  # excluded
        _row(4, "(a)", 1, 1, "ECON-1.8", needs_manual_entry="1"),      # manual
    ]
    eligible, counts = partition_rows(rows)
    assert len(eligible) == 1
    assert counts == {"artifact": 1, "excluded": 1, "manual": 1}


def test_check_blocking_rows_catches_unreviewed():
    rows = [
        _row(1, "(a)", 1, 1, "ECON-1.6", verified="1"),   # fine
        _row(2, "(a)", 1, 1, "ECON-1.6", verified="0"),   # BLOCKS
    ]
    blocking = check_blocking_rows(rows)
    assert len(blocking) == 1
    assert blocking[0]["question_block_id"] == "2"


def test_check_blocking_rows_skips_artifact_and_excluded():
    rows = [
        _row(1, "(a)", 1, 1, "ECON-1.6", verified="0", parser_artifact="1"),
        _row(2, "(a)", 1, 1, "ECON-1.6", verified="0", excluded_reason="dup"),
        _row(3, "(a)", 1, 1, "ECON-1.6", verified="0", needs_manual_entry="1"),
    ]
    # None of these should block — they're appropriately flagged
    assert check_blocking_rows(rows) == []


# ── collision detection ────────────────────────────────────────────────────────

def test_collision_detected_and_db_untouched():
    """Collision check must fire before any write; DB must remain empty."""
    db = _make_db()

    rows = [
        _row(1, "(a)", 1, 1, "ECON-1.6", "First point"),
        _row(1, "(a)", 1, 1, "ECON-1.6", "Duplicate position"),  # same key
    ]
    eligible, _ = partition_rows(rows)

    dupes = check_collisions(eligible, "ECON")
    assert len(dupes) == 1
    # Collisions are now keyed by point_group_id (no per-objective prefix/num)
    expected_pgid = build_point_group_id("ECON", "1", "(a)", "1", "1")
    assert expected_pgid in dupes
    assert len(dupes[expected_pgid]) == 2  # two eligible-list indices

    # Because dupes were found, lock_subject was never called — DB untouched
    count = db.execute("SELECT COUNT(*) FROM mark_points").fetchone()[0]
    assert count == 0


def test_distinct_positions_no_collision():
    rows = [
        _row(1, "(a)", 1, 1, "ECON-1.6"),
        _row(1, "(a)", 1, 2, "ECON-1.6"),   # different point_order
        _row(1, "(b)", 1, 1, "ECON-1.8"),   # different part
    ]
    eligible, _ = partition_rows(rows)
    assert check_collisions(eligible, "ECON") == {}


# ── lock_subject (core write path) ────────────────────────────────────────────

def test_clean_rows_insert_correct_count():
    """A clean, collision-free set of eligible rows produces the right row count."""
    db = _make_db()

    rows = [
        _row(1, "(a)", 1, 1, "ECON-1.6", "Point one",   marks_value="1"),
        _row(1, "(a)", 1, 2, "ECON-1.6", "Point two",   marks_value="2"),
        _row(1, "(b)", 1, 1, "ECON-1.8", "Point three", marks_value="1"),
    ]
    eligible, _ = partition_rows(rows)
    assert check_collisions(eligible, "ECON") == {}

    written = lock_subject(db, eligible, "Economics", "test.pdf", "90-128")
    assert written == 3

    count = db.execute("SELECT COUNT(*) FROM mark_points").fetchone()[0]
    assert count == 3


def test_mark_points_content_is_correct():
    """Spot-check that mark_point_id, objective_id, question_id are correct."""
    db = _make_db()
    rows = [_row(2, "(c)", 1, 1, "ECON-2.2", "GDP definition", marks_value="2")]
    eligible, _ = partition_rows(rows)
    lock_subject(db, eligible, "Economics", "test.pdf", "90-128")

    row = db.execute(
        "SELECT * FROM mark_points WHERE mark_point_id = 'ECON-2.2-qb2(c)v1-mp1'"
    ).fetchone()
    assert row is not None
    row = dict(zip([d[0] for d in db.execute(
        "SELECT * FROM mark_points WHERE mark_point_id = 'ECON-2.2-qb2(c)v1-mp1'"
    ).description],
    db.execute(
        "SELECT * FROM mark_points WHERE mark_point_id = 'ECON-2.2-qb2(c)v1-mp1'"
    ).fetchone()))
    assert row["objective_id"] == "ECON-2.2"
    assert row["question_id"]  == "ECON-qb2(c)v1"
    assert row["point_text"]   == "GDP definition"
    assert row["marks_value"]  == 2
    assert row["point_order"]  == 1
    assert row["doc_id"] is None


def test_idempotent_second_run():
    """Running lock_subject twice must not double the row count."""
    db = _make_db()

    rows = [
        _row(1, "(a)", 1, 1, "ECON-1.6", "Point A"),
        _row(1, "(b)", 1, 1, "ECON-1.8", "Point B"),
    ]
    eligible, _ = partition_rows(rows)

    written1 = lock_subject(db, eligible, "Economics", "test.pdf", "90-128")
    count_after_first = db.execute("SELECT COUNT(*) FROM mark_points").fetchone()[0]

    written2 = lock_subject(db, eligible, "Economics", "test.pdf", "90-128")
    count_after_second = db.execute("SELECT COUNT(*) FROM mark_points").fetchone()[0]

    assert written1 == written2 == 2
    assert count_after_first == count_after_second == 2   # INSERT OR REPLACE, not INSERT


def test_mark_scheme_locks_row_created():
    """lock_subject must write a mark_scheme_locks row for the subject."""
    db = _make_db()
    rows = [_row(1, "(a)", 1, 1, "ECON-1.6", "A point")]
    eligible, _ = partition_rows(rows)
    lock_subject(db, eligible, "Economics", "/path/to/pdf", "90-128")

    lock_row = db.execute(
        "SELECT * FROM mark_scheme_locks WHERE subject_id = 'Economics'"
    ).fetchone()
    assert lock_row is not None
    lock_dict = dict(zip(
        [d[0] for d in db.execute("SELECT * FROM mark_scheme_locks").description],
        db.execute("SELECT * FROM mark_scheme_locks WHERE subject_id='Economics'").fetchone()
    ))
    assert lock_dict["source_pdf"] == "/path/to/pdf"
    assert lock_dict["page_range"] == "90-128"
    assert lock_dict["row_count"]  == 1


def test_unknown_objective_raises_before_write():
    """lock_subject must raise ValueError if an objective_id is not in the DB."""
    db = _make_db()
    rows = [_row(1, "(a)", 1, 1, "ECON-9.99", "Phantom point")]
    eligible, _ = partition_rows(rows)

    with pytest.raises(ValueError, match="unknown objective_id"):
        lock_subject(db, eligible, "Economics", "", "")

    # No partial write
    count = db.execute("SELECT COUNT(*) FROM mark_points").fetchone()[0]
    assert count == 0


# ── point_group_id fanout ──────────────────────────────────────────────────────

def test_fanout_multi_objective_row():
    """A row with N comma-separated objective_ids inserts N mark_points rows,
    all sharing one point_group_id, with unique mark_point_ids."""
    db = _make_db()

    # One source row mapping to three objectives (mirrors block-7 situation)
    rows = [_row(7, "(b)", 1, 1, "ECON-6.9,ECON-6.11,ECON-6.12",
                 "Definition of economic development")]
    eligible, _ = partition_rows(rows)
    assert check_collisions(eligible, "ECON") == {}

    written = lock_subject(db, eligible, "Economics", "test.pdf", "90-128")
    assert written == 3  # one per objective

    mp_rows = db.execute(
        "SELECT mark_point_id, objective_id, point_group_id "
        "FROM mark_points ORDER BY objective_id"
    ).fetchall()
    assert len(mp_rows) == 3

    # All three share the same point_group_id
    pgids = {r[2] for r in mp_rows}
    assert len(pgids) == 1
    expected_pgid = build_point_group_id("ECON", "7", "(b)", "1", "1")
    assert expected_pgid in pgids

    # Each has a distinct mark_point_id
    mpids = {r[0] for r in mp_rows}
    assert len(mpids) == 3

    # Objectives covered
    obj_ids = {r[1] for r in mp_rows}
    assert obj_ids == {"ECON-6.9", "ECON-6.11", "ECON-6.12"}


def test_fanout_idempotent_point_group_id():
    """Running lock_subject twice produces identical point_group_id values (idempotency)."""
    db = _make_db()
    rows = [
        _row(7, "(b)", 1, 1, "ECON-6.9,ECON-6.11,ECON-6.12", "Point A"),
        _row(7, "(c)", 1, 1, "ECON-1.6", "Point B"),  # single-objective
    ]
    eligible, _ = partition_rows(rows)

    lock_subject(db, eligible, "Economics", "test.pdf", "90-128")
    pgids_run1 = {
        r[0] for r in db.execute("SELECT point_group_id FROM mark_points").fetchall()
    }

    # Second lock replaces rows via INSERT OR REPLACE — group ids must be unchanged
    lock_subject(db, eligible, "Economics", "test.pdf", "90-128")
    pgids_run2 = {
        r[0] for r in db.execute("SELECT point_group_id FROM mark_points").fetchall()
    }

    assert pgids_run1 == pgids_run2
    assert len(pgids_run2) == 2  # one group for the 3-obj fanout, one for the single


def test_single_objective_row_gets_point_group_id():
    """A single-objective row still receives a point_group_id (not NULL)."""
    db = _make_db()
    rows = [_row(1, "(a)", 1, 1, "ECON-1.6", "Single point")]
    eligible, _ = partition_rows(rows)
    lock_subject(db, eligible, "Economics", "test.pdf", "90-128")

    pgid = db.execute("SELECT point_group_id FROM mark_points").fetchone()[0]
    assert pgid is not None
    expected = build_point_group_id("ECON", "1", "(a)", "1", "1")
    assert pgid == expected


def _make_full_db() -> sqlite3.Connection:
    """In-memory DB built from the canonical schema.sql + sqlite-vec.

    Used by tests that call apply_runtime_migrations (which requires the full
    table set; the minimal _make_db() lacks chunks, documents, etc.).
    """
    try:
        import sqlite_vec as _sv
    except ImportError:
        pytest.skip("sqlite-vec not installed")
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.enable_load_extension(True)
    _sv.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    for stmt in _SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        if stmt.strip():
            db.execute(stmt)
    db.commit()
    return db


def test_second_lock_with_different_content_leaves_no_stale_orphans():
    """Locking twice with a different eligible row set must produce only the
    FINAL batch — no accumulation of rows from both runs.

    Proves that delete-before-insert is atomic: after the second lock the DB
    contains ONLY the second batch's rows, not the union of both batches.  This
    is the invariant that prevents the 164-stale-row bug: if the mark_point_id
    formula changes between runs, INSERT OR REPLACE alone cannot clean up the
    old-format rows because the PKs differ.  Only the pre-insert DELETE guarantees
    a clean slate every time.
    """
    db = _make_db()

    # First lock: one point for ECON-1.6
    first_batch = [_row(1, "(a)", 1, 1, "ECON-1.6", "First batch point")]
    eligible1, _ = partition_rows(first_batch)
    lock_subject(db, eligible1, "Economics", "test.pdf", "90-128")
    assert db.execute("SELECT COUNT(*) FROM mark_points").fetchone()[0] == 1
    mpids_first = {r[0] for r in db.execute("SELECT mark_point_id FROM mark_points").fetchall()}

    # Second lock: completely different rows — different block, part, objective
    second_batch = [
        _row(2, "(b)", 1, 1, "ECON-1.8", "Second batch point A"),
        _row(2, "(b)", 1, 2, "ECON-2.2", "Second batch point B"),
    ]
    eligible2, _ = partition_rows(second_batch)
    lock_subject(db, eligible2, "Economics", "test.pdf", "90-128")

    count_after_second = db.execute("SELECT COUNT(*) FROM mark_points").fetchone()[0]
    mpids_second = {r[0] for r in db.execute("SELECT mark_point_id FROM mark_points").fetchall()}

    # Must have exactly 2 rows — ONLY the second batch, not 3 (union of both)
    assert count_after_second == 2, (
        f"Expected 2 rows after re-lock, got {count_after_second}: {sorted(mpids_second)}"
    )
    # The first batch's row must be gone
    assert not mpids_first & mpids_second, (
        f"Stale rows from first lock still present: {mpids_first & mpids_second}"
    )


def test_worked_solution_rows_with_doc_id_survive_a_relock():
    """Regression guard: lock_subject's DELETE must never touch a row from a
    DIFFERENT pipeline that happens to share an objective_id with this subject.

    Confirmed real case: Principles_of_Business carries 2447 pre-existing
    mark_points rows from ingest_solutions.py's worked-solutions answer bank,
    each with a real doc_id (e.g. 'sol-fa67a890f043'). The original DELETE
    scoped only by objective ownership matched those rows too, so the very
    first mark-scheme-CSV lock for POB would have silently deleted the entire
    answer bank. This row is inserted directly (bypassing lock_subject, since
    that function only ever writes doc_id=NULL) to simulate that population,
    then a real lock_subject call for the SAME subject/objective must leave it
    untouched -- doc_id IS NULL is the guard that makes this safe regardless
    of which subject is being locked.
    """
    db = _make_db()
    db.execute("""
        INSERT INTO mark_points
            (mark_point_id, objective_id, question_id, doc_id, point_text, marks_value, point_order)
        VALUES ('ECON-2010Jan-P2-q2b-mp1', 'ECON-1.6', 'ECON-2010Jan-P2-q2b-stem',
                'sol-fa67a890f043', 'Worked-solution answer', 1, 1)
    """)
    db.commit()

    rows = [_row(1, "(a)", 1, 1, "ECON-1.6", "New specimen point")]
    eligible, _ = partition_rows(rows)
    lock_subject(db, eligible, "Economics", "test.pdf", "90-128")

    survivor = db.execute(
        "SELECT * FROM mark_points WHERE mark_point_id = 'ECON-2010Jan-P2-q2b-mp1'"
    ).fetchone()
    assert survivor is not None, "worked-solution row was wrongly deleted by the lock"

    new_row = db.execute(
        "SELECT * FROM mark_points WHERE mark_point_id = 'ECON-1.6-qb1(a)v1-mp1'"
    ).fetchone()
    assert new_row is not None, "the new specimen row should still be inserted normally"

    total = db.execute("SELECT COUNT(*) FROM mark_points").fetchone()[0]
    assert total == 2, f"expected worked-solution row + new specimen row = 2, got {total}"


# ── stem-suffix regression guard ──────────────────────────────────────────────

def test_question_ids_normalised_to_stem_after_lock_and_migration():
    """Regression guard: the lock pipeline must leave all question_ids with '-stem'.

    build_question_id always produces 'ECON-qb{block}{part}v{occ}' — never with
    the '-stem' suffix (confirmed from its source on line 61-62 of lock_mark_scheme.py).
    lock_subject does a full DELETE+reinsert using that formatter, so every lock run
    produces fresh rows WITHOUT '-stem', regardless of any prior migration call.

    Before the fix, main() called apply_runtime_migrations only ONCE — before
    lock_subject.  That pre-lock call normalised PRE-EXISTING rows but could not
    touch rows that did not exist yet.  After lock_subject ran, all 552 live ECON
    rows lacked '-stem' (confirmed: With-stem 0 / Without-stem 552 on re-lock).

    The fix adds a second apply_runtime_migrations call AFTER lock_subject in main().

    Part A proves the bug state: lock_subject alone writes question_ids WITHOUT '-stem'.
    Part B proves the fix: apply_runtime_migrations called after lock_subject normalises them.

    If main() is ever reverted to a single pre-lock migration call, every re-lock will
    leave the live DB in the Part-A state — the quiz picker (/api/questions uses
    'chunk_id LIKE %%-stem') will stop matching mark_points, and grading will silently
    return no mark points for every Economics question.
    """
    import app as app_module  # loaded via sys.path insert for backend/

    db = _make_full_db()
    db.execute("INSERT INTO subjects VALUES ('Economics', 'Economics', 1)")
    db.execute("INSERT INTO syllabus_sections VALUES ('ECON-S1', 'Economics', 'Section 1', '1')")
    db.execute(
        "INSERT INTO objectives VALUES "
        "('ECON-1.6', 'ECON-S1', 'Economics', '1.6', 'PPC', NULL, NULL, NULL, 0)"
    )
    db.execute(
        "INSERT INTO objectives VALUES "
        "('ECON-1.8', 'ECON-S1', 'Economics', '1.8', 'Comparative advantage', NULL, NULL, NULL, 0)"
    )
    db.commit()

    rows = [
        _row(1, "(a)(i)", 1, 1, "ECON-1.6", "First mark point"),
        _row(1, "(b)",    1, 1, "ECON-1.8", "Second mark point"),
    ]
    eligible, _ = partition_rows(rows)
    lock_subject(db, eligible, "Economics", "test.pdf", "90-128")

    # Part A — the bug: lock_subject alone writes question_ids WITHOUT '-stem'
    qids_raw = [
        r["question_id"]
        for r in db.execute("SELECT question_id FROM mark_points").fetchall()
    ]
    assert len(qids_raw) == 2
    assert all(not qid.endswith("-stem") for qid in qids_raw), (
        f"Expected no -stem immediately after lock_subject, got: {qids_raw}"
    )

    # Part B — the fix: apply_runtime_migrations after lock_subject normalises them
    # (this is what the second migration call in main() now does automatically)
    app_module.apply_runtime_migrations(db)
    qids_fixed = [
        r["question_id"]
        for r in db.execute("SELECT question_id FROM mark_points").fetchall()
    ]
    assert all(qid.endswith("-stem") for qid in qids_fixed), (
        f"question_ids still missing '-stem' after apply_runtime_migrations: {qids_fixed}"
    )


# ── Rule 2: source_page guard ──────────────────────────────────────────────────

def test_empty_source_page_is_refused():
    """A row destined for mark_points with empty source_page must be caught.

    Rule 2: every mark point must cite its source page.  check_null_source_pages
    returns the offending rows so the caller (main()) can refuse before any write.
    """
    rows = [
        _row(1, "(a)", 1, 1, "ECON-1.6", source_page="90"),   # fine
        _row(2, "(b)", 1, 1, "ECON-1.8", source_page=""),     # violates Rule 2
    ]
    bad = check_null_source_pages(rows, "ECON")
    assert len(bad) == 1
    assert bad[0]["question_block_id"] == "2"


def test_null_source_page_skipped_for_artifacts_and_excluded():
    """Rows that are artifacts or excluded are not eligible — skip the source_page check."""
    rows = [
        _row(1, "(a)", 1, 1, "ECON-1.6", source_page="", parser_artifact="1"),
        _row(2, "(b)", 1, 1, "ECON-1.6", source_page="", excluded_reason="dup"),
        _row(3, "(c)", 1, 1, "ECON-1.6", source_page="", needs_manual_entry="1"),
    ]
    bad = check_null_source_pages(rows, "ECON")
    assert bad == []


# ── parser_artifact / excluded_reason mutual exclusivity ──────────────────────

def test_check_overlapping_classification_detects_both_flags_set():
    rows = [
        _row(1, "(a)", 1, 1, "ECON-1.6"),  # fine
        _row(2, "(b)", 1, 1, "ECON-1.6", parser_artifact="1",
             excluded_reason="contaminated_exam_instructions"),  # overlap
    ]
    overlapping = check_overlapping_classification(rows)
    assert len(overlapping) == 1
    assert overlapping[0]["question_block_id"] == "2"


def test_check_overlapping_classification_clean_when_mutually_exclusive():
    rows = [
        _row(1, "(a)", 1, 1, "ECON-1.6", parser_artifact="1"),
        _row(2, "(b)", 1, 1, "ECON-1.6", excluded_reason="duplicate_of_block_8-15"),
        _row(3, "(c)", 1, 1, "ECON-1.6"),
    ]
    assert check_overlapping_classification(rows) == []


def test_partition_rows_raises_on_overlapping_classification():
    """partition_rows must refuse to proceed -- not silently pick a winner --
    when a row has both parser_artifact=1 and excluded_reason set."""
    rows = [
        _row(1, "(a)", 1, 1, "ECON-1.6", parser_artifact="1",
             excluded_reason="contaminated_exam_instructions",
             point_text="Total"),
    ]
    with pytest.raises(ValueError, match="parser_artifact=1 AND excluded_reason"):
        partition_rows(rows)


# ── Gap 1: --paper / resolve_paper_pages() CSV-filename resolution ────────────

def test_resolve_paper_pages_economics_single_paper_regression():
    """Economics has a single top-level 'pages' key -- paper_key must be None
    regardless of --paper, so main()'s csv_name stays the unsuffixed
    '{subject}_mark_scheme_review.csv' convention it has always used."""
    econ_entry = {
        "pdf": "econ.pdf",
        "pages": [90, 128],
    }
    pages, paper_key = resolve_paper_pages(econ_entry, "Economics", None)
    assert pages == [90, 128]
    assert paper_key is None


def test_resolve_paper_pages_pob_multi_paper_selects_named_paper():
    """POB has paper_02/paper_032 sub-keys -- passing --paper paper_02 must
    resolve to that paper's own pages and return the paper key (used by
    main() to build 'Principles_of_Business_paper_02_mark_scheme_review.csv')."""
    pob_entry = {
        "pdf": "pob.pdf",
        "paper_02":  {"pages": [108, 120], "format": "question_lettered_parts"},
        "paper_032": {"pages": [130, 137], "format": "flat_numbered_items"},
    }
    pages, paper_key = resolve_paper_pages(pob_entry, "Principles_of_Business", "paper_02")
    assert pages == [108, 120]
    assert paper_key == "paper_02"


def test_resolve_paper_pages_pob_missing_paper_arg_exits():
    """A multi-paper subject invoked without --paper must exit, not silently
    pick one of the available papers."""
    pob_entry = {
        "pdf": "pob.pdf",
        "paper_02":  {"pages": [108, 120], "format": "question_lettered_parts"},
        "paper_032": {"pages": [130, 137], "format": "flat_numbered_items"},
    }
    with pytest.raises(SystemExit):
        resolve_paper_pages(pob_entry, "Principles_of_Business", None)


def test_resolve_paper_pages_no_config_entry_regression():
    """A subject entirely absent from mark_scheme_page_ranges.json (empty
    dict) must not require --paper -- matches subjects that have not yet
    been page-ranged, same as the pre-Gap-1 unconditional csv_path."""
    pages, paper_key = resolve_paper_pages({}, "SomeSubject", None)
    assert pages == [None, None]
    assert paper_key is None


# ── Gap 2: check_unmapped_eligible_rows ────────────────────────────────────────

def test_check_unmapped_eligible_rows_flags_empty_mapping():
    rows = [
        _row(1, "(a)", 1, 1, "ECON-1.6"),   # fine -- mapped
        _row(2, "(b)", 1, 1, ""),           # BLOCKS -- empty mapping, no skip flag
    ]
    bad = check_unmapped_eligible_rows(rows)
    assert len(bad) == 1
    assert bad[0]["question_block_id"] == "2"


def test_check_unmapped_eligible_rows_skips_artifact_excluded_manual():
    """Rows that are artifacts/excluded/manual are allowed an empty mapping --
    they're already routed to a skip bucket by partition_rows, never written."""
    rows = [
        _row(1, "(a)", 1, 1, "", parser_artifact="1"),
        _row(2, "(b)", 1, 1, "", excluded_reason="out_of_scope"),
        _row(3, "(c)", 1, 1, "", needs_manual_entry="1"),
    ]
    assert check_unmapped_eligible_rows(rows) == []


def test_check_unmapped_eligible_rows_all_mapped_is_clean():
    rows = [
        _row(1, "(a)", 1, 1, "ECON-1.6"),
        _row(2, "(b)", 1, 1, "ECON-1.8"),
    ]
    assert check_unmapped_eligible_rows(rows) == []


def test_partition_rows_would_silently_lose_unmapped_eligible_row():
    """Confirms the exact bug check_unmapped_eligible_rows guards against: an
    empty mapped_objective_id with no skip flag reaches 'eligible' from
    partition_rows, but _all_objs('') is [] so lock_subject's per-objective
    insert loop for that row runs zero times -- this is why the new gate must
    run BEFORE lock_subject is ever called."""
    rows = [_row(1, "(a)", 1, 1, "")]
    eligible, skip_counts = partition_rows(rows)
    assert len(eligible) == 1  # silently counted eligible
    assert _all_objs(eligible[0]["mapped_objective_id"]) == []  # writes nothing


# ── Gap 3: mark_group_id / group_max_marks written to mark_points ─────────────

def test_build_mark_group_id_prefixes_raw_value():
    assert build_mark_group_id("ECON", "4(c)(i)-g1") == "ECON-4(c)(i)-g1"


def test_build_mark_group_id_empty_is_none():
    assert build_mark_group_id("ECON", "") is None


def test_mark_group_id_and_group_max_marks_written_on_locked_row():
    """A row carrying real group fields must write them through to mark_points."""
    db = _make_db()
    rows = [_row(4, "(c)(i)", 1, 1, "ECON-1.6", "Grouped point",
                 marks_value="1", mark_group_id="4(c)(i)-g1", group_max_marks="4")]
    eligible, _ = partition_rows(rows)
    lock_subject(db, eligible, "Economics", "test.pdf", "90-128")

    row = db.execute(
        "SELECT mark_group_id, group_max_marks FROM mark_points"
    ).fetchone()
    assert row[0] == "ECON-4(c)(i)-g1"
    assert row[1] == 4


def test_mark_group_id_shared_across_fanned_out_rows():
    """A grouped row that fans out to multiple objectives must carry the SAME
    mark_group_id/group_max_marks on every fanned sibling row."""
    db = _make_db()
    rows = [_row(7, "(b)", 1, 1, "ECON-6.9,ECON-6.11", "Grouped fanout point",
                 mark_group_id="7(b)-g1", group_max_marks="3")]
    eligible, _ = partition_rows(rows)
    lock_subject(db, eligible, "Economics", "test.pdf", "90-128")

    rows_out = db.execute(
        "SELECT mark_group_id, group_max_marks FROM mark_points"
    ).fetchall()
    assert len(rows_out) == 2
    assert all(r[0] == "ECON-7(b)-g1" for r in rows_out)
    assert all(r[1] == 3 for r in rows_out)


def test_economics_shaped_rows_with_no_group_columns_lock_unchanged():
    """Regression guard: a CSV row with NO mark_group_id/group_max_marks
    columns at all (the shape of every one of Economics' 552 already-locked
    rows, extracted before the m021 grouping work existed) must still lock
    cleanly, writing NULL for both new columns and leaving every other
    column exactly as it was before this task."""
    db = _make_db()
    row = _row(2, "(c)", 1, 1, "ECON-2.2", "GDP definition", marks_value="2")
    del row["mark_group_id"]
    del row["group_max_marks"]
    eligible, _ = partition_rows([row])
    written = lock_subject(db, eligible, "Economics", "test.pdf", "90-128")
    assert written == 1

    result = db.execute(
        "SELECT objective_id, question_id, point_text, marks_value, "
        "point_order, mark_group_id, group_max_marks FROM mark_points"
    ).fetchone()
    assert result[0] == "ECON-2.2"
    assert result[1] == "ECON-qb2(c)v1"
    assert result[2] == "GDP definition"
    assert result[3] == 2
    assert result[4] == 1
    assert result[5] is None
    assert result[6] is None
